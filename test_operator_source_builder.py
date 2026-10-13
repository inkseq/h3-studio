"""Offline exact source/manifest/bootstrap binding; no GPU or network needed."""
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tools import build_operator_sources as subject
from studio_platform.runtime_catalog import PROFILE_IDS, engine_manifest, get_profile
from studio_platform.wangp_bootstrap import SOURCE_NAMES, read_sources

EXPECTED_PROFILE_GPU_COUNTS = {
    'h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1': 1,
    'h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1': 2,
    'h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-splitqkv-v2': 2,
    'h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1': 1,
    'h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-v2': 1,
    'h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3': 1,
    'h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4': 1,
    'h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2': 1,
}
EXPECTED_PROFILE_MODES = {profile: ('fl', 'ref') for profile in EXPECTED_PROFILE_GPU_COUNTS}
EXPECTED_PROFILE_MODES['h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2'] = ('fl',)


class OperatorSourceBuilderTests(unittest.TestCase):
    def test_all_profiles_modes_and_gpu_slots_are_hash_bound_and_isolated(self):
        self.assertEqual(PROFILE_IDS, tuple(EXPECTED_PROFILE_GPU_COUNTS))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'new-source-set'
            # Building sources must never invoke an installer, downloader,
            # subprocess, or GPU probe even when creating the full matrix.
            with patch('subprocess.run', side_effect=AssertionError('no subprocess')):
                receipt = subject.build(output)
            self.assertEqual(len(receipt['sources']), 19)
            self.assertFalse(receipt['production_adapter_verified'])
            self.assertEqual(json.loads((output/'index.json').read_text()), receipt)
            seen, archives, slots = set(), set(), {}
            spec = importlib.util.spec_from_file_location('offline_builder_bootstrap', subject.ROOT/'deploy/wangp/bootstrap.py')
            bootstrap = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bootstrap)
            def linux_path(value):
                self.assertTrue(PurePosixPath(value).is_absolute())
                self.assertNotIn('..', PurePosixPath(value).parts)
                return PurePosixPath(value)
            for entry in receipt['sources']:
                directory = output / entry['directory']
                self.assertEqual({p.name for p in directory.iterdir()}, SOURCE_NAMES)
                profile_id, mode = entry['runtime_profile_id'], entry['mode']
                index, count = entry['profile_slot_index'], entry['gpu_count']
                self.assertEqual(count, EXPECTED_PROFILE_GPU_COUNTS[profile_id])
                self.assertEqual(get_profile(profile_id)['gpu_count_options'], [count])
                identity = (profile_id, mode, index)
                self.assertNotIn(identity, seen)
                seen.add(identity)
                manifest = engine_manifest(profile_id, mode)
                self.assertEqual(entry['engine_manifest_digest'], manifest.digest)
                config = NS(source_dir=directory, engine_manifest_digest=manifest.digest,
                    deployment_profile_id=profile_id, model_id=get_profile(profile_id)['model_id'],
                    recipe_ids=(manifest.document['generation_recipe_id'],),
                    profile_slot_index=index, expected_host_gpus=count)
                files, document = read_sources(config)
                self.assertEqual(document, manifest.document)
                self.assertEqual(entry['source_sha256'], {name: hashlib.sha256(raw).hexdigest() for name,raw in files.items()})
                runtime = json.loads(files['wangp-runtime.json'])
                # Validate actual Linux path semantics without touching this
                # Windows test host's nonexistent remote directories.
                with patch.object(bootstrap, 'checked_path', side_effect=linux_path):
                    self.assertEqual(bootstrap.validate_config(runtime), runtime)
                self.assertEqual(runtime['source_bundle_sha256'], entry['source_sha256']['wangp-package.tar.gz'])
                self.assertEqual(runtime['model_root'], subject.MODEL_ROOT)
                self.assertEqual(runtime['prepared_root'], subject.PREPARED_ROOT)
                self.assertFalse(runtime['dependency_artifact_url'] or runtime['dependency_artifact_path'])
                self.assertEqual(runtime['port'], 8199+index)
                self.assertTrue(runtime['source_bundle_path'].startswith('/workspace/h3-studio/profile-slot-'+str(index)+'/'))
                slots.setdefault((profile_id,mode),[]).append(runtime)
                archives.add(runtime['source_bundle_sha256'])
            self.assertEqual(seen, {
                (profile_id, mode, index)
                for profile_id, count in EXPECTED_PROFILE_GPU_COUNTS.items()
                for mode in EXPECTED_PROFILE_MODES[profile_id] for index in range(count)
            })
            self.assertEqual(len(archives), 1)
            for profile_id in PROFILE_IDS:
                for mode in EXPECTED_PROFILE_MODES[profile_id]:
                    group = slots[(profile_id,mode)]
                    self.assertEqual(len(group), get_profile(profile_id)['gpu_count_options'][0])
                    for field in ('install_root','config_path','status_path','manifest_path','port'):
                        self.assertEqual(len({value[field] for value in group}), len(group))

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'existing'
            output.mkdir()
            sentinel = output/'retain.txt'
            sentinel.write_text('preserve',encoding='utf-8')
            with patch.object(subject,'small_bundle',side_effect=AssertionError('must not build')):
                with self.assertRaisesRegex(ValueError,'output_exists'):
                    subject.build(output)
            self.assertEqual(list(output.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_text(), 'preserve')

    def test_missing_parent_does_not_create_an_implicit_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'missing'/'child'
            with self.assertRaisesRegex(ValueError,'parent_missing'):
                subject.build(output)
            self.assertFalse(output.parent.exists())


if __name__=='__main__':
    unittest.main()
