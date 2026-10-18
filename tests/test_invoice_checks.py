"""Original ledger stays immutable during bounded nonfinal charge inspection."""

import copy
import json
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch
import httpx

from sqlalchemy import delete, insert, select, update
from studio_platform.invoice_checks import (
    INTERVAL,
    OPERATION,
    inspect_pending_invoices,
    public_reconciliation,
    read_vast_charges,
)
from studio_platform.repository import (
    budget_accounts,
    budget_reservations,
    instance_intents,
    jobs,
)
from studio_platform.operator_capacity import operator_commands, operator_nodes
from studio_platform.repository import scaler_receipts, spending_origins
from studio_platform.spending_limits import public_usage
from test_platform_repository import LedgerCase


def charge(ident="123", amount="2.039", start=100, end=200):
    return {
        "source": "instance-" + ident,
        "type": "instance",
        "amount": amount,
        "start": start,
        "end": end,
        "description": "PRIVATE LABEL",
        "items": [{"type": "gpu", "amount": amount}],
    }


def page(rows, *, total=None, token=None):
    return {
        "success": True,
        "count": len(rows),
        "total": len(rows) if total is None else total,
        "next_token": token,
        "results": rows,
    }


class ChargeReaderTests(unittest.TestCase):
    def test_existing_client_uses_same_key_fixed_get_and_bounded_active_timeout(self):
        from studio_platform.dstack_capacity import DstackClient

        calls = []

        def control(request):
            calls.append(request)
            self.assertEqual(
                request.url.path,
                "/api/project/test-project/backends/vastai/config_info",
            )
            self.assertLessEqual(request.extensions['timeout']['read'], 2)
            return httpx.Response(
                200,
                json={
                    'type': 'vastai',
                    'creds': {
                        'type': 'api_key',
                        'api_key': 'fake-provider-key-123456789',
                    },
                },
            )

        def provider(request):
            calls.append(request)
            self.assertEqual(
                (request.method, str(request.url.copy_with(query=None))),
                ('GET', 'https://console.vast.ai/api/v0/charges/'),
            )
            self.assertEqual(
                request.headers['Authorization'], 'Bearer fake-provider-key-123456789'
            )
            self.assertLessEqual(request.extensions['timeout']['read'], 3)
            return httpx.Response(200, json=page([charge()]))

        with (
            httpx.Client(transport=httpx.MockTransport(control)) as local,
            httpx.Client(transport=httpx.MockTransport(provider)) as remote,
        ):
            client = DstackClient(
                'http://localhost:3000',
                'fake-controller-token-123456',
                'test-project',
                session=local,
                provider_session=remote,
            )
            self.assertEqual(
                client.vast_charges(100, 300)['records']['instance-123'][
                    'reported_cost_microusd'
                ],
                2039000,
            )
        self.assertEqual(len(calls), 2)

    def test_complete_pagination_counts_parent_charge_once_and_drops_labels(self):
        read = Mock(
            side_effect=[
                page([charge()], total=2, token="next"),
                page([charge("124", ".996")], total=2),
            ]
        )
        value = read_vast_charges(read, 100, 300)
        self.assertEqual(
            value["records"]["instance-123"]["reported_cost_microusd"], 2_039_000
        )
        self.assertEqual(
            sum(x["reported_cost_microusd"] for x in value["records"].values()),
            3_035_000,
        )
        self.assertTrue(value["complete"])
        self.assertFalse(value["final_cost_confirmed"])
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertEqual(read.call_args_list[1].args[0]["after_token"], "next")

    def test_bad_money_identity_time_or_partial_pages_never_become_cost_proof(self):
        bad = [
            None,
            charge(amount=True),
            charge(amount="NaN"),
            charge(amount="-.1"),
            charge(amount="0.0000001"),
            charge(ident="0"),
            charge(start=99),
            charge(end=301),
        ]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                read_vast_charges(lambda *_: page([row]), 100, 300)
        for data in [
            page([charge()], total=2),
            page([charge(), charge()]),
            {**page([charge()]), "count": True},
            {**page([]), "next_token": "next"},
        ]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                read_vast_charges(lambda *_: data, 100, 300)

    def test_repeated_cursor_and_bounded_window_are_rejected(self):
        read = Mock(
            side_effect=[
                page([charge()], total=3, token="next"),
                page([charge("124")], total=3, token="next"),
            ]
        )
        with self.assertRaises(ValueError):
            read_vast_charges(read, 100, 300)
        with self.assertRaises(ValueError):
            read_vast_charges(read, 100, 32 * 86400)
        clock = [0.0]

        def late(*_):
            clock[0] = 31.0
            return page([charge()])

        with self.assertRaisesRegex(ValueError, 'timeout'):
            read_vast_charges(late, 100, 300, monotonic=lambda: clock[0])


class InvoiceInspectionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = 1792329300.0
        self.repo.configure_pool("invoice-test", max_instances=3, max_physical_gpus=3)

    def instance(self, *, identity="123", payload_changes=None):
        row = self.repo.reserve_instance_intent(
            self.scope,
            "invoice-test",
            uuid.uuid4().hex,
            reserved_cost_microusd=500_000,
            hard_deadline=self.now + 3600,
            budget_account_ids=("owner-budget",),
            dry_run=False,
            provider="vast",
        )
        cmd = str(uuid.uuid4())
        payload = {
            "tenant_id": self.scope.tenant_id,
            "capacity_backend": "dstack-v1",
            "dstack": {"backend": "vastai", "provider_instance_id": identity},
        }
        payload.update(payload_changes or {})
        with self.repo.transaction() as conn:
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == row["id"])
                .values(state="destroyed", provider_instance_id=identity)
            )
            conn.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == row["id"])
                .values(state="invoice_pending")
            )
            conn.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == "owner-budget")
                .values(reserved_microusd=0)
            )
            conn.execute(
                insert(operator_commands).values(
                    id=cmd,
                    actor="browser:superdan",
                    idempotency_key=cmd,
                    request_hash="a" * 64,
                    kind="start",
                    state="complete",
                    payload={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            conn.execute(
                insert(operator_nodes).values(
                    intent_id=row["id"],
                    command_id=cmd,
                    ordinal=0,
                    binding_id="invoice-test",
                    binding_hash="b" * 64,
                    payload=payload,
                    desired_state="stopped",
                    runtime_state="stopped",
                    updated_at=self.now,
                )
            )
        return row["id"]

    def ledger(self):
        with self.repo.engine.connect() as conn:
            return {
                t.name: [dict(r) for r in conn.execute(select(t)).mappings()]
                for t in (budget_accounts, budget_reservations, instance_intents, jobs)
            }

    def client(self, *, failure=False, during_read=None):
        def read(start, end):
            if during_read:
                during_read()
            if failure:
                raise RuntimeError("PRIVATE AUTHORIZATION must not appear")
            return read_vast_charges(
                lambda *_: page([charge(start=start, end=end, amount=".123")]),
                start,
                end,
            )

        return SimpleNamespace(vast_charges=Mock(side_effect=read))

    def status(self, owner=None):
        with self.repo.engine.connect() as conn:
            return public_reconciliation(
                conn, self.scope.tenant_id, owner or self.scope.owner_id, self.now
            )

    def test_provider_snapshot_records_only_receipt_and_never_settles_or_allocates(
        self,
    ):
        ident = self.instance()
        before = self.ledger()
        client = self.client()
        self.assertEqual(
            inspect_pending_invoices(self.repo, self.scope.tenant_id, client)[
                "checked"
            ],
            1,
        )
        self.assertEqual(before, self.ledger())
        status = self.status()["instance"]
        self.assertEqual(status["state"], "current")
        self.assertEqual(status["reported_cost_microusd"], 123_000)
        self.assertFalse(status["final_cost_confirmed"])
        self.assertEqual(self.status()["job"]["state"], "allocation_required")
        self.assertEqual(self.status("supervan")["instance"]["pending_count"], 0)
        with self.repo.engine.connect() as conn:
            self.assertIsNone(
                conn.execute(
                    select(budget_reservations.c.actual_cost_microusd)
                ).scalar_one()
            )
            self.assertEqual(
                conn.execute(select(scaler_receipts.c.operation)).scalar_one(),
                OPERATION,
            )
            self.assertNotIn(
                "reconciliation",
                public_usage(
                    conn,
                    self.scope.tenant_id,
                    self.scope.owner_id,
                    self.now,
                    actor_id="key:test",
                ),
            )

    def test_restart_uses_durable_interval_and_no_pending_means_no_provider_read(self):
        client = self.client()
        self.assertEqual(
            inspect_pending_invoices(self.repo, self.scope.tenant_id, client)[
                "checked"
            ],
            0,
        )
        client.vast_charges.assert_not_called()
        self.instance()
        inspect_pending_invoices(self.repo, self.scope.tenant_id, client)
        # New caller state: suppression is from original durable receipt.
        self.assertEqual(
            inspect_pending_invoices(self.repo, self.scope.tenant_id, client)[
                "checked"
            ],
            0,
        )
        self.assertEqual(client.vast_charges.call_count, 1)
        self.now += INTERVAL
        self.assertEqual(
            inspect_pending_invoices(self.repo, self.scope.tenant_id, client)[
                "checked"
            ],
            1,
        )
        self.assertEqual(client.vast_charges.call_count, 2)
        self.now += 2 * INTERVAL
        self.assertEqual(self.status()["instance"]["state"], "overdue")

    def test_provider_refusal_and_missing_identity_preserve_unknown_actual(self):
        self.instance()
        before = self.ledger()
        inspect_pending_invoices(
            self.repo, self.scope.tenant_id, self.client(failure=True)
        )
        self.assertEqual(before, self.ledger())
        self.assertEqual(self.status()["instance"]["state"], "unavailable")
        with self.repo.engine.connect() as conn:
            facts = conn.execute(select(scaler_receipts.c.facts)).scalar_one()
            self.assertNotIn("PRIVATE", json.dumps(facts))
        self.instance(identity=None)
        client = self.client()
        inspect_pending_invoices(self.repo, self.scope.tenant_id, client)
        client.vast_charges.assert_not_called()

    def test_changed_identity_during_read_cannot_attach_wrong_provider_charge(self):
        ident = self.instance()

        def changed():
            with self.repo.transaction() as conn:
                conn.execute(
                    update(instance_intents)
                    .where(instance_intents.c.id == ident)
                    .values(provider_instance_id="999")
                )

        result = inspect_pending_invoices(
            self.repo, self.scope.tenant_id, self.client(during_read=changed)
        )
        self.assertEqual(result["checked"], 0)
        with self.repo.engine.connect() as conn:
            self.assertEqual(list(conn.execute(select(scaler_receipts))), [])

    def test_old_window_or_inconsistent_provider_does_not_starve_current_instance(self):
        old = self.instance(identity="111")
        recent = self.instance()
        wrong = self.instance(identity="222")
        with self.repo.transaction() as conn:
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == old)
                .values(created_at=self.now - 40 * 86400)
            )
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == wrong)
                .values(provider="targon")
            )
        client = self.client()
        before = self.ledger()
        result = inspect_pending_invoices(self.repo, self.scope.tenant_id, client)
        self.assertEqual(result["checked"], 3)
        self.assertEqual(before, self.ledger())
        self.assertEqual(client.vast_charges.call_count, 1)
        self.assertGreaterEqual(
            client.vast_charges.call_args.args[0], int(self.now) // 86400 * 86400
        )
        with self.repo.engine.connect() as conn:
            values = {
                x["intent_id"]: x["facts"]
                for x in conn.execute(select(scaler_receipts)).mappings()
            }
        self.assertTrue(values[recent]["complete"])
        self.assertIsNone(values[old]["reported_cost_microusd"])
        self.assertIsNone(values[wrong]["reported_cost_microusd"])

    def test_future_receipt_is_not_a_healthy_check_or_reason_to_read_again(self):
        self.instance()
        client = self.client()
        inspect_pending_invoices(self.repo, self.scope.tenant_id, client)
        with self.repo.transaction() as conn:
            conn.execute(update(scaler_receipts).values(observed_at=self.now + 100))
        self.assertEqual(self.status()["instance"]["state"], "unavailable")
        self.assertEqual(
            self.status()["instance"]["reason_code"],
            "provider_charge_clock_unconfirmed",
        )
        self.assertIsNone(self.status()["instance"]["reported_cost_microusd"])
        self.assertEqual(
            inspect_pending_invoices(self.repo, self.scope.tenant_id, client)["state"],
            "clock_unconfirmed",
        )
        self.assertEqual(client.vast_charges.call_count, 1)

    def test_shared_only_known_owner_uses_origin_and_duplicate_identity_is_not_added(
        self,
    ):
        first = self.instance()
        second = self.instance()
        with self.repo.transaction() as conn:
            conn.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == 'owner-budget')
                .values(owner_id=None)
            )
            conn.execute(
                update(spending_origins)
                .where(spending_origins.c.reference_id == first)
                .values(actor_id='key:original')
            )
            conn.execute(
                delete(spending_origins).where(
                    spending_origins.c.reference_id == second
                )
            )
        inspect_pending_invoices(self.repo, self.scope.tenant_id, self.client())
        self.assertEqual(self.status()['instance']['pending_count'], 1)
        self.assertEqual(self.status()['instance']['reported_cost_microusd'], 123000)
        self.assertEqual(self.status('supervan')['instance']['pending_count'], 0)
        with self.repo.transaction() as conn:
            conn.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == 'owner-budget')
                .values(owner_id=self.scope.owner_id)
            )
        self.assertEqual(self.status()['instance']['pending_count'], 2)
        self.assertEqual(
            self.status()['instance']['reason_code'],
            'provider_charge_identity_conflict',
        )
        self.assertIsNone(self.status()['instance']['reported_cost_microusd'])

    def test_existing_active_tick_checks_once_per_interval_without_new_demand_loop(
        self,
    ):
        from studio_platform.dstack_controller import DstackController

        c = DstackController.__new__(DstackController)
        c.repo = self.repo
        c.service = SimpleNamespace(
            settings=SimpleNamespace(tenant_id=self.scope.tenant_id)
        )
        c.capacity = SimpleNamespace(client=object())
        c.worker_process = None
        c._cleanup_pending = set()
        c._passive_unknown = {}
        c._invoice_checked_at = None
        c._nodes = lambda: []
        c._business_demand = lambda conn: True
        with patch(
            'studio_platform.invoice_checks.inspect_pending_invoices',
            return_value={'state': 'observed'},
        ) as inspect:
            c.tick()
            c.tick()
            self.assertEqual(inspect.call_count, 1)
            self.now += INTERVAL
            c.tick()
            self.assertEqual(inspect.call_count, 2)
            # Same original controller's idle safety path shares the throttle.
            c.safety_sweep()
            self.assertEqual(inspect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
