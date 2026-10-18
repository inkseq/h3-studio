"""Real isolated ledger transitions; no provider calls or runtime credentials."""

import copy
import uuid
from unittest.mock import Mock, patch

from sqlalchemy import insert, select, update

from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    attempts,
    budget_accounts,
    budget_reservations,
    instance_intents,
    jobs,
    outbox,
    registered_workers,
    request_hash,
    scaler_receipts,
)
from studio_platform.operator_capacity import operator_commands, operator_nodes
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
                row = self.queue.complete(
                    claim.lease, [VALID_ARTIFACT], actual_cost_microusd=None
                )
        else:
            row = self.queue.complete(
                claim.lease, [VALID_ARTIFACT], actual_cost_microusd=None
            )
        return row

    def request(self, row, actual=None):
        check = inspect_reference(self.repo, self.scope.tenant_id, 'job', row['id'])
        return {
            'version': 1,
            'operation_id': str(uuid.uuid4()),
            'tenant_id': self.scope.tenant_id,
            'actor': 'reviewed-operator',
            'source': 'https://github.com/inkseq/h3-studio/issues/254',
            'evidence_sha256': 'b' * 64,
            'excluded_provider_instance_ids': ['56330848'],
            'entries': [
                {
                    'reference_type': 'job',
                    'reference_id': row['id'],
                    'expected_sha256': check['expected_sha256'],
                    'actual_cost_microusd': actual,
                    'provider_absence': None,
                }
            ],
        }

    def test_terminal_releases_hold_without_forgiving_invoice_or_settings_exposure(
        self,
    ):
        row = self.terminal()
        budget = self.repo.get_budget('owner-budget')
        self.assertEqual(
            (budget['reserved_microusd'], budget['spent_microusd']), (0, 0)
        )
        self.assertEqual(row['result']['billing_status'], 'pending')
        self.assertIsNone(row['result']['actual_cost_microusd'])
        with self.repo.engine.connect() as conn:
            r = conn.execute(select(budget_reservations)).mappings().one()
            self.assertEqual(
                (r['state'], r['amount_microusd'], r['actual_cost_microusd']),
                ('invoice_pending', 100000, None),
            )
            self.assertEqual(
                daily_projection(
                    conn, self.scope.tenant_id, self.scope.owner_id, self.now
                ),
                100000,
            )
        projection = self.repo.list_budget_projections(
            self.scope.tenant_id, owner_id=self.scope.owner_id
        )['budgets'][0]
        self.assertEqual(
            projection['invoice_pending'],
            {
                'reservation_count': 1,
                'reserved_estimate_microusd': 100000,
                'actual_cost_microusd': None,
                'included_in_settings_exposure': True,
            },
        )
        self.assertIsInstance(projection['remaining_microusd'], int)
        from studio_platform.diagnostics import operator_snapshot

        diagnostic = operator_snapshot(self.repo, tenant_id=self.scope.tenant_id)
        self.assertEqual(diagnostic['budget_counters']['pending_reservations'], 1)
        self.repo.settle_completed_job(
            self.scope, row['id'], actual_cost_microusd=80000
        )
        self.repo.settle_completed_job(
            self.scope, row['id'], actual_cost_microusd=80000
        )
        self.assertEqual(self.repo.get_budget('owner-budget')['spent_microusd'], 80000)
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)
        with self.assertRaises(Conflict):
            self.repo.settle_completed_job(
                self.scope, row['id'], actual_cost_microusd=90000
            )

    def test_dryrun_exact_audited_replay_and_later_invoice(self):
        row = self.terminal(historical=True)
        request = self.request(row)
        self.assertEqual(reconcile(self.repo, request)['state'], 'dry_run')
        self.assertEqual(
            self.repo.get_budget('owner-budget')['reserved_microusd'], 100000
        )
        receipt = reconcile(self.repo, request, apply=True)
        self.assertEqual(reconcile(self.repo, request, apply=True), receipt)
        self.assertEqual(receipt['entries'][0]['billing_status'], 'pending')
        with self.repo.engine.connect() as conn:
            events = list(
                conn.execute(
                    select(outbox).where(
                        outbox.c.event_type == 'budget.recovery_completed'
                    )
                ).mappings()
            )
            self.assertEqual(len(events), 1)
        tampered = copy.deepcopy(request)
        tampered['entries'][0]['actual_cost_microusd'] = 0
        with self.assertRaisesRegex(Conflict, 'operation_conflict'):
            reconcile(self.repo, tampered, apply=True)
        invoice = self.request(self.repo.get_job(self.scope, row['id']), 60000)
        reconcile(self.repo, invoice, apply=True)
        self.assertEqual(self.repo.get_budget('owner-budget')['spent_microusd'], 60000)

    def retried_terminal(self, status, marker=None, historical=False):
        self.job()
        first = self.queue.claim("old-worker", "test-pool", lease_seconds=10)
        if status == "lease_expired":
            self.now += 11
            self.assertEqual(self.queue.recover_expired()[0]["status"], "queued")
        else:
            self.queue.defer_unsubmitted(first.lease, retry_after_s=0)
        second = self.queue.claim("new-worker", "test-pool")
        self.assertEqual(second.job["attempt_no"], 2)
        if marker:
            with self.repo.transaction() as conn:
                conn.execute(
                    update(attempts)
                    .where(attempts.c.id == first.lease.attempt_id)
                    .values(
                        **{
                            marker: self.now
                            if marker == "submission_started_at"
                            else "unknown-task"
                        }
                    )
                )
        self.queue.begin_submission(second.lease)
        self.queue.record_submitted(second.lease, "retried-task")
        self.queue.begin_collection(second.lease)
        if historical:
            with patch('studio_platform.settlement_recovery.release_terminal_job'):
                row = self.queue.complete(
                    second.lease, [VALID_ARTIFACT], actual_cost_microusd=None
                )
        else:
            row = self.queue.complete(
                second.lease, [VALID_ARTIFACT], actual_cost_microusd=None
            )
        with self.repo.engine.connect() as conn:
            old = (
                conn.execute(
                    select(attempts).where(attempts.c.id == first.lease.attempt_id)
                )
                .mappings()
                .one()
            )
        self.assertEqual((old['status'], old['upstream_stopped']), (status, 0))
        return row

    def test_unsubmitted_retry_histories_release_without_rewriting_history(self):
        for status in ('lease_expired', 'deferred'):
            with self.subTest(status=status):
                row = self.retried_terminal(status)
                self.assertEqual(
                    self.repo.get_budget('owner-budget')['reserved_microusd'], 0
                )
                self.assertIsNone(row['result']['actual_cost_microusd'])
                # The separately audited historical path uses the same closure rule.
                historical = self.retried_terminal(status, historical=True)
                reconcile(self.repo, self.request(historical), apply=True)
                self.assertEqual(
                    self.repo.get_budget('owner-budget')['reserved_microusd'], 0
                )

    def test_retry_history_with_either_submission_marker_keeps_original_hold(self):
        expected = 0
        for status in ('lease_expired', 'deferred'):
            for marker in ('submission_started_at', 'upstream_task_id'):
                with self.subTest(status=status, marker=marker):
                    row = self.retried_terminal(status, marker)
                    expected += 100000
                    self.assertEqual(
                        self.repo.get_budget('owner-budget')['reserved_microusd'],
                        expected,
                    )
                    with self.assertRaisesRegex(Conflict, 'upstream_unresolved'):
                        reconcile(self.repo, self.request(row), apply=True)

    def test_changed_snapshot_or_uncertain_attempt_refuses_entire_batch(self):
        row = self.terminal(historical=True)
        request = self.request(row)
        with self.repo.transaction() as conn:
            conn.execute(
                update(attempts)
                .where(attempts.c.job_id == row['id'])
                .values(upstream_stopped=0)
            )
        with self.assertRaisesRegex(Conflict, 'snapshot_changed'):
            reconcile(self.repo, request, apply=True)
        fresh = self.request(row)
        with self.assertRaisesRegex(Conflict, 'upstream_unresolved'):
            reconcile(self.repo, fresh, apply=True)
        self.assertEqual(
            self.repo.get_budget('owner-budget')['reserved_microusd'], 100000
        )

    def test_second_changed_reference_rolls_back_first_and_audit(self):
        first, second = self.terminal(historical=True), self.terminal(historical=True)
        request = self.request(first, 25000)
        request['entries'] += self.request(second)['entries']
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == second['id'])
                .values(updated_at=self.now + 1)
            )
        with self.assertRaisesRegex(Conflict, 'snapshot_changed'):
            reconcile(self.repo, request, apply=True)
        budget = self.repo.get_budget('owner-budget')
        self.assertEqual(
            (budget['reserved_microusd'], budget['spent_microusd']), (200000, 0)
        )
        with self.repo.engine.connect() as conn:
            self.assertIsNone(
                conn.execute(
                    select(outbox).where(
                        outbox.c.event_type == 'budget.recovery_completed'
                    )
                ).first()
            )

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
                conn.execute(
                    update(instance_intents)
                    .where(instance_intents.c.id == node)
                    .values(state='destroyed', updated_at=fixture.now)
                )
                conn.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node)
                    .values(payload=payload)
                )
                before = projected_cost(conn, fixture_module.GUARD, fixture.now)
                release_pending_invoice(fixture.repo, conn, 'instance', node)
                after = projected_cost(conn, fixture_module.GUARD, fixture.now)
                self.assertEqual(after, before)
                self.assertGreaterEqual(
                    after,
                    payload['hourly_cost_microusd'] * 4
                    + payload['extra_reservation_microusd'],
                )
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

    def test_terminal_only_is_not_absence_then_exact_absence_closes_without_zero_invoice(
        self,
    ):
        node = self.start()['node_id']
        self.running(node)
        binding = self.store.load(node)
        self.client.runs[binding['run_name']]['status'] = 'terminated'
        self.capacity.observe(node)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'], 'destroying')
        self.client.provider_inventory = lambda backend: {
            'backend': backend,
            'complete': True,
            'instance_ids': ['56330848'],
            'observed_at': self.now,
        }
        self.capacity.observe_terminal(node)
        row = self.repo.list_instance_intents()[0]
        self.assertEqual(
            (row['state'], row['billing_status'], row['actual_cost_microusd']),
            ('destroyed', 'pending', None),
        )
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)
        self.capacity.observe_terminal(node)
        self.repo.settle_instance_cost(node, actual_cost_microusd=12345)
        self.repo.settle_instance_cost(node, actual_cost_microusd=12345)
        self.assertEqual(self.repo.get_budget('owner-budget')['spent_microusd'], 12345)

    def test_present_partial_stale_inventory_never_closes(self):
        node = self.start()['node_id']
        self.running(node)
        binding = self.store.load(node)
        self.client.runs[binding['run_name']]['status'] = 'terminated'
        for change in (
            {'instance_ids': ['1234567']},
            {'complete': False},
            {'observed_at': self.now - 31},
            {'backend': 'runpod'},
        ):
            self.client.provider_inventory = lambda backend, change=change: {
                'backend': backend,
                'complete': True,
                'instance_ids': [],
                'observed_at': self.now,
                **change,
            }
            self.capacity.observe(node)
            self.assertEqual(
                self.repo.list_instance_intents()[0]['state'], 'destroying'
            )
        self.assertGreater(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)

    def test_recovery_exclusion_and_stale_proof_leave_live_instance_untouched(self):
        from studio_platform.repository import instance_intents

        node = self.start()['node_id']
        self.running(node)
        with self.repo.transaction() as conn:
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node)
                .values(state='destroying')
            )
        check = inspect_reference(self.repo, self.scope.tenant_id, 'instance', node)
        request = {
            'version': 1,
            'operation_id': str(uuid.uuid4()),
            'tenant_id': self.scope.tenant_id,
            'actor': 'operator',
            'source': 'https://github.com/inkseq/h3-studio/issues/254',
            'evidence_sha256': 'b' * 64,
            'excluded_provider_instance_ids': ['1234567'],
            'entries': [
                {
                    'reference_type': 'instance',
                    'reference_id': node,
                    'expected_sha256': check['expected_sha256'],
                    'actual_cost_microusd': None,
                    'provider_absence': {
                        'provider': 'vast',
                        'provider_instance_id': '1234567',
                        'complete': True,
                        'instance_ids': ['56330848'],
                        'observed_at': self.now,
                        'evidence_sha256': 'c' * 64,
                    },
                }
            ],
        }
        with self.assertRaisesRegex(Conflict, 'protected_instance'):
            reconcile(self.repo, request, apply=True)
        request['excluded_provider_instance_ids'] = ['56330848']
        request['entries'][0]['provider_absence']['observed_at'] = self.now - 121
        with self.assertRaisesRegex(Conflict, 'absence_unconfirmed'):
            reconcile(self.repo, request, apply=True)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'], 'destroying')
        self.assertGreater(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)


class ReviewedHistoricalRemovalTests(LedgerCase):
    """Explicit recovery evidence is not a public observer or an invoice."""

    _setup_dstack = dstack_fixtures.DstackOperatorTests._setup_dstack
    preview = dstack_fixtures.DstackOperatorTests.preview
    start = dstack_fixtures.DstackOperatorTests.start

    def setUp(self):
        super().setUp()
        self._setup_dstack()

    def ended(self, key='start-1', *, legacy=None, state='creation_unknown'):
        identity = self.start(key)['node_id']
        with self.repo.transaction() as conn:
            node = dict(
                conn.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == identity)
                )
                .mappings()
                .one()
            )
            values = dict(state=state)
            if legacy:
                values.update(
                    provider=legacy,
                    provider_instance_id='wrk-ended' if legacy == 'targon' else None,
                )
                review = {
                    'schema_version': 1,
                    'state': 'manually_reviewed',
                    'intent_id': identity,
                    'instance_id': values['provider_instance_id'],
                    'provider': legacy,
                    'original_intent_state': state,
                    'actor': 'reviewed-owner',
                    'operation_id': str(uuid.uuid4()),
                    'account_absent': True,
                    'no_continuing_charge': True,
                }
                conn.execute(
                    insert(scaler_receipts).values(
                        id=str(uuid.uuid4()),
                        intent_id=identity,
                        operation='manual_review',
                        observed_at=self.now,
                        facts=review,
                    )
                )
                conn.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == identity)
                    .values(
                        payload={'manual_review': review},
                        desired_state='stopped',
                        runtime_state='manually_reviewed',
                    )
                )
                conn.execute(
                    update(operator_commands)
                    .where(operator_commands.c.id == node['command_id'])
                    .values(kind='start')
                )
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == identity)
                .values(**values)
            )
            conn.execute(
                update(operator_commands)
                .where(operator_commands.c.id == node['command_id'])
                .values(state='running')
            )
        return identity

    def reviewed_request(self, *identities):
        entries, accounts = [], {}
        for identity in identities:
            check = inspect_reference(
                self.repo, self.scope.tenant_id, 'instance', identity, version=2
            )
            with self.repo.engine.connect() as conn:
                row = (
                    conn.execute(
                        select(instance_intents).where(
                            instance_intents.c.id == identity
                        )
                    )
                    .mappings()
                    .one()
                )
                node = (
                    conn.execute(
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == identity
                        )
                    )
                    .mappings()
                    .one()
                )
            accounts[row['provider']] = 'a' * 64
            binding = node['payload'].get('dstack')
            run = (
                {
                    'run_id': binding['run_id'],
                    'run_name': binding['run_name'],
                    'binding_sha256': request_hash(binding),
                    'original_binding_matched': True,
                    'status': 'failed',
                    'observed_at': self.now,
                    'evidence_sha256': 'd' * 64,
                }
                if binding
                else None
            )
            entries.append(
                {
                    'reference_type': 'instance',
                    'reference_id': identity,
                    'expected_sha256': check['expected_sha256'],
                    'actual_cost_microusd': None,
                    'provider_absence': {
                        'kind': 'reviewed_account_empty',
                        'provider': row['provider'],
                        'provider_instance_id': row['provider_instance_id'],
                        'account_binding_sha256': 'a' * 64,
                        'complete': True,
                        'instance_ids': [],
                        'observed_at': self.now,
                        'evidence_sha256': 'c' * 64,
                        'owned_run': run,
                    },
                }
            )
        return {
            'version': 2,
            'operation_id': str(uuid.uuid4()),
            'tenant_id': self.scope.tenant_id,
            'actor': 'reviewed-operator',
            'source': 'https://github.com/inkseq/h3-studio/issues/264',
            'evidence_sha256': 'b' * 64,
            'excluded_provider_instance_ids': ['56330848', '56336890'],
            'reviewed_provider_accounts': accounts,
            'entries': entries,
        }

    def test_identityless_review_fences_original_command_without_inventing_invoice_and_replays_once(
        self,
    ):
        identity = self.ended()
        previous = {
            'operation_id': str(uuid.uuid4()),
            'state': 'applied',
            'request_hash': '9' * 64,
            'entries': [
                {
                    'reference_type': 'job',
                    'reference_id': str(uuid.uuid4()),
                    'billing_status': 'pending',
                }
            ],
        }
        with self.repo.transaction() as conn:
            self.repo._emit(
                conn, 'budget.recovery_completed', previous['operation_id'], previous
            )
        request = self.reviewed_request(identity)
        original = self.repo.get_budget('owner-budget')['reserved_microusd']
        self.assertGreater(original, 0)
        self.assertEqual(reconcile(self.repo, request)['state'], 'dry_run')
        self.assertEqual(
            self.repo.get_budget('owner-budget')['reserved_microusd'], original
        )
        result = reconcile(self.repo, request, apply=True)
        self.now += 121  # An exact completed replay needs no new provider authority.
        self.assertEqual(reconcile(self.repo, request, apply=True), result)
        with self.repo.engine.connect() as conn:
            intent = conn.execute(select(instance_intents)).mappings().one()
            node = conn.execute(select(operator_nodes)).mappings().one()
            command = conn.execute(select(operator_commands)).mappings().one()
            reservation = conn.execute(select(budget_reservations)).mappings().one()
            self.assertEqual(
                (
                    intent['state'],
                    node['desired_state'],
                    node['runtime_state'],
                    command['state'],
                ),
                ('destroyed', 'stopped', 'stopped', 'completed'),
            )
            self.assertEqual(
                (
                    reservation['state'],
                    reservation['actual_cost_microusd'],
                    reservation['amount_microusd'],
                ),
                ('invoice_pending', None, original),
            )
            self.assertEqual(
                daily_projection(
                    conn, self.scope.tenant_id, self.scope.owner_id, self.now
                ),
                original,
            )
            recovered = list(
                conn.execute(
                    select(outbox).where(
                        outbox.c.event_type == 'budget.recovery_completed'
                    )
                ).mappings()
            )
            self.assertEqual(len(recovered), 2)
            self.assertEqual(
                next(
                    r['payload']
                    for r in recovered
                    if r['aggregate_id'] == previous['operation_id']
                ),
                previous,
            )
        self.assertEqual(
            (
                self.repo.get_budget('owner-budget')['spent_microusd'],
                self.repo.get_budget('owner-budget')['reserved_microusd'],
            ),
            (0, 0),
        )
        changed = copy.deepcopy(request)
        changed['entries'][0]['actual_cost_microusd'] = 0
        with self.assertRaises(Conflict):
            reconcile(self.repo, changed, apply=True)

    def test_legacy_tenant_fallback_requires_original_manual_receipt_and_uses_legal_draining_transition(
        self,
    ):
        identity = self.ended(legacy='targon', state='draining')
        with self.assertRaisesRegex(Conflict, 'node_scope_mismatch'):
            inspect_reference(self.repo, self.scope.tenant_id, 'instance', identity)
        request = self.reviewed_request(identity)
        reconcile(self.repo, request, apply=True)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'], 'destroyed')
        self.assertIsNone(self.repo.list_instance_intents()[0]['actual_cost_microusd'])

    def test_identityless_legacy_needs_existing_review_and_all_accounts_same_tenant(
        self,
    ):
        identity = self.ended(legacy='lium')
        self.reviewed_request(identity)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_accounts).values(tenant_id='foreign'))
        with self.assertRaisesRegex(Conflict, 'tenant_mismatch'):
            self.reviewed_request(identity)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_accounts).values(tenant_id=self.scope.tenant_id))
            conn.execute(update(operator_nodes).values(payload={}))
            conn.execute(scaler_receipts.delete())
        with self.assertRaisesRegex(Conflict, 'node_scope_mismatch'):
            self.reviewed_request(identity)

    def test_incomplete_nonempty_stale_wrong_account_or_nonterminal_run_never_releases(
        self,
    ):
        identity = self.ended()
        request = self.reviewed_request(identity)
        for changes in (
            {'complete': False},
            {'instance_ids': ['live']},
            {'observed_at': self.now - 121},
            {'account_binding_sha256': 'f' * 64},
            {'provider': 'targon'},
            {
                'owned_run': {
                    **request['entries'][0]['provider_absence']['owned_run'],
                    'status': 'running',
                }
            },
            {
                'owned_run': {
                    **request['entries'][0]['provider_absence']['owned_run'],
                    'run_id': str(uuid.uuid4()),
                }
            },
            {
                'owned_run': {
                    **request['entries'][0]['provider_absence']['owned_run'],
                    'binding_sha256': 'f' * 64,
                }
            },
        ):
            with self.subTest(changes=changes):
                invalid = copy.deepcopy(request)
                invalid['entries'][0]['provider_absence'].update(changes)
                with self.assertRaises(Conflict):
                    reconcile(self.repo, invalid, apply=True)
        invalid = copy.deepcopy(request)
        invalid['reviewed_provider_accounts'] = {'targon': 'a' * 64}
        invalid['entries'][0]['provider_absence']['account_binding_sha256'] = None
        with self.assertRaisesRegex(Conflict, 'absence_unconfirmed'):
            reconcile(self.repo, invalid, apply=True)
        self.assertGreater(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)

    def test_original_start_command_cas_and_other_live_sibling_block_batch(self):
        first, second = self.ended(), self.ended('start-2')
        request = self.reviewed_request(first)
        with self.repo.transaction() as conn:
            node = (
                conn.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == first)
                )
                .mappings()
                .one()
            )
            conn.execute(
                update(operator_commands)
                .where(operator_commands.c.id == node['command_id'])
                .values(reason_code='changed')
            )
        with self.assertRaisesRegex(Conflict, 'snapshot_changed'):
            reconcile(self.repo, request, apply=True)
        with self.repo.transaction() as conn:
            conn.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == second)
                .values(command_id=node['command_id'], ordinal=1)
            )
        with self.assertRaisesRegex(Conflict, 'sibling_unresolved'):
            reconcile(self.repo, self.reviewed_request(first), apply=True)
        reconcile(self.repo, self.reviewed_request(first, second), apply=True)
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)

    def test_protected_identity_and_changed_account_refuse(self):
        identity = self.ended()
        with self.repo.transaction() as conn:
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == identity)
                .values(provider_instance_id='56336890')
            )
        with self.assertRaisesRegex(Conflict, 'protected_instance'):
            reconcile(self.repo, self.reviewed_request(identity), apply=True)
        with self.repo.transaction() as conn:
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == identity)
                .values(provider_instance_id=None)
            )
        request = self.reviewed_request(identity)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_accounts).values(spent_microusd=1))
        with self.assertRaisesRegex(Conflict, 'snapshot_changed'):
            reconcile(self.repo, request, apply=True)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_accounts).values(reserved_microusd=0))
        with self.assertRaisesRegex(Conflict, 'counter_underflow'):
            reconcile(self.repo, self.reviewed_request(identity), apply=True)

    def test_live_worker_and_historical_unstopped_attempt_are_not_removal_authority(
        self,
    ):
        identity = self.ended(legacy='targon', state='destroying')
        job = self.job()
        with self.repo.transaction() as conn:
            conn.execute(
                insert(registered_workers).values(
                    id='busy-worker',
                    pool='test-pool',
                    provider='targon',
                    instance_id='wrk-ended',
                    spec={},
                    spec_hash='e' * 64,
                    state='busy',
                    current_job_id=job['id'],
                    drain_requested=1,
                    fence=1,
                    expires_at=self.now + 60,
                    updated_at=self.now,
                )
            )
        with self.assertRaisesRegex(Conflict, 'worker_busy'):
            reconcile(self.repo, self.reviewed_request(identity), apply=True)
        queue = TaskQueue(self.repo)
        claim = queue.claim('busy-worker', 'test-pool')
        queue.begin_submission(claim.lease)
        with self.repo.transaction() as conn:
            conn.execute(
                update(registered_workers).values(state='retired', current_job_id=None)
            )
        with self.assertRaisesRegex(Conflict, 'upstream_unresolved'):
            reconcile(self.repo, self.reviewed_request(identity), apply=True)
        with self.repo.transaction() as conn:
            conn.execute(
                update(attempts).values(status='cancelled', upstream_stopped=1)
            )
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job['id'])
                .values(status='queued', lease_worker_id=None, lease_expires_at=None)
            )
        with self.assertRaisesRegex(Conflict, 'worker_busy'):
            reconcile(self.repo, self.reviewed_request(identity), apply=True)

    def test_only_audited_ended_legacy_fence_skips_provider_observation(self):
        from studio_platform.operator_controller import OperatorController

        identity = self.ended(legacy='targon', state='destroying')
        with self.repo.engine.connect() as conn:
            stale_node = dict(
                conn.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == identity)
                )
                .mappings()
                .one()
            )
        reconcile(self.repo, self.reviewed_request(identity), apply=True)
        controller = object.__new__(OperatorController)
        controller.repo, controller.service = self.repo, Mock()
        controller.service.registry.get.side_effect = RuntimeError(
            'original observation reached'
        )
        with self.repo.engine.connect() as conn:
            node = dict(
                conn.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == identity)
                )
                .mappings()
                .one()
            )
        self.assertEqual(controller._observe(node), 'stopped')
        self.assertEqual(controller._observe(stale_node), 'stopped')
        controller.service.registry.get.assert_not_called()
        for changes in (
            {'operation_id': str(uuid.uuid4())},
            {'intent_id': str(uuid.uuid4())},
        ):
            altered = {
                **node,
                'payload': {
                    **node['payload'],
                    'settlement_recovery': {
                        **node['payload']['settlement_recovery'],
                        **changes,
                    },
                },
            }
            with self.repo.transaction() as conn:
                conn.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == identity)
                    .values(payload=altered['payload'])
                )
            with self.assertRaisesRegex(RuntimeError, 'original observation reached'):
                controller._observe(altered)
        with self.repo.transaction() as conn:
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == identity)
                .values(state='creation_unknown')
            )
        with self.assertRaisesRegex(RuntimeError, 'original observation reached'):
            controller._observe(node)

    def test_evidence_that_expires_while_waiting_for_account_lock_refuses_without_writes(
        self,
    ):
        identity = self.ended()
        request, lock = self.reviewed_request(identity), self.repo._locked

        def slow_lock(conn, statement):
            result = lock(conn, statement)
            if getattr(statement, 'get_final_froms', lambda: [])() == [budget_accounts]:
                self.now += 121
            return result

        with patch.object(self.repo, '_locked', side_effect=slow_lock):
            with self.assertRaisesRegex(Conflict, 'absence_unconfirmed'):
                reconcile(self.repo, request, apply=True)
        self.assertGreater(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)

    def test_late_dstack_observation_cannot_zero_invoice_or_revive_reviewed_history(
        self,
    ):
        from studio_platform.dstack_controller import DstackController

        identity = self.ended()
        with self.repo.engine.connect() as conn:
            stale_node = dict(
                conn.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == identity)
                )
                .mappings()
                .one()
            )
        reconcile(self.repo, self.reviewed_request(identity), apply=True)
        # Even a late native rejection observation must not forgive this
        # deliberately retained unknown invoice after the reviewed operation.
        self.store.record(
            identity,
            {
                'state': 'stopped',
                'ready': False,
                'observed_at': self.now,
                'dstack_status': 'failed',
                'no_allocation_proof': {'late': 'must not be interpreted'},
            },
        )
        self.store.record(
            identity,
            {'state': 'creation_unknown', 'ready': False, 'observed_at': self.now},
        )
        with self.repo.engine.connect() as conn:
            reservation = conn.execute(select(budget_reservations)).mappings().one()
            node = conn.execute(select(operator_nodes)).mappings().one()
            self.assertEqual(
                (reservation['state'], reservation['actual_cost_microusd']),
                ('invoice_pending', None),
            )
            self.assertEqual(node['runtime_state'], 'stopped')
        controller = object.__new__(DstackController)
        controller.repo, controller.capacity = self.repo, Mock()
        self.assertEqual(
            controller._tick_node(stale_node), {'node_id': identity, 'state': 'stopped'}
        )
        controller.capacity.store.load.assert_not_called()

    def test_delayed_dstack_start_callback_preserves_completed_original_command(self):
        from studio_platform.dstack_capacity import DstackError

        original_start = self.capacity.start
        for lost in (False, True):

            def complete_during_http(request, pub):
                observation = original_start(request, pub)
                with self.repo.transaction() as conn:
                    conn.execute(
                        update(instance_intents)
                        .where(instance_intents.c.id == request.intent_id)
                        .values(state='creation_unknown')
                    )
                reconcile(
                    self.repo, self.reviewed_request(request.intent_id), apply=True
                )
                if lost:
                    raise DstackError('dstack_api_unavailable')
                return observation

            with patch.object(self.capacity, 'start', side_effect=complete_during_http):
                result = self.start('delayed-' + str(lost))
            self.assertEqual(result['state'], 'completed')
            with self.repo.engine.connect() as conn:
                command = (
                    conn.execute(
                        select(operator_commands).where(
                            operator_commands.c.id == result['command_id']
                        )
                    )
                    .mappings()
                    .one()
                )
                reservation = (
                    conn.execute(
                        select(budget_reservations).where(
                            budget_reservations.c.reference_id == result['node_id']
                        )
                    )
                    .mappings()
                    .one()
                )
            self.assertEqual(command['state'], 'completed')
            self.assertEqual(
                (reservation['state'], reservation['actual_cost_microusd']),
                ('invoice_pending', None),
            )

    def test_legacy_observation_result_and_failure_cannot_overwrite_a_recovery_committed_during_io(
        self,
    ):
        from studio_platform.operator_controller import OperatorController

        for failure in (False, True):
            identity = self.ended(
                'legacy-race-' + str(failure), legacy='targon', state='destroying'
            )
            request = self.reviewed_request(identity)
            with self.repo.engine.connect() as conn:
                node = dict(
                    conn.execute(
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == identity
                        )
                    )
                    .mappings()
                    .one()
                )
            controller = object.__new__(OperatorController)
            controller.repo, controller.service = self.repo, Mock()
            controller.leader_id, controller.boots = 'test-review-race', {}
            controller._policy = Mock(return_value=None)
            controller.service.registry.get.return_value.fingerprint = node[
                'binding_hash'
            ]
            coordinator = Mock()

            def callback(*args, **kwargs):
                reconcile(self.repo, request, apply=True)
                return {'state': 'observed'}

            coordinator.observe_manual_instance.side_effect = callback
            controller._coordinator = Mock(return_value=coordinator)
            if failure:
                controller.enabled, controller.shutting_down = True, False
                controller.inventory_refresh = controller.status_writer = None
                controller._start = Mock(return_value='waiting')
                controller._summarize_commands = Mock()

                def failed_observation(cached):
                    reconcile(self.repo, request, apply=True)
                    raise RuntimeError('late provider failure')

                controller._observe = Mock(side_effect=failed_observation)
                self.assertEqual(controller.tick()['errors'], 0)
            else:
                # Simulate entry preceding the earlier manual review, with the
                # private recovery committing inside the original provider I/O.
                with patch(
                    'studio_platform.operator_controller.manually_reviewed_inactive',
                    return_value=None,
                ):
                    self.assertEqual(controller._observe(node), 'stopped')
            with self.repo.engine.connect() as conn:
                current = (
                    conn.execute(
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == identity
                        )
                    )
                    .mappings()
                    .one()
                )
                reservation = (
                    conn.execute(
                        select(budget_reservations).where(
                            budget_reservations.c.reference_id == identity
                        )
                    )
                    .mappings()
                    .one()
                )
            self.assertEqual(current['runtime_state'], 'stopped')
            self.assertEqual(
                (reservation['state'], reservation['actual_cost_microusd']),
                ('invoice_pending', None),
            )
