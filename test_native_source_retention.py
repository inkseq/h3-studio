"""Offline exact native-byte retention and fail-closed provenance checks."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy.wangp.package_tool import PRIVATE_FILES
from tools import build_dstack_sources, build_platform_release, native_source_retention as retention

ROOT = Path(__file__).resolve().parent
OLD, NEW, INDEX = "a" * 40, "b" * 40, "c" * 64
PROFILE = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3"
BASE = "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1"
SELECTIONS = [(PROFILE, "fl"), (PROFILE, "ref")]
INPUTS = dict(zip(retention.INPUTS, ("42", OLD, INDEX)))


class Provenance(unittest.TestCase):
    def setUp(self):
        self.run = dict(id=42, path=retention.WORKFLOW, event="workflow_dispatch", head_sha=OLD,
            head_branch="main", status="completed", conclusion="success",
            repository={"full_name": retention.REPOSITORY}, head_repository={"full_name": retention.REPOSITORY})
        self.artifacts = {"total_count": 1, "artifacts": [{"id": 84, "name": "sixnine-migration-" + OLD,
            "expired": False, "workflow_run": {"id": 42, "head_sha": OLD, "head_branch": "main"}}]}

    def test_explicit_complete_binding_only(self):
        self.assertIsNone(retention.binding({}))
        self.assertEqual(retention.binding(INPUTS), INPUTS)
        for key in retention.INPUTS:
            value = dict(INPUTS); value.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError): retention.binding(value)
        for value in ("0", "-1", "42/../other", 42, True):
            with self.subTest(run=value), self.assertRaises(ValueError):
                retention.binding(dict(INPUTS, retain_native_sources_run=value))

    def test_same_successful_main_run_and_unexpired_exact_artifact_required(self):
        self.assertEqual(retention.verify_run(self.run, self.artifacts, INPUTS), 84)
        for change in ({"id": 43}, {"path": ".github/workflows/other.yml"}, {"event": "pull_request"},
                {"head_sha": NEW}, {"head_branch": "topic"}, {"status": "in_progress"}, {"conclusion": "failure"},
                {"repository": {"full_name": "other/repo"}}, {"head_repository": {"full_name": "other/repo"}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                retention.verify_run(dict(self.run, **change), self.artifacts, INPUTS)
        for change in ({"expired": True}, {"name": "sixnine-migration-" + NEW}, {"id": 0},
                {"workflow_run": {"id": 43, "head_sha": OLD, "head_branch": "main"}},
                {"workflow_run": {"id": 42, "head_sha": NEW, "head_branch": "main"}}):
            inventory = copy.deepcopy(self.artifacts); inventory["artifacts"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                retention.verify_run(self.run, inventory, INPUTS)
        for count in (0, 2, 101):
            with self.subTest(count=count), self.assertRaises(ValueError):
                retention.verify_run(self.run, dict(self.artifacts, total_count=count), INPUTS)
        duplicate = {"total_count": 2, "artifacts": self.artifacts["artifacts"] * 2}
        with self.assertRaises(ValueError): retention.verify_run(self.run, duplicate, INPUTS)

    def test_prior_run_read_permission_is_scoped_to_container_job(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        global_permissions = workflow.split("\npermissions:\n", 1)[1].split("\njobs:\n", 1)[0]
        self.assertNotIn("actions:", global_permissions)
        containers = workflow.split("\n  containers:\n", 1)[1].split("\n  test:\n", 1)[0]
        self.assertIn("permissions:\n      contents: read\n      actions: read", containers)
        self.assertNotIn("actions: write", workflow)
        self.assertIn("run-id: ${{ inputs.retain_native_sources_run }}", containers)
        self.assertIn("github-token: ${{ github.token }}", containers)


class NativeBytes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.sources = self.temp / "sources"
        build_dstack_sources.build(self.sources, SELECTIONS)
        self.root = self.temp / "checkout"; self.root.mkdir()
        for name in (*PRIVATE_FILES, "deploy/wangp/bootstrap.py"):
            target = self.root / name; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.artifact = self.temp / "artifact"; self.artifact.mkdir()
        self.index_sha = hashlib.sha256((self.sources / "index.json").read_bytes()).hexdigest()
        self.repackage()

    def repackage(self, extra=None):
        path = self.artifact / "source-set.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for source in sorted(self.sources.rglob("*")):
                if source.is_file(): archive.add(source, arcname=source.relative_to(self.sources).as_posix(), recursive=False)
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b"x" * extra.size) if extra.isfile() else None)
        self.raw = path.read_bytes()
        (self.artifact / "migration-manifest.json").write_text(json.dumps({"version": 1, "commit": OLD,
            "source_index_sha256": self.index_sha, "files": {"source-set.tar.gz": hashlib.sha256(self.raw).hexdigest()}}))

    def retain(self, **changes):
        values = dict(directory=self.artifact, commit=OLD, index_sha256=self.index_sha,
            selections=SELECTIONS, root=self.root); values.update(changes)
        return retention.retain(**values)

    def test_all_original_native_bytes_and_index_retained_without_extraction(self):
        self.assertEqual(self.retain(), self.raw)
        with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("Must never extract code")):
            self.assertEqual(self.retain(), self.raw)
        self.assertEqual(sorted(path.name for path in self.artifact.iterdir()),
            ["migration-manifest.json", "source-set.tar.gz"])

    def test_changed_native_code_profile_bootstrap_and_runtime_rejected(self):
        for name in (PRIVATE_FILES[0], "deploy/wangp/profiles/" + PROFILE + ".json", "deploy/wangp/bootstrap.py"):
            source = self.root / name; original = source.read_bytes(); source.write_bytes(original + b"\n# changed")
            with self.subTest(source=name), self.assertRaises(ValueError): self.retain()
            source.write_bytes(original)
        from tools import build_operator_sources
        actual = build_operator_sources._runtime
        with patch.object(build_operator_sources, "_runtime", side_effect=lambda *args: dict(actual(*args), port=9999)), \
                self.assertRaisesRegex(ValueError, "configuration_changed"):
            self.retain()

    def test_selection_commit_checksum_and_index_bindings_fail_closed(self):
        for change in (dict(commit=NEW), dict(index_sha256=INDEX), dict(selections=SELECTIONS[:1]),
                dict(selections=list(reversed(SELECTIONS)))):
            with self.subTest(change=change), self.assertRaises(ValueError): self.retain(**change)
        path = self.artifact / "source-set.tar.gz"; path.write_bytes(self.raw + b"changed")
        with self.assertRaisesRegex(ValueError, "manifest_binding_changed"): self.retain()

    def test_duplicate_traversal_extra_members_links_and_native_tampering_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("private.env", tarfile.REGTYPE),
                ("index.json", tarfile.REGTYPE), ("link", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE)):
            extra = tarfile.TarInfo(name); extra.type = kind; extra.size = 1 if extra.isfile() else 0
            extra.linkname = "index.json"
            self.repackage(extra)
            with self.subTest(name=name), self.assertRaises(ValueError): self.retain()
        source = self.sources / PROFILE / "fl/gpu-0/wangp-bootstrap.py"
        source.write_bytes(b"changed bootstrap")
        self.repackage()
        with self.assertRaisesRegex(ValueError, "configuration_changed"): self.retain()

    def test_new_cpu_manifest_uses_exact_original_archive_and_prior_index(self):
        platform = self.temp / "platform"; platform.mkdir()
        (platform / "release-manifest.json").write_text(json.dumps({"commit": NEW}))
        inspected = [{"Id": "sha256:" + "d" * 64, "Architecture": "amd64", "Os": "linux", "Config": {
            "Labels": {"org.opencontainers.image.revision": NEW}}}]
        process = Mock(); process.__enter__ = Mock(return_value=process); process.__exit__ = Mock(return_value=False)
        process.stdout = io.BytesIO(b"SYNTHETIC_CPU_IMAGE"); process.wait.return_value = 0
        target = self.temp / "new-release"
        with patch.object(build_platform_release.subprocess, "check_output", return_value=json.dumps(inspected).encode()), \
                patch.object(build_platform_release.subprocess, "Popen", return_value=process):
            manifest = build_platform_release.build_migration(NEW, platform, target, SELECTIONS,
                retain_native_sources_directory=self.artifact, retain_native_sources_commit=OLD,
                retain_native_sources_index_sha256=self.index_sha)
        self.assertEqual((target / "source-set.tar.gz").read_bytes(), self.raw)
        self.assertEqual(manifest["commit"], NEW)
        self.assertEqual(manifest["dispatch"]["image"], "sixnine-dispatch:" + NEW)
        self.assertEqual(manifest["source_index_sha256"], self.index_sha)
        self.assertEqual(manifest["files"]["source-set.tar.gz"], hashlib.sha256(self.raw).hexdigest())

    def extended(self, **changes):
        values = dict(directory=self.artifact, commit=OLD, index_sha256=self.index_sha,
            selections=SELECTIONS + [(BASE, "fl")], root=self.root); values.update(changes)
        return retention.retain_or_extend(**values)

    def test_additive_base_selection_preserves_old_bytes_and_exact_single_gpu_identity(self):
        from studio_platform.runtime_catalog import engine_manifest, get_profile
        from studio_platform.native_runtime_document import prepared_runtime_document
        with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("No code extraction")), \
                patch('subprocess.run', side_effect=AssertionError("No process or cloud")):
            raw, digest = self.extended()
        def files(value):
            with tarfile.open(fileobj=io.BytesIO(value), mode="r:gz") as archive:
                return {member.name: archive.extractfile(member).read() for member in archive}
        before, after = files(self.raw), files(raw)
        old_index, new_index = json.loads(before['index.json']), json.loads(after['index.json'])
        self.assertEqual(new_index['sources'][:2], old_index['sources'])
        self.assertNotEqual(digest, self.index_sha)
        self.assertEqual(digest, hashlib.sha256(after['index.json']).hexdigest())
        for name, value in before.items():
            if name != 'index.json': self.assertEqual(after[name], value, name)
        entry = new_index['sources'][2]
        self.assertEqual((entry['runtime_profile_id'],entry['mode'],entry['gpu_count'],entry['profile_slot_index']),
            (BASE, 'fl', 1, 0))
        self.assertEqual(entry['engine_manifest_digest'], engine_manifest(BASE,'fl').digest)
        relative = entry['directory'] + '/'
        package = after[relative+'wangp-package.tar.gz']
        self.assertEqual(package, before[old_index['sources'][0]['directory']+'/wangp-package.tar.gz'])
        self.assertEqual(json.loads(after[relative+'wangp-runtime.json']),
            prepared_runtime_document(BASE,0,1,hashlib.sha256(package).hexdigest()))
        self.assertEqual(entry['source_sha256'], {name:hashlib.sha256(after[relative+name]).hexdigest()
            for name in ('wangp-bootstrap.py','wangp-manifest.json','wangp-runtime.json','wangp-package.tar.gz')})
        self.assertEqual(get_profile(BASE)['minimum_ram_bytes'],256*2**30)
        self.assertEqual(self.raw,(self.artifact/'source-set.tar.gz').read_bytes())

    def test_additive_retention_never_drops_reorders_duplicates_or_substitutes_native_sources(self):
        for choices in (SELECTIONS[:1]+[(BASE,'fl')],list(reversed(SELECTIONS))+[(BASE,'fl')],
                SELECTIONS+[(BASE,'fl'),(BASE,'fl')],SELECTIONS+[('unknown','fl')],
                SELECTIONS+[(BASE,'other')],SELECTIONS+[BASE]):
            with self.subTest(choices=choices),self.assertRaises(ValueError): self.extended(selections=choices)
        self.assertEqual(self.extended(selections=SELECTIONS),(self.raw,self.index_sha))
        source = self.root/PRIVATE_FILES[0]; source.write_bytes(source.read_bytes()+b'\nchanged')
        with self.assertRaisesRegex(ValueError,'package_source_changed'): self.extended()
        self.assertEqual(self.raw,(self.artifact/'source-set.tar.gz').read_bytes())

    def test_additive_retention_keeps_manifest_index_and_archive_tamper_fences(self):
        for changes in (dict(commit=NEW),dict(index_sha256=INDEX)):
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.extended(**changes)
        extra = tarfile.TarInfo('../escape'); extra.size = 1
        self.repackage(extra)
        with self.assertRaisesRegex(ValueError,'archive_member_invalid'): self.extended()
        self.repackage()
        (self.sources/PROFILE/'fl/gpu-0/wangp-manifest.json').write_text('{}')
        self.repackage()
        with self.assertRaisesRegex(ValueError,'native_configuration_changed'): self.extended()

    def test_new_cpu_artifact_binds_extended_index_not_the_old_index(self):
        platform = self.temp/'platform'; platform.mkdir()
        (platform/'release-manifest.json').write_text(json.dumps({'commit':NEW}))
        inspected = [{'Id':'sha256:'+'d'*64,'Architecture':'amd64','Os':'linux','Config':{
            'Labels':{'org.opencontainers.image.revision':NEW}}}]
        process = Mock(); process.__enter__=Mock(return_value=process); process.__exit__=Mock(return_value=False)
        process.stdout=io.BytesIO(b'SYNTHETIC_CPU_IMAGE'); process.wait.return_value=0
        target = self.temp/'extended-release'
        with patch.object(build_platform_release.subprocess,'check_output',return_value=json.dumps(inspected).encode()), \
                patch.object(build_platform_release.subprocess,'Popen',return_value=process):
            manifest=build_platform_release.build_migration(NEW,platform,target,SELECTIONS+[(BASE,'fl')],
                retain_native_sources_directory=self.artifact,retain_native_sources_commit=OLD,
                retain_native_sources_index_sha256=self.index_sha)
        with tarfile.open(target/'source-set.tar.gz','r:gz') as archive:
            raw=archive.extractfile('index.json').read()
        self.assertEqual(manifest['source_index_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(manifest['source_index_sha256'],self.index_sha)
        self.assertEqual(len(json.loads(raw)['sources']),3)


if __name__ == "__main__":
    unittest.main()
