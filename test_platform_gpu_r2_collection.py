"""GPU delivery contract over fake R2 and the original ledger; no media/network."""
import base64
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
from types import SimpleNamespace
import unittest
from unittest import mock

from sqlalchemy import select

from comfy_workflow import native_output_spec
from studio_platform.artifact_writer import ArtifactWriter, ArtifactWritePending, write_receipts
from studio_platform.inference.outputs import GPU_R2_DELIVERY, delivery_spec, native_delivery_spec
from studio_platform.inference.protocol import BackendError
from studio_platform.inference.wangp_contract import operation_id
from studio_platform.inference.wangp_delivery import delivery_fingerprint
from studio_platform.queue import TaskQueue
from studio_platform.repository import artifacts
from studio_platform.storage import (IntegrityError, ObjectNotFound, S3ObjectStore,
    StorageError, UnsupportedStorageOperation)
from studio_platform.storage_asset_journal import AssetQuotaExceeded, artifact_accounting
from studio_platform.worker import WorkerRunner
from test_platform_repository import LedgerCase
from test_platform_storage import FakeS3, config


class GPUBackend:
    """Sealed synthetic descriptors and fake PUT effects, not media validation."""
    kind = "wangp-worker"
    enabled = True
    slot_key = "synthetic-slot"

    def __init__(self, store, description, payloads):
        self.store, self.description, self.payloads = store, description, payloads
        self.prepares = self.uploads = self.status_reads = 0
        self.lose_response = self.reject = self.unknown_before_put = False

    def prepare_delivery(self, job, tag, task, heartbeat):
        self.prepares += 1
        return copy.deepcopy(self.description)

    def delivery_status(self, job, tag, task):
        self.status_reads += 1
        return copy.deepcopy(self.description)

    def upload_delivery(self, job, tag, task, targets, heartbeat):
        self.uploads += 1
        for kind, target in targets.items():
            self.description["objects"][kind] = {"key": target["object_key"],
                "storage_binding": target["storage_binding"], "state": "not_started", "stop_proven": True}
        for kind in (kind for kind in ("video", "audio") if kind in targets):
            target = targets[kind]
            headers = target["upload"].required_headers
            assert headers["Content-Length"] == str(len(self.payloads[kind]))
            assert headers["If-None-Match"] == "*"
            state = "rejected" if self.reject else "unknown" if self.unknown_before_put else "uploaded"
            self.description["objects"][kind] = {"key": target["object_key"],
                "storage_binding": target["storage_binding"], "state": state,
                "stop_proven": state != "unknown"}
            if not self.reject and not self.unknown_before_put:
                self.store._client.objects[target["object_key"]] = {
                    "body": self.payloads[kind], "ContentLength": len(self.payloads[kind]),
                    "ContentType": target["content_type"], "Metadata": {}, "ETag": '"fake"'}
            if self.unknown_before_put:
                break  # The second role's original upload intent has not begun.
        states = {obj["state"] for obj in self.description["objects"].values()}
        self.description["state"] = "unknown" if "unknown" in states else "rejected" if "rejected" in states else "uploaded"
        if self.lose_response or self.unknown_before_put:
            raise BackendError("synthetic_lost_upload_response")
        return copy.deepcopy(self.description)

    def fetch(self, *args):
        raise AssertionError("New route must never download/stage media on CPU")

    def submit(self, *args):
        raise AssertionError("Collection must never regenerate")


class GPUCollectionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        network = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("No network"))
        network.start()
        self.addCleanup(network.stop)
        self.work = Path(self.temp.name) / "work"
        self.fake = FakeS3(config())
        self.store = S3ObjectStore(config(), client=self.fake)

    def fixture(self, *, audio=True, deployment_profile_id=None, key="one-original-job"):
        request = {"model": "MiniMax-H3-Base-BF16", "prompt": "synthetic offline fixture",
            "duration": 4, "resolution": "custom", "width": 256, "height": 256,
            "steps": 50, "generate_audio": audio, "export_crf": 18}
        compiled = {"recipe_id": "h3-base-fl2va-v1", "request": request,
                    "output_spec": native_output_spec(request), "assets": {}}
        delivery = native_delivery_spec(compiled, GPU_R2_DELIVERY)
        execution = {"pool": "test-pool", "backend": "wangp-worker", "enabled": True,
            "configuration_id": "synthetic-config", "engine_manifest_digest": "a"*64,
            "expected_runtime_s": 120, "output_delivery": GPU_R2_DELIVERY, "delivery_spec": delivery}
        if deployment_profile_id is not None:
            execution["deployment_profile_id"] = deployment_profile_id
        plan = self.repo.create_plan(self.scope, compiled, execution,
            expires_at=self.now+3600, estimated_cost_microusd=100_000)
        job = self.repo.create_job(self.scope, plan["id"], key, budget_account_ids=("owner-budget",))
        queue = TaskQueue(self.repo)
        lease = queue.claim("synthetic-worker", "test-pool").lease
        tag, task = lease.attempt_id, operation_id(lease.attempt_id)
        queue.begin_submission(lease)
        queue.record_submitted(lease, task)
        job = queue.begin_collection(lease)
        identity = {"operation_id": task, "job_id": job["id"], "attempt_tag": tag,
            "request_hash": job["request_hash"], "manifest_digest": "a"*64,
            "slot_key": "synthetic-slot", "incarnation": "b"*32}
        payloads = {"video": b"synthetic final MP4 bytes"}
        if audio:
            payloads["audio"] = b"synthetic final independent FLAC bytes"
        roles = {}
        for kind, payload in payloads.items():
            evidence = {"duration_s": delivery["duration_s"], "has_audio": True if kind == "audio" else audio,
                        "delivery_spec": delivery}
            if kind == "video":
                evidence.update(width=256, height=256, fps=24, frame_count=delivery["frame_count"],
                                container_duration_s=delivery["duration_s"])
            roles[kind] = {"kind": kind, "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "content_md5": base64.b64encode(hashlib.md5(payload).digest()).decode(),
                "content_type": "video/mp4" if kind == "video" else "audio/flac", "evidence": evidence}
        description = {"version": 1, "identity": identity, "owner_id": self.scope.owner_id,
            "delivery_spec": delivery, "export_crf": 18, "generate_audio": audio,
            "fingerprint": delivery_fingerprint(identity, delivery, 18, audio, self.scope.owner_id),
            "state": "prepared", "roles": roles, "objects": {}, "stage_durations": {"encode_validate_seconds": 1.5}}
        backend = GPUBackend(self.store, description, payloads)
        runner = WorkerRunner(self.repo, self.store, self.work, backend=backend)
        return queue, job, lease, tag, task, description, backend, runner

    def collect(self, values):
        _, job, lease, tag, task, _, _, runner = values
        with mock.patch("studio_platform.worker.probe", side_effect=AssertionError("No CPU probe")), \
             mock.patch("studio_platform.worker.ffmpeg", side_effect=AssertionError("No CPU encoding/decode")):
            return runner._collect(job, lease, tag, task, lambda: None)

    def test_original_transaction_completes_both_artifacts_without_cpu_media_files(self):
        values = self.fixture()
        queue, job, lease, tag, _, _, backend, _ = values
        result = self.collect(values)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual((backend.prepares, backend.uploads), (1, 1))
        self.assertFalse((self.work/tag).exists())
        outputs = self.repo.list_artifacts(self.scope, job["id"])
        self.assertEqual({a["metadata"]["kind"] for a in outputs}, {"video", "audio"})
        writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id)
        receipt = writer.get(job, lease.attempt_id, tag)
        self.assertEqual(receipt["phase"], "settled")
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"],
                         sum(a["metadata"]["size_bytes"] for a in outputs))
        encoded = json.dumps(receipt)
        for secret in ("X-Amz", "https://", "fake-secret", '"url"', '"headers"'):
            self.assertNotIn(secret, encoded)
        self.collect(values)
        self.assertEqual((backend.prepares, backend.uploads), (1, 1))
        self.assertEqual(self.repo.list_artifacts(self.scope, job["id"]), outputs)

    def test_lost_successful_put_response_reads_original_objects_without_regeneration(self):
        values = self.fixture()
        values[6].lose_response = True
        self.assertEqual(self.collect(values)["state"], "succeeded")
        self.assertEqual((values[6].prepares, values[6].uploads), (1, 1))

    def qualification_control(self, values):
        _, job, _, tag, _, description, _, _ = values
        identity = description["identity"]
        return {"version": 1, "mode": "drop_delivery_rpc_ack_once", "expires_at": self.now + 300,
            "owner_id": job["owner_id"], "job_id": job["id"], "attempt_tag": tag,
            "request_hash": job["request_hash"], "deployment_profile_id": job["execution_plan"]["deployment_profile_id"],
            "manifest_digest": identity["manifest_digest"], "slot_key": identity["slot_key"],
            "incarnation": identity["incarnation"]}

    @contextmanager
    def qualification_file(self, values, *, control=None, raw=None, metadata=None):
        """Model Linux protected ownership without administrator chmod on Windows."""
        path = values[7].work_dir / "qualification-drop-delivery-ack.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if raw is not None else json.dumps(
            self.qualification_control(values) if control is None else control).encode())
        original_lstat, original_open, original_fstat, original_close = Path.lstat, os.open, os.fstat, os.close
        descriptors = set()
        def protected(info):
            fields = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}
            fields.update(st_mode=stat.S_IFREG | 0o640, st_uid=0, st_gid=10001, st_nlink=1,
                          st_file_attributes=0)
            return SimpleNamespace(**(fields | (metadata or {})))
        def lstat(selected, *args, **kwargs):
            info = original_lstat(selected, *args, **kwargs)
            return protected(info) if selected == path else info
        def opened(selected, flags, *args, **kwargs):
            descriptor = original_open(selected, flags, *args, **kwargs)
            if selected == path:
                descriptors.add(descriptor)
            return descriptor
        def fstat(descriptor):
            info = original_fstat(descriptor)
            return protected(info) if descriptor in descriptors else info
        def close(descriptor):
            descriptors.discard(descriptor)
            return original_close(descriptor)
        worker_os = SimpleNamespace(**vars(os))
        worker_os.name = "posix"
        worker_os.O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
        worker_os.open, worker_os.fstat, worker_os.close = opened, fstat, close
        with mock.patch("studio_platform.worker.os", worker_os), mock.patch.object(Path, "lstat", lstat):
            yield

    def test_root_qualification_ack_drop_recovers_original_keys_once_and_restart_only_reads(self):
        values = self.fixture(deployment_profile_id="synthetic-r2-profile")
        _, job, lease, tag, _, _, backend, runner = values
        original_unknown = ArtifactWriter.remote_unknown
        with self.qualification_file(values), mock.patch.object(ArtifactWriter, "remote_unknown", autospec=True,
                side_effect=original_unknown) as unknown, self.assertLogs("studio_platform.worker", level="WARNING") as logs:
            self.assertEqual(self.collect(values)["state"], "succeeded")
            self.assertEqual(unknown.call_count, 1)
            self.assertEqual(unknown.call_args.args[1]["tag"], tag)
            self.assertTrue(runner._qualification_delivery_ack_dropped)
            restarted = WorkerRunner(self.repo, self.store, self.work, backend=backend)
            self.assertEqual(self.collect((*values[:7], restarted))["state"], "succeeded")
            self.assertFalse(restarted._qualification_delivery_ack_dropped)
            self.assertEqual(unknown.call_count, 1)
        self.assertEqual((backend.prepares, backend.uploads), (1, 1))
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["current_attempt_id"], lease.attempt_id)
        self.assertEqual({a["metadata"]["kind"] for a in self.repo.list_artifacts(self.scope, job["id"])}, {"video", "audio"})
        self.assertEqual(len(logs.output), 1)
        self.assertIn(job["id"], logs.output[0])
        self.assertIn(tag, logs.output[0])
        self.assertNotIn("https://", logs.output[0])

    def test_invalid_qualification_controls_cannot_change_successful_collection(self):
        cases = [("malformed", {}, b"{", None), ("oversized", {}, b"x"*4097, None),
            ("duplicate", {}, b'{"version":1,"version":1}', None),
            ("owner", {"owner_id": "supervan"}, None, None),
            ("job", {"job_id": "another-job"}, None, None),
            ("attempt", {"attempt_tag": "another-attempt"}, None, None),
            ("hash", {"request_hash": "f"*64}, None, None),
            ("profile", {"deployment_profile_id": "another-profile"}, None, None),
            ("manifest", {"manifest_digest": "f"*64}, None, None),
            ("slot", {"slot_key": "another-slot"}, None, None),
            ("incarnation", {"incarnation": "c"*32}, None, None),
            ("expired", {"expires_at": self.now}, None, None),
            ("too_far", {"expires_at": self.now+3601}, None, None),
            ("nonfinite", {"expires_at": float("nan")}, None, None),
            ("bool_version", {"version": True}, None, None),
            ("mode", {"mode": "another-mode"}, None, None),
            ("extra", {"extra": True}, None, None),
            ("unprivileged", {}, None, {"st_uid": 10001}),
            ("foreign_group", {}, None, {"st_gid": 10002}),
            ("group_write", {}, None, {"st_mode": stat.S_IFREG | 0o660}),
            ("other_read", {}, None, {"st_mode": stat.S_IFREG | 0o644}),
            ("hardlink", {}, None, {"st_nlink": 2}),
            ("symlink", {}, None, {"st_mode": stat.S_IFLNK | 0o600}),
            ("reparse", {}, None, {"st_file_attributes": 0x400})]
        for name, changes, raw, metadata in cases:
            with self.subTest(case=name):
                values = self.fixture(deployment_profile_id="synthetic-r2-profile", key="qualification-"+name)
                control = self.qualification_control(values) | changes
                with self.qualification_file(values, control=control, raw=raw, metadata=metadata):
                    self.assertEqual(self.collect(values)["state"], "succeeded")
                self.assertFalse(values[7]._qualification_delivery_ack_dropped)
                self.assertEqual((values[6].prepares, values[6].uploads), (1, 1))

    def test_nondict_or_unsealed_upload_ack_does_not_enable_qualification_fault(self):
        for index, state in enumerate((None, "prepared")):
            values = self.fixture(deployment_profile_id="synthetic-r2-profile", key=f"ordinary-upload-{index}")
            backend = values[6]
            upload = backend.upload_delivery
            def returned(*args, **kwargs):
                result = upload(*args, **kwargs)
                if state is None:
                    return None
                result["state"] = state
                return result
            with self.qualification_file(values), mock.patch.object(backend, "upload_delivery", side_effect=returned):
                self.assertEqual(self.collect(values)["state"], "succeeded")
            self.assertFalse(values[7]._qualification_delivery_ack_dropped)
            self.assertEqual(backend.uploads, 1)

    def test_unknown_missing_object_keeps_original_reservation_and_does_not_retry_put(self):
        values = self.fixture()
        _, job, lease, tag, _, _, backend, _ = values
        backend.unknown_before_put = True
        with self.assertRaises(BackendError):
            self.collect(values)
        writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id)
        receipt = writer.get(job, lease.attempt_id, tag)
        before = writer.quota.usage(self.scope.owner_id)["accounted_bytes"]
        with self.assertRaises(ArtifactWritePending):
            self.collect(values)
        self.assertEqual((backend.prepares, backend.uploads), (1, 1))
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], before)
        self.assertEqual(self.repo.list_artifacts(self.scope, job["id"]), [])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["current_attempt_id"], lease.attempt_id)
        # Original PUT becomes visible later: recovery only reads it.
        for kind, role in receipt["roles"].items():
            self.fake.objects[role["key"]] = {"body": backend.payloads[kind], "ContentLength": role["size_bytes"],
                "ContentType": role["content_type"], "Metadata": {}}
        self.assertEqual(self.collect(values)["state"], "succeeded")
        self.assertEqual((backend.prepares, backend.uploads), (1, 1))

    def test_confirmed_upload_rejection_can_renew_only_original_keys(self):
        values = self.fixture()
        backend = values[6]
        backend.reject = True
        with self.assertRaises(ObjectNotFound):
            self.collect(values)
        backend.reject = False
        self.assertEqual(self.collect(values)["state"], "succeeded")
        self.assertEqual((backend.prepares, backend.uploads, backend.status_reads), (1, 2, 1))
        self.assertEqual(set(self.fake.objects), {
            f"owners/superdan/assets/{values[3]}/result.mp4", f"owners/superdan/assets/{values[3]}/result.flac"})

    def test_quota_is_reserved_before_any_gpu_prepare(self):
        values = self.fixture()
        writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id,
                                owner_quota_bytes=1)
        with mock.patch("studio_platform.artifact_writer.ArtifactWriter", return_value=writer):
            with self.assertRaises(AssetQuotaExceeded):
                self.collect(values)
        self.assertEqual((values[6].prepares, values[6].uploads), (0, 0))

    def test_wrong_size_hash_and_object_owner_never_complete_or_overwrite(self):
        values = self.fixture(audio=False)
        _, job, lease, tag, task, description, _, _ = values
        writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id)
        writer.begin_remote(job, lease.attempt_id, tag)
        receipt = writer.prepare_remote(job, lease.attempt_id, tag, task, description)
        role = receipt["roles"]["video"]
        for body, size in ((b"wrong", role["size_bytes"]), (b"wrong", 5)):
            self.fake.objects[role["key"]] = {"body": body, "ContentLength": size,
                "ContentType": role["content_type"], "Metadata": {}}
            with self.assertRaises(IntegrityError):
                writer.verify_remote(receipt)
        role["key"] = "owners/supervan/assets/another/result.mp4"
        before = len(self.fake.calls)
        with self.assertRaises(IntegrityError):
            writer.remote_targets(receipt)
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.repo.list_artifacts(self.scope, job["id"]), [])

    def test_identity_or_sealed_descriptor_changes_reject_without_quota_change(self):
        values = self.fixture()
        _, job, lease, tag, task, description, _, _ = values
        writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id)
        writer.begin_remote(job, lease.attempt_id, tag, kinds=("video", "audio"))
        receipt = writer.prepare_remote(job, lease.attempt_id, tag, task, description)
        before = writer.quota.usage(self.scope.owner_id)["accounted_bytes"]
        frozen = writer.get(job, lease.attempt_id, tag)
        for mutate in (lambda d: d.update(owner_id="supervan"),
                       lambda d: d["identity"].update(request_hash="f"*64),
                       lambda d: d["roles"]["video"].update(sha256="f"*64)):
            changed = copy.deepcopy(description)
            mutate(changed)
            changed["fingerprint"] = delivery_fingerprint(changed["identity"], changed["delivery_spec"],
                changed["export_crf"], changed["generate_audio"], changed["owner_id"])
            with self.assertRaises(IntegrityError):
                writer.prepare_remote(job, lease.attempt_id, tag, task, changed)
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], before)
        self.assertEqual(writer.get(job, lease.attempt_id, tag), frozen)

    def test_signing_failure_stays_planned_and_put_intent_precedes_gpu_dispatch(self):
        values = self.fixture()
        with mock.patch.object(self.fake, "generate_presigned_url", side_effect=RuntimeError("synthetic signer failure")):
            with self.assertRaises(StorageError):
                self.collect(values)
        self.assertEqual((values[6].prepares, values[6].uploads), (1, 0))
        original_upload = values[6].upload_delivery
        def upload(*args, **kwargs):
            writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id)
            receipt = writer.get(values[1], values[2].attempt_id, values[3])
            self.assertTrue(all(role["status"] == "putting" for role in receipt["roles"].values()))
            return original_upload(*args, **kwargs)
        with mock.patch.object(values[6], "upload_delivery", side_effect=upload):
            self.assertEqual(self.collect(values)["state"], "succeeded")
        self.assertEqual((values[6].prepares, values[6].uploads, values[6].status_reads), (1, 1, 0))

    def test_stream_hash_is_bounded_and_closes_body_without_disk_staging(self):
        values = self.fixture(audio=False)
        _, _, _, tag, _, description, backend, _ = values
        payload = b"x" * (2*1024**2+7)
        backend.payloads["video"] = payload
        description["roles"]["video"].update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            content_md5=base64.b64encode(hashlib.md5(payload).digest()).decode())
        reads, closed = [], []
        original_get = self.fake.get_object
        class TrackedBody:
            def __init__(self, body): self.body = body
            def read(self, amount=-1):
                reads.append(amount)
                return self.body.read(amount)
            def close(self):
                closed.append(True)
                return self.body.close()
        def get(**kwargs):
            return {"Body": TrackedBody(original_get(**kwargs)["Body"])}
        with mock.patch.object(self.fake, "get_object", side_effect=get):
            self.assertEqual(self.collect(values)["state"], "succeeded")
        self.assertTrue(reads and all(0 < amount <= 1024**2 for amount in reads))
        self.assertTrue(closed)
        self.assertFalse((self.work/tag).exists())

    def test_existing_unknown_video_and_never_started_audio_complete_without_regeneration(self):
        values = self.fixture()
        backend = values[6]
        backend.unknown_before_put = True
        with self.assertRaises(BackendError):
            self.collect(values)
        writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id)
        receipt = writer.get(values[1], values[2].attempt_id, values[3])
        role = receipt["roles"]["video"]
        self.fake.objects[role["key"]] = {"body": backend.payloads["video"], "ContentLength": role["size_bytes"],
                                        "ContentType": role["content_type"], "Metadata": {}}
        self.assertEqual(self.repo.list_artifacts(self.scope, values[1]["id"]), [])
        self.assertEqual(backend.description["objects"]["video"]["state"], "unknown")
        self.assertEqual(backend.description["objects"]["audio"]["state"], "not_started")
        backend.unknown_before_put = False
        self.assertEqual(self.collect(values)["state"], "succeeded")
        self.assertEqual((backend.prepares, backend.uploads), (1, 2))
        self.assertEqual(backend.description["objects"]["video"]["state"], "unknown")
        self.assertEqual(backend.description["objects"]["audio"]["state"], "uploaded")

    def test_unsealed_or_malformed_delivery_and_old_policy_fail_closed(self):
        values = self.fixture()
        _, job, lease, tag, task, description, _, _ = values
        writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id)
        writer.begin_remote(job, lease.attempt_id, tag, kinds=("video", "audio"))
        before = writer.quota.usage(self.scope.owner_id)["accounted_bytes"]
        for state in ("preparing", "failed"):
            bad = copy.deepcopy(description)
            bad["state"] = state
            with self.assertRaises(ArtifactWritePending):
                writer.prepare_remote(job, lease.attempt_id, tag, task, bad)
        for mutate in (lambda d: d["roles"].pop("audio"),
                       lambda d: d["roles"]["video"]["evidence"].update(frame_count=1),
                       lambda d: d["roles"]["video"].update(content_md5="bad"),
                       lambda d: d["roles"]["video"].update(content_type="text/plain")):
            bad = copy.deepcopy(description)
            mutate(bad)
            with self.assertRaises(ValueError):
                writer.prepare_remote(job, lease.attempt_id, tag, task, bad)
        legacy = copy.deepcopy(job)
        legacy["execution_plan"].update(output_delivery="native-frames-v1",
            delivery_spec=native_delivery_spec(job["request"]))
        with self.assertRaises(IntegrityError):
            writer.begin_remote(legacy, lease.attempt_id, tag, kinds=("video", "audio"))
        self.assertEqual(writer.quota.usage(self.scope.owner_id)["accounted_bytes"], before)
        self.assertEqual(self.fake.calls, [])

    def test_failed_completion_rolls_back_artifacts_accounting_and_settlement_together(self):
        values = self.fixture()
        _, job, lease, tag, _, _, _, runner = values
        original = runner.queue.complete
        def fail(lease, specs, *, actual_cost_microusd, settlement):
            def rollback(conn, checked):
                settlement(conn, checked)
                raise RuntimeError("synthetic transaction rollback")
            return original(lease, specs, actual_cost_microusd=actual_cost_microusd, settlement=rollback)
        with mock.patch.object(runner.queue, "complete", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.collect(values)
        writer = ArtifactWriter(self.repo.engine, self.store, self.work, tenant=self.scope.tenant_id)
        self.assertEqual(writer.get(job, lease.attempt_id, tag)["phase"], "reserved")
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(list(conn.execute(select(artifacts)))), 0)
            self.assertEqual(len(list(conn.execute(select(artifact_accounting)))), 0)
        self.assertEqual(self.collect(values)["state"], "succeeded")
        self.assertEqual((values[6].prepares, values[6].uploads), (1, 1))


class ArtifactPresignTests(unittest.TestCase):
    def test_exact_signed_headers_512mib_cap_and_browser_contract_unchanged(self):
        fake = FakeS3(config())
        store = S3ObjectStore(config(), client=fake)
        md5 = base64.b64encode(hashlib.md5(b"synthetic").digest()).decode()
        url = store.presign_artifact_upload("owners/superdan/assets/attempt/result.mp4",
            content_type="video/mp4", size_bytes=512*1024**2, content_md5=md5)
        params = fake.calls[-1][1]["Params"]
        self.assertEqual({k: params[k] for k in ("ContentType", "ContentLength", "ContentMD5", "IfNoneMatch")},
            {"ContentType": "video/mp4", "ContentLength": 512*1024**2, "ContentMD5": md5, "IfNoneMatch": "*"})
        self.assertEqual(url.required_headers["Content-Length"], str(512*1024**2))
        self.assertNotIn("fake-secret", repr(url))
        with self.assertRaises(TypeError):
            json.dumps(url)
        store.presign_upload("owners/superdan/assets/image/blob", content_type="image/png")
        self.assertNotIn("ContentLength", fake.calls[-1][1]["Params"])
        self.assertNotIn("ContentMD5", fake.calls[-1][1]["Params"])
        for changes in ({"size_bytes": 512*1024**2+1}, {"size_bytes": True}, {"content_md5": "not-md5"},
                        {"expires_seconds": 301}, {"content_type": "application/octet-stream"}):
            with self.assertRaises(StorageError):
                store.presign_artifact_upload("owners/superdan/assets/attempt/result.mp4",
                    **({"content_type": "video/mp4", "size_bytes": 1, "content_md5": md5} | changes))
        with self.assertRaises(UnsupportedStorageOperation):
            S3ObjectStore(config("aws-s3"), client=FakeS3(config("aws-s3"))).presign_artifact_upload(
                "owners/superdan/assets/attempt/result.mp4", content_type="video/mp4", size_bytes=1, content_md5=md5)


if __name__ == "__main__":
    unittest.main()
