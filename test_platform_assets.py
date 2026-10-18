"""Private assets and crash recovery using temporary DBs, CPU media and fake storage."""
from __future__ import annotations

import hashlib
import io
import json
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from PIL import Image
from sqlalchemy import create_engine, insert, inspect, select

from studio_platform import media
from studio_platform.assets import AssetService, AssetConflict, AssetNotFound, AssetQuotaExceeded, asset_table, metadata
from studio_platform.storage import (IntegrityError, LocalObjectStore, ObjectNotFound,
                                    S3ObjectStore, StorageWriteUncertain)
from studio_platform.storage_asset_journal import receipts, quotas
from studio_platform.storage_multipart import MultipartOutcomeUnknown
from test_platform_storage import OfflineTest, config
from test_platform_storage_multipart import FakeMultipartS3, ProcessStopped

MIB = 1024 * 1024


def png(color="navy"):
    output = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(output, format="PNG")
    return output.getvalue()


class FaultStore(LocalObjectStore):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []
        self.failure = None

    def put(self, key, source, **kwargs):
        self.calls.append(key)
        failure = self.failure
        if isinstance(failure, tuple):
            number, kind = failure
            if len(self.calls) != number:
                return super().put(key, source, **kwargs)
            failure = kind
        self.failure = None
        if failure == "before":
            raise StorageWriteUncertain(key)
        result = super().put(key, source, **kwargs)
        if failure == "after":
            raise StorageWriteUncertain(key)
        if failure == "crash":
            raise ProcessStopped()
        return result


class AssetTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine("sqlite:///" + (self.root / "assets.db").as_posix())
        self.addCleanup(self.engine.dispose)
        self.store = FaultStore(self.root / "objects")
        self.service = self.make_service()

    def make_service(self, **options):
        return AssetService(self.engine, self.store, self.root, max_bytes=2*MIB, **options)

    def upload(self, body=None, owner="owner", project="project", client="local-image"):
        return self.service.upload(owner, project, io.BytesIO(png() if body is None else body), "人像.png",
                                   client_asset_id=client)

    def only_asset(self):
        return self.service.list("owner", "project")[0]

    def test_ready_retries_reuse_asset_without_duplicate_objects_or_quota_growth(self):
        first = self.upload()
        used = self.service.usage("owner")["accounted_bytes"]
        second = self.upload()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.calls), 4)
        self.assertEqual(len(self.service.list("owner", "project")), 1)
        self.assertEqual(self.service.usage("owner")["accounted_bytes"], used)
        self.assertEqual(self.service.usage("owner")["active_uploads"], 0)
        restarted = self.make_service()
        self.assertEqual(restarted.resume("owner", first["id"])["status"], "ready")
        self.assertEqual(restarted.usage("owner")["accounted_bytes"], used)

    def test_frontend_composite_client_id_is_distinct_from_storage_paths(self):
        client_id = "shot-one:file_1234-abcd"
        first = self.upload(client=client_id)
        self.assertEqual(self.upload(client=client_id)["id"], first["id"])
        self.assertEqual(first["client_asset_id"], client_id)
        record = self.service.get("owner", first["id"])
        self.assertNotIn(client_id, record["original"]["key"])
        self.assertEqual(self.upload(client="CON")["status"], "ready")
        for bad in ("x"*129, "../escape", "x/y", "x\\y", "%2e%2e", "x\x00y", "x\ny"):
            with self.subTest(client_id=bad), self.assertRaises(ValueError):
                self.upload(client=bad)

    def test_http_frontend_client_id_upload_and_retry(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        from test_platform_api import project
        app = create_app(Settings(self.root / "http", auth_mode="local-test"))
        self.addCleanup(app.state.repository.close)
        # Windows asyncio constructs its internal socketpair over loopback.
        # Allow only that; the HTTP client itself uses in-process ASGI transport.
        self.network.stop()
        original_connect = socket.socket.connect
        def local_connect(sock, address):
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                return original_connect(sock, address)
            raise AssertionError("External networking forbidden")
        with mock.patch.object(socket.socket, "connect", local_connect), TestClient(app) as client:
            self.assertEqual(client.post("/api/auth/login", json={"username": "superdan"}).status_code, 200)
            self.assertEqual(client.post("/v1/projects", json={"project": project()}).status_code, 201)
            def upload(identifier):
                return client.post("/v1/assets", data={"client_project_id": "story-one", "client_asset_id": identifier},
                                   files={"file": ("reference.png", png(), "image/png")})
            identifier = "result-01234567-89ab-cdef-0123-456789abcdef:cloud_artifact_01234567-89ab-cdef-0123-456789abcdef"
            first = upload(identifier)
            self.assertEqual(first.status_code, 201, first.text)
            retry = upload(identifier)
            self.assertEqual(retry.status_code, 201, retry.text)
            self.assertEqual(first.json()["id"], retry.json()["id"])
            for bad in ("x"*129, "../escape", "file/key"):
                self.assertEqual(upload(bad).status_code, 422)

    def test_changed_content_under_same_client_id_is_a_conflict_not_old_asset(self):
        first = self.upload()
        with self.assertRaises(AssetConflict):
            self.upload(png("red"))
        self.assertEqual(len(self.store.calls), 4)
        with self.engine.connect() as conn:
            failed = conn.execute(select(asset_table.c.id).where(asset_table.c.status == "failed")).scalar_one()
        with self.assertRaises(AssetConflict):
            self.service.resume("owner", failed)
        self.assertEqual(self.service.get("owner", first["id"])["status"], "ready")

    def test_owner_and_project_separate_dedup_and_private_status(self):
        first = self.upload()
        other_owner = self.upload(owner="other")
        other_project = self.upload(project="other-project")
        self.assertEqual(len({a["id"] for a in (first, other_owner, other_project)}), 3)
        with self.assertRaises(AssetNotFound):
            self.service.resume("other", first["id"])
        serialized = json.dumps(first)
        for word in ("object_key", "owners/", "stage_id", "operation_id", "session_id", str(self.root)):
            self.assertNotIn(word, serialized)

    def test_lost_put_response_keeps_key_and_staging_then_recovers_without_rewrite(self):
        self.store.failure = "after"
        with self.assertRaises(StorageWriteUncertain) as caught:
            self.upload()
        asset = self.only_asset()
        self.assertEqual(asset["status"], "storage_unknown")
        receipt = self.service.journal.get("owner", asset["id"])
        spec = receipt["objects"]["original"]
        self.assertEqual(spec["key"], caught.exception.key)
        self.assertTrue(self.service._file(receipt, "source.png").exists())
        self.assertEqual(spec["sha256"], hashlib.sha256(png()).hexdigest())
        replay = self.upload()
        self.assertEqual(replay["id"], asset["id"])
        self.assertEqual(len(self.store.calls), 1)
        recovered = self.make_service().resume("owner", asset["id"])
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(len(self.store.calls), 4)  # Original, model and two display copies once each.

    def test_missing_unknown_object_never_causes_blind_retry(self):
        self.store.failure = "before"
        with self.assertRaises(StorageWriteUncertain):
            self.upload()
        asset = self.only_asset()
        self.assertEqual(self.service.usage("owner")["accounted_bytes"], 8*MIB)
        for _ in range(2):
            with self.assertRaises(ObjectNotFound):
                self.service.resume("owner", asset["id"])
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.service.usage("owner")["active_uploads"], 0)
        self.assertEqual(self.service.get("owner", asset["id"])["status"], "storage_unknown")

    def test_second_model_write_can_recover_without_reuploading_either_copy(self):
        self.store.failure = (2, "after")
        with self.assertRaises(StorageWriteUncertain):
            self.upload()
        asset = self.only_asset()
        internal = self.service.get("owner", asset["id"])
        self.assertIn("original", internal)
        self.assertNotIn("model", internal)
        self.assertEqual(self.make_service().resume("owner", asset["id"])["status"], "ready")
        self.assertEqual(len(self.store.calls), 4)

    def test_upload_receipt_and_quotas_are_added_without_mutating_legacy_schema(self):
        columns = {column["name"] for column in inspect(self.engine).get_columns("platform_assets")}
        self.assertEqual(columns, {"id", "tenant", "owner", "project_id", "status", "created", "record"})
        self.assertIn("platform_asset_storage_quota", inspect(self.engine).get_table_names())

    def test_recovery_cannot_switch_the_storage_root(self):
        self.store.failure = "before"
        with self.assertRaises(StorageWriteUncertain):
            self.upload()
        ident = self.only_asset()["id"]
        self.store = FaultStore(self.root / "different-objects")
        with self.assertRaises(AssetConflict):
            self.make_service().resume("owner", ident)
        self.assertEqual(self.store.calls, [])

    def test_crash_requires_fencing_then_uses_the_recorded_key(self):
        self.store.failure = "crash"
        with self.assertRaises(ProcessStopped):
            self.upload()
        asset = self.only_asset()
        self.assertEqual(self.service.usage("owner")["active_uploads"], 1)
        resumed = self.make_service()
        with self.assertRaises(AssetConflict):
            resumed.resume("owner", asset["id"])
        self.assertEqual(resumed.reconcile("owner", asset["id"], interrupted=True)["status"], "ready")
        self.assertEqual(len(self.store.calls), 4)
        self.assertEqual(resumed.usage("owner")["active_uploads"], 0)

    def test_cpu_preparation_failure_preserves_source_and_can_resume(self):
        with mock.patch.object(media, "normalize", side_effect=media.MediaError("temporary CPU failure")):
            with self.assertRaises(media.MediaError):
                self.upload()
        asset = self.only_asset()
        receipt = self.service.journal.get("owner", asset["id"])
        self.assertEqual(self.service._file(receipt, "source.png").read_bytes(), png())
        self.assertEqual(self.service.usage("owner")["accounted_bytes"], len(png()))
        self.assertEqual(self.service.resume("owner", asset["id"])["status"], "ready")

    def test_retained_source_mutation_fails_closed(self):
        with mock.patch.object(media, "normalize", side_effect=media.MediaError("temporary")):
            with self.assertRaises(media.MediaError):
                self.upload()
        asset = self.only_asset()
        receipt = self.service.journal.get("owner", asset["id"])
        self.service._file(receipt, "source.png").write_bytes(png("red"))
        with self.assertRaises(IntegrityError):
            self.service.resume("owner", asset["id"])
        self.assertEqual(self.store.calls, [])

    def test_atomic_quota_rejects_before_receiving_body(self):
        service = self.make_service(owner_quota_bytes=4*MIB)
        source = mock.Mock()
        with self.assertRaises(AssetQuotaExceeded):
            service.upload("owner", "project", source, "source.png")
        source.read.assert_not_called()
        self.assertEqual(service.usage("owner")["accounted_bytes"], 0)
        self.assertEqual(service.usage("owner")["active_uploads"], 0)

    def test_global_and_per_owner_concurrency_are_durable(self):
        self.service = self.make_service(max_active_per_owner=1, max_active_total=1)
        entered, release = threading.Event(), threading.Event()
        class SlowSource(io.BytesIO):
            def read(self, n=-1):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test barrier expired")
                return super().read(n)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self.service.upload, "owner", "project", SlowSource(png()), "source.png")
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(AssetQuotaExceeded):
                    self.upload(owner="other")
                with self.assertRaises(AssetQuotaExceeded):
                    self.upload()
            finally:
                release.set()
            self.assertEqual(future.result()["status"], "ready")
        self.assertEqual(self.service.usage("owner")["active_uploads"], 0)

    def test_concurrent_same_input_claims_one_asset(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: self.upload(), range(2)))
        self.assertEqual(result[0]["id"], result[1]["id"])
        self.assertEqual(len(self.store.calls), 4)
        self.assertEqual(len(self.service.list("owner", "project")), 1)
        self.assertEqual(self.service.usage("owner")["active_uploads"], 0)

    def test_s3_large_object_uses_durable_multipart_and_unknown_completion_recovers(self):
        selected = config(max_single_put_bytes=100)
        fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=fake)
        self.service = self.make_service()
        fake.failure = ("complete", "after")
        with self.assertRaises(MultipartOutcomeUnknown):
            self.upload()
        asset = self.only_asset()
        self.assertEqual(asset["status"], "storage_unknown")
        result = self.make_service().resume("owner", asset["id"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(sum(op == "create" for op, _ in fake.calls), 4)
        self.assertEqual(sum(op == "complete" for op, _ in fake.calls), 4)
        self.assertEqual(sum(op == "put" for op, _ in fake.calls), 0)

    def test_existing_assets_are_not_recreated_and_known_legacy_bytes_are_accounted(self):
        # Separate temporary DB represents an existing preview schema with no new tables.
        engine = create_engine("sqlite:///" + (self.root / "legacy.db").as_posix())
        self.addCleanup(engine.dispose)
        metadata.create_all(engine)
        raw = png()
        original = self.store.write_new("owner", "legacy", io.BytesIO(raw), filename="source.png", content_type="image/png")
        from dataclasses import asdict
        asset = dict(id="legacy", asset_id="legacy", project_id="project", client_asset_id="local-image",
            status="ready", file_name="legacy.png", kind="image", mime="image/png", created_at=1,
            metadata={"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}, original=asdict(original))
        with engine.begin() as conn:
            conn.execute(insert(asset_table).values(id="legacy", tenant="sixnine", owner="owner",
                project_id="project", status="ready", created=1, record=json.dumps(asset)))
        service = AssetService(engine, self.store, self.root, max_bytes=2*MIB)
        self.assertEqual(service.usage("owner")["accounted_bytes"], len(raw))
        result = service.upload("owner", "project", io.BytesIO(raw), "same.png", client_asset_id="local-image")
        self.assertEqual(result["id"], "legacy")
        self.assertEqual(service.get("owner", "legacy"), asset)
        self.assertIn("platform_asset_upload_receipts", inspect(engine).get_table_names())
        self.assertEqual(service.usage("owner")["accounted_bytes"], len(raw))


if __name__ == "__main__":
    unittest.main()
