"""Adopted dstack count authority and fulfillment; isolated ledger/fake reads."""

import copy
from dataclasses import replace
from unittest.mock import patch

from sqlalchemy import func, insert, select, update

from studio_platform.capacity_settings import DEFAULTS, admission_snapshot
from studio_platform.control import WorkerControl
from studio_platform.dstack_operator import DstackOperator, OperatorError
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import (
    BudgetExceeded,
    Conflict,
    budget_reservations,
    instance_intents,
    outbox,
    registered_devices,
    registered_workers,
    spending_settings,
)
from studio_platform.spending_limits import ADMIN_OWNER
from test_platform_repository import LedgerCase
import test_dstack_operator as fixtures
import test_dstack_registration as registration_fixtures


def adopt(case, maximum=3):
    with case.repo.transaction() as connection:
        condition = (
            spending_settings.c.tenant_id == case.scope.tenant_id,
            spending_settings.c.owner_id == ADMIN_OWNER,
        )
        old = (
            connection.execute(select(spending_settings).where(*condition))
            .mappings()
            .first()
        )
        value = {**copy.deepcopy(DEFAULTS), "max_machines": maximum}
        row = {
            "value": value,
            "version": old["version"] + 1 if old else 1,
            "updated_at": case.now,
            "actor_id": "browser:admin",
        }
        if old:
            connection.execute(
                update(spending_settings).where(*condition).values(**row)
            )
        else:
            connection.execute(
                insert(spending_settings).values(
                    tenant_id=case.scope.tenant_id, owner_id=ADMIN_OWNER, **row
                )
            )
        return admission_snapshot(row)


class ReservationCapacityAdmissionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool("dstack-test", max_instances=1, max_physical_gpus=1)
        self.admitted = adopt(self)

    def reserve(
        self, key="one", *, snapshot=None, provenance=None, scope=None, **overrides
    ):
        scope = scope or self.scope
        snapshot = self.admitted if snapshot is None else snapshot
        proof = {
            "capacity_backend": "dstack-v1",
            "lifecycle_version": 2,
            "tenant_id": scope.tenant_id,
            "owner_id": scope.owner_id,
            "project_id": scope.project_id,
            "pool": "dstack-test",
            "provider": "vast",
        }
        values = {
            "hard_deadline": None,
            "provider": "vast",
            "continuing": True,
            "reserved_cost_microusd": 100_000,
            "budget_account_ids": ("owner-budget",),
            "dry_run": False,
            "capacity_policy_snapshot": snapshot,
            "capacity_provenance": proof if provenance is None else provenance,
        }
        values.update(overrides)
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            values.setdefault("capacity_usage", self.repo._global_usage(connection))
            return self.repo.reserve_instance_intent(
                scope, "dstack-test", key, connection=connection, **values
            )

    def ledger(self):
        with self.repo.engine.connect() as connection:
            return (
                connection.scalar(select(func.count()).select_from(instance_intents)),
                connection.scalar(
                    select(func.count()).select_from(budget_reservations)
                ),
                self.repo.get_budget("owner-budget"),
            )

    def test_adopted_max_replaces_both_positive_old_count_limits(self):
        created = [self.reserve(str(i)) for i in range(3)]
        before = self.ledger()
        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            self.reserve("four")
        self.assertEqual(before, self.ledger())
        self.assertEqual(before[:2], (3, 3))
        with self.repo.engine.connect() as connection:
            events = list(
                connection.execute(
                    select(outbox).where(outbox.c.event_type == "instance.reserved")
                ).mappings()
            )
        self.assertEqual(
            {r["aggregate_id"] for r in events}, {r["id"] for r in created}
        )
        self.assertTrue(
            all(
                r["payload"]["capacity_policy_snapshot"] == self.admitted
                for r in events
            )
        )

    def test_original_replay_precedes_new_policy_but_changed_digest_conflicts(self):
        original = self.reserve()
        newer = adopt(self, 1)
        before = self.ledger()
        replay = self.reserve()
        self.assertEqual(replay["id"], original["id"])
        self.assertFalse(replay["created"])
        with self.assertRaisesRegex(Conflict, "instance_intent_conflict"):
            self.reserve(snapshot=newer)
        with self.assertRaisesRegex(Conflict, "capacity_policy_snapshot_changed"):
            self.reserve("new-stale")
        self.assertEqual(before, self.ledger())

    def test_zero_global_or_pool_gate_never_becomes_enabled(self):
        for target in ("global", "pool"):
            for field in ("max_instances", "max_physical_gpus"):
                with self.subTest(target=target, field=field):
                    values = {"max_instances": 1, "max_physical_gpus": 1, field: 0}
                    if target == "global":
                        self.repo.configure_capacity(**values)
                    else:
                        self.repo.configure_pool("dstack-test", **values)
                    before = self.ledger()
                    with self.assertRaises(BudgetExceeded):
                        self.reserve()
                    self.assertEqual(before, self.ledger())
                    self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
                    self.repo.configure_pool(
                        "dstack-test", max_instances=1, max_physical_gpus=1
                    )

    def test_v1_wrong_scope_or_extra_device_cannot_supply_adopted_authority(self):
        for changes in (
            {"capacity_backend": "legacy"},
            {"lifecycle_version": 1},
            {"lifecycle_version": 2.0},
            {"owner_id": "other"},
            {"pool": "other"},
        ):
            proof = {
                "capacity_backend": "dstack-v1",
                "lifecycle_version": 2,
                "tenant_id": self.scope.tenant_id,
                "owner_id": self.scope.owner_id,
                "project_id": self.scope.project_id,
                "pool": "dstack-test",
                "provider": "vast",
                **changes,
            }
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.reserve(provenance=proof)
        for changes in (
            {"physical_gpus": 2},
            {"slots": 2},
            {"continuing": False, "hard_deadline": self.now + 900},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.reserve(**changes)
        self.assertEqual(self.ledger()[:2], (0, 0))

    def test_legacy_call_keeps_original_counts_and_request_hash(self):
        values = dict(
            hard_deadline=self.now + 900,
            provider="vast",
            dry_run=False,
            reserved_cost_microusd=100_000,
            budget_account_ids=("owner-budget",),
        )
        original = self.repo.reserve_instance_intent(
            self.scope, "dstack-test", "legacy", **values
        )
        self.assertFalse(
            self.repo.reserve_instance_intent(
                self.scope, "dstack-test", "legacy", **values
            )["created"]
        )
        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            self.repo.reserve_instance_intent(
                self.scope, "dstack-test", "another", **values
            )
        with self.repo.engine.connect() as connection:
            event = (
                connection.execute(
                    select(outbox).where(
                        outbox.c.aggregate_id == original["id"],
                        outbox.c.event_type == "instance.reserved",
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(
            event["payload"], {"intent_id": original["id"], "pool": "dstack-test"}
        )

    def test_cross_owner_admissions_share_global_lock_and_adopted_limit(self):
        self.admitted = adopt(self, 2)
        self.repo.configure_budget(
            "other-budget",
            tenant_id=self.other.tenant_id,
            owner_id=self.other.owner_id,
            limit_microusd=10_000_000,
        )

        def attempt(index):
            try:
                self.reserve(
                    str(index),
                    scope=self.other if index % 2 else self.scope,
                    budget_account_ids=("other-budget",)
                    if index % 2
                    else ("owner-budget",),
                )
                return "reserved"
            except BudgetExceeded:
                return "blocked"

        results = self.parallel(attempt)
        self.assertEqual(results.count("reserved"), 2)
        self.assertEqual(self.ledger()[:2], (2, 2))


class RegistrationCapacityAdmissionTests(LedgerCase):
    node = registration_fixtures.RegistrationCapacityTests.node
    spec = registration_fixtures.RegistrationCapacityTests.spec
    start = fixtures.DstackOperatorTests.start
    snapshot = registration_fixtures.RegistrationCapacityTests.snapshot

    def setUp(self):
        super().setUp()
        fixtures.DstackOperatorTests._setup_dstack(self)
        self.admitted = adopt(self)
        config = copy.deepcopy(self.config)
        config["policy"].update(
            lifecycle_version=2, reservation_seconds=3600, expires_at=None
        )
        self.service = DstackOperator(self.repo, self.settings, self.capacity, config)
        runtime = patch.object(
            self.service, "_execution_check", return_value=(120, None)
        )
        runtime.start()
        self.addCleanup(runtime.stop)

    def control(self):
        return WorkerControl(
            self.repo, capacity_usage=self.service.registration_capacity_usage
        )

    def test_original_already_counted_v2_resource_registers_after_lowering(self):
        nodes = [self.node("4000001"), self.node("4000002")]
        before = self.snapshot()
        adopt(self, 1)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool("dstack-test", max_instances=1, max_physical_gpus=1)
        for node in nodes:
            self.control().register(self.spec(node))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.client.applied), 2)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(registered_workers)),
                2,
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(registered_devices)),
                2,
            )

    def test_disabled_gate_still_blocks_original_fulfillment(self):
        node = self.node("4000001")
        for target in ("global", "pool"):
            with self.subTest(target=target):
                if target == "global":
                    self.repo.configure_capacity(max_instances=0, max_physical_gpus=1)
                else:
                    self.repo.configure_pool(
                        "dstack-test", max_instances=1, max_physical_gpus=0
                    )
                with self.assertRaises(BudgetExceeded):
                    self.control().register(self.spec(node))
                self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)

    def test_missing_original_snapshot_retains_numeric_capacity_check(self):
        nodes = [self.node("4000001"), self.node("4000002")]
        with self.repo.transaction() as connection:
            row = (
                connection.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == nodes[0])
                )
                .mappings()
                .one()
            )
            payload = copy.deepcopy(row["payload"])
            payload.pop("capacity_policy_snapshot")
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == nodes[0])
                .values(payload=payload)
            )
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            self.control().register(self.spec(nodes[0]))

    def test_another_gpu_cannot_use_original_one_gpu_fulfillment(self):
        node = self.node("4000001")
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        before = self.snapshot()
        with self.assertRaises((ValueError, OperatorError)):
            self.control().register(
                replace(self.spec(node), physical_gpu_ids=("GPU-a", "GPU-b"))
            )
        self.assertEqual(before, self.snapshot())

    def test_v1_node_with_policy_metadata_keeps_legacy_registration_count(self):
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        nodes = [self.node("4000001"), self.node("4000002")]
        with self.repo.engine.connect() as connection:
            payload = connection.execute(
                select(operator_nodes.c.payload).where(
                    operator_nodes.c.intent_id == nodes[0]
                )
            ).scalar_one()
        self.assertIsNotNone(payload.get("capacity_policy_snapshot"))
        self.assertEqual(payload["request"].get("lifecycle_version", 1), 1)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            self.control().register(self.spec(nodes[0]))

    def test_proof_cannot_authorize_an_extra_resource_missing_from_usage(self):
        node = self.node("4000001")
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)

        def missing_original(connection, spec):
            usage = self.service.registration_capacity_usage(connection, spec)
            usage["resources"] = {("vast", "different-counted-instance"): 1}
            return usage

        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            WorkerControl(self.repo, capacity_usage=missing_original).register(
                self.spec(node)
            )
