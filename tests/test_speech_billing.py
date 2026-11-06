"""Speech billing through HTTP with real temporary ledger/receipts and fake audio."""

import copy
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import delete, select, update

from studio_platform import (
    credits,
    service_credits,
    speech_credit_proof,
    speech_provider,
    speech_settings,
)
from studio_platform.speech_catalog import maker
from studio_platform.speech_requests import SpeechRequest
from studio_platform.credit_schema import entries
from studio_platform.guided import receipts
from studio_platform.repository import documents
from test_speech import mp3, set_switches
import test_story_chat_image_credits as image_fixtures


_current_bind = SpeechRequest.bind


def vendor_bind(request, speech):
    """Bind like the pre-#1097 code: vendor fields instead of our voice revision."""
    if "voice_revision" in speech:
        revision = speech["voice_revision"]
        speech = {
            key: value for key, value in speech.items() if key != "voice_revision"
        }
        speech |= maker(speech["voice_id"], revision)
    return _current_bind(request, speech)


class FakeSpeech:
    def __init__(self, audio):
        self.audio = audio
        self.calls = []
        self.fail = None

    def synthesize(self, text, voice, direction, language):
        self.calls.append((text, dict(voice), direction, language))
        if self.fail:
            raise self.fail
        # Provider-added direction characters must not increase customer credits.
        return speech_provider.Speech(self.audio, len(text) + 9)


class SpeechBillingTests(unittest.TestCase):
    policy = image_fixtures.RealCreditImageTests.policy
    balance = image_fixtures.RealCreditImageTests.balance

    @classmethod
    def setUpClass(cls):
        cls.audio = mp3()

    def setUp(self):
        image_fixtures.RealCreditImageTests.setUp(self)
        self.project = self.story["project_id"]
        self.path = f"/v1/projects/{self.project}/speech"
        self.adapter = FakeSpeech(self.audio)
        self.app.state.speech_adapters = {"openrouter": self.adapter}
        set_switches(self.app, voice_enabled=True)
        self.policy(True)

    def payload(self, request_id="speech-paid-0001", **changes):
        return {
            "request_id": request_id,
            "text": "hello",
            "voice_id": "v-warm",
            "direction": "calm",
            "language": "en",
            **changes,
        }

    def speak(self, request_id="speech-paid-0001", **changes):
        return self.client.post(self.path, json=self.payload(request_id, **changes))

    def switches(self, **changes):
        with self.repo.transaction() as connection:
            row = (
                connection.execute(
                    select(documents).where(documents.c.kind == speech_settings.KIND)
                )
                .mappings()
                .one()
            )
            connection.execute(
                update(documents)
                .where(documents.c.kind == speech_settings.KIND)
                .values(payload={**row["payload"], **changes})
            )

    def ledger(self):
        with self.repo.engine.connect() as connection:
            return list(
                connection.execute(
                    select(entries).where(entries.c.reference_type == "speech")
                ).mappings()
            )

    def test_quote_rounds_customer_characters_without_writes_or_provider(self):
        for count, cost in ((1, 2), (100, 2), (101, 4), (499, 10), (500, 10)):
            with self.subTest(count=count):
                response = self.client.post(
                    self.path + "/quote", json=self.payload(text="x" * count)
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json(),
                    {
                        "characters": count,
                        "credits": cost,
                        "price_revision": "speech-characters-v1",
                        "charging": True,
                    },
                )
        normalized = self.client.post(
            self.path + "/quote", json=self.payload(text="[ ] hello  ")
        )
        self.assertEqual(normalized.json()["characters"], 5)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.balance(), (100, 0))

    def test_paid_success_and_replay_capture_once(self):
        response = self.speak(text="x" * 500)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.balance(), (90, 0))
        self.assertEqual(self.speak(text="x" * 500).json(), response.json())
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(
            sorted(row["kind"] for row in self.ledger()), ["capture", "hold"]
        )
        self.assertEqual(
            self.client.get(response.json()["content_path"]).status_code, 200
        )

    def test_insufficient_balance_spends_nothing(self):
        from studio_platform.credit_schema import accounts

        with self.repo.transaction() as connection:
            connection.execute(update(accounts).values(balance=1))
        response = self.speak()
        self.assertEqual(response.status_code, 402, response.text)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.ledger(), [])
        # Refused before the start fence: after a top-up the same request still works.
        with self.repo.transaction() as connection:
            connection.execute(update(accounts).values(balance=100))
        retry = self.speak()
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_free_original_stays_free_when_charging_and_catalog_change(self):
        self.policy(False)
        first = self.speak()
        self.assertEqual(first.status_code, 200, first.text)
        self.policy(True)
        from studio_platform.speech_catalog import VOICES

        changed = {
            **VOICES["v-warm"],
            "model": "future-model",
            "provider_voice": "future-voice",
            "revision": "r2",
        }
        with patch.dict(VOICES, {"v-warm": changed}):
            again = self.speak()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json(), first.json())
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(self.ledger(), [])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_test_switch_keeps_original_request_free(self):
        self.switches(free_while_testing=True)
        self.assertEqual(self.speak().status_code, 200)
        self.switches(free_while_testing=False)
        self.assertEqual(self.speak().status_code, 200)
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(self.ledger(), [])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_paid_capture_recovers_after_switch_off_and_project_edit(self):
        original = service_credits.service_capture
        with patch.object(
            service_credits,
            "service_capture",
            side_effect=credits.CreditError("credit_service_unavailable", 503),
        ):
            first = self.speak()
        self.assertEqual(first.status_code, 503, first.text)
        self.assertEqual(self.balance(), (100, 2))
        self.policy(False)
        self.switches(voice_enabled=False)
        with self.repo.transaction() as connection:
            where = (
                documents.c.kind == "project",
                documents.c.document_id == self.project,
            )
            data = connection.execute(
                select(documents.c.payload).where(*where)
            ).scalar_one()
            data = copy.deepcopy(data)
            data["entities"] = [
                x for x in data["entities"] if x["id"] != "speech-speech-paid-0001"
            ]
            data.get("journey", {}).pop("speechRequests", None)
            connection.execute(update(documents).where(*where).values(payload=data))
        self.assertIs(service_credits.service_capture, original)
        again = self.speak()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(self.balance(), (98, 0))
        self.assertEqual(len(self.adapter.calls), 1)

    def test_pre_1097_receipt_still_recovers_its_interrupted_capture(self):
        # Build the state the pre-#1097 code left behind: a vendor-named binding
        # whose attachment committed but whose capture was interrupted.
        with (
            patch.object(SpeechRequest, "bind", vendor_bind),
            patch.object(speech_credit_proof, "is_legacy", return_value=False),
            patch.object(
                service_credits,
                "service_capture",
                side_effect=credits.CreditError("credit_service_unavailable", 503),
            ),
        ):
            first = self.speak()
        self.assertEqual(first.status_code, 503, first.text)
        self.assertEqual(self.balance(), (100, 2))
        with self.repo.engine.connect() as connection:
            stored = [
                row["response"]
                for row in connection.execute(select(receipts)).mappings()
                if row["key"] == "speech-v1:attach:speech-paid-0001"
            ]
        entity = next(
            e
            for e in stored[0]["project"]["entities"]
            if e["id"] == "speech-speech-paid-0001"
        )
        self.assertEqual(entity["data"]["speech"]["provider_voice"], "george")
        # The current code replays the same request: capture, no second voice call.
        again = self.speak()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(self.balance(), (98, 0))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(
            sorted(row["kind"] for row in self.ledger()), ["capture", "hold"]
        )
        # The public project read hides the old vendor fields; the receipt keeps them.
        raw = self.client.get(f"/v1/projects/{self.project}").text.lower()
        for word in ("openrouter", "eleven", '"george"', "provider_voice"):
            self.assertNotIn(word, raw)
        with self.repo.engine.connect() as connection:
            kept = connection.execute(
                select(receipts.c.response).where(
                    receipts.c.key == "speech-v1:attach:speech-paid-0001"
                )
            ).scalar_one()
        self.assertEqual(kept, stored[0])

    def test_a_vendor_named_binding_never_opens_a_new_hold(self):
        with patch.object(SpeechRequest, "bind", vendor_bind):
            response = self.speak()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.adapter.calls, [])

    def test_changed_arguments_never_reprice_or_generate(self):
        self.assertEqual(self.speak().status_code, 200)
        for changed in (
            {"text": "different"},
            {"voice_id": "v-bright"},
            {"direction": "angry"},
            {"language": "zh"},
        ):
            with self.subTest(changed=changed):
                response = self.speak(**changed)
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.balance(), (98, 0))

    def test_provider_timeout_releases_but_never_retries_same_request(self):
        self.adapter.fail = speech_provider.SpeechError("speech_provider_timeout", 504)
        first = self.speak()
        self.assertEqual(first.status_code, 504, first.text)
        self.assertEqual(self.balance(), (100, 0))
        self.adapter.fail = None
        again = self.speak()
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(
            sorted(row["kind"] for row in self.ledger()), ["hold", "release"]
        )

    def test_bad_audio_does_not_capture(self):
        self.adapter.audio = b"not an audio file"
        response = self.speak()
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertEqual(self.balance(), (100, 0))
        self.assertNotIn("capture", [row["kind"] for row in self.ledger()])

    def test_storage_failure_does_not_capture(self):
        with patch.object(
            self.app.state.assets, "upload", return_value={"status": "failed"}
        ):
            response = self.speak()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.balance(), (100, 0))
        self.assertNotIn("capture", [row["kind"] for row in self.ledger()])

    def test_attachment_commit_unknown_recovers_original_without_replay(self):
        original = self.app.state.guided.mutate

        def commit_then_interrupt(principal, project_id, body, key=None, **kwargs):
            result = original(principal, project_id, body, key, **kwargs)
            if key and key.startswith("speech-v1:attach:"):
                raise RuntimeError("transport_interrupted_after_commit")
            return result

        with patch.object(
            self.app.state.guided, "mutate", side_effect=commit_then_interrupt
        ):
            first = self.speak()
        self.assertEqual(first.status_code, 502, first.text)
        self.assertEqual(self.balance(), (100, 2))
        again = self.speak()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(self.balance(), (98, 0))
        self.assertEqual(len(self.adapter.calls), 1)

    def test_start_commit_unknown_never_resubmits_or_claims_no_charge(self):
        original = self.app.state.guided.mutate

        def commit_then_interrupt(principal, project_id, body, key=None, **kwargs):
            result = original(principal, project_id, body, key, **kwargs)
            if key and key.startswith("speech-v1:start:"):
                raise RuntimeError("transport_interrupted_after_commit")
            return result

        with patch.object(
            self.app.state.guided, "mutate", side_effect=commit_then_interrupt
        ):
            first = self.speak()
        self.assertEqual(first.status_code, 502, first.text)
        self.assertEqual(self.adapter.calls, [])
        again = self.speak()
        self.assertEqual(again.status_code, 502, again.text)
        self.assertEqual(again.json()["detail"]["code"], "speech_outcome_unconfirmed")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.balance(), (100, 0))
        self.assertNotIn("capture", [row["kind"] for row in self.ledger()])

    def test_paid_concurrent_replay_does_not_release_another_callers_hold(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.adapter.synthesize

        def pause(*args):
            entered.set()
            self.assertTrue(finish.wait(timeout=8))
            return original(*args)

        with (
            patch.object(self.adapter, "synthesize", side_effect=pause),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(self.speak)
            self.assertTrue(entered.wait(timeout=8))
            try:
                second = self.speak()
                self.assertEqual(second.status_code, 409, second.text)
                self.assertEqual(self.balance(), (100, 2))
            finally:
                finish.set()
            response = first.result(timeout=12)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.balance(), (98, 0))

    def test_other_actor_original_receipt_still_fences_after_project_save(self):
        self.policy(False)
        self.assertEqual(self.speak().status_code, 200)
        with self.repo.transaction() as connection:
            connection.execute(
                update(receipts)
                .where(
                    receipts.c.key.in_(
                        (
                            "speech-v1:start:speech-paid-0001",
                            "speech-v1:attach:speech-paid-0001",
                        )
                    )
                )
                .values(actor="browser:another-authorized-actor")
            )
            where = (
                documents.c.kind == "project",
                documents.c.document_id == self.project,
            )
            data = copy.deepcopy(
                connection.execute(
                    select(documents.c.payload).where(*where)
                ).scalar_one()
            )
            data["entities"] = [
                x for x in data["entities"] if x["id"] != "speech-speech-paid-0001"
            ]
            data.get("journey", {}).pop("speechRequests", None)
            connection.execute(update(documents).where(*where).values(payload=data))
        response = self.speak()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "speech_request_conflict")
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.ledger(), [])

    def test_free_concurrent_requests_with_same_clock_call_provider_once(self):
        self.policy(False)
        original = self.app.state.guided.mutate
        barrier = threading.Barrier(2)

        def race(principal, project_id, body, key=None, **kwargs):
            if key and key.startswith("speech-v1:start:"):
                barrier.wait(timeout=8)
            return original(principal, project_id, body, key, **kwargs)

        with (
            patch.object(self.repo, "clock", return_value=self.now),
            patch.object(self.app.state.guided, "mutate", side_effect=race),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(self.speak) for _ in range(2)]
            responses = [future.result(timeout=15) for future in futures]
        self.assertIn(200, [response.status_code for response in responses])
        self.assertTrue(
            all(response.status_code in (200, 409) for response in responses),
            [response.text for response in responses],
        )
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.balance(), (100, 0))

    def test_missing_start_proof_never_retroactively_charges(self):
        self.policy(False)
        self.assertEqual(self.speak().status_code, 200)
        with self.repo.transaction() as connection:
            connection.execute(
                delete(receipts).where(
                    receipts.c.key == "speech-v1:start:speech-paid-0001"
                )
            )
        self.policy(True)
        response = self.speak()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(len(self.adapter.calls), 1)

    def test_public_actions_cannot_write_or_replay_server_receipts(self):
        request_id = "speech-paid-0001"
        for phase in ("start", "attach"):
            key = f"speech-v1:{phase}:{request_id}"
            for successful_original in (False, True):
                if successful_original and not self.adapter.calls:
                    self.assertEqual(self.speak().status_code, 200)
                row = self.client.get(f"/v1/projects/{self.project}").json()
                response = self.client.post(
                    f"/v1/projects/{self.project}/actions",
                    headers={"Idempotency-Key": key},
                    json={
                        "expected_version": row["version"],
                        "actions": [
                            {"op": "journey.update", "patch": {"forged": True}}
                        ],
                        "receipt_channel": "speech",
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)
                created = self.client.post(
                    "/v1/projects",
                    headers={"Idempotency-Key": key},
                    json={"title": "Cannot create server receipt"},
                )
                self.assertEqual(created.status_code, 422, created.text)
        self.assertEqual(self.balance(), (98, 0))
        self.assertEqual(len(self.adapter.calls), 1)

    def test_valid_full_project_refusal_releases_hold_without_capture(self):
        from studio_platform.project_validation import validate_project

        with self.repo.transaction() as connection:
            where = (
                documents.c.kind == "project",
                documents.c.document_id == self.project,
            )
            data = copy.deepcopy(
                connection.execute(
                    select(documents.c.payload).where(*where)
                ).scalar_one()
            )
            for index in range(5000 - len(data["entities"])):
                data["entities"].append(
                    {
                        "id": f"limit-note-{index}",
                        "type": "note",
                        "title": "Note",
                        "description": "",
                        "data": {},
                        "version": 1,
                        "order": index,
                        "status": "draft",
                        "parentId": None,
                    }
                )
            validate_project(data, self.app.state.settings.max_project_bytes)
            connection.execute(update(documents).where(*where).values(payload=data))
        response = self.speak()
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(
            sorted(row["kind"] for row in self.ledger()), ["hold", "release"]
        )
        again = self.speak()
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_public_audio_and_lookalike_receipt_never_prove_generated_delivery(self):
        original = self.adapter.synthesize
        request_id = "speech-paid-0001"

        def forge_then_fail(*args):
            original(*args)
            row = self.client.get(f"/v1/projects/{self.project}").json()
            marker = row["project"]["journey"]["speechRequests"][request_id]
            uploaded = self.client.post(
                "/v1/assets",
                data={
                    "client_project_id": self.project,
                    "client_asset_id": "speech:" + request_id,
                },
                files={"file": ("unrelated.mp3", self.audio, "audio/mpeg")},
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            entity_id = "speech-" + request_id
            forged = self.client.post(
                f"/v1/projects/{self.project}/actions",
                headers={"Idempotency-Key": "speech-attach-" + request_id},
                json={
                    "expected_version": row["version"],
                    "actions": [
                        {
                            "op": "asset.attach",
                            "asset_id": uploaded.json()["asset_id"],
                            "entity_id": entity_id,
                            "title": "Public audio",
                        },
                        {
                            "op": "entity.update",
                            "entity_id": entity_id,
                            "patch": {
                                "data": {
                                    "speech": {
                                        **marker["speech"],
                                        "request_id": request_id,
                                        "input_hash": marker["input_hash"],
                                        "provider_characters": 14,
                                    }
                                }
                            },
                        },
                    ],
                },
            )
            self.assertEqual(forged.status_code, 200, forged.text)
            raise speech_provider.SpeechError("speech_provider_timeout", 504)

        with patch.object(self.adapter, "synthesize", side_effect=forge_then_fail):
            response = self.speak()
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(self.balance(), (100, 0))
        self.assertEqual(self.speak().status_code, 409)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(
            sorted(row["kind"] for row in self.ledger()), ["hold", "release"]
        )


if __name__ == "__main__":
    unittest.main()
