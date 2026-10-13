import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from studio_platform.runtime_catalog import (PROFILE_IDS, engine_manifest, get_profile,
    public_catalog, runtime_config, timing_hint, validate_manifest)
from studio_platform.inference.wangp_contract import EngineManifest
from studio_platform.runtime_hosts.wangp_download import selected_files
from studio_platform.runtime_hosts.wangp_session import _profile_memory_admission

TARGON_EVIDENCE = 'https://github.com/apedintensor/h3-studio/issues/86#issuecomment-6083341543'


class RuntimeCatalogTests(unittest.TestCase):
    def test_base_single_gpu_fl_preserves_selected_weights_and_old_base_identity(self):
        from studio_platform.runtime_catalog import weights_manifest
        original, candidate = get_profile(PROFILE_IDS[1]), get_profile(PROFILE_IDS[7])
        self.assertEqual(engine_manifest(PROFILE_IDS[1], 'fl').digest,
            'edd56bd3cdfc745fd5b91826ce8dc10bd56b339819803a49f373eaa801096d2a')
        self.assertEqual(weights_manifest(PROFILE_IDS[7], 'fl'), weights_manifest(PROFILE_IDS[1], 'fl'))
        expected_runtime = original['runtime'].copy()
        expected_runtime.update(ram_admission_metric='effective-total-and-available-v1',
                                minimum_total_ram_bytes=144 * 2**30)
        self.assertEqual(candidate['runtime'], expected_runtime)
        self.assertEqual(candidate['gpu_count_options'], [1])
        self.assertEqual(candidate['minimum_ram_bytes'], 144 * 2**30)
        self.assertEqual(candidate['minimum_free_vram_bytes'], original['minimum_free_vram_bytes'])
        self.assertEqual(candidate['minimum_disk_bytes'], original['minimum_disk_bytes'])
        self.assertEqual(candidate['hardware_admission']['minimum_total_vram_bytes'], 90 * 2**30)
        self.assertEqual(candidate['verified_cases'], [])
        self.assertTrue(candidate['qualification_cases'])
        self.assertTrue(all(case['mode'] == 'fl' and 'measurements' not in case
                            for case in candidate['qualification_cases']))
        manifest = engine_manifest(PROFILE_IDS[7], 'fl')
        self.assertEqual(manifest.digest, 'a50aff06326dcb51dcd74bef968fb452a2d2804e07489d032a460bac4a11dc2d')
        self.assertEqual(validate_manifest(manifest)['id'], PROFILE_IDS[7])
        self.assertFalse(manifest.document['inference_qualified'])
        with self.assertRaisesRegex(ValueError, 'profile_mode_unsupported'):
            engine_manifest(PROFILE_IDS[7], 'ref')

    def test_base_single_gpu_requires_both_effective_capacity_and_available_headroom(self):
        candidate = get_profile(PROFILE_IDS[7])
        total, available, free = 144 * 2**30, 128 * 2**30, candidate['minimum_free_vram_bytes']
        for allocation, headroom, vram, allowed in (
                (total, available, free, True),
                (total - 1, available + 10 * 2**30, free, False),
                (total + 10 * 2**30, available - 1, free, False),
                (total, 0, free, False),  # Hidden ancestor headroom must not be credited.
                (total, available, free - 1, False)):
            torch = SimpleNamespace(cuda=SimpleNamespace(mem_get_info=lambda _: (vram, 96 * 2**30)))
            output = io.StringIO()
            with self.subTest(allocation=allocation, headroom=headroom, vram=vram), \
                    patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=headroom), \
                    patch('studio_platform.runtime_hosts.wangp_session._effective_total_profile_ram', return_value=allocation), \
                    redirect_stdout(output):
                if allowed:
                    _profile_memory_admission(torch, candidate)
                else:
                    with self.assertRaisesRegex(ValueError, 'memory_headroom_insufficient'):
                        _profile_memory_admission(torch, candidate)
            observed = json.loads(output.getvalue())
            self.assertEqual(observed['minimum_total_ram_bytes'], total)
            self.assertEqual(observed['minimum_available_ram_bytes'], available)
            self.assertEqual(observed['available_ram_bytes'], headroom)
        candidate['runtime']['minimum_available_ram_bytes'] = True
        with patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=available), \
                patch('studio_platform.runtime_hosts.wangp_session._effective_total_profile_ram', return_value=total):
            with self.assertRaisesRegex(ValueError, 'available_ram_unconfirmed'):
                _profile_memory_admission(torch, candidate)

    def test_cgroup_revision_changes_identity_without_changing_model_or_limits(self):
        previous, revised = get_profile(PROFILE_IDS[5]), get_profile(PROFILE_IDS[6])
        for value in (previous,revised):
            for key in ('id','label','warnings'):
                value.pop(key)
        self.assertEqual(revised,previous)
        expected = ('a92ae3dddb83ab73e4ac292aa7b33f9ea158ac638523d0b0c1e2e771cb0e7517',
                    'a096e69c6b91d6b7200d414e72db870c0198ea02ebcc7ea16bf787091f66d84c')
        for mode,digest in zip(('fl','ref'),expected):
            manifest = engine_manifest(PROFILE_IDS[6],mode)
            self.assertEqual(manifest.digest,digest)
            self.assertEqual(validate_manifest(manifest)['id'],PROFILE_IDS[6])
            self.assertFalse(manifest.document['inference_qualified'])
        self.assertEqual(tuple(engine_manifest(PROFILE_IDS[5],mode).digest for mode in ('fl','ref')),
            ('e32a141d43774d2e1b61bb45432aea2a2bde3e66e36318c84dd8d4538e64d530',
             '47e46d47404c198c667bcb5114b4680bab5ed0aac60c5f77c610a197d62d7367'))

    def test_total_capacity_revision_preserves_v2_and_older_contracts(self):
        previous = get_profile(PROFILE_IDS[4])
        revised = get_profile(PROFILE_IDS[5])
        self.assertEqual(tuple(engine_manifest(PROFILE_IDS[4], mode).digest for mode in ('fl', 'ref')),
            ('83398cb6215ff149f8059e0a7f2d22421a7763d185a52fda189471507b6ab520',
             'd243360fd7cc08266860e58fa1b4c575bc42f83aed55b752819db770f09fd1e7'))
        self.assertEqual(revised['components'], previous['components'])
        self.assertEqual(revised['runtime']['config'], previous['runtime']['config'])
        self.assertEqual(revised['minimum_ram_bytes'], 29 * 2**30)
        self.assertNotIn('minimum_available_ram_bytes', revised['runtime'])
        self.assertEqual(revised['runtime']['ram_admission_metric'], 'effective-total-v1')
        self.assertEqual(revised['runtime']['minimum_total_ram_bytes'], 29 * 2**30)
        self.assertEqual(revised['verified_cases'], [])
        for mode in ('fl', 'ref'):
            manifest = engine_manifest(PROFILE_IDS[5], mode)
            self.assertEqual(validate_manifest(manifest)['id'], PROFILE_IDS[5])
            self.assertNotEqual(manifest.digest, engine_manifest(PROFILE_IDS[4], mode).digest)
            self.assertFalse(manifest.document['inference_qualified'])

    def test_total_capacity_gate_records_headroom_without_inventing_a_threshold(self):
        revised = get_profile(PROFILE_IDS[5])
        torch = SimpleNamespace(cuda=SimpleNamespace(mem_get_info=lambda _: (32 * 2**30, 32 * 2**30)))
        output = io.StringIO()
        with patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=25 * 2**30), \
                patch('studio_platform.runtime_hosts.wangp_session._effective_total_profile_ram', return_value=29 * 2**30), \
                redirect_stdout(output):
            _profile_memory_admission(torch, revised)
        observation = json.loads(output.getvalue())
        self.assertEqual(observation['effective_total_ram_bytes'], 29 * 2**30)
        self.assertEqual(observation['available_ram_bytes'], 25 * 2**30)
        with patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=96 * 2**30), \
                patch('studio_platform.runtime_hosts.wangp_session._effective_total_profile_ram', return_value=29 * 2**30 - 1), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'memory_headroom_insufficient'):
                _profile_memory_admission(torch, revised)
        torch.cuda.mem_get_info = lambda _: (revised['minimum_free_vram_bytes'] - 1, 32 * 2**30)
        with patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=25 * 2**30), \
                patch('studio_platform.runtime_hosts.wangp_session._effective_total_profile_ram', return_value=29 * 2**30), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'memory_headroom_insufficient'):
                _profile_memory_admission(torch, revised)
        revised['runtime']['ram_admission_metric'] = 'unknown'
        with patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=96 * 2**30):
            with self.assertRaisesRegex(ValueError, 'memory_admission_metric_invalid'):
                _profile_memory_admission(torch, revised)

    def test_29gib_revision_preserves_existing_manifest_and_component_identities(self):
        expected = (
            ('5d1d1224b05991d8c3421da0df71b0c56c038a83b313a6714b9255238560c0e8',
             'dfb3da521a2679f616bdac5ac662c44b1439cae985b21ffb176ba37e85f37da8'),
            ('edd56bd3cdfc745fd5b91826ce8dc10bd56b339819803a49f373eaa801096d2a',
             '556cf81fbedcf5cc415f344f46c8ac6fe317be2837b3af2c0e97621df577d71f'),
            ('8994c58c03ac138cec7e65993c346b6e05121d5ded8c83cede5ee46838f4f11f',
             '5a0ac0c4af433306c759cbfa2c1add17667d66f786eb5be9d0e0a5edc7e1431b'),
            ('b73552b1015a6dbf7f5c4f0fe3d1c91caefc7c0706847e1bf476c4c3315af965',
             '4eb14f285908ac15339e06a285bc7627609296eb8ba02ce70d7bfee9f9f4ac5f'),
        )
        for identity, digests in zip(PROFILE_IDS[:4], expected):
            self.assertEqual(tuple(engine_manifest(identity, mode).digest for mode in ('fl', 'ref')), digests)
        self.assertEqual([get_profile(identity)['minimum_ram_bytes'] // 2**30
                          for identity in PROFILE_IDS[:4]], [96, 256, 256, 96])
        original, revised = get_profile(PROFILE_IDS[0]), get_profile(PROFILE_IDS[4])
        self.assertEqual(revised['minimum_ram_bytes'], 29 * 2**30)
        self.assertEqual(revised['components'], original['components'])
        old_runtime = original['runtime'].copy()
        old_runtime['minimum_available_ram_bytes'] = 29 * 2**30
        self.assertEqual(revised['runtime'], old_runtime)
        for mode in ('fl', 'ref'):
            manifest = engine_manifest(PROFILE_IDS[4], mode)
            self.assertEqual(validate_manifest(manifest)['id'], PROFILE_IDS[4])
            self.assertNotEqual(manifest.digest, engine_manifest(PROFILE_IDS[0], mode).digest)
            self.assertEqual(manifest.document['runtime_profile']['minimum_available_ram_bytes'], 29 * 2**30)
            self.assertFalse(manifest.document['inference_qualified'])
        self.assertEqual(revised['verified_cases'], [])
        self.assertTrue(all('measurements' not in case for case in revised['qualification_cases']))

    def test_native_memory_gate_uses_revised_effective_headroom_without_relaxing_vram(self):
        original, revised = get_profile(PROFILE_IDS[0]), get_profile(PROFILE_IDS[4])
        torch = SimpleNamespace(cuda=SimpleNamespace(mem_get_info=lambda _: (32 * 2**30, 32 * 2**30)))
        with patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=29 * 2**30):
            _profile_memory_admission(torch, revised)
            with self.assertRaisesRegex(ValueError, 'memory_headroom_insufficient'):
                _profile_memory_admission(torch, original)
        with patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=29 * 2**30 - 1):
            with self.assertRaisesRegex(ValueError, 'memory_headroom_insufficient'):
                _profile_memory_admission(torch, revised)
        torch.cuda.mem_get_info = lambda _: (revised['minimum_free_vram_bytes'] - 1, 32 * 2**30)
        with patch('studio_platform.runtime_hosts.wangp_session._available_profile_ram', return_value=96 * 2**30):
            with self.assertRaisesRegex(ValueError, 'memory_headroom_insufficient'):
                _profile_memory_admission(torch, revised)

    def test_catalog_is_detached_public_and_unqualified(self):
        catalog = public_catalog()
        self.assertEqual(len(catalog['profiles']), 8)
        for profile in catalog['profiles']:
            self.assertNotIn('runtime', profile)
            self.assertIs(profile['validation']['production_adapter_verified'], False)
            self.assertEqual({v['mode'] for v in profile['models']},
                             {'fl'} if profile['id'] == PROFILE_IDS[7] else {'fl', 'ref'})
        catalog['profiles'][0]['components'].clear()
        self.assertTrue(get_profile(PROFILE_IDS[0])['components'])
        rendered = json.dumps(catalog)
        self.assertNotIn('C:/', rendered)
        self.assertNotIn('/workspace', rendered)
        self.assertNotIn('prompt', rendered.lower())

    def test_exact_joint_cases_do_not_interpolate_or_claim_cold_cache(self):
        profile = PROFILE_IDS[1]
        hint = timing_hint(profile, 'ref', 832, 480, 124, 24, 20, ['video', 'audio', 'image'])
        self.assertAlmostEqual(hint['cases'][0]['measurements'][0]['total_seconds'], 151.9501322209835)
        self.assertIsNone(hint['estimated_seconds'])
        for args in [('ref', 1344, 768, 124, 24, 50, ['video','audio','image']),
                     ('ref', 832, 480, 124, 24, 20, ['image']),
                     ('ref', 832, 480, 120, 24, 20, ['video','audio','image']),
                     ('ref', 832, 480, 124, 24, True, ['video','audio','image'])]:
            self.assertIsNone(timing_hint(profile, *args))
        for profile in public_catalog()['profiles']:
            for case in profile['verified_cases']:
                for sample in case['measurements']:
                    self.assertEqual(sample['cache_state'], 'not_controlled')
                    self.assertEqual(sample['sample_count'], 1)
                    if profile['id'] == PROFILE_IDS[3]:
                        self.assertEqual(sample['evidence'], TARGON_EVIDENCE)
                    else:
                        self.assertIn('/blob/ff2fa85678e77ba195380b148acc23a4c47ef347/', sample['evidence'])

    def test_failed_bf16_and_trimmed_reference_samples_are_excluded(self):
        self.assertEqual([len(get_profile(p)['verified_cases']) for p in PROFILE_IDS], [8, 7, 7, 2, 0, 0, 0, 0])
        self.assertEqual(get_profile(PROFILE_IDS[2])['runtime']['task_config'], 'bf16,bf16')
        self.assertTrue(get_profile(PROFILE_IDS[2])['runtime']['qkv_splitting'])
        self.assertEqual(get_profile(PROFILE_IDS[0])['model_id'], 'MiniMax-H3-Pruned-Rank8-INT8')

    def test_targon_evidence_is_exact_and_keeps_existing_engine_identity(self):
        profile_id = PROFILE_IDS[3]
        expected = {
            'fl': ('b73552b1015a6dbf7f5c4f0fe3d1c91caefc7c0706847e1bf476c4c3315af965',
                   ['first_frame', 'last_frame'], 188.9950643719999),
            'ref': ('4eb14f285908ac15339e06a285bc7627609296eb8ba02ce70d7bfee9f9f4ac5f',
                    ['audio', 'image', 'video'], 162.733107087),
        }
        for mode, (digest, roles, seconds) in expected.items():
            self.assertEqual(engine_manifest(profile_id, mode).digest, digest)
            hint = timing_hint(profile_id, mode, 832, 480, 124, 24, 20, roles)
            self.assertIsNone(hint['estimated_seconds'])
            self.assertEqual(len(hint['cases']), 1)
            self.assertAlmostEqual(hint['cases'][0]['measurements'][0]['total_seconds'], seconds)
        pending = get_profile(profile_id)['qualification_cases']
        self.assertEqual(len(pending), 6)
        for case in pending:
            self.assertNotIn('measurements', case)
            self.assertIsNone(timing_hint(profile_id, case['mode'], case['width'], case['height'],
                case['frames'], case['fps'], case['steps'], case['input_roles']))

    def test_one_mode_manifest_binds_exact_assets_and_runtime(self):
        for profile_id in PROFILE_IDS:
            for mode in (model['mode'] for model in get_profile(profile_id)['models']):
                manifest = engine_manifest(profile_id, mode)
                self.assertEqual(validate_manifest(manifest)['id'], profile_id)
                files = selected_files(manifest.document, manifest.digest)
                self.assertEqual(len(files), 10)
                self.assertTrue(all(('FL2VA' if mode == 'fl' else 'Ref2VA') in v[3]
                                    for v in files if v[0] == 'transformer'))
                self.assertFalse(manifest.document['inference_qualified'])
                changed = manifest.document
                changed['runtime_profile']['task_config'] += ',lower_ram'
                with self.assertRaisesRegex(ValueError, 'profile_manifest_mismatch'):
                    validate_manifest(EngineManifest.from_dict(changed))

    def test_unknown_profile_and_non_absolute_model_root_are_rejected(self):
        with self.assertRaises(ValueError):
            get_profile('../manifest')
        with self.assertRaises(ValueError):
            runtime_config(PROFILE_IDS[0], 'relative')
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_config(PROFILE_IDS[2], directory)
            self.assertEqual(config['profile'], 3)
            self.assertEqual(config['transformer_quantization'], 'bf16')
            self.assertEqual(config['checkpoints_paths'], [str(Path(directory).resolve())])


if __name__ == '__main__':
    unittest.main()
