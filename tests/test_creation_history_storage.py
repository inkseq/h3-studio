"""Isolated original ledgers/fake R2; no credentials, network or generation."""

import io
import copy
import hashlib
import json
from pathlib import Path
import socket
import uuid
from unittest import mock

from PIL import Image
from sqlalchemy import insert, select, delete

from studio_platform.artifact_writer import ArtifactWriter, write_receipts
from studio_platform.assets import AssetService, asset_table
from studio_platform.content_lifecycle import owner_reference_gate, put_tombstone
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    artifacts,
    budget_accounts,
    budget_reservations,
    documents,
)
from studio_platform.storage import S3ObjectStore, StorageWriteUncertain
from studio_platform.storage_asset_journal import artifact_accounting, receipts
from studio_platform.storage_schema import create_storage_schema
from test_platform_repository import LedgerCase
from test_platform_storage import config
from test_platform_storage_multipart import FakeMultipartS3
from test_platform_assets import png


class CreationHistoryStorageTests(LedgerCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("No network")
        )
        patch.start()
        self.addCleanup(patch.stop)
        selected = config()
        self.fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=self.fake)
        root = Path(self.temp.name)
        self.assets = AssetService(
            self.repo.engine,
            self.store,
            root / "asset-data",
            tenant=self.scope.tenant_id,
            max_bytes=2 * 1024 * 1024,
        )
        self.writer = ArtifactWriter(
            self.repo.engine,
            self.store,
            root / "collection",
            tenant=self.scope.tenant_id,
            max_object_bytes=2 * 1024 * 1024,
        )
        from studio_platform.quick_chat import metadata

        create_storage_schema(self.repo.engine, metadata)

    def upload(self, client="original"):
        return self.assets.upload(
            self.scope.owner_id,
            self.scope.project_id,
            io.BytesIO(png()),
            "source.png",
            client_asset_id=client,
        )

    def history_state(self, state):
        from studio_platform.quick_chat import objects

        with self.repo.transaction() as conn:
            conn.execute(delete(objects).where(objects.c.id == "historical-parent"))
            conn.execute(
                insert(objects).values(
                    id="historical-parent",
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    session_id="historical-parent",
                    kind="session",
                    version=1,
                    payload={
                        "project_id": self.scope.project_id,
                        "history": {"trash_state": state},
                    },
                    created_at=self.now,
                    updated_at=self.now,
                )
            )

    def test_readable_trashed_history_backfills_only_displays_and_preserves_writes(
        self,
    ):
        with mock.patch.object(self.assets, "_prepare_renditions", return_value={}):
            uploaded = self.upload()
        original = self.assets.get(self.scope.owner_id, uploaded["id"])
        receipt = self.assets.journal.get(self.scope.owner_id, uploaded["id"])
        financial, objects = self.financial_snapshot(), copy.deepcopy(self.fake.objects)
        self.history_state("trashed")
        self.assertEqual(self.assets.get(self.scope.owner_id, uploaded["id"]), original)
        with self.assertRaisesRegex(Conflict, "quick_chat_session_not_active"):
            self.assets.journal.save(receipt)
        with self.assertRaisesRegex(Conflict, "quick_chat_session_not_active"):
            self.upload(client="fresh-upload")
        result = self.assets.ensure_renditions(
            self.scope.owner_id, uploaded["id"], expected_version=receipt["version"]
        )
        self.assertTrue(result["rendition_ready"])
        current = self.assets.get(self.scope.owner_id, uploaded["id"])
        for role in ("original", "model"):
            self.assertEqual(current[role], original[role])
        for key, value in objects.items():
            self.assertEqual(self.fake.objects[key], value)
        for role in ("thumb", "preview"):
            spec = current[role]
            with self.store.open(spec["key"]) as stream:
                body = stream.read()
            self.assertEqual(hashlib.sha256(body).hexdigest(), spec["sha256"])
            with Image.open(io.BytesIO(body)) as image:
                image.load()
                self.assertEqual(image.format, "WEBP")
        self.assertEqual(self.financial_snapshot(), financial)
        usage = self.assets.usage(self.scope.owner_id)
        shortened = self.assets.journal.get(self.scope.owner_id, uploaded["id"])
        delta = -current["thumb"]["size_bytes"]
        shortened["reserved"] += delta
        with self.assertRaises(Conflict):
            self.assets.journal.save_renditions(shortened, byte_delta=delta)
        self.assertEqual(self.assets.usage(self.scope.owner_id), usage)
        with self.assertRaisesRegex(Conflict, "rendition_receipt_version_changed"):
            self.assets.ensure_renditions(
                self.scope.owner_id, uploaded["id"], expected_version=receipt["version"]
            )
        self.assertEqual(
            self.assets.ensure_renditions(self.scope.owner_id, uploaded["id"]), result
        )
        self.assertEqual(self.assets.usage(self.scope.owner_id), usage)

    def test_display_save_refuses_original_identity_operation_and_quota_tampering(self):
        with mock.patch.object(self.assets, "_prepare_renditions", return_value={}):
            uploaded = self.upload()
        receipt = self.assets.journal.get(self.scope.owner_id, uploaded["id"])
        self.history_state("trashed")
        before = self.assets.usage(self.scope.owner_id)
        for field in ("original", "binding", "operation", "quota", "owner", "tenant"):
            with self.subTest(field=field):
                value = copy.deepcopy(receipt)
                if field == "original":
                    value["asset"]["original"]["sha256"] = "0" * 64
                elif field == "binding":
                    value["storage_binding"] = "0" * 64
                elif field == "operation":
                    value["operation_id"] = "0" * 32
                elif field == "owner":
                    value["owner"] = self.other.owner_id
                elif field == "tenant":
                    other = copy.copy(self.assets.journal)
                    other.tenant = "foreign-tenant"
                    with self.assertRaises(Conflict):
                        other.save_renditions(value)
                    continue
                else:
                    value["reserved"] += 1
                with self.assertRaises(Conflict):
                    self.assets.journal.save_renditions(value)
        self.assertEqual(self.assets.usage(self.scope.owner_id), before)
        unclaimed = copy.deepcopy(receipt)
        unclaimed["busy"] = True
        with self.assertRaises(Conflict):
            self.assets.journal.save_renditions(unclaimed, active_delta=1)
        self.assertEqual(self.assets.usage(self.scope.owner_id), before)
        self.assertEqual(
            self.assets.journal.get(self.scope.owner_id, uploaded["id"]), receipt
        )

    def copied_local_asset(self):
        from studio_platform.storage import LocalObjectStore
        from studio_platform.storage_migration import create_plan, apply_plan

        root = Path(self.temp.name)
        local = LocalObjectStore(root / "retained-original-local")
        source_service = AssetService(
            self.repo.engine,
            local,
            root / "asset-data",
            tenant=self.scope.tenant_id,
            max_bytes=2 * 1024 * 1024,
        )
        with mock.patch.object(source_service, "_prepare_renditions", return_value={}):
            uploaded = source_service.upload(
                self.scope.owner_id,
                self.scope.project_id,
                io.BytesIO(png()),
                "source.png",
            )
        original = source_service.get(self.scope.owner_id, uploaded["id"])
        receipt = source_service.journal.get(self.scope.owner_id, uploaded["id"])
        plan_root = root / "original-copy"
        create_plan(local.root, self.store.config, plan_root)
        apply_plan(plan_root, self.store)
        return uploaded, original, receipt, plan_root

    def test_copied_local_original_keeps_binding_and_publishes_only_verified_r2_displays(
        self,
    ):
        uploaded, original, receipt, plan_root = self.copied_local_asset()
        root = Path(self.temp.name)
        copied = copy.deepcopy(self.fake.objects)
        financial = self.financial_snapshot()
        self.history_state("trashed")
        with self.assertRaisesRegex(Conflict, "rendition_original_receipt_unconfirmed"):
            self.assets.ensure_renditions(
                self.scope.owner_id, uploaded["id"], expected_version=receipt["version"]
            )
        save = self.assets.journal.save_renditions

        def after_verified_put(value, **kwargs):
            for role in ("thumb", "preview"):
                if role in value["asset"]:
                    self.assertIn(value["asset"][role]["key"], self.fake.objects)
            return save(value, **kwargs)

        put = self.store.put
        lost = []

        def lost_ack(key, source, **kwargs):
            value = put(key, source, **kwargs)
            if not lost:
                lost.append(key)
                raise StorageWriteUncertain(key)
            return value

        with mock.patch.object(
            self.assets.journal, "save_renditions", side_effect=after_verified_put
        ):
            with (
                mock.patch.object(self.store, "put", side_effect=lost_ack),
                self.assertRaises(StorageWriteUncertain),
            ):
                self.assets.ensure_renditions(
                    self.scope.owner_id,
                    uploaded["id"],
                    expected_version=receipt["version"],
                    original_copy_receipts=plan_root,
                )
            pending = self.assets.journal.get(self.scope.owner_id, uploaded["id"])
            self.assertEqual(pending["objects"]["thumb"]["key"], lost[0])
            result = self.assets.ensure_renditions(
                self.scope.owner_id,
                uploaded["id"],
                expected_version=pending["version"],
                original_copy_receipts=plan_root,
            )
        self.assertTrue(result["rendition_ready"])
        current = self.assets.journal.get(self.scope.owner_id, uploaded["id"])
        self.assertEqual(current["storage_binding"], receipt["storage_binding"])
        self.assertEqual(
            current["rendition_backfill"]["target_binding"], self.assets.storage_binding
        )
        for role in ("original", "model"):
            self.assertEqual(current["asset"][role], original[role])
            self.assertEqual(current["objects"][role], receipt["objects"][role])
        for key, value in copied.items():
            self.assertEqual(self.fake.objects[key], value)
        self.assertEqual(len(self.fake.objects) - len(copied), 2)
        self.assertEqual(
            sum(
                name == "put" and details["Key"] == lost[0]
                for name, details in self.fake.calls
            ),
            1,
        )
        self.assertEqual(self.financial_snapshot(), financial)
        calls, usage = len(self.fake.calls), self.assets.usage(self.scope.owner_id)
        self.assets.ensure_renditions(
            self.scope.owner_id, uploaded["id"], original_copy_receipts=plan_root
        )
        self.assertEqual(self.assets.usage(self.scope.owner_id), usage)
        self.assertFalse(any(name == "put" for name, _ in self.fake.calls[calls:]))
        from contextlib import redirect_stdout
        from studio_platform.rendition_backfill import main
        from studio_platform.settings import Settings
        from studio_platform.storage_config import S3Credentials

        selected = self.store.config
        settings = Settings(
            root / "asset-data",
            tenant_id=self.scope.tenant_id,
            database_url=self.url,
            max_upload_bytes=2 * 1024 * 1024,
            storage_provider="r2",
            storage_endpoint=selected.endpoint_url,
            storage_region=selected.region,
            storage_bucket=selected.bucket,
            storage_profile=selected.profile,
        )
        with (
            mock.patch.object(Settings, "from_environment", return_value=settings),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(
                    [
                        "apply",
                        "--owner",
                        self.scope.owner_id,
                        "--asset-id",
                        uploaded["id"],
                        "--receipt-version",
                        str(
                            self.assets.journal.get(
                                self.scope.owner_id, uploaded["id"]
                            )["version"]
                        ),
                        "--original-copy-receipts",
                        str(plan_root),
                    ],
                    credentials_loader=lambda *args, **kwargs: S3Credentials(
                        "fixture-access", "fixture-secret"
                    ),
                    store_factory=lambda *args: self.store,
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["state"], "complete")
        self.assertEqual(self.assets.usage(self.scope.owner_id), usage)
        copy_file = plan_root / "copy-receipt.json"
        changed = json.loads(copy_file.read_bytes())
        changed["verified_at"] += 1
        copy_file.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(Conflict, "rendition_copy_target_changed"):
            self.assets.ensure_renditions(
                self.scope.owner_id, uploaded["id"], original_copy_receipts=plan_root
            )
        self.assertEqual(self.assets.usage(self.scope.owner_id), usage)

    def test_copied_pending_r2_key_tombstone_fences_the_old_local_receipt(self):
        uploaded, _, receipt, plan_root = self.copied_local_asset()
        self.history_state("trashed")
        put = self.store.put

        def lost_ack(key, source, **kwargs):
            put(key, source, **kwargs)
            raise StorageWriteUncertain(key)

        with (
            mock.patch.object(self.store, "put", side_effect=lost_ack),
            self.assertRaises(StorageWriteUncertain),
        ):
            self.assets.ensure_renditions(
                self.scope.owner_id,
                uploaded["id"],
                expected_version=receipt["version"],
                original_copy_receipts=plan_root,
            )
        pending = self.assets.journal.get(self.scope.owner_id, uploaded["id"])
        usage = self.assets.usage(self.scope.owner_id)
        key = pending["objects"]["thumb"]["key"]
        self.tombstones(specs=[{"key": key}], binding=self.assets.storage_binding)
        with (
            mock.patch.object(
                self.store, "put", side_effect=AssertionError("No post-tombstone PUT")
            ),
            self.assertRaisesRegex(Conflict, "content_purging_or_purged"),
        ):
            self.assets.ensure_renditions(
                self.scope.owner_id,
                uploaded["id"],
                expected_version=pending["version"],
                original_copy_receipts=plan_root,
            )
        self.assertEqual(self.assets.usage(self.scope.owner_id), usage)
        # Private audit access confirms the unknown write/accounting was retained.
        self.assertEqual(
            self.assets.journal.get(
                self.scope.owner_id, uploaded["id"], include_deleted=True
            ),
            pending,
        )

    def test_display_update_keeps_real_trash_purge_and_unconfirmed_parent_fences(self):
        with mock.patch.object(self.assets, "_prepare_renditions", return_value={}):
            uploaded = self.upload()
        receipt = self.assets.journal.get(self.scope.owner_id, uploaded["id"])
        for state in ("purging", "purged", "unknown"):
            self.history_state(state)
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(Conflict, "quick_chat_session_not_active"),
            ):
                self.assets.journal.save_renditions(copy.deepcopy(receipt))
        self.history_state("trashed")
        from studio_platform.universal_trash import target_key

        target = dict(
            kind="project", project_id=self.scope.project_id, id=self.scope.project_id
        )
        with self.repo.transaction() as conn:
            conn.execute(
                insert(documents).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=self.scope.owner_id,
                    project_id="__trash",
                    kind="trash_item",
                    document_id=target_key(target),
                    version=1,
                    payload=dict(target=target, state="trashed"),
                    updated_at=self.now,
                )
            )
        with self.assertRaisesRegex(Conflict, "project_in_trash"):
            self.assets.journal.save_renditions(copy.deepcopy(receipt))
        with self.repo.transaction() as conn:
            conn.execute(delete(documents).where(documents.c.kind == "trash_item"))
            asset_target = dict(
                kind="asset", project_id=self.scope.project_id, id=uploaded["id"]
            )
            conn.execute(
                insert(documents).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=self.scope.owner_id,
                    project_id="__trash",
                    kind="trash_item",
                    document_id=target_key(asset_target),
                    version=1,
                    payload=dict(target=asset_target, state="trashed"),
                    updated_at=self.now,
                )
            )
        with self.assertRaisesRegex(Conflict, "content_in_trash"):
            self.assets.journal.save_renditions(copy.deepcopy(receipt))
        from studio_platform.quick_chat import objects as chat_objects

        with self.repo.transaction() as conn:
            conn.execute(delete(documents).where(documents.c.kind == "trash_item"))
            conn.execute(
                delete(chat_objects).where(chat_objects.c.id == "historical-parent")
            )
            conn.execute(
                insert(documents).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=self.scope.owner_id,
                    project_id="__projects",
                    kind="project",
                    document_id=self.scope.project_id,
                    version=1,
                    payload=dict(integration_kind="quick_chat"),
                    updated_at=self.now,
                )
            )
        with self.assertRaisesRegex(Conflict, "quick_chat_session_binding_unconfirmed"):
            self.assets.journal.save_renditions(copy.deepcopy(receipt))
        with self.repo.transaction() as conn:
            conn.execute(
                delete(documents).where(documents.c.project_id == "__projects")
            )
        self.tombstones(asset_ids=[uploaded["id"]])
        with self.assertRaisesRegex(Conflict, "content_purging_or_purged"):
            self.assets.journal.save_renditions(copy.deepcopy(receipt))

    def tombstones(self, *, asset_ids=(), artifact_ids=(), specs=(), binding=None):
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.scope.tenant_id, self.scope.owner_id)
            for kind, values in (("asset", asset_ids), ("artifact", artifact_ids)):
                for value in values:
                    put_tombstone(
                        conn,
                        self.scope.tenant_id,
                        self.scope.owner_id,
                        kind=kind,
                        value=value,
                        action_id="original-purge",
                        now=self.now,
                    )
            for spec in specs:
                put_tombstone(
                    conn,
                    self.scope.tenant_id,
                    self.scope.owner_id,
                    kind="key",
                    value=spec.get("object_key", spec.get("key")),
                    storage_binding=binding,
                    action_id="original-purge",
                    now=self.now,
                )

    def financial_snapshot(self):
        with self.repo.engine.connect() as conn:
            return (
                list(conn.execute(select(budget_accounts)).mappings()),
                list(conn.execute(select(budget_reservations)).mappings()),
            )

    def prepared(self, *, settled=False):
        self.job(cost=100)
        queue = TaskQueue(self.repo)
        lease = queue.claim("original-worker", "test-pool").lease
        queue.begin_submission(lease)
        queue.record_submitted(lease, "original-task")
        job = queue.begin_collection(lease)
        tag = "attempt-" + uuid.uuid4().hex
        directory = self.writer.work_dir / tag
        directory.mkdir(parents=True)
        path = directory / "verified.mp4"
        path.write_bytes(b"validated original output")
        record = self.writer.prepare(
            job, lease.attempt_id, tag, [("video", path, "video/mp4", {})]
        )
        specs = self.writer.write(record)
        if settled:
            queue.complete(
                lease,
                specs,
                actual_cost_microusd=100,
                settlement=self.writer.settlement(record),
            )
            self.writer.cleanup_settled(job, lease.attempt_id, tag)
            record = self.writer.get(job, lease.attempt_id, tag)
        return queue, job, lease, tag, record, specs

    def test_ready_asset_cached_save_resume_and_client_replay_cannot_resurrect(self):
        original = self.upload()
        cached = self.assets.journal.get(self.scope.owner_id, original["id"])
        before = self.assets.usage(self.scope.owner_id)
        financial = self.financial_snapshot()
        keys = set(self.fake.objects)
        self.tombstones(asset_ids=[original["id"]])
        with mock.patch.object(
            self.store, "put", side_effect=AssertionError("No republish")
        ):
            for action in (
                lambda: self.assets.get(self.scope.owner_id, original["id"]),
                lambda: self.assets.resume(self.scope.owner_id, original["id"]),
                lambda: self.assets.journal.save(cached, byte_delta=-1),
                lambda: self.upload(),
            ):
                with self.assertRaisesRegex(Conflict, "content_purging_or_purged"):
                    action()
        visible = self.assets.list(self.scope.owner_id, self.scope.project_id)
        self.assertNotIn(original["id"], [row["id"] for row in visible])
        self.assertFalse(any(row["status"] == "ready" for row in visible))
        self.assertEqual(
            self.assets.journal.get(
                self.scope.owner_id, original["id"], include_deleted=True
            )["asset"]["status"],
            "ready",
        )
        # The rejected fresh duplicate retains its receive reservation/receipt;
        # it cannot consume the purged original's accounting or rewrite history.
        self.assertGreaterEqual(
            self.assets.usage(self.scope.owner_id)["accounted_bytes"],
            before["accounted_bytes"],
        )
        self.assertEqual(set(self.fake.objects), keys)
        self.assertEqual(self.financial_snapshot(), financial)

    def test_source_artifact_key_tombstone_fences_cached_frame_or_derivation(self):
        original = self.upload()
        cached = self.assets.journal.get(self.scope.owner_id, original["id"])
        source_key = "owners/superdan/assets/original-source/result.mp4"
        cached["frame_extraction"] = {
            "original": {"key": source_key},
            "phase": "planned",
        }
        self.tombstones(
            specs=[{"key": source_key}], binding=self.assets.storage_binding
        )
        before = self.assets.usage(self.scope.owner_id)
        with self.assertRaisesRegex(Conflict, "content_purging_or_purged"):
            self.assets.journal.save(cached)
        self.assertEqual(self.assets.usage(self.scope.owner_id), before)

    def test_cached_settled_collection_refuses_before_any_storage_or_signing(self):
        _, job, lease, tag, record, specs = self.prepared(settled=True)
        before = self.assets.usage(self.scope.owner_id)
        financial = self.financial_snapshot()
        self.tombstones(specs=specs, binding=self.writer.binding)
        with (
            mock.patch.object(
                self.store, "stat", side_effect=AssertionError("No storage read")
            ),
            mock.patch.object(
                self.store,
                "presign_artifact_upload",
                side_effect=AssertionError("No signing"),
            ),
        ):
            for action in (
                lambda: self.writer.get(job, lease.attempt_id, tag),
                lambda: self.writer.write(record),
                lambda: self.writer._save(record, byte_delta=-1),
            ):
                with self.assertRaisesRegex(Conflict, "content_purging_or_purged"):
                    action()
        self.assertEqual(self.assets.usage(self.scope.owner_id), before)
        self.assertEqual(self.financial_snapshot(), financial)

    def test_gpu_cached_target_renewal_and_readback_are_fenced_before_capabilities(
        self,
    ):
        import test_platform_gpu_r2_collection as gpu_fixture

        self.work = self.writer.work_dir
        _, job, lease, tag, task, description, _, _ = (
            gpu_fixture.GPUCollectionTests.fixture(self)
        )
        self.writer.begin_remote(job, lease.attempt_id, tag, kinds=("video", "audio"))
        record = self.writer.prepare_remote(
            job, lease.attempt_id, tag, task, description
        )
        self.tombstones(
            specs=[{"key": role["key"]} for role in record["roles"].values()],
            binding=self.writer.binding,
        )
        before = self.assets.usage(self.scope.owner_id)
        with (
            mock.patch.object(
                self.store, "stat", side_effect=AssertionError("No remote read")
            ),
            mock.patch.object(
                self.store,
                "presign_artifact_upload",
                side_effect=AssertionError("No capability"),
            ),
        ):
            for action in (
                lambda: self.writer.remote_targets(record, upload_stopped=True),
                lambda: self.writer.verify_remote(record),
            ):
                with self.assertRaisesRegex(Conflict, "content_purging_or_purged"):
                    action()
        self.assertEqual(self.assets.usage(self.scope.owner_id), before)

    def test_purge_between_verified_put_and_completion_rolls_back_result_and_money(
        self,
    ):
        queue, job, lease, _, record, specs = self.prepared()
        before = self.financial_snapshot()
        usage = self.assets.usage(self.scope.owner_id)
        self.tombstones(specs=specs, binding=self.writer.binding)
        with self.assertRaisesRegex(Conflict, "content_purging_or_purged"):
            queue.complete(
                lease,
                specs,
                actual_cost_microusd=100,
                settlement=self.writer.settlement(record),
            )
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    select(artifacts).where(artifacts.c.job_id == job["id"])
                ).all(),
                [],
            )
            self.assertEqual(
                json.loads(
                    conn.execute(
                        select(write_receipts.c.record).where(
                            write_receipts.c.id == record["id"]
                        )
                    ).scalar_one()
                )["phase"],
                "reserved",
            )
        self.assertEqual(self.financial_snapshot(), before)
        self.assertEqual(self.assets.usage(self.scope.owner_id), usage)

    def test_asset_purge_releases_only_remote_bytes_and_retains_original_receipt(self):
        original = self.upload()
        receipt = self.assets.journal.get(self.scope.owner_id, original["id"])
        specs = list(receipt["objects"].values())
        before = self.assets.usage(self.scope.owner_id)["accounted_bytes"]
        financial = self.financial_snapshot()
        self.tombstones(
            asset_ids=[original["id"]], specs=specs, binding=self.assets.storage_binding
        )
        # Simulate the lifecycle's already-confirmed remote absence. The
        # finalizer must never inspect/delete storage itself.
        with mock.patch.object(
            self.store, "stat", side_effect=AssertionError("No IO in final transaction")
        ):
            with self.repo.transaction() as conn:
                result = self.assets.finalize_purge(
                    conn,
                    self.scope.owner_id,
                    asset_ids=[original["id"]],
                    artifact_specs=[],
                    now=self.now,
                )
        self.assertEqual(
            result["released_bytes"], sum(spec["size_bytes"] for spec in specs)
        )
        self.assertEqual(
            self.assets.usage(self.scope.owner_id)["accounted_bytes"],
            before - result["released_bytes"],
        )
        with self.repo.engine.connect() as conn:
            self.assertIsNone(
                conn.execute(
                    select(asset_table).where(asset_table.c.id == original["id"])
                ).first()
            )
            self.assertIsNotNone(
                conn.execute(
                    select(receipts).where(receipts.c.id == original["id"])
                ).first()
            )
        self.assertEqual(self.financial_snapshot(), financial)

    def test_artifact_purge_preserves_accounting_markers_and_backfill_never_recharges(
        self,
    ):
        _, job, lease, _, record, specs = self.prepared(settled=True)
        with self.repo.engine.connect() as conn:
            artifact_id = conn.execute(
                select(artifacts.c.id).where(artifacts.c.job_id == job["id"])
            ).scalar_one()
            markers = list(conn.execute(select(artifact_accounting)).mappings())
            raw_receipt = conn.execute(
                select(write_receipts.c.record).where(
                    write_receipts.c.id == record["id"]
                )
            ).scalar_one()
        before = self.assets.usage(self.scope.owner_id)["accounted_bytes"]
        financial = self.financial_snapshot()
        self.tombstones(
            artifact_ids=[artifact_id], specs=specs, binding=self.writer.binding
        )
        with self.repo.transaction() as conn:
            self.assets.finalize_purge(
                conn,
                self.scope.owner_id,
                asset_ids=[],
                artifact_specs=[
                    {**spec, "storage_binding": self.writer.binding} for spec in specs
                ],
                now=self.now,
            )
        self.assertEqual(
            self.assets.usage(self.scope.owner_id)["accounted_bytes"],
            before - sum(s["size_bytes"] for s in specs),
        )
        # Even a missing old marker is not evidence to recreate deleted bytes.
        from sqlalchemy import delete

        with self.repo.transaction() as conn:
            conn.execute(delete(artifact_accounting))
        self.assertEqual(self.assets.usage(self.scope.owner_id)["accounted_bytes"], 0)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(artifact_accounting)).all(), [])
            self.assertEqual(
                conn.execute(
                    select(write_receipts.c.record).where(
                        write_receipts.c.id == record["id"]
                    )
                ).scalar_one(),
                raw_receipt,
            )
        self.assertEqual(len(markers), 2)
        self.assertEqual(self.financial_snapshot(), financial)

    def test_finalizer_rejects_unbound_or_wrong_hash_before_any_accounting_changes(
        self,
    ):
        _, _, _, _, _, specs = self.prepared(settled=True)
        self.tombstones(specs=specs, binding=self.writer.binding)
        before = self.assets.usage(self.scope.owner_id)
        for change in (
            {"storage_binding": self.assets.storage_binding},
            {"sha256": "0" * 64},
            {"size_bytes": specs[0]["size_bytes"] + 1},
        ):
            with self.subTest(change=change), self.assertRaises(Conflict):
                with self.repo.transaction() as conn:
                    self.assets.finalize_purge(
                        conn,
                        self.scope.owner_id,
                        asset_ids=[],
                        artifact_specs=[
                            {
                                **specs[0],
                                "storage_binding": self.writer.binding,
                                **change,
                            }
                        ],
                        now=self.now,
                    )
            self.assertEqual(self.assets.usage(self.scope.owner_id), before)

    def test_owner_tombstone_does_not_hide_another_owner_or_shared_ready_reads(self):
        original = self.upload()
        with self.repo.transaction() as conn:
            put_tombstone(
                conn,
                self.scope.tenant_id,
                self.other.owner_id,
                kind="asset",
                value=original["id"],
                action_id="other-action",
                now=self.now,
            )
        self.assertEqual(
            self.assets.get(self.scope.owner_id, original["id"])["status"], "ready"
        )
        # Trash alone is not physical deletion: another story can still use a
        # shared ready asset. New writes to the trashed project are refused.
        from studio_platform.quick_chat import objects

        with self.repo.transaction() as conn:
            conn.execute(
                insert(objects).values(
                    id="session-shared",
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    session_id="session-shared",
                    kind="session",
                    version=1,
                    payload={
                        "project_id": self.scope.project_id,
                        "history": {"trash_state": "trashed"},
                    },
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        self.assertEqual(
            self.assets.get(self.scope.owner_id, original["id"])["status"], "ready"
        )
        with self.assertRaisesRegex(Conflict, "quick_chat_session_not_active"):
            self.upload(client="new-in-trash")

    def test_nested_read_only_asset_and_receipt_lookups_do_not_reacquire_owner_gate(
        self,
    ):
        original = self.upload()
        cached = self.assets.journal.get(self.scope.owner_id, original["id"])
        _, job, lease, tag, _, _ = self.prepared(settled=True)
        # Quick Chat freezes an original turn under this gate before reading
        # material snapshots. A second-connection exclusive gate deadlocks.
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.scope.tenant_id, self.scope.owner_id)
            self.assertEqual(
                self.assets.get(self.scope.owner_id, original["id"])["id"],
                original["id"],
            )
            self.assertEqual(
                self.assets.list(self.scope.owner_id, self.scope.project_id)[0]["id"],
                original["id"],
            )
            self.assertEqual(
                self.assets.journal.get(self.scope.owner_id, original["id"])["id"],
                original["id"],
            )
            self.assertIsNone(
                self.assets.journal.legacy_match(cached, "no-legacy-match")
            )
            self.assertEqual(
                self.writer.get(job, lease.attempt_id, tag)["phase"], "settled"
            )
