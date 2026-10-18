"""Private temporary ledgers and fake storage only; never contacts S3/R2."""

import hashlib
import io
import json
import os
from pathlib import Path
import socket
import uuid
from unittest import mock

from sqlalchemy import insert, select, update

from studio_platform.artifact_writer import (
    ArtifactWriter,
    ArtifactWritePending,
    write_receipts,
)
from studio_platform.assets import AssetService
from studio_platform.queue import TaskQueue
from studio_platform.repository import artifacts
from studio_platform.storage import (
    LocalObjectStore,
    S3ObjectStore,
    StorageWriteUncertain,
    IntegrityError,
)
from studio_platform.storage_asset_journal import (
    AssetConflict,
    AssetQuotaExceeded,
    artifact_accounting,
)
from studio_platform.storage_multipart import (
    MultipartConflict,
    MultipartOutcomeUnknown,
    MIB,
)
from test_platform_repository import LedgerCase
from test_platform_storage import config
from test_platform_storage_multipart import FakeMultipartS3, ProcessStopped


class R2ArtifactScratchTests(LedgerCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("No network allowed")
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.work = Path(self.temp.name) / "work"
        selected = config()
        self.fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=self.fake)
        self.writer = ArtifactWriter(
            self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id
        )

    def prepared(self, *, audio=False):
        self.job(cost=0)
        queue = TaskQueue(self.repo)
        lease = queue.claim("worker", "test-pool").lease
        queue.begin_submission(lease)
        queue.record_submitted(lease, "task")
        job = queue.begin_collection(lease)
        tag = "attempt-" + uuid.uuid4().hex
        directory = self.work / tag
        directory.mkdir(parents=True)
        files, raw_paths = [], []
        for kind, suffix in (
            (("video", "mp4"), ("audio", "flac")) if audio else (("video", "mp4"),)
        ):
            path = directory / ("verified." + suffix)
            path.write_bytes(("verified " + kind).encode())
            raw = directory / ("native-" + uuid.uuid4().hex + "." + suffix)
            raw.write_bytes(("native " + kind).encode())
            files.append(
                (kind, path, "video/mp4" if kind == "video" else "audio/flac", {})
            )
            raw_paths.append(raw)
        external = self.work / "independent-cache.mp4"
        external.write_bytes(b"keep external cache")
        self.writer.begin_staging(
            job,
            lease.attempt_id,
            tag,
            kinds=("video", "audio") if audio else ("video",),
        )
        record = self.writer.prepare(
            job, lease.attempt_id, tag, files, raw_paths=[*raw_paths, external]
        )
        specs = self.writer.write(record)
        return queue, job, lease, tag, record, specs

    def settle(self, *, audio=False):
        values = self.prepared(audio=audio)
        queue, job, lease, tag, record, specs = values
        queue.complete(
            lease,
            specs,
            actual_cost_microusd=0,
            settlement=self.writer.settlement(record),
        )
        return values

    def test_video_audio_and_wangp_raw_paths_are_accounted_then_cleaned_after_commit(
        self,
    ):
        queue, job, lease, tag, record, specs = self.settle(audio=True)
        remote_bytes = sum(spec["size_bytes"] for spec in specs)
        self.assertEqual(
            self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"],
            remote_bytes + record["scratch_cleanup"]["bytes"],
        )
        self.writer.cleanup_settled(job, lease.attempt_id, tag)
        self.writer.cleanup_settled(job, lease.attempt_id, tag)
        self.assertFalse((self.work / tag).exists())
        self.assertEqual(
            self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"],
            remote_bytes,
        )
        self.assertEqual(
            (self.work / "independent-cache.mp4").read_bytes(), b"keep external cache"
        )
        self.assertEqual(len(self.fake.objects), 2)
        current = self.writer.get(job, lease.attempt_id, tag)
        self.assertEqual(
            (current["phase"], current["scratch_cleanup"]["phase"]),
            ("settled", "complete"),
        )
        with mock.patch.object(
            self.store, "put", side_effect=AssertionError("Never republish")
        ):
            self.assertEqual(self.writer.write(current), specs)

    def test_failed_result_transaction_keeps_all_scratch_and_reservation(self):
        queue, job, lease, tag, record, specs = self.prepared()
        before = self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"]
        settlement = self.writer.settlement(record)

        def rollback(conn, values):
            settlement(conn, values)
            raise RuntimeError("rollback fixture")

        with self.assertRaises(RuntimeError):
            queue.complete(lease, specs, actual_cost_microusd=0, settlement=rollback)
        self.writer.cleanup_settled(job, lease.attempt_id, tag)
        self.assertTrue((self.work / tag / "verified.mp4").exists())
        self.assertEqual(
            self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"], before
        )
        self.assertEqual(
            self.writer.get(job, lease.attempt_id, tag)["phase"], "reserved"
        )

    def test_crash_after_deletion_replays_only_quota_once_and_worker_returns_original_success(
        self,
    ):
        queue, job, lease, tag, record, specs = self.settle()
        before = self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"]
        with mock.patch.object(self.writer, "_save", side_effect=ProcessStopped()):
            with self.assertRaises(ProcessStopped):
                self.writer.cleanup_settled(job, lease.attempt_id, tag)
        self.assertEqual(list((self.work / tag).iterdir()), [])
        self.assertEqual(
            self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"], before
        )
        from studio_platform.worker import WorkerRunner

        backend = mock.Mock(kind="wangp", enabled=True)
        runner = WorkerRunner(self.repo, self.store, self.work, backend=backend)
        with mock.patch.object(
            runner.queue, "complete", side_effect=AssertionError("Do not recommit")
        ):
            result = runner._collect(job, lease, tag, "task", lambda: None)
        backend.fetch.assert_not_called()
        self.assertEqual(result["state"], "succeeded")
        self.writer.cleanup_settled(job, lease.attempt_id, tag)
        self.assertEqual(
            self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"],
            sum(s["size_bytes"] for s in specs),
        )

    def test_legacy_corrupt_remote_changed_or_unexpected_scratch_does_not_delete(self):
        for reason in ("legacy", "remote", "changed", "extra"):
            with self.subTest(reason=reason):
                queue, job, lease, tag, record, specs = self.settle()
                current = self.writer.get(job, lease.attempt_id, tag)
                directory = self.work / tag
                before = self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"]
                if reason == "legacy":
                    current.pop("scratch_policy")
                    self.writer._save(current)
                elif reason == "remote":
                    self.fake.objects[specs[0]["object_key"]]["body"] = b"bad"
                elif reason == "changed":
                    (directory / "verified.mp4").write_bytes(b"changed")
                else:
                    (directory / "unrelated.bin").write_bytes(b"preserve")
                if reason == "legacy":
                    self.writer.cleanup_settled(job, lease.attempt_id, tag)
                else:
                    with self.assertRaises(IntegrityError):
                        self.writer.cleanup_settled(job, lease.attempt_id, tag)
                self.assertTrue((directory / "verified.mp4").exists())
                self.assertEqual(
                    self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"],
                    before,
                )

    def test_hard_linked_scratch_is_not_removed(self):
        queue, job, lease, tag, record, specs = self.settle()
        source = self.work / tag / "verified.mp4"
        linked = self.work / "linked-media.mp4"
        os.link(source, linked)
        before = self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"]
        with self.assertRaises(IntegrityError):
            self.writer.cleanup_settled(job, lease.attempt_id, tag)
        self.assertEqual(source.read_bytes(), linked.read_bytes())
        self.assertEqual(
            self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"], before
        )


class ArtifactWriterTests(LedgerCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("No network allowed")
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.work = Path(self.temp.name) / "work"
        self.store = LocalObjectStore(Path(self.temp.name) / "objects")

    def writer(self, **kwargs):
        return ArtifactWriter(
            self.repo.engine,
            self.store,
            self.work,
            tenant=self.scope.tenant_id,
            **kwargs,
        )

    def record(self, writer, *, data=b"verified fixture", tag=None, job=None):
        job = job or self.job(cost=0)
        tag = tag or "attempt-" + uuid.uuid4().hex
        folder = self.work / tag
        folder.mkdir(parents=True)
        path = folder / "verified.mp4"
        path.write_bytes(data)
        attempt = str(uuid.uuid4())
        return (
            job,
            attempt,
            tag,
            writer.prepare(job, attempt, tag, [("video", path, "video/mp4", {})]),
        )

    def test_reserve_actual_object_and_staging_idempotently_then_transaction_settle(
        self,
    ):
        writer = self.writer()
        job = self.job(cost=0)
        queue = TaskQueue(self.repo)
        claim = queue.claim("worker", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "task")
        job = queue.begin_collection(claim.lease)
        folder = self.work / "attempt"
        folder.mkdir(parents=True)
        path = folder / "verified.mp4"
        path.write_bytes(b"bytes")
        (folder / "raw.mp4").write_bytes(b"raw")
        record = writer.prepare(
            job, claim.lease.attempt_id, "attempt", [("video", path, "video/mp4", {})]
        )
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 13)
        self.assertEqual(
            writer.prepare(job, claim.lease.attempt_id, "attempt", [])["id"],
            record["id"],
        )
        specs = writer.write(record)
        settlement = writer.settlement(record)

        def rollback(conn, values):
            settlement(conn, values)
            raise RuntimeError("fake transaction rollback")

        with self.assertRaises(RuntimeError):
            queue.complete(
                claim.lease, specs, actual_cost_microusd=0, settlement=rollback
            )
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(list(conn.execute(select(artifacts)))), 0)
            self.assertEqual(len(list(conn.execute(select(artifact_accounting)))), 0)
        self.assertEqual(
            writer.get(job, claim.lease.attempt_id, "attempt")["phase"], "reserved"
        )
        self.assertEqual(
            queue.complete(
                claim.lease, specs, actual_cost_microusd=0, settlement=settlement
            )["status"],
            "succeeded",
        )
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 13)
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 13)

    def test_shared_quota_blocks_both_artifacts_and_uploads_before_storage_calls(self):
        writer = self.writer(owner_quota_bytes=30)
        self.record(writer, data=b"x" * 10)
        with self.assertRaises(AssetQuotaExceeded):
            self.record(writer, data=b"y" * 6)
        service = AssetService(
            self.repo.engine,
            self.store,
            Path(self.temp.name) / "assets",
            tenant=self.scope.tenant_id,
            max_bytes=8,
            owner_quota_bytes=30,
        )
        with self.assertRaises(AssetQuotaExceeded):
            service.journal.create(
                self.scope.owner_id,
                {
                    "id": uuid.uuid4().hex,
                    "project_id": "p",
                    "status": "validating",
                    "created_at": 1,
                },
            )
        self.assertEqual(service.usage(self.scope.owner_id)["accounted_bytes"], 20)

    def test_concurrent_capacity_only_one_reservation_succeeds(self):
        writer = self.writer(owner_quota_bytes=20)
        # Initialize counters/schema outside concurrency so this checks atomic capacity.
        writer.quota.usage(self.scope.owner_id)
        jobs = [self.job(cost=0), self.job(cost=0)]

        def create(i):
            try:
                self.record(writer, data=b"x" * 10, job=jobs[i])
                return True
            except AssetQuotaExceeded:
                return False

        self.assertEqual(sum(self.parallel(create, 2)), 1)
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 20)

    def test_put_unknown_restart_reads_exact_key_no_second_put(self):
        writer = self.writer()
        job, attempt, tag, record = self.record(writer)
        original = self.store.put

        def lost(key, *args, **kwargs):
            original(key, *args, **kwargs)
            raise StorageWriteUncertain(key)

        with mock.patch.object(self.store, "put", side_effect=lost) as put:
            with self.assertRaises(ArtifactWritePending):
                writer.write(record)
            self.assertEqual(put.call_count, 1)
        restarted = self.writer()
        with mock.patch.object(
            self.store, "put", side_effect=AssertionError("Never repeat ambiguous PUT")
        ):
            specs = restarted.write(restarted.get(job, attempt, tag))
        self.assertEqual(specs[0]["object_key"], record["roles"]["video"]["key"])
        self.assertEqual(
            restarted.quota.usage(self.scope.owner_id)["accounted_bytes"],
            2 * len(b"verified fixture"),
        )

    def test_put_unknown_missing_object_does_not_release_or_repeat(self):
        writer = self.writer()
        job, attempt, tag, record = self.record(writer)
        with mock.patch.object(
            self.store,
            "put",
            side_effect=StorageWriteUncertain(record["roles"]["video"]["key"]),
        ):
            with self.assertRaises(ArtifactWritePending):
                writer.write(record)
        with mock.patch.object(
            self.store, "put", side_effect=AssertionError("Never retry unknown absence")
        ):
            with self.assertRaises(ArtifactWritePending):
                self.writer().write(self.writer().get(job, attempt, tag))
        self.assertGreater(
            writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 0
        )

    def test_crash_after_put_intent_and_changed_staging_fail_closed(self):
        writer = self.writer()
        job, attempt, tag, record = self.record(writer)
        with mock.patch.object(self.store, "put", side_effect=ProcessStopped()):
            with self.assertRaises(ProcessStopped):
                writer.write(record)
        preserved = writer.get(job, attempt, tag)
        self.assertEqual(preserved["roles"]["video"]["status"], "putting")
        (self.work / tag / "verified.mp4").write_bytes(b"changed")
        with self.assertRaises(IntegrityError):
            writer.write(preserved)
        self.assertGreater(
            writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 0
        )

    def test_more_than_100_mib_uses_fixed_key_multipart_once(self):
        selected = config()
        fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=fake)
        writer = self.writer()
        data = b"x" * (100 * MIB + 1)
        job, attempt, tag, record = self.record(writer, data=data)
        specs = writer.write(record)
        self.assertEqual(specs[0]["size_bytes"], len(data))
        self.assertEqual(sum(name == "put" for name, _ in fake.calls), 0)
        self.assertEqual(sum(name == "create" for name, _ in fake.calls), 1)
        self.assertEqual(sum(name == "part" for name, _ in fake.calls), 4)
        self.assertEqual(sum(name == "complete" for name, _ in fake.calls), 1)
        self.assertEqual(list(fake.objects), [record["roles"]["video"]["key"]])
        self.writer().write(self.writer().get(job, attempt, tag))
        self.assertEqual(sum(name == "complete" for name, _ in fake.calls), 1)

    def test_multipart_lost_completion_response_restarts_readback_only(self):
        selected = config(max_single_put_bytes=5 * MIB)
        fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=fake)
        writer = self.writer()
        job, attempt, tag, record = self.record(writer, data=b"x" * (5 * MIB + 1))
        fake.failure = ("complete", "after")
        with self.assertRaises(MultipartOutcomeUnknown):
            writer.write(record)
        restarted = self.writer()
        restarted.write(restarted.get(job, attempt, tag))
        self.assertEqual(sum(name == "create" for name, _ in fake.calls), 1)
        self.assertEqual(sum(name == "complete" for name, _ in fake.calls), 1)
        self.assertEqual(
            restarted.quota.usage(self.scope.owner_id)["accounted_bytes"],
            2 * (5 * MIB + 1),
        )

    def test_fixed_multipart_key_cannot_be_reused_by_different_request_or_owner(self):
        selected = config()
        self.store = S3ObjectStore(selected, client=FakeMultipartS3(selected))
        manager = self.writer().multipart
        key = "owners/superdan/assets/attempt/result.mp4"
        args = dict(
            size_bytes=3, sha256=hashlib.sha256(b"abc").hexdigest(), object_key=key
        )
        manager.begin("superdan", "attempt", "first", **args)
        with self.assertRaises(MultipartConflict):
            manager.begin("superdan", "attempt", "second", **args)
        with self.assertRaises(MultipartConflict):
            manager.begin("supervan", "attempt", "third", **args)

    def test_historical_artifact_backfill_is_additive_idempotent_and_not_double_counted(
        self,
    ):
        writer = self.writer()
        self.record(
            writer, data=b"keep"
        )  # Eight reserved bytes must not be overwritten.
        job = self.job(cost=0)
        queue = TaskQueue(self.repo)
        # Claim oldest queued job, but metadata ownership remains the same.
        claim = queue.claim("legacy", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "legacy-upstream")
        queue.begin_collection(claim.lease)
        spec = dict(
            kind="video",
            object_key="owners/superdan/assets/old/result.mp4",
            size_bytes=11,
            sha256="a" * 64,
            validated=True,
        )
        queue.complete(
            claim.lease, [spec, spec], actual_cost_microusd=0
        )  # Old optional callback signature.
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 19)
        self.assertEqual(
            self.writer().quota.usage(self.scope.owner_id)["accounted_bytes"], 19
        )

    def test_worker_put_response_loss_restart_skips_fetch_encode_and_resubmission(self):
        from studio_platform.worker import MockBackend, WorkerRunner

        request = {
            "request": {
                "prompt": "offline fixture",
                "duration": 4,
                "width": 256,
                "height": 256,
                "generate_audio": False,
            },
            "output_spec": {"width": 256, "height": 256},
        }
        plan = self.repo.create_plan(
            self.scope,
            request,
            {
                "pool": "test-pool",
                "backend": "mock",
                "enabled": True,
                "expected_runtime_s": 1,
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        job = self.repo.create_job(self.scope, plan["id"], "response-loss")
        backend = MockBackend(self.work / "mock", enabled=True)
        runner = WorkerRunner(self.repo, self.store, self.work, backend=backend)
        original = self.store.put

        def lost(key, *args, **kwargs):
            original(key, *args, **kwargs)
            raise StorageWriteUncertain(key)

        with mock.patch.object(self.store, "put", side_effect=lost) as put:
            self.assertEqual(
                runner.run_once("worker", "test-pool")["state"], "collecting"
            )
            self.assertEqual(put.call_count, 1)
        self.now += 31
        restarted = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=MockBackend(self.work / "mock", enabled=True),
        )
        with (
            mock.patch.object(
                restarted.backend,
                "submit",
                side_effect=AssertionError("No regeneration"),
            ),
            mock.patch.object(
                restarted.backend, "fetch", side_effect=AssertionError("No refetch")
            ),
            mock.patch(
                "studio_platform.worker.ffmpeg",
                side_effect=AssertionError("No re-encode"),
            ),
            mock.patch.object(
                self.store, "put", side_effect=AssertionError("No repeat PUT")
            ),
        ):
            self.assertEqual(
                restarted.run_once("restarted", "test-pool")["state"], "succeeded"
            )
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)

    def test_crashed_multipart_requires_explicit_fencing_then_recovers_existing_upload(
        self,
    ):
        selected = config(max_single_put_bytes=5 * MIB)
        fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=fake)
        writer = self.writer()
        job, attempt, tag, record = self.record(writer, data=b"x" * (5 * MIB + 1))
        fake.failure = ("create", "crash")
        with self.assertRaises(ProcessStopped):
            writer.write(record)
        restarted = self.writer()
        with self.assertRaises(MultipartConflict):
            restarted.write(restarted.get(job, attempt, tag))
        restarted.write(restarted.get(job, attempt, tag), fenced=True)
        self.assertEqual(sum(name == "create" for name, _ in fake.calls), 1)
        self.assertEqual(sum(name == "complete" for name, _ in fake.calls), 1)

    def test_tenant_capacity_is_shared_across_users_without_releasing_first_owner(self):
        writer = self.writer(owner_quota_bytes=30, tenant_quota_bytes=30)
        self.record(writer, data=b"x" * 10)
        with self.assertRaises(AssetQuotaExceeded):
            self.record(writer, data=b"y" * 10, job=self.job(scope=self.other, cost=0))
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 20)
        self.assertEqual(writer.quota.usage(self.other.owner_id)["accounted_bytes"], 0)

    def test_old_artifact_with_unknown_size_blocks_new_capacity_instead_of_assuming_zero(
        self,
    ):
        writer = self.writer()
        writer.quota.usage(self.scope.owner_id)
        job = self.job(cost=0)
        queue = TaskQueue(self.repo)
        claim = queue.claim("legacy", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "task")
        queue.begin_collection(claim.lease)
        spec = dict(
            kind="video",
            object_key="owners/superdan/assets/old/result.mp4",
            size_bytes=11,
            sha256="a" * 64,
            validated=True,
        )
        queue.complete(claim.lease, [spec], actual_cost_microusd=0)
        spec.pop("size_bytes")
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(artifacts)
                .where(artifacts.c.job_id == job["id"])
                .values(metadata=spec)
            )
        with self.assertRaises(AssetConflict):
            writer.quota.usage(self.scope.owner_id)
        with mock.patch.object(
            self.store,
            "put",
            side_effect=AssertionError("No write with unknown historical usage"),
        ):
            with self.assertRaises(AssetConflict):
                self.record(writer)

    def test_remote_head_sha_metadata_is_not_accepted_without_content_verification(
        self,
    ):
        selected = config()
        fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=fake)
        writer = self.writer()
        _, _, _, record = self.record(writer, data=b"correct")
        role = record["roles"]["video"]
        fake.objects[role["key"]] = dict(
            body=b"WRONG!!",
            ContentLength=7,
            ContentType="video/mp4",
            Metadata={"sha256": role["sha256"]},
            ETag='"not-proof"',
        )
        with self.assertRaises(IntegrityError):
            writer.write(record)
        self.assertEqual(sum(name == "put" for name, _ in fake.calls), 0)
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 14)

    def test_collection_reserves_before_fetch_encode_or_directory_creation(self):
        from types import SimpleNamespace
        from studio_platform.worker import MockBackend, WorkerRunner

        writer = self.writer(max_object_bytes=10, owner_quota_bytes=20)
        job = self.job(cost=0)
        job["request"] = {
            "request": {
                "duration": 4,
                "width": 256,
                "height": 256,
                "generate_audio": False,
            },
            "output_spec": {"width": 256, "height": 256},
        }
        runner = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=MockBackend(self.work / "mock", enabled=True),
        )
        with (
            mock.patch(
                "studio_platform.artifact_writer.ArtifactWriter", return_value=writer
            ),
            mock.patch.object(
                runner.backend,
                "fetch",
                side_effect=AssertionError("Quota before fetch"),
            ),
            mock.patch(
                "studio_platform.worker.ffmpeg",
                side_effect=AssertionError("Quota before encode"),
            ),
        ):
            with self.assertRaises(AssetQuotaExceeded):
                runner._collect(
                    job,
                    SimpleNamespace(attempt_id=str(uuid.uuid4())),
                    "blocked",
                    "task",
                    lambda: None,
                )
        self.assertFalse((self.work / "blocked").exists())
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 0)

    def test_staging_reservation_survives_restart_and_shrinks_atomically_to_actual(
        self,
    ):
        writer = self.writer(max_object_bytes=100, owner_quota_bytes=400)
        job, attempt, tag = self.job(cost=0), str(uuid.uuid4()), "staging"
        record = writer.begin_staging(job, attempt, tag)
        self.assertEqual(record["phase"], "staging")
        self.assertEqual(
            writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 300
        )
        restarted = self.writer(max_object_bytes=100, owner_quota_bytes=400)
        self.assertEqual(restarted.begin_staging(job, attempt, tag)["id"], record["id"])
        self.assertEqual(
            restarted.quota.usage(self.scope.owner_id)["accounted_bytes"], 300
        )
        (self.work / tag).mkdir(parents=True)
        path = self.work / tag / "verified.mp4"
        path.write_bytes(b"ready")
        (self.work / tag / "raw.mp4").write_bytes(b"download")
        record = restarted.prepare(
            job, attempt, tag, [("video", path, "video/mp4", {})]
        )
        self.assertEqual(record["phase"], "reserved")
        self.assertEqual(
            restarted.quota.usage(self.scope.owner_id)["accounted_bytes"], 18
        )
        restarted.write(record)
        self.assertEqual(
            restarted.quota.usage(self.scope.owner_id)["accounted_bytes"], 18
        )

    def test_failed_staging_remains_charged_and_cannot_write_empty_roles(self):
        writer = self.writer(max_object_bytes=100, owner_quota_bytes=400)
        job, attempt, tag = self.job(cost=0), str(uuid.uuid4()), "staging"
        record = writer.begin_staging(job, attempt, tag)
        with self.assertRaises(ArtifactWritePending):
            writer.write(record)
        with self.assertRaises(AssetQuotaExceeded):
            writer.begin_staging(self.job(cost=0), str(uuid.uuid4()), "second")
        self.assertEqual(
            writer.quota.usage(self.scope.owner_id)["accounted_bytes"], 300
        )

    def test_concurrent_first_constructor_serializes_additive_schema_creation(self):
        from studio_platform.assets import metadata as assets_schema
        from studio_platform.artifact_writer import _schema as writer_schema
        from studio_platform.storage_asset_journal import _schema as quota_schema

        # LedgerCase guarantees an isolated temporary SQLite DB or a unique test
        # Postgres schema. Removing only these empty test tables simulates first boot.
        with self.repo.engine.begin() as conn:
            writer_schema.drop_all(conn)
            quota_schema.drop_all(conn)
            assets_schema.drop_all(conn)
        writers = self.parallel(lambda _: self.writer(), 8)
        self.assertEqual(len(writers), 8)
        self.assertEqual(
            writers[0].quota.usage(self.scope.owner_id)["accounted_bytes"], 0
        )
