"""Owner-approved Agent credit caps keep their unit and original grant authority."""

import copy
import secrets
import uuid
from unittest.mock import patch

from sqlalchemy import Column, MetaData, String, Table, select, update

from studio_platform.agent_connect import AgentConnect, ConnectError, connections
from studio_platform.agent_management import synchronize_cap_versions
from studio_platform.auth import Auth, AuthenticationError, digest, personal_keys
from studio_platform.credit_schema import (
    accounts as credit_accounts,
    entries,
    metadata as credit_metadata,
)
from studio_platform.identity_schema import metadata as identity_metadata, users
from studio_platform.repository import documents
from studio_platform.spending_limits import read_settings
from test_platform_repository import LedgerCase


class AgentCreditCapTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = 1_700_000_000.0
        self.auth = Auth(
            self.repo.engine,
            tenant=self.scope.tenant_id,
            mode='local-test',
            clock=lambda: self.now,
        )
        self.service = AgentConnect(self.auth)
        self.principal = self.auth.session(
            self.auth.login('superdan', '', source='owner')
        )
        self.origin = 'http://127.0.0.1:8850'
        self.user_id = str(uuid.uuid4())

    def enable(self):
        marker = Table(
            'alembic_version',
            MetaData(),
            Column('version_num', String(32), primary_key=True),
        )
        with self.repo.transaction() as conn:
            identity_metadata.create_all(conn)
            credit_metadata.create_all(conn)
            marker.create(conn)
            conn.execute(marker.insert().values(version_num='20261019_0004'))
            for owner in ('superdan', 'supervan'):
                conn.execute(
                    users.insert().values(
                        id=self.user_id if owner == 'superdan' else str(uuid.uuid4()),
                        tenant=self.scope.tenant_id,
                        owner=owner,
                        role='admin' if owner == 'superdan' else 'user',
                        status='active',
                        display_name=owner,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
            conn.execute(
                documents.insert().values(
                    tenant_id=self.scope.tenant_id,
                    owner_id='__credits',
                    project_id='__credits',
                    kind='credit_policy',
                    document_id='active',
                    version=1,
                    updated_at=self.now,
                    payload={'enabled': True, 'active_revision': 'acceptance'},
                )
            )

    def issue(self, *, key=None, **caps):
        return self.service.issue(
            self.principal,
            name='Owner approved assistant',
            authorization_profile_id='creator-full',
            authorization_profile_version=1,
            idempotency_key=key or secrets.token_hex(10),
            origin=self.origin,
            **caps,
        )

    def exchange(self, grant):
        token, verifier = 'sxp_' + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        body = {
            'code': grant['code'],
            'client_challenge': digest(verifier),
            'token_hash': digest(token),
            'key_prefix': token[:12],
            'expected_authorization_fingerprint': grant['connection'][
                'authorization_fingerprint'
            ],
            'origin': self.origin,
            'source': 'synthetic',
        }
        result = self.service.exchange(**body)
        return result, self.auth.bearer(token), body, verifier

    def stored(self):
        with self.repo.engine.connect() as conn:
            return read_settings(conn, self.scope.tenant_id, 'superdan')

    def test_legacy_exchange_keeps_original_usd_units_and_fingerprint(self):
        grant = self.issue(daily_limit_microusd=125000)
        frozen = copy.deepcopy(grant['connection']['authorization'])
        self.assertNotIn('daily_limit_credits', frozen)
        result, agent, _, _ = self.exchange(grant)
        self.assertEqual(result['connection']['authorization'], frozen)
        current = self.service.get(self.principal, grant['connection']['id'])
        self.assertEqual(
            (current['unit'], current['daily_limit_microusd']), ('microusd', 125000)
        )
        self.assertNotIn('daily_limit_credits', current)
        self.assertIn('projected_microusd', current['usage'])
        self.assertEqual(
            self.stored()['value']['connection_limits'][agent.actor_id], 125000
        )
        self.assertNotIn('connection_credit_limits', self.stored()['value'])

    def test_credit_exchange_confirms_exact_cap_and_preserves_both_maps_once(self):
        self.enable()
        grant = self.issue(
            daily_limit_microusd=125000, daily_limit_credits=31, key='dual-cap'
        )
        frozen = copy.deepcopy(grant['connection']['authorization'])
        self.assertEqual(frozen['daily_limit_credits'], 31)
        self.assertEqual(grant['connection']['unit'], 'credits')
        self.assertNotIn('daily_limit_microusd', grant['connection'])
        with self.assertRaisesRegex(ConnectError, 'idempotency_conflict'):
            self.issue(
                daily_limit_microusd=125000, daily_limit_credits=32, key='dual-cap'
            )
        result, agent, body, verifier = self.exchange(grant)
        self.assertEqual(result['connection']['authorization'], frozen)
        self.assertEqual(result['connection']['daily_limit_credits'], 31)
        self.assertNotIn('daily_limit_microusd', result['connection'])
        stored = self.stored()
        self.assertEqual(stored['version'], 1)
        self.assertEqual(stored['value']['connection_limits'][agent.actor_id], 125000)
        self.assertEqual(
            stored['value']['connection_credit_limits'][agent.actor_id], 31
        )
        recovered = self.service.exchange(**body, recovery_verifier=verifier)
        self.assertTrue(recovered['recovered'])
        self.assertEqual(recovered['connection']['authorization'], frozen)
        self.assertEqual(self.stored(), stored)

    def test_browser_credit_edits_preserve_usd_and_unrelated_limits_and_cas(self):
        self.enable()
        grant = self.issue(daily_limit_microusd=125000, daily_limit_credits=31)
        _, agent, _, _ = self.exchange(grant)
        other = self.issue(daily_limit_microusd=70000, daily_limit_credits=9)
        _, other_agent, _, _ = self.exchange(other)
        original = self.stored()
        ident = grant['connection']['id']
        body = {'expected_version': 0, 'daily_limit_credits': 42}
        current = self.service.change_connection(
            self.principal, ident, body, 'raise-credit'
        )
        self.assertEqual(current['daily_limit_credits'], 42)
        self.assertEqual(
            self.service.change_connection(self.principal, ident, body, 'raise-credit'),
            current,
        )
        stored = self.stored()
        self.assertEqual(stored['version'], original['version'] + 1)
        self.assertEqual(
            stored['value']['connection_limits'], original['value']['connection_limits']
        )
        self.assertEqual(
            stored['value']['connection_credit_limits'],
            {agent.actor_id: 42, other_agent.actor_id: 9},
        )
        stale = self.service.change_connection(
            self.principal,
            ident,
            {'expected_version': 0, 'daily_limit_credits': 99},
            'stale',
        )
        self.assertTrue(stale['no_mutation_committed'])
        self.assertEqual(stale['daily_limit_credits'], 42)
        self.assertEqual(self.stored(), stored)
        self.service.change_connection(
            self.principal,
            ident,
            {'expected_version': 1, 'daily_limit_microusd': 150000},
            'legacy-edit',
        )
        after = self.stored()
        self.assertEqual(
            after['value']['connection_credit_limits'],
            stored['value']['connection_credit_limits'],
        )
        self.assertEqual(after['value']['connection_limits'][agent.actor_id], 150000)

    def test_agent_foreign_owner_and_bad_credit_values_cannot_raise_caps(self):
        self.enable()
        grant = self.issue(daily_limit_credits=10)
        _, agent, _, _ = self.exchange(grant)
        before = self.stored()
        for value in (True, -1, 1.5, '20', 10**25):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ConnectError, 'daily_limit_invalid'),
            ):
                self.service.change_connection(
                    self.principal,
                    grant['connection']['id'],
                    {'expected_version': 0, 'daily_limit_credits': value},
                    'invalid',
                )
        with self.assertRaises(AuthenticationError):
            self.service.change_connection(
                agent,
                grant['connection']['id'],
                {'expected_version': 0, 'daily_limit_credits': 999},
                'self-raise',
            )
        with self.assertRaises(AuthenticationError):
            self.service.issue(
                agent,
                name='Self granted',
                authorization_profile_id='creator-full',
                authorization_profile_version=1,
                idempotency_key='machine-issue',
                origin=self.origin,
                daily_limit_credits=999,
            )
        other = self.auth.session(self.auth.login('supervan', '', source='foreign'))
        with self.assertRaisesRegex(ConnectError, 'connection_not_found'):
            self.service.change_connection(
                other,
                grant['connection']['id'],
                {'expected_version': 0, 'daily_limit_credits': 999},
                'foreign',
            )
        self.assertEqual(self.stored(), before)

    def test_exchange_audit_failure_rolls_back_key_caps_and_claim(self):
        self.enable()
        grant = self.issue(daily_limit_microusd=125000, daily_limit_credits=31)
        with patch.object(
            self.service, '_emit', side_effect=RuntimeError('synthetic audit failure')
        ):
            with self.assertRaisesRegex(RuntimeError, 'synthetic audit failure'):
                self.exchange(grant)
        self.assertEqual(self.stored()['version'], 0)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                list(conn.execute(select(personal_keys.c.id)).scalars()), []
            )
            self.assertIsNone(
                conn.execute(select(connections.c.claimed_at)).scalar_one()
            )

    def test_global_credit_cap_change_invalidates_editor_without_clearing_pause(self):
        self.enable()
        grant = self.issue(daily_limit_credits=10)
        _, agent, _, _ = self.exchange(grant)
        ident = grant['connection']['id']
        self.service.change_connection(
            self.principal, ident, {'expected_version': 0, 'paused': True}, 'pause'
        )
        with self.repo.transaction() as conn:
            synchronize_cap_versions(
                conn,
                self.scope.tenant_id,
                'superdan',
                {},
                {},
                self.now,
                old_credit_limits={agent.actor_id: 10},
                new_credit_limits={agent.actor_id: 20},
            )
        current = self.service.get(self.principal, ident)
        self.assertTrue(current['paused'])
        self.assertEqual(current['state_version'], 2)
        with self.repo.transaction() as conn:
            synchronize_cap_versions(
                conn,
                self.scope.tenant_id,
                'superdan',
                {},
                {},
                self.now,
                old_credit_limits={agent.actor_id: 20},
                new_credit_limits={agent.actor_id: 20},
            )
        self.assertEqual(self.service.get(self.principal, ident)['state_version'], 2)

    def test_credit_usage_counts_original_connection_and_unknown_prior_day_hold(self):
        self.enable()
        grant = self.issue(daily_limit_credits=30)
        _, agent, _, _ = self.exchange(grant)
        records = (
            ('grant', 100, 0, 100, 0, 'grant', 'browser:superdan', self.now - 100000),
            ('hold', 0, 5, 100, 5, 'done', agent.actor_id, self.now - 20),
            ('capture', -5, -5, 95, 0, 'done', agent.actor_id, self.now - 10),
            ('hold', 0, 7, 95, 7, 'unknown', agent.actor_id, self.now - 86400),
            ('hold', 0, 9, 95, 16, 'other', 'key:other', self.now - 5),
            ('capture', -9, -9, 86, 7, 'other', 'key:other', self.now - 1),
        )
        with self.repo.transaction() as conn:
            conn.execute(
                credit_accounts.insert().values(
                    user_id=self.user_id,
                    tenant=self.scope.tenant_id,
                    balance=86,
                    held=7,
                    version=6,
                    created_at=self.now - 100000,
                    updated_at=self.now,
                )
            )
            for kind, amount, delta, balance, held, ref, origin, at in records:
                conn.execute(
                    entries.insert().values(
                        id=str(uuid.uuid4()),
                        tenant=self.scope.tenant_id,
                        user_id=self.user_id,
                        kind=kind,
                        amount=amount,
                        held_delta=delta,
                        balance_after=balance,
                        held_after=held,
                        reference_type='job',
                        reference_id=ref,
                        reason='Synthetic original ledger entry',
                        actor=origin,
                        idempotency_key=ref + ':' + kind,
                        fingerprint='a' * 64,
                        created_at=at,
                        price_revision='acceptance',
                        origin=origin,
                    )
                )
        current = self.service.get(self.principal, grant['connection']['id'])
        self.assertEqual(current['usage'], {'exposure_credits': 12})
        self.assertNotIn('daily_limit_microusd', current)
        self.assertNotIn('projected_microusd', current['usage'])

    def test_http_routes_accept_explicit_credit_caps_but_not_bearer_controls(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        from pathlib import Path

        self.enable()
        app = create_app(
            Settings(
                Path(self.temp.name) / 'app',
                tenant_id=self.scope.tenant_id,
                auth_mode='local-test',
                database_url=self.url,
            ),
            repository=self.repo,
        )
        with TestClient(app, base_url=self.origin) as client:
            client.post(
                '/api/auth/login', json={'username': 'superdan'}
            ).raise_for_status()
            headers = {'Origin': self.origin, 'Idempotency-Key': 'http-grant'}
            invalid = client.post(
                '/v1/account/agent-connections',
                headers=headers,
                json={
                    'authorization_profile_id': 'creator-full',
                    'authorization_profile_version': 1,
                    'daily_limit_credits': None,
                },
            )
            self.assertEqual(invalid.status_code, 422)
            issued = client.post(
                '/v1/account/agent-connections',
                headers=headers,
                json={
                    'name': 'HTTP credit cap',
                    'authorization_profile_id': 'creator-full',
                    'authorization_profile_version': 1,
                    'daily_limit_credits': 27,
                },
            )
            issued.raise_for_status()
            grant = issued.json()
            result, _, _, _ = self.exchange(grant)
            ident = grant['connection']['id']
            changed = client.patch(
                '/v1/account/agent-connections/' + ident,
                headers={**headers, 'Idempotency-Key': 'http-cap'},
                json={'expected_version': 0, 'daily_limit_credits': 28},
            )
            changed.raise_for_status()
            self.assertEqual(changed.json()['daily_limit_credits'], 28)
            token = 'sxp_' + secrets.token_urlsafe(32)
            # Exercise bearer rejection with a real key on this same owner.
            with self.repo.transaction() as conn:
                conn.execute(
                    update(personal_keys)
                    .where(personal_keys.c.id == result['key']['id'])
                    .values(token_hash=digest(token))
                )
            client.cookies.clear()
            denied = client.patch(
                '/v1/account/agent-connections/' + ident,
                headers={**headers, 'Authorization': 'Bearer ' + token},
                json={'expected_version': 1, 'daily_limit_credits': 999},
            )
            self.assertIn(denied.status_code, (401, 403))
