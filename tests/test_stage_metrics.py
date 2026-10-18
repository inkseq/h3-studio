"""Bounded fake Cloud reads; no credentials or provider/GPU operations."""
import base64
import json
import math
import tempfile
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit
import uuid

from studio_platform.stage_metrics import (CloudStageReader, MAX_ROWS,
    StageQueryConfig, configured_stage_reader, read_config)


class StageMetricsTests(unittest.TestCase):
    def setUp(self):
        self.config = StageQueryConfig("https://logs-prod-test.grafana.net", "Basic " +
            base64.b64encode(b"123:synthetic-read-only-token").decode())
        self.now, self.tick = 2000000.0, 100.0
        self.node, self.job, self.attempt = (str(uuid.uuid4()) for _ in range(3))
        self.profile = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3"
        self.original = {"job_id": self.job, "attempt_id": self.attempt,
            "submission_started_at": self.now-300, "completed_at": self.now-100}
        self.kwargs = {"node_id": self.node, "provider": "vast", "profile_id": self.profile,
            "mode": "ref", "attempts": [self.original], "now": self.now}
        self.requests, self.rows = [], [self.row()]

    def row(self, **changes):
        value = {"schema_version": 1, "event": "stage_finished", "stage": "generate",
            "stage_id": uuid.uuid4().hex, "node_id": self.node, "job_id": self.job,
            "attempt_id": self.attempt, "provider": "vast", "profile_id": self.profile,
            "mode": "ref", "simulation": False, "outcome": "success",
            "timing_basis": "durable_cpu_interval", "duration_seconds": 140,
            "interval_start_unix_ns": int((self.now-300)*1e9), "trace_id": "a"*32}
        value.update(changes)
        return [str(int((self.now-150)*1e9)), json.dumps(value)]

    def transport(self, config, path, timeout):
        self.requests.append((config, path, timeout))
        return {"status": "success", "data": {"resultType": "streams",
            "result": [{"stream": {"service_name": "sixnine-worker"}, "values": self.rows}]}}

    def reader(self, transport=None):
        return CloudStageReader(self.config, transport=transport or self.transport,
            monotonic=lambda: self.tick)

    def test_original_exact_identity_and_duration_are_correlated(self):
        value = self.reader().generate(**self.kwargs)
        self.assertEqual(value["average_generate_seconds"], 140)
        self.assertEqual(value["generate_samples"], 1)
        self.assertEqual(value["generation_basis"], "grafana_loki_submit_to_upstream_terminal")
        self.assertEqual(value["stage_query_status"], "ok")
        query = parse_qs(urlsplit(self.requests[0][1]).query)
        self.assertEqual(query["limit"], [str(MAX_ROWS)])
        self.assertEqual(query["direction"], ["backward"])
        self.assertEqual(self.requests[0][2], 1.0)
        self.assertIn('| node_id="'+self.node+'"', query["query"][0])
        self.assertNotIn(self.attempt, query["query"][0].split("}")[0])
        self.assertNotIn("synthetic-read-only-token", json.dumps(value))

    def test_wrong_identity_failure_and_simulation_do_not_qualify(self):
        for changes in ({"job_id": str(uuid.uuid4())}, {"attempt_id": str(uuid.uuid4())},
                {"node_id": str(uuid.uuid4())}, {"profile_id": "other-profile"},
                {"provider": "runpod"}, {"mode": "fl"}, {"simulation": True},
                {"simulation": "false"}, {"outcome": "failure"}, {"stage": "collect"},
                {"timing_basis": "monotonic"}, {"interval_start_unix_ns": 1000000000}):
            with self.subTest(changes=changes):
                self.rows = [self.row(**changes)]
                value = self.reader().generate(**self.kwargs)
                self.assertIsNone(value["average_generate_seconds"])
                self.assertEqual(value["generate_samples"], 0)

    def test_duplicate_stage_and_recovered_same_interval_count_one_video(self):
        first = self.row()
        self.rows = [first, first, self.row()]
        value = self.reader().generate(**self.kwargs)
        self.assertEqual(value["average_generate_seconds"], 140)
        self.assertEqual(value["generate_samples"], 1)

    def test_conflicting_stage_id_or_attempt_durations_remain_unknown(self):
        first = self.row()
        data = json.loads(first[1])
        for second in (self.row(stage_id=data["stage_id"], duration_seconds=150),
                       self.row(duration_seconds=150)):
            self.rows = [first, second]
            value = self.reader().generate(**self.kwargs)
            self.assertIsNone(value["average_generate_seconds"])

    def test_bounded_attempt_window_and_invalid_ids_never_query(self):
        cases = ({"node_id": '" | drop'}, {"profile_id": "secret/prompt"},
            {"now": float("nan")}, {"attempts": [{**self.original,
                "submission_started_at": self.now-8*86400}]}, {"attempts": []})
        for changes in cases:
            with self.subTest(changes=changes):
                value = self.reader().generate(**{**self.kwargs, **changes})
                self.assertIsNone(value["average_generate_seconds"])
        self.assertEqual(self.requests, [])

    def test_recent_attempts_are_capped_at_32_and_bounded_7_days(self):
        originals = [{**self.original, "attempt_id": str(uuid.uuid4()),
            "completed_at": self.now-index-1} for index in range(40)]
        self.rows = []
        self.reader().generate(**{**self.kwargs, "attempts": originals})
        query = parse_qs(urlsplit(self.requests[0][1]).query)["query"][0]
        self.assertIn(originals[0]["attempt_id"], query)
        self.assertNotIn(originals[32]["attempt_id"], query)

    def test_cache_is_short_scoped_and_caches_outages(self):
        reader = self.reader()
        first = reader.generate(**self.kwargs)
        first["average_generate_seconds"] = 999
        self.assertEqual(reader.generate(**self.kwargs)["average_generate_seconds"], 140)
        self.assertEqual(len(self.requests), 1)
        self.tick += 61
        reader.generate(**self.kwargs)
        self.assertEqual(len(self.requests), 2)
        reader.generate(**{**self.kwargs, "node_id": str(uuid.uuid4())})
        self.assertEqual(len(self.requests), 3)

    def test_truncated_cloud_result_is_not_a_partial_average(self):
        self.rows = [self.row() for _ in range(MAX_ROWS)]
        value = self.reader().generate(**self.kwargs)
        self.assertEqual(value["stage_query_status"], "query_truncated")
        self.assertIsNone(value["average_generate_seconds"])

    def test_duration_outside_original_interval_is_not_gpu_time(self):
        for duration in (True, -1, 0, 999, float("nan"), float("inf")):
            self.rows = [self.row(duration_seconds=duration)]
            self.assertIsNone(self.reader().generate(**self.kwargs)["average_generate_seconds"])
        self.rows = [[str(int((self.now+60)*1e9)), self.row()[1]]]
        self.assertIsNone(self.reader().generate(**self.kwargs)["average_generate_seconds"])

    def test_transport_failures_are_sanitized_and_optional(self):
        def broken(_config, _path, _timeout):
            raise RuntimeError("Authorization: secret-token private-prompt")
        value = self.reader(broken).generate(**self.kwargs)
        self.assertEqual(value["stage_query_status"], "query_unavailable")
        self.assertNotIn("secret-token", json.dumps(value))
        self.assertNotIn("synthetic", repr(self.config))

    def test_config_rejects_ingest_endpoint_redirect_target_and_other_scope(self):
        for endpoint in ("http://logs-prod-test.grafana.net", "https://otlp-gateway-test.grafana.net/otlp",
                "https://logs-prod-test.grafana.net/loki/api/v1/query", "https://attacker.example",
                "https://logs-prod-test.grafana.net/?secret=value", "https://user@logs-prod-test.grafana.net"):
            with self.assertRaisesRegex(ValueError, "^stage_query_config_invalid$"):
                StageQueryConfig(endpoint, self.config.authorization)
        with self.assertRaisesRegex(ValueError, "^stage_query_config_invalid$"):
            StageQueryConfig(self.config.endpoint, self.config.authorization, "logs:write")

    def test_protected_explicit_config_contract_and_disabled_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"read.json"
            value = {"schema_version": 1, "enabled": True, "endpoint": self.config.endpoint,
                "authorization": self.config.authorization, "scope": "logs:read"}
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(read_config(path).endpoint, self.config.endpoint)
            value["enabled"] = False
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(read_config(path))
            self.assertEqual(configured_stage_reader(path).generate()["stage_query_status"], "disabled")
            path.write_text("private-token-invalid", encoding="utf-8")
            self.assertEqual(configured_stage_reader(path).generate()["stage_query_status"], "config_unavailable")
        self.assertEqual(configured_stage_reader(None).generate()["stage_query_status"], "not_configured")


if __name__ == "__main__":
    unittest.main()
