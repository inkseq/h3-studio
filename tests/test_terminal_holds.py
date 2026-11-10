"""Terminal jobs and removed hosts leave the admission hold (#915).

Each case reproduces a staging row that stayed `reserved` after its job or
host ended. Unknown actual cost always becomes `invoice_pending`, never a
made-up or zero settlement; an upstream that may still run keeps its hold.
"""

from sqlalchemy import insert, select

from studio_platform.alert_snapshot import read_snapshot
from studio_platform.queue import TaskQueue
from studio_platform.repository import budget_reservations, registered_workers
from studio_platform.settlement_recovery import (
    release_removed_holds,
    release_removed_instance,
)
from test_platform_queue import VALID_ARTIFACT
from test_platform_repository import LedgerCase


class TerminalHoldTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.queue = TaskQueue(self.repo)
        self.repo.configure_pool("test-pool", max_instances=4, max_physical_gpus=4)

    def host(self, key, *workers):
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "test-pool",
            key,
            reserved_cost_microusd=50_000,
            hard_deadline=self.now + 86400,
            budget_account_ids=["owner-budget"],
            dry_run=False,
            provider="vast",
        )
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(
            intent["id"], "starting", provider_instance_id="pid-" + key
        )
        with self.repo.transaction() as conn:
            for worker in workers:
                conn.execute(
                    insert(registered_workers).values(
                        id=worker,
                        pool="test-pool",
                        provider="vast",
                        instance_id="pid-" + key,
                        spec={},
                        spec_hash="a" * 64,
                        state="ready",
                        current_job_id=None,
                        drain_requested=0,
                        fence=1,
                        expires_at=self.now + 900,
                        updated_at=self.now,
                    )
                )
        return intent["id"]

    def remove(self, intent_id):
        self.repo.update_instance(intent_id, "destroying")
        self.repo.update_instance(intent_id, "destroyed", destruction_confirmed=True)

    def submitted(self, job, worker):
        claim = self.queue.claim(worker, "test-pool", job_ids=[job["id"]])
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "upstream-" + claim.lease.attempt_id)
        return claim

    def lose(self, claim, reason="capacity_provider_removed"):
        return self.queue.reclaim_capacity(
            self.scope,
            claim.lease.job_id,
            expected_attempt_id=claim.lease.attempt_id,
            expected_fence=claim.lease.fence,
            reason=reason,
            evidence_id="proof-" + claim.lease.attempt_id,
        )

    def finish(self, job, worker):
        claim = self.submitted(job, worker)
        self.queue.begin_collection(claim.lease)
        return self.queue.complete(
            claim.lease, [VALID_ARTIFACT], actual_cost_microusd=None
        )

    def states(self, kind, ident):
        with self.repo.engine.connect() as conn:
            return {
                (row.state, row.actual_cost_microusd)
                for row in conn.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_type == kind,
                        budget_reservations.c.reference_id == ident,
                    )
                )
            }

    def alarmed(self):
        snapshot = read_snapshot(
            self.repo, now=self.now, tenant_id=self.scope.tenant_id
        )
        return {hold["id"] for hold in snapshot["terminal_reservations"]}

    def test_job_done_after_attempt_lost_on_removed_host_waits_for_invoice(self):
        # Staging 881a2328: the first host vanished, the job then succeeded on
        # another host, and its hold stayed reserved for weeks.
        first = self.host("first", "w-first")
        self.host("second", "w-second")
        job = self.job()
        self.lose(self.submitted(job, "w-first"))
        self.remove(first)
        done = self.finish(job, "w-second")
        self.assertEqual(
            (done["status"], done["result"]["billing_status"]),
            ("succeeded", "pending"),
        )
        self.assertEqual(self.states("job", job["id"]), {("invoice_pending", None)})
        self.assertNotIn(job["id"], self.alarmed())
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 0)

    def test_lost_attempt_on_live_host_keeps_hold_until_host_is_removed(self):
        first = self.host("first", "w-first")
        self.host("second", "w-second")
        job = self.job()
        self.lose(self.submitted(job, "w-first"), reason="capacity_owner_recovery")
        self.finish(job, "w-second")
        # The lost upstream may still run on a live host: keep the hold.
        self.assertEqual(self.states("job", job["id"]), {("reserved", None)})
        self.assertIn(job["id"], self.alarmed())
        self.remove(first)
        release_removed_holds(self.repo, first, [job["id"]])
        self.assertEqual(self.states("job", job["id"]), {("invoice_pending", None)})
        self.assertEqual(self.states("instance", first), {("invoice_pending", None)})
        self.assertNotIn(job["id"], self.alarmed())
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 0)

    def test_attempt_lost_while_collecting_has_already_stopped_upstream(self):
        # Staging 27b6213f: the run had finished and only collection was lost.
        self.host("first", "w-first")
        self.host("second", "w-second")
        job = self.job()
        claim = self.submitted(job, "w-first")
        self.queue.begin_collection(claim.lease)
        self.lose(claim, reason="capacity_collection_failed")
        self.finish(job, "w-second")
        self.assertEqual(self.states("job", job["id"]), {("invoice_pending", None)})

    def test_exhausted_reclaims_on_removed_hosts_fail_into_invoice_pending(self):
        removed = self.host("gone", "w-1", "w-2", "w-3")
        self.remove(removed)
        job = self.job()
        for worker in ("w-1", "w-2", "w-3"):
            receipt = self.lose(self.submitted(job, worker))
        self.assertEqual(receipt["state"], "exhausted")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "failed")
        self.assertEqual(self.states("job", job["id"]), {("invoice_pending", None)})

    def test_exhausted_reclaims_on_unknown_hosts_keep_hold(self):
        job = self.job()
        for worker in ("w-1", "w-2", "w-3"):
            self.lose(self.submitted(job, worker))
        self.assertEqual(self.states("job", job["id"]), {("reserved", None)})

    def test_cancel_after_lost_attempt_on_removed_host_waits_for_invoice(self):
        first = self.host("first", "w-first")
        job = self.job()
        self.lose(self.submitted(job, "w-first"))
        self.remove(first)
        cancelled = self.repo.request_cancel(self.scope, job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.states("job", job["id"]), {("invoice_pending", None)})

    def test_host_removed_before_its_job_closed_is_released_on_retry(self):
        # Staging 1446b3e2: the host was removed while its attempt was still
        # open; the job closed later and nothing retried the host's hold.
        host = self.host("only", "w-only")
        job = self.job()
        claim = self.submitted(job, "w-only")
        self.remove(host)
        with self.repo.transaction() as conn:
            release_removed_instance(self.repo, conn, host)
        self.assertEqual(self.states("instance", host), {("reserved", None)})
        self.repo.request_cancel(self.scope, job["id"])
        self.queue.confirm_cancel(claim.lease, upstream_stopped=True)
        self.assertEqual(self.states("job", job["id"]), {("invoice_pending", None)})
        self.assertEqual(self.states("instance", host), {("reserved", None)})
        release_removed_holds(self.repo, host, [job["id"]])
        self.assertEqual(self.states("instance", host), {("invoice_pending", None)})
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(
            (budget["reserved_microusd"], budget["spent_microusd"]), (0, 0)
        )
        # Level-triggered retries are idempotent.
        release_removed_holds(self.repo, host, [job["id"]])
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)

    def test_retry_never_releases_an_open_attempt_or_a_live_host(self):
        host = self.host("live", "w-live")
        job = self.job()
        self.submitted(job, "w-live")
        before = self.repo.get_budget("owner-budget")
        release_removed_holds(self.repo, host, [job["id"]])
        self.remove(host)
        release_removed_holds(self.repo, host, [job["id"]])
        self.assertEqual(self.states("job", job["id"]), {("reserved", None)})
        self.assertEqual(self.states("instance", host), {("reserved", None)})
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
