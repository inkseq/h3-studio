"""Frontend artifact safety and immutable publication, entirely offline."""
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from deploy.platform import frontend_bundle as bundle
from tools.build_frontend_release import build
from tools.publish_frontend_release import publish


COMMIT = "a" * 40
API_COMPATIBILITY = "b" * 64


class FakeS3:
    def __init__(self):
        self.objects, self.puts, self.heads = {}, [], []
        self.error_status = None

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        status = self.error_status or (412 if kwargs["Key"] in self.objects else None)
        if status:
            raise ClientError({"Error": {"Code": "PreconditionFailed" if status == 412 else "AccessDenied"},
                               "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject")
        self.objects[kwargs["Key"]] = {"Metadata": kwargs["Metadata"], "ContentLength": len(kwargs["Body"])}
        return {}

    def head_object(self, **kwargs):
        self.heads.append(kwargs)
        return self.objects[kwargs["Key"]]


class FrontendReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dist = self.root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        self.html = b'<!doctype html><script type="module" src="/assets/index-AbCd_123.js"></script>'
        (self.dist / "index.html").write_bytes(self.html)
        (self.dist / "assets" / "index-AbCd_123.js").write_bytes(b"console.log('safe fixture')")
        (self.dist / "assets" / "style-AbCd-123.css").write_bytes(b"body{color:black}")
        self.target = self.root / "bundle"
        self.contract = patch("tools.release_contract.build_contracts", return_value={"api_compatibility": API_COMPATIBILITY})
        self.contract.start()
        self.addCleanup(self.contract.stop)
        self.manifest = build(COMMIT, self.dist, self.target)

    def write_manifest(self, value):
        (self.target / bundle.MANIFEST_NAME).write_text(json.dumps(value), encoding="utf-8")

    def replace_archive(self, raw, *, manifest=None):
        value = copy.deepcopy(manifest or self.manifest)
        value.update(archive_sha256=bundle.digest(raw), archive_bytes=len(raw))
        (self.target / bundle.ARCHIVE_NAME).write_bytes(raw)
        self.write_manifest(value)
        return value

    def crafted_tar(self, entries, trailing=b""):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, content, kind in entries:
                member = tarfile.TarInfo(name)
                member.type, member.mode, member.mtime = kind, 0o644, 0
                member.size = len(content)
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = "../../outside"
                archive.addfile(member, io.BytesIO(content))
        return gzip.compress(raw.getvalue() + trailing, mtime=0)

    def test_deterministic_build_and_verified_roundtrip(self):
        other = self.root / "second"
        second = build(COMMIT, self.dist, other)
        self.assertEqual(second, self.manifest)
        for name in (bundle.ARCHIVE_NAME, bundle.MANIFEST_NAME):
            self.assertEqual((self.target / name).read_bytes(), (other / name).read_bytes())
        self.assertEqual(bundle.validate(self.target, COMMIT), self.manifest)
        files = bundle.archive_files(self.target, self.manifest)
        self.assertEqual(files["index.html"], self.html)
        self.assertEqual(set(files), set(self.manifest["files"]))
        self.assertEqual(self.manifest["api_compatibility"], API_COMPATIBILITY)
        compressed = (self.target / bundle.ARCHIVE_NAME).read_bytes()
        self.assertEqual(compressed[3], 0)  # no original filename in gzip header
        self.assertEqual(compressed[4:8], bytes(4))
        with tarfile.open(fileobj=io.BytesIO(compressed), mode="r:gz") as archive:
            self.assertTrue(all(m.isfile() and m.uid == m.gid == m.mtime == 0
                                and m.mode == 0o644 and not m.uname and not m.gname for m in archive))

    def test_manifest_exact_identity_fields_and_types(self):
        for change in ({"extra": True}, {"version": True}, {"commit": "c" * 40},
                       {"api_contract": "unknown-v2"}, {"api_compatibility": "x" * 64},
                       {"archive_bytes": True}, {"archive_bytes": bundle.MAX_ARCHIVE + 1}):
            with self.subTest(change=change):
                self.write_manifest({**self.manifest, **change})
                with self.assertRaises(ValueError):
                    bundle.validate(self.target, COMMIT)
        self.write_manifest(self.manifest)
        with self.assertRaises(ValueError):
            bundle.validate(self.target, "main")

    def test_duplicate_manifest_keys_and_size_limit(self):
        path = self.target / bundle.MANIFEST_NAME
        path.write_bytes(b'{"version":1,' + json.dumps(self.manifest).encode()[1:])
        with self.assertRaises(ValueError):
            bundle.validate(self.target, COMMIT)
        path.write_bytes(b" " * (bundle.MAX_MANIFEST + 1))
        with self.assertRaises(ValueError):
            bundle.validate(self.target, COMMIT)

    def test_archive_tampering_and_file_hash_mismatch(self):
        path = self.target / bundle.ARCHIVE_NAME
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "hash_or_size"):
            bundle.validate(self.target, COMMIT)
        path.write_bytes(original)
        forged = copy.deepcopy(self.manifest)
        forged["files"]["index.html"]["sha256"] = "0" * 64
        self.write_manifest(forged)
        with self.assertRaisesRegex(ValueError, "file_hash"):
            bundle.validate(self.target, COMMIT)

    def test_truncated_gzip_rejected_even_with_matching_archive_checksum(self):
        self.replace_archive((self.target / bundle.ARCHIVE_NAME).read_bytes()[:-5])
        with self.assertRaisesRegex(ValueError, "compression"):
            bundle.validate(self.target, COMMIT)

    def test_dist_rejects_extra_files_dotfiles_sourcemaps_and_unhashed_assets(self):
        for name in ("source-manifest.json", ".env", "assets/.secret", "assets/index-AbCd_123.js.map",
                     "assets/unhashed.js"):
            with self.subTest(name=name):
                path = self.dist / name
                path.write_bytes(b"unwanted")
                try:
                    with self.assertRaises(ValueError):
                        build(COMMIT, self.dist, self.root / "rejected")
                    self.assertFalse((self.root / "rejected").exists())
                finally:
                    path.unlink()
        folder = self.dist / "assets" / "nested-AbCd1234.js"
        folder.mkdir()
        with self.assertRaises(ValueError):
            build(COMMIT, self.dist, self.root / "rejected")

    def test_source_and_bundle_links_are_rejected(self):
        link = self.dist / "assets" / "linked-AbCd1234.js"
        try:
            link.symlink_to(self.dist / "index.html")
        except (OSError, NotImplementedError):
            self.skipTest("This account cannot create symbolic links")
        with self.assertRaises(ValueError):
            build(COMMIT, self.dist, self.root / "rejected")
        external = self.root / "outside-manifest"
        manifest_path = self.target / bundle.MANIFEST_NAME
        manifest_path.rename(external)
        manifest_path.symlink_to(external)
        with self.assertRaises(ValueError):
            bundle.validate(self.target, COMMIT)

    def test_archive_traversal_absolute_paths_and_unlisted_files(self):
        for name in ("../index.html", "/index.html", "assets/../index.html", "assets\\index-AbCd1234.js",
                     "assets/extra-AbCd1234.js"):
            with self.subTest(name=name):
                self.replace_archive(self.crafted_tar([(name, b"x", tarfile.REGTYPE)]))
                with self.assertRaises(ValueError):
                    bundle.validate(self.target, COMMIT)

    def test_archive_links_directories_devices_and_duplicate_members(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                self.replace_archive(self.crafted_tar([("index.html", b"", kind)]))
                with self.assertRaises(ValueError):
                    bundle.validate(self.target, COMMIT)
        self.replace_archive(self.crafted_tar([("index.html", self.html, tarfile.REGTYPE)] * 2))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            bundle.validate(self.target, COMMIT)

    def test_manifest_path_collision_and_input_size_bounds(self):
        for name, metadata in (("assets/INDEX-AbCd_123.js", self.manifest["files"]["assets/index-AbCd_123.js"]),
                               ("../outside", {"sha256": "0" * 64, "size": 1})):
            value = copy.deepcopy(self.manifest)
            value["files"][name] = metadata
            self.write_manifest(value)
            with self.assertRaises(ValueError):
                bundle.validate(self.target, COMMIT)
        for size in (True, -1, bundle.MAX_INDEX + 1):
            value = copy.deepcopy(self.manifest)
            value["files"]["index.html"]["size"] = size
            self.write_manifest(value)
            with self.assertRaises(ValueError):
                bundle.validate(self.target, COMMIT)

    def test_archive_expansion_and_hidden_trailing_payload_rejected(self):
        with patch.object(bundle, "MAX_TAR", 1024):
            with self.assertRaisesRegex(ValueError, "expansion"):
                bundle.validate(self.target, COMMIT)
        raw = gzip.decompress((self.target / bundle.ARCHIVE_NAME).read_bytes())
        self.replace_archive(gzip.compress(raw + b"hidden" + bytes(506), mtime=0))
        with self.assertRaisesRegex(ValueError, "trailer"):
            bundle.validate(self.target, COMMIT)

    def test_existing_destination_and_changed_manifest_are_not_overwritten_or_trusted(self):
        original = (self.target / bundle.MANIFEST_NAME).read_bytes()
        with self.assertRaisesRegex(ValueError, "destination_must_be_empty"):
            build(COMMIT, self.dist, self.target)
        self.assertEqual((self.target / bundle.MANIFEST_NAME).read_bytes(), original)
        supplied = copy.deepcopy(self.manifest)
        supplied["api_compatibility"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "manifest_changed"):
            bundle.archive_files(self.target, supplied)

    def test_publication_uses_create_only_expected_prefix_and_reconciles_same_retry(self):
        client = FakeS3()
        first = publish(COMMIT, self.target, client=client)
        second = publish(COMMIT, self.target, client=client)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "published_pending_independent_host_approval")
        self.assertEqual(len(client.puts), 4)
        self.assertEqual(len(client.heads), 2)
        for call in client.puts:
            self.assertEqual(call["IfNoneMatch"], "*")
            self.assertEqual(call["ServerSideEncryption"], "AES256")
            self.assertTrue(call["Key"].startswith(f"releases/{COMMIT}/frontend/"))
            self.assertEqual(call["Metadata"]["sha256"], hashlib.sha256(call["Body"]).hexdigest())
            self.assertEqual(call["ContentLength"], len(call["Body"]))

    def test_publication_refuses_existing_different_digest_or_size(self):
        for field, value in (("Metadata", {"sha256": "0" * 64}), ("ContentLength", 1)):
            with self.subTest(field=field):
                client = FakeS3()
                publish(COMMIT, self.target, client=client)
                client.objects[f"releases/{COMMIT}/frontend/{bundle.ARCHIVE_NAME}"][field] = value
                with self.assertRaisesRegex(ValueError, "differs"):
                    publish(COMMIT, self.target, client=client)

    def test_publication_does_not_reconcile_other_aws_failures_or_send_invalid_bundle(self):
        client = FakeS3()
        client.error_status = 403
        with self.assertRaises(ClientError):
            publish(COMMIT, self.target, client=client)
        self.assertEqual(client.heads, [])
        (self.target / bundle.ARCHIVE_NAME).write_bytes(b"bad")
        client.puts.clear()
        with self.assertRaises(ValueError):
            publish(COMMIT, self.target, client=client)
        self.assertEqual(client.puts, [])


if __name__ == "__main__":
    unittest.main()
