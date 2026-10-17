"""Real isolated ledger transitions; no provider calls or runtime credentials."""
import copy
import uuid
from unittest.mock import patch

from sqlalchemy import select, update

from studio_platform.queue import TaskQueue
from studio_platform.repository import Conflict, attempts, budget_reservations, jobs, outbox
from studio_platform.settlement_recovery import inspect_reference, reconcile
from studio_platform.spending_limits import daily_projection
from test_platform_repository import LedgerCase
from test_platform_queue import VALID_ARTIFACT
import test_platform_queue as queue_fixtures
import test_dstack_operator as dstack_fixtures


class SettlementTests(LedgerCase):
    running = queue_fixtures.QueueTests.running

    def setUp(self):
        super().setUp()
        self.queue = TaskQueue(self.repo)

    def terminal(self, *, historical=False):
        claim = self.running()
        self.queue.begin_collection(claim.lease)
        if historical:
            with patch('studio_platform.settlement_recovery.release_terminal_job'):
                row = self.queue.complete(claim.lease, [VALID_ARTIFACT], actual_cost_microusd=None)
        else:
            row = self.queue.complete(claim.lease, [VALID_ARTIFACT], actual_cost_microusd=None)
        return row

    def request(self, row, actual=None):
        check = inspect_reference(self.repo, self.scope.tenant_id, 'job', row['id'])
        return {'version': 1, 'operation_id': str(uuid.uuid4()), 'tenant_id': self.scope.tenant_id,
            'actor': 'reviewed-operator', 'source': 'https://github.com/inkseq/h3-studio/issues/254',
            'evidence_sha256': 'b'*64, 'excluded_provider_instance_ids': ['56330848'], 'entries': [{'reference_type': 'job', 'reference_id': row['id'],
            'expected_sha256': check['expected_sha256'], 'actual_cost_microusd': actual, 'provider_absence': None}]}

    def test_terminal_releases_hold_without_forgiving_invoice_or_settings_exposure(self):
        row = self.terminal()
        budget = self.repo.get_budget('owner-budget')
        self.assertEqual((budget['reserved_microusd'], budget['spent_microusd']), (0, 0))
        self.assertEqual(row['result']['billing_status'], 'pending')
        self.assertIsNone(row['result']['actual_cost_microusd'])
        with self.repo.engine.connect() as conn:
            r = conn.execute(select(budget_reservations)).mappings().one()
            self.assertEqual((r['state'], r['amount_microusd'], r['actual_cost_microusd']), ('invoice_pending', 100000, None))
            self.assertEqual(daily_projection(conn, self.scope.tenant_id, self.scope.owner_id, self.now), 100000)
        projection = self.repo.list_budget_projections(self.scope.tenant_id, owner_id=self.scope.owner_id)['budgets'][0]
        self.assertEqual(projection['invoice_pending'], {'reservation_count': 1,
            'reserved_estimate_microusd': 100000, 'actual_cost_microusd': None,
            'included_in_settings_exposure': True})
        self.assertIsInstance(projection['remaining_microusd'], int)
        from studio_platform.diagnostics import operator_snapshot
        diagnostic = operator_snapshot(self.repo, tenant_id=self.scope.tenant_id)
        self.assertEqual(diagnostic['budget_counters']['pending_reservations'], 1)
        self.repo.settle_completed_job(self.scope, row['id'], actual_cost_microusd=80000)
        self.repo.settle_completed_job(self.scope, row['id'], actual_cost_microusd=80000)
        self.assertEqual(self.repo.get_budget('owner-budget')['spent_microusd'], 80000)
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)
        with self.assertRaises(Conflict):
            self.repo.settle_completed_job(self.scope, row['id'], actual_cost_microusd=90000)

    def test_dryrun_exact_audited_replay_and_later_invoice(self):
        row = self.terminal(historical=True)
        request = self.request(row)
        self.assertEqual(reconcile(self.repo, request)['state'], 'dry_run')
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 100000)
        receipt = reconcile(self.repo, request, apply=True)
        self.assertEqual(reconcile(self.repo, request, apply=True), receipt)
        self.assertEqual(receipt['entries'][0]['billing_status'], 'pending')
        with self.repo.engine.connect() as conn:
            events = list(conn.execute(select(outbox).where(outbox.c.event_type == 'budget.recovery_completed')).mappings())
            self.assertEqual(len(events), 1)
        tampered = copy.deepcopy(request); tampered['entries'][0]['actual_cost_microusd'] = 0
        with self.assertRaisesRegex(Conflict, 'operation_conflict'):
            reconcile(self.repo, tampered, apply=True)
        invoice = self.request(self.repo.get_job(self.scope, row['id']), 60000)
        reconcile(self.repo, invoice, apply=True)
        self.assertEqual(self.repo.get_budget('owner-budget')['spent_microusd'], 60000)

    def test_changed_snapshot_or_uncertain_attempt_refuses_entire_batch(self):
        row = self.terminal(historical=True)
        request = self.request(row)
        with self.repo.transaction() as conn:
            conn.execute(update(attempts).where(attempts.c.job_id == row['id']).values(upstream_stopped=0))
        with self.assertRaisesRegex(Conflict, 'snapshot_changed'):
            reconcile(self.repo, request, apply=True)
        fresh = self.request(row)
        with self.assertRaisesRegex(Conflict, 'upstream_unresolved'):
            reconcile(self.repo, fresh, apply=True)
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 100000)

    def test_second_changed_reference_rolls_back_first_and_audit(self):
        first, second = self.terminal(historical=True), self.terminal(historical=True)
        request = self.request(first, 25000)
        request['entries'] += self.request(second)['entries']
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == second['id']).values(updated_at=self.now+1))
        with self.assertRaisesRegex(Conflict, 'snapshot_changed'):
            reconcile(self.repo, request, apply=True)
        budget = self.repo.get_budget('owner-budget')
        self.assertEqual((budget['reserved_microusd'], budget['spent_microusd']), (200000, 0))
        with self.repo.engine.connect() as conn:
            self.assertIsNone(conn.execute(select(outbox).where(outbox.c.event_type == 'budget.recovery_completed')).first())

    def test_staging_pending_invoice_remains_in_twenty_dollar_projection(self):
        import test_dstack_test_budget as fixture_module
        from studio_platform.dstack_test_budget import projected_cost
        from studio_platform.settlement_recovery import release_pending_invoice
        from studio_platform.repository import instance_intents
        fixture = fixture_module.DailyTestBudgetTests('runTest')
        fixture.setUp()
        try:
            node = fixture.start()['node_id']
            # A native run ended early, but provider removal was only proven
            # four hours later. Do not silently erase that possible VM bill.
            from studio_platform.operator_capacity import operator_nodes
            _, original_node = fixture.rows()
            payload = copy.deepcopy(original_node['payload'])
            payload['dstack']['first_stopped_at'] = fixture.now
            fixture.now += 14400
            with fixture.repo.transaction() as conn:
                conn.execute(update(instance_intents).where(instance_intents.c.id == node).values(state='destroyed', updated_at=fixture.now))
                conn.execute(update(operator_nodes).where(operator_nodes.c.intent_id == node).values(payload=payload))
                before = projected_cost(conn, fixture_module.GUARD, fixture.now)
                release_pending_invoice(fixture.repo, conn, 'instance', node)
                after = projected_cost(conn, fixture_module.GUARD, fixture.now)
                self.assertEqual(after, before)
                self.assertGreaterEqual(after, payload['hourly_cost_microusd']*4 + payload['extra_reservation_microusd'])
        finally:
            fixture.tearDown()


class RemovalTests(LedgerCase):
    _setup_dstack = dstack_fixtures.DstackOperatorTests._setup_dstack
    preview = dstack_fixtures.DstackOperatorTests.preview
    start = dstack_fixtures.DstackOperatorTests.start
    running = dstack_fixtures.DstackOperatorTests.running

    def setUp(self):
        super().setUp()
        self._setup_dstack()

    def test_terminal_only_is_not_absence_then_exact_absence_closes_without_zero_invoice(self):
        node = self.start()['node_id']; self.running(node)
        binding = self.store.load(node)
        self.client.runs[binding['run_name']]['status'] = 'terminated'
        self.capacity.observe(node)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'], 'destroying')
        self.client.provider_inventory = lambda backend: {'backend': backend, 'complete': True,
            'instance_ids': ['56330848'], 'observed_at': self.now}
        self.capacity.observe_terminal(node)
        row = self.repo.list_instance_intents()[0]
        self.assertEqual((row['state'], row['billing_status'], row['actual_cost_microusd']), ('destroyed', 'pending', None))
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)
        self.capacity.observe_terminal(node)
        self.repo.settle_instance_cost(node, actual_cost_microusd=12345)
        self.repo.settle_instance_cost(node, actual_cost_microusd=12345)
        self.assertEqual(self.repo.get_budget('owner-budget')['spent_microusd'], 12345)

    def test_present_partial_stale_inventory_never_closes(self):
        node = self.start()['node_id']; self.running(node)
        binding = self.store.load(node)
        self.client.runs[binding['run_name']]['status'] = 'terminated'
        for change in ({'instance_ids': ['1234567']}, {'complete': False}, {'observed_at': self.now-31}, {'backend':'runpod'}):
            self.client.provider_inventory = lambda backend, change=change: {'backend': backend,
                'complete': True, 'instance_ids': [], 'observed_at': self.now, **change}
            self.capacity.observe(node)
            self.assertEqual(self.repo.list_instance_intents()[0]['state'], 'destroying')
        self.assertGreater(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)

    def test_recovery_exclusion_and_stale_proof_leave_live_instance_untouched(self):
        from studio_platform.repository import instance_intents
        node = self.start()['node_id']; self.running(node)
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == node).values(state='destroying'))
        check = inspect_reference(self.repo, self.scope.tenant_id, 'instance', node)
        request = {'version':1,'operation_id':str(uuid.uuid4()),'tenant_id':self.scope.tenant_id,
            'actor':'operator','source':'https://github.com/inkseq/h3-studio/issues/254','evidence_sha256':'b'*64,
            'excluded_provider_instance_ids':['1234567'],'entries':[{'reference_type':'instance','reference_id':node,
                'expected_sha256':check['expected_sha256'],'actual_cost_microusd':None,'provider_absence':{
                    'provider':'vast','provider_instance_id':'1234567','complete':True,'instance_ids':['56330848'],
                    'observed_at':self.now,'evidence_sha256':'c'*64}}]}
        with self.assertRaisesRegex(Conflict,'protected_instance'):
            reconcile(self.repo,request,apply=True)
        request['excluded_provider_instance_ids']=['56330848']
        request['entries'][0]['provider_absence']['observed_at']=self.now-121
        with self.assertRaisesRegex(Conflict,'absence_unconfirmed'):
            reconcile(self.repo,request,apply=True)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'],'destroying')
        self.assertGreater(self.repo.get_budget('owner-budget')['reserved_microusd'],0)
