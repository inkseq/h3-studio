"""Original admission/worker hooks keep credits and supplier money atomic."""

import unittest
import uuid

from sqlalchemy import Column, MetaData, String, Table, func, insert, select, update

from studio_platform.auth import Auth, accounts as password_accounts
from studio_platform import credits
from studio_platform.credit_schema import entries, metadata as credit_metadata
from studio_platform.identity import lock_account_gate
from studio_platform.identity_schema import admin_audit, users
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    attempts,
    budget_reservations,
    jobs,
    spending_origins,
)
import test_credits as fixtures
from test_platform_repository import LedgerCase


VALID_ARTIFACT = {
    "kind": "video",
    "object_key": "synthetic/credit-admission.mp4",
    "size_bytes": 1,
    "sha256": "a" * 64,
    "validated": True,
}


class CreditAdmissionTests(LedgerCase):
    policy = fixtures.CreditTests.policy
    seed = fixtures.CreditTests.seed
    balance = fixtures.CreditTests.balance
    assert_invariants = fixtures.CreditTests.assert_invariants

    def setUp(self):
        super().setUp()
        self.user_id, self.other_id = str(uuid.uuid4()), str(uuid.uuid4())
        marker = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        with self.repo.transaction() as connection:
            users.create(connection)
            admin_audit.create(connection)
            credit_metadata.create_all(connection)
            marker.create(connection)
            connection.execute(insert(marker).values(version_num="20261019_0004"))
            for ident, owner in (
                (self.user_id, self.scope.owner_id),
                (self.other_id, self.other.owner_id),
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

    def plan(self, *, quoted=True, amount=20):
        execution = {
            "pool": "test-pool",
            "expected_runtime_s": 120,
            "model_id": "test-model-only",
        }
        if quoted:
            execution["credit_quote"] = {
                "version": 1,
                "enforced": True,
                "user_id": self.user_id,
                "credits": amount,
                "price_revision": "price-1",
                "tier": "standard",
            }
        return self.repo.create_plan(
            self.scope,
            {"prompt": "Synthetic original admission"},
            execution,
            expires_at=self.now + 1000,
            estimated_cost_microusd=100_000,
        )

    def admit(self, plan=None, *, key=None, status="queued", actor="key:original"):
        return self.repo.create_job(
            self.scope,
            (plan or self.plan())["id"],
            key or uuid.uuid4().hex,
            initial_status=status,
            budget_account_ids=("owner-budget",),
            spending_actor=actor,
        )

    def entry_counts(self):
        with self.repo.engine.connect() as connection:
            return dict(
                connection.execute(
                    select(entries.c.kind, func.count()).group_by(entries.c.kind)
                ).all()
            )

    def assert_no_supplier_admission(self):
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(budget_reservations)).all(), [])
            self.assertEqual(connection.execute(select(spending_origins)).all(), [])
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)

    def suspend(self, status="suspended"):
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            connection.execute(
                update(users)
                .where(
                    users.c.id == self.user_id, users.c.tenant == self.scope.tenant_id
                )
                .values(status=status)
            )

    def running(self, job, *, lease_seconds=90):
        queue = TaskQueue(self.repo)
        claim = queue.claim(
            "credit-admission-worker",
            job["pool"],
            job_ids=[job["id"]],
            lease_seconds=lease_seconds,
        )
        self.assertIsNotNone(claim)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "synthetic-" + job["id"])
        return queue, claim

    def test_real_creation_replay_freezes_one_original_credit_and_supplier_hold(self):
        self.seed(100)
        self.policy()
        plan = self.plan()
        original = self.admit(plan, key="one-real-admission")
        replay = self.admit(plan, key="one-real-admission")
        self.assertTrue(original["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(replay["id"], original["id"])
        self.assertEqual(self.balance()["held"], 20)
        self.assertEqual(self.entry_counts(), {"grant": 1, "hold": 1})
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )
        with self.repo.engine.connect() as connection:
            origin = connection.execute(select(spending_origins)).mappings().one()
            self.assertEqual(
                (origin["reference_id"], origin["actor_id"]),
                (original["id"], "key:original"),
            )
        self.assert_invariants()

    def test_402_rolls_back_creation_and_enqueue_supplier_reservations_and_origins(
        self,
    ):
        self.seed(10)
        self.policy()
        plan = self.plan()
        with self.assertRaises(credits.CreditError) as refused:
            self.admit(plan)
        self.assertEqual(
            (refused.exception.status, refused.exception.code),
            (402, "insufficient_credits"),
        )
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(jobs)).all(), [])
        self.assert_no_supplier_admission()
        original = self.admit(plan, status="planned")
        with self.assertRaises(credits.CreditError) as refused:
            self.repo.enqueue(
                self.scope,
                original["id"],
                budget_account_ids=("owner-budget",),
                spending_actor="key:original",
            )
        self.assertEqual(refused.exception.status, 402)
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "planned"
        )
        self.assert_no_supplier_admission()
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (10, 0))
        self.assertEqual(self.entry_counts(), {"grant": 1})
        self.assert_invariants()

    def test_planned_enqueue_checks_canonical_suspension_before_freezing_once(self):
        self.seed(100)
        self.policy()
        plan = self.plan()
        original = self.admit(plan, key="planned-admission", status="planned")
        self.suspend()
        with self.assertRaisesRegex(Conflict, "account_suspended"):
            self.repo.enqueue(
                self.scope,
                original["id"],
                budget_account_ids=("owner-budget",),
            )
        self.assert_no_supplier_admission()
        self.assertEqual(self.balance()["held"], 0)
        self.suspend("active")
        queued = self.repo.enqueue(
            self.scope,
            original["id"],
            budget_account_ids=("owner-budget",),
            spending_actor="key:original",
        )
        self.assertEqual(queued["status"], "queued")
        replay = self.admit(plan, key="planned-admission", status="planned")
        self.assertFalse(replay["created"])
        self.assertEqual(self.entry_counts(), {"grant": 1, "hold": 1})
        self.assertEqual(self.balance()["held"], 20)
        self.assert_invariants()

    def test_activation_refuses_old_inert_plan_but_never_charges_old_accepted_job(self):
        self.seed(100)
        legacy_plan = self.plan(quoted=False)
        original = self.admit(legacy_plan)
        inert = self.admit(legacy_plan, status="planned")
        self.policy()
        before = self.repo.get_budget("owner-budget")
        with self.assertRaisesRegex(credits.CreditError, "credit_quote_required"):
            self.repo.enqueue(
                self.scope, inert["id"], budget_account_ids=("owner-budget",)
            )
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assertEqual(
            self.repo.get_job(self.scope, inert["id"])["status"], "planned"
        )
        queue, claim = self.running(original)
        queue.begin_collection(claim.lease)
        result = queue.complete(claim.lease, [VALID_ARTIFACT], actual_cost_microusd=0)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 0))
        self.assertEqual(self.entry_counts(), {"grant": 1})
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(spending_origins.c.reference_id))
                .scalars()
                .all(),
                [original["id"]],
            )
        self.assert_invariants()

    def test_workos_suspension_does_not_block_validated_capture_with_pending_usd(self):
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.id == self.user_id)
                .values(workos_user_id="user_credit_admission")
            )
        auth = Auth(
            self.repo.engine, tenant=self.scope.tenant_id, clock=lambda: self.now
        )
        token = auth.issue_workos_session(self.scope.owner_id, "user_credit_admission")
        principal = auth.session(token)
        self.assertEqual(principal.auth_mode, "workos")
        self.seed(100)
        self.policy()
        original = self.admit(actor=principal.actor_id)
        queue, claim = self.running(original)
        self.suspend()
        self.policy(False)
        self.assertIsNone(auth.session(token))
        queue.begin_collection(claim.lease)
        result = queue.complete(
            claim.lease, [VALID_ARTIFACT], actual_cost_microusd=None
        )
        self.assertEqual(
            (result["status"], result["result"]["billing_status"]),
            ("succeeded", "pending"),
        )
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (80, 0))
        self.assertEqual(self.entry_counts(), {"grant": 1, "hold": 1, "capture": 1})
        # Later supplier-invoice reconciliation must not repeat customer capture.
        for _ in range(2):
            self.repo.settle_completed_job(
                self.scope, original["id"], actual_cost_microusd=80_000
            )
        self.assertEqual(self.entry_counts(), {"grant": 1, "hold": 1, "capture": 1})
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 80_000)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(password_accounts)).all(), [])
        self.assert_invariants()

    def test_definite_queued_and_confirmed_running_cancellation_release_automatically(
        self,
    ):
        self.seed(100)
        self.policy()
        queued = self.admit()
        for _ in range(2):
            self.assertEqual(
                self.repo.request_cancel(self.scope, queued["id"])["status"],
                "cancelled",
            )
        self.assertEqual(self.balance()["held"], 0)
        running = self.admit()
        queue, claim = self.running(running)
        self.repo.request_cancel(self.scope, running["id"])
        self.assertEqual(self.balance()["held"], 20)
        with self.assertRaisesRegex(Conflict, "cancellation_not_confirmed"):
            queue.confirm_cancel(claim.lease)
        result = queue.confirm_cancel(
            claim.lease, upstream_stopped=True, actual_cost_microusd=30_000
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (100, 0))
        self.assertEqual(self.entry_counts(), {"grant": 1, "hold": 2, "release": 2})
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 30_000)
        self.assert_invariants()

    def test_unknown_expiry_recovers_original_attempt_and_retains_both_obligations(
        self,
    ):
        self.seed(100)
        self.policy()
        original = self.admit()
        queue = TaskQueue(self.repo)
        claim = queue.claim("first-worker", original["pool"], lease_seconds=10)
        queue.begin_submission(claim.lease)
        self.now += 11
        recovered = queue.recover_expired()
        self.assertEqual(
            [(row["id"], row["status"]) for row in recovered],
            [(original["id"], "submission_unknown")],
        )
        self.assertIsNone(queue.claim("no-second-generation", original["pool"]))
        self.repo.request_cancel(self.scope, original["id"])
        reconcile = queue.claim(
            "reconcile-original", original["pool"], purpose="reconcile"
        )
        self.assertEqual(reconcile.lease.attempt_id, claim.lease.attempt_id)
        with self.assertRaisesRegex(Conflict, "cancellation_not_confirmed"):
            queue.confirm_cancel(reconcile.lease)
        with self.assertRaisesRegex(Conflict, "upstream_still_uncertain"):
            queue.fail(reconcile.lease, "unknown_not_free", actual_cost_microusd=0)
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["attempt_no"], 1)
        self.assertEqual(self.balance()["held"], 20)
        self.assertEqual(self.entry_counts(), {"grant": 1, "hold": 1})
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(attempts)
                ).scalar_one(),
                1,
            )
        self.assert_invariants()


def load_tests(_loader, _standard, _pattern):
    # Keep this module's seven real-admission checks distinct from pure core tests.
    return unittest.TestSuite(
        CreditAdmissionTests(name)
        for name in CreditAdmissionTests.__dict__
        if name.startswith("test_")
    )
