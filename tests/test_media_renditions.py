"""Actual CPU image bytes, original receipt recovery and private API fences."""

from dataclasses import replace
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from sqlalchemy import create_engine, delete

from studio_platform.assets import (
    AssetConflict,
    AssetNotFound,
    AssetQuotaExceeded,
    AssetService,
)
from studio_platform.media_renditions import public_renditions
from studio_platform.rendition_backfill import plan, plan_all, main as backfill_main
from studio_platform.settings import Settings
from studio_platform.storage import S3ObjectStore, StorageWriteUncertain, StorageError
from studio_platform.storage_asset_journal import receipts
from studio_platform.storage_config import S3Credentials
from test_platform_assets import FaultStore, png
from test_platform_storage import OfflineTest, config
from test_platform_storage_multipart import FakeMultipartS3, ProcessStopped
import test_platform_api as api_fixture
from fastapi.testclient import TestClient
from studio_platform.api import create_app


class RenditionTests(OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = create_engine("sqlite:///" + (self.root / "assets.db").as_posix())
        self.addCleanup(self.engine.dispose)
        self.store = FaultStore(self.root / "objects")
        self.service = self.make_service()

    def make_service(self, **options):
        return AssetService(
            self.engine, self.store, self.root, max_bytes=2 * 1024 * 1024, **options
        )

    def upload(self, body=None, filename="source.png"):
        return self.service.upload(
            "owner",
            "project",
            io.BytesIO(body or png()),
            filename,
            client_asset_id="same-image",
        )

    def legacy_upload(self):
        with mock.patch.object(self.service, "_prepare_renditions", return_value={}):
            return self.upload()

    def test_actual_sizes_exif_transparency_and_original_model_identity(self):
        out = io.BytesIO()
        source = Image.new("RGB", (1600, 800), "navy")
        exif = Image.Exif()
        exif[274] = 6
        source.save(out, "JPEG", exif=exif)
        raw = out.getvalue()
        value = self.upload(raw, "portrait.jpg")
        held = self.service.get("owner", value["id"])
        self.assertTrue(value["rendition_ready"])
        self.assertEqual(held["original"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            set(held) & {"original", "model", "thumb", "preview"},
            {"original", "model", "thumb", "preview"},
        )
        for role, dimensions, cap in (
            ("thumb", (160, 320), 64 * 1024),
            ("preview", (640, 1280), 512 * 1024),
        ):
            public = value["renditions"][role]
            with self.store.open(held[role]["key"]) as stream:
                encoded = stream.read()
                image = Image.open(io.BytesIO(encoded))
                self.assertEqual((image.format, image.size), ("WEBP", dimensions))
                self.assertEqual(dict(image.getexif()), {})
            self.assertEqual(public["sha256"], hashlib.sha256(encoded).hexdigest())
            self.assertEqual(public["size_bytes"], len(encoded))
            self.assertLessEqual(len(encoded), cap)
            self.assertNotIn("key", public)
        output = io.BytesIO()
        Image.new("RGBA", (256, 256), (20, 60, 80, 90)).save(output, "PNG")
        alpha = self.service.upload(
            "owner", "project", io.BytesIO(output.getvalue()), "alpha.png"
        )
        for role in ("thumb", "preview"):
            spec = self.service.get("owner", alpha["id"])[role]
            with self.store.open(spec["key"]) as stream:
                image = Image.open(stream)
                self.assertEqual(image.size, (256, 256))
                self.assertEqual(image.getpixel((10, 10))[3], 90)

    def test_idempotent_upload_and_unknown_rendition_put_recover_same_key_once(self):
        self.store.failure = (3, "after")
        with self.assertRaises(StorageWriteUncertain):
            self.upload()
        value = self.service.list("owner", "project")[0]
        old = self.service.journal.get("owner", value["id"])
        key = old["objects"]["thumb"]["key"]
        self.assertEqual(old["objects"]["thumb"]["phase"], "put_unknown")
        recovered = self.service.resume("owner", value["id"])
        self.assertTrue(recovered["rendition_ready"])
        self.assertEqual(self.store.calls.count(key), 1)
        used = self.service.usage("owner")
        self.assertEqual(self.upload()["id"], value["id"])
        self.assertEqual(self.service.usage("owner"), used)
        self.assertEqual(
            self.service.model_snapshot("owner", "project", value["id"])["model"],
            self.service.get("owner", value["id"])["model"],
        )

    def test_noisy_transparent_display_respects_byte_bound_without_dropping_alpha(self):
        import random

        source = Image.frombytes(
            'RGBA', (512, 512), random.Random(287).randbytes(512 * 512 * 4)
        )
        body = io.BytesIO()
        source.save(body, 'PNG')
        value = self.upload(body.getvalue())
        self.assertTrue(value['rendition_ready'])
        held = self.service.get('owner', value['id'])
        self.assertLessEqual(held['thumb']['size_bytes'], 64 * 1024)
        with self.store.open(held['thumb']['key']) as stream:
            display = Image.open(stream)
            self.assertIn('A', display.mode)
            self.assertLessEqual(max(display.size), 320)
            low, high = display.getchannel('A').getextrema()
            self.assertLess(low, high)

    def test_ready_original_backfill_reuses_receipt_and_unknown_key_not_new_asset(self):
        value = self.legacy_upload()
        old = self.service.get("owner", value["id"])
        receipt = self.service.journal.get("owner", value["id"])
        self.store.failure = (len(self.store.calls) + 1, "after")
        with self.assertRaises(StorageWriteUncertain):
            self.service.ensure_image_renditions(
                "owner", value["id"], expected_version=receipt["version"]
            )
        pending = self.service.get("owner", value["id"])
        self.assertEqual(
            (pending["status"], pending["original"]), ("ready", old["original"])
        )
        key = self.service.journal.get("owner", value["id"])["objects"]["thumb"]["key"]
        restored = self.service.ensure_image_renditions("owner", value["id"])
        self.assertTrue(restored["rendition_ready"])
        self.assertEqual(self.store.calls.count(key), 1)
        used = self.service.usage("owner")
        self.assertEqual(
            self.service.ensure_image_renditions("owner", value["id"]), restored
        )
        self.assertEqual(self.service.usage("owner"), used)
        self.assertEqual(len(self.service.list("owner", "project")), 1)
        self.assertEqual(
            self.service.get("owner", value["id"])["original"], old["original"]
        )
        self.assertTrue(
            self.service._file(
                self.service.journal.get("owner", value["id"]), "source.png"
            ).exists()
        )

    def test_backfill_refuses_foreign_stale_busy_missing_receipt_and_quota(self):
        value = self.legacy_upload()
        version = self.service.journal.get("owner", value["id"])["version"]
        before = self.service.usage("owner")
        calls = list(self.store.calls)
        with self.assertRaises(AssetNotFound):
            self.service.ensure_image_renditions("other", value["id"])
        with self.assertRaises(AssetConflict):
            self.service.ensure_image_renditions(
                "owner", value["id"], expected_version=version - 1
            )
        with self.assertRaises(AssetConflict):
            self.service.ensure_image_renditions("owner", value["id"], interrupted=True)
        self.assertEqual(self.store.calls, calls)
        self.assertEqual(self.service.usage("owner"), before)
        limited = self.make_service(owner_quota_bytes=before['accounted_bytes'] + 1000)
        with self.assertRaises(AssetQuotaExceeded):
            limited.ensure_image_renditions("owner", value["id"])
        self.assertEqual(
            limited.usage("owner")["accounted_bytes"], before["accounted_bytes"]
        )
        self.assertEqual(limited.usage("owner")["active_uploads"], 0)
        self.assertEqual(self.store.calls, calls)
        with self.engine.begin() as connection:
            connection.execute(delete(receipts).where(receipts.c.id == value["id"]))
        census = plan(self.engine, tenant=self.service.tenant, owner="owner")
        self.assertEqual(census["items"][0]["reason_code"], "missing_original_receipt")
        with self.assertRaises(AssetConflict):
            self.service.ensure_image_renditions("owner", value["id"])
        self.assertEqual(
            plan(self.engine, tenant=self.service.tenant, owner="other")["items"], []
        )

    def test_r2_backfill_after_original_scratch_cleanup_keeps_originals_and_exact_accounting(
        self,
    ):
        fake = FakeMultipartS3(config())
        self.store = S3ObjectStore(config(), client=fake)
        self.service = self.make_service()
        value = self.legacy_upload()
        old = self.service.get("owner", value["id"])
        receipt = self.service.journal.get("owner", value["id"])
        self.assertFalse(self.service._stage(receipt).exists())
        restored = self.service.ensure_image_renditions("owner", value["id"])
        receipt = self.service.journal.get("owner", value["id"])
        self.assertTrue(restored["rendition_ready"])
        self.assertFalse(self.service._stage(receipt).exists())
        self.assertEqual(
            self.service.get("owner", value["id"])["original"], old["original"]
        )
        self.assertEqual(len(fake.objects), 4)
        self.assertEqual(
            self.service.usage("owner")["accounted_bytes"],
            sum(x["size_bytes"] for x in receipt["objects"].values()),
        )
        self.assertEqual(receipt["scratch_cleanup"]["phase"], "complete")

    def test_crashed_backfill_requires_original_receipt_cas_and_stopped_writer_assertion(
        self,
    ):
        value = self.legacy_upload()
        self.store.failure = (len(self.store.calls) + 1, "crash")
        with self.assertRaises(ProcessStopped):
            self.service.ensure_image_renditions("owner", value["id"])
        receipt = self.service.journal.get("owner", value["id"])
        key = receipt["objects"]["thumb"]["key"]
        self.assertTrue(receipt["busy"])
        with self.assertRaises(AssetConflict):
            self.service.ensure_image_renditions("owner", value["id"])
        with self.assertRaises(AssetConflict):
            self.service.ensure_image_renditions(
                "owner",
                value["id"],
                interrupted=True,
                expected_version=receipt["version"] - 1,
            )
        recovered = self.service.ensure_image_renditions(
            "owner", value["id"], interrupted=True, expected_version=receipt["version"]
        )
        self.assertTrue(recovered["rendition_ready"])
        self.assertEqual(self.store.calls.count(key), 1)
        self.assertEqual(self.service.usage("owner")["active_uploads"], 0)

    def test_r2_backfill_cleanup_crash_finishes_original_manifest_and_quota_once(self):
        fake = FakeMultipartS3(config())
        self.store = S3ObjectStore(config(), client=fake)
        self.service = self.make_service()
        value = self.legacy_upload()
        original_save = self.service.journal.save_renditions

        def crashed(receipt, **kwargs):
            if (
                receipt.get("rendition_backfill", {}).get("phase") == "complete"
                and receipt.get("scratch_cleanup", {}).get("phase") == "complete"
            ):
                raise ProcessStopped()
            return original_save(receipt, **kwargs)

        with mock.patch.object(
            self.service.journal, "save_renditions", side_effect=crashed
        ):
            with self.assertRaises(ProcessStopped):
                self.service.ensure_image_renditions("owner", value["id"])
        receipt = self.service.journal.get("owner", value["id"])
        self.assertEqual(receipt["scratch_cleanup"]["phase"], "pending")
        self.assertFalse(list(self.service._stage(receipt).iterdir()))
        with mock.patch.object(
            self.store, "put", side_effect=AssertionError("No new object/key")
        ):
            self.assertTrue(
                self.service.ensure_image_renditions("owner", value["id"])[
                    "rendition_ready"
                ]
            )
        used = self.service.usage("owner")
        self.service.ensure_image_renditions("owner", value["id"])
        self.assertEqual(self.service.usage("owner"), used)
        receipt = self.service.journal.get("owner", value["id"])
        self.assertEqual(
            used["accounted_bytes"],
            sum(s["size_bytes"] for s in receipt["objects"].values()),
        )

    def test_existing_hardlinked_display_path_is_rejected_without_touching_original(
        self,
    ):
        import os
        from studio_platform.storage import IntegrityError

        value = self.legacy_upload()
        receipt = self.service.journal.get("owner", value["id"])
        outside = self.root / "retained-other-file"
        outside.write_bytes(b"must remain")
        os.link(outside, self.service._stage(receipt) / "thumb-v1.webp")
        calls = list(self.store.calls)
        with self.assertRaises(IntegrityError):
            self.service.ensure_image_renditions("owner", value["id"])
        self.assertEqual(outside.read_bytes(), b"must remain")
        self.assertEqual(self.store.calls, calls)

    def test_backfill_preparation_retries_reacquire_headroom_before_any_new_bytes(self):
        value = self.legacy_upload()
        before = self.service.usage('owner')['accounted_bytes']
        calls = list(self.store.calls)
        limited = AssetService(
            self.engine, self.store, self.root, max_bytes=1024, owner_quota_bytes=5000
        )
        with self.assertRaises(AssetQuotaExceeded):
            limited.ensure_image_renditions('owner', value['id'])
        receipt = limited.journal.get('owner', value['id'])
        self.assertEqual(receipt['rendition_backfill']['phase'], 'planned')
        self.assertFalse((limited._stage(receipt) / 'rendition-source.png').exists())
        self.assertEqual(limited.usage('owner')['accounted_bytes'], before)
        with self.assertRaises(AssetQuotaExceeded):
            limited.ensure_image_renditions('owner', value['id'])
        self.assertEqual(self.store.calls, calls)
        roomy = AssetService(
            self.engine, self.store, self.root, max_bytes=1024, owner_quota_bytes=20000
        )
        self.assertTrue(
            roomy.ensure_image_renditions('owner', value['id'])['rendition_ready']
        )

    def test_offline_plan_and_stale_apply_never_load_credentials_or_create_a_store(
        self,
    ):
        value = self.legacy_upload()
        output = io.StringIO()
        settings = Settings(
            self.root, database_url='sqlite:///' + (self.root / 'assets.db').as_posix()
        )
        with (
            mock.patch.object(Settings, 'from_environment', return_value=settings),
            redirect_stdout(output),
        ):
            loader = mock.Mock(side_effect=AssertionError('No credential read'))
            factory = mock.Mock(side_effect=AssertionError('No storage construction'))
            self.assertEqual(
                backfill_main(
                    ['plan', '--owner', 'owner'],
                    credentials_loader=loader,
                    store_factory=factory,
                ),
                0,
            )
            self.assertEqual(
                backfill_main(
                    [
                        'apply',
                        '--owner',
                        'owner',
                        '--asset-id',
                        value['id'],
                        '--receipt-version',
                        '9999',
                    ],
                    credentials_loader=loader,
                    store_factory=factory,
                ),
                1,
            )
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]['items'][0]['reason_code'], 'backfill_required')
        self.assertEqual(
            rows[1]['reason_code'], 'rendition_current_owned_receipt_required'
        )
        loader.assert_not_called()
        factory.assert_not_called()

    def test_copy_proof_flags_require_one_exact_receipt_before_any_configuration_read(
        self,
    ):
        selections = [
            ["plan", "--owner", "owner", "--original-copy-receipts", "private"],
            ["apply", "--all-owners", "--original-copy-receipts", "private"],
            [
                "apply",
                "--owner",
                "owner",
                "--artifact-id",
                "artifact",
                "--asset-id",
                "asset",
                "--work-dir",
                "original",
                "--receipt-version",
                "1",
                "--original-copy-receipts",
                "private",
            ],
            [
                "apply",
                "--owner",
                "owner",
                "--asset-id",
                "asset",
                "--receipt-version",
                "1",
                "--original-local-root",
                "/data/objects",
            ],
            [
                "apply",
                "--owner",
                "owner",
                "--asset-id",
                "asset",
                "--receipt-version",
                "1",
                "--original-copy-receipts",
                "private",
                "--original-local-root",
                "/data/objects",
            ],
        ]
        with mock.patch.object(
            Settings,
            "from_environment",
            side_effect=AssertionError("No configuration or credential load"),
        ) as settings:
            for selection in selections:
                with (
                    self.subTest(selection=selection),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(backfill_main(selection), 1)
                self.assertEqual(
                    json.loads(output.getvalue())["reason_code"],
                    "rendition_copy_requires_exact_receipt_proof",
                )
            settings.assert_not_called()

    def test_all_owner_plan_is_tenant_scoped_paged_and_has_no_storage_calls(self):
        self.legacy_upload()
        with mock.patch.object(self.service, '_prepare_renditions', return_value={}):
            self.service.upload('second', 'project', io.BytesIO(png()), 'source.png')
        foreign = AssetService(self.engine, self.store, self.root, tenant='foreign')
        with mock.patch.object(foreign, '_prepare_renditions', return_value={}):
            foreign.upload('hidden', 'project', io.BytesIO(png()), 'source.png')
        settings = Settings(self.root, database_url=str(self.engine.url))
        loader = mock.Mock(side_effect=AssertionError('No credential read'))
        factory = mock.Mock(side_effect=AssertionError('No store construction'))
        with (
            mock.patch.object(Settings, 'from_environment', return_value=settings),
            mock.patch.object(
                self.store, 'open', side_effect=AssertionError('No object read')
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                backfill_main(
                    ['plan', '--all-owners', '--owner-limit', '1'],
                    credentials_loader=loader,
                    store_factory=factory,
                ),
                0,
            )
        first = json.loads(output.getvalue())
        self.assertEqual([page['owner'] for page in first['owners']], ['owner'])
        self.assertEqual(first['next_owner_offset'], 1)
        second = plan_all(self.engine, tenant='sixnine', owner_limit=1, owner_offset=1)
        self.assertEqual([page['owner'] for page in second['owners']], ['second'])
        self.assertIsNone(second['next_owner_offset'])
        loader.assert_not_called()
        factory.assert_not_called()

    def test_all_owner_apply_reuses_receipts_and_keeps_different_binding_refused(self):
        original = self.legacy_upload()
        with mock.patch.object(self.service, '_prepare_renditions', return_value={}):
            blocked = self.service.upload(
                'second', 'project', io.BytesIO(png()), 'source.png'
            )
        old = self.service.journal.get('second', blocked['id'])
        old['storage_binding'] = '0' * 64
        self.service.journal.save(
            old
        )  # A synthetic old-binding fixture, not a rebind operation.
        original_object = self.service.get('owner', original['id'])['original']
        blocked_object = self.service.get('second', blocked['id'])['original']
        baseline = self.service.usage('second')
        settings = Settings(self.root, database_url=str(self.engine.url))
        with (
            mock.patch.object(Settings, 'from_environment', return_value=settings),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(backfill_main(['apply', '--all-owners']), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['attempted'], 2)
        self.assertEqual(
            [item['state'] for item in result['results']], ['complete', 'refused']
        )
        self.assertEqual(
            result['results'][1]['reason_code'],
            'rendition_original_receipt_unconfirmed',
        )
        self.assertTrue(
            public_renditions(self.service.get('owner', original['id']))[
                'rendition_ready'
            ]
        )
        self.assertEqual(
            self.service.get('owner', original['id'])['original'], original_object
        )
        self.assertEqual(self.service.journal.get('second', blocked['id']), old)
        self.assertEqual(self.service.usage('second'), baseline)
        self.assertEqual(
            self.service.get('second', blocked['id'])['original'], blocked_object
        )

    def test_all_owner_item_bound_and_repeat_never_regenerate_completed_display(self):
        first = self.legacy_upload()
        with mock.patch.object(self.service, '_prepare_renditions', return_value={}):
            second = self.service.upload(
                'second', 'project', io.BytesIO(png()), 'source.png'
            )
        settings = Settings(self.root, database_url=str(self.engine.url))
        with (
            mock.patch.object(Settings, 'from_environment', return_value=settings),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                backfill_main(['apply', '--all-owners', '--max-items', '1']), 2
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(result['results'][1]['reason_code'], 'rendition_item_limit')
        completed = self.service.journal.get('owner', first['id'])
        accounted = self.service.usage('owner')
        with (
            mock.patch.object(Settings, 'from_environment', return_value=settings),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                backfill_main(['apply', '--all-owners', '--max-items', '1']), 0
            )
        self.assertEqual(json.loads(output.getvalue())['attempted'], 1)
        self.assertEqual(self.service.journal.get('owner', first['id']), completed)
        self.assertEqual(self.service.usage('owner'), accounted)
        self.assertTrue(
            public_renditions(self.service.get('second', second['id']))[
                'rendition_ready'
            ]
        )

    def test_all_owner_mode_cannot_globally_take_over_writers_or_override_roots(self):
        with mock.patch.object(
            Settings, 'from_environment', side_effect=AssertionError('invalid mode')
        ):
            for extra in (
                ['--assert-writers-stopped'],
                ['--work-dir', str(self.root)],
                ['--receipt-version', '1'],
                ['--asset-id', 'original'],
            ):
                with self.subTest(extra=extra), redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        backfill_main(['apply', '--all-owners', *extra]), 1
                    )

    def test_all_owner_snapshot_race_refuses_stale_item_after_earlier_success(self):
        from studio_platform import rendition_backfill as module

        first = self.legacy_upload()
        with mock.patch.object(self.service, '_prepare_renditions', return_value={}):
            second = self.service.upload(
                'second', 'project', io.BytesIO(png()), 'source.png'
            )
        settings = Settings(self.root, database_url=str(self.engine.url))
        saved = []

        def race(*args, **kwargs):
            snapshot = plan_all(*args, **kwargs)
            receipt = self.service.journal.get('second', second['id'])
            self.service.journal.save(receipt)
            saved.append(receipt)
            return snapshot

        with (
            mock.patch.object(Settings, 'from_environment', return_value=settings),
            mock.patch.object(module, 'plan_all', side_effect=race),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(backfill_main(['apply', '--all-owners']), 2)
        rows = json.loads(output.getvalue())['results']
        self.assertEqual([item['state'] for item in rows], ['complete', 'refused'])
        self.assertEqual(
            rows[1]['reason_code'], 'rendition_current_owned_receipt_required'
        )
        self.assertTrue(
            public_renditions(self.service.get('owner', first['id']))['rendition_ready']
        )
        self.assertEqual(self.service.journal.get('second', second['id']), saved[0])

    def test_stable_webp_signing_keeps_video_and_default_asset_policy_narrow(self):
        store = S3ObjectStore(config(), S3Credentials("fake-access", "fake-secret"))
        with mock.patch("studio_platform.storage.time.time", return_value=1760572801):
            one = store.presign_immutable_rendition(
                "safe/thumb.webp", content_type="image/webp"
            )
        with mock.patch("studio_platform.storage.time.time", return_value=1760572810):
            two = store.presign_immutable_rendition(
                "safe/thumb.webp", content_type="image/webp"
            )
        self.assertEqual(one.reveal(), two.reveal())
        query = parse_qs(urlsplit(one.reveal()).query)
        self.assertEqual(query["response-content-type"], ["image/webp"])
        self.assertEqual(
            query["response-cache-control"], ["private, max-age=86400, immutable"]
        )
        with self.assertRaises(StorageError):
            store.presign_immutable_rendition("safe/original", content_type="image/png")
        self.assertEqual(
            parse_qs(urlsplit(store.presign_download("safe/original").reveal()).query)[
                "X-Amz-Expires"
            ],
            ["300"],
        )


class RenditionApiTests(unittest.TestCase):
    setUp = api_fixture.ApiTests.setUp
    login = api_fixture.ApiTests.login
    setup_project = api_fixture.ApiTests.setup_project

    def upload(self):
        self.setup_project()
        response = self.client.post(
            "/v1/assets",
            data={"client_project_id": "story-one"},
            files={"file": ("reference.png", png(), "image/png")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_owner_cache_read_metadata_missing_and_strict_selector_no_original_fallback(
        self,
    ):
        value = self.upload()
        url = value["thumbnail_url"]
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/webp")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-authenticated-account"], "superdan")
        self.assertEqual(self.client.head(url).headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.get(value["content_url"]).headers["cache-control"], "no-store"
        )
        for query in (
            "?rendition=original",
            "?rendition=thumb&rendition=preview",
            "?rendition=",
        ):
            rejected = self.client.get(value["content_url"] + query)
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(rejected.headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.get(url + "&expected_account=supervan").status_code, 409
        )
        self.login("supervan")
        foreign = self.client.get(url)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.headers["cache-control"], "no-store")

    def test_missing_thumbnail_never_reads_or_generates_original_on_get(self):
        self.setup_project()
        service = self.app.state.assets
        with mock.patch.object(service, "_prepare_renditions", return_value={}):
            response = self.client.post(
                "/v1/assets",
                data={"client_project_id": "story-one"},
                files={"file": ("old.png", png(), "image/png")},
            )
        value = response.json()
        self.assertFalse(value["rendition_ready"])
        self.assertNotIn("thumbnail_url", value)
        before = service.usage("superdan")
        with (
            mock.patch.object(
                service.store,
                "open",
                side_effect=AssertionError("No original fallback"),
            ),
            mock.patch.object(
                service,
                "ensure_image_renditions",
                side_effect=AssertionError("No GET write"),
            ),
        ):
            missing = self.client.get(value["content_url"] + "?rendition=thumb")
        self.assertEqual(
            (missing.status_code, missing.json()["detail"]),
            (404, "rendition_not_ready"),
        )
        self.assertEqual(service.usage("superdan"), before)

    def test_r2_redirect_auth_hop_stays_uncached_but_webp_object_signature_is_stable(
        self,
    ):
        self.setup_project()
        selected = config()
        fake = FakeMultipartS3(selected)
        store = S3ObjectStore(
            selected, S3Credentials("fake-access", "fake-secret"), client=fake
        )
        app = create_app(
            self.settings, repository=self.app.state.repository, storage=store
        )
        with TestClient(app) as client:
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            uploaded = client.post(
                "/v1/assets",
                data={"client_project_id": "story-one"},
                files={"file": ("display.png", png(), "image/png")},
            ).json()
            url = uploaded["thumbnail_url"]
            with mock.patch(
                "studio_platform.storage.time.time", return_value=1760572801
            ):
                one = client.get(url, follow_redirects=False)
            with mock.patch(
                "studio_platform.storage.time.time", return_value=1760572810
            ):
                two = client.get(url, follow_redirects=False)
            self.assertEqual(one.status_code, 307)
            self.assertEqual(one.headers["location"], two.headers["location"])
            self.assertEqual(one.headers["cache-control"], "no-store")
            self.assertEqual(one.headers["x-authenticated-account"], "superdan")
            self.assertEqual(one.headers["referrer-policy"], "no-referrer")
            self.assertEqual(
                parse_qs(urlsplit(one.headers["location"]).query)[
                    "response-cache-control"
                ],
                ["private, max-age=86400, immutable"],
            )
            client.post("/api/auth/logout")
            denied = client.get(url, follow_redirects=False)
            self.assertEqual(denied.status_code, 401)
            self.assertNotIn("location", denied.headers)

    def test_eight_actual_native_thumbnail_streams_run_together_and_release_leases(
        self,
    ):
        value = self.upload()
        service = self.app.state.assets
        key = service.get("superdan", value["id"])["thumb"]["key"]
        original = service.store.open
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        count = 0

        def held(selected):
            nonlocal count
            if selected == key:
                with lock:
                    count += 1
                    if count == 8:
                        entered.set()
                if not release.wait(4):
                    raise AssertionError("thumb readers serialized")
            return original(selected)

        with (
            mock.patch.object(service.store, "open", side_effect=held),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            pending = [
                pool.submit(self.client.get, value["thumbnail_url"]) for _ in range(8)
            ]
            try:
                self.assertTrue(
                    entered.wait(3),
                    "all eight owner-fenced thumbnail streams should start",
                )
            finally:
                release.set()
            self.assertEqual([item.result().status_code for item in pending], [200] * 8)
        self.assertFalse(self.app.state.request_admission.counts)

    def test_small_gate_allows_eight_readers_while_heavy_gate_stays_bounded(self):
        value = self.upload()
        admission = self.app.state.request_admission
        # Exercise independent media gates with a small fixture; production
        # defaults may be wider without removing either finite bound.
        admission.limits.update(owner_downloads=2, thumbnails=8)
        # Occupied original-media slots do not make tiny display copies queue
        # behind a full video stream. Owner/auth/global limits remain in force.
        self.assertTrue(admission.acquire("downloads"))
        self.assertTrue(admission.acquire("owner_downloads", "superdan"))
        self.assertTrue(admission.acquire("owner_downloads", "superdan"))
        try:
            self.assertEqual(self.client.get(value["preview_url"]).status_code, 429)
            self.assertEqual(self.client.get(value["thumbnail_url"]).status_code, 200)
            for _ in range(8):
                self.assertTrue(admission.acquire("thumbnails"))
            self.assertFalse(admission.acquire("thumbnails"))
            self.assertEqual(self.client.get(value["thumbnail_url"]).status_code, 429)
        finally:
            for _ in range(8):
                admission.release("thumbnails")
            admission.release("downloads")
            for _ in range(2):
                admission.release("owner_downloads", "superdan")
        self.assertFalse(admission.counts)


if __name__ == "__main__":
    unittest.main()
