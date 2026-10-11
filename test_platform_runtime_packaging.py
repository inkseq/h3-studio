"""CPU image metadata/package boundary checks; no image build or network."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from studio_platform.runtime_catalog import PROFILE_IDS
from studio_platform.native_runtime_document import prepared_runtime_document
from tools.build_dstack_sources import build
from tools.release_contract import FIXED

ROOT = Path(__file__).resolve().parent


class RuntimeImagePackagingTests(unittest.TestCase):
    def test_prepared_runtime_document_preserves_existing_wire_bytes(self):
        # Frozen before extracting the constructor: one card and the second
        # slot of a two-card host, with a fixed source-bundle digest fixture.
        cases = ((PROFILE_IDS[0], 0, 1,
                  '403626196bf3e3356b6afac229dbcd9f08555eeda8286f48a186b3667caa8563'),
                 (PROFILE_IDS[2], 1, 2,
                  'e6dcb69197519bee73855bdfaa72bbb0bc411069c3c2c5d9cac7e09c35260ef8'))
        for profile, index, count, expected in cases:
            document = prepared_runtime_document(profile, index, count, 'a'*64)
            wire = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf-8')
            self.assertEqual(hashlib.sha256(wire).hexdigest(), expected)

    def test_runtime_factory_constructs_without_build_tools_in_image(self):
        # Actual constructor, not only module import: _source used to lazily
        # import a checkout-only build tool absent from both CPU runtime images.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            image = root/'image'
            image.mkdir()
            shutil.copytree(ROOT/'studio_platform', image/'studio_platform',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copytree(ROOT/'deploy/wangp/profiles', image/'deploy/wangp/profiles')
            shutil.copyfile(ROOT/'comfy_workflow.py', image/'comfy_workflow.py')
            # Source construction is an offline build operation outside the
            # staged image; the image receives only its protected four inputs.
            build(root/'sources', [(PROFILE_IDS[5], 'fl'), (PROFILE_IDS[5], 'ref')])
            for name, content in (('ssh-key', 'private test fixture'),
                                  ('broker.json', '{"token":"test fixture"}')):
                path = root/name
                path.write_text(content)
                path.chmod(0o600)
            config = {'version': 1, 'work_dir': str(root/'runtime'),
                'source_index_file': str(root/'sources/index.json'),
                'ssh_key_file': str(root/'ssh-key'), 'known_hosts_file': str(root/'known-hosts'),
                'broker_config_file': str(root/'broker.json'), 'first_local_port': 19100,
                'last_local_port': 19102, 'trust_first_host_key': False}
            config_path = root/'runtime-config.json'
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            script = """import json,sys
from pathlib import Path
image=Path(sys.argv[1])
assert not (image/'tools').exists()
assert not (image/'deploy/wangp/package_tool.py').exists()
sys.path.insert(0,str(image))
class NoBuildTools:
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='tools' or fullname.startswith('tools.') or fullname=='deploy.wangp.package_tool':
            raise ModuleNotFoundError('build tools are absent from the runtime image')
sys.meta_path.insert(0,NoBuildTools())
from studio_platform.dstack_factory import RuntimeFactory
factory=RuntimeFactory(None,None,sys.argv[2])
assert {mode for _,mode in factory.sources}=={'fl','ref'}
assert not factory.work_dir.exists()
print(json.dumps({'sources':len(factory.sources),'modes':sorted(mode for _,mode in factory.sources)}))
"""
            result = subprocess.run([sys.executable, '-I', '-c', script, str(image), str(config_path)],
                                    capture_output=True, text=True, check=True, timeout=30, cwd=image)
            self.assertEqual(json.loads(result.stdout), {'sources': 2, 'modes': ['fl', 'ref']})

    def test_profile_metadata_is_explicit_image_input_and_runtime_fingerprint(self):
        dockerfile = (ROOT/'Dockerfile.platform').read_text()
        patterns = (ROOT/'.dockerignore').read_text().splitlines()
        self.assertIn('COPY deploy/wangp/profiles/ ./deploy/wangp/profiles/', dockerfile)
        names = {'deploy/wangp/profiles/'+profile+'.json' for profile in PROFILE_IDS}
        for folder in ('deploy/', 'deploy/wangp/', 'deploy/wangp/profiles/'):
            self.assertIn('!'+folder, patterns)
        self.assertEqual({line[1:] for line in patterns if line.startswith('!deploy/wangp/profiles/') and line.endswith('.json')}, names)
        self.assertTrue(names <= set(FIXED))
        # No private execution/operator/provider configuration is baked in.
        self.assertFalse(any(line.startswith('!deploy/') and ('*' in line or 'execution-policy' in line or 'operator' in line)
                             for line in patterns))

    def test_catalog_and_manifests_load_from_isolated_image_shaped_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary)
            modules = ('studio_platform/__init__.py', 'studio_platform/runtime_catalog.py',
                       'studio_platform/inference/__init__.py', 'studio_platform/inference/wangp_contract.py',
                       'studio_platform/inference/protocol.py')
            for name in (*modules, *('deploy/wangp/profiles/'+profile+'.json' for profile in PROFILE_IDS)):
                target = image/name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT/name, target)
            script = """import json,sys
sys.path.insert(0,sys.argv[1])
from studio_platform.runtime_catalog import PROFILE_IDS,public_catalog,engine_manifest
catalog=public_catalog()
assert len(catalog['profiles'])==6
for profile in catalog['profiles']:
    assert profile['validation']['production_adapter_verified'] is False
    if not profile['verified_cases']:
        assert profile['validation']['scope']=='pending_hardware_qualification'
        assert profile.get('qualification_cases')
        assert all('measurements' not in case for case in profile['qualification_cases'])
    for case in profile['verified_cases']:
        for measurement in case['measurements']:
            if profile['id']==PROFILE_IDS[3]:
                assert measurement['evidence']=='https://github.com/apedintensor/h3-studio/issues/86#issuecomment-6083341543'
            else:
                assert measurement['evidence'].startswith('https://github.com/apedintensor/h3-studio/blob/')
    for mode in ('fl','ref'):
        assert engine_manifest(profile['id'],mode).document['production_adapter_verified'] is False
print(json.dumps({'profiles':len(catalog['profiles']),'manifests':len(PROFILE_IDS)*2}))
"""
            result = subprocess.run([sys.executable,'-I','-S','-c',script,str(image)],
                                    capture_output=True,text=True,check=True,timeout=20,cwd=image)
            self.assertEqual(json.loads(result.stdout), {'profiles':6, 'manifests':12})


if __name__ == '__main__':
    unittest.main()
