"""Bounded copy safety with isolated local files and fake R2; never DB/cloud IO."""

import io
import hashlib
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from unittest import mock

from studio_platform.storage import LocalObjectStore, S3ObjectStore
from studio_platform.storage_migration import (
    MigrationError,
    apply_plan,
    create_plan,
    verify_plan,
    verify_rendition_sources,
)
from studio_platform.storage_migration_cli import main as migration_main
from test_platform_storage import FakeClientError, FakeS3, OfflineTest, config


class LostPutResponse(FakeS3):
    lose = True

    def put_object(self, **kwargs):
        response = super().put_object(**kwargs)
        if self.lose:
            self.lose = False
            raise OSError("fake-private-key-and-url-never-log")
        return response


class StorageMigrationTests(OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="sixnine-migration-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.local = LocalObjectStore(self.root / "local")
        self.config = config()
        self.fake = FakeS3(self.config)
        self.remote = S3ObjectStore(self.config, client=self.fake)
        self.receipts = self.root / "private-plan"
        self.item = self.local.write_new(
            "supervan",
            "original",
            io.BytesIO(b"private-original-bytes"),
            filename="source.png",
            content_type="image/png",
        )

    def plan(self):
        return create_plan(self.local.root, self.config, self.receipts)

    def puts(self):
        return sum(name == "put" for name, _ in self.fake.calls)

    def original_receipt(self):
        spec = {
            key: getattr(self.item, key)
            for key in ("key", "size_bytes", "sha256", "content_type")
        }
        return dict(
            owner="supervan",
            storage_binding=hashlib.sha256(
                json.dumps(
                    dict(provider="local", root=str(self.local.root)), sort_keys=True
                ).encode()
            ).hexdigest(),
            asset=dict(original=spec),
            objects=dict(original=dict(spec, phase="verified")),
        )

    def test_display_source_proof_reads_exact_copied_bytes_without_inventory_or_writes(
        self,
    ):
        self.plan()
        apply_plan(self.receipts, self.remote)
        before = {path.name: path.read_bytes() for path in self.receipts.iterdir()}
        puts = self.puts()
        self.local.write_new(
            "supervan", "later", io.BytesIO(b"unrelated retained object")
        )
        with mock.patch.object(
            LocalObjectStore,
            "__init__",
            side_effect=AssertionError("No Local store construction"),
        ):
            proof = verify_rendition_sources(
                self.receipts, self.remote, self.original_receipt()
            )
        self.assertEqual(
            proof["source_binding"], self.original_receipt()["storage_binding"]
        )
        self.assertNotEqual(proof["target_binding"], proof["source_binding"])
        self.assertEqual(self.puts(), puts)
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.receipts.iterdir()}, before
        )
        self.fake.objects[self.item.key]["body"] = b"corrupted-but-metadata-unchanged"
        with self.assertRaisesRegex(MigrationError, "remote_(?:size|hash)_mismatch"):
            verify_rendition_sources(
                self.receipts, self.remote, self.original_receipt()
            )
        self.assertEqual(self.puts(), puts)

    def test_display_source_binding_is_never_inferred_from_matching_copy_entries(self):
        self.plan()
        apply_plan(self.receipts, self.remote)
        receipt = self.original_receipt()
        receipt["storage_binding"] = "0" * 64
        calls = len(self.fake.calls)
        with self.assertRaisesRegex(
            MigrationError, "rendition_copy_source_binding_mismatch"
        ):
            verify_rendition_sources(self.receipts, self.remote, receipt)
        self.assertEqual(len(self.fake.calls), calls)
        receipt = self.original_receipt()
        receipt["owner"] = "foreign-owner"
        with self.assertRaisesRegex(
            MigrationError, "rendition_copy_original_manifest_mismatch"
        ):
            verify_rendition_sources(self.receipts, self.remote, receipt)
        self.assertEqual(len(self.fake.calls), calls)

    def test_explicit_logical_root_requires_protected_mount_and_actual_inode_identity(
        self,
    ):
        self.plan()
        apply_plan(self.receipts, self.remote)
        from studio_platform.storage_migration import _canonical, _sha

        plan = json.loads((self.receipts / "plan.json").read_bytes())
        copied = json.loads((self.receipts / "copy-receipt.json").read_bytes())
        mount = dict(
            Type="bind",
            Source=str(self.root / "host-only-root"),
            Destination=str(self.root),
        )
        plan["source_root"] = str(
            Path(mount["Source"]) / self.local.root.relative_to(mount["Destination"])
        )
        copied["plan_sha256"] = _sha(_canonical(plan))
        (self.receipts / "plan.json").write_bytes(_canonical(plan))
        (self.receipts / "copy-receipt.json").write_bytes(_canonical(copied))
        provenance = dict(
            version=1,
            type="logical_local_mount_provenance",
            logical_root=str(self.local.root),
            source_root=plan["source_root"],
            source_identity=plan["source_identity"],
            source_binding=self.original_receipt()["storage_binding"],
            raw_plan_sha256=_sha(_canonical(plan)),
            canonical_plan_sha256=_sha(_canonical(plan)),
            copy_receipt_sha256=_sha(_canonical(copied)),
            covering_mount=mount,
            matched_runtime_mounts_sha256="a" * 64,
        )
        path = self.receipts / "mount-provenance.json"
        path.write_bytes(_canonical(provenance))
        if os.name != "nt":
            path.chmod(0o644)
            with self.assertRaisesRegex(MigrationError, "private_file_required"):
                verify_rendition_sources(
                    self.receipts,
                    self.remote,
                    self.original_receipt(),
                    logical_root=self.local.root,
                    provenance_file=path,
                )
        path.chmod(0o600)
        with self.assertRaisesRegex(MigrationError, "mount_provenance_required"):
            verify_rendition_sources(
                self.receipts,
                self.remote,
                self.original_receipt(),
                logical_root=self.local.root,
            )
        proof = verify_rendition_sources(
            self.receipts,
            self.remote,
            self.original_receipt(),
            logical_root=self.local.root,
            provenance_file=path,
        )
        self.assertEqual(proof["provenance_sha256"], _sha(path.read_bytes()))
        from studio_platform.storage_migration import verify_artifact_rendition_sources

        artifact = dict(
            phase="settled",
            owner="supervan",
            binding=_sha(_canonical(dict(provider="local", root=str(self.local.root)))),
            roles=dict(
                video=dict(
                    self.original_receipt()["asset"]["original"], status="verified"
                )
            ),
        )
        provenance["source_binding"] = artifact["binding"]
        path.write_bytes(_canonical(provenance))
        artifact_proof = verify_artifact_rendition_sources(
            self.receipts,
            self.remote,
            artifact,
            logical_root=self.local.root,
            provenance_file=path,
        )
        self.assertEqual(artifact_proof["source_binding"], artifact["binding"])
        self.assertNotEqual(artifact_proof["source_binding"], proof["source_binding"])
        self.assertNotEqual(artifact_proof["target_binding"], proof["target_binding"])
        provenance["source_binding"] = self.original_receipt()["storage_binding"]
        path.write_bytes(_canonical(provenance))
        with self.assertRaisesRegex(MigrationError, "mount_provenance_invalid"):
            verify_artifact_rendition_sources(
                self.receipts,
                self.remote,
                artifact,
                logical_root=self.local.root,
                provenance_file=path,
            )
        provenance["covering_mount"]["Source"] = str(self.root / "wrong-retained-root")
        path.write_bytes(_canonical(provenance))
        with self.assertRaisesRegex(MigrationError, "mount_provenance_invalid"):
            verify_rendition_sources(
                self.receipts,
                self.remote,
                self.original_receipt(),
                logical_root=self.local.root,
                provenance_file=path,
            )
        provenance["covering_mount"]["Source"] = mount["Source"] = str(
            self.root / "host-only-root"
        )
        plan["source_identity"][1] += 1
        copied["plan_sha256"] = _sha(_canonical(plan))
        (self.receipts / "plan.json").write_bytes(_canonical(plan))
        (self.receipts / "copy-receipt.json").write_bytes(_canonical(copied))
        provenance.update(
            source_identity=plan["source_identity"],
            raw_plan_sha256=_sha(_canonical(plan)),
            canonical_plan_sha256=_sha(_canonical(plan)),
            copy_receipt_sha256=_sha(_canonical(copied)),
        )
        path.write_bytes(_canonical(provenance))
        with self.assertRaisesRegex(MigrationError, "source_identity_changed"):
            verify_rendition_sources(
                self.receipts,
                self.remote,
                self.original_receipt(),
                logical_root=self.local.root,
                provenance_file=path,
            )

    def test_plan_is_offline_then_copy_uses_same_key_hash_mime_and_keeps_local(self):
        second = self.local.write_new(
            "superdan",
            "output",
            io.BytesIO(b"separate-audio"),
            filename="verified.flac",
            content_type="audio/flac",
        )
        planned = self.plan()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(planned["objects"], 2)
        result = apply_plan(self.receipts, self.remote)
        self.assertEqual(result["objects"], 2)
        self.assertFalse(result["writer_fence_verified"])
        self.assertFalse(result["database_accessed"])
        self.assertEqual(set(self.fake.objects), {self.item.key, second.key})
        for item in (self.item, second):
            self.assertEqual(self.remote.stat(item.key).sha256, item.sha256)
            self.assertEqual(self.remote.stat(item.key).content_type, item.content_type)
            with (
                self.local.open(item.key) as source,
                self.remote.open(item.key) as target,
            ):
                self.assertEqual(source.read(), target.read())
        self.assertTrue(
            all(
                call["IfNoneMatch"] == "*"
                for name, call in self.fake.calls
                if name == "put"
            )
        )
        self.assertEqual(apply_plan(self.receipts, self.remote)["objects"], 2)
        self.assertEqual(self.puts(), 2)
        self.assertEqual(verify_plan(self.receipts, self.remote)["objects"], 2)
        self.assertFalse(any(name == "delete" for name, _ in self.fake.calls))

    def test_existing_identical_object_is_adopted_without_put(self):
        self.plan()
        with self.local.open(self.item.key) as source:
            self.remote.put(
                self.item.key,
                source,
                content_type=self.item.content_type,
                expected_sha256=self.item.sha256,
            )
        before = self.puts()
        apply_plan(self.receipts, self.remote)
        self.assertEqual(self.puts(), before)

    def test_existing_different_object_is_never_overwritten(self):
        self.plan()
        self.remote.put(
            self.item.key, io.BytesIO(b"other"), content_type=self.item.content_type
        )
        before = self.puts()
        with self.assertRaisesRegex(MigrationError, "remote_metadata_mismatch"):
            apply_plan(self.receipts, self.remote)
        self.assertEqual(self.puts(), before)
        self.assertEqual(self.fake.objects[self.item.key]["body"], b"other")

    def test_matching_head_metadata_does_not_replace_full_get_hash(self):
        self.plan()
        with self.local.open(self.item.key) as source:
            self.remote.put(
                self.item.key,
                source,
                content_type=self.item.content_type,
                expected_sha256=self.item.sha256,
            )
        self.fake.objects[self.item.key]["body"] = b"x" * self.item.size_bytes
        before = self.puts()
        with self.assertRaisesRegex(MigrationError, "remote_hash_mismatch"):
            apply_plan(self.receipts, self.remote)
        self.assertEqual(self.puts(), before)

    def test_lost_put_acknowledgement_resumes_exact_key_without_second_write(self):
        self.fake = LostPutResponse(self.config)
        self.remote = S3ObjectStore(self.config, client=self.fake)
        self.plan()
        with self.assertRaisesRegex(MigrationError, "write_unconfirmed"):
            apply_plan(self.receipts, self.remote)
        receipt = json.loads((self.receipts / "copy-receipt.json").read_text())
        self.assertEqual(receipt["objects"][self.item.key], "unknown")
        self.assertEqual(
            apply_plan(self.receipts, self.remote)["state"], "migration_copy_verified"
        )
        self.assertEqual(self.puts(), 1)

    def test_uncertain_missing_object_never_retries_put(self):
        self.plan()
        original = self.fake.put_object
        self.fake.put_object = mock.Mock(side_effect=OSError("do-not-print-secret"))
        with self.assertRaises(MigrationError):
            apply_plan(self.receipts, self.remote)
        self.fake.put_object = original
        with self.assertRaisesRegex(MigrationError, "unknown_write_requires_review"):
            apply_plan(self.receipts, self.remote)
        self.assertEqual(self.puts(), 0)

    def test_changed_source_refuses_before_remote_access(self):
        self.plan()
        (self.local._directory(self.item.key) / "blob").write_bytes(
            b"x" * self.item.size_bytes
        )
        with self.assertRaises(Exception):
            apply_plan(self.receipts, self.remote)
        self.assertEqual(self.fake.calls, [])

    def test_new_local_publish_requires_fresh_plan_no_partial_scope_claim(self):
        self.plan()
        self.local.write_new("superdan", "new", io.BytesIO(b"new"))
        with self.assertRaisesRegex(MigrationError, "inventory_changed"):
            apply_plan(self.receipts, self.remote)
        self.assertEqual(self.fake.calls, [])

    def test_earlier_source_mutation_during_later_copy_is_not_reported_complete(self):
        self.local.write_new("superdan", "later", io.BytesIO(b"later-object"))
        self.plan()
        objects = json.loads((self.receipts / "plan.json").read_text())["objects"]
        first = objects[0]
        original = self.fake.put_object

        def change_prior(**kwargs):
            response = original(**kwargs)
            if kwargs["Key"] == objects[1]["key"]:
                (self.local._directory(first["key"]) / "blob").write_bytes(
                    b"x" * first["size_bytes"]
                )
            return response

        self.fake.put_object = change_prior
        with self.assertRaises(MigrationError):
            apply_plan(self.receipts, self.remote)
        receipt = json.loads((self.receipts / "copy-receipt.json").read_text())
        self.assertEqual(receipt["phase"], "copying")

    def test_target_identity_change_refuses_before_remote_access(self):
        self.plan()
        changed = config(bucket="another-private-bucket")
        fake = FakeS3(changed)
        with self.assertRaisesRegex(MigrationError, "binding_invalid"):
            apply_plan(self.receipts, S3ObjectStore(changed, client=fake))
        self.assertEqual(fake.calls, [])

    def test_oversize_and_mislocated_logical_key_refuse_before_plan_publish(self):
        with self.assertRaisesRegex(MigrationError, "single_put_limit"):
            create_plan(self.local.root, config(max_single_put_bytes=3), self.receipts)
        self.assertFalse(self.receipts.exists())
        metadata = self.local._directory(self.item.key) / "meta.json"
        payload = json.loads(metadata.read_text())
        payload["key"] = "owners/supervan/assets/unrelated/blob"
        metadata.write_text(json.dumps(payload))
        with self.assertRaisesRegex(MigrationError, "logical_key_mismatch"):
            self.plan()
        self.assertFalse(self.receipts.exists())

    def arguments(self, action):
        return [
            action,
            "--receipt-dir",
            str(self.receipts),
            "--endpoint",
            self.config.endpoint_url,
            "--bucket",
            self.config.bucket,
            "--profile",
            self.config.profile,
        ]

    def test_cli_offline_plan_never_loads_credentials_or_initializes_client(self):
        loader, factory = (
            mock.Mock(side_effect=AssertionError()),
            mock.Mock(side_effect=AssertionError()),
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = migration_main(
                self.arguments("plan") + ["--source-root", str(self.local.root)],
                credentials_loader=loader,
                store_factory=factory,
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "migration_planned")
        loader.assert_not_called()
        factory.assert_not_called()

    def test_cli_apply_and_verify_emit_aggregates_not_object_keys(self):
        self.plan()
        loader = mock.Mock(return_value=object())
        factory = mock.Mock(return_value=self.remote)
        for action in ("apply", "verify"):
            output = io.StringIO()
            with redirect_stdout(output):
                code = migration_main(
                    self.arguments(action)
                    + ["--credentials-file", str(self.root / "protected")],
                    credentials_loader=loader,
                    store_factory=factory,
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["objects"], 1)
            self.assertNotIn(self.item.key, output.getvalue())
            self.assertNotIn("secret", output.getvalue())

    def test_cli_secret_loader_failure_is_redacted_and_never_calls_client(self):
        self.plan()
        loader = mock.Mock(
            side_effect=RuntimeError(
                "SECRET-fake-key https://private.invalid?signature=secret"
            )
        )
        factory, output = mock.Mock(), io.StringIO()
        with redirect_stdout(output):
            code = migration_main(
                self.arguments("apply")
                + ["--credentials-file", str(self.root / "protected")],
                credentials_loader=loader,
                store_factory=factory,
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue())["reason_code"],
            "migration_configuration_or_transport_failed",
        )
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("signature", output.getvalue())
        factory.assert_not_called()

    def test_overlapping_destination_and_hardlinked_source_are_refused(self):
        with self.assertRaisesRegex(MigrationError, "overlaps_source"):
            create_plan(self.local.root, self.config, self.local.root / "plans")
        blob = self.local._directory(self.item.key) / "blob"
        os.link(blob, self.root / "extra-link")
        with self.assertRaises(Exception):
            self.plan()
        self.assertFalse(self.receipts.exists())

    def test_symlink_object_directory_is_refused(self):
        directory = self.local._directory(self.item.key)
        moved = self.root / "moved-object"
        directory.rename(moved)
        try:
            directory.symlink_to(moved, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit test symlinks")
        with self.assertRaises(Exception):
            self.plan()
        self.assertFalse(self.receipts.exists())
