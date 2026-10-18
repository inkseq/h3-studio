"""A connection's chosen cap reaches the same original admission ledger."""
import unittest

from studio_platform.repository import Scope, BudgetExceeded
from studio_platform.spending_limits import read_settings
import test_platform_agent_connect as connect_fixtures


class SpendingConnectionTests(connect_fixtures.LedgerCase):
    def setUp(self):
        super().setUp()
        self.auth=connect_fixtures.Auth(self.repo.engine,mode='local-test',clock=lambda:self.now)
        self.service=connect_fixtures.AgentConnect(self.auth)
        self.session=self.auth.login('superdan','',source='dan')
        self.principal=self.auth.session(self.session)
        self.van_session=self.auth.login('supervan','',source='van')
        self.van=self.auth.session(self.van_session)
        self.origin='http://127.0.0.1:8850'

    issue=connect_fixtures.AgentConnectTests.issue
    material=connect_fixtures.AgentConnectTests.material

    def test_nonzero_connection_cap_is_fingerprinted_saved_and_enforced(self):
        grant=self.issue(daily_limit_microusd=150_000)
        self.assertEqual(grant['connection']['authorization']['daily_limit_microusd'],150_000)
        _,_,exchange=self.material(grant)
        connected=self.service.exchange(**exchange)
        actor='key:'+connected['key']['id']
        with self.repo.engine.connect() as connection:
            value=read_settings(connection,self.auth.tenant,self.principal.owner)['value']
            self.assertEqual(value['connection_limits'][actor],150_000)
        scope=Scope(self.auth.tenant,self.principal.owner,'project-cap',actor)
        self.repo.configure_budget('cap-credit',tenant_id=scope.tenant_id,owner_id=scope.owner_id,limit_microusd=1_000_000)
        def submit(key):
            plan=self.repo.create_plan(scope,{'prompt':'isolated fixture'}, {'pool':'test','expected_runtime_s':1},
                estimated_cost_microusd=100_000,expires_at=self.now+60)
            return self.repo.create_job(scope,plan['id'],key,budget_account_ids=('cap-credit',),spending_actor=actor)
        submit('first')
        with self.assertRaisesRegex(BudgetExceeded,'connection_daily_limit_reached'):submit('second')
        self.assertEqual(self.repo.get_budget('cap-credit')['reserved_microusd'],100_000)


if __name__=='__main__':unittest.main()
