"""Operator assembly tests with fake Session/server; no listener or GPU runtime."""
from contextlib import redirect_stdout
from dataclasses import replace
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from studio_platform.inference.protocol import NotReady
from studio_platform.inference.wangp_contract import EngineManifest, PreparedRequest, canonical_json
from studio_platform.inference import wangp_factory
from studio_platform.runtime_hosts import wangp_launcher
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal


TEST_TOKEN = "synthetic-launcher-test-token-" + "x" * 32


class WanGPLaunchTests(unittest.TestCase):
    def test_bootstrap_receipt_reader_imports_without_runtime_environment(self):
        # Bootstrap uses the provider's base Python, not the locked runtime venv.
        script = """
import sys
class NoOptionalPackages:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'httpx', 'torch', 'sqlalchemy', 'uvicorn', 'pydantic', 'requests'}:
            raise AssertionError('optional dependency imported by bootstrap receipt reader')
sys.meta_path.insert(0, NoOptionalPackages())
from studio_platform.runtime_hosts.wangp_receipts import VERIFICATION_RECEIPT, read_verification_receipt
assert VERIFICATION_RECEIPT == 'runtime-verification.json'
assert callable(read_verification_receipt)
"""
        checked = subprocess.run([sys.executable, "-S", "-c", script],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout, "")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "manifest.json"
        document = json.loads((Path(__file__).resolve().parents[1] / "deploy/wangp/manifest.json").read_text(encoding="utf-8"))
        self.manifest = EngineManifest.from_dict(document)
        self.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        self.token = self.root / "private-token"
        self.token.write_text(TEST_TOKEN, encoding="ascii")
        self.token.chmod(0o600)
        self.state = self.root / "state"
        self.config = self.root / "wgp_config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.models = self.root / "models"
        self.models.mkdir()
        self.argv = ["--runtime-root", str(self.runtime), "--model-root", str(self.models),
            "--config", str(self.config), "--manifest", str(self.manifest_path),
            "--token-file", str(self.token), "--state-dir", str(self.state), "--slot-key", "slot-test"]

    def journal(self, create=False):
        return ReceiptJournal(self.state / "operations.sqlite3", slot_key="slot-test",
                              manifest_digest=self.manifest.digest, create=create)

    def runtime_document(self):
        path = self.root / "runtime.json"
        value = {"version": 1, "enabled": True, "slot_key": "slot-test", "configuration_id": "config-test",
                 "manifest_file": str(self.manifest_path), "token_file": str(self.token), 'runtime_incarnation': 'a'*32}
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        slot = types.SimpleNamespace(runtime_config_file=str(path), endpoint="http://127.0.0.1:8199",
            spec=types.SimpleNamespace(backend="wangp-worker", model_id="MiniMax-H3-Base-BF16",
                configuration_id="config-test", engine_manifest_digest=self.manifest.digest))
        return path, value, slot

    def test_import_and_factory_construction_do_not_start_network_or_session(self):
        from studio_platform.runtime_hosts import wangp_session
        with patch("httpx.Client", side_effect=AssertionError("no client on import/construction")), \
                patch.object(wangp_session, "create_session", side_effect=AssertionError("no Session")):
            importlib.reload(wangp_factory)
            importlib.reload(wangp_launcher)
            _, _, slot = self.runtime_document()
            backend = wangp_factory.create_backend(slot, self.root)
            self.assertEqual(backend.manifest.digest, self.manifest.digest)
            self.assertIsNone(backend.transport._client)
            self.assertNotIn(TEST_TOKEN, repr(backend))
            backend.close()

    def test_factory_requires_configuration_manifest_and_private_endpoint(self):
        path, value, slot = self.runtime_document()
        for invalid in ({**value, "enabled": False}, {**value, "configuration_id": "other"},
                        {**value, "extra": "not-accepted"}, {**value, 'runtime_incarnation': None},
                        {**value, 'runtime_incarnation': ''}, {k:v for k,v in value.items() if k!='runtime_incarnation'}):
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "configuration_mismatch"):
                wangp_factory.create_backend(slot, self.root)
        path.write_text(json.dumps(value), encoding="utf-8")
        slot.spec.engine_manifest_digest = "0" * 64
        with self.assertRaisesRegex(ValueError, "manifest_binding_mismatch"):
            wangp_factory.create_backend(slot, self.root)
        slot.spec.engine_manifest_digest = self.manifest.digest
        slot.endpoint = "http://example.org:8199"
        with self.assertRaisesRegex(ValueError, "loopback_tunnel_required"):
            wangp_factory.create_backend(slot, self.root)
        slot.endpoint = "http://127.0.0.1:8199"
        self.token.write_text("invalid", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "invalid_token"):
            wangp_factory.create_backend(slot, self.root)

    def test_verify_only_does_not_create_journal_read_token_or_start_session(self):
        self.token.unlink()
        from studio_platform.runtime_hosts import wangp_session
        with patch.object(wangp_session, "verify_runtime", return_value={"manifest_digest": self.manifest.digest}), \
                patch.object(wangp_session, "create_session", side_effect=AssertionError("no Session")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wangp_launcher.main(self.argv + ["--verify-only"]), 0)
        self.assertFalse(self.state.exists())
        self.assertIn("runtime_files_verified", output.getvalue())

    def diagnostic_args(self):
        return ["--startup-status-file", str(self.root / "startup.json"), "--startup-id", "f"*32,
                "--expected-manifest-digest", self.manifest.digest]

    def startup_failure(self):
        from studio_platform.runtime_hosts.wangp_startup import read_startup_failure
        return read_startup_failure(self.root / "startup.json", slot_key="slot-test",
            manifest_digest=self.manifest.digest, launch_id="f"*32, pid=os.getpid())

    def test_early_import_failure_has_private_receipt_and_no_journal(self):
        secret = "synthetic-key https://private.example/?token=do-not-print"
        with patch.object(wangp_launcher, "_EARLY_IMPORT_FAILURE", ModuleNotFoundError(secret)), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wangp_launcher.main(self.argv + self.diagnostic_args()), 1)
        self.assertFalse(self.state.exists())
        failure = self.startup_failure()
        self.assertEqual((failure["phase"], failure["error_code"]),
                         ("runtime_imports", "wangp_runtime_dependency_missing"))
        self.assertNotIn(secret, json.dumps(failure) + output.getvalue())

    def test_token_failure_is_distinct_and_receipt_contains_no_exception_text(self):
        secret = "synthetic secret URL https://private.invalid/key"
        with patch.object(wangp_launcher, "private_token_file", side_effect=ValueError(secret)), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wangp_launcher.main(self.argv + self.diagnostic_args()), 1)
        self.assertFalse(self.state.exists())
        failure = self.startup_failure()
        self.assertEqual((failure["phase"], failure["error_code"]),
                         ("runtime_token", "wangp_startup_stage_failed"))
        self.assertNotIn(secret, json.dumps(failure) + output.getvalue())

    def test_actual_subprocess_missing_dependency_reports_before_manifest_read(self):
        script = '''import sys
class DenyFastAPI:
 def find_spec(self,name,path=None,target=None):
  if name.split('.')[0]=='fastapi':raise ModuleNotFoundError('synthetic hidden import details')
sys.meta_path.insert(0,DenyFastAPI())
from studio_platform.runtime_hosts.wangp_launcher import main
raise SystemExit(main(sys.argv[1:]))
'''
        result = subprocess.run([sys.executable, "-c", script, *self.argv, *self.diagnostic_args()],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.state.exists())
        failure = json.loads((self.root / "startup.json").read_text())
        self.assertEqual((failure["phase"], failure["error_type"]), ("runtime_imports", "ModuleNotFoundError"))
        self.assertNotIn("hidden import details", result.stdout + result.stderr + json.dumps(failure))

    def test_changed_verified_manifest_fails_before_session_or_listener(self):
        from studio_platform.runtime_hosts import wangp_session
        with patch.object(wangp_session, "verify_runtime", return_value={"manifest_digest": "0" * 64}), \
                patch.object(wangp_session, "create_session") as session, \
                patch.object(wangp_launcher, "create_app") as app, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wangp_launcher.main(self.argv + ["--create-journal"]), 1)
            session.assert_not_called()
            app.assert_not_called()
        self.assertEqual(output.getvalue().strip(), '{"state": "wangp_runtime_start_failed"}')
        journal = self.journal()
        journal.acquire_host()  # Startup failure released ownership.
        journal.release_host()

    def test_host_lock_precedes_verification_and_session_and_pending_receipts_survive(self):
        original = PreparedRequest("job-original", "attempt-original", "1" * 64, self.manifest.digest,
            canonical_json({"synthetic": True}), canonical_json({"width": 256, "height": 256}), True)
        journal = self.journal(create=True)
        journal.claim(original, "old-incarnation")
        journal.transition(original.operation_id, expected={"prepared"}, state="dispatch_intent")
        events, hosts = [], []

        def check_owned():
            contender = self.journal()
            try:
                with self.assertRaisesRegex(NotReady, "owned"):
                    contender.acquire_host()
            finally:
                contender.release_host()

        def verify(*args):
            check_owned()
            events.append("verify")
            return {"manifest_digest": self.manifest.digest, "inference_verified": False}

        def session(*args):
            check_owned()
            events.append("session")
            receipt = wangp_launcher.read_verification_receipt(self.state, manifest_digest=self.manifest.digest,
                slot_key="slot-test", pid=os.getpid())
            self.assertFalse(receipt["evidence"]["inference_verified"])
            return types.SimpleNamespace(is_idle=lambda: True, close_when_idle=lambda: events.append("session-closed"))

        def app(host, inputs, token):
            hosts.append(host)
            self.assertEqual(token, TEST_TOKEN)
            receipt = wangp_launcher.read_verification_receipt(self.state, manifest_digest=self.manifest.digest,
                slot_key="slot-test", pid=os.getpid(), incarnation=host.incarnation)
            self.assertEqual(receipt["incarnation"], host.incarnation)
            self.assertFalse(host.readiness().idle)
            current = host.inspect(original.operation_id)
            self.assertEqual(current.state, "unknown")
            self.assertEqual(current.identity_digest, original.identity_digest)
            with self.assertRaisesRegex(NotReady, "obligation_pending"):
                journal.claim(replace(original, attempt_tag="different-attempt"), "new-incarnation")
            return object()

        fake_uvicorn = types.ModuleType("uvicorn")
        def serve(application, **kwargs):
            events.append("serve")
            self.assertEqual(kwargs["host"], "127.0.0.1")
            self.assertEqual(kwargs["workers"], 1)
            self.assertFalse(kwargs["access_log"])
            self.assertFalse(kwargs["proxy_headers"])
        fake_uvicorn.run = serve
        from studio_platform.runtime_hosts import wangp_session
        with patch.object(wangp_session, "verify_runtime", side_effect=verify), \
                patch.object(wangp_session, "create_session", side_effect=session), \
                patch.object(wangp_launcher, "create_app", side_effect=app), \
                patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), patch.dict(os.environ), redirect_stdout(io.StringIO()):
            self.assertEqual(wangp_launcher.main(self.argv), 0)
        self.assertEqual(events, ["verify", "session", "serve", "session-closed"])
        self.assertEqual(journal.get(original.operation_id).state, "unknown")
        self.assertTrue(journal.has_obligations())
        journal.acquire_host()
        journal.release_host()

    def test_new_launcher_rehashes_and_old_receipt_cannot_bypass_failed_check(self):
        from studio_platform.runtime_hosts import wangp_session
        journal = self.journal(create=True)
        host = types.SimpleNamespace(manifest=self.manifest, journal=journal, incarnation="a"*32)
        old = wangp_launcher.write_verification_receipt(self.state, host,
            {"manifest_digest": self.manifest.digest, "inference_verified": False})
        with patch.object(wangp_session, "verify_runtime", side_effect=ValueError("wangp_component_hash_mismatch")) as verify, \
                patch.object(wangp_session, "create_session") as session, \
                patch.object(wangp_launcher, "create_app") as app, redirect_stdout(io.StringIO()):
            self.assertEqual(wangp_launcher.main(self.argv), 1)
        verify.assert_called_once()
        session.assert_not_called()
        app.assert_not_called()
        self.assertEqual(wangp_launcher.read_verification_receipt(self.state, manifest_digest=self.manifest.digest,
            slot_key="slot-test", pid=os.getpid()), old)
        with self.assertRaisesRegex(ValueError, "receipt_mismatch"):
            wangp_launcher.read_verification_receipt(self.state, manifest_digest=self.manifest.digest,
                slot_key="slot-test", pid=os.getpid(), incarnation="b"*32)

    def test_receipt_write_failure_never_initializes_session_and_retains_journal(self):
        from studio_platform.runtime_hosts import wangp_session
        with patch.object(wangp_session, "verify_runtime", return_value={
                "manifest_digest": self.manifest.digest, "inference_verified": False}) as verify, \
                patch.object(wangp_launcher.os, "replace", side_effect=OSError("synthetic IO failure")), \
                patch.object(wangp_session, "create_session") as session, \
                patch.object(wangp_launcher, "create_app") as app, redirect_stdout(io.StringIO()):
            self.assertEqual(wangp_launcher.main(self.argv + ["--create-journal"]), 1)
        verify.assert_called_once()
        session.assert_not_called()
        app.assert_not_called()
        self.assertTrue((self.state / "operations.sqlite3").exists())
        self.assertFalse((self.state / wangp_launcher.VERIFICATION_RECEIPT).exists())
        self.assertEqual(list(self.state.glob(".runtime-verification-*.tmp")), [])
        journal = self.journal()
        journal.acquire_host()  # Failure before Session released the owned slot.
        journal.release_host()

    def test_private_receipt_rejects_wrong_identity_and_failed_write_preserves_old_bytes(self):
        journal = self.journal(create=True)
        host = types.SimpleNamespace(manifest=self.manifest, journal=journal, incarnation="a"*32)
        evidence = {"manifest_digest": self.manifest.digest, "inference_verified": False}
        original = wangp_launcher.write_verification_receipt(self.state, host, evidence)
        path = self.state / wangp_launcher.VERIFICATION_RECEIPT
        before = path.read_bytes()
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for change in ({"pid": os.getpid()+1}, {"slot_key": "other"}, {"manifest_digest": "0"*64},
                       {"incarnation": "b"*32}):
            expected = dict(manifest_digest=self.manifest.digest, slot_key="slot-test", pid=os.getpid(), incarnation="a"*32)
            expected.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "receipt_mismatch"):
                wangp_launcher.read_verification_receipt(self.state, **expected)
        with patch.object(wangp_launcher.os, "replace", side_effect=OSError("synthetic IO failure")), self.assertRaises(OSError):
            wangp_launcher.write_verification_receipt(self.state, host, evidence)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.state.glob(".runtime-verification-*.tmp")), [])
        self.assertEqual(original["evidence"], evidence)

    def test_shutdown_waits_for_runtime_and_closes_before_releasing_host(self):
        now, events = [0], []
        host = types.SimpleNamespace(close=lambda: events.append("host-closed"))
        session = types.SimpleNamespace(is_idle=lambda: now[0] >= .5,
            close_when_idle=lambda: events.append("session-closed"))
        def sleep(seconds):
            now[0] += seconds
            events.append("wait")
        wangp_launcher.shutdown_owned_host(host, session, grace_seconds=1,
            clock=lambda: now[0], sleeper=sleep,
            terminate=lambda _: self.fail("must allow actual completion"))
        self.assertEqual(events, ["wait", "wait", "session-closed", "host-closed"])

    def test_shutdown_timeout_or_close_error_never_releases_journal_ownership(self):
        for idle in (False, True):
            with self.subTest(idle=idle):
                now, events = [0], []
                host = types.SimpleNamespace(close=lambda: events.append("host-closed"))
                def cannot_close():
                    raise RuntimeError("synthetic-close-failure")
                session = types.SimpleNamespace(is_idle=lambda: idle, close_when_idle=cannot_close)
                def sleep(seconds):
                    now[0] += seconds
                with self.assertRaisesRegex(RuntimeError, "termination_required"):
                    wangp_launcher.shutdown_owned_host(host, session, grace_seconds=1,
                        clock=lambda: now[0], sleeper=sleep, terminate=lambda code: events.append(("terminate", code)))
                self.assertEqual(events, [("terminate", 1)])

    def test_partial_session_initialization_is_not_treated_as_proven_stopped(self):
        now, events = [0], []
        pending = wangp_launcher.PendingSession()
        host = types.SimpleNamespace(close=lambda: events.append("host-closed"))
        def sleep(seconds):
            now[0] += seconds
        with self.assertRaisesRegex(RuntimeError, "termination_required"):
            wangp_launcher.shutdown_owned_host(host, pending, grace_seconds=1,
                clock=lambda: now[0], sleeper=sleep, terminate=lambda code: events.append(("terminate", code)))
        self.assertEqual(events, [("terminate", 1)])

    def test_interrupting_shutdown_requires_process_termination_before_release(self):
        events = []
        def interrupted(seconds):
            raise KeyboardInterrupt()
        with self.assertRaisesRegex(RuntimeError, "termination_required"):
            wangp_launcher.shutdown_owned_host(
                types.SimpleNamespace(close=lambda: events.append("host-closed")),
                wangp_launcher.PendingSession(), grace_seconds=1, clock=lambda: 0,
                sleeper=interrupted, terminate=lambda code: events.append(("terminate", code)))
        self.assertEqual(events, [("terminate", 1)])


if __name__ == "__main__":
    unittest.main()
