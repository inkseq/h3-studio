"""Test-only daily authority uses original rows; no provider or clock service."""
import copy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import insert, select, update

from studio_platform.dstack_operator import DstackOperator, OperatorError, _request
from studio_platform.dstack_capacity import DstackError
from studio_platform.dstack_test_budget import ACCOUNT, budget_reason, day_window, projected_cost
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import BudgetExceeded, budget_accounts, budget_reservations, instance_intents
from studio_platform.worker_admission import dstack_budget_reason, managed_window_reason
from test_platform_repository import LedgerCase
import test_dstack_operator as original

GUARD = {"version": 1, "timezone": "Australia/Sydney", "daily_limit_microusd": 20_000_000,
    "budget_account_id": ACCOUNT, "max_runtime_seconds": 7200, "stop_margin_seconds": 300}


class LayeredClient(original.Client):
    def apply(self, spec):
        intent = spec["configuration"]["tags"]["sixnine_intent"]
        assert self.store.load(intent)["apply_started"] is True
        with self.store.repo.engine.connect() as connection:
            rows = list(connection.execute(select(budget_reservations).where(
                budget_reservations.c.reference_id == intent)).mappings())
            assert len(rows) == 2 and all(row["state"] == "reserved" for row in rows)
        self.applied.append(intent)
        value = {"id": str(uuid.uuid4()), "project_name": self.project,
            "run_spec": copy.deepcopy(spec), "status": "submitted", "cost": .1}
        self.runs[spec["run_name"]] = value
        return copy.deepcopy(value)


class DailyTestBudgetTests(LedgerCase):
    preview = original.DstackLifetimeTests.preview
    start = original.DstackOperatorTests.start
    running = original.DstackOperatorTests.running
    counts = original.DstackOperatorTests.counts

    def setUp(self):
        super().setUp()
        original.DstackOperatorTests._setup_dstack(self)
        self.client = LayeredClient(self.store)
        self.client.project = "sixnine-staging"
        self.capacity.client = self.client
        self.config['policy'].update(lifecycle_version=2, reservation_seconds=3600)
        profile = self.service.profiles['5090-fl']
        self.execution_policy = dict(enabled=True, backend='wangp-worker', dispatch_backend='hatchet-v1',
            pool=profile['pool'], configuration_id=profile['configuration_id'], model_id=profile['model_id'],
            engine_manifest_digest=profile['manifest_digest'], qualification=dict(status='accepted',
                verified_at=1., expires_at=20000.), reservation=dict(expected_runtime_s=1800,
                expires_at=20000.), envelope=dict(max_duration_seconds=4.))
        patched = patch('studio_platform.execution_profiles.selected_policy', return_value=self.execution_policy)
        patched.start(); self.addCleanup(patched.stop)
        self.settings.public_origin = "https://staging.inkseq.com"
        self.config["project"] = "sixnine-staging"
        self.config["policy"].update(max_instances=1, max_physical_gpus=1, test_gpu_guard=copy.deepcopy(GUARD))
        profile = self.config["profiles"][0]
        profile.update(pool="inkseq-staging-v6-fl", project_id="project-inkseq-staging-superdan",
            budget_account_ids=[ACCOUNT, "owner-budget"], extra_reservation_microusd=1_000_000)
        self.execution_policy["pool"] = profile["pool"]
        self.repo.configure_pool(profile["pool"], max_instances=1, max_physical_gpus=1)
        self.repo.configure_budget(ACCOUNT, tenant_id=self.scope.tenant_id, limit_microusd=7_300_000_000)
        self.service = DstackOperator(self.repo, self.settings, self.capacity, self.config)

    def rows(self):
        with self.repo.engine.connect() as connection:
            return (dict(connection.execute(select(instance_intents)).mappings().one()),
                    dict(connection.execute(select(operator_nodes)).mappings().one()))

    def history(self, *, amount, actual=None, created=None, settled=None):
        # The fixture uses original accepted rows, not a competing daily ledger.
        node_id = self.start()["node_id"]
        with self.repo.transaction() as connection:
            if actual is not None:
                self.repo._settle(connection, "instance", node_id, actual)
            connection.execute(update(budget_reservations).where(budget_reservations.c.reference_id == node_id)
                .values(amount_microusd=amount, created_at=self.now if created is None else created,
                    **({"settled_at": settled} if settled is not None else {})))
        return node_id

    def test_native_timeout_is_only_opt_in_and_cannot_change_idle_or_production_scope(self):
        node = self.start()["node_id"]
        binding = self.store.load(node)
        self.assertEqual(binding["run_spec"]["configuration"]["max_duration"], 7200)
        intent, row = self.rows()
        self.assertIsNone(intent["hard_deadline"])
        self.assertEqual(row["payload"]["lifecycle"]["test_gpu_guard"], GUARD)
        for options in ({"keep_on": "until_stopped"}, {"idle_shutdown_seconds": 601}):
            with self.assertRaisesRegex(OperatorError, "test_idle_policy"):
                self.preview(**options)
        self.settings.public_origin = "https://inkseq.com"
        with self.assertRaisesRegex(OperatorError, "test_guard_binding"):
            DstackOperator(self.repo, self.settings, self.capacity, self.config)
        from studio_platform.dstack_operator import _request
        old = replace(_request(row["payload"]["request"]), test_max_duration_s=None)
        self.assertEqual(old.run_spec(original.PUB)["configuration"]["max_duration"], "off")

    def test_changed_admin_runtime_reaches_native_spec_and_original_guard(self):
        from studio_platform.repository import spending_settings
        from studio_platform.spending_limits import ADMIN_OWNER, admin_defaults
        value=admin_defaults(self.service)
        value['max_runtime_seconds']=4000
        with self.repo.transaction() as connection:
            connection.execute(insert(spending_settings).values(tenant_id=self.settings.tenant_id,
                owner_id=ADMIN_OWNER,version=1,value=value,updated_at=self.now,actor_id=self.owner.actor_id))
        node=self.start(preview=self.preview())['node_id']
        self.assertEqual(self.store.load(node)['run_spec']['configuration']['max_duration'],4000)
        intent,row=self.rows()
        self.assertEqual(row['payload']['request']['test_max_duration_s'],4000)
        self.assertEqual(row['payload']['lifecycle']['test_gpu_guard']['max_runtime_seconds'],4000)
        with self.repo.engine.connect() as connection:
            self.assertIsNone(budget_reason(connection,intent,row,self.now))

    def test_runtime_below_verified_execution_runway_refuses_before_any_offer_or_money(self):
        from studio_platform.repository import spending_settings
        from studio_platform.spending_limits import ADMIN_OWNER, admin_defaults
        value=admin_defaults(self.service)
        value['max_runtime_seconds']=2000  # verified1800 + stop margin300 cannot fit
        with self.repo.transaction() as connection:
            connection.execute(insert(spending_settings).values(tenant_id=self.settings.tenant_id,
                owner_id=ADMIN_OWNER,version=1,value=value,updated_at=self.now,actor_id=self.owner.actor_id))
        with patch.object(self.capacity,'plan',side_effect=AssertionError('no provider calls')):
            with self.assertRaisesRegex(OperatorError,'dstack_test_runtime_limit_reached'):
                self.preview()
        self.assertEqual(self.counts(),(0,0,0,0))
        self.assertEqual(self.client.applied,[])

    def test_sydney_day_boundaries_include_dst_short_and_long_days(self):
        def stamp(value):
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
        start, end = day_window(stamp("2026-10-03T16:00:00"))
        self.assertEqual(end-start, 23*3600)
        start, end = day_window(stamp("2026-04-04T16:00:00"))
        self.assertEqual(end-start, 25*3600)
        start, end = day_window(stamp("2026-10-15T12:59:59"))
        next_start, _ = day_window(stamp("2026-10-15T13:00:00"))
        self.assertEqual(end, next_start)

    def test_unknown_hold_crosses_midnight_and_layers_are_not_double_counted(self):
        self.start()
        with self.repo.engine.connect() as connection:
            before = [dict(r) for r in connection.execute(select(budget_reservations)).mappings()]
            amount = projected_cost(connection, GUARD, self.now)
        self.assertEqual(amount, 1_812_500)
        self.now += 86400
        with self.repo.engine.connect() as connection:
            # Unknown allocation may still be billing; elapsed max-rate counts
            # conservatively instead of being discarded at midnight.
            self.assertGreater(projected_cost(connection, GUARD, self.now), 18_000_000)
            self.assertEqual([dict(r) for r in connection.execute(select(budget_reservations)).mappings()], before)

    def test_daily_projection_uses_actuals_and_resets_by_calendar_without_counter_reset(self):
        self.history(amount=1_812_500, actual=19_000_000)
        with self.repo.engine.connect() as connection:
            self.assertEqual(projected_cost(connection, GUARD, self.now), 19_000_000)
            before = [dict(r) for r in connection.execute(select(budget_accounts)).mappings()]
        preview = self.preview()
        with self.assertRaisesRegex(OperatorError, "test_daily_limit"):
            self.start(key="different", preview=preview)
        self.assertEqual(len(self.client.applied), 1)
        self.now = day_window(self.now)[1] + 1
        with self.repo.engine.connect() as connection:
            self.assertEqual(projected_cost(connection, GUARD, self.now), 0)
            self.assertEqual([dict(r) for r in connection.execute(select(budget_accounts)).mappings()], before)

    def test_controller_requests_normal_stop_before_native_limit_and_preserves_money(self):
        node_id = self.start()["node_id"]
        self.running(node_id)
        before = self.repo.get_budget(ACCOUNT)
        self.now += 6900
        from studio_platform.dstack_controller import DstackController
        controller = DstackController(self.service, SimpleNamespace(readiness=self.capacity.readiness))
        result = controller.tick()["nodes"][0]
        with self.repo.engine.connect() as connection:
            node = connection.execute(select(operator_nodes)).mappings().one()
            self.assertEqual(node["desired_state"], "stopped")
            self.assertEqual(node["payload"]["stop_reason_code"], "dstack_test_runtime_limit_reached")
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(len(self.client.stopped), 1)
        self.assertEqual(self.repo.get_budget(ACCOUNT), before)

    def test_admission_and_late_submission_share_full_runtime_runway_and_daily_cap(self):
        self.start()
        intent, node = self.rows()
        now = self.now + 5200
        self.assertEqual(managed_window_reason(intent, node, now, expected_runtime_s=1800),
            "dstack_test_runtime_limit_reached")
        with self.repo.transaction() as connection:
            self.assertEqual(dstack_budget_reason(connection, intent, node, now, expected_runtime_s=1800),
                "dstack_test_runtime_limit_reached")
            connection.execute(update(budget_reservations).where(budget_reservations.c.account_id == ACCOUNT)
                .values(amount_microusd=20_000_000))
            self.assertEqual(budget_reason(connection, intent, node, self.now), "dstack_test_daily_limit_reached")
            with self.assertRaisesRegex(BudgetExceeded, "test_daily_limit"):
                self.service.fund_window(connection, intent, node, self.now)

    def test_malformed_or_foreign_shared_reservation_fails_closed(self):
        self.start()
        with self.repo.transaction() as connection:
            connection.execute(update(operator_nodes).values(payload={"project_id": "production"}))
            with self.assertRaisesRegex(BudgetExceeded, "test_budget_unconfirmed"):
                projected_cost(connection, GUARD, self.now)

    def test_delayed_original_apply_rechecks_daily_cost_before_paid_request(self):
        first = self.start()["node_id"]
        preview = self.preview()
        empty = {"instances": 0, "physical_gpus": 0, "hourly_cost_microusd": 0,
            "active_instance_ids": ()}
        # Simulate transport interruption after the second original reservation
        # commits but before its normal exact-run pre-read/begin_apply.
        with patch.object(self.service, "_limits", return_value=empty), patch.object(
                self.capacity, "start", side_effect=DstackError("dstack_api_unavailable")):
            second = self.start(key="paused-original", preview=preview)["node_id"]
        with self.repo.transaction() as connection:
            node = connection.execute(select(operator_nodes).where(operator_nodes.c.intent_id == first)).mappings().one()
            payload = copy.deepcopy(node["payload"])
            payload["dstack"]["estimated_cost_microusd"] = 20_000_000
            connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == first).values(payload=payload))
            pending = connection.execute(select(operator_nodes.c.payload).where(
                operator_nodes.c.intent_id == second)).scalar_one()
            before = [dict(r) for r in connection.execute(select(budget_reservations)).mappings()]
        with self.assertRaisesRegex(OperatorError, "test_daily_limit"):
            self.capacity.start(_request(pending["request"]), original.PUB)
        self.assertEqual(self.client.applied, [first])
        self.assertFalse(self.store.load(second).get("apply_started", False))
        with self.repo.engine.connect() as connection:
            self.assertEqual([dict(r) for r in connection.execute(select(budget_reservations)).mappings()], before)


if __name__ == "__main__":
    unittest.main()
