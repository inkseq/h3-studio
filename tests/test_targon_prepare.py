"""Native VM preparation validation only; no installs, downloads, or subprocesses."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from studio_platform.runtime_catalog import engine_manifest, get_profile

SOURCE = Path(__file__).resolve().parents[1]/"deploy/wangp/targon_prepare.py"


def load_preparer():
    spec = importlib.util.spec_from_file_location("targon_prepare_fixture",SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TargonPrepareTests(unittest.TestCase):
    def setUp(self):
        self.module = load_preparer()
        self.temp = tempfile.TemporaryDirectory(prefix="h3-targon-prepare-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tag = "f1111111-1111-4111-8111-111111111111"
        self.profile = get_profile(self.module.PROFILE_ID)
        self.config = {"deployment_profile_id":self.module.PROFILE_ID,"profile_slot_index":0,"expected_host_gpus":1,
            "prepared_root":str(self.module.SOURCE),"install_root":"/root/sixnine-cache/operator/profile-slot-0",
            "model_root":"/root/sixnine-cache/models","status_path":str(self.root/"setup-status.json"),
            "manifest_path":str(self.root/"wangp-manifest.json"),"source_bundle_path":str(self.root/"wangp-package.tar.gz"),
            "dependency_artifact_url":"","dependency_artifact_path":""}
        manifest = engine_manifest(self.module.PROFILE_ID,"ref")
        (self.root/"wangp-runtime.json").write_text(json.dumps(self.config))
        (self.root/"wangp-manifest.json").write_text(manifest.document_json)
        (self.root/"wangp-bootstrap.py").write_text("# fake bootstrap; never executed")
        (self.root/"wangp-token").write_text("fixture-token-never-read")
        raw = json.dumps(self.profile).encode()
        with tarfile.open(self.root/"wangp-package.tar.gz","w:gz") as archive:
            item = tarfile.TarInfo("deploy/wangp/profiles/"+self.module.PROFILE_ID+".json")
            item.size = len(raw)
            archive.addfile(item,io.BytesIO(raw))
        self.identity = {"intent_id":self.tag,"provider":"targon","instance_id":"1-workload",
            "deployment_profile_id":self.module.PROFILE_ID,"runtime_python":str(self.module.VENV/"bin/python"),
            "hard_deadline":time.time()+3600,"engine_manifest_digest":manifest.digest,
            "sources":{name:hashlib.sha256((self.root/name).read_bytes()).hexdigest() for name in
                ("wangp-runtime.json","wangp-manifest.json","wangp-bootstrap.py","wangp-package.tar.gz")}}
        self.write_identity()
        self.module.BOOT_ROOT = self.root

    def write_identity(self):
        (self.root/"sixnine-bootstrap-identity.json").write_text(json.dumps(self.identity))

    def context(self):
        return self.module.bootstrap_context(self.root/"wangp-runtime.json",self.tag,self.root/"wangp-token")

    def test_import_and_plan_are_inert(self):
        with patch("subprocess.Popen",side_effect=AssertionError("process")), \
                patch("urllib.request.urlopen",side_effect=AssertionError("network")):
            module = load_preparer()
            value = module.plan()
            self.assertEqual(value["python"],"3.12.14")
            self.assertFalse(value["models_downloaded"])
            self.assertFalse(value["generation_started"])

    def test_profile_and_requirement_drift_are_rejected(self):
        self.module.validate_profile(self.profile)
        self.profile["runtime"]["core_versions"]["torch"] = "other"
        with self.assertRaisesRegex(ValueError,"profile_mismatch"):
            self.module.validate_profile(self.profile)
        with self.assertRaisesRegex(ValueError,"upstream_requirements_mismatch"):
            self.module.transformed_requirements(b"torch==different\n")

    def test_boot_context_preserves_original_manifest_and_deadline(self):
        root, digest, preparer = self.context()
        self.assertEqual(root,self.root)
        self.assertEqual(digest,self.identity["engine_manifest_digest"])
        self.assertEqual(preparer.deadline,self.identity["hard_deadline"])
        self.assertEqual(preparer.instance_id,"1-workload")
        self.assertNotIn("TARGON_API_KEY",preparer.env)

    def test_boot_context_rejects_altered_source_and_other_token_path(self):
        (self.root/"wangp-bootstrap.py").write_text("changed")
        with self.assertRaisesRegex(ValueError,"source_identity_mismatch"):
            self.context()
        with self.assertRaisesRegex(ValueError,"boot_paths_invalid"):
            self.module.bootstrap_context(self.root/"wangp-runtime.json",self.tag,self.root/"another-token")

    def test_missing_or_expired_original_deadline_is_rejected(self):
        for value in (None,time.time()-1):
            self.identity["hard_deadline"] = value
            self.write_identity()
            with self.assertRaisesRegex(ValueError,"authority_invalid"):
                self.context()

    def test_success_execs_existing_bootstrap_with_same_identity_and_token_path(self):
        with patch.object(self.module.Preparer,"apply",return_value={"status":"prepared"}) as apply, \
                patch.object(self.module.os,"execv") as execute:
            self.module.prepare_then_bootstrap(self.root/"wangp-runtime.json",self.tag,self.root/"wangp-token")
        apply.assert_called_once()
        python = str(self.module.VENV/"bin/python")
        execute.assert_called_once_with(python,[python,"-u",str(self.root/"wangp-bootstrap.py"),"--config",
            str(self.root/"wangp-runtime.json"),"--slot-key",self.tag,"--token-file",str(self.root/"wangp-token")])
        value = json.loads((self.root/"setup-status.json").read_text())
        self.assertEqual(value["state"],"booting")
        self.assertEqual(value["manifest_digest"],self.identity["engine_manifest_digest"])

    def test_failed_preparation_records_static_failure_and_never_executes(self):
        with patch.object(self.module.Preparer,"apply",side_effect=RuntimeError("private fixture detail")), \
                patch.object(self.module.os,"execv") as execute:
            with self.assertRaisesRegex(ValueError,"^targon_prepare_failed$"):
                self.module.prepare_then_bootstrap(self.root/"wangp-runtime.json",self.tag,self.root/"wangp-token")
        execute.assert_not_called()
        raw = (self.root/"setup-status.json").read_text()
        value = json.loads(raw)
        self.assertEqual(value["state"],"failed")
        self.assertEqual(value["failure_phase"],"runtime_imports")
        self.assertNotIn("private fixture detail",raw)
        self.assertNotIn("fixture-token",raw)

    def test_download_wall_timeout_clamps_to_original_deadline(self):
        preparer = self.module.Preparer('1-owned', time.time()+3600)
        now = preparer.deadline-37
        alarm = SimpleNamespace(SIGALRM=14, ITIMER_REAL=0, handler='original', armed=[])
        def install_handler(_, handler):
            previous, alarm.handler = alarm.handler, handler
            return previous
        alarm.signal = install_handler
        alarm.getitimer = lambda _: (0, 0)
        alarm.setitimer = lambda _, seconds: alarm.armed.append(seconds)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): alarm.handler(14, None)
        with patch.object(self.module, 'STATE', self.root), \
                patch.object(self.module, 'signal', alarm), \
                patch.object(self.module.time, 'time', return_value=now), \
                patch.object(self.module.urllib.request, 'urlopen', return_value=Response()):
            with self.assertRaisesRegex(TimeoutError, '^targon_prepare_download_timeout$'):
                preparer.installer()
        self.assertEqual(alarm.armed, [7, 0])
        self.assertEqual(alarm.handler, 'original')
        with patch.object(self.module, 'STATE', self.root), \
                patch.object(self.module.time, 'time', return_value=preparer.deadline-29), \
                patch.object(self.module.urllib.request, 'urlopen') as download:
            with self.assertRaisesRegex(ValueError, 'deadline_reached'):
                preparer.installer()
        download.assert_not_called()

    def test_package_process_timeout_uses_remaining_original_deadline(self):
        preparer = self.module.Preparer('1-owned', time.time()+3600)
        calls = []
        process = SimpleNamespace(returncode=0, communicate=lambda **kwargs: (calls.append(kwargs) or b'OK', b''))
        with patch.object(self.module.time, 'time', return_value=preparer.deadline-50), \
                patch.object(self.module.subprocess, 'Popen', return_value=process):
            self.assertEqual(preparer.run(['fake-installer'], timeout=2400), 'OK')
        self.assertEqual(calls, [{'timeout':20}])


if __name__ == "__main__":
    unittest.main()
