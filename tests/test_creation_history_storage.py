"""Isolated original ledgers/fake R2; no credentials, network or generation."""

import io
import json
from pathlib import Path
import socket
import uuid
from unittest import mock

from sqlalchemy import insert, select

from studio_platform.artifact_writer import ArtifactWriter, write_receipts
from studio_platform.assets import AssetService, asset_table
from studio_platform.content_lifecycle import owner_reference_gate, put_tombstone
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    artifacts,
    budget_accounts,
    budget_reservations,
)
from studio_platform.storage import S3ObjectStore
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
