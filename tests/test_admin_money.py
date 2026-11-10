"""Admin money views (#485, #966): revenue and ledger spend beside balances."""

from pathlib import Path
from types import SimpleNamespace
import uuid

from sqlalchemy import Column, MetaData, String, Table, insert, select, update

from studio_platform.auth import Principal
from studio_platform.credit_schema import accounts, entries
from studio_platform.credit_schema import metadata as credit_metadata
from studio_platform.credits_pricing import PricingError
from studio_platform.credits_routes import CreditAPI
from studio_platform.identity_schema import metadata, users
from studio_platform.payment_schema import events, frozen_metadata, payments
from studio_platform.spending_by_provider import ledger_spend
from studio_platform.repository import budget_reservations, instance_intents
from studio_platform.schema_version import LIVE_PAYMENT_REVISION
from test_platform_repository import LedgerCase

DAY = 86400


class AdminMoneyCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = 400 * DAY
        marker = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        self.ids = {name: str(uuid.uuid4()) for name in ("superdan", "supervan")}
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            credit_metadata.create_all(connection)
            frozen_metadata(revision=LIVE_PAYMENT_REVISION).create_all(connection)
            marker.create(connection)
            connection.execute(insert(marker).values(version_num=LIVE_PAYMENT_REVISION))
            for owner, ident in self.ids.items():
                connection.execute(
                    insert(users).values(
                        id=ident,
                        tenant="sixnine",
                        owner=owner,
                        role="admin" if owner == "superdan" else "user",
                        status="active",
                        display_name=owner,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.settings = SimpleNamespace(tenant_id="sixnine")
        self.service = CreditAPI(self.repo, self.settings)
        self.admin = Principal(
            "superdan", "browser:superdan", role="admin", identity_source="canonical"
        )
        self.user = Principal(
            "supervan", "browser:supervan", identity_source="canonical"
        )


class RevenueTests(AdminMoneyCase):
    def pay(self, *, live, user=None, days_ago=1, cents=1000, gross=None, **extra):
        ident = str(uuid.uuid4())
        paid = self.now - days_ago * DAY
        row = {
            "id": ident,
            "tenant": "sixnine",
            "user_id": user or str(uuid.uuid4()),
            "pack_id": "starter",
            "pack_revision": "r1",
            "pack_snapshot": {},
            "price_id": "price_one",
            "credits": 1000,
            "amount_cents": cents,
            "currency": "usd",
            "account_scope": "acct_one",
            "livemode": int(live),
            "idempotency_key": ident,
            "request_fingerprint": "a" * 64,
            "terms_version": "t1",
            "terms_accepted_at": paid,
            "status": "paid",
            "created_at": paid,
            "updated_at": paid,
            "paid_at": paid,
            "refunded_cents": 0,
            "refund_credits": 0,
            "clawed_back_credits": 0,
            "unmet_credits": 0,
            "dispute_open": 0,
            "stripe_dispute_id": None,
            **extra,
        }
        with self.repo.transaction() as connection:
            connection.execute(insert(payments).values(**row))
            if gross is not None:
                connection.execute(
                    insert(events).values(
                        event_id="evt_" + uuid.uuid4().hex,
                        tenant="sixnine",
                        account_scope="acct_one",
                        livemode=int(live),
                        type="checkout.session.completed",
                        verified_payload={
                            "type": "checkout.session.completed",
                            "object": {
                                "payment_status": "paid",
                                "currency": "usd",
                                "amount_cents": gross,
                            },
                        },
                        fingerprint="b" * 64,
                        received_at=paid,
                        processed_at=paid,
                        status="processed",
                        payment_id=ident,
                        reason_code=None,
                    )
                )
        return row

    def ledger(self, kind, amount, *, balance=0, user=None, days_ago=1):
        user = user or self.ids["supervan"]
        with self.repo.transaction() as connection:
            connection.execute(
                insert(entries).values(
                    id=str(uuid.uuid4()),
                    tenant="sixnine",
                    user_id=user,
                    kind=kind,
                    amount=amount,
                    held_delta=0,
                    balance_after=balance,
                    held_after=0,
                    reference_type="job",
                    reference_id=uuid.uuid4().hex,
                    reason="fixture",
                    actor="fixture",
                    idempotency_key=uuid.uuid4().hex,
                    fingerprint="c" * 64,
                    created_at=self.now - days_ago * DAY,
                )
            )

    def read(self, days=30):
        return self.service.admin_revenue(self.admin, days)

    def test_live_and_test_orders_are_reported_apart(self):
        self.pay(live=True)
        self.pay(live=False)
        self.pay(live=False)
        value = self.read()
        self.assertEqual(value["live"]["orders"], 1)
        self.assertEqual(value["test"]["orders"], 2)
        self.assertEqual(value["live"]["gross_cents"], 1000)
        self.assertEqual(value["test"]["gross_cents"], 2000)

    def test_fixture_numbers(self):
        repeat = str(uuid.uuid4())
        # A payer from before the period is a repeat payer, not a first-time one.
        self.pay(live=True, user=repeat, days_ago=40)
        self.pay(live=True, user=repeat, days_ago=2, cents=1000, gross=1100)
        self.pay(live=True, days_ago=3, pack_id="studio", cents=5000)
        self.pay(
            live=True,
            days_ago=4,
            refunded_cents=1000,
            refund_credits=1000,
            clawed_back_credits=1000,
            status="refunded",
        )
        self.pay(live=True, days_ago=5, dispute_open=1, stripe_dispute_id="dp_x")
        self.pay(live=True, days_ago=31)
        value = self.read(30)
        live = value["live"]
        self.assertEqual(live["orders"], 4)
        self.assertEqual(live["orders_by_pack"], {"starter": 3, "studio": 1})
        self.assertEqual(live["gross_cents"], 1100 + 5000 + 1000 + 1000)
        self.assertEqual(live["tax_cents"], 100)
        self.assertEqual(live["refunded_cents"], 1000)
        self.assertEqual((live["disputed_count"], live["open_disputes"]), (1, 1))
        self.assertEqual((live["paying_users"], live["first_time_payers"]), (4, 3))
        self.assertEqual(live["credits_sold"], 4000)
        # Kept pre-tax: 1000 + 5000 + 0 (refunded) + 0 (open dispute) = 6000;
        # fees at 7% of each gross plus 20 cents: 77 + 350 + 70 + 70 + 4 * 20.
        self.assertEqual(live["net_estimate_cents"], 6000 - 567 - 80)
        self.assertTrue(value["net_estimate"]["estimated"])
        self.assertIsNone(value["provider_cost_usd"])
        self.assertEqual(value["test"]["orders"], 0)
        self.assertEqual(self.read(90)["live"]["orders"], 6)
        self.assertEqual(self.read(7)["live"]["orders"], 4)

    def test_credits_used_and_outstanding(self):
        self.ledger("capture", -300)
        self.ledger("capture", -50, days_ago=60)
        with self.repo.transaction() as connection:
            for owner, balance in (("superdan", 700), ("supervan", 1200)):
                connection.execute(
                    insert(accounts).values(
                        user_id=self.ids[owner],
                        tenant="sixnine",
                        balance=balance,
                        held=0,
                        version=1,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        value = self.read()
        self.assertEqual(value["credits_used"], 300)
        self.assertEqual(value["credits_outstanding"], 1900)
        self.assertEqual(self.read(90)["credits_used"], 350)

    def test_only_admins_and_known_periods(self):
        with self.assertRaises(PricingError) as refused:
            self.service.admin_revenue(self.user, 30)
        self.assertEqual(refused.exception.status, 403)
        with self.assertRaises(PricingError) as invalid:
            self.read(14)
        self.assertEqual(invalid.exception.status, 422)

    def test_read_writes_nothing(self):
        self.pay(live=True)
        with self.repo.engine.connect() as connection:
            before = connection.execute(select(payments)).mappings().all()
        self.read()
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(payments)).mappings().all(), before
            )


class ProviderSpendTests(AdminMoneyCase):
    def setUp(self):
        super().setUp()
        # The ledger scope's owner must be an active account to hold a job.
        with self.repo.transaction() as connection:
            connection.execute(
                insert(users).values(
                    id=str(uuid.uuid4()),
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    role="user",
                    status="active",
                    display_name="ledger",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )

    def rental(self, provider, amount, *, layers=1, actual=None, days_ago=1):
        reference = str(uuid.uuid4())
        created = self.now - days_ago * DAY
        accounts = ["owner-budget"]
        for index in range(1, layers):
            name = "layer-%d" % index
            self.repo.configure_budget(
                name, tenant_id="test-tenant", limit_microusd=10_000_000
            )
            accounts.append(name)
        with self.repo.transaction() as connection:
            connection.execute(
                insert(instance_intents).values(
                    id=reference,
                    intent_key=reference,
                    pool="rental-pool",
                    request_hash="a" * 64,
                    state="destroyed" if actual is not None else "running",
                    physical_gpus=1,
                    slots=1,
                    reserved_cost_microusd=amount,
                    provider=provider,
                    created_at=created,
                    updated_at=created,
                )
            )
            for account in accounts:
                connection.execute(
                    insert(budget_reservations).values(
                        id=str(uuid.uuid4()),
                        account_id=account,
                        reference_type="instance",
                        reference_id=reference,
                        amount_microusd=amount,
                        state="reserved" if actual is None else "settled",
                        actual_cost_microusd=actual,
                        created_at=created,
                        settled_at=None if actual is None else created + 60,
                    )
                )

    def hosted(self, backend, cost, actual):
        plan = self.plan(
            cost=cost,
            execution={"pool": "hosted", "backend": backend, "expected_runtime_s": 1},
        )
        job = self.repo.create_job(
            self.scope,
            plan["id"],
            uuid.uuid4().hex,
            budget_account_ids=("owner-budget",),
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == job["id"])
                .values(
                    state="settled", actual_cost_microusd=actual, settled_at=self.now
                )
            )

    def test_spend_per_provider_counts_each_rental_once(self):
        self.rental("vast", 900_000, layers=3, actual=400_000)
        self.rental("vast", 500_000)
        self.rental("lium", 300_000)
        self.rental("modal-dialout", 200_000, actual=150_000)
        self.rental("vast", 700_000, actual=700_000, days_ago=45)
        self.hosted("everyframe-render", 80_000, 60_000)
        self.hosted("mock", 50_000, 50_000)
        with self.repo.engine.connect() as connection:
            spend = ledger_spend(connection, self.now)["providers"]
        self.assertEqual(
            spend["vast"],
            {"settled_microusd": 400_000, "open_microusd": 500_000, "references": 2},
        )
        self.assertEqual(spend["lium"]["open_microusd"], 300_000)
        self.assertEqual(spend["modal"]["settled_microusd"], 150_000)
        self.assertEqual(spend["everyframe"]["settled_microusd"], 60_000)
        self.assertNotIn("mock", spend)

    def test_http_balances_carry_ledger_and_refuse_non_admins(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        self.rental("runpod", 100_000, actual=90_000)
        app = create_app(
            Settings(
                Path(self.temp.name) / "money-api",
                auth_mode="local-test",
                database_url=self.url,
            ),
            repository=self.repo,
        )
        with TestClient(app) as client:
            client.post(
                "/api/auth/login", json={"username": "supervan"}
            ).raise_for_status()
            for path in ("/v1/admin/provider-balances", "/v1/admin/revenue?days=30"):
                self.assertEqual(client.get(path).status_code, 403, path)
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            rows = {
                row["provider"]: row
                for row in client.get("/v1/admin/provider-balances").json()["rows"]
            }
            self.assertEqual(rows["runpod"]["ledger"]["settled_microusd"], 90_000)
            self.assertEqual(rows["vast"]["ledger"]["references"], 0)
            self.assertIsNone(rows["openrouter"]["ledger"])
            revenue = client.get("/v1/admin/revenue?days=7")
            self.assertEqual(revenue.status_code, 200)
            self.assertEqual(revenue.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/v1/admin/revenue?days=14").status_code, 422)
