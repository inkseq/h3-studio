"""Snapshot batching preserves original projections and account scan scope."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
import uuid
from unittest.mock import Mock, patch

from sqlalchemy import event, insert, select, update

from studio_platform import dstack_test_budget
from studio_platform.dstack_test_budget import (
    projected_cost,
    projected_costs,
    task_guard,
)
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import BudgetExceeded, scaler_receipts
import test_dstack_test_budget as dstack_fixture
import test_modal_budget as modal_fixture


def account_scans(statements):
    return sum(
        'FROM platform_budget_reservations LEFT OUTER JOIN' in statement
        for statement in statements
    )


class DstackBudgetBatchTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing fixture without rediscovering its 28 test methods.
        self.case = dstack_fixture.DailyTestBudgetTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def guard(self, task='issue-267', margin=300):
        return {
            **task_guard(
                dstack_fixture.GUARD, task, limit=20_000_000, max_runtime=7200
            ),
            'stop_margin_seconds': margin,
        }

    def test_batch_preserves_input_order_duplicate_tasks_and_guard_margins(self):
        case = self.case
        node_id = case.start()['node_id']
        case.now += 3600
        guards = [
            self.guard(margin=600),
            self.guard(),
            self.guard(margin=600),
            self.guard('other'),
        ]
        with case.repo.transaction() as connection:
            before = case._projection_rows(connection)
            statements = []
            listener = lambda *args: statements.append(args[2])
            event.listen(connection, 'before_cursor_execute', listener)
            expected = [projected_cost(connection, guard, case.now) for guard in guards]
            self.assertEqual(account_scans(statements), 4)
            statements.clear()
            self.assertEqual(projected_costs(connection, guards, case.now), expected)
            self.assertEqual(expected, [1_875_000, 1_812_500, 1_875_000, 0])
            self.assertEqual(account_scans(statements), 1)
            event.remove(connection, 'before_cursor_execute', listener)
            self.assertEqual(case._projection_rows(connection), before)
            # The default admission path still reads its current rows and floor.
            self.assertEqual(
                projected_cost(
                    connection,
                    guards[1],
                    case.now,
                    replacement_id=node_id,
                    minimum_reserved=3_000_000,
                ),
                3_000_000,
            )
            case.repo._settle(connection, 'instance', node_id, 700_000)
            self.assertEqual(projected_cost(connection, guards[1], case.now), 700_000)

    def test_batch_keeps_removed_floor_extra_settled_actual_and_unproved_commitment(
        self,
    ):
        case = self.case
        with patch.object(
            case.service,
            'provider_inventory',
            side_effect=lambda backend: {
                'backend': backend,
                'complete': True,
                'observed_at': case.now,
                'instance_ids': [],
            },
        ):
            case._removed_projection_node(key='floor', cost=0, elapsed=3600)
            settled = case._removed_projection_node(key='settled', cost=0.2)
            unproved = case._removed_projection_node(
                key='unproved', task='other', cost=0
            )
        with case.repo.transaction() as connection:
            case.repo._settle(connection, 'instance', settled, 700_000)
            payload = copy.deepcopy(
                connection.execute(
                    select(operator_nodes.c.payload).where(
                        operator_nodes.c.intent_id == unproved
                    )
                ).scalar_one()
            )
            payload['dstack']['provider_removal_proof'] = None
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == unproved)
                .values(payload=payload)
            )
            before = case._projection_rows(connection)
            guards = [self.guard('other'), self.guard(), self.guard('absent')]
            expected = [projected_cost(connection, guard, case.now) for guard in guards]
            # A removed machine no longer holds its transport allowance (#1140).
            self.assertEqual(expected, [1_812_500, 1_450_000, 0])
            progress = []
            self.assertEqual(
                projected_costs(
                    connection, guards, case.now, _diagnostic_progress=progress.append
                ),
                expected,
            )
            self.assertEqual(progress, ['budget_rows', 'budget_projection'])
            self.assertEqual(case._projection_rows(connection), before)

    def test_batch_conflicting_attribution_fails_for_either_implicated_task(self):
        case = self.case
        case.start()
        _, node = case.rows()
        payload = copy.deepcopy(node['payload'])
        payload['request']['test_task_id'] = 'conflicting'
        with case.repo.transaction() as connection:
            connection.execute(update(operator_nodes).values(payload=payload))
            self.assertEqual(
                projected_costs(connection, [self.guard('unrelated')], case.now), [0]
            )
            for task in ('issue-267', 'conflicting'):
                with self.subTest(task=task):
                    with self.assertRaisesRegex(
                        BudgetExceeded, 'test_budget_unconfirmed'
                    ):
                        projected_cost(connection, self.guard(task), case.now)
                    with self.assertRaisesRegex(
                        BudgetExceeded, 'test_budget_unconfirmed'
                    ):
                        projected_costs(
                            connection,
                            [self.guard('unrelated'), self.guard(task)],
                            case.now,
                        )

    def test_progress_brackets_consumption_once_without_changing_queries_or_values(
        self,
    ):
        case = self.case
        case.start()
        guards = [self.guard(), self.guard('other'), self.guard()]
        original_rows = dstack_test_budget._reservation_rows
        original_projection = dstack_test_budget._projected_rows
        entries = []

        def rows(connection):
            entries.append('read_entered')
            for row in original_rows(connection):
                entries.append('row_consumed')
                yield row
            entries.append('read_complete')

        def project(*args, **kwargs):
            entries.append('guard_projected')
            return original_projection(*args, **kwargs)

        with case.repo.engine.connect() as connection:
            before = case._projection_rows(connection)
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                expected = projected_costs(connection, guards, case.now)
            self.assertEqual((output.getvalue(), errors.getvalue()), ('', ''))
            statements = []
            listener = lambda *args: statements.append(args[2])
            event.listen(connection, 'before_cursor_execute', listener)
            try:
                with (
                    patch.object(dstack_test_budget, '_reservation_rows', rows),
                    patch.object(dstack_test_budget, '_projected_rows', project),
                ):
                    observed = projected_costs(
                        connection,
                        guards,
                        case.now,
                        _diagnostic_progress=entries.append,
                    )
            finally:
                event.remove(connection, 'before_cursor_execute', listener)
            self.assertEqual(observed, expected)
            self.assertEqual(account_scans(statements), 1)
            self.assertEqual(case._projection_rows(connection), before)
        self.assertEqual(
            entries,
            [
                'budget_rows',
                'read_entered',
                'row_consumed',
                'read_complete',
                'budget_projection',
                'guard_projected',
                'guard_projected',
                'guard_projected',
            ],
        )

    def test_progress_callback_errors_cannot_change_accounting_or_business_error(self):
        case = self.case
        case.start()
        guards = [self.guard(), self.guard('other'), self.guard()]
        callback = Mock(side_effect=OSError('PRIVATE_DIAGNOSTIC_FAILURE'))
        with case.repo.engine.connect() as connection:
            before = case._projection_rows(connection)
            expected = projected_costs(connection, guards, case.now)
            self.assertEqual(
                projected_costs(
                    connection, guards, case.now, _diagnostic_progress=callback
                ),
                expected,
            )
            self.assertEqual(
                [call.args for call in callback.call_args_list],
                [('budget_rows',), ('budget_projection',)],
            )
            failure = BudgetExceeded('original_budget_failure')
            callback.reset_mock()
            with (
                patch.object(
                    dstack_test_budget, '_projected_rows', side_effect=failure
                ),
                self.assertRaises(BudgetExceeded) as caught,
            ):
                projected_costs(
                    connection, guards, case.now, _diagnostic_progress=callback
                )
            self.assertIs(caught.exception, failure)
            self.assertEqual(callback.call_count, 2)
            self.assertEqual(case._projection_rows(connection), before)


class ModalBudgetBatchTests(modal_fixture.ModalBudgetCase):
    def test_batch_scans_account_once_but_keeps_original_modal_binding_reads(self):
        self.reserve()
        self.now += 900
        guards = [
            dict(modal_fixture.GUARD, task_id='other'),
            dict(modal_fixture.GUARD, stop_margin_seconds=600),
            modal_fixture.GUARD,
        ]
        with self.repo.engine.connect() as connection:
            expected = [projected_cost(connection, guard, self.now) for guard in guards]
            statements = []
            listener = lambda *args: statements.append(args[2])
            event.listen(connection, 'before_cursor_execute', listener)
            self.assertEqual(projected_costs(connection, guards, self.now), expected)
            event.remove(connection, 'before_cursor_execute', listener)
        self.assertEqual(expected, [0, 1_333_334, 1_333_334])
        self.assertEqual(account_scans(statements), 1)
        self.assertEqual(
            sum('FROM platform_scaler_receipts' in sql for sql in statements), 3
        )

    def test_unrelated_missing_modal_binding_still_fails_closed(self):
        self.reserve(bind=False)
        guard = dict(modal_fixture.GUARD, task_id='unrelated')
        with self.repo.engine.connect() as connection:
            with self.assertRaisesRegex(
                BudgetExceeded, 'modal_test_budget_unconfirmed'
            ):
                projected_cost(connection, guard, self.now)
            with self.assertRaisesRegex(
                BudgetExceeded, 'modal_test_budget_unconfirmed'
            ):
                projected_costs(connection, [guard], self.now)

    def test_unrelated_duplicate_modal_binding_still_fails_closed(self):
        identity = self.reserve()
        with self.repo.transaction() as connection:
            original = dict(
                connection.execute(
                    select(scaler_receipts).where(
                        scaler_receipts.c.intent_id == identity
                    )
                )
                .mappings()
                .one()
            )
            connection.execute(
                insert(scaler_receipts).values(**dict(original, id=str(uuid.uuid4())))
            )
            guard = dict(modal_fixture.GUARD, task_id='unrelated')
            with self.assertRaisesRegex(
                BudgetExceeded, 'modal_test_budget_unconfirmed'
            ):
                projected_cost(connection, guard, self.now)
            with self.assertRaisesRegex(
                BudgetExceeded, 'modal_test_budget_unconfirmed'
            ):
                projected_costs(connection, [guard, modal_fixture.GUARD], self.now)
