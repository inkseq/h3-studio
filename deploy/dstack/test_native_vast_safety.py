"""Offline native boundaries; optional cached upstream bytes never contact APIs.

Run the actual pinned API/compute/allocator fragments with --upstream-cache DIR
and --public-cache DIR. Ordinary discovery also runs all pure HTTP/proof tests.
"""
import argparse
import ast
import asyncio
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import MethodType, SimpleNamespace as NS
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).parent
CACHES = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


safety, installer = load("native_vast_safety"), load("patch_native_vast")


def response(status, data, headers=None):
    return NS(status_code=status, headers=headers or {}, json=Mock(return_value=data), close=Mock())


def unavailable(ask_id):
    return {"success": False, "error": "invalid_args", "ask_id": int(ask_id),
            "msg": f"error 404/3603: no_such_ask Instance type by id {ask_id} is not available."}


def attempt(ask, error):
    return NS(backend="vastai", instance=str(ask), error=safety.closed_error(ask, error))


def proof(errors, tried=None):
    value = safety.failure_message(NS(errors=errors, tried=len(errors) if tried is None else tried))
    return json.loads(value[len(safety.ATTEMPTS_PREFIX):])


class NativeVastSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.
        safety._cooldown_until = 0.
        clock = patch.object(safety.time, "monotonic", side_effect=lambda: self.now)
        clock.start();self.addCleanup(clock.stop)
    def advance(self, seconds):self.now += seconds

    def test_read_429_recovers_with_bounded_jitter(self):
        call = Mock(side_effect=[response(429, {}, {"Retry-After": "5"}), response(429, {}), response(200, {"instances": []})])
        with patch.object(safety.time, "sleep", side_effect=self.advance) as sleep:
            self.assertEqual(safety.read_request(call).status_code, 200)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(len(sleep.call_args_list), 2)
        self.assertTrue(all(0 < value.args[0] <= 30 for value in sleep.call_args_list))
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], 5)

    def test_long_numeric_or_http_date_defers_without_an_early_next_request(self):
        from email.utils import formatdate
        for header in ["999", formatdate(2999, usegmt=True), "not-a-valid-delay"]:
            safety._cooldown_until = 0.
            with self.subTest(header=header), patch.object(safety.time, "time", return_value=2000), patch.object(safety.time, "sleep") as sleep:
                call = Mock(return_value=response(429, {}, {"Retry-After": header}))
                with self.assertRaises(safety.VastReadRateLimited) as limited:safety.read_request(call)
                self.assertEqual(call.call_count, 1)
                minimum = 999 if header != "not-a-valid-delay" else 30
                self.assertGreaterEqual(limited.exception.retry_after, minimum)
                with self.assertRaises(safety.VastReadRateLimited):safety.read_request(call)
                self.assertEqual(call.call_count, 1)  # Later native ticks obey the same cooldown.
                blocked = NS(put=Mock())
                with self.assertRaises(safety.VastReadRateLimited):safety.create_instance_once(blocked, 123, {})
                blocked.put.assert_not_called()
                safety._cooldown_until = 0.
                session = NS(put=Mock(return_value=response(429, {"success": False}, {"Retry-After": header})))
                with self.assertRaises(safety.VastSafeReject) as caught:safety.create_instance_once(session, 123, {})
                self.assertNotIsInstance(caught.exception, safety.VastAllocationUnknown)
                self.assertEqual(caught.exception.outcome["code"], "rate_limited")
                self.assertTrue(caught.exception.sixnine_vast_stop_fallback)
                self.assertEqual(session.put.call_count, 1)
                self.assertEqual(proof([attempt(123, caught.exception)])["outcome"], "no_allocation")
                sleep.assert_not_called()
        with patch.object(safety.time, "time", return_value=2000):
            self.assertGreaterEqual(safety._delay(response(429, {}, {"Retry-After": formatdate(2005, usegmt=True)})), 5)

    def test_exhausted_read_429_is_not_an_empty_offer_list(self):
        call = Mock(return_value=response(429, {"offers": []}))
        with patch.object(safety.time, "sleep", side_effect=self.advance), self.assertRaisesRegex(safety.VastReadRateLimited, "sixnine_vast_read_rate_limited"):
            safety.read_request(call)
        self.assertEqual(call.call_count, 3)

    def test_only_explicit_nonallocation_rejections_allow_next_offer(self):
        for status, data, expected_code in [
                (400, {"success": False, "error": "invalid_args", "ask_id": 123}, "invalid_args"),
                (404, {"success": False, "error": "no_such_ask"}, "no_such_ask"),
                (404, unavailable(123), "no_such_ask"),
                (410, {"success": False, "error": "no_such_ask", "ask_id": "123"}, "no_such_ask"),
                (429, {"success": False}, "rate_limited"),
                (429, {"detail": "API requests too frequent endpoint threshold=4.5"}, "rate_limited"),
                (429, {"detail": "API requests too frequent endpoint threshold=7.25"}, "rate_limited")]:
            with self.subTest(status=status), patch.object(safety.time, "sleep", side_effect=self.advance):
                session = NS(put=Mock(return_value=response(status, data)))
                with self.assertRaises(safety.VastSafeReject) as caught:
                    safety.create_instance_once(session, "123", {"label": "original"})
                self.assertNotIsInstance(caught.exception, safety.VastAllocationUnknown)
                self.assertEqual(caught.exception.outcome["code"], expected_code)
                self.assertTrue(safety.safe_for_fallback(safety.closed_error("123", caught.exception)))
                session.put.assert_called_once_with("/v0/asks/123/", json={"label": "original"})

    def test_unknown_put_or_returned_contract_never_retries_or_cleans_up(self):
        outcomes = [TimeoutError("private https://secret.invalid/token"), response(500, {"success": False}),
                    response(200, {"success": False, "new_contract": 555}), response(429, {"success": False, "new_contract": 555}),
                    response(200, {"success": True}), response(404, {"success": False, "error": "different"}),
                    response(429, []), response(429, {"success": False, "new_contract": 0}),
                    response(400, {"success": False, "error": "invalid_price"}),
                    response(400, {"success": False, "error": "no_ssh_key_for_vm"}),
                    response(400, {"success": False, "error": "invalid_args", "ask_id": 124}),
                    response(400, {"success": False, "error": "invalid_args", "ask_id": True}),
                    response(400, {"success": False, "error": "invalid_args", "ask_id": None}),
                    response(400, {"success": False, "error": "invalid_args", "new_contract": 555}),
                    response(404, {**unavailable(123), "ask_id": 124}),
                    response(404, {**unavailable(124), "ask_id": 123}),
                    response(404, {**unavailable(123), "msg": "error404/3603: no_such_ask"}),
                    response(404, {"success": False, "error": 3603, "msg": "no_such_ask"}),
                    response(410, {"success": False, "error": "no_such_ask", "ask_id": "0123"}),
                    response(410, {"success": False, "error": "no_such_ask", "new_contract": 555}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=4.5 extra"}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=0"}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=-1"}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=nan"}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=inf"}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=1000001"}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=4.5", "extra": True}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=4.5", "success": True}),
                    response(429, {"detail": "API requests too frequent endpoint threshold=4.5", "new_contract": 555}),
                    response(500, unavailable(123)), response(200, {"success": True, "new_contract": 555, "ask_id": 124})]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                session = NS(put=Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome), delete=Mock())
                with self.assertRaises(safety.VastAllocationUnknown) as caught:
                    safety.create_instance_once(session, 123, {})
                self.assertEqual(session.put.call_count, 1)
                session.delete.assert_not_called()
                self.assertFalse(safety.safe_for_fallback(safety.closed_error(123, caught.exception)))
                self.assertNotIn("secret", str(caught.exception))

    def test_invalid_args_diagnostic_accepts_only_closed_documented_details(self):
        for message, expected in [
                ("error 400/3471: Invalid args: len(args) > 4096", (3471, "args", "length_limit")),
                ("Invalid args: len(args) > 8192", (None, "args", "length_limit")),
                ("error 400/3467: Invalid args: 'id' is required and must be a valid offer ID", (3467, "id", "required_or_invalid")),
                ("Invalid args: 'id' is required and must be a valid offer ID", (None, "id", "required_or_invalid")),
                ("Invalid args: disk size is too large: requested PRIVATE", (None, "disk", "out_of_range")),
                ("Invalid disk size: PRIVATE", (None, "disk", "invalid")),
                ("Missing template_id: PRIVATE", (None, "template_id", "required")),
                ("Invalid args: 'image_login' is required: Bearer PRIVATE", (None, "image_login", "required")),
                ("error 400/3499: Invalid args: env is not supported: PRIVATE", (3499, "env", "unsupported")),
                ("Unsupported runtype: PRIVATE", (None, "runtype", "unsupported")),
                ("Invalid PRIVATE: disk is too large", (None, "unknown", "unspecified")),
                ("error 400/3499: Invalid args: env contains Bearer PRIVATE", (3499, "unknown", "unspecified")),
                ("error 400/3499: PRIVATE nonstandard provider detail", (3499, "unknown", "unspecified")),
                ("error 400/3471: Invalid args: len(args) > 4096 https://private.invalid/secret", (3471, "unknown", "unspecified")),
                ("error 400/3471: Invalid args: len(args) > 4096\nBearer PRIVATE", (None, "unknown", "unspecified")),
                ("Invalid args: len(args) > 4096\nBearer PRIVATE", (None, "unknown", "unspecified")),
                ("Invalid args: len(args) > 4096 https://private.invalid/secret", (None, "unknown", "unspecified")),
                ("Invalid args: len(args) > 0", (None, "unknown", "unspecified")),
                ("Bearer PRIVATE error 400/3471: Invalid args: len(args) > 4096", (None, "unknown", "unspecified")),
                ("error 404/3471: Invalid args: len(args) > 4096", (None, "unknown", "unspecified")),
                ("error 400/347100000000000: Invalid args: len(args) > 4096", (None, "unknown", "unspecified")),
                ("error 400/3471: Invalid args: " + "PRIVATE" * 400, (None, "unknown", "unspecified")),
                ({"msg": "PRIVATE"}, (None, "unknown", "unspecified")),
                (None, (None, "unknown", "unspecified"))]:
            with self.subTest(expected=expected):
                self.assertEqual(safety._invalid_args_diagnostic(message), expected)

    def test_argument_metadata_counts_only_utf8_bytes_without_stringifying_other_values(self):
        class Private:
            def __str__(self):raise AssertionError("must not stringify private values")
        for value, expected in [(["PRIVATE", "\u4e2d"], (2, 10, 7)), ("PRIVATE\u00e9", (1, 9, 9)),
                                ([], (0, 0, 0)), (["\ud800"], (1, None, None)),
                                ([Private()], (None, None, None)), ({"PRIVATE": "value"}, (None, None, None)),
                                (None, (None, None, None))]:
            with self.subTest(expected=expected):
                self.assertEqual(safety._argument_sizes({"args": value}), expected)
        self.assertEqual(safety._field_shape({"msg": Private()}, "msg"), ("other", None))

    def test_flat_message_variants_log_closed_sizes_and_keep_the_exact_original_put(self):
        for message_field in ("msg", "message", "detail"):
            data = {"success": False, "error": "invalid_args", "ask_id": 123,
                    message_field: "Invalid args: len(args) > 8192", "echoed_payload": "PRIVATE"}
            payload = {"args": ["PRIVATE", "\u4e2d"], "env": {"TOKEN": "PRIVATE"}}
            original = copy.deepcopy(payload)
            session = NS(put=Mock(return_value=response(400, data)))
            with self.subTest(message_field=message_field), self.assertLogs(safety._logger, level="WARNING") as logs:
                with self.assertRaises(safety.VastSafeReject) as caught:
                    safety.create_instance_once(session, 123, payload)
            self.assertNotIsInstance(caught.exception, safety.VastAllocationUnknown)
            self.assertEqual(set(caught.exception.outcome), safety.OUTCOME_KEYS)
            self.assertIn("provider_subcode=None field=args reason=length_limit", logs.output[0])
            self.assertIn("args_type=array args_count=2 args_total_bytes=10 args_max_bytes=7", logs.output[0])
            self.assertIn(message_field + "_type=string " + message_field + "_length=30", logs.output[0])
            for private in ("PRIVATE", "TOKEN", "8192", "Invalid args", "echoed_payload"):
                self.assertNotIn(private, logs.output[0])
            session.put.assert_called_once_with("/v0/asks/123/", json=original)
            self.assertIs(session.put.call_args.kwargs["json"], payload)
            self.assertEqual(payload, original)

    def test_nested_unknown_message_shapes_never_log_values_or_authorize_allocation_proof(self):
        data = {"success": False, "error": "invalid_args", "msg": {"message": "PRIVATE"},
                "message": ["Invalid args: len(args) > 8192"], "detail": "Bearer PRIVATE"}
        with self.assertLogs(safety._logger, level="WARNING") as logs:
            with self.assertRaises(safety.VastSafeReject):
                safety.create_instance_once(NS(put=Mock(return_value=response(400, data))), 123, {"args": "PRIVATE"})
        self.assertIn("field=unknown reason=unspecified", logs.output[0])
        self.assertIn("msg_type=object msg_length=None message_type=array message_length=None", logs.output[0])
        self.assertNotIn("PRIVATE", logs.output[0])
        self.assertNotIn("8192", logs.output[0])
        # A flat error detail is diagnostic text, never a new trusted refusal code.
        for field in ("error", "message"):
            unproven = {"success": False, field: "Invalid args: len(args) > 8192"}
            with self.subTest(field=field), patch.object(safety._logger, "warning") as log:
                with self.assertRaises(safety.VastAllocationUnknown):
                    safety.create_instance_once(NS(put=Mock(return_value=response(400, unproven))), 123, {})
                log.assert_not_called()

    def test_rejected_create_logs_no_body_or_payload_and_preserves_original_proof(self):
        for message, expected in [
                ("error 400/3471: Invalid args: len(args) > 4096", "provider_subcode=3471 field=args reason=length_limit"),
                ("error 400/3499: Invalid args: PRIVATE https://private.invalid/secret", "provider_subcode=3499 field=unknown reason=unspecified")]:
            data = {"success": False, "error": "invalid_args", "ask_id": 123,
                    "msg": message, "echoed_payload": {"env": "PRIVATE"}}
            received = response(400, data)
            session = NS(put=Mock(return_value=received), delete=Mock())
            with self.subTest(message=expected), self.assertLogs(safety._logger, level="WARNING") as logs:
                with self.assertRaises(safety.VastSafeReject) as caught:
                    safety.create_instance_once(session, 123, {"image_login": "PRIVATE", "args": ["PRIVATE"]})
            self.assertNotIsInstance(caught.exception, safety.VastAllocationUnknown)
            self.assertEqual(caught.exception.outcome, {"ask_id": "123", "http_status": 400,
                "code": "invalid_args", "outcome": "rejected", "contract_id": None})
            self.assertEqual(proof([attempt(123, caught.exception)])["outcome"], "no_allocation")
            self.assertEqual(set(caught.exception.outcome), safety.OUTCOME_KEYS)
            self.assertEqual(len(logs.output), 1)
            self.assertIn(expected, logs.output[0])
            for secret in ("PRIVATE", "https://", "echoed_payload", "image_login"):
                self.assertNotIn(secret, logs.output[0])
                self.assertNotIn(secret, str(caught.exception))
            session.put.assert_called_once()
            session.delete.assert_not_called()
            received.close.assert_called_once()

    def test_argument_diagnostic_logs_static_reason_and_public_disk_size_only(self):
        for disk, expected in ((128, "128"), (128.5, "128.5"), (True, "None"),
                               ("PRIVATE", "None"), (float("nan"), "None"),
                               (float("inf"), "None"), (10 ** 400, "None")):
            data = {"success": False, "error": "invalid_args", "ask_id": 123,
                    "msg": "Invalid args: disk size is too large: PRIVATE https://private.invalid/token"}
            with self.subTest(disk_type=type(disk).__name__), self.assertLogs(safety._logger, level="WARNING") as logs:
                with self.assertRaises(safety.VastSafeReject) as caught:
                    safety.create_instance_once(NS(put=Mock(return_value=response(400, data))), 123,
                                                {"disk": disk, "env": {"TOKEN": "PRIVATE"}})
            self.assertIn("field=disk reason=out_of_range", logs.output[0])
            self.assertIn("disk_gb=" + expected, logs.output[0])
            self.assertNotIn("PRIVATE", logs.output[0])
            self.assertNotIn("https://", logs.output[0])
            self.assertEqual(set(caught.exception.outcome), safety.OUTCOME_KEYS)
            self.assertEqual(proof([attempt(123, caught.exception)])["outcome"], "no_allocation")

    def test_logging_failure_and_ambiguous_errors_cannot_change_allocation_classification(self):
        data = {"success": False, "error": "invalid_args", "ask_id": 123,
                "msg": "error 400/3471: Invalid args: len(args) > 4096"}
        with patch.object(safety._logger, "warning", side_effect=RuntimeError("PRIVATE")):
            with self.assertRaises(safety.VastSafeReject) as caught:
                safety.create_instance_once(NS(put=Mock(return_value=response(400, data))), 123, {})
        self.assertNotIsInstance(caught.exception, safety.VastAllocationUnknown)
        self.assertEqual(proof([attempt(123, caught.exception)])["outcome"], "no_allocation")
        for change in ({"new_contract": 555}, {"ask_id": 124}, {"success": True}):
            with self.subTest(change=change), patch.object(safety._logger, "warning") as log:
                session = NS(put=Mock(return_value=response(400, {**data, **change})))
                with self.assertRaises(safety.VastAllocationUnknown) as unknown:
                    safety.create_instance_once(session, 123, {})
                log.assert_not_called()
                self.assertFalse(safety.safe_for_fallback(safety.closed_error(123, unknown.exception)))
                session.put.assert_called_once()

    def test_success_contract_is_original_even_when_connection_close_fails(self):
        received = response(200, {"success": True, "new_contract": 555})
        received.close.side_effect = OSError("closed")
        session = NS(put=Mock(return_value=received))
        self.assertEqual(safety.create_instance_once(session, 123, {}), 555)
        self.assertEqual(session.put.call_count, 1)

    def test_complete_all_safe_proof_and_legacy_or_partial_refusal(self):
        value = {"ask_id": "123", "http_status": 404, "code": "no_such_ask", "outcome": "rejected", "contract_id": None}
        genuine = safety.VastSafeReject(value)
        self.assertEqual(proof([attempt(123, genuine)])["outcome"], "no_allocation")
        for errors, tried in [([attempt(123, genuine)], 2), ([attempt(123, Exception(str(genuine)))], 1),
                              ([NS(backend="vastai", instance="123", error=str(genuine)[:70])], 1),
                              ([attempt(123, genuine)], 26), ([], 0)]:
            with self.subTest(tried=tried):
                self.assertEqual(proof(errors, tried)["outcome"], "unknown")
        self.assertEqual(proof([attempt(123, Exception("PRIVATE_PROVIDER_BODY"))])["attempts"][0]["code"], "mutation_unknown")

    def test_source_provenance_is_closed_and_complete(self):
        data = json.loads((HERE / "native-vast-provenance.json").read_text())
        self.assertEqual(data["dstack_version"], "0.22.5")
        self.assertEqual(len(data["sources"]), 6)
        for path, source in data["sources"].items():
            self.assertNotIn("..", Path(path).parts)
            self.assertRegex(source["sha256"], "^[a-f0-9]{64}$")

    def test_real_pinned_upstream_loop_and_installer(self):
        if not CACHES:
            self.skipTest("supply the reviewed source caches for native AST boundary checks")
        provenance = json.loads((HERE / "native-vast-provenance.json").read_text())
        sources = {}
        for path, expected in provenance["sources"].items():
            file = next((cache / path.replace("/", "__") for cache in CACHES if (cache / path.replace("/", "__")).is_file()), None)
            self.assertIsNotNone(file, path)
            raw = file.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected["sha256"])
            sources[path] = installer.transform(path, raw.decode())
            ast.parse(sources[path])
        compute = sources["dstack/_internal/core/backends/vastai/compute.py"]
        self.assertIn('"inet_down": {"gte": 1000}', compute)
        self.assertNotIn('"inet_down": {"gt": 128}', compute)
        native_read_guards(sources, self.advance)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in sources:
                target = root / path;target.parent.mkdir(parents=True, exist_ok=True)
                original = next(cache / path.replace("/", "__") for cache in CACHES if (cache / path.replace("/", "__")).is_file())
                target.write_bytes(original.read_bytes())
            with patch.object(installer.importlib.metadata, "version", return_value="0.22.5"):
                self.assertEqual(installer.install(root, check_only=True), {"checked_sources": 6, "writes": 0})
                before = {path: (root / path).read_bytes() for path in sources}
                first = next(iter(sources));(root / first).write_bytes(b"drift")
                with self.assertRaisesRegex(ValueError, "source_hash_mismatch"):
                    installer.install(root)
                self.assertTrue(all((root / path).read_bytes() == data for path, data in before.items() if path != first))
                (root / first).write_bytes(before[first])
                installed = installer.install(root)
                self.assertEqual(installed["patched_sources"], 6)
                with self.assertRaises(ValueError):installer.install(root)
        loop = native_loop(sources)
        for responses, expected_puts, expected in [
            ([response(404, unavailable(100)), response(429, {"detail": "API requests too frequent endpoint threshold=4.5"}), response(200, {"success": True, "new_contract": 555})], 3, "success"),
            ([response(400, {"success": False, "error": "invalid_args", "ask_id": 100}), response(410, {"success": False, "error": "no_such_ask", "ask_id": 101}), response(200, {"success": True, "new_contract": 555})], 3, "success"),
            ([TimeoutError("unknown"), response(200, {"success": True, "new_contract": 555})], 1, "unknown"),
            ([response(200, {"success": False, "new_contract": 999}), response(200, {"success": True, "new_contract": 555})], 1, "unknown"),
            ([response(429, {"success": False}, {"Retry-After": "999"}), response(200, {"success": True, "new_contract": 555})], 1, "no_allocation"),
            ([response(404, unavailable(100)), response(429, {"success": False})], 2, "no_allocation"),
            ([response(400, {"success": False, "error": "invalid_args"}), response(410, {"success": False, "error": "no_such_ask"})], 2, "no_allocation")]:
            safety._cooldown_until = 0.
            with self.subTest(expected=expected), patch.object(safety.time, "sleep", side_effect=self.advance):
                session = NS(put=Mock(side_effect=responses), delete=Mock())
                result = loop(session, len(responses))
                self.assertEqual(session.put.call_count, expected_puts)
                session.delete.assert_not_called()
                if expected == "success":self.assertEqual(result.provisioning_data.instance_id, "555")
                else:self.assertEqual(json.loads(result.message[len(safety.ATTEMPTS_PREFIX):])["outcome"], expected)
        safety._cooldown_until = 0.
        session = NS(put=Mock(return_value=response(200, {"success": True, "new_contract": 555})))
        selected = loop(session, 3, ["101", "100", "102"])
        self.assertEqual(selected.offer.instance.name, "101")
        self.assertEqual(session.put.call_args.args[0], "/v0/asks/101/")

    def test_explicit_order_only_reorders_already_qualified_vast_positions(self):
        def item(provider, name):return (provider, NS(backend=NS(value=provider), instance=NS(name=name)))
        original = [item("vastai", "100"), item("runpod", "x"), item("vastai", "101"), item("vastai", "102")]
        ordered = safety.order_offers(original, ["101", "100"])
        self.assertEqual([item[1].instance.name for item in ordered], ["101", "x", "100", "102"])
        self.assertIs(ordered[1], original[1])
        self.assertEqual({id(item) for item in original}, {id(item) for item in ordered})

    def test_pinned_allocated_instance_rate_limit_stays_provisioning(self):
        if not CACHES:
            self.skipTest("supply the reviewed source caches for native AST boundary checks")
        provenance = json.loads((HERE / "native-vast-provenance.json").read_text())
        sources = {}
        for path in ("dstack/_internal/core/backends/vastai/api_client.py",
                     "dstack/_internal/core/backends/vastai/compute.py"):
            file = next(cache / path.replace("/", "__") for cache in CACHES
                        if (cache / path.replace("/", "__")).is_file())
            raw = file.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance["sources"][path]["sha256"])
            sources[path] = installer.transform(path, raw.decode())
        import __future__
        class ProvisioningError(Exception):pass
        scope = {**vars(safety), "ProvisioningError": ProvisioningError,
                 "logger": NS(warning=Mock()), "DSTACK_RUNNER_SSH_PORT": 10022}
        selected = []
        for path, names in [("dstack/_internal/core/backends/vastai/api_client.py", {"VastAIRateLimitError", "get_instance"}),
                            ("dstack/_internal/core/backends/vastai/compute.py", {"update_provisioning_data"})]:
            selected.extend(copy.deepcopy(node) for node in ast.walk(ast.parse(sources[path]))
                            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names)
        module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
        exec(compile(module, "pinned_native_provisioning_read", "exec", flags=__future__.annotations.compiler_flag), scope)
        for header, expected_reads in [({}, 3), ({"Retry-After": "999"}, 1)]:
            safety._cooldown_until = 0.
            with self.subTest(headers=header), patch.object(safety.time, "sleep", side_effect=self.advance):
                session = NS(get=Mock(return_value=response(429, {}, header)), put=Mock(), delete=Mock())
                api = NS(s=session);api.get_instance = MethodType(scope["get_instance"], api)
                compute = NS(api_client=api, terminate_instance=Mock())
                update = MethodType(scope["update_provisioning_data"], compute)
                original = NS(instance_id="555", hostname=None, ssh_port=None, backend_data={"original": True})
                before = copy.deepcopy(vars(original))
                self.assertIsNone(update(original, "original-public", "original-private"))
                self.assertEqual(session.get.call_count, expected_reads)
                # The next native tick is also transient, without another HTTP call.
                self.assertIsNone(update(original, "original-public", "original-private"))
                with self.assertRaises(scope["VastAIRateLimitError"]):api.get_instance("555")
                self.assertEqual(session.get.call_count, expected_reads)
                session.get.assert_called_with("/v0/instances/555/")
                self.assertEqual(vars(original), before)
                session.put.assert_not_called();session.delete.assert_not_called()
                compute.terminate_instance.assert_not_called()


def native_loop(sources):
    """Execute exact pinned methods/loop AST with only HTTP and native IO faked."""
    import __future__
    from enum import Enum
    class BackendError(Exception):pass
    class SkipOffer(Exception):pass
    class Backend(Enum):VAST = "vastai"
    logger = NS(debug=Mock(), info=Mock(), warning=Mock(), exception=Mock())
    boundary_errors = []
    def checked_error(ask, error):
        boundary_errors.append(error)
        return safety.closed_error(ask, error)
    scope = {**vars(safety), "NoCapacityError": BackendError, "BackendError": BackendError, "SkipOffer": SkipOffer,
             "logger": logger, "fmt": lambda value: "original", "settings": NS(MAX_OFFERS_TRIED=25),
             "_OfferAttemptError": NS, "_NewCapacityAttempts": NS, "_FailedNewCapacityProvisioning": NS,
             "_ProvisionNewCapacityResult": NS, "_build_placement_group_cleanup": lambda **kwargs: None,
             "placement_group_model_to_placement_group_optional": lambda value: None,
             "_get_offer_volumes": lambda *args: [], "JobConfiguration": NS, "JobProvisioningData": NS,
             "filter_non_placeholder_instances": lambda items: [], "is_cloud_cluster": lambda fleet: False,
             "BACKENDS_WITH_GROUP_PROVISIONING_SUPPORT": set(), "master_job_provisioning_data": None,
             "build_authorized_keys": lambda *args: [], "get_docker_commands": lambda *args: [],
             "generate_unique_instance_name_for_job": lambda *args, **kwargs: "original",
             "validate_extra_ignore": lambda *args: NS(min_bid=None), "VastAIOfferBackendData": object,
             "DSTACK_RUNNER_SSH_PORT": 10022, "MAX_INSTANCE_NAME_LEN": 60}
    scope["closed_error"] = checked_error
    selected = []
    for path, names in [("dstack/_internal/core/backends/vastai/api_client.py", {"create_instance"}),
                        ("dstack/_internal/core/backends/vastai/compute.py", {"run_job"}),
                        ("dstack/_internal/server/background/pipeline_tasks/jobs_submitted.py", {"_get_offer_attempt_error", "_get_new_capacity_failure_message"})]:
        for node in ast.walk(ast.parse(sources[path])):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:selected.append(copy.deepcopy(node))
    parent = next(node for node in ast.walk(ast.parse(sources["dstack/_internal/server/background/pipeline_tasks/jobs_submitted.py"])) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_provision_new_capacity")
    index = next(index for index, node in enumerate(parent.body) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "offers_iter" for target in node.targets))
    index -= 1  # Include the exact approved-order assignment immediately before the iterator.
    tail = ast.parse("async def actual_loop(offers):\n pass\n").body[0]
    tail.body = ast.parse("placement_group_model = None").body + copy.deepcopy(parent.body[index:]);selected.append(tail)
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, "pinned_native_boundaries", "exec", flags=__future__.annotations.compiler_flag), scope)
    async def run_async(method, *args):return method(*args)
    scope["run_async"] = run_async
    def run(session, count, instance_types=None):
        api = NS(s=session);api.create_instance = MethodType(scope["create_instance"], api)
        compute = NS(api_client=api);compute.run_job = MethodType(scope["run_job"], compute)
        offers = [(NS(compute=lambda: compute), NS(backend=Backend.VAST, region="original", price=.5,
                   instance=NS(name=str(100+index), resources=NS(spot=False, disk=NS(size_mib=131072))), backend_data={})) for index in range(count)]
        job = NS(job_spec=NS(image_name="original", registry_auth=None))
        scope.update(job=job, jobs=[job], run=NS(), profile=NS(instance_types=instance_types), job_model=NS(), fleet_model=NS(instances=[], name="original"),
                     project_ssh_public_key="synthetic", project_ssh_private_key="synthetic", volumes=[],
                     placement_group_model=None, new_placement_group_models=[], requirements=NS(), extra_authorized_keys=[])
        result = asyncio.run(scope["actual_loop"](offers))
        if session.put.call_count == 0:
            raise AssertionError("mock boundary missing: " + repr(boundary_errors[-1]))
        return result
    return run


def native_read_guards(sources, wait):
    """The actual catalog/aggregation and preload functions, without providers."""
    import __future__
    import os
    def function(path, name, scope):
        selected = next(node for node in ast.walk(ast.parse(sources[path])) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
        module = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(selected)], type_ignores=[]))
        exec(compile(module, "pinned_native_read_boundary", "exec", flags=__future__.annotations.compiler_flag), scope)
        return scope[name]
    class HTTPException(Exception):
        def __init__(self, **kwargs):self.__dict__.update(kwargs)
    post = Mock(return_value=response(429, {}))
    catalog_get = function("gpuhunt/providers/vastai.py", "get", {**vars(safety), "requests": NS(post=post), "bundles_url": "https://console.vast.ai/api/v0/bundles/", "QueryFilter": NS})
    provider = NS(make_filters=lambda query: {}, extra_filters=None)
    async def run_async(*args):return catalog_get(provider)
    async_scope = {**vars(safety), "asyncio": asyncio, "logger": NS(debug=Mock()), "run_async": run_async,
                   "get_offers_tracked": None, "HTTPException": HTTPException}
    aggregate = function("dstack/_internal/server/services/backends/__init__.py", "get_backend_offers", async_scope)
    with patch.object(safety.time, "sleep", side_effect=wait):
        try:asyncio.run(aggregate([NS(TYPE=NS(value="vastai"))], NS(), False, False))
        except HTTPException as error:
            assert error.status_code == 429 and error.detail == "sixnine_vast_read_rate_limited"
        else:raise AssertionError("native catalog exhaustion became empty inventory")
    assert post.call_count == 3
    # Another native pipeline request while the valid long hint is active does
    # not reach even the gpuhunt HTTP mock; the public header keeps that delay.
    safety._cooldown_until = safety.time.monotonic() + 999
    try:asyncio.run(aggregate([NS(TYPE=NS(value="vastai"))], NS(), False, False))
    except HTTPException as error:assert int(error.headers["Retry-After"]) >= 999
    else:raise AssertionError("native retry lost the provider cooldown")
    assert post.call_count == 3
    default_catalog = Mock()
    preload = function("dstack/_internal/server/background/scheduled_tasks/offers_catalog.py", "preload_offers_catalog", {"os": os, "gpuhunt": NS(default_catalog=default_catalog)})
    with patch.dict(os.environ, {"SIXNINE_DSTACK_PRELOAD_CATALOG": "0"}):asyncio.run(preload())
    default_catalog.assert_not_called()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--upstream-cache", type=Path)
    parser.add_argument("--public-cache", type=Path)
    args, remaining = parser.parse_known_args()
    CACHES[:] = [value for value in (args.upstream_cache, args.public_cache) if value]
    unittest.main(argv=[sys.argv[0], *remaining])
