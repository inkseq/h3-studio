"""Voice settlement with real temporary ledger rows and immutable fake audio edits."""

import copy
import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import insert, select, update

from studio_platform import (
    credits,
    service_credits as service,
    speech_credit_proof as proof,
)
from studio_platform.assets import asset_table
from studio_platform.credit_schema import entries
from studio_platform.guided import receipts
from studio_platform.repository import Scope, request_hash
import test_service_credit_core as fixtures


class SpeechCreditTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceCreditTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)
        self.repo, self.scope, self.now = self.f.repo, self.f.scope, self.f.now
        self.speech = dict(
            voice_id="v-warm",
            voice_revision="r1",
            direction="calm",
            language="zh",
            text_sha256=hashlib.sha256("a".encode()).hexdigest(),
            characters=1,
        )
        self.bind(characters=1)

    def bind(self, *, characters, request_id="speech_request_1234"):
        self.speech = {**self.speech, "characters": characters}
        self.reference = "speech:" + request_id
        self.binding = dict(
            tenant=self.scope.tenant_id,
            owner=self.scope.owner_id,
            project_id=self.scope.project_id,
            actor_id=self.scope.actor_id,
            card_id="speech-" + request_id,
            request_id=request_id,
            input_hash=request_hash(self.speech),
            speech=dict(self.speech),
        )

    def call(self, operation, **kwargs):
        with self.repo.transaction() as connection:
            return operation(
                connection,
                self.repo,
                kwargs.pop("scope", self.scope),
                kwargs.pop("reference_id", self.reference),
                binding=kwargs.pop("binding", self.binding),
                service="speech",
                **kwargs,
            )

    def attach(self):
        asset_id = hashlib.sha256(self.reference.encode()).hexdigest()[:32]
        self.attached = dict(
            entity_id=self.binding["card_id"],
            asset_id=asset_id,
            content_path=f"/v1/assets/{asset_id}/content",
            voice_id=self.speech["voice_id"],
        )
        self.entity = {
            "id": self.binding["card_id"],
            "type": "audio",
            "data": {
                "cloudAssetId": asset_id,
                "cloudContentPath": self.attached["content_path"],
                "speech": {
                    **self.speech,
                    "request_id": self.binding["request_id"],
                    "input_hash": self.binding["input_hash"],
                    "provider_characters": 510,
                },
            },
        }
        self.f.project["entities"].append(copy.deepcopy(self.entity))
        self.f.save_project()
        self.response = {
            "id": self.scope.project_id,
            "version": 2,
            "project": copy.deepcopy(self.f.project),
        }
        self.asset_record = {
            "id": asset_id,
            "kind": "audio",
            "status": "ready",
            "project_id": self.scope.project_id,
            "client_asset_id": self.reference,
        }
        with self.repo.transaction() as connection:
            connection.execute(
                insert(asset_table).values(
                    id=asset_id,
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    project_id=self.scope.project_id,
                    status="ready",
                    created=self.now,
                    record=json.dumps(self.asset_record),
                )
            )
            connection.execute(
                insert(receipts).values(
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    actor=self.scope.actor_id,
                    project_id=self.scope.project_id,
                    key="speech-v1:attach:" + self.binding["request_id"],
                    fingerprint="e" * 64,
                    created_at=self.now,
                    response=self.response,
                )
            )

    def change_receipt(self, change):
        value = copy.deepcopy(self.response)
        change(value)
        with self.repo.transaction() as connection:
            connection.execute(
                update(receipts)
                .where(
                    receipts.c.key == "speech-v1:attach:" + self.binding["request_id"]
                )
                .values(response=value)
            )

    def test_quote_boundaries_exclude_provider_metadata(self):
        for characters, expected in ((1, 2), (100, 2), (101, 4), (499, 10), (500, 10)):
            with self.subTest(characters=characters):
                self.assertEqual(
                    proof.quote(characters),
                    {"credits": expected, "price_revision": "speech-characters-v1"},
                )
        for characters in (0, 501, True, "100", 1.5):
            with self.assertRaises(credits.CreditError):
                proof.quote(characters)
        self.bind(characters=500)
        hold = self.call(service.service_hold)
        self.assertEqual(hold["hold"]["held_delta"], 10)
        self.attach()
        self.assertEqual(
            self.call(service.service_capture)["attachment"], self.attached
        )
        self.assertEqual(self.f.totals()[:2], (90, 0))

    def test_success_replay_and_policy_change_keep_original_quote(self):
        first = self.call(service.service_hold)
        self.assertEqual(first["entry"]["reference_type"], "speech")
        self.assertEqual(
            first["entry"]["fingerprint"],
            request_hash({"binding": self.binding, "quote": proof.quote(1)}),
        )
        self.f.policy(False)
        self.assertFalse(self.call(service.service_hold)["created"])
        self.attach()
        self.assertTrue(self.call(service.service_capture)["created"])
        self.assertFalse(self.call(service.service_capture)["created"])
        self.assertEqual(self.f.totals()[:2], (98, 0))

    def test_off_never_writes_and_release_blocks_later_capture(self):
        self.f.policy(False)
        before = self.f.totals()
        self.assertIsNone(self.call(service.service_hold))
        self.assertEqual(self.f.totals(), before)
        self.f.policy(True)
        self.call(service.service_hold)
        self.assertTrue(
            self.call(service.service_release, reason="provider_failed")["created"]
        )
        self.assertFalse(
            self.call(service.service_release, reason="provider_failed")["created"]
        )
        self.attach()
        with self.assertRaisesRegex(credits.CreditError, "credit_terminal_conflict"):
            self.call(service.service_capture)
        self.assertEqual(self.f.totals()[:2], (100, 0))

    def test_strict_binding_and_scope_reject_before_hold(self):
        bad = []
        for field, value in (
            ("characters", True),
            ("characters", 501),
            ("language", "fr"),
            ("direction", "unknown"),
            ("voice_id", "../escape"),
            ("text_sha256", "bad"),
            ("provider", "bad provider"),
        ):
            changed = copy.deepcopy(self.binding)
            changed["speech"][field] = value
            changed["input_hash"] = request_hash(changed["speech"])
            bad.append(changed)
        for field, value in (
            ("card_id", "unrelated"),
            ("request_id", "short"),
            ("input_hash", "b" * 64),
            ("speech", None),
        ):
            bad.append({**self.binding, field: value})
        bad.append({**self.binding, "extra": "unreviewed"})
        for binding in bad:
            with self.subTest(binding=binding), self.assertRaises(credits.CreditError):
                self.call(service.service_hold, binding=binding)
        with self.assertRaisesRegex(credits.CreditError, "credit_idempotency_conflict"):
            self.call(
                service.service_hold,
                scope=Scope(self.scope.tenant_id, "other", self.scope.project_id),
            )
        self.assertEqual(self.f.totals()[:2], (100, 0))

    def test_changed_request_or_quote_cannot_replay_hold(self):
        self.call(service.service_hold)
        changed = copy.deepcopy(self.binding)
        changed["speech"]["direction"] = "happy"
        changed["input_hash"] = request_hash(changed["speech"])
        with self.assertRaisesRegex(credits.CreditError, "credit_idempotency_conflict"):
            self.call(service.service_hold, binding=changed)
        with self.assertRaisesRegex(credits.CreditError, "credit_idempotency_conflict"):
            self.call(
                service.service_hold,
                quote={"credits": 4, "price_revision": proof.PRICE_REVISION},
            )
        self.assertEqual(self.f.totals()[:2], (100, 2))

    def test_immutable_attachment_survives_mutable_project_edit(self):
        self.call(service.service_hold)
        self.attach()
        self.f.project["entities"] = []
        self.f.save_project()
        self.assertEqual(
            self.call(service.service_capture)["attachment"], self.attached
        )
        self.assertEqual(self.f.totals()[:2], (98, 0))

    def test_mutable_entity_without_immutable_edit_is_not_proof(self):
        self.call(service.service_hold)
        self.attach()
        with self.repo.transaction() as connection:
            connection.execute(
                update(receipts).values(
                    key="speech-attach-" + self.binding["request_id"]
                )
            )
        with self.assertRaisesRegex(credits.CreditError, "credit_attachment_required"):
            self.call(service.service_capture)
        self.assertTrue(
            self.call(service.service_release, reason="provider_failed")["created"]
        )
        self.assertEqual(self.f.totals()[:2], (100, 0))

    def test_forged_receipt_provenance_path_and_duplicate_entities_fail(self):
        self.call(service.service_hold)
        self.attach()
        changes = [
            lambda r: r.update(id="other-project"),
            lambda r: r.update(version=True),
            lambda r: r["project"]["entities"][-1].update(type="image"),
            lambda r: r["project"]["entities"][-1]["data"].update(
                cloudContentPath="https://foreign.invalid/audio"
            ),
            lambda r: r["project"]["entities"][-1]["data"]["speech"].update(
                characters=2
            ),
            lambda r: r["project"]["entities"][-1]["data"]["speech"].update(
                characters=True
            ),
            lambda r: r["project"]["entities"][-1]["data"]["speech"].update(
                provider_characters=True
            ),
            lambda r: r["project"]["entities"][-1]["data"]["speech"].update(
                provider_characters=-1
            ),
            lambda r: r["project"]["entities"].append(copy.deepcopy(self.entity)),
        ]
        for change in changes:
            with self.subTest(change=change):
                self.change_receipt(change)
                with self.assertRaisesRegex(
                    credits.CreditError, "credit_attachment_outcome_unconfirmed"
                ):
                    self.call(service.service_capture)
        self.change_receipt(lambda r: None)
        self.assertTrue(self.call(service.service_capture)["created"])

    def test_foreign_nonready_wrong_request_or_kind_asset_refused(self):
        self.call(service.service_hold)
        self.attach()
        for update_values in (
            {"owner": "foreign"},
            {"status": "validating"},
            {"project_id": "foreign"},
            {"record": json.dumps({**self.asset_record, "kind": "image"})},
            {
                "record": json.dumps(
                    {**self.asset_record, "client_asset_id": "speech:other-request"}
                )
            },
        ):
            with self.subTest(update_values=update_values):
                with self.repo.transaction() as connection:
                    connection.execute(update(asset_table).values(**update_values))
                with self.assertRaisesRegex(
                    credits.CreditError, "credit_attachment_outcome_unconfirmed"
                ):
                    self.call(service.service_capture)
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(asset_table).values(
                            owner=self.scope.owner_id,
                            status="ready",
                            project_id=self.scope.project_id,
                            record=json.dumps(self.asset_record),
                        )
                    )

    def test_capture_release_race_has_one_terminal(self):
        self.call(service.service_hold)
        self.attach()

        def attempt(operation):
            try:
                return self.call(
                    operation,
                    **(
                        {"reason": "stale_unattached"}
                        if operation is service.service_release
                        else {}
                    ),
                )
            except credits.CreditError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    attempt, (service.service_capture, service.service_release)
                )
            )
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.f.totals()[:2], (98, 0))
        with self.repo.engine.connect() as connection:
            terminals = (
                connection.execute(
                    select(entries.c.kind).where(
                        entries.c.reference_type == "speech",
                        entries.c.kind.in_(("capture", "release")),
                    )
                )
                .scalars()
                .all()
            )
        self.assertEqual(terminals, ["capture"])

    def test_deleted_asset_cannot_be_captured_from_original_edit(self):
        from studio_platform.content_lifecycle import put_tombstone
        from studio_platform.quick_chat import objects

        self.call(service.service_hold)
        self.attach()
        objects.create(self.repo.engine, checkfirst=True)
        with self.repo.transaction() as connection:
            put_tombstone(
                connection,
                self.scope.tenant_id,
                self.scope.owner_id,
                kind="asset",
                value=self.attached["asset_id"],
                action_id="test-deletion",
                now=self.now,
            )
        with self.assertRaisesRegex(
            credits.CreditError, "credit_attachment_outcome_unconfirmed"
        ):
            self.call(service.service_capture)
        with self.assertRaisesRegex(credits.CreditError, "credit_attachment_exists"):
            self.call(service.service_release, reason="stale_unattached")
        self.assertEqual(self.f.totals()[:2], (100, 2))

    def test_speech_hold_blocks_expiry_adoption_and_counts_with_images(self):
        from studio_platform.credit_expiry import check_adoption
        from studio_platform.credit_lots import POLICY
        from studio_platform.credits_pricing import PricingError

        self.f.call(service.service_hold)
        self.call(service.service_hold)
        with self.repo.transaction() as connection:
            self.assertEqual(
                credits.daily_projection(
                    connection, self.scope.tenant_id, self.scope.owner_id, self.now
                ),
                7,
            )
        self.f.call(service.service_release, reason="provider_failed")
        with self.repo.transaction() as connection:
            with self.assertRaisesRegex(PricingError, "credit_expiry_adoption_busy"):
                check_adoption(
                    connection,
                    self.scope.tenant_id,
                    {},
                    {"expiry_policy": POLICY},
                    self.now,
                )
        self.call(service.service_release, reason="provider_failed")
        with self.repo.transaction() as connection:
            check_adoption(
                connection,
                self.scope.tenant_id,
                {},
                {"expiry_policy": POLICY},
                self.now,
            )


if __name__ == "__main__":
    unittest.main()
