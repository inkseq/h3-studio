"""Production route contract, exact-input preparation; no external services."""

import copy
import json
from types import SimpleNamespace
import unittest

from studio_platform.dstack_operator import DstackOperator
from studio_platform.execution_policy import validate_policy, authority_current
from studio_platform.runtime_catalog import engine_manifest
from tools.prepare_taomate_bindings import BINDINGS, digest, prepare


class ProductionBindingTests(unittest.TestCase):
    def setUp(self):
        self.bindings = json.loads(BINDINGS.read_text())
        self.execution = {
            'schema_version': 1,
            'policies': [
                {
                    'deployment_profile_id': 'preserved-legacy',
                    'recipe_ids': ['original'],
                }
            ],
        }
        self.operator = {
            'version': 1,
            'profiles': [{'id': 'original'}],
            'policy': {
                'enabled': True,
                'max_instances': 2,
                'max_hourly_cost_microusd': 1700000,
                'demand_start': {'profile_ids': ['original']},
            },
        }
        self.raw = tuple(
            json.dumps(x).encode() for x in (self.execution, self.operator)
        )
        self.bindings['source_receipts'].update(
            production_execution_sha256=digest(self.raw[0]),
            production_operator_sha256=digest(self.raw[1]),
        )

    def test_preparation_preserves_original_routes_and_caps(self):
        old = copy.deepcopy((self.execution, self.operator))
        execution, operator = prepare(*self.raw, self.bindings)
        self.assertEqual(execution['policies'][:1], old[0]['policies'])
        self.assertEqual(operator['profiles'][:1], old[1]['profiles'])
        self.assertEqual(operator['policy']['max_instances'], 2)
        self.assertEqual(operator['policy']['max_hourly_cost_microusd'], 1700000)
        self.assertEqual(
            operator['policy']['demand_start']['profile_ids'][:1], ['original']
        )
        self.assertEqual((self.execution, self.operator), old)
        with self.assertRaisesRegex(ValueError, 'original_binding_changed'):
            prepare(self.raw[0] + b' ', self.raw[1], self.bindings)

    def test_changed_or_already_bound_originals_refuse(self):
        execution, operator = prepare(*self.raw, self.bindings)
        raw = tuple(json.dumps(x).encode() for x in (execution, operator))
        with self.assertRaisesRegex(ValueError, 'original_binding_changed'):
            prepare(*raw, self.bindings)
        self.bindings['source_receipts'].update(
            production_execution_sha256=digest(raw[0]),
            production_operator_sha256=digest(raw[1]),
        )
        with self.assertRaisesRegex(ValueError, 'already_exists'):
            prepare(*raw, self.bindings)

    def test_production_policies_match_native_five_step_routes(self):
        service = SimpleNamespace(
            policy={},
            _capacity_owner_known=lambda owner: owner in {'superdan', 'supervan'},
        )
        service._capacity_request = lambda *args: DstackOperator._capacity_request(
            service, *args
        )
        for profile in self.bindings['operator_profiles']:
            validated = DstackOperator._validate_profile(service, profile)
            manifest = engine_manifest(profile['runtime_profile_id'], profile['mode'])
            policy = next(
                p
                for p in self.bindings['execution_policies']
                if p['configuration_id'] == profile['configuration_id']
            )
            validate_policy(policy)
            self.assertTrue(authority_current(policy, 1793154000, 1800))
            self.assertEqual(policy['pool'], profile['pool'])
            self.assertEqual(policy['engine_manifest_digest'], manifest.digest)
            self.assertEqual(
                policy['engine_manifest_digest'], validated['manifest_digest']
            )
            self.assertEqual(policy['envelope']['max_steps'], 5)
            self.assertEqual(profile['memory_gib'], 41)
            self.assertEqual(profile['max_price_microusd'], 850000)
            self.assertEqual(
                profile['budget_account_ids'][0], 'demand-gpu-remaining-20261004'
            )
            self.assertEqual(policy['budget_accounts'][0], 'demand-job-total-20261004')
