"""Storage contract tests: temporary local files and fake clients, no provider IO."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pickle
import socket
import tempfile
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from studio_platform.storage import (
    IntegrityError,
    InvalidObjectKey,
    LocalObjectStore,
    ObjectAlreadyExists,
    ObjectNotFound,
    ObjectStore,
    ObjectTooLarge,
    S3ObjectStore,
    SecretURL,
    StorageError,
    StorageWriteUncertain,
    UnsupportedStorageOperation,
    key_belongs_to,
    make_object_key,
    validate_key,
)
from studio_platform.storage_config import (
    CredentialFields,
    R2_CREDENTIAL_FIELDS,
    S3Credentials,
    S3StorageConfig,
    StorageConfigurationError,
    load_storage_credentials,
)

R2_ENDPOINT = "https://" + "a" * 32 + ".r2.cloudflarestorage.com"


def config(provider="r2", **changes):
    values = dict(
        provider=provider,
        endpoint_url=R2_ENDPOINT,
        region="auto",
        bucket="test-bucket",
        service="cloudflare-r2",
        profile="cloudflare-r2--test",
        enabled=True,
    )
    if provider == "hippius":
        values.update(
            endpoint_url="https://s3.hippius.com",
            region="decentralized",
            service="hippius-s3",
            profile="hippius-s3--test",
        )
    if provider == "aws-s3":
        values.update(
            endpoint_url="https://s3.ap-southeast-2.amazonaws.com",
            region="ap-southeast-2",
            service="aws-s3",
            profile="aws-s3--test",
        )
    values.update(changes)
    return S3StorageConfig(**values)


class FakeClientError(Exception):
    def __init__(self, status, code="TestFailure"):
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }
        super().__init__(
            "https://private.invalid?X-Amz-Signature=fake-secret-never-log"
        )


class FakeS3:
    def __init__(self, selected):
        self.meta = SimpleNamespace(
            endpoint_url=selected.endpoint_url, region_name=selected.region
        )
        self.objects, self.calls = {}, []
        self.fail = None
        self.body = None

    def put_object(self, **kwargs):
        self.calls.append(("put", {k: v for k, v in kwargs.items() if k != "Body"}))
        if self.fail:
            raise self.fail
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise FakeClientError(412, "PreconditionFailed")
        payload = kwargs["Body"].read()
        self.objects[key] = dict(
            body=payload,
            ContentLength=len(payload),
            ContentType=kwargs["ContentType"],
            Metadata=kwargs["Metadata"],
            ETag='"fake-etag"',
        )
        return {"ETag": '"fake-etag"', "VersionId": "version-1"}

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        if self.fail:
            raise self.fail
        if kwargs["Key"] not in self.objects:
            raise FakeClientError(404, "NoSuchKey")
        return {k: v for k, v in self.objects[kwargs["Key"]].items() if k != "body"}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.fail:
            raise self.fail
        if kwargs["Key"] not in self.objects:
            raise FakeClientError(404, "NoSuchKey")
        self.body = io.BytesIO(self.objects[kwargs["Key"]]["body"])
        return {"Body": self.body}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        self.objects.pop(kwargs["Key"], None)
        return {}

    def generate_presigned_url(self, operation, **kwargs):
        self.calls.append(("presign", {"operation": operation, **kwargs}))
        if self.fail:
            raise self.fail
        params = kwargs["Params"]
        return (
            self.meta.endpoint_url
            + "/"
            + params["Bucket"]
            + "/"
            + params["Key"]
            + "?X-Amz-Signature=fake-secret-never-log"
        )


class OfflineTest(unittest.TestCase):
    def setUp(self):
        # Catches accidental default credential probes and all actual HTTP.
        self.network = mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("No network allowed")
        )
        self.network.start()
        self.addCleanup(self.network.stop)


class LocalStorageTests(OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="h3-storage-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = LocalObjectStore(self.root / "isolated-objects")

    def test_new_object_roundtrip_and_restart(self):
        payload = b"private video bytes"
        info = self.store.write_new(
            "superdan",
            "asset-one",
            io.BytesIO(payload),
            filename="source.mp4",
            content_type="video/mp4",
            max_bytes=len(payload),
        )
        self.assertIsInstance(self.store, ObjectStore)
        self.assertTrue(key_belongs_to(info.key, "superdan"))
        self.assertFalse(key_belongs_to(info.key, "supervan"))
        self.assertEqual(info.size_bytes, len(payload))
        self.assertEqual(info.sha256, hashlib.sha256(payload).hexdigest())
        restarted = LocalObjectStore(self.store.root)
        self.assertEqual(restarted.stat(info.key), info)
        with restarted.open(info.key) as source:
            self.assertEqual(source.read(), payload)
        self.assertTrue(source.closed)

    def test_original_and_derivative_use_different_keys(self):
        first = self.store.write_new("user", "asset", io.BytesIO(b"original"))
        second = self.store.write_new("user", "asset", io.BytesIO(b"derived"))
        self.assertNotEqual(first.key, second.key)
        with self.store.open(first.key) as source:
            self.assertEqual(source.read(), b"original")

    def test_fixed_key_is_never_overwritten(self):
        self.store.put("safe/key", io.BytesIO(b"first"))
        with self.assertRaises(ObjectAlreadyExists):
            self.store.put("safe/key", io.BytesIO(b"second"))
        with self.store.open("safe/key") as source:
            self.assertEqual(source.read(), b"first")

    def test_two_concurrent_publishers_only_one_wins(self):
        def publish(index):
            try:
                self.store.put("same/object", io.BytesIO(str(index).encode()))
                return "won"
            except ObjectAlreadyExists:
                return "lost"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(publish, range(8)))
        self.assertEqual(results.count("won"), 1)
        self.assertEqual(results.count("lost"), 7)

    def test_failed_upload_is_not_published_and_staging_is_removed(self):
        for bad_limit, bad_hash, expected in (
            (2, None, ObjectTooLarge),
            (10, "0" * 64, IntegrityError),
        ):
            with self.assertRaises(expected):
                self.store.put(
                    "failed/object",
                    io.BytesIO(b"123"),
                    max_bytes=bad_limit,
                    expected_sha256=bad_hash,
                )
            with self.assertRaises(ObjectNotFound):
                self.store.stat("failed/object")
            self.assertEqual(list(self.store._staging.iterdir()), [])

    def test_invalid_keys_cannot_escape_or_hit_windows_special_files(self):
        keys = [
            "../secret",
            "/absolute",
            "C:/secret",
            "safe\\secret",
            "a//b",
            "a/./b",
            "a/../b",
            "a/%2e%2e/b",
            "a/file?token=value",
            "a/file#part",
            "a/file:stream",
            "a/NUL.txt",
            "a/CON",
            "a/trailing.",
            "a/space name",
            "a/\x00",
            "a/图片",
        ]
        for key in keys:
            with self.subTest(key=key), self.assertRaises(InvalidObjectKey):
                self.store.put(key, io.BytesIO(b"never written"))
        self.assertFalse((self.root / "secret").exists())

    def test_post_publish_sync_failure_retains_reconciliation_key(self):
        from studio_platform import storage

        with mock.patch.object(
            storage, "_sync_directory", side_effect=[None, OSError("sync failed")]
        ):
            with self.assertRaises(StorageWriteUncertain) as result:
                self.store.write_new("owner", "asset", io.BytesIO(b"bytes"))
        with self.store.open(result.exception.key) as source:
            self.assertEqual(source.read(), b"bytes")

    def test_limits_and_header_injection_rejected(self):
        for kwargs in (
            {"max_bytes": -1},
            {"max_bytes": True},
            {"expected_sha256": "bad"},
            {"content_type": "video/mp4\r\nLocation: other"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(StorageError):
                self.store.write_new("user", "asset", io.BytesIO(b"123"), **kwargs)

    def test_link_root_and_blob_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("This Windows token cannot create symlinks")
        with self.assertRaises(StorageError):
            LocalObjectStore(link / "data")
        self.assertFalse((target / "data").exists())
        info = self.store.write_new("user", "asset", io.BytesIO(b"safe"))
        blob = self.store._directory(info.key) / "blob"
        blob.unlink()
        protected = target / "protected"
        protected.write_bytes(b"secret")
        blob.symlink_to(protected)
        for action in (self.store.open, self.store.stat, self.store.delete):
            with self.assertRaises(StorageError):
                action(info.key)
        self.assertEqual(protected.read_bytes(), b"secret")

    def test_hardlink_and_reparse_attributes_rejected(self):
        info = self.store.write_new("user", "asset", io.BytesIO(b"safe"))
        blob = self.store._directory(info.key) / "blob"
        os.link(blob, self.root / "other-link")
        with self.assertRaises(StorageError):
            self.store.open(info.key)
        from studio_platform.storage import _no_links

        fake = SimpleNamespace(st_mode=0o040700, st_file_attributes=0x400)
        with (
            mock.patch.object(Path, "lstat", return_value=fake),
            self.assertRaises(StorageError),
        ):
            _no_links(self.root)

    def test_delete_idempotent_and_keeps_other_assets(self):
        one = self.store.write_new("one", "asset", io.BytesIO(b"one"))
        two = self.store.write_new("two", "asset", io.BytesIO(b"two"))
        self.store.delete(one.key)
        self.store.delete(one.key)
        with self.assertRaises(ObjectNotFound):
            self.store.open(one.key)
        self.assertEqual(self.store.stat(two.key).size_bytes, 3)

    def test_local_requires_authenticated_app_routes(self):
        with self.assertRaises(UnsupportedStorageOperation):
            self.store.presign_download("safe/key")
        with self.assertRaises(UnsupportedStorageOperation):
            self.store.presign_upload("safe/key", content_type="video/mp4")


class CloudStorageTests(OfflineTest):
    def store(self, provider="r2", **changes):
        selected = config(provider, **changes)
        fake = FakeS3(selected)
        return S3ObjectStore(selected, client=fake), fake

    def test_cloud_disabled_and_missing_credentials_cannot_use_default_chain(self):
        with self.assertRaises(StorageConfigurationError):
            S3ObjectStore(config(enabled=False))
        with self.assertRaises(StorageConfigurationError):
            S3ObjectStore(config())

    def test_explicit_provider_endpoint_region_and_profile(self):
        invalid = [
            {"endpoint_url": ""},
            {"endpoint_url": "https://other.invalid"},
            {"endpoint_url": R2_ENDPOINT + "?token=secret"},
            {
                "endpoint_url": "https://name:secret@"
                + "a" * 32
                + ".r2.cloudflarestorage.com"
            },
            {"region": "us-east-1"},
            {"profile": ""},
            {"provider": "minio"},
            {"bucket": "../escape"},
            {"endpoint_url": "http://localhost:9000"},
        ]
        for changes in invalid:
            with (
                self.subTest(changes=changes),
                self.assertRaises(StorageConfigurationError),
            ):
                config(**changes)
        selected = config()
        wrong = FakeS3(config("hippius"))
        with self.assertRaises(StorageConfigurationError):
            S3ObjectStore(selected, client=wrong)

    def test_r2_aws_atomic_writes_and_readback(self):
        for provider in ("r2", "aws-s3"):
            with self.subTest(provider=provider):
                store, fake = self.store(provider)
                info = store.write_new(
                    "owner", "asset", io.BytesIO(b"bytes"), content_type="video/mp4"
                )
                self.assertEqual(fake.calls[0][1]["IfNoneMatch"], "*")
                self.assertEqual(store.stat(info.key).sha256, info.sha256)
                with store.open(info.key) as stream:
                    self.assertEqual(stream.read(), b"bytes")
                self.assertTrue(fake.body.closed)
                with self.assertRaises(ObjectAlreadyExists):
                    store.put(info.key, io.BytesIO(b"replace"))
                store.delete(info.key)
                with self.assertRaises(ObjectNotFound):
                    store.stat(info.key)

    def test_hippius_new_only_and_capability_rejections(self):
        store, fake = self.store("hippius")
        self.assertTrue(store.capabilities.experimental)
        self.assertFalse(store.capabilities.conditional_create)
        self.assertFalse(store.capabilities.browser_upload_verified)
        with self.assertRaises(UnsupportedStorageOperation):
            store.put("specified/key", io.BytesIO(b"no"))
        with self.assertRaises(UnsupportedStorageOperation):
            store.presign_upload("specified/key", content_type="video/mp4")
        self.assertEqual(fake.calls, [])
        first = store.write_new("owner", "asset", io.BytesIO(b"one"))
        second = store.write_new("owner", "asset", io.BytesIO(b"two"))
        self.assertNotEqual(first.key, second.key)
        self.assertNotIn("IfNoneMatch", fake.calls[0][1])
        signed = store.presign_download(first.key)
        self.assertTrue(
            signed.reveal().startswith("https://s3.hippius.com/test-bucket/")
        )

    def test_bounded_uploads_fail_before_any_cloud_call(self):
        store, fake = self.store(max_single_put_bytes=3)
        with self.assertRaises(ObjectTooLarge):
            store.write_new("owner", "asset", io.BytesIO(b"four"), max_bytes=10)
        with self.assertRaises(IntegrityError):
            store.write_new(
                "owner", "asset", io.BytesIO(b"ok"), expected_sha256="0" * 64
            )
        self.assertEqual(fake.calls, [])

    def test_unknown_write_returns_reconciliation_key_without_fallback_or_retry(self):
        store, fake = self.store("hippius")
        fake.fail = TimeoutError(
            "https://private.invalid?X-Amz-Signature=fake-secret-never-log"
        )
        try:
            store.write_new("owner", "asset", io.BytesIO(b"abc"))
        except StorageWriteUncertain as error:
            self.assertTrue(key_belongs_to(error.key, "owner"))
            self.assertNotIn("fake-secret", "".join(traceback.format_exception(error)))
        else:
            self.fail("Unknown write must not succeed")
        self.assertEqual(len(fake.calls), 1)

    def test_sdk_failures_and_signed_urls_do_not_leak_via_repr_or_errors(self):
        store, fake = self.store()
        signed = store.presign_upload(
            "safe/key", content_type="video/mp4", expires_seconds=300
        )
        self.assertIsInstance(signed, SecretURL)
        self.assertNotIn("fake-secret", repr(signed))
        self.assertNotIn("fake-secret", str(signed))
        self.assertEqual(
            signed.required_headers, {"Content-Type": "video/mp4", "If-None-Match": "*"}
        )
        with self.assertRaises(TypeError):
            json.dumps(signed)
        credentials = S3Credentials("fake-access", "fake-secret")
        self.assertNotIn("fake-secret", repr(credentials))
        for secret_object in (signed, credentials):
            with self.assertRaises(TypeError):
                pickle.dumps(secret_object)
        fake.fail = FakeClientError(403)
        for action in (
            lambda: store.stat("safe/key"),
            lambda: store.presign_download("safe/key"),
        ):
            try:
                action()
            except StorageError as error:
                self.assertNotIn(
                    "fake-secret", "".join(traceback.format_exception(error))
                )

    def test_presign_refuses_custom_domains_expiry_and_invalid_keys(self):
        store, fake = self.store()
        for expiry in (0, 3601, True):
            with self.assertRaises(StorageError):
                store.presign_download("safe/key", expires_seconds=expiry)
        with self.assertRaises(InvalidObjectKey):
            store.presign_download("../key")
        fake.meta.endpoint_url = "https://media.example.test"
        with self.assertRaises(StorageError):
            store.presign_download("safe/key")

    def test_download_disposition_is_signed_and_injection_rejected(self):
        store, fake = self.store()
        from unittest.mock import patch

        with patch.object(
            fake, "generate_presigned_url", wraps=fake.generate_presigned_url
        ) as generate:
            store.presign_download("safe/key", download_filename="章节.mp4")
            self.assertEqual(
                generate.call_args.kwargs["Params"]["ResponseContentDisposition"],
                "attachment; filename*=UTF-8''%E7%AB%A0%E8%8A%82.mp4",
            )
            for filename in ("../other", "bad\r\nheader", "a\\b", ""):
                with self.assertRaises(StorageError):
                    store.presign_download("safe/key", download_filename=filename)
        hippius, _ = self.store("hippius")
        with self.assertRaises(UnsupportedStorageOperation):
            hippius.presign_download("safe/key", download_filename="file.mp4")

    def test_real_sdk_initialization_and_presign_are_offline_and_explicit(self):
        try:
            import boto3  # noqa: F401
        except ImportError:
            self.skipTest("Optional boto3 dependency is not installed")
        with mock.patch.dict(
            os.environ,
            {
                "AWS_ENDPOINT_URL": "https://wrong.invalid",
                "AWS_ENDPOINT_URL_S3": "https://wrong.invalid",
            },
        ):
            store = S3ObjectStore(config(), S3Credentials("fake-access", "fake-secret"))
            self.assertEqual(store._client.meta.endpoint_url, R2_ENDPOINT)
            self.assertEqual(store._client.meta.config.retries["total_max_attempts"], 1)
            signed = store.presign_download("safe/key")
            self.assertTrue(
                signed.reveal().startswith(R2_ENDPOINT + "/test-bucket/safe/key?")
            )
            upload = store.presign_upload("safe/new", content_type="video/mp4")
            self.assertIn("If-None-Match", upload.required_headers)
            # A remote worker's plain PUT sends only Content-Type (#795).
            worker = store.presign_worker_upload("safe/tmp", content_type="video/mp4")
            self.assertEqual(worker.required_headers, {"Content-Type": "video/mp4"})
            from urllib.parse import parse_qs, urlsplit

            signed = parse_qs(urlsplit(worker.reveal()).query)["X-Amz-SignedHeaders"]
            self.assertEqual(signed, ["content-type;host"])
            self.assertEqual(
                parse_qs(urlsplit(upload.reveal()).query)["X-Amz-SignedHeaders"],
                ["content-type;host;if-none-match"],
            )
            with self.assertRaises(StorageError):
                store._client.meta.events.emit(
                    "before-send.s3.PutObject",
                    request=SimpleNamespace(url="https://wrong.invalid/redirect"),
                )

    def test_immutable_video_r2_signatures_are_bucket_stable_and_do_not_change_default_signers(
        self,
    ):
        from datetime import datetime, timezone
        from urllib.parse import parse_qs, urlsplit
        from studio_platform.storage import (
            VIDEO_SIGNING_BUCKET_SECONDS,
            VIDEO_SIGNING_VALIDITY_SECONDS,
        )
        import botocore.auth

        store = S3ObjectStore(config(), S3Credentials("fake-access", "fake-secret"))
        start = (
            1760572800 // VIDEO_SIGNING_BUCKET_SECONDS * VIDEO_SIGNING_BUCKET_SECONDS
        )
        clock = botocore.auth.get_current_datetime

        def sign(now, **options):
            with mock.patch(
                "studio_platform.storage.time", SimpleNamespace(time=lambda: now)
            ):
                return store.presign_immutable_video(
                    "safe/video.mp4", content_type="video/mp4", **options
                )

        early, late, next_bucket = (
            sign(start),
            sign(start + VIDEO_SIGNING_BUCKET_SECONDS - 1),
            sign(start + VIDEO_SIGNING_BUCKET_SECONDS),
        )
        self.assertEqual(early.reveal(), late.reveal())
        self.assertNotEqual(late.reveal(), next_bucket.reveal())
        self.assertEqual(early.expires_at, start + VIDEO_SIGNING_VALIDITY_SECONDS)
        self.assertEqual(
            next_bucket.expires_at,
            start + VIDEO_SIGNING_BUCKET_SECONDS + VIDEO_SIGNING_VALIDITY_SECONDS,
        )
        query = parse_qs(urlsplit(early.reveal()).query)
        self.assertEqual(
            query["X-Amz-Date"],
            [datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m%dT%H%M%SZ")],
        )
        self.assertEqual(query["X-Amz-Expires"], ["86400"])
        self.assertEqual(
            query["response-cache-control"], ["private, max-age=86400, immutable"]
        )
        self.assertEqual(query["response-content-type"], ["video/mp4"])
        self.assertEqual(query["response-content-disposition"], ["inline"])
        attachment = sign(start + 1, download_filename="章节.mp4")
        self.assertEqual(
            parse_qs(urlsplit(attachment.reveal()).query)[
                "response-content-disposition"
            ],
            ["attachment; filename*=UTF-8''%E7%AB%A0%E8%8A%82.mp4"],
        )
        self.assertNotEqual(attachment.reveal(), early.reveal())
        self.assertEqual(early.required_headers, {})
        self.assertNotIn("fake-secret", repr(early))
        # Existing SDK GET/PUT dates stay current, short-lived and unmodified,
        # even when immutable video signing occurs in the same process.
        with ThreadPoolExecutor(max_workers=3) as pool:
            urls = list(
                pool.map(
                    lambda _: store.presign_upload(
                        "safe/new", content_type="video/mp4"
                    ).reveal(),
                    range(3),
                )
            )
        default = parse_qs(
            urlsplit(store.presign_download("safe/video.mp4").reveal()).query
        )
        self.assertEqual(default["X-Amz-Expires"], ["300"])
        self.assertNotEqual(default["X-Amz-Date"], query["X-Amz-Date"])
        self.assertNotIn("response-cache-control", default)
        for url in urls:
            self.assertEqual(parse_qs(urlsplit(url).query)["X-Amz-Expires"], ["300"])
        self.assertIs(botocore.auth.get_current_datetime, clock)

    def test_immutable_video_policy_is_not_a_general_long_lived_or_public_presign(self):
        store = S3ObjectStore(config(), S3Credentials("fake-access", "fake-secret"))
        for mime in (
            "image/png",
            "audio/flac",
            "application/octet-stream",
            "video/mp4\r\npublic",
        ):
            with self.assertRaises(StorageError):
                store.presign_immutable_video("safe/video", content_type=mime)
        for filename in ("../file.mp4", "bad\r\nheader", ""):
            with self.assertRaises(StorageError):
                store.presign_immutable_video(
                    "safe/video", content_type="video/mp4", download_filename=filename
                )
        with self.assertRaises(InvalidObjectKey):
            store.presign_immutable_video("../video", content_type="video/mp4")
        with self.assertRaises(StorageError):
            store.presign_download("safe/video", expires_seconds=86400)
        for provider in ("hippius", "aws-s3"):
            other, _ = self.store(provider)
            with self.assertRaises(UnsupportedStorageOperation):
                other.presign_immutable_video("safe/video", content_type="video/mp4")
        fake_store, _ = self.store()
        with self.assertRaisesRegex(StorageError, "Explicit storage credentials"):
            fake_store.presign_immutable_video("safe/video", content_type="video/mp4")

    def test_stream_transport_errors_are_safe_and_body_is_closed(self):
        store, fake = self.store()
        body = mock.Mock()
        body.read.side_effect = RuntimeError(
            "https://x.invalid?X-Amz-Signature=fake-secret-never-log"
        )
        with mock.patch.object(fake, "get_object", return_value={"Body": body}):
            try:
                with store.open("safe/key") as source:
                    source.read()
            except StorageError as error:
                self.assertNotIn(
                    "fake-secret", "".join(traceback.format_exception(error))
                )
            else:
                self.fail("The broken download must remain a failure")
        body.close.assert_called_once()


class RegistryCredentialTests(OfflineTest):
    def loaded(self, **changes):
        result = dict(
            service="cloudflare-r2",
            profile="cloudflare-r2--test",
            base_url=None,
            env={
                "R2_ENDPOINT": R2_ENDPOINT,
                "R2_ACCESS_KEY_ID": "fake-access",
                "R2_SECRET_ACCESS_KEY": "fake-secret",
            },
        )
        result.update(changes)
        return SimpleNamespace(**result)

    def test_loader_is_explicit_and_leaves_environment_unchanged(self):
        loader = mock.Mock(return_value=self.loaded())
        before = dict(os.environ)
        result = load_storage_credentials(
            config(), fields=R2_CREDENTIAL_FIELDS, loader=loader
        )
        self.assertIsInstance(result, S3Credentials)
        loader.assert_called_once_with("cloudflare-r2", profile="cloudflare-r2--test")
        self.assertEqual(dict(os.environ), before)

    def test_profile_endpoint_and_missing_fields_fail_closed(self):
        cases = [
            self.loaded(profile="other"),
            self.loaded(service="other"),
            self.loaded(base_url="https://other.invalid"),
            self.loaded(env={}),
            self.loaded(
                env={
                    "R2_ENDPOINT": "https://other.invalid",
                    "R2_ACCESS_KEY_ID": "fake-access",
                    "R2_SECRET_ACCESS_KEY": "fake-secret",
                }
            ),
        ]
        for loaded in cases:
            with self.assertRaises(StorageConfigurationError):
                load_storage_credentials(
                    config(), fields=R2_CREDENTIAL_FIELDS, loader=lambda *a, **k: loaded
                )

    def test_loader_error_never_exposes_values(self):
        loader = mock.Mock(side_effect=RuntimeError("fake-secret-never-log"))
        try:
            load_storage_credentials(
                config(), fields=R2_CREDENTIAL_FIELDS, loader=loader
            )
        except StorageConfigurationError as error:
            self.assertNotIn("fake-secret", "".join(traceback.format_exception(error)))
        else:
            self.fail("Expected configuration failure")

    def test_required_session_field_never_falls_back_to_environment(self):
        fields = CredentialFields(
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_ENDPOINT",
            "R2_SESSION_TOKEN",
        )
        with mock.patch.dict(os.environ, {"R2_SESSION_TOKEN": "unrelated-fake-token"}):
            with self.assertRaises(StorageConfigurationError):
                load_storage_credentials(
                    config(), fields=fields, loader=lambda *a, **k: self.loaded()
                )

    def test_hippius_credentials_cannot_be_wallet_seed(self):
        with self.assertRaises(StorageConfigurationError):
            S3Credentials("twelve words are not api access", "some secret")
        with self.assertRaises(StorageConfigurationError):
            S3ObjectStore(
                config("hippius"), S3Credentials("not-an-s3-access-key", "fake-secret")
            )
        with self.assertRaises(StorageConfigurationError):
            CredentialFields("secret-value", "FIELD", "ENDPOINT")


if __name__ == "__main__":
    unittest.main()
