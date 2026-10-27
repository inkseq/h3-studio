"""Image advice isolation and one-shot behavior; synthetic images, no providers."""

import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from PIL import Image

from studio_platform.inky_providers import Jev
from studio_platform.inky_source import sources
from studio_platform.inky_uploads import UploadAdvice, image_bytes, match, preview
from studio_platform.inky_worker import InkyWorker
from test_inky_store import InkyStoreTests


def score(value):
    return {"probabilities": {str(i): int(i == value) for i in range(4)}}


class InkyUploadTests(InkyStoreTests):
    def setUp(self):
        super().setUp()
        self.shot = self.project["entities"][-1]
        self.shot["data"]["prompt"] = ""
        self.shot["data"]["candidateIds"] = ["photo"]
        self.project["entities"].append(
            {
                "id": "photo",
                "type": "image",
                "data": {"source": "upload", "cloudAssetId": "a" * 32},
            }
        )
        self.save()
        self.raw = io.BytesIO()
        Image.new("RGB", (320, 320), "red").save(self.raw, "WEBP")
        self.raw = self.raw.getvalue()
        self.record = {
            "status": "ready",
            "kind": "image",
            "preview": {
                "key": "private-preview",
                "size_bytes": len(self.raw),
                "sha256": hashlib.sha256(self.raw).hexdigest(),
            },
        }
        self.assets = Mock()
        self.store.assets = self.assets
        self.assets.get.return_value = self.record
        self.assets.store.open.side_effect = lambda key: io.BytesIO(self.raw)
        self.judge = Mock()
        self.judge.upload.return_value = {"answers": {"c0": score(3), "c1": score(0)}}
        self.worker = InkyWorker(
            self.store, judge=self.judge, uploads=UploadAdvice(self.assets, self.judge)
        )

    def refresh(self):
        self.worker.refresh(self.principal, self.scope.project_id)

    def test_attached_upload_empty_action_one_shot_cast_and_no_examples(self):
        self.refresh()
        item = self.read()["items"][0]
        self.assertEqual(
            (item["kind"], item["field"], item["shot_id"], item["card_name"]),
            ("upload_card", "cast", "shot", "Lin"),
        )
        self.assertNotIn("example", item)
        self.judge.assert_not_called()
        self.assets.get.assert_called_with(
            self.principal.owner, "a" * 32, project_id=self.scope.project_id
        )
        self.refresh()
        self.read()
        self.judge.upload.assert_called_once()
        self.assertEqual(self.project["entities"][1]["data"], {})

    def test_unattached_library_and_generated_image_make_no_call(self):
        self.shot["data"]["candidateIds"] = []
        self.save()
        self.refresh()
        self.judge.upload.assert_not_called()
        self.shot["data"]["candidateIds"] = ["photo"]
        self.project["entities"][-1]["data"]["source"] = "generation"
        self.save()
        self.refresh()
        self.judge.upload.assert_not_called()

    def test_card_change_stales_old_advice_and_late_publication(self):
        source = self.claim()
        self.project["entities"][1]["description"] = "New appearance"
        self.save()
        self.assertFalse(
            self.store.publish(
                self.principal,
                self.scope.project_id,
                source,
                [{"kind": "upload_card", "card_id": "person"}],
            )
        )
        self.refresh()
        self.assertEqual(len(self.read()["items"]), 1)
        self.project["entities"][1]["title"] = "Renamed"
        self.save()
        self.assertEqual(self.read(), {"items": []})

    def test_deleted_upload_hides_advice_without_another_provider_call(self):
        self.refresh()
        self.assertTrue(self.read()["items"])
        self.assets.get.side_effect = LookupError("deleted")
        self.assertEqual(self.read(), {"items": []})
        self.judge.upload.assert_called_once()

    def test_ignore_survives_edits_and_unknown_failure_never_retries(self):
        self.refresh()
        self.store.ignore(
            self.principal, self.scope.project_id, self.read()["items"][0]["id"]
        )
        self.shot["version"] = 2
        self.save()
        self.refresh()
        self.judge.upload.assert_called_once()
        self.assertEqual(self.read(), {"items": []})

    def test_failed_receipt_read_is_private_and_does_not_replay(self):
        self.assets.get.side_effect = ValueError("PRIVATE KEY AND CONTENT")
        with self.assertLogs("studio_platform.inky_worker", "INFO") as logs:
            self.refresh()
        self.refresh()
        self.assets.get.assert_called_once()
        self.judge.upload.assert_not_called()
        self.assertNotIn("PRIVATE", str(logs.output))

    def test_preview_integrity_and_scopes_fail_before_provider(self):
        upload = sources(self.project)["shot"]["upload"]
        self.record["preview"]["sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "inky_image_unavailable"):
            preview(self.assets, self.principal, self.scope.project_id, upload)
        denied = Mock(support_viewer=False)
        denied.allows.return_value = False
        self.assets.get.reset_mock()
        with self.assertRaises(ValueError):
            preview(self.assets, denied, self.scope.project_id, upload)
        self.assets.get.assert_not_called()

    def test_score_floor_ties_and_malformed_probabilities(self):
        upload = sources(self.project)["shot"]["upload"]
        self.assertIsNone(match(upload, {"answers": {"c0": score(1), "c1": score(0)}}))
        self.assertIsNone(match(upload, {"answers": {"c0": score(3), "c1": score(3)}}))
        with self.assertRaises(ValueError):
            match(upload, {"answers": {"c0": score(3)}})

    def test_image_protocol_has_one_asset_and_opt_in_header(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(200, json=self.judge.upload.return_value)

        client = Jev(
            loader=lambda _: SimpleNamespace(
                api_key="fake", base_url="https://api.typesafe.ai/v1"
            ),
            transport=httpx.MockTransport(handle),
        )
        upload = sources(self.project)["shot"]["upload"]
        jpeg = image_bytes(self.raw)
        client.upload(upload, jpeg)
        body = json.loads(calls[0].content)
        self.assertEqual(calls[0].headers["TypeSafe-Danger-Zone-Images"], "true")
        self.assertEqual(set(body["assets"]), {body["state"]["image"]})
        self.assertEqual(body["model"], "jev-1.14.0")
        self.assertNotIn("asset_id", str(body))
        self.assertNotIn("private-preview", str(body))
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertFalse(image.getexif())


def load_tests(loader, tests, pattern):
    import unittest

    return unittest.TestSuite(
        InkyUploadTests(name)
        for name in InkyUploadTests.__dict__
        if name.startswith("test_")
    )
