"""Image charges: original receipts, owner isolation and one terminal outcome."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from unittest.mock import patch
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import Column, MetaData, String, Table, func, insert, select, update

from studio_platform import credits, credits_pricing, service_credits as service
from studio_platform.assets import asset_table
from studio_platform.credit_schema import accounts, entries, metadata, prices
from studio_platform.guided import receipts as edit_receipts
from studio_platform.identity_schema import users, admin_audit
from studio_platform.repository import Scope, documents, spending_settings
from test_platform_repository import LedgerCase


class ServiceCreditTests(LedgerCase):
    def setUp(self):
        super().setUp()
        users.create(self.repo.engine)
        admin_audit.create(self.repo.engine)
        metadata.create_all(self.repo.engine)
        edit_receipts.create(self.repo.engine)
        version = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        version.create(self.repo.engine)
        self.user = str(uuid.uuid4())
        self.reference = "story_chat_image:request_1234"
        self.binding = dict(
            tenant=self.scope.tenant_id,
            owner=self.scope.owner_id,
            project_id=self.scope.project_id,
            card_id="character-one",
            request_id="request_1234",
            input_hash="a" * 64,
            actor_id=self.scope.actor_id,
        )
        self.project = {
            "entities": [
                {"id": "character-one", "type": "character", "data": {"looks": []}}
            ],
            "journey": {"storyChat": {"images": {}}},
        }
        with self.repo.transaction() as conn:
            conn.execute(insert(version).values(version_num="20261019_0004"))
            conn.execute(
                insert(users).values(
                    id=self.user,
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    role="user",
                    status="active",
                    display_name="QA",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            conn.execute(
                insert(documents).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=self.scope.owner_id,
                    project_id="__projects",
                    kind="project",
                    document_id=self.scope.project_id,
                    payload=self.project,
                    version=1,
                    updated_at=self.now,
                )
            )
            credits.grant(
                conn,
                self.scope.tenant_id,
                self.scope.owner_id,
                100,
                actor="browser:admin",
                reason="test",
                idempotency_key="seed",
                now=self.now,
            )
            conn.execute(
                insert(prices).values(
                    tenant=self.scope.tenant_id,
                    revision="images-1",
                    rates=credits_pricing.DEFAULT_RATES,
                    configuration={
                        **credits_pricing.DEFAULT_CONFIGURATION,
                        "image_credits": 5,
                    },
                    published_by=self.user,
                    published_at=self.now,
                )
            )
        self.policy(True)

    def policy(self, enabled):
        with self.repo.transaction() as conn:
            where = (
                documents.c.tenant_id == self.scope.tenant_id,
                documents.c.owner_id == credits.POLICY_OWNER,
                documents.c.project_id == credits.POLICY_PROJECT,
                documents.c.kind == credits.POLICY_KIND,
                documents.c.document_id == credits.POLICY_ID,
            )
            value = {"enabled": enabled, "active_revision": "images-1"}
            if conn.execute(select(documents.c.version).where(*where)).first():
                conn.execute(update(documents).where(*where).values(payload=value))
            else:
                conn.execute(
                    insert(documents).values(
                        tenant_id=self.scope.tenant_id,
                        owner_id=credits.POLICY_OWNER,
                        project_id=credits.POLICY_PROJECT,
                        kind=credits.POLICY_KIND,
                        document_id=credits.POLICY_ID,
                        payload=value,
                        version=1,
                        updated_at=self.now,
                    )
                )

    def call(self, operation, **kwargs):
        with self.repo.transaction() as conn:
            return operation(
                conn,
                self.repo,
                kwargs.pop("scope", self.scope),
                self.reference,
                binding=kwargs.pop("binding", self.binding),
                **kwargs,
            )

    def save_project(self):
        with self.repo.transaction() as conn:
            conn.execute(
                update(documents)
                .where(
                    documents.c.owner_id == self.scope.owner_id,
                    documents.c.project_id == "__projects",
                    documents.c.document_id == self.scope.project_id,
                )
                .values(payload=self.project)
            )

    def attach(self, *, kind="character", owner=None, status="ready"):
        receipt = dict(
            card_id=self.binding["card_id"],
            asset_id=hashlib.sha256(self.reference.encode()).hexdigest()[:32],
            entity_id="image-" + self.binding["request_id"],
            at="2026-10-23T10:00:00.000Z",
        )
        card = self.project["entities"][0]
        card["type"] = kind
        if kind == "character":
            card["data"] = {
                "looks": [{"id": "main", "imageIds": [receipt["entity_id"]]}]
            }
        elif kind == "location":
            card["data"] = {
                "versions": [{"id": "main", "imageIds": [receipt["entity_id"]]}]
            }
        else:
            card["data"] = {"imageIds": [receipt["entity_id"]]}
        self.project["entities"].append(
            {
                "id": receipt["entity_id"],
                "type": "image",
                "data": {"cloudAssetId": receipt["asset_id"]},
            }
        )
        self.project["journey"]["storyChat"]["images"][self.binding["request_id"]] = (
            receipt
        )
        record = {
            "id": receipt["asset_id"],
            "kind": "image",
            "status": status,
            "project_id": self.scope.project_id,
            "client_asset_id": "story-chat-image:" + self.binding["request_id"],
        }
        with self.repo.transaction() as conn:
            conn.execute(
                insert(asset_table).values(
                    id=receipt["asset_id"],
                    tenant=self.scope.tenant_id,
                    owner=owner or self.scope.owner_id,
                    project_id=self.scope.project_id,
                    status=status,
                    created=self.now,
                    record=json.dumps(record),
                )
            )
        self.save_project()
        self.save_edit_receipt()
        return receipt

    def save_edit_receipt(self):
        with self.repo.transaction() as conn:
            conn.execute(
                insert(edit_receipts).values(
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    actor=self.scope.actor_id,
                    project_id=self.scope.project_id,
                    key="story-chat-image-" + self.binding["request_id"],
                    fingerprint="e" * 64,
                    created_at=self.now,
                    response={
                        "id": self.scope.project_id,
                        "version": 2,
                        "updated_at": self.now,
                        "project": self.project,
                    },
                )
            )

    def totals(self):
        with self.repo.engine.connect() as conn:
            row = conn.execute(select(accounts)).mappings().one()
            count = conn.execute(select(func.count()).select_from(entries)).scalar_one()
        return row["balance"], row["held"], count

    def test_off_has_no_ledger_write_and_unpriced_on_refuses(self):
        self.policy(False)
        before = self.totals()
        self.assertIsNone(self.call(service.service_hold))
        self.assertIsNone(self.call(service.service_capture))
        self.assertIsNone(self.call(service.service_release, reason="provider_failed"))
        self.assertEqual(self.totals(), before)
        self.policy(True)
        with self.repo.transaction() as conn:
            conn.execute(
                update(prices).values(
                    configuration=credits_pricing.DEFAULT_CONFIGURATION
                )
            )
        with self.assertRaisesRegex(credits.CreditError, "credit_image_price_required"):
            self.call(service.service_hold)
        self.assertEqual(self.totals(), before)

    def test_hold_replays_original_quote_binding_and_actor(self):
        held = self.call(
            service.service_hold, quote={"credits": 5, "price_revision": "images-1"}
        )
        self.assertTrue(held["created"])
        self.assertEqual(held["hold"]["reference_type"], "story_chat_image")
        self.policy(False)
        with patch.object(
            credits_pricing,
            "image_price",
            side_effect=AssertionError("must recover receipt"),
        ):
            replay = self.call(service.service_hold)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["entry"]["id"], held["entry"]["id"])
        self.assertIsNone(replay["attachment"])
        for binding in (
            {**self.binding, "input_hash": "b" * 64},
            {**self.binding, "card_id": "other-card"},
            {**self.binding, "actor_id": "key:other"},
        ):
            with (
                self.subTest(binding=binding),
                self.assertRaisesRegex(
                    credits.CreditError, "credit_idempotency_conflict"
                ),
            ):
                self.call(service.service_hold, binding=binding)
        with self.assertRaisesRegex(credits.CreditError, "credit_idempotency_conflict"):
            self.call(
                service.service_hold, quote={"credits": 6, "price_revision": "images-1"}
            )
        self.assertEqual(self.totals(), (100, 5, 2))

    def test_capture_requires_owned_ready_attached_image_and_keeps_frozen_price(self):
        self.call(service.service_hold)
        with self.assertRaisesRegex(credits.CreditError, "credit_attachment_required"):
            self.call(service.service_capture)
        receipt = self.attach()
        self.policy(False)
        with self.repo.transaction() as conn:
            conn.execute(
                update(prices).values(
                    configuration={
                        **credits_pricing.DEFAULT_CONFIGURATION,
                        "image_credits": 45,
                    }
                )
            )
        self.assertEqual(self.call(service.service_hold)["attachment"], receipt)
        result = self.call(service.service_capture)
        self.assertEqual(result["entry"]["amount"], -5)
        self.assertEqual(result["entry"]["price_revision"], "images-1")
        self.assertEqual(result["entry"]["actor"], self.scope.actor_id)
        self.assertEqual(result["attachment"], receipt)
        self.assertFalse(self.call(service.service_capture)["created"])
        self.assertEqual(
            self.call(service.service_hold)["terminal"]["id"], result["entry"]["id"]
        )
        with self.assertRaisesRegex(credits.CreditError, "credit_terminal_conflict"):
            self.call(service.service_release, reason="provider_failed")
        self.assertEqual(self.totals(), (95, 0, 3))

    def test_release_is_original_terminal_and_refuses_capture(self):
        self.call(service.service_hold)
        self.policy(False)
        released = self.call(
            service.service_release, reason="provider_timeout_possible_output"
        )
        replay = self.call(service.service_release, reason="stale_unattached")
        self.assertFalse(replay["created"])
        self.assertEqual(replay["entry"], released["entry"])
        with self.assertRaisesRegex(credits.CreditError, "credit_terminal_conflict"):
            self.call(service.service_capture)
        self.assertEqual(self.totals(), (100, 0, 3))

    def test_release_refuses_existing_or_unknown_attachment(self):
        self.call(service.service_hold)
        for state in (None, [], {self.binding["request_id"]: None}):
            self.project["journey"]["storyChat"]["images"] = state
            self.save_project()
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(credits.CreditError, "credit_attachment"),
            ):
                self.call(service.service_release, reason="store_failed")
        for reason in ("raw provider error", [], None):
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(
                    credits.CreditError, "credit_release_reason_invalid"
                ),
            ):
                self.call(service.service_release, reason=reason)
        self.assertEqual(self.totals(), (100, 5, 2))

    def test_capture_rejects_foreign_asset_or_missing_card_link(self):
        self.call(service.service_hold)
        self.attach(owner="supervan")
        with self.assertRaisesRegex(
            credits.CreditError, "credit_attachment_outcome_unconfirmed"
        ):
            self.call(service.service_capture)
        with self.repo.transaction() as conn:
            conn.execute(update(asset_table).values(owner=self.scope.owner_id))
        self.project["entities"][0]["data"]["looks"][0]["imageIds"] = []
        self.save_project()
        with self.repo.transaction() as conn:
            conn.execute(
                update(edit_receipts).values(
                    response={
                        "id": self.scope.project_id,
                        "version": 2,
                        "project": self.project,
                    }
                )
            )
        with self.assertRaisesRegex(
            credits.CreditError, "credit_attachment_outcome_unconfirmed"
        ):
            self.call(service.service_capture)
        with self.assertRaisesRegex(credits.CreditError, "credit_attachment_exists"):
            self.call(service.service_release, reason="card_missing")
        self.assertEqual(self.totals(), (100, 5, 2))

    def test_capture_supports_existing_location_and_prop_links(self):
        for kind in ("location", "prop"):
            with self.subTest(kind=kind):
                self.binding["request_id"] = "request_" + kind
                self.reference = "story_chat_image:" + self.binding["request_id"]
                self.call(service.service_hold)
                self.attach(kind=kind)
                self.assertTrue(self.call(service.service_capture)["created"])
        self.assertEqual(self.totals(), (90, 0, 5))

    def test_original_edit_receipt_survives_removed_current_image_and_blocks_release(
        self,
    ):
        self.call(service.service_hold)
        receipt = self.attach()
        self.project["entities"] = []
        self.project["journey"] = {}
        self.save_project()
        with self.assertRaisesRegex(credits.CreditError, "credit_attachment_exists"):
            self.call(service.service_release, reason="stale_unattached")
        self.assertEqual(self.call(service.service_capture)["attachment"], receipt)
        self.assertEqual(self.totals(), (95, 0, 3))

    def test_current_receipt_alone_cannot_capture_without_committed_edit(self):
        self.call(service.service_hold)
        self.attach()
        with self.repo.transaction() as conn:
            conn.execute(update(edit_receipts).values(key="some-unrelated-edit"))
        with self.assertRaisesRegex(
            credits.CreditError, "credit_attachment_outcome_unconfirmed"
        ):
            self.call(service.service_capture)
        with self.assertRaisesRegex(credits.CreditError, "credit_attachment_exists"):
            self.call(service.service_release, reason="store_failed")
        self.assertEqual(self.totals(), (100, 5, 2))

    def test_legacy_usd_limits_require_explicit_credit_review_only_for_new_hold(self):
        self.call(service.service_hold)
        with self.repo.transaction() as conn:
            conn.execute(
                insert(spending_settings).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=self.scope.owner_id,
                    actor_id=self.scope.actor_id,
                    version=1,
                    updated_at=self.now,
                    value={
                        "daily_limit_microusd": 1000,
                        "ask_above_microusd": 0,
                        "connection_limits": {},
                    },
                )
            )
        self.assertFalse(self.call(service.service_hold)["created"])
        self.call(service.service_release, reason="provider_failed")
        self.binding["request_id"] = "request_reviewed"
        self.reference = "story_chat_image:request_reviewed"
        with self.assertRaisesRegex(
            credits.CreditError, "credit_limits_review_required"
        ):
            self.call(service.service_hold)
        with self.repo.transaction() as conn:
            conn.execute(
                update(spending_settings).values(
                    value={
                        "daily_limit_microusd": 1000,
                        "ask_above_microusd": 0,
                        "connection_limits": {},
                        "daily_limit_credits": 0,
                        "ask_above_credits": 0,
                        "connection_credit_limits": {},
                    }
                )
            )
        self.assertTrue(self.call(service.service_hold)["created"])

    def test_binding_requires_exact_scope_and_reference(self):
        for binding in (
            {**self.binding, "owner": "supervan"},
            {**self.binding, "project_id": "elsewhere"},
            {**self.binding, "tenant": "another-tenant"},
            {**self.binding, "request_id": "different"},
            {**self.binding, "prompt": "must never be stored"},
        ):
            with self.subTest(binding=binding), self.assertRaises(credits.CreditError):
                self.call(service.service_hold, binding=binding)
        self.assertEqual(self.totals(), (100, 0, 1))

    def test_lock_order_and_daily_cap_before_account_cas(self):
        order = []
        with (
            patch.object(
                service,
                "lock_account_gate",
                side_effect=lambda *a: order.append("gate"),
            ),
            patch.object(
                service,
                "owner_reference_gate",
                side_effect=lambda *a: order.append("content"),
            ),
            patch.object(
                service, "lock_owner", side_effect=lambda *a: order.append("owner")
            ),
            patch.object(
                service,
                "check_credit_spending",
                side_effect=lambda *a, **k: order.append("limits"),
            ),
            patch.object(credits, "_account", wraps=credits._account) as account,
        ):
            self.call(service.service_hold)
            self.assertEqual(order, ["gate", "content", "owner", "limits"])
            self.assertTrue(account.called)
        with self.repo.transaction() as conn:
            conn.execute(
                insert(spending_settings).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=self.scope.owner_id,
                    actor_id=self.scope.actor_id,
                    version=1,
                    value={
                        "daily_limit_credits": 5,
                        "ask_above_credits": 0,
                        "connection_credit_limits": {},
                    },
                    updated_at=self.now,
                )
            )
        self.binding["request_id"] = "request_second"
        self.reference = "story_chat_image:request_second"
        with self.assertRaisesRegex(credits.CreditError, "user_daily_limit_reached"):
            self.call(service.service_hold)
        self.assertEqual(self.totals(), (100, 5, 2))

    def test_projection_resolves_only_same_reference_type_and_counts_capture_once(self):
        self.call(service.service_hold)
        with self.repo.transaction() as conn:
            row = credits._account(conn, self.scope.tenant_id, self.user, now=self.now)
            credits._change(
                conn,
                row,
                kind="hold",
                amount=0,
                held_delta=7,
                reference_type="job",
                reference_id=self.reference,
                actor=self.scope.actor_id,
                origin=self.scope.actor_id,
                reason="test collision",
                idempotency_key="job-collision",
                now=self.now,
            )
        self.attach()
        self.call(service.service_capture)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                credits.daily_projection(
                    conn, self.scope.tenant_id, self.scope.owner_id, self.now
                ),
                12,
            )
            self.assertEqual(
                credits.daily_projection(
                    conn,
                    self.scope.tenant_id,
                    self.scope.owner_id,
                    self.now,
                    actor_id=self.scope.actor_id,
                ),
                12,
            )

    def test_concurrent_holds_freeze_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: self.call(service.service_hold), range(2))
            )
        self.assertEqual(sorted(r["created"] for r in results), [False, True])
        self.assertEqual(self.totals(), (100, 5, 2))

    def test_distinct_concurrent_requests_respect_owner_and_connection_caps(self):
        scope = Scope(
            self.scope.tenant_id, self.scope.owner_id, self.scope.project_id, "key:test"
        )
        for mode in ("owner", "connection"):
            limits = {
                "daily_limit_credits": 5 if mode == "owner" else 0,
                "ask_above_credits": 0,
                "connection_credit_limits": {scope.actor_id: 5}
                if mode == "connection"
                else {},
            }
            with self.repo.transaction() as conn:
                if mode == "owner":
                    conn.execute(
                        insert(spending_settings).values(
                            tenant_id=scope.tenant_id,
                            owner_id=scope.owner_id,
                            actor_id=scope.actor_id,
                            version=1,
                            value=limits,
                            updated_at=self.now,
                        )
                    )
                else:
                    conn.execute(update(spending_settings).values(value=limits))

            def attempt(index):
                binding = {
                    **self.binding,
                    "actor_id": scope.actor_id,
                    "request_id": f"{mode}_request_{index}",
                }
                reference = "story_chat_image:" + binding["request_id"]
                try:
                    with self.repo.transaction() as conn:
                        result = service.service_hold(
                            conn, self.repo, scope, reference, binding=binding
                        )
                    return result, binding, reference
                except credits.CreditError as error:
                    return error.code, binding, reference

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, range(2)))
            success = [item for item in results if isinstance(item[0], dict)]
            self.assertEqual(len(success), 1)
            self.assertIn(
                ("user" if mode == "owner" else "connection") + "_daily_limit_reached",
                [item[0] for item in results if isinstance(item[0], str)],
            )
            with self.repo.transaction() as conn:
                service.service_release(
                    conn,
                    self.repo,
                    scope,
                    success[0][2],
                    binding=success[0][1],
                    reason="provider_failed",
                )
        self.assertEqual(self.totals(), (100, 0, 5))

    def test_sydney_midnight_carries_mixed_open_holds_but_not_yesterday_capture(self):
        self.now = datetime(
            2026, 10, 22, 23, 59, tzinfo=ZoneInfo("Australia/Sydney")
        ).timestamp()
        self.call(service.service_hold)
        self.attach()
        self.call(service.service_capture)
        with self.repo.transaction() as conn:
            row = credits._account(conn, self.scope.tenant_id, self.user, now=self.now)
            credits._change(
                conn,
                row,
                kind="hold",
                amount=0,
                held_delta=7,
                reference_type="job",
                reference_id="projection-only-job",
                actor=self.scope.actor_id,
                origin=self.scope.actor_id,
                reason="Projection fixture",
                idempotency_key="prior-job-hold",
                now=self.now,
            )
        self.binding["request_id"] = "overnight_image"
        self.reference = "story_chat_image:overnight_image"
        self.call(service.service_hold)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                credits.daily_projection(
                    conn, self.scope.tenant_id, self.scope.owner_id, self.now
                ),
                17,
            )
        self.now += 120
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                credits.daily_projection(
                    conn, self.scope.tenant_id, self.scope.owner_id, self.now
                ),
                12,
            )
        self.call(service.service_release, reason="provider_failed")
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                credits.daily_projection(
                    conn, self.scope.tenant_id, self.scope.owner_id, self.now
                ),
                7,
            )

    def test_capture_refuses_asset_from_a_different_image_request(self):
        self.call(service.service_hold)
        receipt = self.attach()
        with self.repo.transaction() as conn:
            record = json.loads(conn.execute(select(asset_table.c.record)).scalar_one())
            record["client_asset_id"] = "story-chat-image:unrelated_request"
            conn.execute(
                update(asset_table)
                .where(asset_table.c.id == receipt["asset_id"])
                .values(record=json.dumps(record))
            )
        with self.assertRaisesRegex(
            credits.CreditError, "credit_attachment_outcome_unconfirmed"
        ):
            self.call(service.service_capture)
        self.assertEqual(self.totals(), (100, 5, 2))

    def test_capture_release_race_has_exactly_one_terminal(self):
        for attached in (False, True):
            with self.subTest(attached=attached):
                self.binding["request_id"] = (
                    "race_attached" if attached else "race_unattached"
                )
                self.reference = "story_chat_image:" + self.binding["request_id"]
                self.call(service.service_hold)
                if attached:
                    self.attach()

                def run(operation):
                    try:
                        return self.call(
                            operation,
                            **(
                                {"reason": "provider_failed"}
                                if operation == service.service_release
                                else {}
                            ),
                        )
                    except credits.CreditError as error:
                        return error.code

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(
                        pool.map(
                            run, [service.service_capture, service.service_release]
                        )
                    )
                success = [r for r in results if isinstance(r, dict)]
                self.assertEqual(len(success), 1)
                self.assertEqual(
                    success[0]["entry"]["kind"], "capture" if attached else "release"
                )
        self.assertEqual(self.totals(), (95, 0, 5))
