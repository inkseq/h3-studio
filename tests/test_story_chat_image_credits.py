"""Story card pictures against the real audited credit primitive (#463), through the API.

Fake image model only; real ledger, real receipts. Covers the original-decision rules:
a request that started while charging was off is never charged later, and the material a
hold froze is the material drawn and recovered.
"""

from pathlib import Path
import tempfile
import unittest
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, String, Table, insert, inspect, select, update

from studio_platform import credits, credits_pricing
from studio_platform.api import create_app
from studio_platform.credit_schema import accounts, metadata, prices
from studio_platform.identity_schema import admin_audit, users
from studio_platform.repository import documents
from studio_platform.settings import Settings
from studio_platform.story_chat_images import ImageError
from test_story_chat import CAST, ScriptedModel
from test_story_chat_images import FakeImages


class RealCreditImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            Path(self.tmp.name), auth_mode="local-test", story_chat_enabled=True
        )
        self.model, self.images = ScriptedModel(), FakeImages()
        self.app = create_app(
            settings, story_chat_client=self.model, story_chat_image_client=self.images
        )
        self.repo = self.app.state.repository
        self.addCleanup(self.repo.engine.dispose)
        self.tenant = settings.tenant_id
        self.now = self.repo.clock()
        engine = self.repo.engine
        existing = set(inspect(engine).get_table_names())
        for table in (users, admin_audit):
            if table.name not in existing:
                table.create(engine)
        metadata.create_all(engine)
        version = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        if "alembic_version" not in existing:
            version.create(engine)
        with self.repo.transaction() as conn:
            if not conn.execute(select(version)).first():
                conn.execute(insert(version).values(version_num="20261019_0004"))
            if not conn.execute(
                select(users.c.id).where(users.c.owner == "superdan")
            ).first():
                conn.execute(
                    insert(users).values(
                        id=str(uuid.uuid4()),
                        tenant=self.tenant,
                        owner="superdan",
                        role="user",
                        status="active",
                        display_name="QA",
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
            credits.grant(
                conn,
                self.tenant,
                "superdan",
                100,
                actor="browser:admin",
                reason="test",
                idempotency_key="seed",
                now=self.now,
            )
            conn.execute(
                insert(prices).values(
                    tenant=self.tenant,
                    revision="images-1",
                    rates=credits_pricing.DEFAULT_RATES,
                    configuration={
                        **credits_pricing.DEFAULT_CONFIGURATION,
                        "image_credits": 5,
                    },
                    published_by="admin",
                    published_at=self.now,
                )
            )
        self.policy(False)
        self.client = TestClient(self.app)
        self.assertEqual(
            self.client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).status_code,
            200,
        )
        chat = self.client.post("/v1/story-chats", json={"language": "en"}).json()
        self.model.plans.append(CAST)
        self.story = self.client.post(
            f"/v1/story-chats/{chat['project_id']}/turns",
            json={"turn_id": "turn-0001", "text": "aliens"},
        ).json()
        self.path = f"/v1/story-chats/{chat['project_id']}/images"
        self.zib = self.story["cards"]["characters"][0]["id"]

    def policy(self, enabled):
        where = (
            documents.c.tenant_id == self.tenant,
            documents.c.owner_id == credits.POLICY_OWNER,
            documents.c.project_id == credits.POLICY_PROJECT,
            documents.c.kind == credits.POLICY_KIND,
            documents.c.document_id == credits.POLICY_ID,
        )
        value = {"enabled": enabled, "active_revision": "images-1"}
        with self.repo.transaction() as conn:
            if conn.execute(select(documents.c.version).where(*where)).first():
                conn.execute(update(documents).where(*where).values(payload=value))
            else:
                conn.execute(
                    insert(documents).values(
                        tenant_id=self.tenant,
                        owner_id=credits.POLICY_OWNER,
                        project_id=credits.POLICY_PROJECT,
                        kind=credits.POLICY_KIND,
                        document_id=credits.POLICY_ID,
                        payload=value,
                        version=1,
                        updated_at=self.now,
                    )
                )

    def balance(self):
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(select(accounts.c.balance, accounts.c.held))
                .mappings()
                .one()
            )
        return row["balance"], row["held"]

    def draw(self, request_id):
        return self.client.post(
            self.path, json={"request_id": request_id, "card_id": self.zib}
        )

    def test_charged_picture_holds_then_captures_five(self):
        self.policy(True)
        response = self.draw("pic-paid-0001")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.balance(), (95, 0))
        self.assertEqual(
            self.client.get("/v1/story-chats/config").json()["image_credits"], 5
        )
        # A replay settles nothing new and never draws again.
        self.assertEqual(self.draw("pic-paid-0001").status_code, 200)
        self.assertEqual(self.balance(), (95, 0))
        self.assertEqual(len(self.images.calls), 1)

    def test_provider_failure_releases_the_hold(self):
        self.policy(True)
        self.images.fail = ImageError("image_not_returned", 422)
        self.assertEqual(self.draw("pic-fail-0001").status_code, 422)
        self.assertEqual(self.balance(), (100, 0))

    def test_free_request_is_never_charged_after_charging_turns_on(self):
        self.assertEqual(self.draw("pic-free-0001").status_code, 200)
        self.assertEqual(self.balance(), (100, 0))
        self.policy(True)
        replay = self.draw("pic-free-0001")
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(len(self.images.calls), 1)

    def test_free_unknown_outcome_is_never_charged_after_charging_turns_on(self):
        self.images.fail = ImageError("image_upstream_timeout", 504)
        self.assertEqual(self.draw("pic-free-0002").status_code, 504)
        self.policy(True)
        self.images.fail = None
        replay = self.draw("pic-free-0002")
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(
            replay.json()["detail"]["code"], "story_chat_image_in_progress"
        )
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(len(self.images.calls), 1)

    def test_card_changed_before_the_fence_draws_nothing_and_releases(self):
        self.policy(True)
        original = self.app.state.guided.mutate

        def edit_then_mutate(principal, project_id, body, key=None, **kwargs):
            if (
                key
                and key.startswith("story-chat-image-start-")
                and not getattr(self, "_edited", False)
            ):
                self._edited = True
                current = self.client.get("/v1/projects/" + project_id).json()
                self.client.post(
                    f"/v1/projects/{project_id}/actions",
                    json={
                        "expected_version": current["version"],
                        "actions": [
                            {
                                "op": "entity.update",
                                "entity_id": self.zib,
                                "patch": {"description": "now purple"},
                            }
                        ],
                    },
                )
            return original(principal, project_id, body, key, **kwargs)

        self.app.state.guided.mutate = edit_then_mutate
        response = self.draw("pic-race-0001")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "story_chat_card_changed")
        self.assertEqual(self.images.calls, [])
        self.assertEqual(self.balance(), (100, 0))

    def test_capture_interrupted_after_attach_recovers_with_original_material(self):
        self.policy(True)
        from studio_platform import service_credits

        original = service_credits.service_capture
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise credits.CreditError("credit_service_unavailable", 503)
            return original(*args, **kwargs)

        service_credits.service_capture = flaky
        self.addCleanup(setattr, service_credits, "service_capture", original)
        first = self.draw("pic-cap-00001")
        self.assertEqual(first.status_code, 503, first.text)
        self.assertEqual(self.balance(), (100, 5))
        # The card is edited afterwards; recovery still binds the original material.
        current = self.client.get("/v1/projects/" + self.story["project_id"]).json()
        self.client.post(
            f"/v1/projects/{self.story['project_id']}/actions",
            json={
                "expected_version": current["version"],
                "actions": [
                    {
                        "op": "entity.update",
                        "entity_id": self.zib,
                        "patch": {"title": "Zibby"},
                    }
                ],
            },
        )
        replay = self.draw("pic-cap-00001")
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(self.balance(), (95, 0))
        self.assertEqual(len(self.images.calls), 1)

    def start_receipt(self, request_id, change):
        """Rewrite (or delete) the stored start receipt to simulate legacy history."""
        from sqlalchemy import delete

        from studio_platform.guided import receipts

        where = receipts.c.key == "story-chat-image-start-" + request_id
        with self.repo.transaction() as conn:
            if change is None:
                conn.execute(delete(receipts).where(where))
                return
            response = conn.execute(
                select(receipts.c.response).where(where)
            ).scalar_one()
            change(
                response["project"]["journey"]["storyChat"]["imageRequests"][request_id]
            )
            conn.execute(update(receipts).where(where).values(response=response))

    def test_attachment_without_a_valid_start_is_never_charged(self):
        self.assertEqual(self.draw("pic-legacy-001").status_code, 200)
        self.start_receipt("pic-legacy-001", None)
        self.policy(True)
        replay = self.draw("pic-legacy-001")
        self.assertEqual(replay.status_code, 502, replay.text)
        self.assertEqual(
            replay.json()["detail"]["code"], "story_chat_image_outcome_unconfirmed"
        )
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(len(self.images.calls), 1)

    def test_start_record_without_a_hash_fails_closed_not_500(self):
        self.assertEqual(self.draw("pic-nohash-01").status_code, 200)
        self.start_receipt("pic-nohash-01", lambda record: record.pop("input_hash"))
        replay = self.draw("pic-nohash-01")
        self.assertEqual(replay.status_code, 502, replay.text)
        self.assertEqual(
            replay.json()["detail"]["code"], "story_chat_image_outcome_unconfirmed"
        )

    def test_same_id_with_changed_arguments_is_refused(self):
        self.assertEqual(self.draw("pic-args-0001").status_code, 200)
        other = self.client.post(
            self.path,
            json={
                "request_id": "pic-args-0001",
                "card_id": self.zib,
                "instruction": "bigger eyes",
            },
        )
        self.assertEqual(other.status_code, 409, other.text)
        self.assertEqual(
            other.json()["detail"]["code"], "story_chat_image_request_conflict"
        )
        mia = self.story["cards"]["characters"][1]["id"]
        swapped = self.client.post(
            self.path, json={"request_id": "pic-args-0001", "card_id": mia}
        )
        self.assertEqual(swapped.status_code, 409, swapped.text)
        self.assertEqual(
            swapped.json()["detail"]["code"], "story_chat_image_request_conflict"
        )
        # The original request itself still replays, after later card edits too.
        current = self.client.get("/v1/projects/" + self.story["project_id"]).json()
        self.client.post(
            f"/v1/projects/{self.story['project_id']}/actions",
            json={
                "expected_version": current["version"],
                "actions": [
                    {
                        "op": "entity.update",
                        "entity_id": self.zib,
                        "patch": {"description": "edited"},
                    }
                ],
            },
        )
        self.assertEqual(self.draw("pic-args-0001").status_code, 200)
        self.assertEqual(len(self.images.calls), 1)


if __name__ == "__main__":
    unittest.main()
