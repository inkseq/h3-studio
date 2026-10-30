"""Public quote, owner isolation and audited pricing/credit APIs; offline SQL."""

from concurrent.futures import ThreadPoolExecutor
import copy
from decimal import Decimal
from fractions import Fraction
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from sqlalchemy import Column, MetaData, String, Table, func, insert, select, update
from sqlalchemy.engine import Connection

from studio_platform.auth import Auth, Principal
from studio_platform.credit_schema import metadata as credit_metadata, prices
from studio_platform.credits import CreditError
from studio_platform.credits_pricing import (
    PROFILE_CREDIT_MULTIPLIERS,
    DEFAULT_RATES,
    PricingError,
    admin_margin_estimate,
    hide_dollar_estimate,
    public_prices,
    public_quote,
    quote,
    user_estimate,
)
from studio_platform.credits_routes import CreditAPI
from studio_platform.identity_schema import admin_audit, metadata, users
from studio_platform.render_plans import compile_film_export, compile_render
from test_platform_repository import LedgerCase
import test_platform_render_plans as render_fixtures


class CreditsRouteTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.auth = Auth(self.repo.engine, mode="local-test", clock=lambda: self.now)
        self.marker = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        self.ids = {owner: str(uuid.uuid4()) for owner in ("superdan", "supervan")}
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            credit_metadata.create_all(connection)
            self.marker.create(connection)
            connection.execute(insert(self.marker).values(version_num="20261019_0004"))
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

    def count(self, table):
        with self.repo.engine.connect() as connection:
            return connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()

    def publish(self, **extra):
        return self.service.publish(
            self.admin,
            {"expected_revision": None, "rates": dict(DEFAULT_RATES), **extra},
            key="first-price",
        )

    def enable(self):
        revision = self.publish()["revision"]
        self.service.policy(
            self.admin,
            {
                "expected_revision": revision,
                "enabled": True,
                "reason": "Disposable acceptance",
            },
            key="enable",
        )
        return revision

    @staticmethod
    def compiled(**extra):
        return {
            "recipe_id": "h3-base-fl2va-v1",
            "request": {"model": "MiniMax-H3-Pruned-Rank8-INT8", "steps": 20},
            "output_spec": {
                "frames": 124,
                "width": 832,
                "height": 480,
                "actual_duration": 999,
            },
            **extra,
        }

    def get_quote(self, compiled=None, **extra):
        with self.repo.engine.connect() as connection:
            return quote(
                connection, "sixnine", "supervan", compiled or self.compiled(), **extra
            )

    def test_reads_zero_without_creating_or_granting_a_balance(self):
        self.assertEqual(
            self.service.balance(self.user),
            {
                "balance": 0,
                "held": 0,
                "available": 0,
                "today_used": 0,
                "daily_limit": 0,
            },
        )
        self.assertIsNone(self.get_quote())
        self.assertEqual(
            self.service.history(self.user), {"entries": [], "next_cursor": None}
        )

    def test_unadopted_credit_endpoints_fail_closed(self):
        with self.repo.transaction() as connection:
            connection.execute(update(self.marker).values(version_num="20261019_0003"))
        with self.assertRaises(PricingError) as error:
            self.service.balance(self.user)
        self.assertEqual(error.exception.status, 503)

    def test_publish_is_immutable_cas_audited_and_idempotent(self):
        first = self.publish()
        self.assertEqual(self.publish(), first)
        self.assertEqual(self.count(prices), 1)
        self.assertEqual(self.count(admin_audit), 1)
        with self.assertRaisesRegex(PricingError, "idempotency_conflict"):
            self.service.publish(
                self.admin,
                {"expected_revision": None, "rates": {**DEFAULT_RATES, "standard": 5}},
                key="first-price",
            )
        with self.assertRaisesRegex(PricingError, "credit_prices_changed"):
            self.service.publish(
                self.admin, {"expected_revision": None, "rates": dict(DEFAULT_RATES)}
            )
        second = self.service.publish(
            self.admin,
            {
                "expected_revision": first["revision"],
                "rates": {**DEFAULT_RATES, "standard": 5},
                "welcome_credits": 25,
            },
        )
        self.assertNotEqual(second["revision"], first["revision"])
        self.assertEqual(self.count(prices), 2)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(prices.c.rates).where(prices.c.revision == first["revision"])
                ).scalar_one(),
                DEFAULT_RATES,
            )

    def test_racing_publish_uses_one_original_revision(self):
        def publish(_):
            try:
                return self.service.publish(
                    self.admin,
                    {"expected_revision": None, "rates": dict(DEFAULT_RATES)},
                )["revision"]
            except PricingError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=3) as executor:
            values = list(executor.map(publish, range(3)))
        self.assertEqual(values.count("credit_prices_changed"), 2)
        self.assertEqual(self.count(prices), 1)
        self.assertEqual(self.count(admin_audit), 1)

    def test_quote_uses_exact_frames_one_take_and_no_client_tier_discount(self):
        revision = self.enable()
        standard = self.get_quote()
        self.assertEqual(
            (
                standard["credits"],
                standard["frames"],
                standard["fps"],
                standard["takes"],
            ),
            (21, 124, 24, 1),
        )
        self.assertEqual(standard["price_revision"], revision)
        self.assertEqual(standard["output_seconds"], 124 / 24)
        low_steps = self.compiled(
            request={
                "model": "MiniMax-H3-Pruned-Rank8-INT8",
                "steps": 3,
                "tier": "fast",
                "speed_preset": "taomate",
            }
        )
        self.assertEqual(self.get_quote(low_steps)["tier"], "standard")
        large = self.compiled(output_spec={"frames": 124, "width": 1280, "height": 720})
        self.assertEqual(
            (self.get_quote(large)["tier"], self.get_quote(large)["credits"]),
            ("standard", 21),
        )
        unpruned = self.compiled(request={"model": "MiniMax-H3-Base-INT8", "steps": 3})
        self.assertEqual(
            (self.get_quote(unpruned)["tier"], self.get_quote(unpruned)["credits"]),
            ("refine", 62),
        )

    def test_quote_checks_server_profile_identity_not_caller_quality_provenance(self):
        self.enable()
        from studio_platform.quality_tiers import PROFILE_TIERS, billing_tier
        from studio_platform.runtime_catalog import get_profile

        for identity, tier in PROFILE_TIERS.items():
            compiled = self.compiled(
                deployment_profile_id=identity,
                request={"model": get_profile(identity)["model_id"], "tier": "fast"},
                quality_selection={"tier": "fast", "mapping_revision": "caller"},
            )
            frozen = self.get_quote(compiled)
            self.assertEqual(frozen["tier"], billing_tier(tier))
            # The multiplier applies before the single round-up: 4 credits/s x 124/24 s.
            base = Fraction(4 if billing_tier(tier) == "standard" else 12) * Fraction(
                124, 24
            )
            multiplier = PROFILE_CREDIT_MULTIPLIERS.get(identity, 1)
            self.assertEqual(frozen["credits"], math.ceil(base * multiplier))
        for identity, model in (
            ("unregistered-private-profile", "MiniMax-H3-Pruned-Rank8-INT8"),
            (next(iter(PROFILE_TIERS)), "MiniMax-H3-Base-INT8"),
            (None, "unregistered-private-model"),
        ):
            compiled = self.compiled(request={"model": model})
            if identity is not None:
                compiled["deployment_profile_id"] = identity
            with self.assertRaisesRegex(PricingError, "credit_quote_invalid"):
                self.get_quote(compiled)

    def test_profile_multiplier_raises_usage_without_changing_tier(self):
        self.enable()
        from studio_platform.quality_tiers import PROFILE_TIERS
        from studio_platform.runtime_catalog import get_profile

        identity = next(i for i, tier in PROFILE_TIERS.items() if tier == "standard")
        compiled = self.compiled(
            deployment_profile_id=identity,
            request={"model": get_profile(identity)["model_id"]},
        )
        with patch.dict(PROFILE_CREDIT_MULTIPLIERS, {identity: Fraction(13, 10)}):
            frozen = self.get_quote(compiled)
        # 124 frames at the standard rate is 20.67 credits (21 billed); x1.3 is
        # 26.87, rounded up once to 27.
        self.assertEqual((frozen["tier"], frozen["credits"]), ("standard", 27))
        self.assertEqual(frozen["breakdown"][0]["rate"], 4)
        self.assertNotIn("multiplier", json.dumps(frozen))

    def render_compiled(self):
        compiled, _, blockers, _ = compile_render(
            render_fixtures.BODY, render_fixtures.story(), render_fixtures.source
        )
        self.assertFalse(blockers)
        return compiled

    def test_real_chapter_compiler_quotes_zero_with_credits_enabled(self):
        revision = self.enable()
        compiled = self.render_compiled()
        original = copy.deepcopy(compiled)
        self.assertNotIn("frames", compiled["output_spec"])
        value = self.get_quote(compiled, execution={"backend": "cpu-render"})
        self.assertEqual(
            (value["tier"], value["credits"], value["frames"], value["fps"]),
            ("render", 0, 48, 24),
        )
        self.assertTrue(value["enforced"])
        self.assertEqual(value["price_revision"], revision)
        self.assertEqual(value["output_seconds"], 2)
        self.assertEqual(compiled, original)

    def test_real_film_export_compiler_quotes_all_chapter_frames_at_zero(self):
        revision = self.enable()
        chapter = self.render_compiled()
        receipt = {
            "chapter_id": "chapter-one",
            "video": next(iter(chapter["sources"].values())),
            "audio": None,
            "output_spec": chapter["output_spec"],
        }
        compiled = compile_film_export(
            "story-one", "film-one", [receipt, {**receipt, "chapter_id": "chapter-two"}]
        )
        self.assertEqual(compiled["request"]["render"]["version"], 4)
        self.assertNotIn("frames", compiled["output_spec"])
        value = self.get_quote(compiled, execution={"backend": "cpu-render"})
        self.assertEqual(
            (value["tier"], value["credits"], value["frames"], value["output_seconds"]),
            ("render", 0, 96, 4),
        )
        self.assertEqual(value["price_revision"], revision)
        self.assertEqual(public_quote({"credit_quote": value})["credits"], 0)

    def test_generation_cannot_borrow_render_duration_or_free_backend(self):
        self.enable()
        self.assertGreater(
            self.get_quote(execution={"backend": "cpu-render"})["credits"], 0
        )
        render = self.render_compiled()
        for compiled, execution in (
            (render, None),
            (render, {"backend": "other"}),
            (self.compiled(recipe_id="chapter-roughcut-v1"), {"backend": "cpu-render"}),
            (
                self.compiled(output_spec={**render["output_spec"]}),
                {"backend": "cpu-render"},
            ),
        ):
            with self.subTest(recipe=compiled["recipe_id"], execution=execution):
                with self.assertRaisesRegex(PricingError, "credit_quote_invalid"):
                    self.get_quote(compiled, execution=execution)

    def test_quotes_reject_conflicting_duration_fields_for_render_and_generation(self):
        self.enable()
        for compiled, execution, other_key in (
            (self.render_compiled(), {"backend": "cpu-render"}, "frames"),
            (self.compiled(), None, "frame_count"),
        ):
            for other in (1, True, "124", None):
                with self.subTest(recipe=compiled["recipe_id"], other=other):
                    changed = copy.deepcopy(compiled)
                    changed["output_spec"][other_key] = other
                    with self.assertRaisesRegex(PricingError, "credit_quote_invalid"):
                        self.get_quote(changed, execution=execution)

    def test_quotes_reject_invalid_canonical_frames_and_fps(self):
        self.enable()
        for compiled, execution, frame_key in (
            (self.render_compiled(), {"backend": "cpu-render"}, "frame_count"),
            (self.compiled(), None, "frames"),
        ):
            for field, values in (
                (frame_key, (None, True, False, 0, -1, 1.5, "48", 10_000_001)),
                ("fps", (None, True, 0, -1, 1.5, "24", 241)),
            ):
                for invalid in values:
                    with self.subTest(
                        recipe=compiled["recipe_id"], field=field, value=invalid
                    ):
                        changed = copy.deepcopy(compiled)
                        changed["output_spec"][field] = invalid
                        with self.assertRaisesRegex(
                            PricingError, "credit_quote_invalid"
                        ):
                            self.get_quote(changed, execution=execution)

    def test_resolution_revision_uses_compiled_dimensions_and_preserves_old_quote(self):
        revision = self.enable()
        compiled = self.compiled()
        compiled['output_spec'].update(frames=120, fps=24, width=1344, height=768)
        compiled['request'].update(resolution='480P', quality_tier='fast', steps=5)
        compiled['deployment_profile_id'] = (
            'h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7'
        )
        frozen = self.get_quote(compiled)
        self.assertEqual(frozen['credits'], 20)
        body = {
            'expected_revision': revision,
            'rates': dict(DEFAULT_RATES),
            'resolution_multipliers': {'480P': 1, '768P': 2},
            'image_credits': 5,
            'reason': 'Disposable CR75 acceptance',
        }
        published = self.service.publish(self.admin, body, key='resolution-prices')
        self.assertNotEqual(published['revision'], revision)
        self.assertEqual(
            self.service.publish(self.admin, body, key='resolution-prices'), published
        )
        sharp = self.get_quote(compiled)
        self.assertEqual(
            (sharp['credits'], sharp['resolution'], sharp['breakdown'][0]['rate']),
            (40, '768P', 8),
        )
        compiled['output_spec'].update(width=768, height=1344)
        self.assertEqual(self.get_quote(compiled)['credits'], 40)
        compiled['output_spec'].update(width=480, height=832)
        self.assertEqual(self.get_quote(compiled)['credits'], 20)
        self.assertEqual(public_quote({'credit_quote': frozen})['credits'], 20)
        self.assertNotIn('resolution_multiplier', frozen)
        with self.repo.engine.connect() as connection:
            old = connection.execute(
                select(prices.c.configuration).where(prices.c.revision == revision)
            ).scalar_one()
            new = connection.execute(
                select(prices.c.configuration).where(
                    prices.c.revision == published['revision']
                )
            ).scalar_one()
        self.assertNotIn('resolution_multipliers', old)
        self.assertEqual(new['image_credits'], 5)
        self.assertEqual(new['resolution_multipliers'], {'480P': 1, '768P': 2})
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                public_prices(connection, "sixnine")["resolution_multipliers"],
                {"480P": 1, "768P": 2},
            )
        compiled['output_spec'].update(frames=124, width=1344, height=768)
        self.assertEqual(self.get_quote(compiled)['credits'], 42)
        with self.assertRaisesRegex(PricingError, 'credit_prices_changed'):
            self.service.publish(self.admin, body, key='stale-resolution-revision')

    def test_resolution_pricing_rejects_invalid_adoption_and_unpriced_dimensions(self):
        revision = self.enable()
        for invalid in (
            {'480P': True, '768P': 2},
            {'480P': 1, '768P': 0},
            {'480P': 1},
            {'480P': 1, '768P': 2, '1080P': 3},
            {'480P': 2, '768P': 1},
            {'480P': 1, '768P': 1.5},
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(PricingError, 'credit_prices_invalid'),
            ):
                self.service.publish(
                    self.admin,
                    {
                        'expected_revision': revision,
                        'rates': dict(DEFAULT_RATES),
                        'resolution_multipliers': invalid,
                    },
                )
        self.service.publish(
            self.admin,
            {
                'expected_revision': revision,
                'rates': dict(DEFAULT_RATES),
                'resolution_multipliers': {'480P': 1, '768P': 2},
            },
        )
        for width, height in ((1920, 1080), (None, 480), (True, 480), (0, 768)):
            compiled = self.compiled()
            compiled['output_spec'].update(width=width, height=height)
            with (
                self.subTest(width=width, height=height),
                self.assertRaisesRegex(PricingError, 'credit_quote_invalid'),
            ):
                self.get_quote(compiled)
        rendered = self.render_compiled()
        quote = self.get_quote(rendered, execution={"backend": "cpu-render"})
        self.assertEqual((quote["tier"], quote["credits"]), ("render", 0))
        self.assertNotIn("resolution_multiplier", quote)

    def test_new_price_does_not_reprice_frozen_plan_and_projection_is_read_only(self):
        revision = self.enable()
        frozen = self.get_quote()
        self.service.publish(
            self.admin,
            {"expected_revision": revision, "rates": {**DEFAULT_RATES, "standard": 10}},
        )
        self.assertEqual(self.get_quote()["credits"], 52)
        self.assertEqual(frozen["credits"], 21)
        projection = public_quote({"credit_quote": frozen})
        self.assertEqual(
            set(projection), {"credits", "price_revision", "breakdown", "admission"}
        )
        self.assertNotIn("user_id", projection)
        self.assertTrue(hide_dollar_estimate({"credit_quote": frozen}, self.user))
        self.assertFalse(hide_dollar_estimate({"credit_quote": frozen}, self.admin))
        self.assertFalse(hide_dollar_estimate({}, self.user))
        self.assertEqual(
            user_estimate(
                {"cost_microusd": 500, "estimated_seconds": 45}, hide_dollars=True
            ),
            {"estimated_seconds": 45},
        )

    def test_pricing_basis_keeps_unmeasured_provider_cost_null(self):
        self.publish()
        value = self.service.admin_prices(self.admin)
        self.assertEqual(value["cost_multiplier"], 3)
        self.assertEqual(value["fee_rate"], 0.07)
        self.assertEqual(value["basis"]["standard"]["seconds_per_5s_clip"], 87)
        self.assertIsNone(value["basis"]["standard"]["hourly_usd"])
        self.assertIsNone(value["report_30d"]["provider_cost_usd"])

    def test_admin_prices_http_renders_exact_postgres_numeric_capture_totals(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.credit_schema import entries
        from studio_platform.settings import Settings

        self.publish()
        app = create_app(
            Settings(
                Path(self.temp.name) / "numeric-api",
                auth_mode="local-test",
                database_url=self.url,
            ),
            repository=self.repo,
        )
        original_execute = Connection.execute
        aggregates = []

        def postgres_sum(connection, statement, *args, **kwargs):
            result = original_execute(connection, statement, *args, **kwargs)
            if "sum(" in str(statement).lower():
                # Keep the real aggregate query on SQLite or the isolated PG
                # harness; expose psycopg's SUM(BIGINT) numeric result on both.
                value = Decimal(result.scalar_one())
                aggregates.append(value)
                return SimpleNamespace(scalar_one=lambda: value)
            return result

        with TestClient(app) as client:
            client.post(
                "/api/auth/login", json={"username": self.admin.owner}
            ).raise_for_status()
            for captured in (0, (1 << 53) + 1):
                with self.subTest(captured=captured):
                    if captured:
                        with self.repo.transaction() as connection:
                            connection.execute(
                                insert(entries).values(
                                    id=str(uuid.uuid4()),
                                    tenant=self.settings.tenant_id,
                                    user_id=self.ids[self.user.owner],
                                    kind="capture",
                                    amount=-captured,
                                    held_delta=-captured,
                                    balance_after=0,
                                    held_after=0,
                                    reference_type="job",
                                    reference_id="numeric-report-fixture",
                                    reason="Synthetic captured-credit report fixture",
                                    actor=self.user.actor_id,
                                    idempotency_key="numeric-report-fixture",
                                    fingerprint="a" * 64,
                                    created_at=self.now,
                                )
                            )
                    before = [
                        self.count(table) for table in (entries, prices, admin_audit)
                    ]
                    with patch.object(Connection, "execute", new=postgres_sum):
                        response = client.get("/v1/admin/credits/prices")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    report = response.json()["report_30d"]
                    self.assertIs(type(report["captured_credits"]), int)
                    self.assertEqual(report["captured_credits"], captured)
                    self.assertIsNone(report["provider_cost_usd"])
                    self.assertEqual(
                        [self.count(table) for table in (entries, prices, admin_audit)],
                        before,
                    )
        self.assertEqual(aggregates, [Decimal(0), Decimal((1 << 53) + 1)])

    def test_margin_uses_actual_catalog_tax_inside_price_and_bonus_only_once(self):
        from studio_platform.payments_routes import PACK_SCOPE
        from studio_platform.repository import documents

        self.publish()
        packs = [
            {
                "id": "starter",
                "price_id": "price_starter",
                "amount_cents": 500,
                "credits": 500,
                "bonus_percent": 0,
                "currency": "usd",
            },
            {
                "id": "pro",
                "price_id": "price_pro",
                "amount_cents": 10000,
                "credits": 11500,
                "bonus_percent": 15,
                "currency": "usd",
            },
        ]
        with self.repo.transaction() as connection:
            # A conflicting other tenant must not become this tenant's price.
            for tenant, current_packs in (
                ("sixnine", packs),
                ("another-tenant", [dict(packs[0], credits=999999)]),
            ):
                connection.execute(
                    insert(documents).values(
                        tenant_id=tenant,
                        **PACK_SCOPE,
                        version=1,
                        updated_at=self.now,
                        payload={"revision": "catalog-one", "packs": current_packs},
                    )
                )
        before = {
            table.name: self.count(table) for table in (documents, prices, admin_audit)
        }
        value = self.service.admin_prices(self.admin)
        margin = value["margin_estimate"]
        self.assertEqual(margin["pack_revision"], "catalog-one")
        self.assertEqual(margin["worst_case_pack_id"], "pro")
        # Tax is added at Checkout: $100 - $7 - $0.20 over all 11,500 credits.
        self.assertAlmostEqual(margin["worst_case_net_usd_per_credit"], 92.8 / 11500)
        self.assertEqual(margin["assumptions"]["processing_and_mor_fee_rate"], 0.07)
        self.assertFalse(margin["assumptions"]["tax_inclusive"])
        self.assertEqual(margin["assumptions"]["consumer_tax_rate"], 0)
        self.assertTrue(margin["assumptions"]["pack_credits_include_bonus"])
        refine = margin["tiers"]["refine"]
        self.assertAlmostEqual(
            refine["estimated_net_usd_per_5s_clip"], 60 * 92.8 / 11500
        )
        self.assertEqual(refine["status"], "gpu_cost_unmeasured")
        self.assertIsNone(refine["estimated_margin"])
        self.assertIsNone(refine["below_30_percent"])
        self.assertGreater(refine["max_gpu_hourly_usd_for_30_percent"], 0)
        self.assertNotIn("price_pro", str(margin))
        self.assertEqual(
            before,
            {
                table.name: self.count(table)
                for table in (documents, prices, admin_audit)
            },
        )
        with self.repo.engine.connect() as connection:
            from studio_platform.credits_pricing import public_prices

            self.assertEqual(
                set(public_prices(connection, "sixnine")), {"revision", "rates"}
            )

    def test_margin_does_not_make_up_packs_or_gpu_cost_or_rewrite_old_prices(self):
        self.publish()
        value = self.service.admin_prices(self.admin)
        self.assertEqual(value["margin_estimate"]["packs"], [])
        self.assertIsNone(value["margin_estimate"]["worst_case_net_usd_per_credit"])
        for tier in value["margin_estimate"]["tiers"].values():
            self.assertEqual(tier["status"], "catalog_unconfigured")
            self.assertIsNone(tier["estimated_margin"])
        with self.repo.engine.connect() as connection:
            saved = connection.execute(select(prices.c.configuration)).scalar_one()
        self.assertNotIn("margin_estimate", saved)
        self.assertEqual(saved["fee_rate"], 0.07)

    def test_margin_estimate_flags_measured_cost_risk_and_higher_fees(self):
        import copy
        from studio_platform.credits_pricing import DEFAULT_CONFIGURATION

        config = copy.deepcopy(DEFAULT_CONFIGURATION)
        config["basis"]["refine"]["hourly_usd"] = 2
        price = {"rates": dict(DEFAULT_RATES), "configuration": config}
        catalog = {
            "revision": "fixture",
            "packs": [
                {
                    "id": "pro",
                    "credits": 11500,
                    "amount_cents": 10000,
                    "bonus_percent": 15,
                }
            ],
        }
        before = copy.deepcopy((price, catalog))
        low = admin_margin_estimate(price, catalog)
        self.assertTrue(low["tiers"]["refine"]["below_30_percent"])
        self.assertEqual(low["tiers"]["refine"]["status"], "estimated")
        self.assertEqual((price, catalog), before)
        config["fee_rate"] = 0.10
        higher_fee = admin_margin_estimate(price, catalog)
        self.assertLess(
            higher_fee["worst_case_net_usd_per_credit"],
            low["worst_case_net_usd_per_credit"],
        )
        # Metadata never double-discounts the already-total pack credit count.
        catalog["packs"][0]["bonus_percent"] = 0
        self.assertEqual(
            admin_margin_estimate(price, catalog)["worst_case_net_usd_per_credit"],
            higher_fee["worst_case_net_usd_per_credit"],
        )

    def test_credited_job_result_hides_provider_currency_without_changing_history(self):
        from studio_platform.generation_services import GenerationRead

        self.enable()
        frozen = self.get_quote()
        result = {
            'actual_cost_microusd': 500,
            'rental_allocation': {
                'hourly_cost_microusd': 2000,
                'machine_id': 'test-host',
            },
            'generation_seconds': 12,
        }
        job = {
            'id': 'owned-job',
            'status': 'succeeded',
            'owner_id': self.user.owner,
            'project_id': 'story',
            'plan_id': 'original-plan',
            'request': {},
            'execution_plan': {'credit_quote': frozen},
            'result': result,
        }
        read = GenerationRead(
            self.repo, SimpleNamespace(tenant_id=self.settings.tenant_id)
        )
        with patch.object(
            self.repo.engine, 'connect', side_effect=AssertionError('No DTO SQL')
        ):
            ordinary = read.public_job(
                job, artifact_records=[], cancellation={}, principal=self.user
            )
            admin = read.public_job(
                job, artifact_records=[], cancellation={}, principal=self.admin
            )
            legacy = read.public_job(
                {**job, 'execution_plan': {}}, artifact_records=[], cancellation={}
            )
        self.assertEqual(
            ordinary['result'],
            {
                'rental_allocation': {'machine_id': 'test-host'},
                'generation_seconds': 12,
            },
        )
        self.assertEqual(admin['result'], result)
        self.assertEqual(legacy['result'], result)
        self.assertEqual(job['result']['actual_cost_microusd'], 500)

    def test_storyboard_and_film_sum_actual_quotes_without_dollar_or_database_reads(
        self,
    ):
        from studio_platform.film_runs import FilmRuns
        from studio_platform.storyboard_versions import StoryboardVersions

        self.enable()
        frozen = self.get_quote()
        plans = [
            {
                **public_quote({"credit_quote": frozen}),
                "status": "ready",
                "estimate": {"cost_microusd": 500},
            }
            for _ in range(3)
        ]
        receipt = {
            "id": "preflight",
            "project_id": "story",
            "project_version": 1,
            "fingerprint": "a" * 64,
            "items": [{"plan": plan, "machine_seconds": 90} for plan in plans],
        }
        # Pure DTO helpers must never turn a long list into per-job SQL reads.
        with patch.object(
            self.repo.engine,
            "connect",
            side_effect=AssertionError("No DTO database reads"),
        ):
            user = StoryboardVersions._public_preflight(receipt, self.user)
            admin = StoryboardVersions._public_preflight(receipt, self.admin)
            self.assertEqual(user["credits"], 63)
            self.assertNotIn("cost_microusd", user["estimate"])
            self.assertTrue(
                all(
                    "cost_microusd" not in item["plan"]["estimate"]
                    for item in user["items"]
                )
            )
            self.assertEqual(admin["estimate"]["cost_microusd"], 1500)
            film = {
                "generation": [
                    {"shot_id": str(n), **plan} for n, plan in enumerate(plans)
                ],
                "chapters": [],
                "export": {"artifact_id": None},
                "results": {},
                "counts": {},
                "estimate": {"cost_microusd": 1500, "final_bill": False},
            }
            public_film = FilmRuns.public(film, self.user)
            self.assertEqual(public_film["credits"], 63)
            self.assertNotIn("cost_microusd", public_film["estimate"])
            self.assertEqual(
                FilmRuns.public(film, self.admin)["estimate"]["cost_microusd"], 1500
            )
        receipt["items"].append({"plan": None})
        self.assertIsNone(
            StoryboardVersions._public_preflight(receipt, self.user)["credits"]
        )

    def test_real_generation_plan_hash_freezes_quote_before_price_change(self):
        from studio_platform.assets import AssetService
        from studio_platform.execution_policy import ExecutionPolicies
        from studio_platform.generation_services import (
            GenerationAccess,
            GenerationPlanning,
            GenerationRead,
        )
        from studio_platform.repository import request_hash
        from studio_platform.settings import Settings
        from studio_platform.storage import LocalObjectStore

        revision = self.enable()
        settings = Settings(
            Path(self.temp.name),
            generation_enabled=True,
            execution_backend="mock",
            database_url=self.url,
        )
        access = GenerationAccess(self.repo, "sixnine")
        read = GenerationRead(self.repo, access)
        planning = GenerationPlanning(
            repo=self.repo,
            assets=AssetService(
                self.repo.engine,
                LocalObjectStore(Path(self.temp.name) / "objects"),
                Path(self.temp.name),
            ),
            settings=settings,
            policies=ExecutionPolicies(settings, self.repo),
            access=access,
            read=read,
        )
        project = {
            "id": "credit-story",
            "links": [],
            "entities": [
                {
                    "id": "shot-one",
                    "type": "shot",
                    "version": 1,
                    "title": "Shot",
                    "description": "Offline fixture",
                    "data": {},
                }
            ],
        }
        body = {
            "recipe_id": "h3-base-fl2va-v1",
            "prompt": "Offline quote fixture",
            "client_ref": {
                "project_id": project["id"],
                "shot_id": "shot-one",
                "shot_version": 1,
            },
            "controls": {"duration": 5, "resolution": "480P", "seed": "42"},
        }
        result = planning.create(self.user, body, project)
        saved = self.repo.get_plan(
            access.scope(self.user, project["id"]), result["plan_id"]
        )
        self.assertEqual(
            saved["execution_plan"]["credit_quote"]["price_revision"], revision
        )
        self.assertEqual(saved["plan_hash"], request_hash(saved["execution_plan"]))
        self.assertNotIn("cost_microusd", result["estimate"])
        self.service.publish(
            self.admin,
            {"expected_revision": revision, "rates": {**DEFAULT_RATES, "refine": 30}},
        )
        retained = planning.retained_plan(saved, self.user)
        self.assertEqual(
            (retained["credits"], retained["price_revision"]),
            (result["credits"], revision),
        )
        self.assertNotEqual(
            planning.create(self.user, body, project)["credits"], result["credits"]
        )
        self.assertIn(
            "cost_microusd", planning.retained_plan(saved, self.admin)["estimate"]
        )

    def test_admin_is_fresh_human_canonical_and_tenant_scoped(self):
        for principal in (
            self.user,
            Principal("superdan", "key:admin", machine=True, role="admin"),
        ):
            with self.assertRaisesRegex(PricingError, "admin_required"):
                self.service.admin_prices(principal)
        with self.repo.transaction() as connection:
            connection.execute(
                update(users).where(users.c.owner == "superdan").values(role="user")
            )
        with self.assertRaisesRegex(PricingError, "admin_required"):
            self.publish()
        self.assertEqual(self.count(admin_audit), 0)

    def test_adjust_is_one_entry_one_audit_and_user_visible_reason(self):
        body = {"delta": 100, "reason": "Staging acceptance grant"}
        first = self.service.adjust(self.admin, self.ids["supervan"], body, key="grant")
        self.assertEqual(
            self.service.adjust(self.admin, self.ids["supervan"], body, key="grant"),
            first,
        )
        self.assertEqual(self.service.balance(self.user)["balance"], 100)
        self.assertEqual(self.service.balance(self.admin)["balance"], 0)
        self.assertEqual(self.count(admin_audit), 1)
        history = self.service.history(self.user)["entries"]
        self.assertEqual(len(history), 1)
        self.assertEqual(
            (history[0]["kind"], history[0]["amount"], history[0]["reason"]),
            ("adjust", 100, body["reason"]),
        )
        self.assertIsInstance(history[0]["created_at"], str)
        with self.assertRaisesRegex(PricingError, "idempotency_conflict"):
            self.service.adjust(
                self.admin, self.ids["supervan"], {**body, "delta": 200}, key="grant"
            )

    def test_audit_failure_rolls_back_credit_entry_and_account(self):
        with patch.object(
            self.service, "_audit", side_effect=RuntimeError("synthetic audit failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                self.service.adjust(
                    self.admin,
                    self.ids["supervan"],
                    {"delta": 100, "reason": "Atomic grant"},
                    key="failed",
                )
        self.assertEqual(self.service.balance(self.user)["balance"], 0)
        self.assertEqual(self.service.history(self.user)["entries"], [])

    def test_policy_requires_a_published_price_reason_and_idempotency(self):
        with self.assertRaisesRegex(PricingError, "credit_prices_unavailable"):
            self.service.policy(
                self.admin,
                {"expected_revision": None, "enabled": True, "reason": "No price yet"},
                key="empty",
            )
        revision = self.publish()["revision"]
        body = {
            "expected_revision": revision,
            "enabled": True,
            "reason": "Activate staging",
        }
        with self.assertRaisesRegex(PricingError, "idempotency_key_required"):
            self.service.policy(self.admin, body)
        value = self.service.policy(self.admin, body, key="activate")
        self.assertEqual(self.service.policy(self.admin, body, key="activate"), value)
        self.assertEqual(value, {"active_revision": revision, "enabled": True})
        self.assertEqual(self.count(admin_audit), 2)

    def test_https_public_prices_account_and_exact_402_error_contract(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        self.publish()
        app = create_app(
            Settings(
                Path(self.temp.name) / "api",
                auth_mode="local-test",
                database_url=self.url,
            ),
            repository=self.repo,
        )

        @app.get("/v1/disposable-credit-refusal")
        def refusal():
            raise CreditError(
                "insufficient_credits",
                402,
                detail="Not enough credits.",
                need=21,
                have=0,
            )

        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/credits/prices").status_code, 200)
            self.assertEqual(client.get("/v1/credits").status_code, 401)
            client.post(
                "/api/auth/login", json={"username": "supervan"}
            ).raise_for_status()
            result = client.get("/v1/credits")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(
                set(result.json()),
                {"balance", "held", "available", "today_used", "daily_limit"},
            )
            self.assertEqual(client.get("/v1/admin/credits/prices").status_code, 403)
            refused = client.get("/v1/disposable-credit-refusal")
            self.assertEqual(refused.status_code, 402)
            self.assertEqual(
                refused.json(),
                {
                    "code": "insufficient_credits",
                    "detail": "Not enough credits.",
                    "need": 21,
                    "have": 0,
                },
            )
