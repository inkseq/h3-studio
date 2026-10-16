"""Additive host release boundaries; synthetic artifacts/fake Docker only."""
from contextlib import ExitStack, contextmanager
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


@contextmanager
def synthetic_host_parent(root):
    """Emulate only the fixture parent's UID; preserve real path/mode checks."""
    lstat = Path.lstat
    def fixture_lstat(path, *args, **kwargs):
        info = lstat(path, *args, **kwargs)
        if path == root:
            fields = list(info); fields[4] = 0
            return os.stat_result(fields)
        return info
    with patch.object(Path, "lstat", fixture_lstat):
        yield


class MigrationArtifacts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host_parent = synthetic_host_parent(self.root)
        self.host_parent.__enter__(); self.addCleanup(self.host_parent.__exit__, None, None, None)
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
        inspected = [{"Id": self.image_id, "Architecture": "amd64", "Os": "linux", "Config": {
            "Labels": {"org.opencontainers.image.revision": COMMIT}}}]
        provenance = json.loads((ROOT / "deploy/dstack/native-vast-provenance.json").read_text())
        checked = {field: hashlib.sha256((ROOT / "deploy/dstack" / name).read_bytes()).hexdigest()
            for field, name in (("helper_sha256", "native_vast_safety.py"), ("installer_sha256", "patch_native_vast.py"),
                                ("source_provenance_sha256", "native-vast-provenance.json"))}
        checked["installed"] = {"patched_sources": 6, "dstack_version": "0.22.3",
            "upstream_commit": provenance["dstack_upstream_commit"], "helper_sha256": checked["helper_sha256"],
            "original_sources": {name: item["sha256"] for name,item in provenance["sources"].items()},
            "sources": {name:"e"*64 for name in provenance["sources"]}}
        def saved(command, **kwargs):
            image = io.BytesIO()
            with tarfile.open(fileobj=image, mode="w") as archive:
                for name, data in ((self.image_id[7:] + ".json", raw), ("manifest.json", json.dumps([{
                        "Config": self.image_id[7:] + ".json", "RepoTags": [command[-1]], "Layers": []}]).encode())):
                    member = tarfile.TarInfo(name); member.size = len(data); archive.addfile(member, io.BytesIO(data))
            process = Mock(); process.__enter__ = Mock(return_value=process); process.__exit__ = Mock(return_value=False)
            process.stdout = io.BytesIO(image.getvalue()); process.wait.return_value = 0
            return process
        self.artifact = self.root / "artifact"
        with patch.object(builder.subprocess, "check_output", side_effect=lambda command, **kwargs:
                json.dumps(checked if command[1] == "run" else inspected).encode()), \
                patch.object(builder.subprocess, "Popen", side_effect=saved):
            self.value = builder.build_migration(COMMIT, self.platform, self.artifact, [(PROFILE, "fl")])

    def test_exact_dispatch_and_public_source_set_round_trip(self):
        value = maintenance.manifest(self.artifact, COMMIT,
            platform_manifest_sha256=release.checksum(self.platform / "release-manifest.json"))
        self.assertEqual(value, self.value)
        self.assertEqual(release.validate_image_archive(self.artifact / "dispatch-image.tar.gz",
            {"commit": COMMIT, **value["dispatch"]}), (self.image_id,))
        self.assertEqual(release.validate_image_archive(self.artifact / "dstack-server-image.tar.gz",
            {"commit": COMMIT, **value["dstack"]}), (self.image_id,))
        self.assertEqual(builder.DSTACK_IMAGE_CHECK, maintenance.DSTACK_IMAGE_CHECK)
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

    def test_native_source_commit_map_is_optional_bounded_and_covers_exact_packages(self):
        value = maintenance.manifest(self.artifact, COMMIT)
        target = self.root/'mapped-sources'
        maintenance.extract_sources(self.artifact/'source-set.tar.gz',target,value['source_index_sha256'],
            source_commits=value['native_source_commits'])
        maintenance.verify_sources(target,value['source_index_sha256'],source_commits=value['native_source_commits'])
        self.assertEqual(set(value['native_source_commits'].values()),{COMMIT})
        for origins in ({}, {'f'*64:COMMIT}, dict(value['native_source_commits'], **{'f'*64:COMMIT}),
                {next(iter(value['native_source_commits'])):'not-a-commit'}):
            with self.subTest(origins=origins),self.assertRaisesRegex(release.ReleaseError,'source_commits_changed'):
                maintenance.verify_sources(target,value['source_index_sha256'],source_commits=origins)
        path = self.artifact/maintenance.MANIFEST
        original = path.read_bytes()
        for origins in ({}, [], {'f'*64:True}, {'f'*64:'not-a-commit'}, {'bad':COMMIT}):
            path.write_text(json.dumps(dict(value,native_source_commits=origins)))
            with self.subTest(origins=origins),self.assertRaisesRegex(release.ReleaseError,'source_commits_invalid'):
                maintenance.manifest(self.artifact,COMMIT)
        path.write_bytes(original)
        legacy = dict(value);legacy.pop('native_source_commits')
        path.write_text(json.dumps(legacy))
        self.assertEqual(maintenance.manifest(self.artifact,COMMIT),legacy)

    def test_derivative_archive_and_installed_proof_are_bound_before_load(self):
        directory = self.root / "derivative"; directory.mkdir()
        self.artifact.rename(directory / "artifact"); self.artifact = directory / "artifact"
        with patch.object(release, "command") as command:
            server = maintenance.dstack_image(directory, self.value, {}, load=True)
        self.assertEqual(server["archive_image_ids"], (self.image_id,))
        self.assertEqual(server["installed_provenance"], maintenance.dstack_provenance(self.artifact, self.value))
        self.assertEqual(command.call_args.args[0], ["load", "--input", str(self.artifact / "dstack-server-image.tar.gz")])
        # A different image's archive cannot be relabelled as this derivative.
        (self.artifact / "dstack-server-image.tar.gz").write_bytes((self.artifact / "dispatch-image.tar.gz").read_bytes())
        with patch.object(release, "command") as command, self.assertRaisesRegex(release.ReleaseError, "identity_mismatch"):
            maintenance.dstack_image(directory, self.value, {}, load=True)
        command.assert_not_called()

    def test_legacy_artifact_has_no_derivative_fallback_or_extra_file_acceptance(self):
        legacy = dict(self.value)
        legacy.pop("dstack")
        legacy.pop("layout")
        legacy["version"] = 1
        legacy["files"] = {name:digest for name,digest in self.value["files"].items() if name not in maintenance.DSTACK_FILES}
        for name in ("hatchet-entrypoint.sh", "broker.example.json"):
            (self.artifact/name).write_text("historical fixture")
            legacy["files"][name] = release.checksum(self.artifact/name)
        for name in maintenance.DSTACK_FILES:
            (self.artifact/name).unlink()
        (self.artifact/maintenance.MANIFEST).write_text(json.dumps(legacy))
        self.assertEqual(maintenance.manifest(self.artifact,COMMIT),legacy)
        with patch.object(release,"command") as command:
            self.assertIsNone(maintenance.dstack_image(self.root,legacy,{},load=True))
        command.assert_not_called()
        (self.artifact/"dstack-native-provenance.json").write_text("{}")
        with self.assertRaisesRegex(release.ReleaseError,"artifact_files_invalid"):
            maintenance.manifest(self.artifact,COMMIT)

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
        path = self.artifact / "runtime-config.example.json"
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
        with patch.object(release, "regular"), patch.object(release, "checksum", side_effect=["a" * 64, "a" * 64, "b" * 64, "b" * 64]), \
                patch.object(maintenance.importlib.util, "spec_from_file_location") as importer, \
                self.assertRaisesRegex(release.ReleaseError, "helpers_need_reviewed_update"):
            maintenance.installed_checker(self.artifact)
        importer.assert_not_called()

    def test_only_two_reviewed_legacy_pairs_can_use_installed_checker(self):
        trusted_path = Path(maintenance.__file__).resolve().with_name("migration_check.py")
        original = self.artifact / maintenance.MANIFEST
        for commit, pair in maintenance.LEGACY_HELPER_PAIRS.items():
            original.write_text(json.dumps({"commit": commit}))
            spec, module = Mock(), types.ModuleType("trusted_checker")
            with patch.object(release, "regular"), \
                    patch.object(release, "checksum", side_effect=["a" * 64, "b" * 64, *pair]), \
                    patch.object(maintenance.importlib.util, "spec_from_file_location", return_value=spec) as importer, \
                    patch.object(maintenance.importlib.util, "module_from_spec", return_value=module):
                self.assertIs(maintenance.installed_checker(self.artifact), module)
                importer.assert_called_once_with("sixnine_trusted_migration_checker", trusted_path)
                spec.loader.exec_module.assert_called_once_with(module)
        legacy_commit = next(iter(maintenance.LEGACY_HELPER_PAIRS))
        pair = maintenance.LEGACY_HELPER_PAIRS[legacy_commit]
        for value, incoming in (({"commit": COMMIT}, pair),
                ({"commit": legacy_commit, "dstack": {}}, pair),
                ({"commit": legacy_commit}, (pair[0], "f" * 64))):
            original.write_text(json.dumps(value))
            with patch.object(release, "regular"), \
                    patch.object(release, "checksum", side_effect=["a" * 64, "b" * 64, *incoming]), \
                    patch.object(maintenance.importlib.util, "spec_from_file_location") as importer, \
                    self.assertRaisesRegex(release.ReleaseError, "helpers_need_reviewed_update"):
                maintenance.installed_checker(self.artifact)
            importer.assert_not_called()

    def test_retained_current_previous_derivatives_use_only_exact_installed_checker_pairs(self):
        expected_commits = {'46130d852459f12d81d09c7e23178a8544d1d4d4', '02681cacaf98822b5072a028fa9b313976d1c0ca', 'c848dde8bf1d49044743ae1f497a148e50b84409', '6e6fe5ee14e82983161f7f2bf15cbfd6e8eb54f2',
                            'b5902a1cf9bf2b32fe2de7ea35ca1cc7bbde3524'}
        self.assertEqual(set(maintenance.RETAINED_DSTACK_HELPER_PAIRS), expected_commits)
        self.assertEqual(maintenance.RETAINED_DSTACK_HELPER_PAIRS['b5902a1cf9bf2b32fe2de7ea35ca1cc7bbde3524'],
                         ('a4d0c782ab523b30c7f4db0b958b785480e17bb5ac0578edb93c47055d80c080',
                          '0d8db5801395b3ecbf3ceea9109c187fb9e39f1de08473694c9ed45df0a23534'))
        trusted_path = Path(maintenance.__file__).resolve().with_name('migration_check.py')
        for commit, pair in maintenance.RETAINED_DSTACK_HELPER_PAIRS.items():
            (self.artifact/maintenance.MANIFEST).write_text(json.dumps({'commit': commit, 'dstack': {
                'image': 'sixnine-dstack:'+commit, 'image_id': 'sha256:'+'c'*64}}))
            spec, module = Mock(), types.ModuleType('trusted_checker')
            with patch.object(release, 'regular'), \
                    patch.object(release, 'checksum', side_effect=['a'*64, 'b'*64, *pair]), \
                    patch.object(maintenance.importlib.util, 'spec_from_file_location', return_value=spec) as importer, \
                    patch.object(maintenance.importlib.util, 'module_from_spec', return_value=module):
                self.assertIs(maintenance.installed_checker(self.artifact), module)
                importer.assert_called_once_with('sixnine_trusted_migration_checker', trusted_path)
            for incoming in ((pair[0], 'f'*64), ('f'*64, pair[1])):
                with patch.object(release, 'regular'), \
                        patch.object(release, 'checksum', side_effect=['a'*64, 'b'*64, *incoming]), \
                        patch.object(maintenance.importlib.util, 'spec_from_file_location') as importer, \
                        self.assertRaisesRegex(release.ReleaseError, 'helpers_need_reviewed_update'):
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
        self.assertEqual(len(api.objects), len(maintenance.artifact_files(self.value)) + 1)
        self.assertFalse((self.root / "approved-migrations").exists())
        key = "releases/" + COMMIT + "/migration/runtime-config.example.json"
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
        self.assertNotIn("hatchet-entrypoint", (prepared / "overlay.yaml").read_text())
        self.assertEqual(maintenance.prepare(self.root, COMMIT, self.platform), (prepared, value))


class MigrationGuardAndDeployment(unittest.TestCase):
    def setUp(self):
        layout = patch.object(maintenance, "directory_manifest", return_value={"version": 1})
        layout.start(); self.addCleanup(layout.stop)
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


class StoryboardMaintenance(unittest.TestCase):
    """Protected flag updates with fake CPU services; no network/provider clients."""
    def setUp(self):
        layout = patch.object(maintenance, "directory_manifest", return_value={"version": 1})
        layout.start(); self.addCleanup(layout.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host_parent = synthetic_host_parent(self.root)
        self.host_parent.__enter__(); self.addCleanup(self.host_parent.__exit__, None, None, None)
        directory = self.root / "migration"; directory.mkdir()
        environment = {key: str(self.root / "protected" / key.lower()) for key in maintenance.PATH_FIELDS}
        environment.update(SIXNINE_OPERATOR_CAPACITY_OWNERS="superdan,supervan",
            SIXNINE_HATCHET_POSTGRES_VOLUME="original-postgres", SIXNINE_HATCHET_CONFIG_VOLUME="original-config",
            SIXNINE_MIGRATION_GENERATION_ENABLED="1", SIXNINE_MIGRATION_EXECUTION_BACKEND="wangp-worker",
            SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID=PROFILE)
        self.config = {"version": 1, "host_memory_mib": 3840, "environment": environment}
        self.paths = directory / "paths.json"; self.paths.write_text(json.dumps(self.config))
        self.active = directory / "active.json"
        self.active.write_text(json.dumps({"version": 1, "active": True, "current": COMMIT, "pending": None,
            "paths_sha256": release.checksum(self.paths), "legacy_cpu_handoff_sha256": "3" * 64}))
        self.release_state = self.root / "release-state.json"
        self.release_state.write_text(json.dumps({"current": COMMIT, "previous": "b" * 40,
            "pending": None, "status": "app_ready", "dependencies": {"db": "original"}}))
        self.before = {name: path.read_bytes() for name, path in (
            ("paths", self.paths), ("active", self.active), ("release", self.release_state))}
        self.runtime = {name: {"image_id": "sha256:" + "1" * 64,
            "environment_sha256": "2" * 64, "mounts_sha256": "3" * 64} for name in maintenance.STORYBOARD_SERVICES}
        regular = release.regular
        def fixture_regular(path, *, root_owned=False, maximum=None):
            if root_owned: self.assertTrue(Path(path).is_relative_to(self.root))
            return regular(path, maximum=maximum)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(release, "regular", side_effect=fixture_regular))
        self.stack.enter_context(patch.object(maintenance, "protected_directory",
            side_effect=lambda path: self.assertTrue(Path(path).is_relative_to(self.root) and Path(path).is_dir())))
        # Windows CPU runners have no fchmod; production retains Linux ownership checks.
        if not hasattr(os, "fchmod"):
            self.stack.enter_context(patch.object(os, "fchmod", create=True))
        self.stack.enter_context(patch.object(maintenance, "guard_legacy"))
        self.checker = Mock()
        def context(root, commit, config):
            env = {**config["environment"], "SIXNINE_IMAGE": "original-app", "SIXNINE_DISPATCH_IMAGE": "original-dispatch"}
            return self.root / "releases" / COMMIT, self.root / "migration/releases" / COMMIT, env, {}, {}, self.checker
        self.original_context = maintenance._storyboard_context
        self.context = self.stack.enter_context(patch.object(maintenance, "_storyboard_context", side_effect=context))
        self.stack.enter_context(patch.object(maintenance, "verify_services"))
        self.original_runtime = maintenance._storyboard_runtime
        self.runtime_mock = self.stack.enter_context(patch.object(maintenance, "_storyboard_runtime", return_value=self.runtime))
        self.stack.enter_context(patch.object(release, "wait_ready"))
        self.stack.enter_context(patch.object(release, "wait_proxy_stable"))
        self.stack.enter_context(patch.object(publisher.boto3, "client", side_effect=AssertionError("No AWS client in flag maintenance")))
        self.calls = []
        self.stack.enter_context(patch.object(maintenance, "compose",
            side_effect=lambda b, d, e, *a, **k: self.calls.append((a, dict(e))) or b""))

    def update(self, owners="superdan,supervan"):
        return maintenance.storyboard_owners_locked(self.root, COMMIT, owners,
            release.checksum(self.paths), release.checksum(self.active))

    def receipt(self):
        return maintenance.storyboard_receipt(self.root)

    def restore(self):
        return maintenance.restore_storyboard_owners_locked(self.root, COMMIT,
            release.checksum(self.root / "migration" / maintenance.STORYBOARD_RECEIPT))

    def test_flag_update_preserves_original_bytes_pins_and_reconnects_only_cpu_namespace(self):
        result = self.update()
        self.assertEqual(result["state"], "approved_storyboard_owners_updated")
        self.assertEqual(maintenance.settings(self.root)[0]["environment"][maintenance.STORYBOARD_FLAG], "superdan,supervan")
        self.assertEqual(json.loads(self.active.read_text())["paths_sha256"], release.checksum(self.paths))
        self.assertEqual(self.release_state.read_bytes(), self.before["release"])
        self.assertEqual(self.calls[0][0], ("stop", *maintenance.SHARED))
        self.assertEqual(self.calls[1][0], ("up", "-d", "--no-deps", "--force-recreate", "app", *maintenance.SHARED))
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(env["SIXNINE_HATCHET_POSTGRES_VOLUME"] == "original-postgres"
            and env["SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"] == PROFILE for _, env in self.calls))
        receipt = self.receipt(); self.assertEqual(receipt["status"], "completed")
        saved, old, new = maintenance._storyboard_backup(self.root, receipt)
        self.assertEqual(saved["before", "paths"], self.before["paths"])
        self.assertEqual(saved["before", "active"], self.before["active"])
        new["environment"].pop(maintenance.STORYBOARD_FLAG)
        self.assertEqual(new, old)
        maintenance.storyboard_maintenance_fence(self.root)
        self.calls.clear()
        self.assertEqual(self.update()["state"], "approved_storyboard_owners_unchanged")
        self.assertFalse(self.calls)
        self.update("")
        self.assertEqual(maintenance.settings(self.root)[0]["environment"][maintenance.STORYBOARD_FLAG], "")
        self.assertEqual(self.release_state.read_bytes(), self.before["release"])

    def test_invalid_accounts_and_stale_original_hash_reject_before_runtime_or_intent(self):
        for owners in ("other", "superdan,superdan", "superdan,", "superdan, supervan", True):
            with self.subTest(owners=owners), self.assertRaisesRegex(release.ReleaseError, "arguments_invalid"):
                self.update(owners)
        with self.assertRaisesRegex(release.ReleaseError, "original_binding_changed"):
            maintenance.storyboard_owners_locked(self.root, COMMIT, "superdan", "f" * 64, release.checksum(self.active))
        self.context.assert_not_called(); self.assertFalse(self.calls)
        self.assertFalse((self.root / "migration" / maintenance.STORYBOARD_RECEIPT).exists())

    def test_current_release_and_independent_approval_are_required_before_cpu_mutation(self):
        actual_context = self.context.side_effect
        self.context.side_effect = self.original_context
        changed = json.loads(self.release_state.read_text()); changed["pending"] = "b" * 40
        self.release_state.write_text(json.dumps(changed))
        with self.assertRaisesRegex(release.ReleaseError, "current_release_required"):
            self.update()
        self.release_state.write_bytes(self.before["release"])
        for path in (self.root / "releases" / COMMIT, self.root / "migration/releases" / COMMIT / "artifact"):
            path.mkdir(parents=True)
        with patch.object(release, "approved_manifest", side_effect=release.ReleaseError("independent_release_approval_missing")):
            with self.assertRaisesRegex(release.ReleaseError, "independent_release_approval_missing"):
                self.update()
        self.assertFalse(self.calls)
        self.context.side_effect = actual_context

    def test_runtime_failure_keeps_old_config_and_blocks_blind_deploy_or_replay(self):
        with patch.object(maintenance, "_storyboard_refresh", side_effect=release.ReleaseError("synthetic_runtime_failure")):
            with self.assertRaisesRegex(release.ReleaseError, "synthetic_runtime_failure"):
                self.update()
        self.assertEqual(self.receipt()["status"], "failed")
        self.assertEqual(self.paths.read_bytes(), self.before["paths"])
        self.assertEqual(self.active.read_bytes(), self.before["active"])
        with self.assertRaisesRegex(release.ReleaseError, "pending_restore_required"):
            maintenance.apply_locked(self.root, COMMIT)
        with self.assertRaisesRegex(release.ReleaseError, "pending_restore_required"):
            self.update()
        self.assertEqual(self.restore()["state"], "approved_storyboard_owners_restored")
        self.assertEqual(self.receipt()["status"], "restored")
        maintenance.storyboard_maintenance_fence(self.root)

    def test_interrupted_two_file_commit_restores_exact_original_bytes(self):
        writer = maintenance._storyboard_bytes
        def interrupt(path, data):
            if path == self.active: raise OSError("synthetic interrupted marker write")
            writer(path, data)
        with patch.object(maintenance, "_storyboard_bytes", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "interrupted marker"):
                self.update()
        self.assertNotEqual(self.paths.read_bytes(), self.before["paths"])
        self.assertEqual(self.active.read_bytes(), self.before["active"])
        self.restore()
        for name, path in (("paths", self.paths), ("active", self.active), ("release", self.release_state)):
            self.assertEqual(path.read_bytes(), self.before[name])
        self.assertEqual(self.receipt()["status"], "restored")

    def test_restore_requires_exact_receipt_and_never_overwrites_unrelated_configuration(self):
        with patch.object(maintenance, "_storyboard_refresh", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError): self.update()
        with self.assertRaisesRegex(release.ReleaseError, "restore_binding_changed"):
            maintenance.restore_storyboard_owners_locked(self.root, COMMIT, "0" * 64)
        changed = json.loads(self.paths.read_text()); changed["host_memory_mib"] += 1
        self.paths.write_text(json.dumps(changed)); unrelated = self.paths.read_bytes()
        with self.assertRaisesRegex(release.ReleaseError, "restore_unrelated_change"):
            self.restore()
        self.assertEqual(self.paths.read_bytes(), unrelated)
        self.assertFalse(self.calls)

    def test_changed_worker_settings_or_mounts_fail_verification_and_preserve_binding(self):
        changed = {name: dict(item) for name, item in self.runtime.items()}
        changed["controller"]["environment_sha256"] = "4" * 64
        self.runtime_mock.side_effect = [self.runtime, changed]
        with patch.object(maintenance.time, "monotonic", side_effect=[0, 181]):
            with self.assertRaisesRegex(release.ReleaseError, "runtime_or_mounts_changed"):
                self.update()
        self.assertEqual(self.paths.read_bytes(), self.before["paths"])
        self.assertEqual(self.active.read_bytes(), self.before["active"])
        self.assertEqual(self.receipt()["status"], "failed")

    def test_runtime_observation_redacts_environment_and_rejects_flag_on_workers(self):
        values = {name: {"Image": "sha256:" + "1" * 64, "State": {"Running": True}, "Mounts": [],
            "Config": {"Labels": {"com.docker.compose.project": "sixnine-platform", "com.docker.compose.service": name},
                "Env": ["PRIVATE_VALUE=fixture-should-not-be-in-receipt"] +
                    ([maintenance.STORYBOARD_FLAG + "=superdan"] if name == "app" else [])}}
            for name in maintenance.STORYBOARD_SERVICES}
        self.runtime_mock.side_effect = self.original_runtime
        identifiers = {name: str(index) * 64 for index, name in enumerate(maintenance.STORYBOARD_SERVICES, 1)}
        by_id = {identifiers[name]: value for name, value in values.items()}
        with patch.object(maintenance, "compose", side_effect=lambda b, d, e, *a, **k: identifiers[a[-1]].encode()), \
                patch.object(release, "command", side_effect=lambda args, **k: json.dumps([by_id[args[1]]]).encode()):
            result = maintenance._storyboard_runtime(self.root, self.root, {maintenance.STORYBOARD_FLAG: "superdan"})
            self.assertNotIn("PRIVATE_VALUE", json.dumps(result)); self.assertNotIn("fixture-should", json.dumps(result))
            values["controller"]["Config"]["Env"].append(maintenance.STORYBOARD_FLAG + "=superdan")
            with self.assertRaisesRegex(release.ReleaseError, "storyboard_worker_environment"):
                maintenance._storyboard_runtime(self.root, self.root, {maintenance.STORYBOARD_FLAG: "superdan"})

    def test_public_maintenance_wrapper_holds_existing_nonblocking_release_lock(self):
        locked = Mock()
        fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=4, flock=locked)
        def action(*args):
            locked.assert_called_once(); self.assertEqual(locked.call_args.args[1], 5)
            self.assertFalse(locked.call_args.args[0].closed)
            return {"state": "synthetic-lock-held"}
        with patch.dict(sys.modules, {"fcntl": fcntl}), patch.object(release, "check_host") as host, \
                patch.object(maintenance, "storyboard_owners_locked", side_effect=action):
            result = maintenance.storyboard_maintenance(self.root, COMMIT, owners="superdan",
                paths_sha256="a" * 64, active_sha256="b" * 64)
        host.assert_called_once_with(self.root)
        self.assertEqual(result["state"], "synthetic-lock-held")
        self.assertTrue(locked.call_args.args[0].closed)


if __name__ == "__main__":
    unittest.main()


class NotifyUpgradeBoundaries(unittest.TestCase):
    def test_only_exact_verified_cpu_containers_removed_without_volumes(self):
        calls = []
        ids = {name: digit * 64 for name, digit in (("dispatch", "1"), ("hatchet", "2"), ("hatchet-db", "3"))}
        def command(args, **kwargs):
            calls.append(args)
            if args[0] == "ps":
                return ids[args[-1].split("=")[-1]].encode()
            if args[0] == "inspect":
                service = next(key for key, identity in ids.items() if identity == args[1])
                return json.dumps([{"Config": {"Labels": {"com.docker.compose.project": "sixnine-platform",
                    "com.docker.compose.service": service}}, "HostConfig": {}}]).encode()
            return b""
        with patch.object(release, "command", side_effect=command):
            maintenance.stop_superseded_cpu({}, remove=True)
        self.assertEqual(calls[-2], ["stop", "--time", "210", *ids.values()])
        self.assertEqual(calls[-1], ["rm", *ids.values()])
        self.assertFalse(any("-v" in call or "down" in call for call in calls))

    def test_foreign_or_gpu_identity_fails_before_any_stop(self):
        for config in ({"Devices": [{"PathOnHost": "/dev/nvidia0"}]}, {"Privileged": True}):
            calls = []
            def command(args, **kwargs):
                calls.append(args)
                if args[0] == "ps": return b"aaaaaaaaaaaa"
                return json.dumps([{"Config": {"Labels": {"com.docker.compose.project": "sixnine-platform",
                    "com.docker.compose.service": "dispatch"}}, "HostConfig": config}]).encode()
            with patch.object(release, "command", side_effect=command), self.assertRaisesRegex(
                    release.ReleaseError, "retired_cpu_identity_invalid"):
                maintenance.stop_superseded_cpu({}, remove=True)
            self.assertFalse(any(call[0] in ("stop", "rm") for call in calls))

    def test_new_manifest_layout_is_explicit_and_legacy_file_set_retained(self):
        old = maintenance.artifact_files({"version": 1})
        new = maintenance.artifact_files({"version": 2, "layout": "postgres-notify-v1", "dstack": {}})
        self.assertIn("hatchet-entrypoint.sh", old)
        self.assertNotIn("hatchet-entrypoint.sh", new)
        self.assertNotIn("broker.example.json", new)
        self.assertTrue(maintenance.DSTACK_FILES <= new)
        self.assertFalse(maintenance.notify_layout({"version": 2, "layout": "unapproved"}))
