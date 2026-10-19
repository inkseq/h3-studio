"""Safe failed-Session evidence across real journals/queue; no models or network."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.inference.protocol import Outcome, safe_failure_code
from studio_platform.inference.wangp import WanGPBackend
from studio_platform.inference.wangp_http import HTTPWanGPTransport
from studio_platform.inference.wangp_contract import (
    OperationReceipt,
    RuntimeObservation,
    canonical_json,
    operation_id,
)
from studio_platform.worker import WorkerRunner
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.repository import attempts, budget_reservations
from studio_platform.runtime_hosts.wangp import WanGPHost
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
from studio_platform.runtime_hosts.wangp_session import (
    PinnedWanGPSession,
    _failure_code,
)
from studio_platform.runtime_hosts.wangp_http import StagedInputs, create_app
from test_platform_repository import LedgerCase
from test_platform_wangp_receipts import manifest, prepared
import test_platform_wangp_session as session_fixture


PRIVATE = "PRIVATE-PROMPT /private/input.mp4 https://private.invalid/?token=SECRET"


class ClassificationTests(unittest.TestCase):
    def test_first_upstream_stage_and_signature_are_static_not_raw_or_exception_claims(
        self,
    ):
        cases = (
            ("generation", "CUDA out of memory. " + PRIVATE, "cuda_out_of_memory"),
            (
                "generation",
                "The size of tensor a (7) must match tensor b " + PRIVATE,
                "tensor_shape_mismatch",
            ),
            (
                "runtime",
                "No module named 'PRIVATE-DEPENDENCY' " + PRIVATE,
                "dependency_missing",
            ),
            (
                "validation",
                "Invalid data found when processing input " + PRIVATE,
                "media_decode_failed",
            ),
            (
                "generation",
                "Seed must be between 0 and 2**32 - 1 " + PRIVATE,
                "seed_out_of_range",
            ),
            ("validation", PRIVATE, "unclassified"),
        )
        for stage, message, category in cases:
            with self.subTest(stage=stage, category=category):
                code = _failure_code(
                    [
                        NS(stage=stage, message=message),
                        NS(stage="runtime", message="later error"),
                    ]
                )
                self.assertEqual(code, f"wangp_{stage}_{category}")
                self.assertEqual(safe_failure_code(code), code)
                self.assertNotIn("PRIVATE", code)
        self.assertEqual(
            _failure_code([NS(stage=PRIVATE, message=PRIVATE)]),
            "wangp_unknown_unclassified",
        )
        self.assertEqual(
            _failure_code(
                [NS(stage="generation", message="x" * 4096 + "CUDA out of memory")]
            ),
            "wangp_generation_unclassified",
        )

    def test_unknown_shape_and_arbitrary_codes_never_persist_text(self):
        class Unprintable:
            def __str__(self):
                raise AssertionError("must not stringify upstream diagnostics")

        for errors in (
            None,
            {},
            [],
            [Unprintable()],
            [NS(stage=Unprintable(), message=Unprintable())],
        ):
            self.assertEqual(_failure_code(errors), "wangp_unknown_unclassified")
        for code in (PRIVATE, "wangp_generation_" + PRIVATE, "RuntimeError", [], None):
            self.assertIsNone(safe_failure_code(code))
        self.assertIsNone(Outcome("failed", "task").error_code)  # legacy constructor


class FailureRetentionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.engine = manifest()
        self.result = NS(
            success=False,
            cancelled=False,
            errors=[NS(stage="generation", message="CUDA out of memory. " + PRIVATE)],
        )
        self.upstream_job = session_fixture.FakeJob(self.result)
        self.upstream = session_fixture.FakeSession(self.upstream_job)
        self.alive, self.quiesced = False, []
        self.session = PinnedWanGPSession(
            self.upstream,
            self.outputs,
            worker_alive=lambda: self.alive,
            quiesce=lambda: self.quiesced.append(True),
        )
        self.journal_path = self.root / "runtime-journal" / "receipts.sqlite"
        self.journal = ReceiptJournal(
            self.journal_path,
            slot_key="slot-1",
            manifest_digest=self.engine.digest,
            create=True,
        )
        self.host = self.make_host(self.journal)
        self.backend = WanGPBackend(
            enabled=True,
            slot_key="slot-1",
            manifest=self.engine,
            transport=self.host,
            compiler=lambda job, tag, store, heartbeat: replace(
                prepared(tag=tag, audio=False),
                job_id=job["id"],
                request_hash=job["request_hash"],
            ),
        )
        self.control = WorkerControl(self.repo)
        self.control.register(
            WorkerSpec(
                "worker",
                "finite-pool",
                "synthetic-provider",
                "synthetic-instance",
                ("synthetic-device",),
                ("synthetic-fl",),
                "synthetic-model",
                "synthetic-config",
                "wangp-worker",
                self.engine.digest,
            )
        )
        self.control.mark_ready("worker", upstream_idle_confirmed=True)
        self.identity = {
            "intent_id": "synthetic-intent",
            "instance_id": "synthetic-instance",
            "configuration_id": "synthetic-config",
            "qualification_profile": QUEUED_TASK_PROFILE,
            "backend": "wangp-worker",
            "engine_manifest_digest": self.engine.digest,
            "sources": {
                "wangp-bootstrap.py": "a" * 64,
                "wangp-manifest.json": "b" * 64,
            },
        }
        self.evidence = self.root / "control" / "queued-task-evidence.json"

    def make_host(self, journal):
        host = WanGPHost(
            session=self.session,
            journal=journal,
            manifest=self.engine,
            output_root=self.outputs,
            sealed_root=self.root / "sealed",
        )
        self.addCleanup(host.close)
        return host

    def tearDown(self):
        self.host.close()
        super().tearDown()

    def new_job(self, key="original"):
        request = {
            "recipe_id": "synthetic-fl",
            "request": {
                "model": "synthetic-model",
                "duration": 5,
                "resolution": "480P",
                "steps": 50,
                "generate_audio": False,
                "prompt": "synthetic",
            },
            "assets": {},
            "output_spec": json.loads(prepared().output_spec_json),
        }
        plan = self.repo.create_plan(
            self.scope,
            request,
            {
                "pool": "finite-pool",
                "backend": "wangp-worker",
                "enabled": True,
                "configuration_id": "synthetic-config",
                "engine_manifest_digest": self.engine.digest,
                "expected_runtime_s": 10,
            },
            expires_at=self.now + 3600,
            estimated_cost_microusd=100_000,
        )
        return self.repo.create_job(
            self.scope, plan["id"], key, budget_account_ids=("owner-budget",)
        )

    def runner(self):
        return WorkerRunner(
            self.repo,
            None,
            self.root / "worker",
            backend=self.backend,
            control=self.control,
            retry_after_s=0,
            submission_guard=lambda _: True,
        )

    def begin(self):
        job = self.new_job()
        runner = self.runner()
        runner.run_once("worker", "finite-pool")
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(len(self.upstream.calls), 1)
        return current, runner

    def finish(self, runner):
        self.upstream_job.done, self.upstream.active_job = True, None
        return runner.run_once("worker", "finite-pool")

    def use_private_http(self):
        token = "synthetic-diagnostics-only-" + "x" * 32
        client = TestClient(
            create_app(self.host, StagedInputs(self.root / "inputs"), token=token)
        )
        self.addCleanup(client.close)
        calls = []

        def dispatch(request):
            calls.append((request.method, request.url.path))
            response = client.request(
                request.method,
                request.url.raw_path.decode(),
                headers=dict(request.headers),
                content=request.read(),
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers=dict(response.headers),
            )

        transport = HTTPWanGPTransport(
            "http://127.0.0.1:8199",
            token,
            transport=httpx.MockTransport(dispatch),
            expected_incarnation=self.host.incarnation,
        )
        self.addCleanup(transport.close)
        self.backend.transport = transport
        return calls

    def test_classified_failure_crosses_private_http_once_and_is_durable(self):
        self.result.errors = [
            NS(
                stage="generation",
                message="Seed must be between 0 and 2**32 - 1 " + PRIVATE,
            )
        ]
        calls = self.use_private_http()
        original, runner = self.begin()
        self.assertEqual(self.finish(runner)["state"], "failed")
        current = self.repo.get_job(self.scope, original["id"])
        code = "wangp_generation_seed_out_of_range"
        self.assertEqual(current["error_code"], code)
        receipt = self.backend.transport.inspect(
            operation_id(current["current_attempt_id"])
        )
        self.assertEqual(receipt.reason, code)
        self.assertEqual((receipt.stop_proven, receipt.job_id), (True, original["id"]))
        self.assertEqual(calls.count(("POST", "/v1/operations")), 1)
        self.assertNotIn("PRIVATE", canonical_json(receipt.to_dict()))

    def test_unrecognized_upstream_error_crosses_http_as_static_unknown(self):
        self.result.errors = [NS(stage=PRIVATE, message=PRIVATE)]
        calls = self.use_private_http()
        original, runner = self.begin()
        self.assertEqual(self.finish(runner)["state"], "failed")
        current = self.repo.get_job(self.scope, original["id"])
        code = "wangp_unknown_unclassified"
        self.assertEqual(current["error_code"], code)
        self.assertEqual(calls.count(("POST", "/v1/operations")), 1)

    def test_failed_session_survives_host_and_cpu_restart_without_replay_or_secret_text(
        self,
    ):
        original, runner = self.begin()
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(self.finish(runner)["state"], "failed")
        current = self.repo.get_job(self.scope, original["id"])
        code = "wangp_generation_cuda_out_of_memory"
        self.assertEqual(current["error_code"], code)
        self.assertEqual(current["current_attempt_id"], original["current_attempt_id"])
        self.assertEqual(
            (current["attempt_no"], current["request_hash"]),
            (1, original["request_hash"]),
        )
        with self.repo.engine.connect() as connection:
            attempt = (
                connection.execute(
                    select(attempts).where(
                        attempts.c.id == current["current_attempt_id"]
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(
            (attempt["error_code"], attempt["upstream_stopped"]), (code, 1)
        )
        receipt = self.host.inspect(attempt["upstream_task_id"])
        self.assertEqual(OperationReceipt.from_dict(receipt.to_dict()).reason, code)
        self.assertEqual(
            self.repo.get_budget("owner-budget"), {**budget, "reserved_microusd": 0}
        )
        with self.repo.engine.connect() as conn:
            invoice = (
                conn.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_type == "job",
                        budget_reservations.c.reference_id == current["id"],
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(
            (
                invoice["state"],
                invoice["amount_microusd"],
                invoice["actual_cost_microusd"],
            ),
            ("invoice_pending", budget["reserved_microusd"], None),
        )
        self.host.close()
        self.journal = ReceiptJournal(
            self.journal_path, slot_key="slot-1", manifest_digest=self.engine.digest
        )
        self.host = self.make_host(self.journal)
        self.backend.transport = self.host
        self.assertEqual(self.host.inspect(attempt["upstream_task_id"]).reason, code)
        fresh = self.runner()
        fresh.run_once("worker", "finite-pool")
        self.assertEqual(len(self.upstream.calls), 1)
        for text in (
            canonical_json(receipt.to_dict()),
            self.journal_path.read_bytes().decode("utf-8", errors="ignore"),
        ):
            for forbidden in (
                "PRIVATE-PROMPT",
                "private.invalid",
                "SECRET",
                "/private/input.mp4",
            ):
                self.assertNotIn(forbidden, text)

    def test_failed_diagnostic_write_retries_original_handle_not_generation(self):
        original, runner = self.begin()
        transition = self.journal.transition
        fail_once = [True]

        def interrupted(*args, **kwargs):
            if kwargs.get("state") == "failed" and fail_once.pop():
                raise OSError(PRIVATE)
            return transition(*args, **kwargs)

        # One failed terminal commit; the original handle must still be available.
        with patch.object(self.journal, "transition", side_effect=interrupted):
            self.finish(runner)
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "running"
        )
        self.now += 31
        self.assertEqual(runner.run_once("worker", "finite-pool")["state"], "failed")
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["error_code"],
            "wangp_generation_cuda_out_of_memory",
        )
        self.assertEqual(len(self.upstream.calls), 1)

    def test_diagnostic_does_not_turn_live_worker_into_stop_proof(self):
        original, runner = self.begin()
        self.alive = True
        self.finish(runner)
        current = self.repo.get_job(self.scope, original["id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["error_code"], "upstream_status_unknown")
        self.assertEqual(self.quiesced, [])
        self.assertEqual(len(self.upstream.calls), 1)

    def test_cancelled_result_ignores_failure_code_and_remains_cancelled(self):
        original, runner = self.begin()
        self.repo.request_cancel(self.scope, original["id"])
        self.result.cancelled = True
        self.assertEqual(self.finish(runner)["state"], "cancelled")
        self.assertNotEqual(
            self.repo.get_job(self.scope, original["id"])["error_code"],
            "wangp_generation_cuda_out_of_memory",
        )
        self.assertEqual(len(self.upstream.calls), 1)

    def test_worker_rejects_arbitrary_transport_text_and_preserves_legacy_generic_failure(
        self,
    ):
        original, runner = self.begin()
        with patch.object(
            self.backend, "poll", return_value=Outcome("failed", "task", None, PRIVATE)
        ):
            runner.run_once("worker", "finite-pool")
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["error_code"],
            "upstream_generation_failed",
        )


if __name__ == "__main__":
    unittest.main()
