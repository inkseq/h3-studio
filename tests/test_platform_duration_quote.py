"""Duration admission/ledger boundaries; isolated databases, no cloud or SDK calls."""
import copy
import json
import unittest
from unittest.mock import patch

from studio_platform.autoscale import ScalePolicy
from studio_platform.capabilities import capabilities, compile_request
from studio_platform.execution_policy import validate_policy
from studio_platform.repository import request_hash
from studio_platform.scaler import LaunchSpec
from test_platform_api import generation_request
import test_platform_execution_policy as policy_fixtures


REFERENCE = 124 / 24


def opt_in(value, now):
    value['reservation'].update(cost_microusd=800_000, expected_runtime_s=1800,
        duration_reference_seconds=REFERENCE, expires_at=now+10800)
    value['qualification']['expires_at'] = now+10800


class DurationQuoteTests(unittest.TestCase):
    write = policy_fixtures.ExecutionPolicyTests.write
    evaluate = policy_fixtures.ExecutionPolicyTests.evaluate

    def setUp(self):
        policy_fixtures.ExecutionPolicyTests.setUp(self)
        self.now = self.repo.clock()
        opt_in(self.value, self.now)
        for account, owner in (('test-tenant:sixnine', None), ('test-owner:sixnine:superdan', 'superdan')):
            self.repo.configure_budget(account, tenant_id='sixnine', owner_id=owner, limit_microusd=10_000_000)
        self.write()

    def duration(self, requested, **controls):
        body = generation_request()
        body['controls'].update(duration=requested, **controls)
        self.compiled, self.fingerprint = compile_request(body, lambda _: None)
        return self.evaluate()

    def test_native_fourteen_and_fifteen_increase_ledger_allowance_not_performance_claim(self):
        before = copy.deepcopy(self.value)
        for requested, native, runtime, cost in ((14, 14.375, 5009, 2225807),
                (15, 362/24, 5255, 2335484)):
            with self.subTest(requested=requested):
                result = self.duration(requested)
                self.assertTrue(result.execution['enabled'], result.execution['blockers'])
                self.assertEqual(self.compiled['output_spec']['actual_duration'], native)
                self.assertEqual(result.execution['expected_runtime_s'], runtime)
                self.assertEqual(result.cost, cost)
                self.assertEqual(result.estimate['cost_microusd'], cost)
                self.assertEqual(result.estimate['estimate_basis'], 'operator_allowance')
                self.assertFalse(result.estimate['performance_scaling_verified'])
                self.assertFalse(result.estimate['actual_charge_known'])
                self.assertEqual(result.execution['policy_hash'], request_hash(before))
                self.assertEqual(self.value, before)

    def test_shorter_low_resolution_fewer_steps_do_not_reduce_original_minimum(self):
        for duration in (4, 5):
            with self.subTest(duration=duration):
                result = self.duration(duration, resolution='480P', steps=4)
                self.assertEqual(result.cost, 800_000)
                self.assertEqual(result.execution['expected_runtime_s'], 1800)
                self.assertEqual(result.estimate['duration_scale_factor'], 1)

    def test_old_policy_without_opt_in_keeps_cost_runtime_and_estimate_contract(self):
        self.value['reservation'].pop('duration_reference_seconds')
        self.write()
        result = self.duration(15)
        self.assertTrue(result.execution['enabled'])
        self.assertEqual(result.cost, 800_000)
        self.assertEqual(result.execution['expected_runtime_s'], 1800)
        self.assertNotIn('duration_scale_factor', result.estimate)
        self.assertEqual(result.estimate['kind'], 'budget_reservation')

    def test_native_duration_not_requested_or_edit_duration_drives_reservation(self):
        body = generation_request()
        body['controls'].update(duration=14)
        body['client_edit'] = {'planned_duration_seconds': 5}
        self.compiled, self.fingerprint = compile_request(body, lambda _: None)
        self.assertEqual(self.evaluate().cost, 2225807)

    def test_boundary_budget_checks_scaled_amount_and_exact_budget_can_create_original_job(self):
        amount = 2335484
        account = 'test-owner:sixnine:superdan'
        self.repo.configure_budget(account, tenant_id='sixnine', owner_id='superdan', limit_microusd=amount-1)
        denied = self.duration(15)
        self.assertFalse(denied.execution['enabled'])
        self.assertTrue(any('额度不足' in reason for reason in denied.execution['blockers']))
        self.repo.configure_budget(account, tenant_id='sixnine', owner_id='superdan', limit_microusd=amount)
        allowed = self.evaluate()
        self.assertTrue(allowed.execution['enabled'])
        plan = self.repo.create_plan(self.scope, self.compiled, allowed.execution,
            expires_at=allowed.expires_at, estimated_cost_microusd=allowed.cost)
        job = self.repo.create_job(self.scope, plan['id'], 'scaled-budget-test',
            budget_account_ids=allowed.execution['budget_account_ids'])
        self.assertEqual(job['expected_runtime_s'], 5255)
        self.assertEqual(self.repo.get_budget(account)['reserved_microusd'], amount)
        self.assertEqual(self.repo.get_budget(account)['limit_microusd'], amount)

    def test_original_expiry_and_activation_use_same_long_duration_allowance(self):
        deadline = self.now + 6000
        self.value['qualification']['expires_at'] = deadline
        self.value['reservation']['expires_at'] = deadline
        self.write()
        with patch.object(self.repo, 'clock', return_value=self.now):
            result = self.duration(15)
            self.assertEqual(result.expires_at, deadline-5255)
            job = {'execution_plan': result.execution, 'request': self.compiled}
            self.assertTrue(self.policies.activation_allowed(job))
        with patch.object(self.repo, 'clock', return_value=deadline-5255):
            self.assertFalse(self.policies.activation_allowed(job))
            self.assertFalse(self.evaluate().execution['enabled'])
        self.assertEqual(self.value['reservation']['expires_at'], deadline)

    def test_reference_schema_rejects_zero_bool_nan_tiny_and_unknown_fields(self):
        for reference in (0, True, float('nan'), 1e-300):
            value = copy.deepcopy(self.value)
            value['reservation']['duration_reference_seconds'] = reference
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                validate_policy(value)
        value = copy.deepcopy(self.value)
        value['reservation']['duration_scale'] = 1
        with self.assertRaises(ValueError):
            validate_policy(value)

    def test_public_duration_allowance_explains_baseline_expiry_without_operator_identity_leak(self):
        result = capabilities(self.settings)
        for recipe in result['recipes']:
            support = recipe['execution_support']
            allowance = support['duration_allowance']
            self.assertEqual(allowance['reference_native_duration_seconds'], REFERENCE)
            self.assertEqual(allowance['baseline_runtime_allowance_s'], 1800)
            self.assertEqual(allowance['estimate_basis'], 'operator_allowance')
            self.assertFalse(allowance['performance_scaling_verified'])
            self.assertIn('baseline_latest_start', allowance['expires_at_semantics'])
            self.assertIn('具体时长和冷启动必须预检', allowance['description'])
            self.assertNotEqual(allowance['native_sampling_duration_source'], allowance['requested_export_duration_source'])
            self.assertEqual(support['expires_at'], self.value['reservation']['expires_at']-1800)
        public = json.dumps(result)
        for private in (self.value['pool'], self.value['configuration_id'],
                self.value['qualification']['evidence_id'], self.value['reservation']['source_id'],
                *self.value['budget_accounts']):
            self.assertNotIn(private, public)
        self.value['reservation'].pop('duration_reference_seconds')
        self.write()
        self.assertTrue(all('duration_allowance' not in recipe['execution_support']
            for recipe in capabilities(self.settings)['recipes']))

    def test_cold_deadline_includes_scaled_runtime_and_never_changes_approved_window(self):
        # The warm fixture contains a registered fake slot. The cold check
        # hides it from serving while giving the isolated ledger room for a
        # separate hypothetical capacity approval; nothing is rented.
        self.repo.configure_capacity(max_instances=2, max_physical_gpus=2)
        self.repo.configure_pool('synthetic-pool', max_instances=2, max_physical_gpus=2)
        for available, enabled in ((8255, False), (8256, True)):
            scale = ScalePolicy(dry_run=False, max_instances=1, max_physical_gpus=1,
                cold_start_s=3000, hard_deadline=self.now+available,
                approved_remaining_microusd=10_000_000, instance_reservation_microusd=4_500_000)
            approval_id = 'long-cold-'+str(available)
            self.repo.approve_capacity(approval_id, tenant_id=self.scope.tenant_id,
                pool='synthetic-pool', model_id=self.value['model_id'], configuration_id='synthetic-config',
                recipe_ids=self.value['recipe_ids'], policy_hash=request_hash(self.value),
                qualification_evidence_id=self.value['qualification']['evidence_id'],
                qualification_expires_at=self.value['qualification']['expires_at'],
                quote_expires_at=self.value['reservation']['expires_at'], expires_at=self.now+available,
                launch=LaunchSpec('test-only', 'synthetic-config', self.value['model_id']),
                scale_policy=scale, budget_scope=self.scope,
                budget_account_ids=['test-owner:sixnine:superdan'], enabled=True)
            with patch.object(self.repo, 'clock', return_value=self.now), \
                    patch.object(self.policies.control, 'pool_status', return_value={'ready': 0, 'busy': 0}):
                result = self.duration(15)
            with self.subTest(available=available):
                self.assertEqual(result.execution['enabled'], enabled, result.execution)
                self.assertEqual(result.execution['expected_runtime_s'], 5255)
                if enabled:
                    self.assertEqual(result.expires_at, self.now+1)
                else:
                    self.assertTrue(any('剩余运行窗口不足' in reason for reason in result.execution['blockers']), result.execution)
            # Prevent the next scenario from selecting this immutable approval.
            self.repo.set_capacity_approval_enabled(approval_id, enabled=False)


if __name__ == '__main__':
    unittest.main()
