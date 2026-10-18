"""Retired bootstrap limits never replace owner/settings safety boundaries."""
import unittest
from sqlalchemy import select
from studio_platform.budget_policy import effective_budget_limit, fixed_cap_retired
from studio_platform.repository import Scope, BudgetExceeded, budget_reservations
from test_platform_repository import LedgerCase


class BudgetPolicy254Tests(LedgerCase):
    def test_original_owner_shared_caps_retired_without_losing_accounting(self):
        scope=Scope('sixnine','supervan','p')
        ids=('demand-job-owner-supervan-20261004','demand-job-total-20261004','demand-gpu-remaining-20261004')
        for name in ids:
            self.repo.configure_budget(name,tenant_id='sixnine',owner_id='supervan' if 'owner' in name else None,limit_microusd=1)
        with self.repo.transaction() as c:
            self.repo._reserve(c,scope,ids,'job','reference-254',2_000_000)
        self.assertTrue(all(self.repo.get_budget(name)['reserved_microusd']==2_000_000 for name in ids))
        with self.repo.transaction() as c:
            self.repo._settle(c,'job','reference-254',500_000)
        self.assertTrue(all(self.repo.get_budget(name)['reserved_microusd']==0 and self.repo.get_budget(name)['spent_microusd']==500_000 for name in ids))

    def test_names_do_not_disable_foreign_or_custom_limits(self):
        self.repo.configure_budget('demand-job-owner-supervan-20261004',tenant_id='foreign',owner_id='supervan',limit_microusd=1)
        row=self.repo.get_budget('demand-job-owner-supervan-20261004')
        self.assertFalse(fixed_cap_retired(row));self.assertEqual(effective_budget_limit(row),1)
        with self.assertRaises(BudgetExceeded):
            with self.repo.transaction() as c:self.repo._reserve(c,Scope('foreign','supervan','p'),[row['id']],'job','r',2)
        self.repo.configure_budget('custom',tenant_id='sixnine',owner_id='supervan',limit_microusd=1)
        with self.assertRaises(BudgetExceeded):
            with self.repo.transaction() as c:self.repo._reserve(c,Scope('sixnine','supervan','p'),['custom'],'job','r2',2)

    def test_wrong_owner_or_project_cannot_reuse_retired_seed_identity(self):
        for owner,project in [('superdan',None),('supervan','foreign')]:
            row={'id':'demand-job-owner-supervan-20261004','tenant_id':'sixnine','owner_id':owner,'project_id':project,'limit_microusd':5}
            self.assertFalse(fixed_cap_retired(row));self.assertEqual(effective_budget_limit(row),5)


if __name__=='__main__':unittest.main()
