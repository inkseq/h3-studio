"""Offline exact native-byte retention and fail-closed provenance checks."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
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
V4 = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4"
SELECTIONS = [(PROFILE, "fl"), (PROFILE, "ref")]
INPUTS = dict(zip(retention.INPUTS, ("42", OLD, INDEX)))


def synthetic_dstack_bundle(commit, destination):
    # These retention cases inspect only immutable GPU bytes; the dedicated
    # migration tests validate real synthetic Docker tar/provenance boundaries.
    (Path(destination)/"dstack-server-image.tar.gz").write_bytes(b"SYNTHETIC_DSTACK_IMAGE")
    (Path(destination)/"dstack-native-provenance.json").write_text("{}")
    return {"image":"sixnine-dstack:"+commit,"image_id":"sha256:"+"d"*64}


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
        self.assertIn("run-id: ${{ inputs.retain_native_sources_run || vars.SIXNINE_NATIVE_SOURCE_RUN }}", containers)
        self.assertIn("name: sixnine-migration-${{ inputs.retain_native_sources_commit || vars.SIXNINE_NATIVE_SOURCE_COMMIT }}", containers)
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
        with patch.object(build_platform_release,"bundle_dstack",side_effect=synthetic_dstack_bundle), \
                patch.object(build_platform_release.subprocess, "check_output", return_value=json.dumps(inspected).encode()), \
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
        with patch.object(build_platform_release,'bundle_dstack',side_effect=synthetic_dstack_bundle), \
                patch.object(build_platform_release.subprocess,'check_output',return_value=json.dumps(inspected).encode()), \
                patch.object(build_platform_release.subprocess,'Popen',return_value=process):
            manifest=build_platform_release.build_migration(NEW,platform,target,SELECTIONS+[(BASE,'fl')],
                retain_native_sources_directory=self.artifact,retain_native_sources_commit=OLD,
                retain_native_sources_index_sha256=self.index_sha)
        with tarfile.open(target/'source-set.tar.gz','r:gz') as archive:
            raw=archive.extractfile('index.json').read()
        self.assertEqual(manifest['source_index_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(manifest['source_index_sha256'],self.index_sha)
        self.assertEqual(len(json.loads(raw)['sources']),3)

    def git_fixture(self):
        # A real local object database exercises pinned blob reads without
        # network, checking out history or importing its package tool.
        self.sources = self.temp / 'git-sources'
        self.original = SELECTIONS + [(BASE, 'fl')]
        from deploy.wangp import package_tool
        new_member = 'deploy/wangp/profiles/'+V4+'.json'
        with patch.object(package_tool,'PRIVATE_FILES',tuple(name for name in PRIVATE_FILES if name!=new_member)):
            build_dstack_sources.build(self.sources, self.original)
        self.index_sha = hashlib.sha256((self.sources/'index.json').read_bytes()).hexdigest()
        target = self.root/'deploy/wangp/package_tool.py'
        current_tool = (ROOT/'deploy/wangp/package_tool.py').read_bytes()
        target.write_bytes(current_tool.replace(('    "'+new_member+'",').encode(),b''))
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.DEVNULL).decode().strip()
        self.git = git
        git('init','-q')
        git('config','core.autocrlf','false')
        git('config','user.email','offline@example.test'); git('config','user.name','Offline fixture')
        git('add','.'); git('commit','-qm','Original exact native sources')
        self.old_commit = git('rev-parse','HEAD')
        self.repackage()
        manifest = json.loads((self.artifact/'migration-manifest.json').read_text())
        manifest['commit'] = self.old_commit
        (self.artifact/'migration-manifest.json').write_text(json.dumps(manifest))
        source = self.root/'studio_platform/runtime_hosts/wangp_session.py'
        source.write_bytes(source.read_bytes()+b'\n# New immutable profile source fixture\n')
        target.write_bytes(current_tool)
        git('add','.'); git('commit','-qm','Fresh native source')
        self.new_commit = git('rev-parse','HEAD')

    def git_extended(self, **changes):
        from deploy.wangp import package_tool
        values = dict(directory=self.artifact,commit=self.old_commit,index_sha256=self.index_sha,
            selections=self.original+[(V4,'fl'),(V4,'ref')],root=self.root,current_commit=self.new_commit)
        values.update(changes)
        with patch.object(package_tool,'ROOT',self.root), \
                patch.object(tarfile.TarFile,'extractall',side_effect=AssertionError('No history extraction')):
            return retention.retain_from_git_or_extend(**values)

    def test_git_retention_preserves_v3_base_and_appends_fresh_v4_with_durable_origins(self):
        self.git_fixture()
        raw,index,origins = self.git_extended()
        def files(value):
            with tarfile.open(fileobj=io.BytesIO(value),mode='r:gz') as archive:
                return {member.name:archive.extractfile(member).read() for member in archive}
        before,after = files(self.raw),files(raw)
        old_index,new_index = json.loads(before['index.json']),json.loads(after['index.json'])
        self.assertEqual(new_index['sources'][:3],old_index['sources'])
        for name,value in before.items():
            if name!='index.json': self.assertEqual(after[name],value,name)
        self.assertEqual(index,hashlib.sha256(after['index.json']).hexdigest())
        old_digest = old_index['sources'][0]['source_sha256']['wangp-package.tar.gz']
        new_digest = new_index['sources'][3]['source_sha256']['wangp-package.tar.gz']
        self.assertNotEqual(old_digest,new_digest)
        self.assertEqual(origins,{old_digest:self.old_commit,new_digest:self.new_commit})
        self.assertEqual(new_index['sources'][4]['source_sha256']['wangp-package.tar.gz'],new_digest)
        # The next CPU preparation can retain the mixed set without rewriting
        # either native generation, even though its manifest commit is newer.
        later = self.temp/'mixed'; later.mkdir()
        (later/'source-set.tar.gz').write_bytes(raw)
        (later/'migration-manifest.json').write_text(json.dumps({'version':1,'commit':self.new_commit,
            'source_index_sha256':index,'files':{'source-set.tar.gz':hashlib.sha256(raw).hexdigest()},
            'native_source_commits':origins}))
        again = self.git_extended(directory=later,commit=self.new_commit,index_sha256=index)
        self.assertEqual(again,(raw,index,origins))

    def test_git_retention_requires_exact_blob_origin_and_complete_valid_map(self):
        self.git_fixture()
        with self.assertRaisesRegex(ValueError,'source_commit_bytes_changed|archive_member_invalid'):
            self.git_extended(current_commit=self.old_commit)
        with self.assertRaisesRegex(ValueError,'source_commit_unavailable'):
            self.git_extended(current_commit='d'*40)
        path = self.artifact/'migration-manifest.json'
        original = json.loads(path.read_text())
        package = json.loads((self.sources/'index.json').read_text())['sources'][0]['source_sha256']['wangp-package.tar.gz']
        for origins in ({}, {package:self.new_commit}, {package:self.old_commit,'f'*64:self.old_commit},
                {package:'not-a-commit'}, []):
            path.write_text(json.dumps(dict(original,native_source_commits=origins)))
            with self.subTest(origins=origins),self.assertRaises(ValueError): self.git_extended()
        path.write_text(json.dumps(original))
        # AST literal reading cannot execute a historical package-tool import.
        tool = self.root/'deploy/wangp/package_tool.py'
        tool.write_text("raise AssertionError('Historical source must never execute')\nPRIVATE_FILES = ('comfy_workflow.py',)\n")
        self.git('add','.');self.git('commit','-qm','Untrusted historical member declaration')
        changed = self.git('rev-parse','HEAD')
        path.write_text(json.dumps(dict(original,native_source_commits={package:changed})))
        with self.assertRaisesRegex(ValueError,'archive_member_invalid'):
            self.git_extended()

    def test_git_retention_refuses_links_nonliteral_members_and_selection_replacement(self):
        self.git_fixture()
        for selections in (list(reversed(self.original))+[(V4,'fl')], self.original[:2]+[(V4,'fl')]):
            with self.subTest(selections=selections),self.assertRaisesRegex(ValueError,'additive_selection_required'):
                self.git_extended(selections=selections)
        tool = self.root/'deploy/wangp/package_tool.py'
        original = tool.read_bytes()
        for text in ("PRIVATE_FILES = tuple(['comfy_workflow.py'])\n", "PRIVATE_FILES = ('../escape',)\n",
                "PRIVATE_FILES = ('comfy_workflow.py','comfy_workflow.py')\n"):
            tool.write_text(text);self.git('add','.');self.git('commit','-qm','Invalid historical member declaration')
            with self.subTest(text=text),self.assertRaisesRegex(ValueError,'source_members_invalid'):
                retention._git_sources(self.root,self.git('rev-parse','HEAD'))
        tool.write_bytes(original);self.git('add','.');self.git('commit','-qm','Restore tool')
        # A Git symlink is rejected by tree mode before any file read.
        result = subprocess.run(['git','-C',str(self.root),'hash-object','-w','--stdin'],
            input=b'outside-target',stdout=subprocess.PIPE,check=True)
        self.git('update-index','--add','--cacheinfo','120000,'+result.stdout.decode().strip()+',comfy_workflow.py')
        self.git('commit','-qm','Symlink source')
        with self.assertRaisesRegex(ValueError,'source_file_invalid'):
            retention._git_sources(self.root,self.git('rev-parse','HEAD'))

    def test_git_source_mode_manifest_and_workflow_are_explicit(self):
        self.git_fixture()
        platform=self.temp/'git-platform';platform.mkdir()
        (platform/'release-manifest.json').write_text(json.dumps({'commit':self.new_commit}))
        inspected=[{'Id':'sha256:'+'d'*64,'Architecture':'amd64','Os':'linux','Config':{
            'Labels':{'org.opencontainers.image.revision':self.new_commit}}}]
        process=Mock();process.__enter__=Mock(return_value=process);process.__exit__=Mock(return_value=False)
        process.stdout=io.BytesIO(b'SYNTHETIC_CPU_IMAGE');process.wait.return_value=0
        original_popen = subprocess.Popen
        def popen(command, **kwargs):
            return process if command[0] == 'docker' else original_popen(command, **kwargs)
        from deploy.wangp import package_tool
        with patch.object(package_tool,'ROOT',self.root),patch.object(build_platform_release,'ROOT',self.root), \
                patch.object(build_platform_release,'bundle_dstack',side_effect=synthetic_dstack_bundle), \
                patch.object(build_platform_release.subprocess,'check_output',return_value=json.dumps(inspected).encode()), \
                patch.object(build_platform_release.subprocess,'Popen',side_effect=popen), \
                patch.object(build_platform_release.shutil,'copyfile'):
            # Config templates are separate public artifact members.
            # subprocess.check_output is patched for Docker only; the Git blob
            # reader deliberately uses run(), so pinned object proof stays real.
            destination=self.temp/'git-release'
            def copy(source,target): Path(target).write_bytes(b'public config template')
            with patch.object(build_platform_release.shutil,'copyfile',side_effect=copy):
                manifest=build_platform_release.build_migration(self.new_commit,platform,destination,
                    self.original+[(V4,'fl'),(V4,'ref')],retain_native_sources_directory=self.artifact,
                    retain_native_sources_commit=self.old_commit,retain_native_sources_index_sha256=self.index_sha,
                    retain_native_sources_from_git=True)
        self.assertEqual(set(manifest['native_source_commits'].values()),{self.old_commit,self.new_commit})
        workflow=(ROOT/'.github/workflows/ci.yml').read_text().split('\n  containers:\n',1)[1].split('\n  test:\n',1)[0]
        self.assertIn('fetch-depth: 0',workflow)
        self.assertIn('--retain-native-sources-from-git',workflow)
        for mode in ('fl','ref'): self.assertIn(V4+':'+mode,workflow)
        self.assertIn('h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2:fl',workflow)
        self.assertNotIn('h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2:ref',workflow)


if __name__ == "__main__":
    unittest.main()
