"""Offline durable multipart state-machine tests; all objects and DBs are temporary."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import traceback
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest import mock

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from studio_platform.storage import (IntegrityError, ObjectNotFound, ObjectTooLarge,
                                    S3ObjectStore, UnsupportedStorageOperation)
from studio_platform.storage_config import S3Credentials
from studio_platform.storage_multipart import (MIB, MultipartConflict, MultipartJournal,
                                              MultipartOutcomeUnknown, MultipartUploadManager)
from test_platform_storage import FakeClientError, FakeS3, OfflineTest, config


class ProcessStopped(BaseException):
    pass


class FakeMultipartS3(FakeS3):
    def __init__(self, selected):
        super().__init__(selected)
        self.uploads = {}
        self.sequence = 0
        self.failure = None

    def _failure(self, operation, stage):
        if self.failure == (operation, stage):
            self.failure = None
            if stage == "crash":
                raise ProcessStopped()
            raise RuntimeError("https://private.invalid?X-Amz-Signature=fake-secret-never-log")

    def _record(self, operation, args):
        self.calls.append((operation, {k: v for k, v in args.items() if k != "Body"}))
        self._failure(operation, "before")

    def _finish(self, operation):
        self._failure(operation, "after")
        self._failure(operation, "crash")

    def create_multipart_upload(self, **args):
        self._record("create", args)
        self.sequence += 1
        identifier = "upload-" + str(self.sequence)
        self.uploads[identifier] = {**args, "parts": {}}
        self._finish("create")
        return {"UploadId": identifier}

    def upload_part(self, **args):
        self._record("part", args)
        if args["UploadId"] not in self.uploads:
            raise FakeClientError(404, "NoSuchUpload")
        value = args["Body"].read()
        etag = '"' + hashlib.md5(value, usedforsecurity=False).hexdigest() + '"'
        self.uploads[args["UploadId"]]["parts"][args["PartNumber"]] = {"body": value, "ETag": etag}
        self._finish("part")
        return {"ETag": etag}

    def list_parts(self, **args):
        self._record("list_parts", args)
        if args["UploadId"] not in self.uploads:
            raise FakeClientError(404, "NoSuchUpload")
        return {"Parts": [{"PartNumber": n, "Size": len(v["body"]), "ETag": v["ETag"]}
                          for n, v in sorted(self.uploads[args["UploadId"]]["parts"].items())]}

    def list_multipart_uploads(self, **args):
        self._record("list_uploads", args)
        return {"Uploads": [{"Key": value["Key"], "UploadId": identifier}
                            for identifier, value in self.uploads.items() if value["Key"].startswith(args["Prefix"])]}

    def complete_multipart_upload(self, **args):
        self._record("complete", args)
        upload = self.uploads.get(args["UploadId"])
        if upload is None:
            raise FakeClientError(404, "NoSuchUpload")
        if args.get("IfNoneMatch") == "*" and args["Key"] in self.objects:
            raise FakeClientError(412, "PreconditionFailed")
        payload = b"".join(upload["parts"][part["PartNumber"]]["body"] for part in args["MultipartUpload"]["Parts"])
        self.objects[args["Key"]] = {"body": payload, "ContentLength": len(payload),
            "ContentType": upload["ContentType"], "Metadata": upload["Metadata"], "ETag": '"multipart-not-sha256-2"'}
        del self.uploads[args["UploadId"]]
        self._finish("complete")
        return {"ETag": '"multipart-not-sha256-2"'}

    def abort_multipart_upload(self, **args):
        self._record("abort", args)
        self.uploads.pop(args["UploadId"], None)
        self._finish("abort")
        return {}


class MultipartTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.url = "sqlite:///" + (Path(self.temp.name) / "journal.db").as_posix()
        postgres = os.environ.get("PLATFORM_TEST_DATABASE_URL")
        if postgres:
            parsed = make_url(postgres)
            if parsed.host not in {"127.0.0.1", "localhost"} or parsed.database != "sixnine_test":
                raise RuntimeError("Multipart tests only allow the explicit local sixnine_test database")
            schema = "multipart_test_" + uuid.uuid4().hex
            bootstrap = create_engine(parsed, echo=False, hide_parameters=True)
            with bootstrap.begin() as conn:
                conn.execute(text('CREATE SCHEMA "' + schema + '"'))
            def drop_test_schema():
                try:
                    with bootstrap.begin() as conn:
                        conn.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                finally:
                    bootstrap.dispose()
            self.addCleanup(drop_test_schema)
            self.url = parsed.update_query_dict({"options": "-csearch_path=" + schema})
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)
        self.journal = MultipartJournal(self.engine)
        self.selected = config()
        self.fake = FakeMultipartS3(self.selected)
        self.store = S3ObjectStore(self.selected, client=self.fake)
        self.manager = MultipartUploadManager(self.store, self.journal, part_size=5 * MIB)

    def begin(self, body=b"abc", **changes):
        args = dict(size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), filename="source.mp4", content_type="video/mp4")
        args.update(changes)
        return self.manager.begin("owner", "asset", "request-1", **args)

    def uploaded(self, body=b"abc"):
        session = self.begin(body)
        for offset in range(0, len(body), self.manager.part_size):
            self.manager.upload_part("owner", session["id"], offset // self.manager.part_size + 1,
                                     io.BytesIO(body[offset:offset + self.manager.part_size]))
        return session

    def count(self, name):
        return sum(operation == name for operation, _ in self.fake.calls)

    def restarted(self):
        # A fresh engine/journal simulates process restart without changing DB.
        engine = create_engine(self.url)
        self.addCleanup(engine.dispose)
        return MultipartUploadManager(self.store, MultipartJournal(engine), part_size=32 * MIB)

    def test_multiple_parts_restart_and_full_readback(self):
        body = b"x" * (5 * MIB) + b"lastpart"
        session = self.begin(body)
        self.manager.upload_part("owner", session["id"], 1, io.BytesIO(body[:5 * MIB]))
        manager = self.restarted()
        manager.reconcile("owner", session["id"])
        manager.upload_part("owner", session["id"], 2, io.BytesIO(body[5 * MIB:]))
        result = manager.complete("owner", session["id"])
        self.assertEqual(result.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(self.fake.objects[result.key]["body"], body)
        self.assertEqual(manager.complete("owner", session["id"]), result)
        self.assertEqual(self.count("create"), 1)
        self.assertEqual(self.count("complete"), 1)
        self.assertTrue(self.fake.body.closed)

    def test_idempotency_concurrent_create_and_conflict(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            sessions = list(pool.map(lambda _: self.begin(), range(8)))
        self.assertEqual(len({s["id"] for s in sessions}), 1)
        self.assertEqual(self.count("create"), 1)
        with self.assertRaises(MultipartConflict):
            self.begin(b"different")

    def test_owner_tenant_and_provider_boundaries(self):
        session = self.begin()
        before = len(self.fake.calls)
        with self.assertRaises(ObjectNotFound):
            self.manager.get("other", session["id"])
        other = MultipartUploadManager(self.store, MultipartJournal(self.engine, tenant="other"))
        with self.assertRaises(ObjectNotFound):
            other.get("owner", session["id"])
        selected = config(bucket="other-bucket")
        changed = MultipartUploadManager(S3ObjectStore(selected, client=FakeMultipartS3(selected)), self.journal)
        with self.assertRaises(MultipartConflict):
            changed.get("owner", session["id"])
        self.assertEqual(len(self.fake.calls), before)

    def test_size_caps_and_unsupported_provider_are_explicit(self):
        for size in (0, 512 * MIB + 1, True):
            with self.assertRaises(ObjectTooLarge):
                self.begin(size_bytes=size)
        with self.assertRaises(IntegrityError):
            self.begin(sha256=None)
        selected = config("hippius")
        with self.assertRaises(UnsupportedStorageOperation):
            MultipartUploadManager(S3ObjectStore(selected, client=FakeMultipartS3(selected)), self.journal)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.manager.capabilities["max_object_bytes"], 512 * MIB)
        self.assertFalse(self.manager.capabilities["browser_multipart"])

    def test_part_length_number_and_same_bytes_replay(self):
        session = self.begin()
        for number, body in ((0, b"abc"), (2, b"abc"), (1, b"ab"), (1, b"abcd")):
            with self.assertRaises(Exception):
                self.manager.upload_part("owner", session["id"], number, io.BytesIO(body))
        self.assertEqual(self.count("part"), 0)
        self.manager.upload_part("owner", session["id"], 1, io.BytesIO(b"abc"))
        self.manager.upload_part("owner", session["id"], 1, io.BytesIO(b"abc"))
        self.assertEqual(self.count("part"), 1)
        with self.assertRaises(MultipartConflict):
            self.manager.upload_part("owner", session["id"], 1, io.BytesIO(b"xyz"))

    def test_creation_lost_response_adopts_exact_upload_without_resubmission(self):
        self.fake.failure = ("create", "after")
        with self.assertRaises(MultipartOutcomeUnknown) as caught:
            self.begin()
        replay = self.begin()
        self.assertEqual(replay["id"], caught.exception.session_id)
        self.assertEqual(replay["status"], "creation_unknown")
        recovered = self.restarted().reconcile("owner", replay["id"])
        self.assertEqual(recovered["status"], "active")
        self.assertEqual(self.count("create"), 1)

    def test_creation_unknown_with_no_remote_does_not_create_again(self):
        self.fake.failure = ("create", "before")
        with self.assertRaises(MultipartOutcomeUnknown) as caught:
            self.begin()
        with self.assertRaises(MultipartOutcomeUnknown):
            self.manager.reconcile("owner", caught.exception.session_id)
        self.assertEqual(self.count("create"), 1)

    def test_crashed_operation_requires_explicit_fenced_recovery(self):
        self.fake.failure = ("create", "crash")
        with self.assertRaises(ProcessStopped):
            self.begin()
        session = self.begin()
        manager = self.restarted()
        with self.assertRaises(MultipartConflict):
            manager.reconcile("owner", session["id"])
        self.assertEqual(manager.reconcile("owner", session["id"], interrupted=True)["status"], "active")
        self.assertEqual(self.count("create"), 1)

    def test_part_lost_response_and_part_missing_have_safe_recovery(self):
        session = self.begin()
        self.fake.failure = ("part", "before")
        with self.assertRaises(MultipartOutcomeUnknown):
            self.manager.upload_part("owner", session["id"], 1, io.BytesIO(b"abc"))
        with self.assertRaises(MultipartConflict):
            self.manager.upload_part("owner", session["id"], 1, io.BytesIO(b"abc"))
        self.manager.reconcile("owner", session["id"])
        with self.assertRaises(MultipartConflict):
            self.manager.upload_part("owner", session["id"], 1, io.BytesIO(b"xyz"))
        self.fake.failure = ("part", "after")
        with self.assertRaises(MultipartOutcomeUnknown):
            self.manager.upload_part("owner", session["id"], 1, io.BytesIO(b"abc"))
        recovered = self.restarted().reconcile("owner", session["id"])
        self.assertEqual(recovered["completed_parts"], [1])
        self.manager.complete("owner", session["id"])
        self.assertEqual(self.count("part"), 2)

    def test_completion_lost_response_only_reconciles_final_object(self):
        session = self.uploaded()
        self.fake.failure = ("complete", "after")
        with self.assertRaises(MultipartOutcomeUnknown) as caught:
            self.manager.complete("owner", session["id"])
        result = self.restarted().complete("owner", session["id"])
        self.assertEqual(result.key, caught.exception.key)
        self.assertEqual(self.count("complete"), 1)

    def test_completion_unknown_with_no_object_does_not_complete_again(self):
        session = self.uploaded()
        self.fake.failure = ("complete", "before")
        with self.assertRaises(MultipartOutcomeUnknown):
            self.manager.complete("owner", session["id"])
        with self.assertRaises(ObjectNotFound):
            self.manager.complete("owner", session["id"])
        with self.assertRaises(MultipartConflict):
            self.manager.abort("owner", session["id"])
        self.assertEqual(self.count("complete"), 1)

    def test_readback_failure_does_not_repeat_completion(self):
        session = self.uploaded()
        self.fake.fail = RuntimeError("fake-secret-never-log")
        with self.assertRaises(Exception):
            self.manager.complete("owner", session["id"])
        self.assertEqual(self.manager.get("owner", session["id"])["status"], "verification_pending")
        self.fake.fail = None
        self.manager.complete("owner", session["id"])
        self.assertEqual(self.count("complete"), 1)

    def test_metadata_sha_does_not_replace_full_content_verification(self):
        session = self.uploaded()
        record = self.journal.get("owner", session["id"])
        # Corrupt bytes but retain the recorded ETag to exercise full readback.
        self.fake.uploads[record["upload_id"]]["parts"][1]["body"] = b"xyz"
        with self.assertRaises(IntegrityError):
            self.manager.complete("owner", session["id"])
        self.assertNotEqual(self.manager.get("owner", session["id"])["status"], "completed")
        self.assertEqual(self.count("get"), 1)

    def test_changed_or_unreserved_remote_parts_block_complete(self):
        session = self.uploaded()
        record = self.journal.get("owner", session["id"])
        self.fake.uploads[record["upload_id"]]["parts"][2] = {"body": b"unexpected", "ETag": '"other"'}
        with self.assertRaises(MultipartConflict):
            self.manager.complete("owner", session["id"])
        with self.assertRaises(IntegrityError):
            self.manager.reconcile("owner", session["id"])
        self.assertEqual(self.count("complete"), 0)

    def test_abort_lost_response_verifies_absence_and_is_idempotent(self):
        session = self.uploaded()
        self.fake.failure = ("abort", "after")
        with self.assertRaises(MultipartOutcomeUnknown):
            self.manager.abort("owner", session["id"])
        self.assertEqual(self.restarted().reconcile("owner", session["id"])["status"], "aborted")
        self.manager.abort("owner", session["id"])
        self.assertEqual(self.count("abort"), 1)
        self.assertEqual(self.fake.objects, {})

    def test_conditional_complete_only_claimed_for_reviewed_aws(self):
        selected = config("aws-s3")
        self.fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=self.fake)
        self.manager = MultipartUploadManager(self.store, self.journal, part_size=5 * MIB)
        session = self.uploaded()
        self.manager.complete("owner", session["id"])
        call = next(args for op, args in self.fake.calls if op == "complete")
        self.assertEqual(call["IfNoneMatch"], "*")
        self.assertTrue(self.manager.capabilities["conditional_complete"])

    def test_public_status_and_failures_do_not_leak_urls_or_provider_identifiers(self):
        self.fake.failure = ("create", "after")
        try:
            self.begin()
        except MultipartOutcomeUnknown as error:
            self.assertNotIn("fake-secret", "".join(traceback.format_exception(error)))
            status = self.manager.get("owner", error.session_id)
            self.assertNotIn("key", status)
            self.assertNotIn("upload_id", status)
        else:
            self.fail("Expected a safe unknown outcome")
        with self.engine.connect() as conn:
            records = conn.execute(text("SELECT record FROM storage_multipart_uploads")).scalars().all()
        serialized = json.dumps(records)
        self.assertNotIn("X-Amz", serialized)
        self.assertNotIn("fake-secret", serialized)
        self.assertNotIn("https:", serialized)

    def test_concurrent_completion_submits_only_once(self):
        session = self.uploaded()
        def complete(_):
            try:
                return self.manager.complete("owner", session["id"])
            except MultipartConflict:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(complete, range(6)))
        self.assertTrue(any(result is not None for result in results))
        self.assertEqual(self.count("complete"), 1)

    def test_completion_with_stale_active_snapshot_returns_verified_result(self):
        session = self.uploaded()
        stale = self.restarted()
        snapshot_read, completion_finished = Barrier(2, timeout=10), Barrier(2, timeout=10)
        list_parts = stale._list_parts

        def delayed_list(record):
            # Force the losing caller to keep its active snapshot until the
            # winner has verified the object and the provider has removed MPU.
            self.assertEqual(record["status"], "active")
            snapshot_read.wait()
            completion_finished.wait()
            return list_parts(record)

        with mock.patch.object(stale, "_list_parts", side_effect=delayed_list):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(stale.complete, "owner", session["id"])
                snapshot_read.wait()
                try:
                    winner = self.manager.complete("owner", session["id"])
                finally:
                    completion_finished.wait()
                self.assertEqual(pending.result(timeout=10), winner)
        self.assertEqual(self.count("complete"), 1)
        self.assertEqual(self.count("get"), 1)
        self.assertEqual(self.manager.get("owner", session["id"])["status"], "completed")

    def test_missing_upload_without_new_journal_version_remains_missing(self):
        session = self.uploaded()
        record = self.journal.get("owner", session["id"])
        del self.fake.uploads[record["upload_id"]]
        with self.assertRaises(ObjectNotFound):
            self.manager.complete("owner", session["id"])
        self.assertEqual(self.journal.get("owner", session["id"]), record)
        self.assertEqual(self.count("complete"), 0)
        self.assertEqual(self.count("get"), 0)

    def test_stale_completion_cannot_turn_concurrent_abort_into_success(self):
        session = self.uploaded()
        stale = self.restarted()
        list_parts = stale._list_parts
        def abort_before_list(record):
            self.manager.abort("owner", session["id"])
            return list_parts(record)
        with mock.patch.object(stale, "_list_parts", side_effect=abort_before_list):
            with self.assertRaises(MultipartConflict):
                stale.complete("owner", session["id"])
        self.assertEqual(self.manager.get("owner", session["id"])["status"], "aborted")
        self.assertEqual(self.count("complete"), 0)
        self.assertEqual(self.count("get"), 0)

    def test_stale_completion_preserves_inflight_and_unknown_outcomes(self):
        for stage, error, status in (("crash", ProcessStopped, "completing"),
                                     ("after", MultipartOutcomeUnknown, "completion_unknown")):
            with self.subTest(stage=stage):
                session = self.manager.begin("owner", "asset", "uncertain-" + stage,
                    size_bytes=3, sha256=hashlib.sha256(b"abc").hexdigest(),
                    filename="source.mp4", content_type="video/mp4")
                self.manager.upload_part("owner", session["id"], 1, io.BytesIO(b"abc"))
                stale = self.restarted()
                list_parts = stale._list_parts
                before = self.count("complete")
                def interrupted_before_list(record):
                    self.fake.failure = ("complete", stage)
                    with self.assertRaises(error):
                        self.manager.complete("owner", session["id"])
                    return list_parts(record)
                with mock.patch.object(stale, "_list_parts", side_effect=interrupted_before_list):
                    with self.assertRaises(MultipartConflict):
                        stale.complete("owner", session["id"])
                self.assertEqual(self.manager.get("owner", session["id"])["status"], status)
                self.assertEqual(self.count("complete"), before + 1)
                self.assertEqual(self.count("get"), 0)

    def test_missing_upload_reread_rechecks_provider_binding(self):
        session = self.uploaded()
        stale = self.restarted()
        list_parts = stale._list_parts
        def changed_binding_before_list(record):
            self.manager.complete("owner", session["id"])
            changed = self.journal.get("owner", session["id"])
            changed["binding"] = "different-storage-binding"
            self.journal.save("owner", changed)
            return list_parts(record)
        with mock.patch.object(stale, "_list_parts", side_effect=changed_binding_before_list):
            with self.assertRaisesRegex(MultipartConflict, "different provider"):
                stale.complete("owner", session["id"])
        self.assertEqual(self.count("complete"), 1)

    def test_crash_before_creation_intent_resumes_persisted_key(self):
        with mock.patch.object(self.journal, "save", side_effect=ProcessStopped):
            with self.assertRaises(ProcessStopped):
                self.begin()
        session = self.begin()
        persisted = self.journal.get("owner", session["id"])
        creation = next(args for op, args in self.fake.calls if op == "create")
        self.assertEqual(creation["Key"], persisted["key"])
        self.assertEqual(self.count("create"), 1)

    def test_real_sdk_request_shapes_are_valid_with_stubbed_transport(self):
        from botocore.response import StreamingBody
        from botocore.stub import ANY, Stubber
        selected = config("aws-s3")
        store = S3ObjectStore(selected, S3Credentials("fake-access", "fake-secret"))
        manager = MultipartUploadManager(store, self.journal)
        with Stubber(store._client) as stub:
            stub.add_response("create_multipart_upload", {"UploadId": "sdk-upload"},
                {"Bucket": selected.bucket, "Key": ANY, "ContentType": "video/mp4", "Metadata": ANY})
            session = manager.begin("owner", "asset", "sdk-request", size_bytes=3,
                sha256=hashlib.sha256(b"abc").hexdigest(), filename="source.mp4", content_type="video/mp4")
            key = self.journal.get("owner", session["id"])["key"]
            common = {"Bucket": selected.bucket, "Key": key, "UploadId": "sdk-upload"}
            stub.add_response("upload_part", {"ETag": '"etag-1"'}, {**common, "PartNumber": 1,
                "Body": ANY, "ContentLength": 3, "ContentMD5": "kAFQmDzST7DWlj99KOF/cg=="})
            manager.upload_part("owner", session["id"], 1, io.BytesIO(b"abc"))
            stub.add_response("list_parts", {"Parts": [{"PartNumber": 1, "Size": 3, "ETag": '"etag-1"'}]},
                              {**common, "PartNumberMarker": 0})
            stub.add_response("complete_multipart_upload", {"ETag": '"final-etag"'}, {**common,
                "IfNoneMatch": "*", "MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": '"etag-1"'}]}})
            stub.add_response("head_object", {"ContentLength": 3, "ContentType": "video/mp4"},
                              {"Bucket": selected.bucket, "Key": key})
            stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(b"abc"), 3)},
                              {"Bucket": selected.bucket, "Key": key})
            self.assertEqual(manager.complete("owner", session["id"]).size_bytes, 3)
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
