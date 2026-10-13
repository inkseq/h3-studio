"""Offline continuation guards; no provider, SDK network, or native execution."""
from contextlib import contextmanager
import copy
import errno
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

from tools import resume_wangp_download as r

BOOTSTRAP = (Path(__file__).parent / "deploy/wangp/bootstrap.py").read_bytes().replace(b"\r\n", b"\n")


class Fixture:
    def __init__(self, root):
        self.root = root
        self.base = root / "install"
        self.remote = root / "remote"
        self.base.mkdir(); self.remote.mkdir()
        self.source = self.base / "platform"
        (self.source / "studio_platform").mkdir(parents=True)
        (self.source / "studio_platform/__init__.py").write_bytes(b"")
        self.package = self.remote / "wangp-package.tar.gz"
        with tarfile.open(self.package, "w:gz") as archive:
            member = tarfile.TarInfo("studio_platform/__init__.py")
            member.size = 0
            archive.addfile(member, io.BytesIO(b""))
        self.config = {"version": 1, "install_root": str(self.base),
            "source_bundle_path": str(self.package), "source_bundle_sha256": r.sha(self.package.read_bytes()),
            "dependency_artifact_url": "", "dependency_artifact_path": "", "dependency_artifact_sha256": "0" * 64,
            "manifest_path": str(self.remote / "wangp-manifest.json"), "model_root": str(root / "models"),
            "config_path": str(self.base / "wgp_config.json"), "status_path": str(self.remote / "setup-status.json"),
            "port": 9814, "prepared_root": str(root / "prepared"), "deployment_profile_id": "test-profile",
            "expected_host_gpus": 1, "profile_slot_index": 0}
        self.paths = r.paths_for(self.config, self.remote / "wangp-runtime.json", self.remote / "wangp-token")
        diagnosis = {"error_code": "model_download_failed", "stop_status": "confirmed"}
        raw = {"config": json.dumps(self.config).encode(), "token": b"x" * 64,
               "package": self.package.read_bytes(), "manifest": b'{"version":1}', "bootstrap": BOOTSTRAP,
               "marker": b'{"slot_key":"original-slot"}',
               "status": json.dumps({"slot_key": "original-slot", "state": "failed", "failure_phase": "model_download",
                   "manifest_digest": "f" * 64, "download_failure": diagnosis}).encode(),
               "failure": json.dumps({"state": "failed", "manifest_digest": "f" * 64,
                   "error_code": "model_download_failed", "download_failure": diagnosis}).encode()}
        raw["identity"] = json.dumps({"intent_id": "original-slot", "dstack_run_id": "original-run",
            "instance_id": "12345", "deployment_profile_id": "test-profile", "profile_slot_index": 0,
            "expected_host_gpus": 1, "engine_manifest_digest": "f" * 64,
            "sources": {"wangp-bootstrap.py": r.sha(raw["bootstrap"]), "wangp-runtime.json": r.sha(raw["config"]),
                        "wangp-package.tar.gz": r.sha(raw["package"]), "wangp-manifest.json": r.sha(raw["manifest"])}}).encode()
        (self.base / "model-download").mkdir()
        for name, path in self.paths.items():
            path.write_bytes(raw[name]); path.chmod(0o664 if name == "identity" else 0o600)
        self.raw = raw
        self.now = 1000.0
        self.authority = {"version": 1, "purpose": "resume-original-model-download-v1", "resume_id": "a" * 32,
            "slot_key": "original-slot", "run_id": "original-run", "instance_id": "12345",
            "issued_at": self.now, "expires_at": self.now + 300, "hard_deadline": self.now + 3600,
            "parent_metadata": {str(path): {"uid": 0, "gid": 0, "mode": path.stat().st_mode & 0o7777,
                "is_directory": True} for base in (self.base, self.remote) for path in (base, *base.parents)},
            "input_sha256": {name: r.sha(value) for name, value in raw.items()},
            "recovery_sha256": r.sha(Path(r.__file__).read_bytes())}
        self.authority_path = root / "authority.json"
        self.write_authority()

    def write_authority(self):
        self.authority_path.write_text(json.dumps(self.authority))
        self.authority_path.chmod(0o600)

    def bind_manifest(self):
        from studio_platform.runtime_catalog import engine_manifest
        manifest = engine_manifest("h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3", "ref")
        self.raw["manifest"] = manifest.document_json.encode()
        identity = json.loads(self.raw["identity"])
        identity["engine_manifest_digest"] = manifest.digest
        identity["sources"]["wangp-manifest.json"] = r.sha(self.raw["manifest"])
        self.raw["identity"] = json.dumps(identity).encode()
        for name in ("status", "failure"):
            value = json.loads(self.raw[name]); value["manifest_digest"] = manifest.digest
            self.raw[name] = json.dumps(value).encode()
        for name in ("manifest", "identity", "status", "failure"):
            self.paths[name].write_bytes(self.raw[name])
            self.authority["input_sha256"][name] = r.sha(self.raw[name])
        self.write_authority()
        return manifest

    def bind_completed(self):
        manifest=self.bind_manifest()
        root=Path(self.config['model_root']);root.mkdir()
        files=sorted([copy.deepcopy(item) for component in manifest.document['components'].values()
                      for item in component['files']],key=lambda item:item['path'])
        for item in files:
            target=root/item['path'];target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('wb') as stream:stream.truncate(item['size_bytes'])  # sparse metadata-only fixtures
        total=sum(item['size_bytes'] for item in files)
        value={'state':'downloaded_unverified','manifest_digest':manifest.digest,'files_complete':len(files),
               'files_total':len(files),'bytes_complete':total,'bytes_total':total,'inference_verified':False}
        progress=self.base/'model-download/progress.json';progress.write_text(json.dumps(value));progress.chmod(0o600)
        self.authority.update(version=3,purpose='resume-original-model-download-v3',completed_download={
            'progress_sha256':r.sha(progress.read_bytes()),'files':files,'keep_until':self.authority['hard_deadline'],
            **{name:value[name] for name in ('files_complete','files_total','bytes_complete','bytes_total')}})
        self.write_authority()
        return manifest,progress


def alias_for(manifest):
    return {"repository": "DeepBeepMeep/MiniMax-H3",
        "original_revision": "adc81ccb71352192214d83d5fafb9487e860be39",
        "download_revision": "8d9093438ffb3e33d6a058cc20fa3b7ca31f5752", "evidence_sha256": "e" * 64,
        "files": [copy.deepcopy(item) for component in manifest.document["components"].values()
                  for item in component["files"]]}


@unittest.skipUnless(sys.platform == "linux" and os.getuid() == 0, "protected Linux/root isolated fixtures")
class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/root")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))
        original_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))
        self.clock = patch.object(r.time, "time", return_value=self.fixture.now)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.idle = patch.object(r, "process_idle")
        self.idle.start(); self.addCleanup(self.idle.stop)
        python = patch.object(r.platform, "python_version", return_value="3.12.14")
        python.start(); self.addCleanup(python.stop)

    def invoke(self, effect):
        f = self.fixture
        return r.resume(f.paths["config"], f.paths["token"], f.authority_path, effect=effect)

    def test_valid_intent_is_durable_before_effect_and_preserves_originals(self):
        f = self.fixture
        called = []
        def effect(config, paths, authority, attempt):
            called.append(attempt)
            journal = json.loads(next(f.base.glob("resume-original-download-*.json")).read_bytes())
            self.assertEqual(journal["status"], "continuation_started")
            self.assertEqual((attempt.parent / "original-failure.json").read_text().strip(),
                             json.dumps(json.loads(f.raw["failure"]), sort_keys=True, separators=(",", ":")))
            paths["status"].write_text('{"state":"ready"}')
            return {"state": "ready", "phase": "runtime_ready"}
        self.assertEqual(self.invoke(effect)["state"], "ready")
        self.assertEqual(len(called), 1)
        for name in r.INPUTS - {"status"}:
            self.assertEqual(f.paths[name].read_bytes(), f.raw[name])
        self.assertEqual(self.invoke(lambda *args: self.fail("must not replay"))["state"], "reconcile_required")

    def test_interruption_and_changed_authority_never_replay(self):
        def interrupted(*args):
            raise OSError("synthetic-interruption")
        with self.assertRaises(OSError): self.invoke(interrupted)
        self.assertEqual(self.invoke(lambda *args: self.fail("must not replay"))["state"], "reconcile_required")
        self.fixture.authority["resume_id"] = "b" * 32
        self.fixture.write_authority()
        with self.assertRaisesRegex(ValueError, "intent_changed"):
            self.invoke(lambda *args: self.fail("must not replay"))

    def test_completed_original_reuses_once_and_keeps_all_originals(self):
        f=self.fixture;_,progress=f.bind_completed();before=progress.read_bytes()
        called=[]
        self.assertEqual(self.invoke(lambda *args:called.append(True) or {'state':'ready'})['state'],'ready')
        self.assertEqual(called,[True]);self.assertEqual(progress.read_bytes(),before)
        for name in r.INPUTS:self.assertEqual(f.paths[name].read_bytes(),f.raw[name])
        self.assertEqual(self.invoke(lambda *args:self.fail('must not replay'))['state'],'reconcile_required')

    def test_completed_progress_file_and_finite_policy_guards_precede_effects(self):
        for case in ('hash','count','bytes','files','keep_until','unfinished','foreign','size','symlink'):
            with self.subTest(case=case),tempfile.TemporaryDirectory(dir='/root') as directory:
                f=Fixture(Path(directory));_,progress=f.bind_completed()
                evidence=f.authority['completed_download']
                if case=='hash':evidence['progress_sha256']='0'*64
                elif case=='count':evidence['files_complete']-=1
                elif case=='bytes':evidence['bytes_complete']-=1
                elif case=='files':evidence['files'].pop()
                elif case=='keep_until':evidence['keep_until']+=1
                elif case in ('unfinished','foreign'):
                    value=json.loads(progress.read_bytes())
                    if case=='unfinished':value['state']='downloading'
                    else:value['manifest_digest']='0'*64
                    progress.write_text(json.dumps(value));evidence['progress_sha256']=r.sha(progress.read_bytes())
                else:
                    target=Path(f.config['model_root'])/evidence['files'][0]['path']
                    if case=='size':
                        with target.open('r+b') as stream:stream.truncate(1)
                    else:
                        target.unlink();target.symlink_to(progress)
                f.write_authority()
                with self.assertRaises(ValueError):
                    r.resume(f.paths['config'],f.paths['token'],f.authority_path,
                             effect=lambda *args:self.fail('unsafe effect'))
                self.assertFalse(list(f.base.glob('resume-original-download-*.json')))

    def test_completed_execution_rechecks_under_owned_cache_lock_without_sdk(self):
        f=self.fixture;manifest,progress=f.bind_completed();events=[]
        source=f.source;expected=r.approved_parents(f.authority)
        def tail(*args):
            download_once=args[-1];download_once(lambda phase,**fields:events.append((phase,fields)))
            return {'state':'ready'}
        def check(*args):
            events.append(('rechecked',{}))
            return original_check(*args)
        original_check=r.completed_download
        from studio_platform.runtime_hosts import wangp_profile_bootstrap,wangp_download
        with patch.object(wangp_profile_bootstrap,'prepare',return_value=(f.root/'prepared',sys.executable,{})),              patch.object(wangp_profile_bootstrap,'_acquire_cache_lock') as locked,              patch.object(wangp_download,'run_download',side_effect=AssertionError('No SDK or new child')),              patch.object(r,'completed_download',side_effect=check),patch.object(r,'original_tail',return_value=tail):
            self.assertEqual(r.execute(f.config,f.paths,f.authority,f.base/'new-attempt')['state'],'ready')
        locked.assert_called_once();self.assertEqual([event[0] for event in events],['rechecked','model_download'])
        self.assertFalse((f.base/'new-attempt').exists())
        self.assertEqual(f.paths['package'].read_bytes(),f.raw['package'])

    def failed_original_then_v2(self):
        f = self.fixture
        manifest = f.bind_manifest()
        result = {"slot_key": "original-slot", "state": "failed", "failure_phase": "model_download",
            "manifest_digest": manifest.digest,
            "code": "model_download_failed", "download_failure": {
                "error_code": "model_download_failed", "stop_status": "confirmed"}}
        def failed(config, paths, authority, attempt):
            paths["status"].write_text(json.dumps(result))
            return result
        self.invoke(failed)
        prior_path = next(f.base.glob("resume-original-download-*.json"))
        prior_raw = prior_path.read_bytes()
        f.authority.update(version=2, purpose="resume-original-model-download-v2", resume_id="b" * 32,
            previous_intent_sha256=r.sha(prior_raw), download_alias=alias_for(manifest))
        f.authority["input_sha256"]["status"] = r.sha(f.paths["status"].read_bytes())
        f.write_authority()
        return prior_path, prior_raw

    def test_v2_terminal_prior_allows_one_new_bound_intent_without_replaying(self):
        f = self.fixture
        prior_path, prior_raw = self.failed_original_then_v2()
        called = []
        def effect(config, paths, authority, attempt):
            called.append(attempt)
            self.assertEqual(prior_path.read_bytes(), prior_raw)
            journals = list(f.base.glob("resume-original-download-*.json"))
            self.assertEqual(len(journals), 2)
            current = json.loads(next(path for path in journals if path != prior_path).read_bytes())
            self.assertEqual(current["status"], "continuation_started")
            self.assertEqual(authority["download_alias"]["download_revision"], "8d9093438ffb3e33d6a058cc20fa3b7ca31f5752")
            return {"state": "ready"}
        self.assertEqual(self.invoke(effect)["state"], "ready")
        self.assertEqual(len(called), 1)
        self.assertEqual(prior_path.read_bytes(), prior_raw)
        self.assertEqual(self.invoke(lambda *args: self.fail("new intent must not replay"))["state"], "reconcile_required")

    def test_v2_prior_hash_or_alias_mismatch_refuses_before_new_effect(self):
        f = self.fixture
        prior_path, prior_raw = self.failed_original_then_v2()
        f.authority["previous_intent_sha256"] = "0" * 64; f.write_authority()
        with self.assertRaisesRegex(ValueError, "previous_intent_changed"):
            self.invoke(lambda *args: self.fail("effect"))
        f.authority["previous_intent_sha256"] = r.sha(prior_raw)
        f.authority["download_alias"]["files"][0]["size_bytes"] += 1; f.write_authority()
        with self.assertRaisesRegex(ValueError, "download_alias_content_changed"):
            self.invoke(lambda *args: self.fail("effect"))
        self.assertEqual(prior_path.read_bytes(), prior_raw)
        self.assertEqual(len(list(f.base.glob("resume-original-download-*.json"))), 1)

    def test_v2_alias_import_follows_original_source_verification(self):
        f = self.fixture
        self.failed_original_then_v2()
        validated = []
        original = r.validate_alias
        def alias(value, document):
            self.assertEqual(sys.path[0], str(f.source))
            validated.append(True)
            return original(value, document)
        with patch.object(r, "validate_alias", side_effect=alias) as check:
            (f.source / "studio_platform/extra.py").write_text("raise AssertionError('unverified')")
            with self.assertRaisesRegex(ValueError, "extracted_source_changed"):
                self.invoke(lambda *args: self.fail("unverified source must not execute"))
            check.assert_not_called()
            (f.source / "studio_platform/extra.py").unlink()
            self.assertEqual(self.invoke(lambda *args: {"state": "ready"})["state"], "ready")
        self.assertEqual(validated, [True])

    def test_stop_identity_phase_and_hash_guards_precede_any_effect(self):
        f = self.fixture
        changes = [("failure", "download_failure", {"error_code": "model_download_failed", "stop_status": "unconfirmed"}),
                   ("status", "failure_phase", "runtime_start"), ("identity", "dstack_run_id", "different-run"),
                   ("identity", "engine_manifest_digest", "0" * 64)]
        for name, key, value in changes:
            with self.subTest(name=name, key=key):
                modified = json.loads(f.raw[name]); modified[key] = value
                f.paths[name].write_text(json.dumps(modified))
                f.authority["input_sha256"][name] = r.sha(f.paths[name].read_bytes()); f.write_authority()
                with self.assertRaises(ValueError): self.invoke(lambda *args: self.fail("effect"))
                self.assertFalse(list(f.base.glob("resume-original-download-*.json")))
                f.paths[name].write_bytes(f.raw[name])
                f.authority["input_sha256"][name] = r.sha(f.raw[name]); f.write_authority()

    def test_time_and_existing_runtime_guards(self):
        f = self.fixture
        for age in (-1, 301):
            f.authority["issued_at"] = f.now - age; f.write_authority()
            with self.assertRaisesRegex(ValueError, "authority_expired"):
                self.invoke(lambda *args: self.fail("effect"))
        f.authority["issued_at"] = f.now; f.write_authority()
        Path(f.config["config_path"]).symlink_to(f.root / "missing")
        with self.assertRaisesRegex(ValueError, "linked_path_refused|runtime_already_started"):
            self.invoke(lambda *args: self.fail("effect"))

    def test_narrow_historical_identity_permissions_and_source_integrity(self):
        f = self.fixture
        self.assertEqual(r.read_input("identity", f.paths["identity"]), f.raw["identity"])
        with self.assertRaisesRegex(ValueError, "file_uncontrolled"): r.read(f.paths["identity"])
        f.paths["identity"].chmod(0o666)
        with self.assertRaisesRegex(ValueError, "file_uncontrolled"): r.read_input("identity", f.paths["identity"])
        (f.source / "studio_platform/extra.py").write_text("raise RuntimeError()")
        with self.assertRaisesRegex(ValueError, "extracted_source_changed"):
            r.source_matches(f.package, f.source)

    def test_only_exact_approved_root_group_directories_allow_legacy_permissions(self):
        f = self.fixture
        for path in (f.base, f.remote, f.source, f.source / "studio_platform"):
            path.chmod(0o775)
        for path in (f.base, f.remote):
            f.authority["parent_metadata"][str(path)]["mode"] = 0o775
        f.write_authority()
        result = self.invoke(lambda *args: {"state": "failed"})
        self.assertEqual(result["state"], "failed")
        with self.assertRaisesRegex(ValueError, "parent_uncontrolled"):
            r.read(f.paths["config"])
        f.remote.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "parent_metadata_changed"):
            r.read_input("config", f.paths["config"], f.authority["parent_metadata"])
        f.authority["parent_metadata"][str(f.remote)]["mode"] = 0o777
        with self.assertRaisesRegex(ValueError, "parent_authority_invalid"):
            r.approved_parents(f.authority)
        f.authority["parent_metadata"][str(f.remote)]["mode"] = 0o775
        f.authority["parent_metadata"][str(f.remote)]["gid"] = 10001
        with self.assertRaisesRegex(ValueError, "parent_authority_invalid"):
            r.approved_parents(f.authority)

    def test_exact_original_tail_download_failure_never_launches_runtime(self):
        f = self.fixture
        from studio_platform.runtime_hosts.wangp_download import DownloadFailure
        original = r.original_module(BOOTSTRAP)
        with patch.object(original.subprocess, "Popen", side_effect=AssertionError("must not launch")):
            tail = r.original_tail(original, BOOTSTRAP)
            result = tail(f.config, "original-slot", f.paths["token"], f.base, f.source, f.root / "prepared",
                          types.SimpleNamespace(), "f" * 64, sys.executable, {},
                          lambda status: (_ for _ in ()).throw(DownloadFailure("model_download_failed", "confirmed")))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["failure_phase"], "model_download")
        self.assertEqual(result["download_failure"], {"error_code": "model_download_failed", "stop_status": "confirmed"})

    def test_original_remaining_setup_launches_once_after_download_completion(self):
        f = self.fixture
        original = r.original_module(BOOTSTRAP)
        modules = {}
        def fake(name, **values):
            module = types.ModuleType(name); module.__dict__.update(values); modules[name] = module
        fake("studio_platform.runtime_catalog", runtime_config=lambda *args: {"profile": "unchanged"})
        fake("studio_platform.runtime_hosts.wangp_profile_bootstrap", selected_devices=lambda *args, **kwargs: args[1])
        fake("studio_platform.runtime_hosts.wangp_startup", read_startup_failure=lambda *args, **kwargs: None)
        fake("studio_platform.runtime_hosts.wangp_receipts", VERIFICATION_RECEIPT="verified.json",
             read_verification_receipt=lambda *args, **kwargs: {"incarnation": "original-runtime"})
        events = []
        def launch(command, **kwargs):
            events.append("launch")
            state = f.base / "slot-state"; state.mkdir(); (state / "verified.json").write_text("{}")
            return types.SimpleNamespace(pid=42, poll=lambda: None)
        @contextmanager
        def ready(*args, **kwargs):
            yield io.BytesIO(json.dumps({"manifest_digest": "f" * 64, "slot_key": "original-slot",
                "incarnation": "original-runtime", "idle": True}).encode())
        with patch.dict(sys.modules, modules), patch.object(original.subprocess, "run",
                return_value=types.SimpleNamespace(stdout="GPU-12345678-1234-1234-1234-123456789012, 32768\n")), \
                patch.object(original.subprocess, "Popen", side_effect=launch) as popen, \
                patch.object(original, "urlopen", side_effect=ready):
            tail = r.original_tail(original, BOOTSTRAP)
            result = tail(f.config, "original-slot", f.paths["token"], f.base, f.source, f.root / "prepared",
                types.SimpleNamespace(document={"source_revision": "a" * 40}), "f" * 64, sys.executable,
                {"CUDA_VISIBLE_DEVICES": "GPU-12345678-1234-1234-1234-123456789012"},
                lambda status: events.append("download"))
            self.assertEqual(result["state"], "ready", result)
            self.assertEqual(popen.call_count, 1)
        self.assertEqual(events, ["download", "launch"])
        self.assertEqual(result["state"], "ready")
        self.assertEqual(f.paths["marker"].read_bytes(), f.raw["marker"])
        self.assertEqual(json.loads(Path(f.config["config_path"]).read_bytes()), {"profile": "unchanged"})


class PureGuards(unittest.TestCase):
    def test_alias_preserves_manifest_and_changes_only_exact_sdk_revision(self):
        from studio_platform.runtime_catalog import engine_manifest
        manifest = engine_manifest("h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3", "ref")
        document = copy.deepcopy(manifest.document)
        alias = alias_for(manifest)
        r.validate_alias(alias, document)
        arguments = {"repo_id": alias["repository"], "filename": alias["files"][0]["path"],
            "revision": alias["original_revision"], "local_dir": "/original/models", "token": False,
            "endpoint": "https://huggingface.co", "force_download": False, "etag_timeout": 30}
        original = dict(arguments)
        self.assertEqual(r.alias_arguments(arguments, alias), {**original, "revision": alias["download_revision"]})
        self.assertEqual(arguments, original)
        self.assertEqual(document, manifest.document)
        for key, value in (("revision", "main"), ("repo_id", "other/repo"), ("filename", "other-file"),
                           ("token", "implicit-identity"), ("endpoint", "https://other.invalid")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "download_alias_binding_changed"):
                r.alias_arguments({**arguments, key: value}, alias)

    def test_alias_requires_complete_original_content_and_immutable_revision(self):
        from studio_platform.runtime_catalog import engine_manifest
        manifest = engine_manifest("h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3", "ref")
        original = alias_for(manifest)
        for mutation in ("missing", "size", "sha256", "git_blob", "revision", "repository"):
            alias = copy.deepcopy(original)
            if mutation == "missing": alias["files"].pop()
            elif mutation == "size": alias["files"][0]["size_bytes"] += 1
            elif mutation == "sha256": alias["files"][0]["sha256"] = "0" * 64
            elif mutation == "git_blob": next(item for item in alias["files"] if "git_blob_sha1" in item)["git_blob_sha1"] = "0" * 40
            elif mutation == "revision": alias["download_revision"] = "main"
            else: alias["repository"] = "other/repo"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                r.validate_alias(alias, manifest.document)

    def test_prior_requires_terminal_confirmed_download_and_original_binding(self):
        inputs = {name: "a" * 64 for name in r.INPUTS}
        original = {"status": "observed_result", "slot_key": "original", "hard_deadline": 5000.,
            "input_sha256": inputs, "result": {"state": "failed", "failure_phase": "model_download",
                "code": "model_download_failed", "download_failure": {
                    "error_code": "model_download_failed", "stop_status": "confirmed"}}}
        authority = {"slot_key": "original", "hard_deadline": 5000., "input_sha256": dict(inputs)}
        raw = json.dumps(original).encode(); authority["previous_intent_sha256"] = r.sha(raw)
        r.previous_failed_intent(raw, authority)
        for mutation in ("pending", "unknown", "stop", "slot", "deadline", "input"):
            prior = copy.deepcopy(original)
            if mutation == "pending": prior["status"] = "continuation_started"
            elif mutation == "unknown": prior["result"]["state"] = "unknown"
            elif mutation == "stop": prior["result"]["download_failure"]["stop_status"] = "unconfirmed"
            elif mutation == "slot": prior["slot_key"] = "other"
            elif mutation == "deadline": prior["hard_deadline"] += 1
            else: prior["input_sha256"]["manifest"] = "0" * 64
            raw = json.dumps(prior).encode(); authority["previous_intent_sha256"] = r.sha(raw)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "previous_intent_not_terminal"):
                r.previous_failed_intent(raw, authority)

    def test_safe_classification_never_contains_signed_url_or_exception_text(self):
        failure_type = type("HTTPError", (Exception,), {})
        failure = failure_type("https://secret.invalid/?token=SECRET")
        failure.response = types.SimpleNamespace(status_code=403)
        self.assertEqual(r.classification(failure), {"exception_type": "HTTPError", "http_status": 403})
        failure.response.status_code = True
        self.assertIsNone(r.classification(failure)["http_status"])
        self.assertNotIn("SECRET", json.dumps(r.classification(failure)))
        self.assertEqual(r.classification(type("SECRET", (Exception,), {})("secret"))["exception_type"], "OtherDownloadError")

    def test_process_scan_rejects_original_child_and_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, command in (("1", b"init\0"), ("2", b"python\0-m\0studio_platform.runtime_hosts.wangp_download\0")):
                child = proc / pid; child.mkdir()
                (child / "stat").write_bytes(pid.encode() + b" (name) " + b" ".join([b"S"] + [b"0"] * 18 + [b"123"]))
                (child / "cmdline").write_bytes(command)
            with self.assertRaisesRegex(ValueError, "live_process"): r.process_idle(9814, proc=proc, self_pid=99)
            (proc / "2/cmdline").write_bytes(b"sleep\0")
            with patch.object(r.socket, "socket") as factory:
                factory.return_value.__enter__.return_value.connect_ex.return_value = 0
                with self.assertRaisesRegex(ValueError, "runtime_port_listening"): r.process_idle(9814, proc=proc, self_pid=99)
                factory.return_value.__enter__.return_value.connect_ex.return_value = errno.ECONNREFUSED
                r.process_idle(9814, proc=proc, self_pid=99)


if __name__ == "__main__":
    unittest.main()
