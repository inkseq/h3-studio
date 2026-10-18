"""Offline prepared-profile/source-bundle checks; no GPU, cloud, or network."""

import ast
import copy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from studio_platform.runtime_catalog import PROFILE_IDS, engine_manifest, get_profile
from studio_platform.runtime_hosts import wangp_profile_bootstrap as subject

ROOT = Path(__file__).resolve().parents[1]
UUIDS = [
    'GPU-11111111-1111-1111-1111-111111111111',
    'GPU-22222222-2222-2222-2222-222222222222',
]


class ProfileBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = get_profile(PROFILE_IDS[2])
        self.requirements = b'synthetic exact install metadata\n'
        (self.root / 'requirements.txt').write_bytes(self.requirements)
        self.profile['runtime']['requirements_sha256'] = hashlib.sha256(
            self.requirements
        ).hexdigest()
        self.config = {
            'deployment_profile_id': PROFILE_IDS[2],
            'prepared_root': str(self.root),
            'profile_slot_index': 1,
            'expected_host_gpus': 2,
            'manifest_path': str(self.root / 'manifest.json'),
        }
        self.changed = False
        self.extra = b''
        self.commands = []

    def run_fake(self, args, **kwargs):
        self.commands.append(args)
        if 'rev-parse' in args:
            return NS(returncode=0, stdout=self.profile['source_revision'])
        if 'diff' in args:
            self.assertIn(':(exclude)requirements.txt', args)
            return NS(returncode=1 if self.changed else 0)
        if 'ls-files' in args:
            return NS(returncode=0, stdout=self.extra)
        if args[0] == 'nvidia-smi':
            return NS(
                returncode=0, stdout='\n'.join(uuid + ', 97252' for uuid in UUIDS)
            )
        raise AssertionError('Unexpected subprocess')

    def prepare(self):
        with (
            patch(
                'studio_platform.runtime_catalog.validate_manifest',
                return_value=self.profile,
            ),
            patch.object(subject.subprocess, 'run', side_effect=self.run_fake),
            patch.object(
                subject.importlib.metadata,
                'version',
                side_effect=self.profile['runtime']['core_versions'].__getitem__,
            ),
        ):
            return subject.prepare(
                self.config,
                engine_manifest(PROFILE_IDS[2], 'fl'),
                self.root,
                lambda phase: None,
            )

    def test_exact_requirements_exception_and_uuid_environment_binding(self):
        with patch.dict(
            os.environ,
            {
                'LIUM_API_KEY': 'synthetic-not-exported',
                'AWS_SECRET_ACCESS_KEY': 'synthetic-not-exported',
            },
        ):
            root, python, environment = self.prepare()
        self.assertEqual(root, self.root.resolve())
        self.assertEqual(python, sys.executable)
        self.assertEqual(environment['CUDA_VISIBLE_DEVICES'], UUIDS[1])
        self.assertNotIn('LIUM_API_KEY', environment)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', environment)
        devices = [{'uuid': uuid, 'total_bytes': 96 * 1024**3} for uuid in UUIDS]
        # A later NVML listing may have a different ordering; keep the bound GPU.
        self.assertEqual(
            subject.selected_devices(
                self.config, list(reversed(devices)), expected_uuid=UUIDS[1]
            ),
            [devices[1]],
        )

    def test_changed_requirements_executable_source_and_untracked_code_fail(self):
        (self.root / 'requirements.txt').write_bytes(b'other install metadata')
        with self.assertRaisesRegex(ValueError, 'requirements_mismatch'):
            self.prepare()
        (self.root / 'requirements.txt').write_bytes(self.requirements)
        self.changed = True
        with self.assertRaisesRegex(ValueError, 'source_modified'):
            self.prepare()
        self.changed = False
        self.extra = b'new_plugin.py\0'
        with self.assertRaisesRegex(ValueError, 'untracked_code'):
            self.prepare()

    def test_missing_or_duplicate_gpu_identity_is_not_reassigned(self):
        devices = [{'uuid': uuid, 'total_bytes': 96 * 1024**3} for uuid in UUIDS]
        for value in ([], devices[:1], [devices[0], devices[0]]):
            with self.assertRaisesRegex(ValueError, 'gpu_observation_invalid'):
                subject.selected_devices(self.config, value)
        with self.assertRaisesRegex(ValueError, 'gpu_observation_invalid'):
            subject.selected_devices(
                self.config,
                devices,
                expected_uuid='GPU-33333333-3333-3333-3333-333333333333',
            )

    def test_shared_cache_lock_has_finite_wait_without_download_replay(self):
        elapsed, phases = [0.0], []

        def busy(stream):
            raise BlockingIOError()

        def sleep(seconds):
            elapsed[0] += seconds

        with self.assertRaisesRegex(ValueError, 'cache_lock_timeout'):
            subject._acquire_cache_lock(
                io.BytesIO(),
                phases.append,
                timeout=1,
                clock=lambda: elapsed[0],
                sleeper=sleep,
                locker=busy,
            )
        self.assertEqual(elapsed[0], 1)
        self.assertEqual(phases, ['model_download_waiting_for_shared_cache'])
        with (
            patch.object(
                subject,
                '_acquire_cache_lock',
                side_effect=ValueError('wangp_profile_cache_lock_timeout'),
            ),
            patch(
                'studio_platform.runtime_hosts.wangp_download.run_download'
            ) as download,
            self.assertRaisesRegex(ValueError, 'cache_lock_timeout'),
        ):
            subject.download_profile(
                self.config,
                sys.executable,
                self.root,
                'a' * 64,
                self.root / 'models',
                self.root / 'state',
                {},
                lambda phase: None,
            )
        download.assert_not_called()

    def test_private_bundle_contains_its_internal_import_dependencies(self):
        spec = importlib.util.spec_from_file_location(
            'profile_package_closure_test', ROOT / 'deploy/wangp/package_tool.py'
        )
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        members = set(package.PRIVATE_FILES)
        for name in sorted(members):
            if not name.endswith('.py'):
                continue
            module = name[:-3].replace('/', '.')
            parent = (
                module.removesuffix('.__init__')
                if name.endswith('/__init__.py')
                else module.rpartition('.')[0]
            )
            for node in ast.walk(ast.parse((ROOT / name).read_text(encoding='utf-8'))):
                targets = []
                if isinstance(node, ast.ImportFrom):
                    base = (
                        importlib.util.resolve_name(
                            '.' * node.level + (node.module or ''), parent
                        )
                        if node.level
                        else node.module
                    )
                    if base:
                        targets = [base] + [
                            base + '.' + item.name for item in node.names
                        ]
                elif isinstance(node, ast.Import):
                    targets = [item.name for item in node.names]
                for target in targets:
                    paths = [
                        target.replace('.', '/') + suffix
                        for suffix in ('.py', '/__init__.py')
                    ]
                    existing = [path for path in paths if (ROOT / path).is_file()]
                    with self.subTest(source=name, dependency=target):
                        self.assertFalse(
                            existing and members.isdisjoint(existing),
                            'Private runtime dependency missing: ' + target,
                        )

    def test_extracted_private_bundle_compiles_all_profiles_without_workspace_imports(
        self,
    ):
        spec = importlib.util.spec_from_file_location(
            'profile_package_test', ROOT / 'deploy/wangp/package_tool.py'
        )
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        archive = self.root / 'source.tar.gz'
        package.small_bundle(archive)
        extracted = self.root / 'extracted'
        extracted.mkdir()
        with tarfile.open(archive) as source:
            self.assertIn('comfy_workflow.py', source.getnames())
            source.extractall(extracted, filter='data')
        script = r'''
import sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
class NoRuntimeImports:
    def find_spec(self,name,path=None,target=None):
        if name.split('.')[0] in {'torch','transformers','huggingface_hub','requests','boto3','sqlalchemy'}:
            raise AssertionError('runtime or network dependency imported')
sys.meta_path.insert(0,NoRuntimeImports())
from studio_platform.runtime_catalog import PROFILE_IDS,get_profile,engine_manifest
from studio_platform.inference.wangp_profile_dispatch import H3ProfileCompiler,validate_prepared
from comfy_workflow import native_output_spec
import comfy_workflow
assert Path(comfy_workflow.__file__).resolve().is_relative_to(root)
from studio_platform.h3_profile_support import MAX_STEPS
assert MAX_STEPS==100
for identity in PROFILE_IDS:
    profile=get_profile(identity)
    for mode in (model['mode'] for model in profile['models']):
        inputs={'first_frame':'a','last_frame':'b'} if mode=='fl' else {'images':['a','b']}
        request={'model':profile['model_id'],'mode':mode,'prompt':'Synthetic offline fixture','steps':MAX_STEPS,
            'duration':5,'resolution':'480P','seed':'42','inputs':inputs}
        manifest=engine_manifest(identity,mode)
        assets={key:{'metadata':{'kind':'image','model_ready':True,'width':832,'height':480},
            'model':{'key':'owners/owner/assets/'+key+'/file','sha256':'a'*64,'size_bytes':4}} for key in ('a','b')}
        job={'id':'job','owner_id':'owner','request_hash':'b'*64,
            'execution_plan':{'deployment_profile_id':identity,'engine_manifest_digest':manifest.digest},
            'request':{'request':request,'assets':assets,'output_spec':native_output_spec(request),
                'deployment_profile_id':identity,'recipe_id':manifest.document['generation_recipe_id']}}
        import io
        class Store:
            def open(self,key): return io.BytesIO(b'data')
        prepared=H3ProfileCompiler(manifest,lambda item,*a,**k:item)(job,'attempt',Store(),lambda:None)
        validate_prepared(prepared,manifest)
print('all FL/REF profile bundles verified without external modules')
'''
        checked = subprocess.run(
            [sys.executable, '-I', '-S', '-c', script, str(extracted)],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(
            checked.stdout.strip(),
            'all FL/REF profile bundles verified without external modules',
        )


if __name__ == '__main__':
    unittest.main()
