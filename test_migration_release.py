"""Additive host release boundaries; synthetic artifacts/fake Docker only."""
from contextlib import ExitStack
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
COMMIT = "a" * 40
PROFILE = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


validator = load("migration_release_validator", ROOT / "deploy/platform/check_config.py")
with patch.dict(sys.modules, {"check_config": validator}):
    release = load("migration_release_original", ROOT / "deploy/platform/release.py")
with patch.dict(sys.modules, {"release": release}):
    maintenance = load("migration_release_helper", ROOT / "deploy/dstack/maintenance.py")
from tools import build_platform_release as builder
from tools import publish_aws_release as publisher
from botocore.exceptions import ClientError


class MigrationArtifacts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Synthetic host artifacts belong to the unprivileged test runner.
        # Inject host ownership only for this fixture, retaining the real
        # regular-file/link/size checks and every production ownership guard.
        regular = release.regular
        def fixture_regular(path, *, root_owned=False, maximum=None):
            if root_owned:
                self.assertTrue(Path(path).is_relative_to(self.root))
            return regular(path, maximum=maximum)
        ownership = patch.object(release, "regular", side_effect=fixture_regular)
        ownership.start(); self.addCleanup(ownership.stop)
        directories = patch.object(maintenance, "protected_directory",
            side_effect=lambda path: self.assertTrue(Path(path).is_relative_to(self.root) and Path(path).is_dir()))
        directories.start(); self.addCleanup(directories.stop)
        chown = patch.object(maintenance.os, "chown", create=True)
        chown.start(); self.addCleanup(chown.stop)
        self.platform = self.root / "platform"; self.platform.mkdir()
        (self.platform / "release-manifest.json").write_text(json.dumps({"commit": COMMIT}))
        raw = json.dumps({"os": "linux", "architecture": "amd64", "config": {
            "Labels": {"org.opencontainers.image.revision": COMMIT}}}).encode()
        self.image_id = "sha256:" + hashlib.sha256(raw).hexdigest()
        image = io.BytesIO()
        with tarfile.open(fileobj=image, mode="w") as archive:
            for name, data in ((self.image_id[7:] + ".json", raw), ("manifest.json", json.dumps([{
                    "Config": self.image_id[7:] + ".json", "RepoTags": ["sixnine-dispatch:" + COMMIT], "Layers": []}]).encode())):
                member = tarfile.TarInfo(name); member.size = len(data); archive.addfile(member, io.BytesIO(data))
        inspected = [{"Id": self.image_id, "Architecture": "amd64", "Os": "linux", "Config": {
            "Labels": {"org.opencontainers.image.revision": COMMIT}}}]
        process = Mock(); process.__enter__ = Mock(return_value=process); process.__exit__ = Mock(return_value=False)
        process.stdout = io.BytesIO(image.getvalue()); process.wait.return_value = 0
        self.artifact = self.root / "artifact"
        with patch.object(builder.subprocess, "check_output", return_value=json.dumps(inspected).encode()), \
                patch.object(builder.subprocess, "Popen", return_value=process):
            self.value = builder.build_migration(COMMIT, self.platform, self.artifact, [(PROFILE, "fl")])

    def test_exact_dispatch_and_public_source_set_round_trip(self):
        value = maintenance.manifest(self.artifact, COMMIT,
            platform_manifest_sha256=release.checksum(self.platform / "release-manifest.json"))
        self.assertEqual(value, self.value)
        self.assertEqual(release.validate_image_archive(self.artifact / "dispatch-image.tar.gz",
            {"commit": COMMIT, **value["dispatch"]}), (self.image_id,))
        target = self.root / "sources"
        with patch.object(maintenance.os, "chown", create=True):
            maintenance.extract_sources(self.artifact / "source-set.tar.gz", target, value["source_index_sha256"])
        self.assertEqual(len(json.loads((target / "index.json").read_text())["sources"]), 1)
        directory = self.root / "prepared"; directory.mkdir(); target.rename(directory / "sources")
        target = directory / "sources"
        maintenance.default_profile(directory, {"SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID": PROFILE})
        with self.assertRaisesRegex(release.ReleaseError, "default_profile_not_packaged"):
            maintenance.default_profile(directory, {"SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID": "another-profile"})
        maintenance.verify_sources(target, value["source_index_sha256"])
        (target / PROFILE / "fl/gpu-0/wangp-bootstrap.py").write_text("changed")
        with self.assertRaisesRegex(release.ReleaseError, "source_set_changed"):
            maintenance.verify_sources(target, value["source_index_sha256"])

    def test_optional_admin_and_stage_query_config_preserve_old_host_contract(self):
        directory = self.root / "migration"
        directory.mkdir()
        environment = {key: str(self.root / "protected" / key.lower()) for key in maintenance.PATH_FIELDS}
        environment.update(SIXNINE_OPERATOR_CAPACITY_OWNERS="superdan,supervan",
            SIXNINE_HATCHET_POSTGRES_VOLUME="existing-postgres", SIXNINE_HATCHET_CONFIG_VOLUME="existing-config",
            SIXNINE_MIGRATION_GENERATION_ENABLED="0", SIXNINE_MIGRATION_EXECUTION_BACKEND="disabled",
            SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID=PROFILE)
        def write():
            (directory / "paths.json").write_text(json.dumps({"version":1, "environment":environment,
                "host_memory_mib":3840}))
        write()
        maintenance.settings(self.root)
        environment.update(SIXNINE_OPERATOR_ADMIN_OWNERS="superdan", SIXNINE_STAGE_QUERY_CONFIG_FILE="")
        write()
        maintenance.settings(self.root)
        environment["SIXNINE_STAGE_QUERY_CONFIG_FILE"]=str(self.root / "protected" / "stage-query.json")
        write()
        maintenance.settings(self.root)
        environment["SIXNINE_STAGE_QUERY_CONFIG_FILE"]="relative.json"
        write()
        with self.assertRaisesRegex(release.ReleaseError, "path_reference_invalid"):
            maintenance.settings(self.root)
        environment["SIXNINE_STAGE_QUERY_CONFIG_FILE"]=""
        environment["SIXNINE_OPERATOR_ADMIN_OWNERS"]="other"
        write()
        with self.assertRaisesRegex(release.ReleaseError, "admin_reference_invalid"):
            maintenance.settings(self.root)

    def test_r2_host_settings_require_complete_exact_opt_in_without_secret_values(self):
        local = {}
        maintenance.storage_settings(local)
        maintenance.storage_settings({"SIXNINE_STORAGE_PROVIDER": "local"})
        environment = {"SIXNINE_STORAGE_PROVIDER": "r2",
            "SIXNINE_STORAGE_ENDPOINT": "https://"+"a"*32+".r2.cloudflarestorage.com",
            "SIXNINE_STORAGE_REGION": "auto", "SIXNINE_STORAGE_BUCKET": "sixnine-synthetic-media",
            "SIXNINE_STORAGE_PROFILE": "cloudflare-r2--rig-root",
            "SIXNINE_STORAGE_CREDENTIAL_FILE": "/run/sixnine-secrets/r2_storage"}
        maintenance.storage_settings(environment)
        for key in environment:
            with self.subTest(missing=key), self.assertRaises(release.ReleaseError):
                maintenance.storage_settings({name: value for name, value in environment.items() if name != key})
        for change in ({"SIXNINE_STORAGE_PROVIDER": "local"}, {"SIXNINE_STORAGE_PROFILE": "cloudflare-r2--other"},
                {"SIXNINE_STORAGE_ENDPOINT": "https://outside.example"},
                {"SIXNINE_STORAGE_CREDENTIAL_FILE": "/run/sixnine-secrets/grafana_cloud"}):
            with self.subTest(changed=next(iter(change))), self.assertRaises(release.ReleaseError):
                maintenance.storage_settings(dict(environment, **change))
        directory = self.root / "migration"; directory.mkdir()
        base = {key: str(self.root / "protected" / key.lower()) for key in maintenance.PATH_FIELDS}
        base.update(SIXNINE_OPERATOR_CAPACITY_OWNERS="superdan,supervan",
            SIXNINE_HATCHET_POSTGRES_VOLUME="existing-postgres", SIXNINE_HATCHET_CONFIG_VOLUME="existing-config",
            SIXNINE_MIGRATION_GENERATION_ENABLED="0", SIXNINE_MIGRATION_EXECUTION_BACKEND="disabled",
            SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID=PROFILE)
        def write(value):
            (directory / "paths.json").write_text(json.dumps({"version":1, "environment":value,
                "host_memory_mib":3840}))
        write(dict(base, **environment))
        if os.name == "posix":
            maintenance.settings(self.root)  # Exact /run binding is a Linux host contract.
        write(dict(base, **environment, R2_SECRET_ACCESS_KEY="SYNTHETIC_FORBIDDEN"))
        with self.assertRaisesRegex(release.ReleaseError, "migration_paths_invalid"):
            maintenance.settings(self.root)

    def test_optional_storyboard_flag_is_default_off_and_closed_to_explicit_accounts(self):
        directory = self.root / "migration"; directory.mkdir()
        environment = {key: str(self.root / "protected" / key.lower()) for key in maintenance.PATH_FIELDS}
        environment.update(SIXNINE_OPERATOR_CAPACITY_OWNERS="superdan,supervan",
            SIXNINE_HATCHET_POSTGRES_VOLUME="existing-postgres", SIXNINE_HATCHET_CONFIG_VOLUME="existing-config",
            SIXNINE_MIGRATION_GENERATION_ENABLED="0", SIXNINE_MIGRATION_EXECUTION_BACKEND="disabled",
            SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID=PROFILE)
        def read():
            (directory / "paths.json").write_text(json.dumps({"version": 1, "environment": environment,
                "host_memory_mib": 3840}))
            return maintenance.settings(self.root)[0]["environment"]
        self.assertNotIn("SIXNINE_STORYBOARD_OWNERS", read())
        for value in ("", "superdan", "supervan", "superdan,supervan"):
            environment["SIXNINE_STORYBOARD_OWNERS"] = value
            self.assertEqual(read()["SIXNINE_STORYBOARD_OWNERS"], value)
        for value in ("other", "superdan,superdan", "superdan,", "superdan, supervan", True):
            environment["SIXNINE_STORYBOARD_OWNERS"] = value
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                read()

    def test_tamper_wrong_platform_extra_file_and_hardlink_rejected(self):
        with self.assertRaisesRegex(release.ReleaseError, "platform_binding_changed"):
            maintenance.manifest(self.artifact, COMMIT, platform_manifest_sha256="b" * 64)
        path = self.artifact / "broker.example.json"
        original = path.read_bytes(); path.write_bytes(b"changed")
        with self.assertRaisesRegex(release.ReleaseError, "checksum_mismatch"):
            maintenance.manifest(self.artifact, COMMIT)
        path.write_bytes(original)
        (self.artifact / "private.env").write_text("synthetic not allowed")
        with self.assertRaisesRegex(release.ReleaseError, "files_invalid"):
            maintenance.manifest(self.artifact, COMMIT)
        (self.artifact / "private.env").unlink()
        os.link(path, self.root / "extra-link")
        with self.assertRaisesRegex(release.ReleaseError, "not_regular"):
            maintenance.manifest(self.artifact, COMMIT)

    def test_source_archive_traversal_and_links_cannot_escape(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            archive_path = self.root / "unsafe.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                member = tarfile.TarInfo(name); member.type = kind; member.linkname = "../outside"
                archive.addfile(member)
            with self.assertRaisesRegex(release.ReleaseError, "archive_unsafe"):
                maintenance.extract_sources(archive_path, self.root / ("target-" + kind.decode()), "b" * 64)
            self.assertFalse((self.root / "outside").exists())

    def test_self_consistent_artifact_is_not_its_own_approval(self):
        (self.root / "approved-migrations").mkdir()
        with self.assertRaises(FileNotFoundError):
            maintenance.approved(self.root, self.artifact, COMMIT)
        approved = self.root / "approved-migrations" / (COMMIT + ".sha256")
        approved.write_text("b" * 64)
        with patch.object(maintenance, "protected_directory"), patch.object(release, "regular"), \
                self.assertRaisesRegex(release.ReleaseError, "approval_missing"):
            maintenance.approved(self.root, self.artifact, COMMIT)

    def test_incoming_helper_is_never_the_execution_trust_path(self):
        with patch.object(release, "regular"), patch.object(release, "checksum", side_effect=["a" * 64, "b" * 64]), \
                patch.object(maintenance.importlib.util, "spec_from_file_location") as importer, \
                self.assertRaisesRegex(release.ReleaseError, "helpers_need_reviewed_update"):
            maintenance.installed_checker(self.artifact)
        importer.assert_not_called()

    def test_publication_is_conditional_immutable_and_never_self_approves(self):
        class S3:
            def __init__(self): self.objects = {}
            def put_object(self, **request):
                self.assertions(request)
                if request["Key"] in self.objects:
                    raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
                self.objects[request["Key"]] = (request["Body"].read(), request["Metadata"])
            def assertions(self, request):
                assert request["IfNoneMatch"] == "*" and request["ServerSideEncryption"] == "AES256"
                assert request["Key"].startswith("releases/" + COMMIT + "/migration/")
            def head_object(self, **request):
                data, metadata = self.objects[request["Key"]]
                return {"Metadata": metadata, "ContentLength": len(data)}
        api = S3()
        first = publisher.publish_migration(COMMIT, self.artifact, api=api)
        self.assertEqual(first["status"], "migration_published_pending_independent_host_approval")
        self.assertEqual(publisher.publish_migration(COMMIT, self.artifact, api=api), first)
        self.assertEqual(len(api.objects), len(maintenance.FILES) + 1)
        self.assertFalse((self.root / "approved-migrations").exists())
        key = "releases/" + COMMIT + "/migration/broker.example.json"
        data, metadata = api.objects[key]; api.objects[key] = (data + b"changed", metadata)
        with self.assertRaisesRegex(ValueError, "immutable migration artifact differs"):
            publisher.publish_migration(COMMIT, self.artifact, api=api)

    def test_unapproved_fetch_stops_before_large_image_and_preserves_retry(self):
        (self.root / "incoming-migrations").mkdir()
        (self.root / "approved-migrations").mkdir()
        fetcher = load("migration_release_fetcher", ROOT / "deploy/platform/fetch_release_s3.py")
        calls = []
        class S3:
            def get_object(inner, **request):
                name = request["Key"].split("/migration/", 1)[1]; calls.append(name)
                data = (self.artifact / name).read_bytes()
                return {"ContentLength": len(data), "Body": io.BytesIO(data)}
        with patch.dict(sys.modules, {"fetch_release_s3": fetcher}):
            with self.assertRaises(FileNotFoundError):
                maintenance.fetch(self.root, COMMIT, api=S3())
            self.assertEqual(calls, [maintenance.MANIFEST])
            (self.root / "approved-migrations" / (COMMIT + ".sha256")).write_text(
                release.checksum(self.artifact / maintenance.MANIFEST))
            target = maintenance.fetch(self.root, COMMIT, api=S3())
            self.assertEqual(maintenance.manifest(target, COMMIT), self.value)
            self.assertEqual(maintenance.fetch(self.root, COMMIT, api=S3()), target)

    def test_interrupted_preparation_does_not_publish_half_bundle(self):
        incoming = self.root / "incoming-migrations"; incoming.mkdir()
        self.artifact.rename(incoming / COMMIT)
        (self.root / "approved-migrations").mkdir()
        (self.root / "approved-migrations" / (COMMIT + ".sha256")).write_text(
            release.checksum(incoming / COMMIT / maintenance.MANIFEST))
        parent = self.root / "migration/releases"; parent.mkdir(parents=True)
        with patch.object(maintenance, "extract_sources", side_effect=OSError("synthetic copy interruption")):
            with self.assertRaises(OSError): maintenance.prepare(self.root, COMMIT, self.platform)
        self.assertFalse((parent / COMMIT).exists())
        self.assertEqual(list(parent.iterdir()), [])
        prepared, value = maintenance.prepare(self.root, COMMIT, self.platform)
        self.assertEqual(value, self.value)
        self.assertIn(str(prepared / "artifact/hatchet-entrypoint.sh").replace("\\", "\\\\"),
            (prepared / "overlay.yaml").read_text())
        self.assertEqual(maintenance.prepare(self.root, COMMIT, self.platform), (prepared, value))


class MigrationGuardAndDeployment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root / "migration").mkdir()
        self.marker = self.root / "migration/active.json"
        self.marker.write_text(json.dumps({"version": 1, "active": True, "paths_sha256": "b" * 64, "current": "c" * 40}))

    def test_future_ordinary_apply_routes_through_installed_helper(self):
        fake = Mock()
        with patch.object(release, "regular"), patch.object(release, "migration_helper", return_value=fake), \
                patch.object(release, "prepare_bundle") as old_prepare:
            release.apply_locked(self.root, COMMIT)
        fake.apply_locked.assert_called_once_with(self.root, COMMIT); old_prepare.assert_not_called()
        self.marker.write_text(json.dumps({"version": 1, "active": False}))
        with patch.object(release, "regular"), self.assertRaisesRegex(release.ReleaseError, "explicit_maintenance"):
            release.migration_deployment_enabled(self.root)

    def test_still_running_legacy_controller_blocks_before_new_service_start(self):
        with patch.object(release, "gpu_deployment_context", return_value=None), \
                patch.object(release, "command", return_value=b"old-controller"):
            with self.assertRaisesRegex(release.ReleaseError, "legacy_controller_still_running"):
                maintenance.guard_legacy(self.root)

    def handoff(self):
        document = {"version": 1, "purpose": "legacy-cpu-authority-handoff-only",
            "host_id": "i-03d81d2d153b5e2fd", "target_commit": COMMIT, "checked_at": 1000,
            "markers_sha256": {name: None for name in maintenance.LEGACY_MARKERS},
            "providers": {name: {"account_scope_verified": True, "complete": True,
                "listed_allocations": 0, "checked_at": 999} for name in ("lium", "targon")},
            "business": {"nonterminal_jobs": 0, "nonterminal_attempts": 0,
                "admission_closed_before_probe": True, "checked_at": 1000},
            "preservation": {"original_markers": "unchanged", "ledger": "unchanged",
                "budgets_and_reservations": "unchanged", "provider_deletion": "not_asserted",
                "final_billing": "not_asserted", "legacy_retirement": "deferred"},
            "evidence_refs": ["ssm:original-account-list-and-locked-pg-probe"]}
        receipt = self.root / "migration/legacy-cpu-handoff.json"
        approval = self.root / "migration/approved-legacy-cpu-handoff.sha256"
        def write(value):
            receipt.write_text(json.dumps(value)); approval.write_text(release.checksum(receipt))
        write(document)
        return document, receipt, approval, write

    def test_protected_fresh_cpu_handoff_preserves_original_pins_and_future_new_work(self):
        document, receipt, approval, write = self.handoff()
        marker = self.root / "operator-capacity/active.json"; marker.parent.mkdir()
        original = b'{"active":true,"state":"running","original_billing":"unknown"}'
        marker.write_bytes(original)
        document["markers_sha256"]["operator-capacity/active.json"] = release.checksum(marker); write(document)
        reader = maintenance.Path.read_text
        def read(path, *args, **kwargs):
            return document["host_id"] if path.as_posix() == "/var/lib/cloud/data/instance-id" else reader(path, *args, **kwargs)
        with patch.object(maintenance.Path, "read_text", read), patch.object(release, "regular"), \
                patch.object(maintenance, "protected_directory"), patch.object(release, "command", return_value=b""), \
                patch.object(release, "gpu_deployment_context") as old_guard, patch.object(maintenance.time, "time", return_value=1001):
            digest = maintenance.guard_legacy(self.root, commit=COMMIT)
            self.assertEqual(digest, release.checksum(receipt)); old_guard.assert_not_called()
            self.assertEqual(marker.read_bytes(), original)
            with patch.object(release, "command", return_value=b"old-app-can-accept-new-work"), \
                    self.assertRaisesRegex(release.ReleaseError, "admission_not_fenced"):
                maintenance.guard_legacy(self.root, commit=COMMIT)
            active = {"current": COMMIT, "legacy_cpu_handoff_sha256": digest}
            self.assertEqual(maintenance.legacy_handoff(self.root, "d" * 40, active=active, now=9000), digest)
            with patch.object(release, "command", return_value=b"actual-old-cpu-process"), \
                    self.assertRaisesRegex(release.ReleaseError, "legacy_controller_still_running"):
                maintenance.guard_legacy(self.root, commit="d" * 40, active=active)
            marker.write_bytes(original + b" ")
            with self.assertRaisesRegex(release.ReleaseError, "legacy_marker_changed"):
                maintenance.legacy_handoff(self.root, COMMIT, now=1001)

    def test_cpu_handoff_rejects_stale_unknown_scope_jobs_unfenced_admission_or_wrong_approval(self):
        document, receipt, approval, write = self.handoff()
        reader = maintenance.Path.read_text
        def read(path, *args, **kwargs):
            return document["host_id"] if path.as_posix() == "/var/lib/cloud/data/instance-id" else reader(path, *args, **kwargs)
        with patch.object(maintenance.Path, "read_text", read), patch.object(release, "regular"), \
                patch.object(maintenance, "protected_directory"):
            with self.assertRaisesRegex(release.ReleaseError, "handoff_stale"):
                maintenance.legacy_handoff(self.root, COMMIT, now=1301)
            for mutate, code in (
                    (lambda d: d.update(checked_at=1002), "handoff_stale"),
                    (lambda d: d.update(host_id="i-0123456789abcdef0"), "handoff_wrong_host"),
                    (lambda d: d["providers"]["targon"].update(complete=False), "account_observation_invalid"),
                    (lambda d: d["providers"]["lium"].update(checked_at=0), "account_observation_stale"),
                    (lambda d: d["providers"]["lium"].update(checked_at=1002), "account_observation_stale"),
                    (lambda d: d["business"].update(nonterminal_jobs=1), "handoff_invalid"),
                    (lambda d: d["business"].update(nonterminal_attempts=True), "handoff_invalid"),
                    (lambda d: d["business"].update(admission_closed_before_probe=False), "handoff_invalid"),
                    (lambda d: d["business"].update(checked_at=0), "business_observation_stale"),
                    (lambda d: d["preservation"].update(final_billing="settled"), "handoff_invalid"),
                    (lambda d: d["preservation"].update(provider_deletion="confirmed"), "handoff_invalid")):
                changed = json.loads(json.dumps(document)); mutate(changed); write(changed)
                with self.assertRaisesRegex(release.ReleaseError, code):
                    maintenance.legacy_handoff(self.root, COMMIT, now=1001)
            write(document); approval.write_text("b" * 64)
            with self.assertRaisesRegex(release.ReleaseError, "independent_approval_missing"):
                maintenance.legacy_handoff(self.root, COMMIT, now=1001)

    def test_complete_namespace_update_preserves_volumes_and_failed_release_state(self):
        old = "c" * 40; state = {"current": old, "previous": "d" * 40, "dependencies": {"db": "original"}}
        (self.root / "release-state.json").write_text(json.dumps(state))
        config = {"host_memory_mib": 3840, "environment": {
            "SIXNINE_HATCHET_CONFIG_VOLUME": "original-config", "SIXNINE_HATCHET_POSTGRES_VOLUME": "original-postgres"}}
        base = self.root / "releases" / COMMIT; directory = self.root / "migration/releases" / COMMIT
        app = {"image": "platform", "image_id": "sha256:" + "1" * 64, "archive_image_ids": ("sha256:" + "1" * 64,)}
        dispatch = {"dispatch": {"image": "dispatch", "image_id": "sha256:" + "2" * 64}}
        calls, writes = [], []
        checker = Mock()
        changes = {"regular": lambda *a, **k: None, "approved_manifest": lambda *a: None,
            "prepare_bundle": lambda *a: base, "deployment_environment": lambda *a: {},
            "pinned_dependencies": lambda *a: state["dependencies"], "load_approved_image": lambda *a: app,
            "validate_image_archive": lambda *a: ("sha256:" + "2" * 64,), "command": lambda *a, **k: b"",
            "checksum": lambda *a: "b" * 64, "wait_ready": lambda *a: None,
            "retire_frontend_pointer": lambda *a: None, "wait_proxy_stable": lambda *a: None,
            "write_state": lambda root, value: writes.append(value)}
        with ExitStack() as stack:
            for name, value in changes.items(): stack.enter_context(patch.object(release, name, side_effect=value))
            stack.enter_context(patch.object(maintenance, "settings", return_value=(config, "b" * 64)))
            stack.enter_context(patch.object(maintenance, "guard_legacy"))
            stack.enter_context(patch.object(maintenance, "default_profile"))
            stack.enter_context(patch.object(maintenance, "prepare", return_value=(directory, dispatch)))
            stack.enter_context(patch.object(maintenance, "installed_checker", return_value=checker))
            stack.enter_context(patch.object(maintenance, "pinned_loaded_image", side_effect=lambda value, env: value["image"] + "@" + value["image_id"]))
            stack.enter_context(patch.object(maintenance, "atomic"))
            stack.enter_context(patch.object(maintenance, "compose", side_effect=lambda b, d, e, *a, **k: calls.append((a, dict(e))) or b""))
            verified = stack.enter_context(patch.object(maintenance, "verify_services"))
            maintenance.apply_locked(self.root, COMMIT)
            self.assertEqual(calls[0][0], ("stop", *maintenance.SHARED))
            update = next(args for args, _ in calls if "--force-recreate" in args)
            self.assertEqual(update[-5:], ("app", *maintenance.SHARED))
            self.assertTrue(all(env["SIXNINE_HATCHET_POSTGRES_VOLUME"] == "original-postgres" for _, env in calls))
            self.assertFalse(any("down" in args or "-v" in args for args, _ in calls))
            self.assertEqual(writes[-1]["status"], "app_ready")
            verified.side_effect = release.ReleaseError("synthetic namespace failure")
            stack.enter_context(patch.object(maintenance.time, "monotonic", side_effect=[0, 181]))
            with self.assertRaisesRegex(release.ReleaseError, "original_state_preserved"):
                maintenance.apply_locked(self.root, COMMIT)
            self.assertEqual(writes[-1]["current"], old)
            self.assertEqual(writes[-1]["pending"], COMMIT)
            self.assertEqual(writes[-1]["status"], "migration_failed_needs_reconciliation")

    def test_actual_stale_network_namespace_is_not_ready(self):
        app_id = "1" * 64
        def inspect(args, **kwargs):
            name = names[args[1]]
            return json.dumps([{"Id": args[1], "Name": "container-" + name,
                "Config": {"Labels": {"com.docker.compose.project": "sixnine-platform", "com.docker.compose.service": name}},
                "Image": "sha256:" + "a" * 64, "State": {"Running": True, "Health": {"Status": "healthy"}},
                "HostConfig": {"NetworkMode": "container:" + ("9" * 64 if name == "dispatch" else app_id)}}]).encode()
        names = {str(i) * 64: name for i, name in enumerate(("app", *maintenance.SHARED, "hatchet-db"), 1)}
        identities = {name: ident for ident, name in names.items()}
        expected = {"archive_image_ids": ("sha256:" + "a" * 64,)}
        with patch.object(maintenance, "compose", side_effect=lambda b, d, e, *a, **k: identities[a[-1]].encode()), \
                patch.object(release, "command", side_effect=inspect), \
                self.assertRaisesRegex(release.ReleaseError, "shared_namespace_stale"):
            maintenance.verify_services(self.root, self.root, {}, expected, expected)

    def test_readiness_checks_actual_original_broker_volume_and_pinned_images(self):
        names = ("app", *maintenance.SHARED, "hatchet-db")
        ids = {name: str(index) * 64 for index, name in enumerate(names, 1)}
        app_image, dispatch_image = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        environment = {"SIXNINE_HATCHET_CONFIG_VOLUME": "original-config", "SIXNINE_HATCHET_POSTGRES_VOLUME": "original-postgres"}
        values = {}
        for index, name in enumerate(names, 1):
            values[ids[name]] = {"Id": ids[name], "Name": "/container-" + name,
                "Config": {"Labels": {"com.docker.compose.project": "sixnine-platform", "com.docker.compose.service": name}},
                "Image": app_image if name == "app" else dispatch_image if name in {"dispatch", "controller"} else "sha256:" + str(index) * 64,
                "State": {"Running": True, "Restarting": False, "Health": {"Status": "healthy"}},
                "HostConfig": {"NetworkMode": "container:" + ids["app"]}, "Mounts": []}
        for name, volume, destination in (("hatchet", "original-config", "/config"),
                ("hatchet-db", "original-postgres", "/var/lib/postgresql/data")):
            values[ids[name]]["Mounts"].append({"Type": "volume", "Name": volume, "Destination": destination})
        def inspect(args, **kwargs):
            if args[0] == "inspect": return json.dumps([values[args[1]]]).encode()
            service = "dstack" if args[2].startswith("dstackai/") else "hatchet" if args[2].startswith("ghcr.io/") else "hatchet-db"
            return json.dumps([{"Id": values[ids[service]]["Image"]}]).encode()
        app = {"archive_image_ids": (app_image,)}; dispatch = {"archive_image_ids": (dispatch_image,)}
        with patch.object(maintenance, "compose", side_effect=lambda b, d, e, *a, **k: ids[a[-1]].encode()), \
                patch.object(release, "command", side_effect=inspect):
            maintenance.verify_services(self.root, self.root, environment, app, dispatch)
            values[ids["hatchet-db"]]["Mounts"][0]["Name"] = "unrelated-blank-postgres"
            with self.assertRaisesRegex(release.ReleaseError, "broker_volume_changed"):
                maintenance.verify_services(self.root, self.root, environment, app, dispatch)


if __name__ == "__main__":
    unittest.main()
