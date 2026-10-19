"""Canonical terms, bounded policy CAS and immutable moderation evidence."""

import copy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from studio_platform.auth import Principal
from studio_platform import content_policy as content
from studio_platform.content_routes import register_routes
from studio_platform.identity_schema import admin_audit, metadata, users
from studio_platform.repository import budget_accounts, documents
from test_platform_repository import LedgerCase


class ContentPolicyTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ids = {
            owner: str(uuid.uuid4()) for owner in ('admin', 'supervan', 'other')
        }
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            for owner, ident in self.ids.items():
                connection.execute(
                    insert(users).values(
                        id=ident,
                        tenant='sixnine',
                        owner=owner,
                        role='admin' if owner == 'admin' else 'user',
                        status='active',
                        display_name=owner,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.admin = Principal('admin', 'browser:admin')
        self.user = Principal('supervan', 'browser:supervan')
        self.api = content.ContentPolicyAPI(
            self.repo, SimpleNamespace(tenant_id='sixnine')
        )
        self.compiled = {
            'recipe_id': 'h3-fixture',
            'request': {'model': 'H3', 'prompt': 'A paper boat on a lake'},
        }
        self.execution = {'backend': 'wangp-worker', 'pool': 'h3-ref'}
        self.job = {
            'id': str(uuid.uuid4()),
            'tenant_id': 'sixnine',
            'owner_id': 'supervan',
            'request_hash': 'f' * 64,
        }
        self.attempt = str(uuid.uuid4())
        self.specs = [
            {
                'kind': 'video',
                'object_key': 'private/result.mp4',
                'size_bytes': 100,
                'sha256': 'a' * 64,
                'validated': True,
            }
        ]

    def policy(self, *, revision=None, key='fixture-policy', **changes):
        body = {
            'expected_revision': revision,
            'version': 'fixture-v1',
            'status': 'reviewed',
            'moderation': {'enabled': False, 'keywords': [], 'output_classifier': None},
            'reason': 'Disposable policy fixture',
            **changes,
        }
        return self.api.update(self.admin, body, key=key)

    def audit_count(self):
        with self.repo.engine.connect() as connection:
            return connection.execute(
                select(func.count()).select_from(admin_audit)
            ).scalar_one()

    def test_defaults_are_draft_off_and_do_not_claim_video_classification(self):
        self.assertEqual(
            self.api.public(),
            {
                'revision': None,
                'version': '2026-10-draft',
                'status': 'draft',
                'moderation': {
                    'enabled': False,
                    'prompt_screening': False,
                    'output_screening': False,
                },
            },
        )
        self.assertEqual(self.api.admin(self.admin)['available_classifiers'], [])
        self.assertIsNone(
            content.check_input(self.repo, 'sixnine', 'supervan', self.compiled)
        )
        self.assertIsNone(
            content.check_output(self.repo, self.job, self.attempt, self.specs)
        )
        with self.repo.engine.connect() as connection:
            user = (
                connection.execute(
                    select(users).where(users.c.id == self.ids['supervan'])
                )
                .mappings()
                .one()
            )
            self.assertIsNone(
                content.require_generation_terms(
                    connection, 'sixnine', 'supervan', self.execution, self.compiled
                )
            )
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^terms_not_reviewed$'
            ):
                content.reviewed_terms(connection, self.user, user)
        with self.assertRaisesRegex(content.ContentPolicyError, '^terms_not_reviewed$'):
            self.api.accept_terms(self.user, {'version': '2026-10-draft'})

    def test_policy_current_canonical_admin_cas_audit_and_idempotency(self):
        with self.repo.engine.connect() as connection:
            dollars = [
                dict(row)
                for row in connection.execute(select(budget_accounts)).mappings()
            ]
        for actor in (
            replace(self.user, role='admin'),
            replace(self.admin, machine=True),
        ):
            with self.assertRaises(content.ContentPolicyError) as caught:
                self.api.update(
                    actor,
                    {
                        'expected_revision': None,
                        'version': 'v1',
                        'status': 'draft',
                        'moderation': {
                            'enabled': False,
                            'keywords': [],
                            'output_classifier': None,
                        },
                        'reason': 'No authority',
                    },
                    key='denied',
                )
            self.assertEqual(caught.exception.status, 403)
        saved = self.policy()
        self.assertEqual(self.policy(), saved)
        with self.assertRaisesRegex(
            content.ContentPolicyError, '^idempotency_conflict$'
        ):
            self.policy(status='draft')
        with self.assertRaisesRegex(
            content.ContentPolicyError, '^content_policy_changed$'
        ):
            self.policy(key='stale')
        self.assertEqual(self.audit_count(), 1)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                [
                    dict(row)
                    for row in connection.execute(select(budget_accounts)).mappings()
                ],
                dollars,
            )
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.id == self.ids['admin'])
                .values(status='suspended')
            )
        with self.assertRaises(content.ContentPolicyError) as caught:
            self.api.admin(self.admin)
        self.assertEqual(caught.exception.status, 403)

    def test_closed_policy_rejects_urls_unknown_classifiers_and_unbounded_keywords(
        self,
    ):
        for moderation in (
            {
                'enabled': True,
                'keywords': [],
                'output_classifier': 'https://attacker.test',
            },
            {'enabled': True, 'keywords': [], 'output_classifier': 'unregistered'},
            {'enabled': True, 'keywords': ['word'] * 101, 'output_classifier': None},
            {'enabled': True, 'keywords': ['Word', 'word'], 'output_classifier': None},
            {'enabled': True, 'keywords': ['x' * 101], 'output_classifier': None},
            {
                'enabled': True,
                'keywords': [],
                'output_classifier': None,
                'api_key': 'private',
            },
        ):
            with (
                self.subTest(moderation=moderation),
                self.assertRaises(content.ContentPolicyError),
            ):
                self.policy(moderation=moderation)
        self.assertEqual(self.audit_count(), 0)

    def test_acceptance_is_human_current_version_and_replay_preserves_timestamp(self):
        saved = self.policy()
        with self.assertRaisesRegex(
            content.ContentPolicyError, '^terms_version_changed$'
        ):
            self.api.accept_terms(self.user, {'version': 'old'})
        with self.assertRaises(content.ContentPolicyError) as caught:
            self.api.accept_terms(
                replace(self.user, machine=True), {'version': 'fixture-v1'}
            )
        self.assertEqual(caught.exception.status, 403)
        first = self.api.accept_terms(self.user, {'version': 'fixture-v1'})
        self.now += 20
        self.assertEqual(
            self.api.accept_terms(self.user, {'version': 'fixture-v1'}), first
        )
        self.assertEqual(self.audit_count(), 2)
        with self.repo.engine.connect() as connection:
            user = (
                connection.execute(
                    select(users).where(users.c.id == self.ids['supervan'])
                )
                .mappings()
                .one()
            )
            self.assertEqual(content.reviewed_terms(connection, self.user, user), first)
            self.assertFalse(
                content.account_terms_metadata(connection, 'sixnine', 'supervan')[
                    'acceptance_required'
                ]
            )
        self.policy(revision=saved['revision'], key='version2', version='fixture-v2')
        with self.repo.engine.connect() as connection:
            self.assertTrue(
                content.account_terms_metadata(connection, 'sixnine', 'supervan')[
                    'acceptance_required'
                ]
            )
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^terms_acceptance_required$'
            ):
                content.require_generation_terms(
                    connection, 'sixnine', 'supervan', self.execution, self.compiled
                )

    def test_new_paid_admission_uses_owner_acceptance_and_only_genuine_free_bypass(
        self,
    ):
        self.policy()
        with self.repo.engine.connect() as connection:
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^terms_acceptance_required$'
            ):
                content.require_generation_terms(
                    connection, 'sixnine', 'supervan', self.execution, self.compiled
                )
            self.assertIsNone(
                content.require_generation_terms(
                    connection,
                    'sixnine',
                    'supervan',
                    {'backend': 'mock', 'pool': 'mock'},
                    self.compiled,
                )
            )
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^terms_acceptance_required$'
            ):
                content.require_generation_terms(
                    connection,
                    'sixnine',
                    'supervan',
                    {'backend': 'mock', 'pool': 'mock', 'credit_quote': {'credits': 1}},
                    self.compiled,
                )
        accepted = self.api.accept_terms(self.user, {'version': 'fixture-v1'})
        with self.repo.engine.connect() as connection:
            evidence = content.require_generation_terms(
                connection, 'sixnine', 'supervan', self.execution, self.compiled
            )
            self.assertEqual(
                {k: evidence[k] for k in ('version', 'accepted_at')}, accepted
            )
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^terms_acceptance_required$'
            ):
                content.require_generation_terms(
                    connection, 'sixnine', 'other', self.execution, self.compiled
                )

    def test_prompt_refusal_has_safe_persistent_hash_only_and_cannot_be_bypassed(self):
        saved = self.policy(
            moderation={
                'enabled': True,
                'keywords': ['forbidden'],
                'output_classifier': None,
            }
        )
        compiled = copy.deepcopy(self.compiled)
        compiled['request']['prompt'] = 'private-user-text FORBIDDEN private-end'
        for _ in range(2):
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^content_moderation_refused$'
            ) as caught:
                content.check_input(self.repo, 'sixnine', 'supervan', compiled)
            self.assertNotIn('forbidden', caught.exception.detail.casefold())
        with self.repo.engine.connect() as connection:
            receipts = (
                connection.execute(
                    select(documents.c.payload).where(
                        documents.c.kind == 'content_input'
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(receipts), 1)
            self.assertNotIn('private-user-text', json.dumps(receipts))
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^content_moderation_refused$'
            ):
                content.verify_input(connection, 'sixnine', 'supervan', compiled, None)
        receipt = content.check_input(self.repo, 'sixnine', 'supervan', self.compiled)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                content.verify_input(
                    connection,
                    'sixnine',
                    'supervan',
                    {**self.compiled, 'content_moderation': receipt},
                    receipt,
                ),
                receipt,
            )
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^content_moderation_receipt_invalid$'
            ):
                content.verify_input(
                    connection, 'sixnine', 'other', self.compiled, receipt
                )
        self.policy(
            revision=saved['revision'],
            key='new-keywords',
            moderation={'enabled': True, 'keywords': [], 'output_classifier': None},
        )
        with self.repo.engine.connect() as connection:
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^content_moderation_changed$'
            ):
                content.verify_input(
                    connection, 'sixnine', 'supervan', self.compiled, receipt
                )
        self.assertFalse(self.api.public()['moderation']['output_screening'])

    def test_output_classification_is_immutable_owned_and_bound_to_exact_media_attempt(
        self,
    ):
        classifier = Mock(return_value='refused')
        with patch.dict(content.CLASSIFIERS, {'fixture': classifier}):
            saved = self.policy(
                moderation={
                    'enabled': True,
                    'keywords': [],
                    'output_classifier': 'fixture',
                }
            )
            receipt = content.check_output(
                self.repo, self.job, self.attempt, self.specs
            )
            self.assertEqual(receipt['status'], 'refused')
            self.assertEqual(
                content.check_output(self.repo, self.job, self.attempt, self.specs),
                receipt,
            )
            classifier.assert_called_once()
            with self.repo.engine.connect() as connection:
                self.assertEqual(
                    content.verify_output(
                        connection, self.job, self.attempt, self.specs, receipt
                    ),
                    receipt,
                )
                for job, attempt, specs, proof in (
                    (self.job, self.attempt, self.specs, None),
                    (self.job, str(uuid.uuid4()), self.specs, receipt),
                    (
                        {**self.job, 'owner_id': 'other'},
                        self.attempt,
                        self.specs,
                        receipt,
                    ),
                    (
                        self.job,
                        self.attempt,
                        [{**self.specs[0], 'sha256': 'b' * 64}],
                        receipt,
                    ),
                    (
                        self.job,
                        self.attempt,
                        self.specs,
                        {**receipt, 'status': 'allowed'},
                    ),
                ):
                    with self.assertRaises(content.ContentPolicyError):
                        content.verify_output(connection, job, attempt, specs, proof)
            self.policy(
                revision=saved['revision'],
                key='off-again',
                moderation={
                    'enabled': False,
                    'keywords': [],
                    'output_classifier': None,
                },
            )
            with self.repo.engine.connect() as connection:
                self.assertEqual(
                    content.verify_output(
                        connection, self.job, self.attempt, self.specs, receipt
                    ),
                    receipt,
                )

    def test_unavailable_classifier_does_not_store_or_invent_an_output_decision(self):
        classifier = Mock(side_effect=RuntimeError('private-provider-token'))
        with patch.dict(content.CLASSIFIERS, {'fixture': classifier}):
            self.policy(
                moderation={
                    'enabled': True,
                    'keywords': [],
                    'output_classifier': 'fixture',
                }
            )
            with self.assertRaisesRegex(
                content.ContentPolicyError, '^content_moderation_unavailable$'
            ) as caught:
                content.check_output(self.repo, self.job, self.attempt, self.specs)
            self.assertNotIn('private-provider', caught.exception.detail)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count())
                    .select_from(documents)
                    .where(documents.c.kind == 'content_output')
                ).scalar_one(),
                0,
            )
        with self.assertRaisesRegex(
            content.ContentPolicyError, '^content_moderation_unavailable$'
        ):
            content.check_output(self.repo, self.job, self.attempt, self.specs)

    def test_http_public_metadata_and_machine_terms_refusal(self):
        app = FastAPI()
        register_routes(
            app,
            self.api.settings,
            self.repo,
            lambda request: replace(self.user, machine=True),
        )
        with TestClient(app) as client:
            public = client.get('/v1/legal/policy')
            self.assertEqual(public.status_code, 200)
            self.assertEqual(public.json()['status'], 'draft')
            response = client.post(
                '/v1/account/terms', json={'version': '2026-10-draft'}
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['code'], 'human_account_required')
            self.assertEqual(response.headers['cache-control'], 'no-store')


if __name__ == '__main__':
    unittest.main()
