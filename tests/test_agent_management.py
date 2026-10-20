"""Real isolated DB/HTTP connection controls; synthetic credentials, no providers."""

import copy
import json
import secrets
import time
import uuid
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import select, func, insert
import test_platform_repository as ledger
from studio_platform.api import create_app
from studio_platform.auth import digest, Principal
from studio_platform.settings import Settings
from studio_platform.repository import (
    Scope,
    NotFound,
    Conflict,
    BudgetExceeded,
    jobs,
    spending_settings,
    budget_reservations,
)
from studio_platform.agent_connect import ConnectError, connections
from studio_platform.agent_management import DEFAULTS, record_refusal
from studio_platform.guided import empty_project, new_entity
from studio_platform.quick_chat import DEFAULT_SETTINGS, EMPTY_INPUTS


class ManagementTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.origin = 'http://127.0.0.1:8850'
        self.app = create_app(
            Settings(
                Path(self.temp.name) / 'app',
                tenant_id=self.scope.tenant_id,
                auth_mode='local-test',
                database_url=self.url,
                generation_enabled=True,
                execution_backend='mock',
            ),
            repository=self.repo,
        )
        self.auth = self.app.state.auth
        self.service = self.app.state.agent_connect
        self.auth.clock = lambda: self.now
        self.service.clock = lambda: self.now
        token = self.auth.login('superdan', '', source='test')
        self.principal = self.auth.session(token)
        self.qc = self.app.state.quick_chat
        self.sid = self.qc.create_session(self.principal, {}, 'session')['session'][
            'id'
        ]
        self.sid2 = self.qc.create_session(self.principal, {}, 'session2')['session'][
            'id'
        ]
        p = empty_project({'id': 'story', 'title': 'Story'})
        new_entity(p, {'id': 'chapter', 'type': 'chapter', 'title': 'Chapter'})
        new_entity(
            p, {'id': 'scene', 'type': 'scene', 'title': 'Scene', 'parentId': 'chapter'}
        )
        self.repo.put_document(
            Scope(self.scope.tenant_id, 'superdan', '__projects'),
            'project',
            'story',
            p,
            expected_version=0,
        )

    def issue(self, scope=None, permission='generate', cap=0, key=None, legacy=False):
        return self.service.issue(
            self.principal,
            name='Synthetic assistant',
            authorization_profile_id='creator-full',
            authorization_profile_version=1,
            idempotency_key=key or secrets.token_hex(12),
            origin=self.origin,
            daily_limit_microusd=cap,
            **(
                {}
                if legacy
                else {'scope': scope or {'kind': 'all'}, 'permission': permission}
            ),
        )

    def connect(self, **kwargs):
        grant = self.issue(**kwargs)
        token = 'sxp_' + secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(32)
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
        return grant, result, self.auth.bearer(token), token, verifier, body

    def test_scoped_organise_grant_no_cross_session_or_paid_authority(self):
        grant, result, agent, token, _, _ = self.connect(
            scope={'kind': 'session', 'id': self.sid}, permission='organise'
        )
        self.assertFalse(agent.all_projects)
        self.assertNotIn('jobs:write', agent.scopes)
        self.assertNotIn('assistant:run', agent.scopes)
        self.assertEqual(
            [r['id'] for r in self.qc.list_sessions(agent)['sessions']], [self.sid]
        )
        with self.assertRaises(NotFound):
            self.qc.get_session(agent, self.sid2)
        with self.assertRaises(NotFound):
            self.qc._access(agent, self.sid, 'jobs:write')
        with self.assertRaises(NotFound):
            self.qc._access(agent, self.sid, 'assistant:run')
        with TestClient(
            self.app, base_url=self.origin, headers={'Authorization': 'Bearer ' + token}
        ) as client:
            self.assertEqual(client.get('/v1/projects/story').status_code, 404)
            self.assertIn(
                client.post('/v1/operator/instances', json={}).status_code,
                (403, 404, 405, 422),
            )
        self.assertEqual(
            grant['connection']['authorization']['project_ids'],
            result['key']['project_ids'],
        )

    def test_settings_snapshot_legacy_fingerprint_and_single_use_recovery(self):
        legacy = self.issue(legacy=True)
        auth = legacy['connection']['authorization']
        self.assertNotIn('scope', auth)
        self.assertNotIn('permission', auth)
        self.assertNotIn('recovery_ttl_seconds', auth)
        self.assertEqual(
            digest(json.dumps(auth, sort_keys=True, separators=(',', ':'))),
            legacy['connection']['authorization_fingerprint'],
        )
        settings = self.service.change_settings(
            self.principal,
            {
                'expected_version': 0,
                'value': {**DEFAULTS, 'code_ttl_seconds': 800, 'key_lifetime_days': 2},
            },
            'settings',
        )
        grant, result, agent, token, verifier, body = self.connect(
            scope={'kind': 'story', 'id': 'story'}
        )
        frozen = copy.deepcopy(grant['connection']['authorization'])
        self.assertEqual(grant['connection']['expires_at'] - self.now, 800)
        self.service.change_settings(
            self.principal,
            {'expected_version': 1, 'value': {**DEFAULTS, 'key_lifetime_days': 7}},
            'settings2',
        )
        replay = self.service.exchange(**body, recovery_verifier=verifier)
        self.assertEqual(replay['key']['id'], result['key']['id'])
        self.assertEqual(replay['connection']['authorization'], frozen)
        with self.assertRaises(ConnectError):
            self.service.exchange(**body)

    def test_unexchanged_connection_has_explicit_empty_display_shape(self):
        pending = self.issue()['connection']
        value = self.service.get(self.principal, pending['id'])['usage']['breakdown']
        self.assertEqual(value['attribution_scope'], 'connection')
        self.assertEqual(value['attribution_status'], 'not_connected')
        self.assertEqual(value['exposure_microusd'], 0)
        self.assertEqual(set(value['by_kind']), {'job', 'instance'})
        self.assertNotIn('unattributed_exposure_microusd', value)
        grant, _, _, _, _, _ = self.connect()
        connected = self.service.get(self.principal, grant['connection']['id'])[
            'usage'
        ]['breakdown']
        self.assertEqual(set(value), set(connected))
        self.assertEqual(connected['attribution_status'], 'recorded')

    def test_connected_agent_never_inherits_unrelated_shared_legacy_projection(self):
        self.repo.configure_budget(
            "usage-shared", tenant_id=self.scope.tenant_id, limit_microusd=20_000_000
        )
        self.repo.configure_budget(
            "usage-other",
            tenant_id=self.scope.tenant_id,
            owner_id=self.other.owner_id,
            limit_microusd=20_000_000,
        )
        reference = str(uuid.uuid4())
        with self.repo.transaction() as conn:
            for account, ref, amount in (
                ("usage-other", reference, 100),
                ("usage-shared", reference, 11304170),
                ("usage-shared", str(uuid.uuid4()), 500),
            ):
                conn.execute(
                    insert(budget_reservations).values(
                        id=str(uuid.uuid4()),
                        account_id=account,
                        reference_type="instance",
                        reference_id=ref,
                        amount_microusd=amount,
                        state="reserved",
                        created_at=self.now,
                    )
                )
        grant, _, agent, _, _, _ = self.connect(cap=100_000)
        usage = self.service.get(self.principal, grant['connection']['id'])['usage']
        self.assertEqual(usage['projected_microusd'], 0)
        self.assertEqual(usage['breakdown']['exposure_microusd'], 0)
        self.assertEqual(usage['charge_breakdown']['exposure_microusd'], 0)
        with self.repo.engine.connect() as conn:
            before = list(conn.execute(select(budget_reservations)).mappings())
        self.repo.create_job(
            self.scope,
            self.plan(cost=60_000)['id'],
            "agent-own-original",
            budget_account_ids=("owner-budget",),
            spending_actor=agent.actor_id,
        )
        usage = self.service.get(self.principal, grant['connection']['id'])['usage']
        self.assertEqual(usage['projected_microusd'], 60_000)
        with self.assertRaisesRegex(BudgetExceeded, 'connection_daily_limit_reached'):
            self.repo.create_job(
                self.scope,
                self.plan(cost=60_000)['id'],
                "agent-own-over-cap",
                budget_account_ids=("owner-budget",),
                spending_actor=agent.actor_id,
            )
        with self.repo.engine.connect() as conn:
            for row in before:
                self.assertEqual(
                    dict(
                        conn.execute(
                            select(budget_reservations).where(
                                budget_reservations.c.id == row['id']
                            )
                        )
                        .mappings()
                        .one()
                    ),
                    dict(row),
                )

    def test_pause_resume_revoke_stale_principal_and_accepted_replay(self):
        grant, result, agent, token, _, _ = self.connect()
        ident = grant['connection']['id']
        plan = self.plan()
        original = self.repo.create_job(
            self.scope,
            plan['id'],
            'accepted',
            budget_account_ids=('owner-budget',),
            spending_actor=agent.actor_id,
        )
        paused = self.service.change_connection(
            self.principal, ident, {'expected_version': 0, 'paused': True}, 'pause'
        )
        self.assertEqual(paused['status'], 'paused')
        self.assertIsNone(self.auth.bearer(token))
        with self.assertRaisesRegex(Conflict, 'connection_paused'):
            self.repo.create_job(
                self.scope,
                self.plan()['id'],
                'afterpause',
                budget_account_ids=('owner-budget',),
                spending_actor=agent.actor_id,
            )
        self.assertEqual(
            self.repo.create_job(
                self.scope,
                plan['id'],
                'accepted',
                budget_account_ids=('owner-budget',),
                spending_actor=agent.actor_id,
            )['id'],
            original['id'],
        )
        resumed = self.service.change_connection(
            self.principal, ident, {'expected_version': 1, 'paused': False}, 'resume'
        )
        self.assertEqual(resumed['status'], 'connected')
        self.assertIsNotNone(self.auth.bearer(token))
        self.service.revoke(self.principal, ident)
        with self.assertRaisesRegex(Conflict, 'connection_unavailable'):
            self.repo.create_job(
                self.scope,
                self.plan()['id'],
                'afterrevoke',
                budget_account_ids=('owner-budget',),
                spending_actor=agent.actor_id,
            )
        self.assertEqual(
            self.repo.get_job(self.scope, original['id'])['status'], 'queued'
        )

    def test_parallel_cap_admission_editable_usage_and_refusal_audit_after_rollback(
        self,
    ):
        grant, result, agent, token, _, _ = self.connect(cap=100_000)
        ident = grant['connection']['id']
        plans = [self.plan(cost=60_000) for _ in range(2)]

        def submit(index):
            try:
                return self.repo.create_job(
                    self.scope,
                    plans[index]['id'],
                    'race' + str(index),
                    budget_account_ids=('owner-budget',),
                    spending_actor=agent.actor_id,
                )
            except BudgetExceeded as error:
                record_refusal(
                    self.repo.engine, self.scope.tenant_id, agent, str(error), self.now
                )
                return str(error)

        values = self.parallel(submit, 2)
        self.assertEqual(sum(isinstance(v, dict) for v in values), 1)
        self.assertIn('connection_daily_limit_reached', values)
        public = self.service.get(self.principal, ident)
        self.assertEqual(public['usage']['projected_microusd'], 60_000)
        self.assertIn(
            'connection_daily_limit_reached',
            [
                e['event']
                for e in self.service.activity(self.principal, ident)['events']
            ],
        )
        version = None
        with self.repo.engine.connect() as conn:
            version = conn.execute(select(spending_settings.c.version)).scalar_one()
        self.service.change_connection(
            self.principal,
            ident,
            {'expected_version': 0, 'daily_limit_microusd': 200_000},
            'raise-cap',
        )
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(spending_settings.c.version)).scalar_one(),
                version + 1,
            )
        self.now += 86400
        self.principal = self.auth.session(
            self.auth.login('superdan', '', source='next-day')
        )
        self.assertEqual(
            self.service.get(self.principal, ident)['usage']['projected_microusd'],
            60_000,
        )

    def test_concurrent_controls_closed_stale_receipt_and_cross_owner(self):
        grant, _, _, _, _, _ = self.connect()
        ident = grant['connection']['id']
        values = self.parallel(
            lambda i: self.service.change_connection(
                self.principal,
                ident,
                {'expected_version': 0, 'paused': True},
                'pause' + str(i),
            ),
            2,
        )
        self.assertEqual(sum(v.get('no_mutation_committed', False) for v in values), 1)
        loser = next(i for i, v in enumerate(values) if v.get('no_mutation_committed'))
        self.assertEqual(
            values[loser],
            self.service.change_connection(
                self.principal,
                ident,
                {'expected_version': 0, 'paused': True},
                'pause' + str(loser),
            ),
        )
        other = self.auth.session(self.auth.login('supervan', '', source='other'))
        with self.assertRaises(ConnectError):
            self.service.get(other, ident)
        with self.assertRaises(ConnectError):
            self.service.activity(other, ident)
        with self.assertRaises(ConnectError):
            self.service.change_connection(
                other, ident, {'expected_version': 1, 'paused': False}, 'foreign'
            )

    def test_creation_attribution_unknown_history_immutable_revisions(self):
        grant, _, agent, _, _, _ = self.connect()
        sid = self.qc.create_session(agent, {}, 'agent-session')['session']['id']
        self.assertEqual(
            self.qc.get_session(agent, sid)['session']['created_by']['connection_id'],
            grant['connection']['id'],
        )
        self.assertIsNone(
            self.qc.get_session(self.principal, self.sid)['session']['created_by']
        )
        value = self.qc.save_card(
            agent,
            sid,
            {**DEFAULT_SETTINGS, 'prompt': 'Synthetic prompt', 'inputs': EMPTY_INPUTS},
            'agent-card',
        )
        original = copy.deepcopy(value['revision'])
        row = self.qc.group_takes(agent, sid, value['card']['id'])['takes'][0]
        self.assertEqual(row['created_by']['connection_id'], grant['connection']['id'])
        self.qc.star_take(
            self.principal,
            sid,
            row['id'],
            {'expected_version': 0, 'starred': True},
            'star',
        )
        self.assertEqual(self.qc.get_revision(agent, sid, original['id']), original)
        transferred = self.qc.transfer_take(
            agent,
            sid,
            row['id'],
            {
                'target_project_id': 'story',
                'expected_project_version': 1,
                'parent_id': 'scene',
            },
            'story',
        )
        self.assertEqual(transferred['status'], 'completed')
        from studio_platform.agent_management import creator

        with self.repo.engine.connect() as conn:
            self.assertEqual(
                creator(
                    conn,
                    self.scope.tenant_id,
                    agent.owner,
                    'shot',
                    transferred['shot_id'],
                    'story',
                )['connection_id'],
                grant['connection']['id'],
            )
        self.assertNotIn(
            'Synthetic prompt',
            json.dumps(
                self.service.activity(self.principal, grant['connection']['id'])
            ),
        )

    def test_organise_title_and_expired_stale_principal_cannot_pay(self):
        from unittest.mock import Mock
        from studio_platform.auth import personal_keys
        from sqlalchemy import update

        _, _, agent, _, _, _ = self.connect(permission='organise')
        supplier = Mock()
        self.qc.title_generator = supplier
        sid = self.qc.create_session(agent, {}, 'organise-title')['session']['id']
        self.qc.save_card(
            agent,
            sid,
            {
                **DEFAULT_SETTINGS,
                'prompt': 'Synthetic naming input',
                'inputs': EMPTY_INPUTS,
            },
            'organise-draft',
        )
        self.assertFalse(self.qc.generate_title(agent, sid))
        supplier.generate.assert_not_called()
        _, _, generator, _, _, _ = self.connect()
        with self.repo.transaction() as conn:
            conn.execute(
                update(personal_keys)
                .where(personal_keys.c.id == generator.actor_id[4:])
                .values(expires_at=self.repo.clock() - 1)
            )
        with self.assertRaisesRegex(Conflict, 'connection_unavailable'):
            self.repo.create_job(
                self.scope,
                self.plan()['id'],
                'expired',
                budget_account_ids=('owner-budget',),
                spending_actor=generator.actor_id,
            )

    def test_pause_and_admission_serialize_without_cancelling_accepted_work(self):
        grant, _, agent, _, _, _ = self.connect()
        plan = self.plan(cost=60_000)

        def action(index):
            if index == 0:
                return self.service.change_connection(
                    self.principal,
                    grant['connection']['id'],
                    {'expected_version': 0, 'paused': True},
                    'race-pause',
                )
            try:
                return self.repo.create_job(
                    self.scope,
                    plan['id'],
                    'race-admit',
                    budget_account_ids=('owner-budget',),
                    spending_actor=agent.actor_id,
                )
            except Conflict as error:
                return str(error)

        values = self.parallel(action, 2)
        self.assertEqual(values[0]['status'], 'paused')
        if isinstance(values[1], dict):
            self.assertEqual(
                self.repo.get_job(self.scope, values[1]['id'])['status'], 'queued'
            )
        else:
            self.assertEqual(values[1], 'connection_paused')
        with self.assertRaisesRegex(Conflict, 'connection_paused'):
            self.repo.create_job(
                self.scope,
                self.plan()['id'],
                'after-race',
                budget_account_ids=('owner-budget',),
                spending_actor=agent.actor_id,
            )

    def global_settings(self, body=None):
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace(principal=self.principal))
        method = 'GET' if body is None else 'PUT'
        endpoint = next(
            r.endpoint
            for r in self.app.routes
            if getattr(r, 'path', None) == '/v1/account/spending-settings'
            and method in r.methods
        )
        response = endpoint(request) if body is None else endpoint(request, body)
        return json.loads(response.body)

    def test_global_cap_edit_invalidates_row_cas_and_removal_preserves_pause(self):
        grant, _, agent, _, _, _ = self.connect(cap=100_000)
        ident = grant['connection']['id']
        self.service.change_connection(
            self.principal,
            ident,
            {'expected_version': 0, 'paused': True},
            'paused-global',
        )
        before = self.global_settings()
        value = copy.deepcopy(before['value'])
        value['connection_limits'][agent.actor_id] = 200_000
        self.global_settings({'expected_version': before['version'], 'value': value})
        stale = self.service.change_connection(
            self.principal,
            ident,
            {'expected_version': 1, 'daily_limit_microusd': 300_000},
            'stale-cap',
        )
        self.assertTrue(stale['no_mutation_committed'])
        self.assertEqual(stale['daily_limit_microusd'], 200_000)
        self.assertTrue(stale['paused'])
        self.assertEqual(stale['state_version'], 2)
        current = self.global_settings()
        value = copy.deepcopy(current['value'])
        value['connection_limits'].pop(agent.actor_id)
        self.global_settings({'expected_version': current['version'], 'value': value})
        current = self.service.get(self.principal, ident)
        self.assertEqual(current['daily_limit_microusd'], 0)
        self.assertEqual(current['state_version'], 3)
        self.assertTrue(current['paused'])
        global_before = self.global_settings()
        value = copy.deepcopy(global_before['value'])
        value['daily_limit_microusd'] = 900_000
        value['ask_above_microusd'] = 50_000
        self.global_settings(
            {'expected_version': global_before['version'], 'value': value}
        )
        self.assertEqual(self.service.get(self.principal, ident)['state_version'], 3)
        self.assertTrue(self.service.get(self.principal, ident)['paused'])

    def test_row_cap_edit_invalidates_global_cas_preserving_unrelated_caps(self):
        grant, _, agent, _, _, _ = self.connect(cap=100_000)
        other, _, other_agent, _, _, _ = self.connect(cap=70_000)
        old = self.global_settings()
        value = copy.deepcopy(old['value'])
        value['connection_limits'][agent.actor_id] = 300_000
        self.service.change_connection(
            self.principal,
            grant['connection']['id'],
            {'expected_version': 0, 'daily_limit_microusd': 200_000},
            'row-first',
        )
        with self.assertRaisesRegex(Conflict, 'spending_settings_changed'):
            self.global_settings({'expected_version': old['version'], 'value': value})
        after = self.global_settings()
        self.assertEqual(after['value']['connection_limits'][agent.actor_id], 200_000)
        self.assertEqual(
            after['value']['connection_limits'][other_agent.actor_id], 70_000
        )
        self.assertEqual(
            self.service.get(self.principal, other['connection']['id'])[
                'state_version'
            ],
            0,
        )

    def test_two_channel_cap_race_has_one_authoritative_winner(self):
        grant, _, agent, _, _, _ = self.connect(cap=100_000)
        old = self.global_settings()
        value = copy.deepcopy(old['value'])
        value['connection_limits'][agent.actor_id] = 200_000

        def action(index):
            if index == 0:
                try:
                    return self.global_settings(
                        {'expected_version': old['version'], 'value': value}
                    )
                except Conflict as error:
                    return str(error)
            return self.service.change_connection(
                self.principal,
                grant['connection']['id'],
                {'expected_version': 0, 'daily_limit_microusd': 300_000},
                'channel-race',
            )

        values = self.parallel(action, 2)
        current = self.service.get(self.principal, grant['connection']['id'])
        if isinstance(values[0], str):
            self.assertEqual(values[0], 'spending_settings_changed')
            self.assertEqual(current['daily_limit_microusd'], 300_000)
            self.assertFalse(values[1].get('no_mutation_committed', False))
        else:
            self.assertTrue(values[1]['no_mutation_committed'])
            self.assertEqual(current['daily_limit_microusd'], 200_000)
        self.assertEqual(current['state_version'], 1)

    def test_same_story_entity_id_has_project_scoped_immutable_creator(self):
        from studio_platform.agent_management import creator
        from types import SimpleNamespace

        first, _, agent_a, _, _, _ = self.connect()
        second, _, agent_b, _, _, _ = self.connect()
        for project_id, actor in [
            ('story-a', agent_a),
            ('story-b', agent_b),
            ('story-human', self.principal),
        ]:
            payload = empty_project({'id': project_id, 'title': 'Synthetic story'})
            new_entity(
                payload, {'id': 'chapter', 'type': 'chapter', 'title': 'Chapter'}
            )
            new_entity(
                payload,
                {
                    'id': 'scene',
                    'type': 'scene',
                    'title': 'Scene',
                    'parentId': 'chapter',
                },
            )
            new_entity(
                payload,
                {'id': 'shot-1', 'type': 'shot', 'title': 'Shot', 'parentId': 'scene'},
            )
            self.app.state.guided.mutate(
                actor, project_id, payload, 'create-' + project_id, create=True
            )
        get_project = next(
            r.endpoint
            for r in self.app.routes
            if getattr(r, 'path', None) == '/v1/projects/{project_id}'
            and 'GET' in r.methods
        )
        request = SimpleNamespace(state=SimpleNamespace(principal=self.principal))
        self.assertEqual(
            get_project('story-a', request)['created_by']['shot-1']['connection_id'],
            first['connection']['id'],
        )
        self.assertEqual(
            get_project('story-b', request)['created_by']['shot-1']['connection_id'],
            second['connection']['id'],
        )
        self.assertEqual(get_project('story-human', request)['created_by'], {})
        with self.repo.transaction() as conn:
            from studio_platform.agent_management import record_created

            record_created(
                conn,
                self.scope.tenant_id,
                agent_b,
                'shot',
                'shot-1',
                self.now,
                'story-a',
            )
            self.assertEqual(
                creator(
                    conn,
                    self.scope.tenant_id,
                    agent_a.owner,
                    'shot',
                    'shot-1',
                    'story-a',
                )['connection_id'],
                first['connection']['id'],
            )
            self.assertIsNone(
                creator(
                    conn,
                    self.scope.tenant_id,
                    agent_a.owner,
                    'shot',
                    'shot-1',
                    'unknown-story',
                )
            )

    def test_legacy_untyped_entities_keep_audit_without_invented_creator(self):
        from studio_platform.project_activity import append_activity, activity
        from studio_platform.agent_management import creator

        grant, _, agent, _, _, _ = self.connect()
        payload = {
            'entities': [
                {'id': 'old', 'title': 'Original retained title'},
                {'id': 'future', 'type': 'unknown-kind'},
                {'id': 'valid', 'type': 'shot'},
            ]
        }
        original = copy.deepcopy(payload)
        with self.repo.transaction() as conn:
            append_activity(
                conn,
                tenant_id=self.scope.tenant_id,
                principal=agent,
                project_id='legacy-story',
                version=1,
                occurred_at=self.now,
                before={'entities': []},
                after=payload,
            )
            self.assertIsNone(
                creator(
                    conn,
                    self.scope.tenant_id,
                    agent.owner,
                    'unknown-kind',
                    'future',
                    'legacy-story',
                )
            )
            self.assertIsNone(
                creator(
                    conn,
                    self.scope.tenant_id,
                    agent.owner,
                    'shot',
                    'old',
                    'legacy-story',
                )
            )
            self.assertEqual(
                creator(
                    conn,
                    self.scope.tenant_id,
                    agent.owner,
                    'shot',
                    'valid',
                    'legacy-story',
                )['connection_id'],
                grant['connection']['id'],
            )
            row = (
                conn.execute(
                    select(activity).where(activity.c.project_id == 'legacy-story')
                )
                .mappings()
                .one()
            )
            self.assertEqual(set(row['target_entity_ids']), {'old', 'future', 'valid'})
        self.assertEqual(payload, original)
