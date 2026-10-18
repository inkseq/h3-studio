"""Pinned FL/REF rollout policy regression; local metadata only, no capacity."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from studio_platform.execution_policy import validate_policy
from studio_platform.execution_profiles import read_profiles
from studio_platform.runtime_catalog import get_profile, engine_manifest, timing_hint


FIXTURE = Path(__file__).parent/'fixtures/targon_execution_policies.json'


class TargonExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policies = json.loads(FIXTURE.read_text(encoding='utf-8'))['policies']

    def test_rollout_policies_remain_runtime_required_after_partial_native_evidence(self):
        loaded = read_profiles(FIXTURE.resolve())
        self.assertEqual(len(loaded), 2)
        for mode, value in zip(('fl', 'ref'), self.policies):
            with self.subTest(mode=mode):
                profile = get_profile(value['deployment_profile_id'])
                self.assertEqual(len(profile['verified_cases']), 2)
                self.assertEqual(len(profile['qualification_cases']), 6)
                self.assertIs(validate_policy(value), value)
                self.assertEqual(value['pool'], 'op-targon-pruned-'+mode)
                self.assertEqual(value['configuration_id'], 'op-targon-pruned-'+mode)
                self.assertEqual(value['qualification']['status'], 'runtime_required')
                self.assertEqual(value['budget_accounts'], ['demand-job-total-20261004',
                    'demand-job-owner-{owner_id}-20261004'])
                self.assertEqual(loaded[(profile['id'],value['recipe_ids'][0])], value)
                manifest = engine_manifest(profile['id'], mode)
                self.assertEqual(value['engine_manifest_digest'], manifest.digest)
                self.assertIs(manifest.document['production_adapter_verified'], False)
                case = next(c for c in profile['qualification_cases'] if c['mode']==mode)
                self.assertIsNone(timing_hint(profile['id'],mode,case['width'],case['height'],
                    case['frames'],case['fps'],case['steps'],case['input_roles']))

    def test_supported_steps_are_not_limited_by_historical_measurements(self):
        for original in self.policies:
            with self.subTest(recipe=original['recipe_ids'][0]):
                self.assertEqual(original['envelope']['max_steps'], 50)
                candidate = copy.deepcopy(original)
                candidate['envelope']['max_steps'] = 100
                self.assertIs(validate_policy(candidate), candidate)
                # Support is not permission to rewrite the protected saved
                # resource policy, invent timings or claim qualification.
                self.assertEqual(original['envelope']['max_steps'], 50)
                for field in ('qualification', 'reservation', 'budget_accounts',
                              'engine_manifest_digest', 'configuration_id'):
                    self.assertEqual(candidate[field], original[field])

    def test_candidate_cannot_exceed_implemented_limits_or_claim_completed_qualification(self):
        for original in self.policies:
            for field, value, diagnostic in (
                    ('max_steps', 101, 'policy exceeds implemented model support'),
                    ('max_pixels', 1032193, 'Invalid execution envelope limit'),
                    ('max_duration_seconds', 16, 'policy exceeds implemented model support')):
                broken = copy.deepcopy(original)
                broken['envelope'][field] = value
                with self.subTest(recipe=original['recipe_ids'][0], field=field):
                    with self.assertRaisesRegex(ValueError, diagnostic):
                        validate_policy(broken)
            broken = copy.deepcopy(original)
            broken['envelope']['controls']['audio_decode'] = ['chunked']
            with self.assertRaisesRegex(ValueError, 'policy includes unmapped controls'):
                validate_policy(broken)
            broken = copy.deepcopy(original)
            broken['qualification']['status'] = 'accepted'
            with self.assertRaisesRegex(ValueError, '^Invalid execution qualification$'):
                validate_policy(broken)

    def test_candidate_requires_explicit_pending_metadata_without_measurements(self):
        value = self.policies[0]
        for change in ('scope','production_adapter_verified','measurements'):
            profile = get_profile(value['deployment_profile_id'])
            if change=='scope': profile['validation']['scope']='historical_hardware_verified'
            elif change=='production_adapter_verified': profile['validation'][change]=True
            else: profile['qualification_cases'][0]['measurements']=[]
            with patch('studio_platform.runtime_catalog.get_profile', return_value=profile):
                with self.assertRaisesRegex(ValueError, 'candidate qualification is not pending'):
                    validate_policy(value)

    def test_missing_historical_mode_cases_do_not_hide_implemented_support(self):
        for mode, value in zip(('fl', 'ref'), self.policies):
            with self.subTest(mode=mode):
                profile = get_profile(value['deployment_profile_id'])
                case = next(c for c in profile['verified_cases'] if c['mode'] == mode)
                profile['qualification_cases'] = [c for c in profile['qualification_cases']
                    if c['mode'] != mode]
                profile['verified_cases'] = [c for c in profile['verified_cases'] if c['mode'] != mode]
                with patch('studio_platform.runtime_catalog.get_profile', return_value=profile):
                    self.assertIs(validate_policy(value), value)
                    manifest = engine_manifest(profile['id'], mode)
                    self.assertEqual(value['engine_manifest_digest'], manifest.digest)
                    self.assertIs(manifest.document['production_adapter_verified'], False)
                    self.assertIsNone(timing_hint(profile['id'], mode, case['width'], case['height'],
                        case['frames'], case['fps'], case['steps'], case['input_roles']))

    def test_missing_implemented_mode_mapping_still_fails_closed(self):
        for mode, value in zip(('fl', 'ref'), self.policies):
            with self.subTest(mode=mode):
                profile = get_profile(value['deployment_profile_id'])
                profile['models'] = [model for model in profile['models'] if model['mode'] != mode]
                with patch('studio_platform.runtime_catalog.get_profile', return_value=profile):
                    with self.assertRaisesRegex(ValueError, '^wangp_profile_mode_unsupported$'):
                        validate_policy(value)


if __name__ == '__main__':
    unittest.main()
