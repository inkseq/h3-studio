"""CPU image metadata/package boundary checks; no image build or network."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from studio_platform.runtime_catalog import PROFILE_IDS
from tools.release_contract import FIXED

ROOT = Path(__file__).resolve().parent


class RuntimeImagePackagingTests(unittest.TestCase):
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
