"""Safe observation must not change work, leak inputs or grow without bound."""

import base64
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from studio_platform.telemetry import (
    CloudTelemetryConfig,
    NullTelemetry,
    StageTelemetry,
    configured_telemetry,
    controller_resource_config,
    create_cloud_telemetry,
    read_config,
)
from studio_platform.runtime_catalog import PROFILE_IDS


def config():
    return CloudTelemetryConfig(
        "https://otlp-gateway-prod-us-east-0.grafana.net/otlp",
        "Basic " + base64.b64encode(b"12345:PRIVATE_WRITE_TOKEN").decode(),
    )


class RecordingLogger:
    def __init__(self):
        self.events = []

    def emit(self, **kwargs):
        self.events.append(json.loads(kwargs["body"]))


class Instrument:
    def __init__(self):
        self.points = []

    def add(self, value, attrs):
        self.points.append((value, attrs))

    record = add


class RecordingMeter:
    def create_counter(self, *_args, **_kwargs):
        return Instrument()

    create_histogram = create_counter


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        try:
            from opentelemetry.trace import NonRecordingSpan, SpanContext
        except ImportError:
            self.skipTest("Optional SDK API dependency not installed")
        self.logger = RecordingLogger()
        self.clock = [100.0]
        self.tracer = type(
            "Tracer",
            (),
            {
                "start_span": lambda _self, *_args, **_kwargs: NonRecordingSpan(
                    SpanContext(1, 2, False)
                )
            },
        )()
        self.telemetry = StageTelemetry(
            logger=self.logger,
            tracer=self.tracer,
            meter=RecordingMeter(),
            monotonic=lambda: self.clock[0],
            timestamp=lambda: 1770000000000000000,
        )

    def test_start_is_visible_before_finish_and_duration_is_monotonic(self):
        stage = self.telemetry.start(
            "model_load",
            {
                "profile_id": PROFILE_IDS[0],
                "provider": "vast",
                "mode": "fl",
                "gpu_type": "rtx5090",
                "warmth": "cold",
                "job_id": "11111111-1111-1111-1111-111111111111",
                "gpu_index": 0,
            },
        )
        self.assertEqual([e["event"] for e in self.logger.events], ["stage_started"])
        self.assertEqual(
            self.logger.events[0]["job_id"], "11111111-1111-1111-1111-111111111111"
        )
        self.clock[0] = 107.25
        self.assertEqual(stage.finish(), 7.25)
        self.assertIsNone(stage.finish())
        self.assertEqual(len(self.logger.events), 2)
        self.assertEqual(self.logger.events[1]["duration_seconds"], 7.25)
        for _, labels in self.telemetry.duration.points:
            self.assertNotIn("job_id", labels)
            self.assertNotIn("stage_id", labels)

    def test_payload_projection_discards_private_context_and_free_form_errors(self):
        secret = "SECRET /private/media.mp4 https://x/?signature=SECRET"
        stage = self.telemetry.start(
            secret,
            {
                "prompt": secret,
                "media": secret,
                "url": secret,
                "provider": secret,
                "profile_id": secret,
                "job_id": secret,
                "dstack_run": secret,
                "owner_id": secret,
                "memory": secret,
                "listed_memory_gib": float("nan"),
                "width": 832,
                "steps": 50,
                "gpu_index": True,
            },
        )
        stage.finish("failure", error_code=secret)
        raw = json.dumps(self.logger.events)
        self.assertNotIn("SECRET", raw)
        self.assertEqual(self.logger.events[-1]["error_code"], "unknown_error")
        self.assertEqual(self.logger.events[-1]["stage"], "unknown")
        self.assertEqual(self.logger.events[-1]["width"], 832)
        self.assertNotIn("gpu_index", self.logger.events[-1])
        self.assertNotIn("listed_memory_gib", self.logger.events[-1])

    def test_telemetry_failure_preserves_operation_result_and_original_exception(self):
        with patch.object(self.logger, "emit", side_effect=RuntimeError("SECRET")):
            self.assertEqual(
                self.telemetry.call("generate", {}, lambda: "original result"),
                "original result",
            )
        with self.assertRaisesRegex(RuntimeError, "PRIVATE original error"):
            self.telemetry.call(
                "generate",
                {},
                lambda: (_ for _ in ()).throw(RuntimeError("PRIVATE original error")),
            )
        self.assertEqual(self.logger.events[-1]["error_code"], "stage_failed")
        self.assertNotIn("PRIVATE", json.dumps(self.logger.events))

    def test_metric_labelsets_are_capped_across_many_safe_dimensions(self):
        from studio_platform.telemetry import STAGES, GPU_TYPES

        for stage in STAGES:
            for gpu in GPU_TYPES:
                for mode in ("fl", "ref"):
                    for height in (480, 720, 768):
                        self.telemetry.start(
                            stage,
                            {"mode": mode, "gpu_type": gpu, "height": height},
                        ).finish()
        self.assertLessEqual(self.telemetry.status()["metric_labelsets"], 512)
        self.assertTrue(
            any(
                attrs.get("stage") == "unknown"
                for _, attrs in self.telemetry.duration.points
            )
        )

    def test_content_bands_label_only_content_stages(self):
        context = {
            "mode": "ref",
            "gpu_type": "rtx5090",
            "frames": 362,
            "fps": 24,
            "height": 768,
            "image_refs": 3,
            "audio_refs": 1,
        }
        self.telemetry.start("generate", context).finish(duration_seconds=4000)
        self.telemetry.start("queue_wait", context).finish(duration_seconds=5)
        points = {attrs["stage"]: attrs for _, attrs in self.telemetry.duration.points}
        self.assertEqual(
            {
                k: points["generate"][k]
                for k in ("length", "resolution", "image_refs_band", "audio_ref")
            },
            {
                "length": "gt12s",
                "resolution": "gt720",
                "image_refs_band": "3-5",
                "audio_ref": "yes",
            },
        )
        self.assertEqual(points["queue_wait"]["length"], "any")

    def test_captured_gpu_names_reach_allocation_and_cold_events_without_host_metric_labels(
        self,
    ):
        for captured, expected in (
            ("RTX5090", "rtx5090"),
            ("RTX 5090", "rtx5090"),
            ("RTXPRO6000S", "rtx-pro6000"),
            ("RTXPRO6000WS", "rtx-pro6000"),
            ("H100", "h100"),
            ("H200", "h200"),
            ("B200", "b200"),
            ("B300", "b300"),
            ("PRIVATE unsupported model", "unknown"),
            ({"PRIVATE": "value"}, "unknown"),
        ):
            with self.subTest(captured=captured):
                context = {
                    "gpu": captured,
                    "machine_id": "149273",
                    "offer_id": "55370775",
                    "provider_instance_id": "55970464",
                    "provider": "vast",
                    "simulation": False,
                }
                self.telemetry.host_event(context)
                allocation = self.logger.events[-1]
                self.telemetry.start("capacity_provision", context).finish(
                    duration_seconds=42, timing_basis="durable_cpu_interval"
                )
                cold = self.logger.events[-1]
                self.assertEqual(
                    (allocation["gpu_type"], cold["gpu_type"]), (expected, expected)
                )
                self.assertEqual(allocation["machine_id"], "149273")
                self.assertEqual(cold["duration_seconds"], 42)
                for _, labels in self.telemetry.duration.points:
                    for field in (
                        "machine_id",
                        "offer_id",
                        "provider_instance_id",
                        "gpu",
                    ):
                        self.assertNotIn(field, labels)
        from studio_platform.telemetry import _context

        self.assertEqual(
            _context({"gpu_type": "h100", "gpu": "RTX5090"})["gpu_type"], "h100"
        )
        self.assertEqual(
            _context({"gpu_type": "PRIVATE", "gpu": "RTX5090"})["gpu_type"], "unknown"
        )
        self.assertNotIn("PRIVATE", json.dumps(self.logger.events))

    def test_recovered_interval_and_unknown_note_keep_observation_time_and_original_duration(
        self,
    ):
        stage = self.telemetry.start("generate", {}, started_at=1769999900.25)
        stage.note("submission_response_unknown")
        stage.note("submission_response_unknown")
        self.assertEqual(
            [e["event"] for e in self.logger.events],
            ["stage_started", "stage_observation"],
        )
        self.assertEqual(self.logger.events[0]["time_unix_ns"], 1770000000000000000)
        # Repository epoch seconds are floats; this is a reconstructed CPU
        # interval, not a nanosecond-precision GPU timestamp.
        self.assertLess(
            abs(self.logger.events[0]["interval_start_unix_ns"] - 1769999900250000000),
            1000,
        )
        self.assertFalse(self.telemetry.duration.points)
        self.clock[0] += 0.2
        self.assertEqual(
            stage.finish(duration_seconds=47, timing_basis="durable_cpu_interval"), 47
        )
        self.assertEqual(self.logger.events[-1]["timing_basis"], "durable_cpu_interval")
        self.assertEqual(self.logger.events[-1]["duration_seconds"], 47)
        stage.note("submission_response_unknown")
        self.assertEqual(len(self.logger.events), 3)

    def test_invalid_interval_and_broken_clock_do_not_invent_samples_or_replace_errors(
        self,
    ):
        for duration in (-1, float("nan"), float("inf"), True, 31536001):
            stage = self.telemetry.start("generate", {}, started_at=float("nan"))
            self.clock[0] += 2
            self.assertEqual(
                stage.finish(
                    duration_seconds=duration, timing_basis="durable_cpu_interval"
                ),
                2,
            )
            self.assertNotIn("interval_start_unix_ns", self.logger.events[-1])
            self.assertEqual(self.logger.events[-1]["timing_basis"], "monotonic")
        before = len(self.telemetry.duration.points)
        with self.assertRaisesRegex(RuntimeError, "PRIVATE original error"):
            with self.telemetry.start("generate", {}):
                self.telemetry.monotonic = lambda: (_ for _ in ()).throw(
                    RuntimeError("PRIVATE clock error")
                )
                raise RuntimeError("PRIVATE original error")
        self.assertEqual(len(self.telemetry.duration.points), before)
        self.assertNotIn("PRIVATE", json.dumps(self.logger.events))


class ConfigurationTests(unittest.TestCase):
    def test_controller_labels_follow_exact_cpu_release_and_existing_origin(self):
        original = config()
        revision = 'a' * 40
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'controller-image'
            image.write_text('sixnine-platform:' + revision + '\n', encoding='ascii')
            with patch('studio_platform.telemetry._CONTROLLER_IMAGE_FILE', image):
                for origin, environment in (
                    ('https://inkseq.com', 'production'),
                    ('https://staging.inkseq.com', 'staging'),
                ):
                    with (
                        self.subTest(environment=environment),
                        patch.dict(os.environ, {'PUBLIC_ORIGIN': origin}, clear=True),
                    ):
                        observed = controller_resource_config(original)
                        self.assertEqual(observed.service_name, 'sixnine-controller')
                        self.assertEqual(observed.service_version, revision)
                        self.assertEqual(observed.environment, environment)
                        self.assertEqual(observed.endpoint, original.endpoint)
                        self.assertEqual(observed.authorization, original.authorization)
                        self.assertEqual(dict(os.environ), {'PUBLIC_ORIGIN': origin})
                self.assertEqual(original, config())

    def test_invalid_controller_identity_does_not_export_under_acceptance_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'controller-image'
            for reference, env in (
                ('sixnine-platform:latest', {'PUBLIC_ORIGIN': 'https://inkseq.com'}),
                (
                    'sixnine-platform:' + 'b' * 40,
                    {'PUBLIC_ORIGIN': 'https://unexpected.example'},
                ),
                (
                    'sixnine-platform:' + 'b' * 40,
                    {
                        'PUBLIC_ORIGIN': 'https://inkseq.com',
                        'SIXNINE_PUBLIC_ORIGIN': 'https://staging.inkseq.com',
                    },
                ),
            ):
                image.write_text(reference, encoding='ascii')
                with (
                    self.subTest(reference=reference, env=env),
                    patch.dict(os.environ, env, clear=True),
                    patch('studio_platform.telemetry._CONTROLLER_IMAGE_FILE', image),
                ):
                    with self.assertRaisesRegex(
                        ValueError, '^telemetry_controller_identity_invalid$'
                    ):
                        controller_resource_config(config())

    def test_local_config_and_disabled_export_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'absent'
            with patch('studio_platform.telemetry._CONTROLLER_IMAGE_FILE', missing):
                self.assertEqual(controller_resource_config(config()), config())
            filename = Path(folder) / 'disabled.json'
            filename.write_text(
                json.dumps({'schema_version': 1, 'enabled': False, **vars(config())}),
                encoding='utf8',
            )
            filename.chmod(0o600)
            with patch(
                'studio_platform.telemetry.controller_resource_config'
            ) as identity:
                self.assertFalse(configured_telemetry(filename).enabled)
                identity.assert_not_called()

    def test_host_dashboard_formats_result_values_without_scaling_physical_ids_or_controls(
        self,
    ):
        dashboard = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "deploy/observability/sixnine-host-scorecard.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(dashboard["uid"], "sixnine-host-scorecard")
        tables = {
            panel["id"]: panel
            for panel in dashboard["panels"]
            if panel["type"] == "table"
        }
        for panel_id, metric_unit in ((2, "none"), (3, "s"), (4, "s"), (5, "none")):
            panel = tables[panel_id]
            defaults = panel["fieldConfig"]["defaults"]
            self.assertEqual(defaults["unit"], "none")
            overrides = panel["fieldConfig"]["overrides"]
            # Actual single-ref Loki frames expose Value #A; older frames use Value.
            for name in ("Value #A", "Value", "machine_id", "width", "frames", "steps"):
                properties = {"unit": defaults["unit"]}
                for override in overrides:
                    matcher = override["matcher"]
                    matched = (
                        matcher["id"] == "byName" and matcher["options"] == name
                    ) or (
                        matcher["id"] == "byRegexp"
                        and re.fullmatch(matcher["options"], name)
                    )
                    if matched:
                        properties.update(
                            {
                                prop["id"]: prop["value"]
                                for prop in override["properties"]
                            }
                        )
                self.assertEqual(
                    properties["unit"],
                    metric_unit if name.startswith("Value") else "none",
                )
                if name == "machine_id":
                    self.assertEqual(properties["decimals"], 0)
            self.assertEqual(panel["targets"][0]["refId"], "A")
            order = next(
                t["options"]["indexByName"]
                for t in panel["transformations"]
                if t["id"] == "organize"
            )
            self.assertLess(order["machine_id"], order["Value #A"])
            if "profile_id" in order:
                self.assertLess(order["Value #A"], order["profile_id"])
                self.assertLess(order["Value"], order["profile_id"])

    def test_https_cloud_destination_and_authorization_are_required_and_not_represented(
        self,
    ):
        self.assertNotIn("PRIVATE_WRITE_TOKEN", repr(config()))
        for endpoint in (
            "http://otlp-gateway-prod-us-east-0.grafana.net/otlp",
            "https://user:SECRET@otlp-gateway-prod-us-east-0.grafana.net/otlp",
            "https://otlp-gateway-prod-us-east-0.grafana.net/otlp?token=SECRET",
            "https://otlp-gateway-evil.grafana.net.example.com/otlp",
            "https://127.0.0.1/otlp",
        ):
            with self.assertRaisesRegex(ValueError, "^telemetry_config_invalid$"):
                CloudTelemetryConfig(endpoint, config().authorization)
        with self.assertRaisesRegex(ValueError, "^telemetry_config_invalid$"):
            CloudTelemetryConfig(config().endpoint, "Bearer SECRET")

    def test_explicit_protected_file_and_disabled_defaults_do_not_mutate_environment(
        self,
    ):
        before = dict(os.environ)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telemetry.json"
            content = {"schema_version": 1, "enabled": True, **vars(config())}
            path.write_text(json.dumps(content), encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            self.assertEqual(read_config(path), config())
            with self.assertRaisesRegex(ValueError, "^telemetry_config_invalid$"):
                read_config("relative.json")
            content["extra_secret"] = "SECRET"
            path.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "^telemetry_config_invalid$"):
                read_config(path)
            self.assertEqual(
                configured_telemetry(path).status(),
                {"enabled": False, "code": "telemetry_config_unavailable"},
            )
        self.assertEqual(before, dict(os.environ))
        self.assertEqual(NullTelemetry().call("generate", {}, lambda: 7), 7)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(configured_telemetry().enabled)

    @unittest.skipIf(os.name == "nt", "POSIX permission assertion")
    def test_group_readable_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telemetry.json"
            path.write_text(
                json.dumps({"schema_version": 1, "enabled": True, **vars(config())})
            )
            path.chmod(0o640)
            with self.assertRaisesRegex(ValueError, "^telemetry_config_invalid$"):
                read_config(path)


class SDKTests(unittest.TestCase):
    def setUp(self):
        try:
            from opentelemetry.sdk._logs import LoggerProvider
        except ImportError:
            self.skipTest("Install the optional hashed telemetry lock for SDK tests")

    def test_real_otlp_resource_contains_controller_release_and_environment(self):
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )

        bodies = []

        class Response:
            status = 204

            def close(self):
                pass

        class Pool:
            def __init__(self, **_kwargs):
                pass

            def request(self, _method, url, **kwargs):
                if url.endswith('/logs'):
                    bodies.append(kwargs['body'])
                return Response()

            def clear(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'controller-image'
            image.write_text('sixnine-platform:' + 'c' * 40, encoding='ascii')
            for environment in ('production', 'staging'):
                origin = (
                    'https://inkseq.com'
                    if environment == 'production'
                    else 'https://staging.inkseq.com'
                )
                bodies.clear()
                with (
                    patch.dict(os.environ, {'PUBLIC_ORIGIN': origin}, clear=True),
                    patch('studio_platform.telemetry._CONTROLLER_IMAGE_FILE', image),
                    patch('urllib3.PoolManager', Pool),
                ):
                    telemetry = create_cloud_telemetry(
                        controller_resource_config(config())
                    )
                    try:
                        telemetry.start('runtime_ready', {}).finish()
                        self.assertTrue(telemetry.force_flush())
                        self.assertTrue(bodies)
                        resource = (
                            ExportLogsServiceRequest.FromString(bodies[0])
                            .resource_logs[0]
                            .resource
                        )
                        attributes = {
                            a.key: a.value.string_value for a in resource.attributes
                        }
                        self.assertEqual(
                            attributes['service.name'], 'sixnine-controller'
                        )
                        self.assertEqual(attributes['service.version'], 'c' * 40)
                        self.assertEqual(
                            attributes['deployment.environment.name'], environment
                        )
                        self.assertNotIn('PRIVATE', str(resource))
                    finally:
                        telemetry.close()

    def test_real_otlp_encodings_emit_start_before_span_ends_and_never_expose_http_failure(
        self,
    ):
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        requests = []

        class Response:
            status = 204
            reason = "SECRET response text"

            def close(self):
                pass

        class Pool:
            def __init__(self, **_kwargs):
                pass

            def request(self, method, url, **kwargs):
                requests.append((method, url, kwargs["body"]))
                return Response()

            def clear(self):
                pass

        with patch("urllib3.PoolManager", Pool):
            telemetry = create_cloud_telemetry(config())
            try:
                stage = telemetry.start(
                    "generate",
                    {
                        "provider": "vast",
                        "mode": "fl",
                        "job_id": "11111111-1111-1111-1111-111111111111",
                        "prompt": "SECRET prompt",
                    },
                )
                self.assertTrue(telemetry.force_flush())
                logs = [
                    ExportLogsServiceRequest.FromString(body)
                    for _, url, body in requests
                    if url.endswith("logs")
                ]
                self.assertTrue(logs)
                self.assertIn("stage_started", str(logs[0]))
                self.assertNotIn("SECRET prompt", str(logs[0]))
                self.assertFalse(any(url.endswith("traces") for _, url, _ in requests))
                stage.finish("success")
                self.assertTrue(telemetry.force_flush())
                traces = [
                    ExportTraceServiceRequest.FromString(body)
                    for _, url, body in requests
                    if url.endswith("traces")
                ]
                metrics = [
                    ExportMetricsServiceRequest.FromString(body)
                    for _, url, body in requests
                    if url.endswith("metrics")
                ]
                self.assertTrue(traces)
                self.assertTrue(metrics)
                self.assertNotIn("job_id", str(metrics))
                self.assertNotIn("SECRET", str(logs + traces + metrics))
                Response.status = 302
                telemetry.start("collect", {}).finish()
                with self.assertLogs(
                    "opentelemetry.exporter", level="ERROR"
                ) as captured:
                    telemetry.force_flush()
                self.assertNotIn("SECRET", " ".join(captured.output))
                self.assertGreater(telemetry.status()["export_failures"], 0)
            finally:
                telemetry.close()

    def test_start_exports_asynchronously_without_waiting_for_span_completion(self):
        import threading
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )

        emitted = threading.Event()
        bodies = []

        class Response:
            status = 204

            def close(self):
                pass

        class Pool:
            def __init__(self, **_kwargs):
                pass

            def request(self, _method, url, **kwargs):
                if url.endswith("logs"):
                    bodies.append(ExportLogsServiceRequest.FromString(kwargs["body"]))
                    emitted.set()
                return Response()

            def clear(self):
                pass

        with patch("urllib3.PoolManager", Pool):
            telemetry = create_cloud_telemetry(config())
            try:
                telemetry.start("model_load", {"provider": "vast", "mode": "fl"})
                # No finish, force_flush or span end: a later crash cannot hide a
                # start already delivered by the independent background log path.
                self.assertTrue(emitted.wait(2))
                self.assertIn("stage_started", str(bodies))
                self.assertNotIn("stage_finished", str(bodies))
            finally:
                telemetry.close()

    def test_actual_sdk_queues_stay_bounded_when_cloud_is_unavailable(self):
        import threading

        entered, release = threading.Event(), threading.Event()

        class Pool:
            def __init__(self, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                entered.set()
                release.wait(2)
                raise RuntimeError("SECRET private upstream exception")

            def clear(self):
                release.set()

        with patch("urllib3.PoolManager", Pool):
            telemetry = create_cloud_telemetry(config())
            try:
                started = time.monotonic()
                for _ in range(600):
                    telemetry.start("generate", {}).finish()
                self.assertLess(time.monotonic() - started, 2)
                logs, traces, _ = telemetry.providers
                log_processor = logs._multi_log_record_processor._log_record_processors[
                    0
                ]
                span_processor = traces._active_span_processor._span_processors[0]
                self.assertLessEqual(len(log_processor._batch_processor._queue), 256)
                self.assertLessEqual(len(span_processor._batch_processor._queue), 256)
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
                telemetry.close()


if __name__ == "__main__":
    unittest.main()
