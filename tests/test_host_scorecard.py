"""Qualified original samples, typed allocation and bounded optional Cloud data."""

import copy
import json
import uuid
import unittest
import threading
from unittest.mock import Mock
from types import SimpleNamespace

from sqlalchemy import insert, select, update

import test_platform_repository as ledger
import test_dstack_operator as fixtures
import test_stage_metrics as stage_fixture
from studio_platform.host_scorecard import OperatorHosts
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import attempts, artifacts, jobs
from studio_platform.stage_metrics import CloudStageReader


class HostScorecardTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        fixtures.DstackOperatorTests._setup_dstack(self)
        self.settings.operator_admin_owners = ("superdan",)
        preview = self.service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        self.node = self.service.start(
            self.owner, {"preview_id": preview["preview_id"]}, "typed-node"
        )["node_id"]
        self.now += 10
        fixtures.DstackOperatorTests.running(self, self.node)
        self.hosts = OperatorHosts(self.repo, self.settings)

    def sample(self, *, duration=5, refs=0, error=None):
        request = {
            "deployment_profile_id": fixtures.PROFILE,
            "request": {
                "mode": "fl",
                "duration": duration,
                "steps": 20,
                "prompt": "PRIVATE prompt never selected",
                "inputs": {"images": [{}] * refs, "videos": [], "audios": []},
            },
            "output_spec": {"width": 832, "height": 480, "fps": 24, "frame_count": 124},
        }
        plan = self.repo.create_plan(
            self.scope, request, {"pool": "dstack-test"}, expires_at=self.now + 1000
        )
        job = self.repo.create_job(self.scope, plan["id"], uuid.uuid4().hex)
        attempt = str(uuid.uuid4())
        with self.repo.transaction() as conn:
            conn.execute(
                insert(attempts).values(
                    id=attempt,
                    job_id=job["id"],
                    number=1,
                    status="failed" if error else "succeeded",
                    fence=1,
                    worker_id="dstack-" + self.node.replace("-", ""),
                    created_at=self.now,
                    updated_at=self.now + 20,
                    submission_started_at=self.now,
                    upstream_task_id="original-task",
                    upstream_stopped=1,
                    collection_failures=0,
                    error_code=error,
                )
            )
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    status="failed" if error else "succeeded",
                    current_attempt_id=attempt,
                    attempt_no=1,
                    updated_at=self.now + 20,
                )
            )
            if not error:
                conn.execute(
                    insert(artifacts).values(
                        id=str(uuid.uuid4()),
                        job_id=job["id"],
                        attempt_id=attempt,
                        metadata={"kind": "video", "validated": True},
                        created_at=self.now + 20,
                    )
                )
        self.now += 30
        return job["id"], attempt

    def test_controls_are_separate_and_unknown_timings_not_fabricated(self):
        self.sample(duration=5)
        self.sample(duration=10)
        self.sample(duration=5, refs=1)
        host = self.hosts.list(self.owner)["hosts"][0]
        self.assertEqual(host["runs"], 1)
        self.assertEqual(len(host["groups"]), 3)
        self.assertEqual({g["duration_s"] for g in host["groups"]}, {5, 10})
        self.assertEqual({g["image_refs"] for g in host["groups"]}, {0, 1})
        self.assertTrue(
            all(
                g["generation_samples"] == 0
                and g["median_generation_s"] is None
                and g["cost_per_video_microusd"] is None
                and g["download_mb_s"] is None
                for g in host["groups"]
            )
        )
        self.assertTrue(
            all(
                g["cold_start_basis"] == "bootstrap_to_native_worker_ready"
                for g in host["groups"]
            )
        )
        self.assertNotIn("PRIVATE", json.dumps(host))
        self.assertEqual(self.hosts.list(self.other_owner)["hosts"], [])

    def test_bound_failures_separate_execution_from_startup_policy(self):
        self.sample(error="wangp_generation_cuda_out_of_memory")
        host = self.hosts.list(self.owner)["hosts"][0]
        self.assertEqual(host["execution_failure_count"], 1)
        self.assertEqual(host["failure_count"], 1)
        self.assertEqual(host["qualified_start_failure_count"], 0)
        self.assertEqual(host["policy"]["effective"], "default")
        with self.repo.transaction() as conn:
            row = (
                conn.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == self.node
                    )
                )
                .mappings()
                .one()
            )
            payload = copy.deepcopy(row["payload"])
            payload["stop_requested_at"] = self.now
            conn.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == self.node)
                .values(payload=payload)
            )
        last = self.hosts.list(self.owner)["hosts"][0]
        self.assertEqual(last["last_diagnostic"]["outcome"], "cancelled")
        self.assertEqual(last["failure_count"], 1)

    def test_same_account_physical_snapshot_survives_terminal_provider_absence(self):
        binding = self.store.load(self.node)
        self.store.record(
            self.node,
            {
                "state": "stopped",
                "ready": False,
                "reason_code": "dstack_run_terminal_billing_pending",
                "observed_at": self.now,
            },
        )
        host = self.hosts.list(self.owner)["hosts"][0]
        self.assertEqual(host["machine_id"], "19493")
        self.assertEqual(host["latest_observation"]["allocated_ram_mib"], 96 * 1024)
        self.assertEqual(host["rentals"][0]["state"], "stopped")
        self.assertEqual(
            self.store.load(self.node)["billing_state"], binding["billing_state"]
        )

    def test_per_offer_history_reuses_one_batch_and_matches_owner_page(self):
        self.sample()
        self.sample(error="wangp_generation_cuda_out_of_memory")
        calls = []

        def read(*, attempts, now):
            calls.append(attempts)
            return [
                {
                    "job_id": r["job_id"],
                    "attempt_id": r["attempt_id"],
                    "duration_seconds": 10,
                    "warmth": "warm",
                }
                for r in attempts
            ]

        self.hosts.stage_reader = SimpleNamespace(host_samples=read)
        page = self.hosts.list(self.owner)["hosts"][0]
        with self.repo._budget_read() as conn:
            per_offer = self.hosts.histories(
                conn,
                self.owner.owner,
                [{"offer_id": "12345", "machine_id": "19493"}],
                profile_id="5090-fl",
                observed_at=self.now,
                expires_at=self.now + 120,
            )["12345"][0]
        self.assertEqual(len(calls), 2)  # exactly one call per whole projection
        self.assertEqual(per_offer["groups"], page["groups"])
        self.assertEqual(
            per_offer["execution_failure_count"], page["execution_failure_count"]
        )
        self.assertEqual(per_offer["last_failure"], page["last_failure"])


class HostCloudSamplesTests(unittest.TestCase):
    setUp = stage_fixture.StageMetricsTests.setUp
    row = stage_fixture.StageMetricsTests.row
    reader = stage_fixture.StageMetricsTests.reader
    transport = stage_fixture.StageMetricsTests.transport

    def test_physical_host_is_closed_log_context_not_metric_labels(self):
        from studio_platform.telemetry import StageTelemetry, _context

        value = object.__new__(StageTelemetry)
        value._closed, value.timestamp, value.logger = False, lambda: 1000000000, Mock()
        value._lock, value._labelsets = threading.Lock(), set()
        raw = {
            "machine_id": "19492",
            "offer_id": "12345",
            "provider_instance_id": "1234567",
            "physical_id_source": "vast_machine_id",
            "observed_at": 1000,
            "provider": "vast",
            "profile_id": self.profile,
            "mode": "fl",
            "simulation": False,
            "node_id": self.node,
            "PRIVATE": "key-not-exported",
        }
        value.host_event(raw)
        emitted = json.loads(value.logger.emit.call_args.kwargs["body"])
        self.assertEqual(emitted["event"], "host_allocation_observed")
        self.assertEqual(emitted["machine_id"], "19492")
        self.assertEqual(emitted["host_identity_source"], "vast_machine_id")
        self.assertNotIn("PRIVATE", emitted)
        labels = value._labels("generate", _context(raw))
        for name in ("machine_id", "offer_id", "provider_instance_id", "node_id"):
            self.assertNotIn(name, labels)

    def test_host_page_is_one_query_and_original_samples_are_not_averages(self):
        attempts = []
        self.rows = []
        for duration in (10, 20, 120):
            self.job, self.attempt = str(uuid.uuid4()), str(uuid.uuid4())
            self.rows.append(self.row(duration_seconds=duration, warmth="warm"))
            attempts.append(
                {
                    **self.original,
                    "job_id": self.job,
                    "attempt_id": self.attempt,
                    "node_id": self.node,
                    "profile_id": self.profile,
                    "mode": "ref",
                    "provider": "vast",
                }
            )
        reader = self.reader()
        value = reader.host_samples(attempts=attempts, now=self.now)
        self.assertEqual(sorted(r["duration_seconds"] for r in value), [10, 20, 120])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(reader.host_samples(attempts=attempts, now=self.now), value)
        self.assertEqual(len(self.requests), 1)
        self.rows *= 100
        self.tick += 61
        self.assertEqual(reader.host_samples(attempts=attempts, now=self.now), [])
