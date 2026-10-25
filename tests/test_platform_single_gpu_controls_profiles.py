"""Additive Base controls on the existing one-GPU path; no provider/model calls."""

import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from studio_platform.capabilities import compile_request
from studio_platform.dstack_factory import RuntimeFactory
from studio_platform.execution_policy import input_envelope_blockers, validate_policy
from studio_platform.h3_profile_support import envelope
from studio_platform.inference import wangp_profile_dispatch
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.runtime_catalog import (
    PROFILE_DIRECTORY,
    PROFILE_IDS,
    engine_manifest,
    get_profile,
)
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_full_controls import image_asset
from tools.build_dstack_sources import build


INT8 = 'h3-unpruned33b-int8-sdpa-p3-144gib-single-controls-v4'
BF16 = 'h3-unpruned33b-bf16-sdpa-p3-256gib-single-controls-v4'
SINGLE_PROFILES = (INT8, BF16)


class SingleGPUControlsProfileTests(unittest.TestCase):
    def test_retained_fourteen_profile_files_and_twenty_five_manifests_are_immutable(
        self,
    ):
        retained = []
        self.assertEqual(PROFILE_IDS[14:16], SINGLE_PROFILES)
        self.assertEqual(len(PROFILE_IDS[:14]), 14)
        for identity in PROFILE_IDS[:14]:
            raw = (
                (PROFILE_DIRECTORY / (identity + '.json'))
                .read_bytes()
                .replace(b'\r\n', b'\n')
            )
            retained.append(
                {
                    'id': identity,
                    'profile_sha256': hashlib.sha256(raw).hexdigest(),
                    'manifests': {
                        model['mode']: engine_manifest(identity, model['mode']).digest
                        for model in json.loads(raw)['models']
                    },
                }
            )
        wire = json.dumps(retained, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(
            hashlib.sha256(wire).hexdigest(),
            '0fc91e6f2c5de8e8fc21a51d0df1518a7d25f216cfb0caa252e9555b5c2ef2b7',
        )

    def test_actual_single_slot_geometry_keeps_precision_and_full_native_mapping(self):
        single = get_profile(
            'h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-fl-r2-v3'
        )
        int8_full = get_profile('h3-unpruned33b-int8-sdpa-p3-controls-v2')
        bf16_full = get_profile('h3-unpruned33b-bf16-sdpa-p3-controls-v3')
        selected = get_profile(INT8)
        self.assertEqual(selected['runtime'], single['runtime'])
        self.assertEqual(selected['hardware_admission'], single['hardware_admission'])
        self.assertEqual(selected['minimum_ram_bytes'], single['minimum_ram_bytes'])
        self.assertEqual(selected['components'], int8_full['components'])
        self.assertEqual(get_profile(BF16)['runtime'], bf16_full['runtime'])
        self.assertEqual(get_profile(BF16)['components'], bf16_full['components'])
        self.assertEqual(get_profile(BF16)['minimum_ram_bytes'], 256 * 2**30)
        for identity in SINGLE_PROFILES:
            profile = get_profile(identity)
            self.assertEqual(profile['gpu_count_options'], [1])
            self.assertEqual(profile['tested_topology']['gpus_per_host'], 1)
            self.assertEqual(profile['minimum_free_vram_bytes'], 88 * 2**30)
            self.assertEqual(profile['minimum_disk_bytes'], 350 * 2**30)
            self.assertEqual(profile['verified_cases'], [])
            for mode in ('fl', 'ref'):
                with self.subTest(profile=identity, mode=mode):
                    manifest = engine_manifest(identity, mode)
                    self.assertEqual(
                        manifest.document['topology'], {'slots': 1, 'gpus': 1}
                    )
                    self.assertEqual(
                        manifest.document['output_delivery'], 'native-frames-r2-v1'
                    )
                    inputs = {
                        'first_frame': 'first',
                        'last_frame': 'last',
                        'guides': [{'media_id': 'injection', 'time_seconds': 0.5}],
                    }
                    if mode == 'ref':
                        inputs['images'] = ['reference']
                    body = generation_request(
                        deployment_profile_id=identity,
                        recipe_id=manifest.document['generation_recipe_id'],
                        inputs=inputs,
                        controls={'steps': 37, 'seed': '42', 'sampler_name': 'er_sde'},
                    )
                    compiled, _ = compile_request(
                        body, image_asset, backend='wangp-worker'
                    )
                    settings = wangp_profile_dispatch.compile_settings(
                        compiled['request'],
                        {
                            key: item['metadata']
                            for key, item in compiled['assets'].items()
                        },
                        compiled['output_spec'],
                        {key: 'input-' + key for key in compiled['assets']},
                        identity,
                    )
                    self.assertEqual(
                        settings['config'], profile['runtime']['task_config']
                    )
                    self.assertEqual(compiled['request']['model'], profile['model_id'])
                    value = policy(time.time())
                    value.update(
                        deployment_profile_id=identity,
                        model_id=profile['model_id'],
                        backend='wangp-worker',
                        recipe_ids=[manifest.document['generation_recipe_id']],
                        engine_manifest_digest=manifest.digest,
                        output_delivery='native-frames-r2-v1',
                        envelope=envelope(identity, mode),
                    )
                    value['qualification'].update(
                        status='runtime_required', profile=QUEUED_TASK_PROFILE
                    )
                    self.assertEqual(validate_policy(value), value)
                    self.assertEqual(
                        input_envelope_blockers(
                            compiled, value['envelope']['input_limits']
                        ),
                        [],
                    )

    def test_both_modes_build_and_construct_the_actual_single_gpu_runtime_factory(self):
        with tempfile.TemporaryDirectory(
            prefix='sixnine-single-controls-'
        ) as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            selected = [
                (identity, mode)
                for identity in SINGLE_PROFILES
                for mode in ('fl', 'ref')
            ]
            with patch(
                'subprocess.run',
                side_effect=AssertionError('no model, GPU or installer call'),
            ):
                receipt = build(root / 'sources', selected)
            self.assertEqual(len(receipt['sources']), 4)
            self.assertTrue(
                all(
                    row['gpu_count'] == 1 and row['profile_slot_index'] == 0
                    for row in receipt['sources']
                )
            )
            for name in ('ssh-key', 'broker.json'):
                path = root / name
                path.write_text('private offline fixture')
                path.chmod(0o600)
            config = {
                'version': 1,
                'work_dir': str(root / 'runtime'),
                'source_index_file': str(root / 'sources/index.json'),
                'ssh_key_file': str(root / 'ssh-key'),
                'known_hosts_file': str(root / 'known-hosts'),
                'broker_config_file': str(root / 'broker.json'),
                'first_local_port': 19100,
                'last_local_port': 19103,
                'trust_first_host_key': False,
            }
            path = root / 'runtime-config.json'
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            factory = RuntimeFactory(None, None, path)
            self.assertEqual(set(factory.sources), set(selected))
            self.assertFalse(factory.work_dir.exists())


if __name__ == '__main__':
    unittest.main()
