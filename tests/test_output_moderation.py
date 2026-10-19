"""Atomic output refusal with disposable ledgers and fake R2, never provider IO."""

from contextlib import contextmanager
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from sqlalchemy import insert, select

from studio_platform.artifact_writer import ArtifactWriter
from studio_platform.credit_schema import entries
from studio_platform.queue import TaskQueue
from studio_platform.repository import LeaseLost, artifacts, documents, outbox
from studio_platform.storage import S3ObjectStore
from studio_platform.storage_asset_journal import artifact_accounting
from studio_platform.worker import WorkerRunner
import test_credits as credit_fixtures
from test_platform_storage import config
from test_platform_storage_multipart import FakeMultipartS3


class OutputModerationTests(credit_fixtures.CreditTests):
    def setUp(self):
        super().setUp()
        network = patch.object(
            socket.socket, "connect", side_effect=AssertionError("No network allowed")
        )
        network.start()
        self.addCleanup(network.stop)
        self.work = Path(self.temp.name) / "output-review"
        selected = config()
        self.fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=self.fake)
        self.writer = ArtifactWriter(
            self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id
        )
        self.queue = TaskQueue(self.repo)

    def collecting(self):
        self.seed()
        original = self.credit_job()
        self.hold(original)
        claim = self.queue.claim("output-review", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "original-provider-task")
        job = self.queue.begin_collection(claim.lease)
        tag = "attempt-" + uuid.uuid4().hex
        directory = self.work / tag
        directory.mkdir(parents=True)
        media = directory / "verified.mp4"
        media.write_bytes(b"synthetic verified video bytes")
        raw = directory / "native.mp4"
        raw.write_bytes(b"synthetic native provider bytes")
        self.writer.begin_staging(job, claim.lease.attempt_id, tag, kinds=("video",))
        record = self.writer.prepare(
            job,
            claim.lease.attempt_id,
            tag,
            [("video", media, "video/mp4", {})],
            raw_paths=[raw],
        )
        specs = self.writer.write(record)
        return job, claim.lease, tag, record, specs

    @staticmethod
    def decision(status="refused"):
        # Service receipt persistence/authenticity has its own tests. These
        # transaction tests stub only that verifier, never money/storage writes.
        return {
            "status": status,
            "policy_revision": "reviewed-policy-1",
            "policy_version": "reviewed-version-1",
            "input_hash": "d" * 64,
            "reason_code": "content_not_allowed" if status == "refused" else "allowed",
        }

    def finish(self, values, *, cost=80_000, decision=None):
        job, lease, tag, record, specs = values
        decision = self.decision() if decision is None else decision
        with patch(
            "studio_platform.content_policy.verify_output", return_value=decision
        ):
            return self.queue.complete(
                lease,
                specs,
                actual_cost_microusd=cost,
                settlement=self.writer.settlement(record),
                moderation=decision,
            )

    def kinds(self):
        with self.repo.engine.connect() as connection:
            return (
                connection.execute(
                    select(entries.c.kind).where(entries.c.user_id == self.user_id)
                )
                .scalars()
                .all()
            )

    def assert_private_settled(self, values):
        job, lease, tag, record, specs = values
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(artifacts.c.id)).all(), [])
            self.assertTrue(connection.execute(select(artifact_accounting)).all())
        self.assertEqual(
            self.writer.get(job, lease.attempt_id, tag)["phase"], "settled"
        )
        self.assertEqual(len(self.fake.objects), 1)
        self.assertGreater(
            self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 0
        )

    def runner(self):
        backend = SimpleNamespace(kind="synthetic", submit=Mock(), poll=Mock())
        return WorkerRunner(self.repo, self.store, self.work, backend=backend), backend

    def test_output_refusal_retains_provider_storage_and_releases_customer_once(self):
        values = self.collecting()
        result = self.finish(values)
        self.assertEqual(
            (result["status"], result["error_code"]),
            ("failed", "content_moderation_refused"),
        )
        self.assertEqual(result["result"]["artifact_ids"], [])
        self.assertEqual(result["result"]["moderation"], self.decision())
        self.assertEqual(result["result"]["actual_cost_microusd"], 80_000)
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 0))
        self.assertCountEqual(self.kinds(), ["grant", "hold", "release"])
        self.assert_private_settled(values)
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(
            (budget["spent_microusd"], budget["reserved_microusd"]), (80_000, 0)
        )
        attempt = self.queue.get_attempt(self.scope, result["id"])
        self.assertEqual(
            (
                attempt["status"],
                attempt["upstream_stopped"],
                attempt["actual_cost_microusd"],
            ),
            ("failed", 1, 80_000),
        )
        with self.repo.engine.connect() as connection:
            event = connection.execute(
                select(outbox.c.payload).where(outbox.c.event_type == "job.failed")
            ).scalar_one()
        self.assertEqual(event["moderation"], self.decision())
        with self.assertRaises(LeaseLost):
            self.finish(values)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertCountEqual(self.kinds(), ["grant", "hold", "release"])
        self.assert_invariants()

    def test_output_unknown_provider_cost_stays_pending_then_settles_without_capture(
        self,
    ):
        values = self.collecting()
        result = self.finish(values, cost=None)
        self.assertEqual(result["result"]["billing_status"], "pending")
        self.assertIsNone(result["result"]["actual_cost_microusd"])
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 0))
        self.assert_private_settled(values)
        for _ in range(2):
            result = self.repo.settle_completed_job(
                self.scope, result["id"], actual_cost_microusd=90_000
            )
        self.assertEqual(result["result"]["moderation"], self.decision())
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 90_000)
        self.assertCountEqual(self.kinds(), ["grant", "hold", "release"])
        self.assert_invariants()

    def test_output_allowed_after_cancel_keeps_original_capture_and_cost(self):
        values = self.collecting()
        self.repo.request_cancel(self.scope, values[0]["id"])
        result = self.finish(values, decision=self.decision("allowed"))
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["result"]["completed_after_cancel_request"])
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (80, 0))
        self.assertCountEqual(self.kinds(), ["grant", "hold", "capture"])
        with self.repo.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(artifacts.c.id)).all()), 1)
        self.assert_invariants()

    def test_output_refusal_after_cancel_keeps_cancellation_and_no_capture(self):
        values = self.collecting()
        self.repo.request_cancel(self.scope, values[0]["id"])
        result = self.finish(values)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["result"]["completed_after_cancel_request"])
        self.assertIsNotNone(result["cancellation"])
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 0))
        self.assert_private_settled(values)

    def test_output_transaction_failure_rolls_back_storage_provider_and_credit_together(
        self,
    ):
        values = self.collecting()
        before = self.repo.get_budget("owner-budget")
        with patch.object(
            self.repo, "_emit", side_effect=RuntimeError("synthetic event failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic event failure"):
                self.finish(values)
        job, lease, tag, record, specs = values
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["status"], "collecting"
        )
        self.assertEqual(
            self.writer.get(job, lease.attempt_id, tag)["phase"], "reserved"
        )
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 20))
        self.assertCountEqual(self.kinds(), ["grant", "hold"])
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(artifact_accounting)).all(), [])
        self.assertEqual(self.finish(values)["status"], "failed")
        self.assert_private_settled(values)
        self.assert_invariants()

    def test_output_missing_verified_receipt_cannot_publish_or_settle(self):
        from studio_platform.content_policy import ContentPolicyError

        values = self.collecting()
        job, lease, tag, record, specs = values
        settlement = Mock(wraps=self.writer.settlement(record))
        with patch(
            "studio_platform.content_policy.verify_output",
            side_effect=ContentPolicyError("content_moderation_unavailable"),
        ):
            with self.assertRaises(ContentPolicyError):
                self.queue.complete(
                    lease, specs, actual_cost_microusd=80_000, settlement=settlement
                )
        settlement.assert_not_called()
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["status"], "collecting"
        )
        self.assertEqual(self.balance()["held"], 20)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(artifacts.c.id)).all(), [])

    def test_output_unavailable_retries_collection_same_attempt_without_provider_submit(
        self,
    ):
        from studio_platform.content_policy import ContentPolicyError

        values = self.collecting()
        job, lease, tag, record, specs = values
        runner, backend = self.runner()
        with patch(
            "studio_platform.content_policy.check_output",
            side_effect=ContentPolicyError("content_moderation_unavailable"),
        ):
            result = runner._complete_collection(
                self.writer, job, lease, tag, specs, 80_000, record
            )
        self.assertEqual(result["state"], "collecting")
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["error_code"],
            "content_moderation_unavailable",
        )
        self.assertEqual(self.balance()["held"], 20)
        self.now += 31
        claim = self.queue.claim("original-collector", "test-pool", purpose="collect")
        self.assertEqual(claim.lease.attempt_id, lease.attempt_id)
        with (
            patch(
                "studio_platform.content_policy.check_output",
                return_value=self.decision(),
            ),
            patch(
                "studio_platform.content_policy.verify_output",
                return_value=self.decision(),
            ),
            patch("studio_platform.artifact_renditions.ensure") as renditions,
        ):
            result = runner._complete_collection(
                self.writer, claim.job, claim.lease, tag, specs, 80_000, record
            )
        self.assertEqual(result["state"], "failed")
        backend.submit.assert_not_called()
        backend.poll.assert_not_called()
        renditions.assert_not_called()
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assert_invariants()

    def test_output_settled_refusal_replay_cleans_only_scratch(self):
        values = self.collecting()
        self.finish(values)
        job, lease, tag, record, specs = values
        runner, backend = self.runner()
        before_budget, before_balance = (
            self.repo.get_budget("owner-budget"),
            self.balance(),
        )
        with (
            patch("studio_platform.content_policy.check_output") as classifier,
            patch("studio_platform.artifact_renditions.ensure") as renditions,
        ):
            result = runner._collect(
                job, lease, tag, "original-provider-task", lambda: None
            )
        self.assertEqual(result["state"], "failed")
        self.assertFalse((self.work / tag).exists())
        self.assertEqual(len(self.fake.objects), 1)
        self.assertEqual(self.repo.get_budget("owner-budget"), before_budget)
        self.assertEqual(self.balance(), before_balance)
        classifier.assert_not_called()
        renditions.assert_not_called()
        backend.submit.assert_not_called()
        backend.poll.assert_not_called()

    def test_output_classifier_runs_outside_atomic_verifier_transaction(self):
        values = self.collecting()
        job, lease, tag, record, specs = values
        runner, _ = self.runner()
        original_transaction = self.repo.transaction
        active = []

        @contextmanager
        def transaction():
            with original_transaction() as connection:
                active.append(connection)
                try:
                    yield connection
                finally:
                    active.pop()

        def classify(repo, original, attempt_id, media):
            self.assertFalse(active)
            self.assertIs(repo, self.repo)
            self.assertEqual(
                (original["id"], attempt_id, media),
                (job["id"], lease.attempt_id, specs),
            )
            return self.decision()

        def verify(connection, original, attempt_id, media, receipt):
            self.assertEqual(active, [connection])
            self.assertEqual(receipt, self.decision())
            return receipt

        with (
            patch.object(self.repo, "transaction", side_effect=transaction),
            patch("studio_platform.content_policy.check_output", side_effect=classify),
            patch("studio_platform.content_policy.verify_output", side_effect=verify),
        ):
            result = runner._complete_collection(
                self.writer, job, lease, tag, specs, 80_000, record
            )
        self.assertEqual(result["state"], "failed")

    def test_output_persisted_classifier_receipt_reaches_atomic_refusal(self):
        from studio_platform import content_policy

        values = self.collecting()
        job, lease, tag, record, specs = values
        with self.repo.transaction() as connection:
            connection.execute(
                insert(documents).values(
                    tenant_id=self.scope.tenant_id,
                    **content_policy.POLICY_SCOPE,
                    version=1,
                    payload={
                        "revision": "synthetic-output-1",
                        "version": "synthetic-test-only",
                        "status": "draft",
                        "moderation": {
                            "enabled": True,
                            "keywords": [],
                            "output_classifier": "synthetic-refusal",
                        },
                    },
                    updated_at=self.now,
                )
            )
        classifier = Mock(return_value="refused")
        runner, backend = self.runner()
        with (
            patch.dict(content_policy.CLASSIFIERS, {"synthetic-refusal": classifier}),
            patch("studio_platform.artifact_renditions.ensure") as renditions,
        ):
            result = runner._complete_collection(
                self.writer, job, lease, tag, specs, 80_000, record
            )
        self.assertEqual(result["state"], "failed")
        classifier.assert_called_once()
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 0))
        current = self.repo.get_job(self.scope, job["id"])
        receipt = current["result"]["moderation"]
        self.assertEqual(receipt["status"], "refused")
        self.assertEqual(receipt["reason_code"], "classifier_refused")
        self.assert_private_settled(values)
        renditions.assert_not_called()
        backend.submit.assert_not_called()
        backend.poll.assert_not_called()
        self.assert_invariants()


def load_tests(loader, tests, pattern):
    # Reuse only the credit fixture helpers, without repeating its invariant suite.
    return unittest.TestSuite(
        OutputModerationTests(name)
        for name in OutputModerationTests.__dict__
        if name.startswith("test_output_")
    )


if __name__ == "__main__":
    unittest.main()
