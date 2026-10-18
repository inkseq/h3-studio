"""HTTP owner/admin separation and exact-plan approval, disposable local SQL."""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.repository import Scope


class SpendingRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(
            Settings(
                Path(self.temp.name),
                auth_mode="local-test",
                operator_capacity_owners=("superdan", "supervan"),
                operator_admin_owners=("superdan",),
            )
        )
        self.client = TestClient(self.app)
        self.login("superdan")

    def tearDown(self):
        self.client.close()
        self.app.state.repository.close()
        self.temp.cleanup()

    def login(self, owner):
        self.assertEqual(
            self.client.post(
                '/api/auth/login', json={'username': owner, 'password': ''}
            ).status_code,
            200,
        )

    def test_settings_are_owned_cas_and_machine_cannot_edit(self):
        path = '/v1/account/spending-settings'
        original = self.client.get(path).json()
        self.assertEqual(original['value']['daily_limit_microusd'], 0)
        self.assertEqual(original['usage']['breakdown']['schema_version'], 1)
        self.assertEqual(original['usage']['breakdown']['attribution_scope'], 'account')
        value = {**original['value'], 'daily_limit_microusd': 2_000_000}
        self.assertEqual(
            self.client.put(
                path, json={'expected_version': 0, 'value': value}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.put(
                path, json={'expected_version': 0, 'value': value}
            ).status_code,
            409,
        )
        self.login('supervan')
        self.assertEqual(
            self.client.get(path).json()['value']['daily_limit_microusd'], 0
        )
        self.assertEqual(
            self.client.get('/v1/operator/admin/spending-settings').status_code, 403
        )
        self.assertEqual(
            self.client.put(
                path,
                json={
                    'expected_version': 0,
                    'value': {**value, 'connection_limits': {'key:foreign': 1}},
                },
            ).status_code,
            404,
        )
        token = self.app.state.auth.create_key(
            'supervan',
            authenticated_session=self.app.state.auth.session(
                self.client.cookies.get('sixnine_session')
            ),
            name='limited',
            scopes=['jobs:read'],
            project_ids=[],
            all_projects=True,
            expires_in_days=1,
        )
        with TestClient(
            self.app, headers={'Authorization': 'Bearer ' + token['api_key']}
        ) as machine:
            self.assertEqual(
                machine.put(
                    path, json={'expected_version': 0, 'value': value}
                ).status_code,
                403,
            )

    def test_approval_rejects_foreign_and_expired_plans(self):
        repo = self.app.state.repository
        plan = repo.create_plan(
            Scope('sixnine', 'supervan', 'project-x'),
            {'prompt': 'private'},
            {'pool': 'test', 'expected_runtime_s': 1},
            expires_at=repo.clock() + 60,
        )
        response = self.client.post(
            '/v1/account/spending-confirmations', json={'plan_ids': [plan['id']]}
        )
        self.assertEqual(response.status_code, 404)
        original_now = repo.clock()
        expired = repo.create_plan(
            Scope('sixnine', 'superdan', 'project-x'),
            {},
            {'pool': 'test', 'expected_runtime_s': 1},
            expires_at=original_now + 60,
        )
        repo.clock = lambda: original_now + 120
        self.assertEqual(
            self.client.post(
                '/v1/account/spending-confirmations', json={'plan_id': expired['id']}
            ).status_code,
            409,
        )

    def test_admin_task_limit_preserves_legacy_alias_cas_and_audit(self):
        from sqlalchemy import insert, select
        from studio_platform.repository import spending_settings, outbox
        from studio_platform.spending_limits import ADMIN_OWNER, ADMIN_DEFAULTS

        path = '/v1/operator/admin/spending-settings'
        legacy = {**ADMIN_DEFAULTS, 'test_daily_limit_microusd': 17_000_000}
        del legacy['test_task_limit_microusd']
        with self.app.state.repository.transaction() as c:
            c.execute(
                insert(spending_settings).values(
                    tenant_id='sixnine',
                    owner_id=ADMIN_OWNER,
                    version=1,
                    value=legacy,
                    updated_at=1.0,
                    actor_id='browser:superdan',
                )
            )
        current = self.client.get(path).json()
        self.assertEqual(current['test_budget_scope'], 'acceptance_task')
        self.assertEqual(current['value']['test_task_limit_microusd'], 17_000_000)
        self.assertNotIn('test_daily_limit_microusd', current['value'])
        # Old UI payloads use the original audited route and normalize once.
        result = self.client.put(
            path,
            json={
                'expected_version': 1,
                'value': {**legacy, 'test_daily_limit_microusd': 18_000_000},
            },
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['value']['test_task_limit_microusd'], 18_000_000)
        self.assertEqual(
            self.client.put(
                path, json={'expected_version': 1, 'value': current['value']}
            ).status_code,
            409,
        )
        ambiguous = {**current['value'], 'test_daily_limit_microusd': 1}
        self.assertEqual(
            self.client.put(
                path, json={'expected_version': 2, 'value': ambiguous}
            ).status_code,
            422,
        )
        with self.app.state.repository.engine.connect() as c:
            audits = list(
                c.execute(
                    select(outbox).where(
                        outbox.c.event_type == 'spending.settings_updated'
                    )
                ).mappings()
            )
            self.assertEqual(len(audits), 1)
            self.assertEqual(
                audits[0]['payload']['new']['value']['test_task_limit_microusd'],
                18_000_000,
            )


if __name__ == '__main__':
    unittest.main()
