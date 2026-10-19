"""Accepted Film authorization binds only its original ordered batch items."""

import copy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import func, insert, select, update

from studio_platform.batches import BatchService, batches
from studio_platform.content_policy import ContentPolicyAPI, ContentPolicyError
from studio_platform.film_runs import KIND, accepted_policy_admission
from studio_platform.generation_services import freeze_generation_policy
from studio_platform.identity_schema import metadata, users
from studio_platform.repository import Conflict, documents, jobs, request_hash
import test_film_runs as fixtures


class ContentFilmTests(unittest.TestCase):
    capabilities = staticmethod(fixtures.FilmTests.capabilities)
    preview = fixtures.FilmTests.preview
    raw = fixtures.FilmTests.raw

    def setUp(self):
        fixtures.FilmTests.setUp(self)
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            connection.execute(
                insert(users).values(
                    id=str(uuid.uuid4()),
                    tenant='tenant',
                    owner='owner',
                    role='admin',
                    status='active',
                    display_name='Owner',
                    created_at=self.repo.clock(),
                    updated_at=self.repo.clock(),
                )
            )
        self.policy = ContentPolicyAPI(self.repo, self.app.state.settings)
        self.saved_policy = self.policy.update(
            self.principal,
            {
                'expected_revision': None,
                'version': 'fixture-v1',
                'status': 'reviewed',
                'moderation': {
                    'enabled': False,
                    'keywords': [],
                    'output_classifier': None,
                },
                'reason': 'Disposable accepted Film fixture',
            },
            key='fixture-policy-v1',
        )
        self.policy.accept_terms(self.principal, {'version': 'fixture-v1'})

    def prepare(self, count=2):
        preview = self.preview()
        original = self.raw(preview['id'])
        plans = []
        for index in range(count):
            compiled = {
                'recipe_id': 'fixture-h3',
                'request': {'model': 'H3', 'prompt': 'A paper boat ' + str(index)},
                'client_ref': {
                    'project_id': self.scope.project_id,
                    'shot_id': 'shot-one',
                },
                'film_source_ref': {
                    'run_id': preview['id'],
                    'snapshot_hash': original['snapshot_hash'],
                },
            }
            execution = {
                'backend': 'comfy-http',
                'pool': 'fixture-h3',
                'expected_runtime_s': 5,
            }
            freeze_generation_policy(self.repo, 'tenant', 'owner', compiled, execution)
            plans.append(
                self.repo.create_plan(
                    self.scope,
                    compiled,
                    execution,
                    expires_at=self.repo.clock() + 3600,
                    estimated_cost_microusd=100,
                )
            )

        def link(value):
            value['generation'] = [
                {'shot_id': 'shot-' + str(index), 'plan_id': plan['id'], 'job_id': None}
                for index, plan in enumerate(plans)
            ]
            value['plan_hashes'] = {
                plan['id']: request_hash(plan['request']) for plan in plans
            }

        self.service._mutate(self.principal, 'story-one', preview['id'], link)
        return preview, plans

    def submit(self, preview):
        with patch(
            'studio_platform.spending_limits.check_submission_confirmation',
            return_value=SimpleNamespace(version=1),
        ):
            return self.service.submit(
                self.principal,
                'story-one',
                {'preflight_id': preview['id'], 'confirm': True},
                'original-submit',
            )

    def reserve(self, preview, plans, *, suffix='shots'):
        return BatchService(self.repo, 'tenant').reserve(
            self.principal,
            'story-one',
            [plan['id'] for plan in plans],
            preview['id'] + ':' + suffix,
        )

    def bump(self):
        return self.policy.update(
            self.principal,
            {
                'expected_revision': self.saved_policy['revision'],
                'version': 'fixture-v2',
                'status': 'reviewed',
                'moderation': {
                    'enabled': True,
                    'keywords': ['paper'],
                    'output_classifier': None,
                },
                'reason': 'Disposable changed terms and prompt rule',
            },
            key='fixture-policy-v2',
        )

    def permitted(self, plan, key, *, scope=None):
        with self.repo.engine.connect() as connection:
            return accepted_policy_admission(
                connection, self.repo, scope or self.scope, plan, key
            )

    def test_exact_original_batch_continues_after_policy_bump_without_another_copy(
        self,
    ):
        preview, plans = self.prepare()
        self.submit(preview)
        batch = self.reserve(preview, plans)
        self.bump()
        key = 'batch:' + batch['id'] + ':0'
        self.assertTrue(self.permitted(plans[0], key))
        # The real repository policy boundary preserves only this accepted child.
        with self.repo.engine.connect() as connection:
            self.repo._check_generation_policy(
                connection, self.scope, plans[0], idempotency_key=key
            )
            with self.assertRaises(ContentPolicyError):
                self.repo._check_generation_policy(
                    connection, self.scope, plans[0], idempotency_key='fresh-copy'
                )
        original = self.repo.create_job(
            self.scope, plans[0]['id'], key, initial_status='planned'
        )
        replay = self.repo.create_job(
            self.scope, plans[0]['id'], key, initial_status='planned'
        )
        self.assertEqual(replay['id'], original['id'])
        self.assertFalse(replay['created'])
        with self.assertRaises(ContentPolicyError):
            self.repo.create_job(
                self.scope, plans[0]['id'], 'fresh-copy', initial_status='planned'
            )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(func.count()).select_from(jobs)).scalar_one(),
                1,
            )

    def test_wrong_order_index_other_batch_or_actor_never_earns_the_exemption(self):
        preview, plans = self.prepare()
        self.submit(preview)
        batch = self.reserve(preview, plans)
        key = 'batch:' + batch['id'] + ':0'
        self.assertFalse(self.permitted(plans[1], key))
        self.assertFalse(
            self.permitted(
                plans[0], key, scope=replace(self.scope, actor_id='foreign-actor')
            )
        )
        self.assertFalse(
            self.permitted(
                plans[0], key, scope=replace(self.scope, tenant_id='foreign')
            )
        )
        other = BatchService(self.repo, 'tenant').reserve(
            self.principal, 'story-one', [plan['id'] for plan in plans], 'another-batch'
        )
        self.assertFalse(self.permitted(plans[0], 'batch:' + other['id'] + ':0'))
        with self.repo.transaction() as connection:
            connection.execute(
                update(batches)
                .where(batches.c.id == batch['id'])
                .values(items=list(reversed(batch['items'])))
            )
        self.assertFalse(self.permitted(plans[0], key))

    def test_unaccepted_invalid_time_submission_or_exact_plan_binding_falls_back(self):
        preview, plans = self.prepare()
        batch = self.reserve(preview, plans)
        key = 'batch:' + batch['id'] + ':0'
        self.assertFalse(self.permitted(plans[0], key))
        self.submit(preview)
        original = copy.deepcopy(self.raw(preview['id']))
        for changes in (
            {'accepted_at': True},
            {'accepted_at': 0},
            {'accepted_at': self.repo.clock() + 3600},
            {'submission_hash': ''},
            {'plan_hashes': {plans[1]['id']: request_hash(plans[0]['request'])}},
        ):
            value = {**copy.deepcopy(original), **changes}
            with self.repo.transaction() as connection:
                connection.execute(
                    update(documents)
                    .where(
                        self.repo._scope(documents, self.scope),
                        documents.c.kind == KIND,
                        documents.c.document_id == preview['id'],
                    )
                    .values(payload=value)
                )
            with self.subTest(changes=changes):
                self.assertFalse(self.permitted(plans[0], key))

    def test_missing_optional_batch_table_or_direct_key_never_creates_authority(self):
        preview, plans = self.prepare()
        self.submit(preview)
        self.assertFalse(self.permitted(plans[0], 'direct-copy'))
        self.assertFalse(self.permitted(plans[0], 'batch:' + str(uuid.uuid4()) + ':0'))
        self.assertFalse(self.permitted(plans[0], None))

    def test_first_parent_submission_rechecks_current_terms_and_exact_original_hashes(
        self,
    ):
        preview, plans = self.prepare()
        self.bump()
        with self.assertRaises(ContentPolicyError):
            self.submit(preview)
        value = self.raw(preview['id'])
        self.assertIsNone(value['accepted_at'])
        self.assertFalse(value.get('submission_hash'))
        self.enqueue.assert_not_called()
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(func.count()).select_from(jobs)).scalar_one(),
                0,
            )
        # Even without a new policy, a copied/swapped child hash is not consent.
        self.policy.accept_terms(self.principal, {'version': 'fixture-v2'})
        self.service._mutate(
            self.principal,
            'story-one',
            preview['id'],
            lambda current: current.update(
                plan_hashes={plans[0]['id']: 'f' * 64, plans[1]['id']: 'e' * 64}
            ),
        )
        with self.assertRaisesRegex(Conflict, '^film_source_changed$'):
            self.submit(preview)
        self.enqueue.assert_not_called()

    def test_original_parent_replay_does_not_reauthorize_new_policy(self):
        preview, _plans = self.prepare()
        self.submit(preview)
        self.bump()
        with patch.object(
            self.repo,
            '_check_generation_policy',
            side_effect=AssertionError('Original replay must not reauthorize'),
        ):
            again = self.submit(preview)
        self.assertEqual(again['id'], preview['id'])
        self.assertIsNotNone(again['accepted_at'])
        self.assertEqual(self.enqueue.call_count, 2)
        self.assertEqual(
            self.enqueue.call_args_list[0].args[1:],
            self.enqueue.call_args_list[1].args[1:],
        )


if __name__ == '__main__':
    unittest.main()
