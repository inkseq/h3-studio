"""Offline admission boundaries for real-job boot validation; no GPU calls."""

from dataclasses import replace
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from studio_platform.capabilities import capabilities
from studio_platform.execution_policy import validate_policy
from studio_platform.qualification_profiles import (
    QUEUED_TASK_PROFILE,
    MULTIMODAL_INPUT_LIMITS,
)
from studio_platform.repository import request_hash
from studio_platform.settings import Settings
from test_platform_execution_policy import policy


class QueuedTaskPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.value = policy(self.now)
        self.value.update(
            pool="synthetic-pool",
            configuration_id="synthetic-config",
            recipe_ids=["h3-base-fl2va-v1", "h3-base-ref2va-v1"],
        )
        self.value['qualification'].update(
            status='runtime_required',
            profile=QUEUED_TASK_PROFILE,
            evidence_id="synthetic-test-only",
        )
        self.value['reservation']['expected_runtime_s'] = 1800
        self.value['envelope'].update(
            max_duration_seconds=6,
            max_reference_files=3,
            max_guides=1,
            allow_first_last=True,
            input_limits=copy.deepcopy(MULTIMODAL_INPUT_LIMITS),
        )
        self.value['envelope']['controls'].update(
            encoder_device=['cpu'], video_decode=['tiled'], ref_image_size=['max']
        )
        self.path = self.root / 'synthetic-policy.json'
        self.settings = Settings(
            self.root,
            auth_mode='local-test',
            generation_enabled=True,
            execution_backend='comfy-worker',
            execution_policy_file=self.path,
        )

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')
        self.path.chmod(0o600)

    def test_runtime_readiness_cannot_be_labelled_full_accepted_qualification(self):
        for status in ('accepted', 'unverified'):
            value = copy.deepcopy(self.value)
            value['qualification']['status'] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                validate_policy(value)

    def test_no_scope_widening_just_because_synthetic_suite_is_removed(self):
        for field, changed in (
            ('max_images', 2),
            ('max_videos', 2),
            ('max_audios', 2),
            ('max_video_duration_seconds', 5),
        ):
            value = copy.deepcopy(self.value)
            value['envelope']['input_limits'][field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_policy(value)
        value = copy.deepcopy(self.value)
        value['envelope'].pop('input_limits')
        with self.assertRaises(ValueError):
            validate_policy(value)

    def test_public_capabilities_describe_real_task_without_claiming_verified_worker(
        self,
    ):
        self.write(self.value)
        result = capabilities(self.settings)
        for recipe in result['recipes']:
            support = recipe['execution_support']
            self.assertEqual(support['status'], 'runtime_required')
            self.assertEqual(support['verification_method'], 'queued_user_task')
            self.assertTrue(support['runtime_verification_required'])
            self.assertFalse(support['capacity_checked'])
            self.assertIn('真实任务', support['reason'])
            self.assertNotIn('验证通过后才执行原任务', support['reason'])
        self.assertNotIn('budget_accounts', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
