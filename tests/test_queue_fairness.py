"""Fair generation dispatch keeps the original ledger, fences and recovery paths."""

from collections import Counter
from unittest.mock import patch

from sqlalchemy import insert, select

from studio_platform.capacity_settings import DEFAULTS, metadata
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Scope,
    attempts,
    scheduler_state,
    spending_settings,
)
from studio_platform.spending_limits import ADMIN_OWNER
from test_platform_queue import VALID_ARTIFACT
from test_platform_repository import LedgerCase


class FairQueueTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.queue = TaskQueue(self.repo)
        self.adopt(self.scope.tenant_id)

    def adopt(self, tenant):
        with self.repo.transaction() as connection:
            connection.execute(
                insert(spending_settings).values(
                    tenant_id=tenant,
                    owner_id=ADMIN_OWNER,
                    value=dict(DEFAULTS),
                    version=1,
                    actor_id="test-admin",
                    updated_at=self.now,
                )
            )

    def ordered(self):
        with self.repo.engine.connect() as connection:
            return self.queue.ordered_candidates(
                connection, "test-pool", purpose="generate", dispatch_backend="legacy"
            )[0]

    def cursor(self):
        with self.repo.engine.connect() as connection:
            return connection.scalar(
                select(scheduler_state.c.last_owner).where(
                    scheduler_state.c.pool == "test-pool"
                )
            )

    def finish(self, claim):
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "cpu-" + claim.lease.attempt_id)
        self.queue.begin_collection(claim.lease)
        return self.queue.complete(
            claim.lease, [VALID_ARTIFACT], actual_cost_microusd=0
        )

    def test_old_batch_cannot_take_every_new_slot(self):
        for i in range(20):
            self.job(key="batch-" + str(i), cost=0)
        self.now += 901
        newcomer = self.job(scope=self.other, cost=0)
        claimed = [self.queue.claim("worker-" + str(i), "test-pool") for i in range(2)]
        self.assertIn(newcomer["id"], [c.job["id"] for c in claimed])

    def test_single_slot_rotates_even_after_fast_completion_and_restart(self):
        scopes = [self.scope, self.other, Scope("test-tenant", "third", "p3")]
        for scope in scopes:
            for i in range(4):
                self.job(scope=scope, cost=0, key="batch-" + str(i))
        served = []
        for _ in range(12):
            self.queue = TaskQueue(self.repo)  # no process-local rotation state
            claim = self.queue.claim("cpu", "test-pool")
            served.append(claim.job["owner_id"])
            self.finish(claim)
        for start in range(0, 12, 3):
            self.assertEqual(
                set(served[start : start + 3]), {s.owner_id for s in scopes}
            )

    def test_read_only_projection_interleaves_heads_and_preserves_owner_fifo(self):
        expected = {}
        for scope in (self.scope, self.other):
            expected[scope.owner_id] = []
            for _ in range(3):
                expected[scope.owner_id].append(self.job(scope=scope, cost=0)["id"])
                self.now += 1
        before = self.cursor()
        ordered = self.ordered()
        self.assertEqual(self.cursor(), before)
        for start in range(0, 6, 2):
            self.assertEqual(
                len({j["owner_id"] for j in ordered[start : start + 2]}), 2
            )
        for owner, identities in expected.items():
            self.assertEqual(
                [j["id"] for j in ordered if j["owner_id"] == owner], identities
            )

    def test_concurrent_claims_balance_owners_and_keep_unique_attempts(self):
        for scope in (self.scope, self.other):
            for _ in range(8):
                self.job(scope=scope, cost=0)
        claims = self.parallel(lambda i: self.queue.claim("cpu-" + str(i), "test-pool"))
        self.assertEqual(
            Counter(c.job["owner_id"] for c in claims), {"superdan": 4, "supervan": 4}
        )
        self.assertEqual(len({c.lease.attempt_id for c in claims}), 8)
        self.assertTrue(all(c.job["attempt_no"] == 1 for c in claims))

    def test_selected_delivery_yields_without_cursor_or_attempt_write(self):
        for scope in (self.scope, self.other):
            for _ in range(2):
                self.job(scope=scope, cost=0)
        ordered = self.ordered()
        self.assertIsNone(
            self.queue.claim("cpu", "test-pool", selected_job_id=ordered[-1]["id"])
        )
        self.assertIsNone(self.cursor())
        self.assertTrue(
            all(
                self.repo.get_job(
                    Scope(j["tenant_id"], j["owner_id"], "project-1"), j["id"]
                )["attempt_no"]
                == 0
                for j in ordered
            )
        )
        self.assertEqual(
            self.queue.claim("cpu", "test-pool", selected_job_id=ordered[0]["id"]).job[
                "id"
            ],
            ordered[0]["id"],
        )

    def test_validator_skips_incompatible_owner_without_stalling_other_work(self):
        for scope in (self.scope, self.other):
            self.job(scope=scope, cost=0)
        first = self.ordered()[0]
        claim = self.queue.claim(
            "cpu", "test-pool", validator=lambda j: j["owner_id"] != first["owner_id"]
        )
        self.assertNotEqual(claim.job["owner_id"], first["owner_id"])

    def test_owner_identity_includes_tenant_and_legacy_positions_stay_fixed(self):
        other_tenant = Scope("another-tenant", self.scope.owner_id, "project-1")
        self.adopt(other_tenant.tenant_id)
        for scope in (self.scope, other_tenant):
            for _ in range(2):
                self.job(scope=scope, cost=0)
        claimed = [self.queue.claim("cpu-" + str(i), "test-pool") for i in range(2)]
        self.assertEqual(
            {c.job["tenant_id"] for c in claimed}, {"test-tenant", "another-tenant"}
        )
        legacy = self.job(scope=Scope("legacy", "old", "p"), cost=0)
        with patch(
            "studio_platform.queue_fairness.fair_generation_order",
            side_effect=lambda c, ordered, p, a, k: ordered,
        ):
            old = [j["id"] for j in self.ordered()]
        current = [j["id"] for j in self.ordered()]
        self.assertEqual(current.index(legacy["id"]), old.index(legacy["id"]))

    def test_unknown_attempt_is_never_replayed_and_its_hold_stays(self):
        self.job()
        running = self.queue.claim("original", "test-pool")
        self.queue.begin_submission(running.lease)
        self.now += 91
        self.queue.recover_expired()
        before = self.repo.get_job(self.scope, running.job["id"])
        budget = self.repo.get_budget("owner-budget")
        self.job(scope=self.other, cost=0)
        claim = self.queue.claim("new-slot", "test-pool")
        self.assertEqual(claim.job["owner_id"], self.other.owner_id)
        self.assertEqual(self.repo.get_job(self.scope, running.job["id"]), before)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(before["status"], "submission_unknown")

    def test_failed_claim_rolls_back_cursor_and_attempt(self):
        job = self.job(cost=0)
        with patch.object(
            self.repo, "_emit", side_effect=RuntimeError("test rollback")
        ):
            with self.assertRaises(RuntimeError):
                self.queue.claim("cpu", "test-pool")
        self.assertIsNone(self.cursor())
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)
        with self.repo.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(attempts)).first())

    def test_recovery_sort_is_unchanged_and_cannot_advance_generate_cursor(self):
        self.job(cost=0)
        claim = self.queue.claim("cpu", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "original")
        self.queue.release(claim.lease, retry_after_s=0)
        cursor = self.cursor()
        with patch(
            "studio_platform.queue_fairness.fair_generation_order",
            side_effect=AssertionError("generation only"),
        ):
            recovered = self.queue.claim("poller", "test-pool", purpose="reconcile")
        self.assertEqual(recovered.lease.attempt_id, claim.lease.attempt_id)
        self.assertEqual(self.cursor(), cursor)
        self.assertFalse(
            metadata({"version": 1, "value": dict(DEFAULTS)})["controls"][
                "per_user_machine_share_policy"
            ]["pending_implementation"]
        )
