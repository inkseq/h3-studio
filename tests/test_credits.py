"""Customer-credit invariants on disposable SQLite or isolated explicit test PG."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from sqlalchemy import Column, MetaData, String, Table, func, insert, select, update

from studio_platform import credits
from studio_platform.credit_schema import metadata, accounts, entries
from studio_platform.identity_schema import users, admin_audit
from studio_platform.queue import TaskQueue
from studio_platform.repository import attempts, documents, jobs, plans, request_hash
from studio_platform.spending_limits import lock_owner
from test_platform_repository import LedgerCase


class CreditTests(LedgerCase):
    def setUp(self):
        super().setUp()
        users.create(self.repo.engine)
        admin_audit.create(self.repo.engine)
        metadata.create_all(self.repo.engine)
        self.version = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        self.version.create(self.repo.engine)
        self.user_id, self.other_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self.repo.transaction() as connection:
            connection.execute(insert(self.version).values(version_num="20261019_0004"))
            for ident, owner in (
                (self.user_id, "superdan"),
                (self.other_id, "supervan"),
            ):
                connection.execute(
                    insert(users).values(
                        id=ident,
                        tenant=self.scope.tenant_id,
                        owner=owner,
                        role="user",
                        status="active",
                        display_name=owner,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )

    def policy(self, enabled=True, **extra):
        condition = (
            documents.c.tenant_id == self.scope.tenant_id,
            documents.c.owner_id == credits.POLICY_OWNER,
            documents.c.project_id == credits.POLICY_PROJECT,
            documents.c.kind == credits.POLICY_KIND,
            documents.c.document_id == credits.POLICY_ID,
        )
        payload = {"enabled": enabled, "active_revision": "price-1", **extra}
        with self.repo.transaction() as connection:
            exists = connection.execute(
                select(documents.c.version).where(*condition)
            ).first()
            if exists:
                connection.execute(
                    update(documents).where(*condition).values(payload=payload)
                )
            else:
                connection.execute(
                    insert(documents).values(
                        tenant_id=self.scope.tenant_id,
                        owner_id=credits.POLICY_OWNER,
                        project_id=credits.POLICY_PROJECT,
                        kind=credits.POLICY_KIND,
                        document_id=credits.POLICY_ID,
                        version=1,
                        payload=payload,
                        updated_at=self.now,
                    )
                )

    def seed(self, amount=100, *, owner="superdan", key="initial-grant"):
        with self.repo.transaction() as connection:
            return credits.grant(
                connection,
                self.scope.tenant_id,
                owner,
                amount,
                actor="browser:admin",
                reason="Staging test grant",
                idempotency_key=key,
                now=self.now,
            )

    def credit_job(self, amount=20, *, actor="key:original-agent"):
        # Preserve a real pre-credit accepted job, then attach the synthetic
        # frozen quote using fixture-only SQL. Pure core tests need to inspect
        # holds before/after explicitly calling them; real new-admission tests
        # use enabled policy and the original atomic quote/hold hooks instead.
        self.policy(False)
        execution = {
            "pool": "test-pool",
            "expected_runtime_s": 120,
            "model_id": "test-model-only",
        }
        plan = self.repo.create_plan(
            self.scope,
            {"prompt": "Synthetic core test"},
            execution,
            expires_at=self.now + 1000,
            estimated_cost_microusd=100_000,
        )
        job = self.repo.create_job(
            self.scope,
            plan["id"],
            uuid.uuid4().hex,
            budget_account_ids=("owner-budget",),
            spending_actor=actor,
        )
        execution = {
            **execution,
            "credit_quote": {
                "version": 1,
                "enforced": True,
                "user_id": self.user_id,
                "credits": amount,
                "price_revision": "price-1",
                "tier": "standard",
            },
        }
        digest = request_hash(
            {
                "request_hash": plan["request_hash"],
                "plan_hash": request_hash(execution),
                "estimated_cost_microusd": plan["estimated_cost_microusd"],
            }
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(plans)
                .where(plans.c.id == plan["id"])
                .values(execution_plan=execution, plan_hash=request_hash(execution))
            )
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(execution_plan=execution, request_hash=digest)
            )
        self.policy(True)
        return {**job, "execution_plan": execution, "request_hash": digest}

    def hold(self, job, *, actor="key:original-agent"):
        with self.repo.transaction() as connection:
            # The original admission already owns this lock before calling core.
            lock_owner(connection, self.scope.tenant_id, self.scope.owner_id)
            return credits.hold(connection, self.repo, self.scope, job, actor)

    def balance(self):
        with self.repo.engine.connect() as connection:
            return credits.account(
                connection, self.scope.tenant_id, self.scope.owner_id, now=self.now
            )

    def assert_invariants(self):
        with self.repo.engine.connect() as connection:
            for row in connection.execute(select(accounts)).mappings():
                actual = connection.execute(
                    select(
                        func.coalesce(func.sum(entries.c.amount), 0),
                        func.coalesce(func.sum(entries.c.held_delta), 0),
                    ).where(
                        entries.c.tenant == row["tenant"],
                        entries.c.user_id == row["user_id"],
                    )
                ).one()
                self.assertEqual(actual, (row["balance"], row["held"]))
                self.assertGreaterEqual(row["balance"], row["held"])
                self.assertGreaterEqual(row["held"], 0)

    def complete(self, job, *, before_commit=None):
        queue = TaskQueue(self.repo)
        claim = queue.claim("credit-core-test", job["pool"], job_ids=[job["id"]])
        self.assertIsNotNone(claim)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "synthetic-" + job["id"])
        queue.begin_collection(claim.lease)
        if before_commit is not None:
            before_commit()
        result = queue.complete(
            claim.lease,
            [
                {
                    "kind": "video",
                    "object_key": "synthetic/" + job["id"] + ".mp4",
                    "size_bytes": 1,
                    "sha256": "a" * 64,
                    "validated": True,
                }
            ],
            actual_cost_microusd=0,
        )
        with self.repo.transaction() as connection:
            credits.capture(connection, self.repo, result)
        return result

    def test_grant_adjust_and_exact_replay_preserve_signed_ledger(self):
        first = self.seed(50)
        self.assertTrue(first["created"])
        self.assertFalse(self.seed(50)["created"])
        with self.repo.transaction() as connection:
            changed = credits.adjust(
                connection,
                self.scope.tenant_id,
                "superdan",
                -10,
                actor="browser:admin",
                reason="Audited correction",
                idempotency_key="correction",
                now=self.now,
            )
        self.assertEqual(changed["account"]["balance"], 40)
        with self.assertRaisesRegex(credits.CreditError, "credit_idempotency_conflict"):
            self.seed(51)
        self.assertEqual(self.balance()["balance"], 40)
        self.assert_invariants()

    def test_hold_capture_debits_quote_once_and_retains_supplier_truth(self):
        self.seed()
        job = self.credit_job()
        first = self.hold(job)
        self.assertTrue(first["created"])
        self.assertFalse(self.hold(job)["created"])
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 20))
        result = self.complete(job)
        before = self.repo.get_budget("owner-budget")
        with self.repo.transaction() as connection:
            replay = credits.capture(
                connection, self.repo, result, reason="Changed diagnostic only"
            )
        self.assertFalse(replay["created"])
        self.assertEqual(self.balance()["balance"], 80)
        self.assertEqual(self.balance()["held"], 0)
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assert_invariants()

    def test_capacity_reclaim_carries_one_hold_and_delivered_attempt_captures_once(
        self,
    ):
        from studio_platform.repository import LeaseLost
        from test_platform_queue import VALID_ARTIFACT

        self.seed()
        job = self.credit_job()
        self.hold(job)
        queue = TaskQueue(self.repo)
        first = queue.claim("lost-worker", job["pool"], job_ids=[job["id"]])
        queue.begin_submission(first.lease)
        queue.record_submitted(first.lease, "original-upstream")
        before = self.repo.get_budget("owner-budget")
        queue.reclaim_capacity(
            self.scope,
            job["id"],
            expected_attempt_id=first.lease.attempt_id,
            expected_fence=first.lease.fence,
            reason="capacity_owner_recovery",
            evidence_id="original-absence",
        )
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 20))
        second = queue.claim("replacement-worker", job["pool"], job_ids=[job["id"]])
        queue.begin_submission(second.lease)
        queue.record_submitted(second.lease, "replacement-upstream")
        queue.begin_collection(second.lease)
        done = queue.complete(second.lease, [VALID_ARTIFACT], actual_cost_microusd=17)
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (80, 0))
        self.assertEqual(done["result"]["capacity_reclaim_count"], 1)
        self.assertEqual(done["result"]["billing_status"], "pending")
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        with self.assertLogs("studio_platform.capacity_reclaim", "WARNING"):
            with self.assertRaises(LeaseLost):
                queue.complete(first.lease, [VALID_ARTIFACT], actual_cost_microusd=9)
        with self.repo.transaction() as conn:
            self.assertFalse(credits.capture(conn, self.repo, done)["created"])
            ledger = (
                conn.execute(
                    select(entries.c.kind).where(entries.c.reference_id == job["id"])
                )
                .scalars()
                .all()
            )
            old = (
                conn.execute(
                    select(attempts).where(attempts.c.id == first.lease.attempt_id)
                )
                .mappings()
                .one()
            )
        self.assertCountEqual(ledger, ["hold", "capture"])
        self.assertIsNone(old["actual_cost_microusd"])
        self.assertEqual(old["upstream_stopped"], 0)
        self.assert_invariants()

    def test_capacity_reclaim_exhaustion_releases_user_hold_without_supplier_settlement(
        self,
    ):
        self.seed()
        job = self.credit_job()
        self.hold(job)
        queue = TaskQueue(self.repo)
        before = self.repo.get_budget("owner-budget")
        for index in range(3):
            claim = queue.claim(
                "worker-" + str(index), job["pool"], job_ids=[job["id"]]
            )
            queue.begin_submission(claim.lease)
            queue.record_submitted(claim.lease, "upstream-" + str(index))
            receipt = queue.reclaim_capacity(
                self.scope,
                job["id"],
                expected_attempt_id=claim.lease.attempt_id,
                expected_fence=claim.lease.fence,
                reason="capacity_owner_recovery",
                evidence_id="lost-" + str(index),
            )
            self.assertEqual(self.balance()["held"], 20 if index < 2 else 0)
        self.assertEqual(receipt["state"], "exhausted")
        self.assertEqual(self.balance()["balance"], 100)
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        with self.repo.engine.connect() as conn:
            self.assertCountEqual(
                conn.execute(
                    select(entries.c.kind).where(entries.c.reference_id == job["id"])
                )
                .scalars()
                .all(),
                ["hold", "release"],
            )
        self.assert_invariants()

    def test_cancel_reclaimed_queued_or_claimed_work_preserves_supplier_liability(self):
        from studio_platform.repository import LeaseLost

        self.seed()
        for replacement_claimed in (False, True):
            with self.subTest(replacement_claimed=replacement_claimed):
                job = self.credit_job()
                self.hold(job)
                queue = TaskQueue(self.repo)
                old = queue.claim("old-worker", job["pool"], job_ids=[job["id"]])
                queue.begin_submission(old.lease)
                queue.record_submitted(old.lease, "unconfirmed-original")
                queue.reclaim_capacity(
                    self.scope,
                    job["id"],
                    expected_attempt_id=old.lease.attempt_id,
                    expected_fence=old.lease.fence,
                    reason="capacity_owner_recovery",
                    evidence_id="owner-recovery",
                )
                before = self.repo.get_budget("owner-budget")
                fresh = (
                    queue.claim("replacement", job["pool"], job_ids=[job["id"]])
                    if replacement_claimed
                    else None
                )
                done = self.repo.request_cancel(self.scope, job["id"])
                self.assertEqual(done["status"], "cancelled")
                self.assertEqual(done["result"]["billing_status"], "pending")
                self.assertEqual(done["result"]["capacity_reclaim_count"], 1)
                self.assertEqual(
                    (self.balance()["balance"], self.balance()["held"]), (100, 0)
                )
                self.assertEqual(self.repo.get_budget("owner-budget"), before)
                self.assertEqual(self.repo.request_cancel(self.scope, job["id"]), done)
                if fresh:
                    with self.assertRaises(LeaseLost):
                        queue.begin_submission(fresh.lease)
                self.assertIsNone(queue.claim("late", job["pool"], job_ids=[job["id"]]))
                with self.repo.engine.connect() as conn:
                    prior = (
                        conn.execute(
                            select(attempts).where(
                                attempts.c.id == old.lease.attempt_id
                            )
                        )
                        .mappings()
                        .one()
                    )
                    self.assertEqual(
                        (
                            prior["status"],
                            prior["upstream_stopped"],
                            prior["actual_cost_microusd"],
                        ),
                        ("attempt_lost", 0, None),
                    )
                    self.assertCountEqual(
                        conn.execute(
                            select(entries.c.kind).where(
                                entries.c.reference_id == job["id"]
                            )
                        )
                        .scalars()
                        .all(),
                        ["hold", "release"],
                    )
                self.assert_invariants()

    def test_failure_and_proven_queued_cancel_release_full_hold(self):
        self.seed()
        for state in ("failed", "cancelled"):
            job = self.credit_job()
            self.hold(job)
            if state == "failed":
                queue = TaskQueue(self.repo)
                claim = queue.claim(
                    "credit-core-fail", job["pool"], job_ids=[job["id"]]
                )
                result = queue.fail(
                    claim.lease, "synthetic_failure", actual_cost_microusd=0
                )
            else:
                result = self.repo.request_cancel(self.scope, job["id"])
            with self.repo.transaction() as connection:
                credits.release(connection, self.repo, result)
                self.assertFalse(
                    credits.release(connection, self.repo, result)["created"]
                )
            self.assertEqual(
                (self.balance()["balance"], self.balance()["held"]), (100, 0)
            )
        self.assert_invariants()

    def test_unknown_attempt_never_releases_even_with_manually_terminal_status(self):
        self.seed()
        job = self.credit_job()
        self.hold(job)
        queue = TaskQueue(self.repo)
        claim = queue.claim("credit-core-unknown", job["pool"], job_ids=[job["id"]])
        queue.begin_submission(claim.lease)
        queue.mark_submission_unknown(claim.lease)
        with self.assertRaisesRegex(credits.CreditError, "credit_outcome_unconfirmed"):
            with self.repo.transaction() as connection:
                credits.release(connection, self.repo, job)
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
        with self.assertRaisesRegex(credits.CreditError, "credit_outcome_unconfirmed"):
            with self.repo.transaction() as connection:
                credits.release(connection, self.repo, job)
        self.assertEqual(self.balance()["held"], 20)
        self.assert_invariants()

    def test_insufficient_credits_and_negative_adjustment_cannot_spend_held_money(self):
        self.seed(25)
        first, second = self.credit_job(), self.credit_job()
        self.hold(first)
        with self.assertRaises(credits.CreditError) as result:
            self.hold(second)
        self.assertEqual(
            (result.exception.status, result.exception.code, result.exception.data),
            (402, "insufficient_credits", {"need": 20, "have": 5}),
        )
        with self.assertRaisesRegex(credits.CreditError, "credit_balance_conflict"):
            with self.repo.transaction() as connection:
                credits.adjust(
                    connection,
                    self.scope.tenant_id,
                    "superdan",
                    -6,
                    actor="browser:admin",
                    reason="Cannot remove reserved funds",
                    idempotency_key="invalid-adjust",
                    now=self.now,
                )
        self.assertEqual(self.balance()["available"], 5)
        self.assert_invariants()

    def test_concurrent_different_jobs_do_not_overdraw(self):
        self.seed(30)
        choices = [self.credit_job(), self.credit_job()]

        def admit(job):
            try:
                return self.hold(job)["created"]
            except credits.CreditError as error:
                self.assertEqual(error.code, "insufficient_credits")
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(admit, choices)), 1)
        self.assertEqual(self.balance()["held"], 20)
        self.assert_invariants()

    def test_concurrent_first_grants_and_same_hold_retry_have_one_effect(self):
        def add(index):
            return self.seed(1, key="parallel-" + str(index))

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(row["created"] for row in pool.map(add, range(8))))
        job = self.credit_job(amount=5)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.hold(job), range(8)))
        self.assertEqual(sum(row["created"] for row in results), 1)
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (8, 5))
        self.assert_invariants()

    def test_terminal_operations_continue_after_suspension_and_policy_disable(self):
        self.seed()
        job = self.credit_job()
        self.hold(job)

        def suspend():
            with self.repo.transaction() as connection:
                connection.execute(
                    update(users)
                    .where(users.c.id == self.user_id)
                    .values(status="suspended")
                )
            self.policy(False)

        result = self.complete(job, before_commit=suspend)
        with self.repo.transaction() as connection:
            replay = credits.capture(connection, self.repo, result)
        self.assertFalse(replay["created"])
        self.assertEqual(self.balance()["balance"], 80)
        self.assert_invariants()

    def test_legacy_jobs_and_no_actual_hold_are_not_charged_retroactively(self):
        self.seed()
        quoted_but_never_held = self.credit_job()
        completed = self.complete(quoted_but_never_held)
        with self.repo.transaction() as connection:
            self.assertIsNone(credits.capture(connection, self.repo, completed))
        self.assertEqual(self.balance()["balance"], 100)
        self.assert_invariants()

    def test_mutually_exclusive_terminal_key_rejects_later_release(self):
        self.seed()
        job = self.credit_job()
        self.hold(job)
        result = self.complete(job)
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
        with self.assertRaisesRegex(credits.CreditError, "credit_idempotency_conflict"):
            with self.repo.transaction() as connection:
                credits.release(connection, self.repo, result)
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (80, 0))
        self.assert_invariants()

    def test_refund_ceiling_includes_prior_refund_and_waive_and_never_uses_usd(self):
        self.seed()
        job = self.credit_job()
        self.hold(job)
        result = self.complete(job)
        with self.repo.transaction() as connection:
            credits.grant(
                connection,
                self.scope.tenant_id,
                "superdan",
                7,
                actor="browser:admin",
                reason="Partial reviewed refund",
                kind="refund",
                reference_type="job",
                reference_id=job["id"],
                idempotency_key="partial-refund",
                now=self.now,
            )
            refunded = credits.refund_job(
                connection,
                self.repo,
                result,
                actor="browser:admin",
                reason="Prelaunch customer waiver",
                idempotency_key="job:" + job["id"] + ":waive",
            )
            self.assertEqual(refunded["entry"]["amount"], 13)
            self.assertFalse(
                credits.refund_job(
                    connection,
                    self.repo,
                    result,
                    actor="browser:admin",
                    reason="Prelaunch customer waiver",
                    idempotency_key="job:" + job["id"] + ":waive",
                )["created"]
            )
            self.assertIsNone(
                credits.refund_job(
                    connection,
                    self.repo,
                    result,
                    actor="browser:admin",
                    reason="Another waiver cannot add money",
                    idempotency_key="different-waiver",
                )
            )
        self.assertEqual(self.balance()["balance"], 100)
        self.assert_invariants()

    def test_daily_projection_preserves_actor_and_old_unknown_holds(self):
        self.seed()
        first, second = (
            self.credit_job(actor="key:one"),
            self.credit_job(actor="key:two"),
        )
        self.hold(first, actor="key:one")
        self.hold(second, actor="key:two")
        self.complete(first)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                credits.daily_projection(
                    connection, self.scope.tenant_id, "superdan", self.now
                ),
                40,
            )
            self.assertEqual(
                credits.daily_projection(
                    connection, self.scope.tenant_id, "superdan", self.now, "key:one"
                ),
                20,
            )
            self.assertEqual(
                credits.daily_projection(
                    connection, self.scope.tenant_id, "superdan", self.now + 86400
                ),
                20,
            )
        self.assertEqual(self.balance()["today_used"], 20)

    def test_postgres_numeric_sums_are_normalized_before_json_or_refund_writes(self):
        self.seed()
        job = self.credit_job()
        self.hold(job)
        result = self.complete(job)
        aggregate_calls = []
        with self.repo.transaction() as connection:
            original_execute = connection.execute

            def postgres_sum(statement, *args, **kwargs):
                response = original_execute(statement, *args, **kwargs)
                if 'sum(' in str(statement).lower():
                    # Match psycopg's numeric result for SUM(BIGINT), while
                    # keeping actual account/entry queries and writes intact.
                    value = Decimal(response.scalar_one())
                    aggregate_calls.append(value)
                    return SimpleNamespace(scalar_one=lambda: value)
                return response

            with patch.object(connection, 'execute', side_effect=postgres_sum):
                balance = credits.account(
                    connection, self.scope.tenant_id, 'superdan', now=self.now
                )
                projected = credits.daily_projection(
                    connection, self.scope.tenant_id, 'superdan', self.now
                )
                self.assertIs(type(balance['today_used']), int)
                self.assertIs(type(projected), int)
                self.assertEqual((balance['today_used'], projected), (20, 20))
                json.dumps({'account': balance, 'projected': projected})
                waived = credits.refund_job(
                    connection,
                    self.repo,
                    result,
                    actor='browser:admin',
                    reason='Reviewed waiver',
                    idempotency_key='decimal-refund',
                )
                self.assertIs(type(waived['entry']['amount']), int)
                self.assertEqual(waived['entry']['amount'], 20)
        self.assertEqual(len(aggregate_calls), 4)
        self.assertEqual(self.balance()['balance'], 100)
        self.assert_invariants()

    def test_database_aggregate_conversion_rejects_fractional_unknown_or_overflow_values(
        self,
    ):
        for value in (
            Decimal('0'),
            Decimal('20.000'),
            Decimal(credits.MAX_CREDITS),
            0,
            credits.MAX_CREDITS,
        ):
            with self.subTest(value=value):
                normalized = credits._aggregate_integer(value)
                self.assertIs(type(normalized), int)
                self.assertEqual(normalized, value)
        for value in (
            True,
            None,
            20.0,
            '20',
            -1,
            Decimal('-1'),
            Decimal('1.5'),
            Decimal('NaN'),
            Decimal('Infinity'),
            Decimal('1E99999'),
            credits.MAX_CREDITS + 1,
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(credits.CreditError, 'credit_aggregate_invalid'),
            ):
                credits._aggregate_integer(value)

    def test_history_is_bounded_iso_and_cursor_is_owner_bound(self):
        for index in range(5):
            self.seed(1, key="history-" + str(index))
        private_url = "https://pay.stripe.com/receipts/private-contact-details"
        with self.repo.transaction() as connection:
            connection.execute(update(entries).values(receipt_url=private_url))
        with self.repo.engine.connect() as connection:
            first = credits.entries(
                connection, self.scope.tenant_id, "superdan", limit=2
            )
            second = credits.entries(
                connection,
                self.scope.tenant_id,
                "superdan",
                limit=2,
                cursor=first["next_cursor"],
            )
            self.assertFalse(
                {row["id"] for row in first["entries"]}
                & {row["id"] for row in second["entries"]}
            )
            self.assertTrue(
                all(row["created_at"].endswith("+00:00") for row in first["entries"])
            )
            self.assertNotIn("fingerprint", json.dumps(first))
            self.assertNotIn("receipt_url", json.dumps(first))
            self.assertEqual(set(connection.scalars(select(entries.c.receipt_url))), {private_url})
            with self.assertRaisesRegex(credits.CreditError, "credit_cursor_invalid"):
                credits.entries(
                    connection,
                    self.scope.tenant_id,
                    "supervan",
                    cursor=first["next_cursor"],
                )
            with self.assertRaises(credits.CreditError):
                credits.entries(connection, self.scope.tenant_id, "superdan", limit=101)

    def test_disabled_policy_preserves_legacy_and_malformed_enabled_fails_closed(self):
        with self.repo.engine.connect() as connection:
            self.assertFalse(
                credits.credit_policy(connection, self.scope.tenant_id)["enabled"]
            )
        self.policy(True, unknown="not a second policy")
        with self.repo.engine.connect() as connection:
            with self.assertRaisesRegex(credits.CreditError, "credit_policy_invalid"):
                credits.credit_policy(connection, self.scope.tenant_id)

    def test_new_admission_requires_real_frozen_quote_and_integer_credit_amount(self):
        self.seed()
        job = self.credit_job()
        for frozen in (
            {},
            {**job["execution_plan"]["credit_quote"], "enforced": False},
            {**job["execution_plan"]["credit_quote"], "credits": True},
        ):
            changed = {
                **job,
                "execution_plan": {**job["execution_plan"], "credit_quote": frozen},
            }
            with self.assertRaises(credits.CreditError):
                self.hold(changed)
        self.assertEqual(self.balance()["held"], 0)
        self.assert_invariants()

    def test_original_actor_cannot_be_replaced_and_changed_quote_conflicts(self):
        self.seed()
        job = self.credit_job(actor="key:original")
        first = self.hold(job, actor="key:replacement")
        self.assertEqual(first["entry"]["origin"], "key:original")
        self.assertFalse(self.hold(job, actor="key:yet-another")["created"])
        changed = {
            **job,
            "execution_plan": {
                **job["execution_plan"],
                "credit_quote": {
                    **job["execution_plan"]["credit_quote"],
                    "credits": 21,
                },
            },
        }
        with self.assertRaisesRegex(credits.CreditError, "credit_idempotency_conflict"):
            self.hold(changed)
        self.assertEqual(self.balance()["held"], 20)
        self.assert_invariants()

    def test_status_alone_without_validated_artifact_cannot_capture_or_refund(self):
        self.seed()
        job = self.credit_job()
        self.hold(job)
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="succeeded")
            )
        with self.assertRaisesRegex(credits.CreditError, "credit_outcome_unconfirmed"):
            with self.repo.transaction() as connection:
                credits.capture(connection, self.repo, job)
        with self.repo.transaction() as connection:
            self.assertIsNone(
                credits.refund_job(
                    connection,
                    self.repo,
                    job,
                    actor="browser:admin",
                    reason="No captured charge",
                    idempotency_key="unearned-refund",
                )
            )
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 20))
        self.assert_invariants()

    def test_account_and_job_ownership_are_not_selected_by_idempotency_key(self):
        self.seed(50)
        self.seed(12, owner="supervan")
        job = self.credit_job()
        with self.assertRaisesRegex(credits.CreditError, "credit_job_not_found"):
            with self.repo.transaction() as connection:
                credits.hold(connection, self.repo, self.other, job)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                credits.account(
                    connection, self.scope.tenant_id, "supervan", now=self.now
                )["balance"],
                12,
            )
            with self.assertRaisesRegex(
                credits.CreditError, "credit_account_not_found"
            ):
                credits.account(connection, "foreign-tenant", "superdan", now=self.now)
        self.assertEqual(self.balance()["balance"], 50)
        self.assert_invariants()
