"""Read-only alerts from original events, accounting and provider obligations."""

import copy
import builtins
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import runpy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import OperationalError
from studio_platform import alert_snapshot
from studio_platform import dstack_test_budget, spending_limits
from studio_platform.alert_snapshot import read_snapshot
from studio_platform.alert_conditions import evaluate
from studio_platform.dstack_test_budget import day_window, projected_cost
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import (
    attempts,
    budget_reservations,
    instance_intents,
    jobs,
    outbox,
    registered_workers,
)
from test_platform_repository import LedgerCase
import test_dstack_test_budget as budget_fixture


class SnapshotDiagnosticTests(unittest.TestCase):
    def invoke(
        self,
        *,
        settings_error=None,
        repository_error=None,
        read_error=None,
        close_error=None,
        value=None,
        stderr=None,
    ):
        from studio_platform.settings import Settings

        output = io.StringIO()
        errors = stderr if stderr is not None else io.StringIO()
        repo = Mock()
        repo.close.side_effect = close_error
        with (
            patch.object(
                Settings,
                'from_environment',
                return_value=SimpleNamespace(
                    database_url='PRIVATE_URL', tenant_id='PRIVATE_TENANT'
                ),
                side_effect=settings_error,
            ),
            patch.object(
                alert_snapshot,
                'Repository',
                return_value=repo,
                side_effect=repository_error,
            ),
            patch.object(
                alert_snapshot,
                'read_snapshot',
                return_value=value,
                side_effect=read_error,
            ),
            patch.object(alert_snapshot.time, 'time', return_value=1000),
            patch.object(alert_snapshot.time, 'monotonic', return_value=10),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = alert_snapshot.main()
        return status, json.loads(output.getvalue()), errors

    def test_failures_keep_original_stdout_and_exit_and_only_log_safe_category(self):
        database_error = OperationalError(
            'SELECT PRIVATE_SQL',
            {'credential': 'PRIVATE_VALUE'},
            RuntimeError('PRIVATE_PASSWORD'),
        )
        for options, category in (
            ({'settings_error': ValueError('PRIVATE_SETTING')}, 'configuration'),
            ({'repository_error': database_error}, 'database'),
            ({'read_error': database_error}, 'database'),
            ({'read_error': ValueError('PRIVATE_ROW')}, 'validation'),
            ({'read_error': TypeError('PRIVATE_ROW')}, 'validation'),
            ({'read_error': RuntimeError('PRIVATE_DETAILS')}, 'internal'),
            ({'value': {'PRIVATE_KEY': object()}}, 'serialization'),
        ):
            with self.subTest(category=category, options=list(options)):
                status, value, errors = self.invoke(**options)
                self.assertEqual(status, 0)
                self.assertEqual(
                    value,
                    {
                        'version': 1,
                        'observed_at': 1000,
                        'complete': False,
                        'gpus': [],
                        'jobs': [],
                        'test_spend': None,
                        'failures': [],
                    },
                )
                records = [json.loads(line) for line in errors.getvalue().splitlines()]
                self.assertEqual(records[-1], {'category': category, 'elapsed_ms': 0})
                self.assertTrue(
                    all(set(row) == {'phase', 'elapsed_ms'} for row in records[:-1])
                )
                self.assertNotIn('PRIVATE', errors.getvalue())

    def test_success_keeps_stdout_and_emits_no_failure_diagnostic(self):
        original = {
            'version': 1,
            'observed_at': 1000,
            'complete': True,
            'gpus': [],
            'jobs': [],
            'test_spend': None,
            'failures': [],
        }
        status, value, errors = self.invoke(value=original)
        self.assertEqual((status, value), (0, original))
        records = [json.loads(line) for line in errors.getvalue().splitlines()]
        self.assertEqual(
            [row['phase'] for row in records],
            ['settings_import', 'settings', 'repository', 'serialize', 'close'],
        )
        self.assertTrue(all(set(row) == {'phase', 'elapsed_ms'} for row in records))

    def test_diagnostic_write_failure_does_not_change_snapshot_or_exit(self):
        for method in ('write', 'flush'):
            with self.subTest(method=method):
                stream = Mock()
                getattr(stream, method).side_effect = OSError('PRIVATE_STDERR_FAILURE')
                status, value, _ = self.invoke(
                    read_error=ValueError('PRIVATE_ROW'), stderr=stream
                )
                self.assertEqual(status, 0)
                self.assertFalse(value['complete'])

    def test_cli_reports_import_entry_before_dependency_failure(self):
        original_import = builtins.__import__

        def controlled_import(name, *args, **kwargs):
            if name == 'sqlalchemy':
                raise ImportError('PRIVATE_IMPORT_FAILURE')
            return original_import(name, *args, **kwargs)

        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch('builtins.__import__', side_effect=controlled_import),
            self.assertRaisesRegex(ImportError, 'PRIVATE_IMPORT_FAILURE'),
        ):
            runpy.run_path(alert_snapshot.__file__, run_name='__main__')
        records = [json.loads(line) for line in errors.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['phase'], 'imports')
        self.assertEqual(set(records[0]), {'phase', 'elapsed_ms'})
        self.assertNotIn('PRIVATE', errors.getvalue())

    def test_library_import_remains_silent(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            runpy.run_path(
                alert_snapshot.__file__,
                run_name='studio_platform._diagnostic_import_probe',
            )
        self.assertEqual(errors.getvalue(), '')

    def test_connection_and_first_query_have_distinct_entry_markers(self):
        for failed in ('connect', 'jobs'):
            with self.subTest(failed=failed):
                repo, connection = Mock(), Mock()
                connection.dialect.name = 'sqlite'
                if failed == 'connect':
                    repo.engine.connect.side_effect = RuntimeError('PRIVATE_CONNECT')
                else:
                    context = repo.engine.connect.return_value
                    context.__enter__ = Mock(return_value=connection)
                    context.__exit__ = Mock(return_value=False)
                    connection.execute.side_effect = RuntimeError('PRIVATE_QUERY')
                errors = io.StringIO()
                with (
                    redirect_stderr(errors),
                    patch.object(alert_snapshot.time, 'monotonic', return_value=11),
                    self.assertRaises(RuntimeError),
                ):
                    read_snapshot(
                        repo, now=1000, tenant_id='synthetic', _diagnostic_started=10
                    )
                records = [json.loads(line) for line in errors.getvalue().splitlines()]
                self.assertEqual(
                    [row['phase'] for row in records],
                    ['connect']
                    if failed == 'connect'
                    else ['connect', 'jobs', 'connection_close'],
                )
                self.assertTrue(all(row['elapsed_ms'] == 1000 for row in records))
                self.assertNotIn('PRIVATE', errors.getvalue())

    def test_close_checkpoint_preserves_swallowed_close_failure(self):
        original = {'complete': True}
        status, value, errors = self.invoke(
            value=original, close_error=RuntimeError('PRIVATE_CLOSE')
        )
        self.assertEqual((status, value), (0, original))
        records = [json.loads(line) for line in errors.getvalue().splitlines()]
        self.assertEqual(records[-1]['phase'], 'close')
        self.assertNotIn('PRIVATE', errors.getvalue())

    def test_phase_time_is_capped_cumulative_entry_time(self):
        for now, expected in ((9, 0), (11.25, 1250), (100, 60000)):
            with self.subTest(now=now):
                errors = io.StringIO()
                with (
                    redirect_stderr(errors),
                    patch.object(alert_snapshot.time, 'monotonic', return_value=now),
                ):
                    alert_snapshot._progress('close', 10)
                self.assertEqual(
                    json.loads(errors.getvalue()),
                    {'phase': 'close', 'elapsed_ms': expected},
                )


class BudgetSnapshotDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.case = budget_fixture.DailyTestBudgetTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.case.history(amount=1_812_500, actual=700_000)

    def ledger(self):
        with self.case.repo.engine.connect() as connection:
            return self.case._projection_rows(connection)

    def main(self, options):
        from studio_platform.settings import Settings

        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(
                Settings,
                'from_environment',
                return_value=SimpleNamespace(
                    database_url='PRIVATE_URL', tenant_id=self.case.scope.tenant_id
                ),
            ),
            patch.object(alert_snapshot, 'Repository', return_value=self.case.repo),
            patch.object(
                alert_snapshot,
                'read_snapshot',
                side_effect=lambda repo, **kwargs: read_snapshot(
                    repo, **kwargs, **options
                ),
            ),
            patch.object(alert_snapshot.time, 'time', return_value=self.case.now),
            patch.object(alert_snapshot.time, 'monotonic', return_value=10),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = alert_snapshot.main()
        return status, json.loads(output.getvalue()), errors.getvalue()

    def test_main_budget_phases_preserve_v2_explicit_v2_and_v1_queries_and_ledger(self):
        _, node = self.case.rows()
        explicit = node['payload']['lifecycle']['test_gpu_guard']
        before = self.ledger()
        prefix = [
            'settings_import',
            'settings',
            'repository',
            'connect',
            'jobs',
            'failures',
            'guard_rows',
            'gpu_activity',
            'terminal_holds',
            'budget',
            'budget_guard',
        ]
        for options, scoped in (
            ({}, True),
            ({'test_guard': explicit}, True),
            ({'test_guard': budget_fixture.GUARD}, False),
        ):
            with self.subTest(explicit=bool(options), scoped=scoped):
                statements = []
                listener = lambda *args: statements.append(args[2])
                event.listen(self.case.repo.engine, 'before_cursor_execute', listener)
                try:
                    silent = io.StringIO()
                    with redirect_stderr(silent):
                        original = read_snapshot(
                            self.case.repo,
                            now=self.case.now,
                            tenant_id=self.case.scope.tenant_id,
                            **options,
                        )
                    self.assertEqual(silent.getvalue(), '')
                    original_statements = list(statements)
                    statements.clear()
                    status, observed, errors = self.main(options)
                    self.assertEqual((status, observed), (0, original))
                    self.assertEqual(statements, original_statements)
                finally:
                    event.remove(
                        self.case.repo.engine, 'before_cursor_execute', listener
                    )
                phases = [json.loads(line)['phase'] for line in errors.splitlines()]
                self.assertEqual(
                    phases,
                    prefix
                    + (
                        ['budget_settings_import', 'budget_settings', 'budget_rows']
                        if scoped
                        else []
                    )
                    + ['budget_projection', 'connection_close', 'serialize', 'close'],
                )
                self.assertEqual(len(phases), len(set(phases)))
                spend = observed['test_spend']
                self.assertEqual(
                    (spend['tasks'][0] if scoped else spend)['projected_microusd'],
                    700_000,
                )
                self.assertNotIn('PRIVATE', errors)
                self.assertEqual(self.ledger(), before)

    def test_budget_failures_leave_the_entered_phase_before_cleanup(self):
        original_import = builtins.__import__
        original_rows = dstack_test_budget._reservation_rows

        def fail_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'spending_limits' and level == 1:
                raise ImportError('PRIVATE_SETTINGS_IMPORT')
            return original_import(name, globals, locals, fromlist, level)

        def fail_consumption(connection):
            for row in original_rows(connection):
                yield row
                raise RuntimeError('PRIVATE_ROW_CONSUMPTION')
            self.fail('expected an original reservation row')

        before = self.ledger()
        for phase in (
            'budget_settings_import',
            'budget_settings',
            'budget_rows',
            'budget_projection',
        ):
            with self.subTest(phase=phase), ExitStack() as stack:
                if phase == 'budget_settings_import':
                    stack.enter_context(
                        patch('builtins.__import__', side_effect=fail_import)
                    )
                elif phase == 'budget_settings':
                    stack.enter_context(
                        patch.object(
                            spending_limits,
                            'read_settings',
                            side_effect=RuntimeError('PRIVATE_SETTINGS_READ'),
                        )
                    )
                elif phase == 'budget_rows':
                    stack.enter_context(
                        patch.object(
                            dstack_test_budget,
                            '_reservation_rows',
                            side_effect=fail_consumption,
                        )
                    )
                else:
                    stack.enter_context(
                        patch.object(
                            dstack_test_budget,
                            '_projected_rows',
                            side_effect=RuntimeError('PRIVATE_PROJECTION'),
                        )
                    )
                status, value, errors = self.main({})
                records = [json.loads(line) for line in errors.splitlines()]
                phases = [row['phase'] for row in records if 'phase' in row]
                self.assertEqual((status, value['complete']), (0, False))
                self.assertEqual(phases[-3:], [phase, 'connection_close', 'close'])
                self.assertEqual(records[-1], {'category': 'internal', 'elapsed_ms': 0})
                self.assertNotIn('PRIVATE', errors)
            self.assertEqual(self.ledger(), before)


class AlertSnapshotTests(LedgerCase):
    def snapshot(self):
        return read_snapshot(self.repo, now=self.now, tenant_id=self.scope.tenant_id)

    def test_fixed_query_markers_do_not_change_original_snapshot(self):
        original = self.snapshot()
        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch.object(alert_snapshot.time, 'monotonic', return_value=11),
        ):
            observed = read_snapshot(
                self.repo,
                now=self.now,
                tenant_id=self.scope.tenant_id,
                _diagnostic_started=10,
            )
        self.assertEqual(observed, original)
        self.assertEqual(
            [json.loads(line)['phase'] for line in errors.getvalue().splitlines()],
            [
                'connect',
                'jobs',
                'failures',
                'guard_rows',
                'gpu_activity',
                'terminal_holds',
                'budget',
                'connection_close',
            ],
        )

    def transition(self, job, status, event_type):
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(status=status, updated_at=self.now)
            )
            self.repo._emit(c, event_type, job["id"], {})

    def test_polling_updated_at_and_same_status_events_do_not_reset_state_age(self):
        job = self.job()
        self.now += 60
        self.transition(job, "running", "job.running")
        first = self.now
        for _ in range(4):
            self.now += 600
            self.transition(job, "running", "job.running")
        current = self.snapshot()["jobs"][0]
        self.assertEqual(current["state_since"], first)
        self.assertEqual(current["wake_count"], 0)
        alert = evaluate(self.snapshot(), None, environment="staging", now=self.now)
        self.assertEqual(alert["notifications"][0]["code"], "job_stuck")
        self.transition(job, "collecting", "job.collecting")
        self.assertEqual(self.snapshot()["jobs"][0]["state_since"], self.now)

    def test_missing_transition_history_is_unknown_not_updated_at(self):
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(status="running", updated_at=1)
            )
        self.assertIsNone(self.snapshot()["jobs"][0]["state_since"])

    def test_wake_counts_exclude_normal_poll_leases_and_are_windowed(self):
        job = self.job()
        for _ in range(12):
            self.transition(job, "queued", "job.queued")
        with self.repo.transaction() as c:
            for _ in range(50):
                self.repo._emit(c, "job.leased", job["id"], {"purpose": "reconcile"})
        self.assertEqual(self.snapshot()["jobs"][0]["wake_count"], 13)
        self.now += 1801
        self.assertEqual(self.snapshot()["jobs"][0]["wake_count"], 0)

    def test_failure_codes_are_closed_and_snapshot_has_no_request_or_sql_writes(self):
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    status="failed",
                    error_code="PRIVATE token prompt",
                    result={
                        "failure_diagnostic": {"reason_code": "PRIVATE token prompt"}
                    },
                )
            )
        statements = []

        def capture(c, cu, statement, *args):
            statements.append(statement)

        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            value = self.snapshot()
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertEqual(
            value["failures"][0]["kind"], "generation_failure_unclassified"
        )
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("request", json.dumps(value))
        self.assertTrue(
            all(
                s.lstrip().upper().startswith(("SELECT", "SET", "SHOW"))
                for s in statements
            )
        )

    def test_foreign_tenant_jobs_do_not_enter_snapshot(self):
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(tenant_id="foreign")
            )
        self.assertEqual(self.snapshot()["jobs"], [])

    def test_lifetime_history_over_limit_is_aggregated_without_hiding_active_jobs(self):
        import uuid

        active = self.job()
        with self.repo.transaction() as c:
            template = dict(
                c.execute(select(jobs).where(jobs.c.id == active["id"]))
                .mappings()
                .one()
            )
            history = []
            for i in range(10005):
                ident = str(uuid.uuid4())
                history.append(
                    {
                        **template,
                        "id": ident,
                        "idempotency_key": ident,
                        "status": "failed",
                        "error_code": "private-unclassified-" + str(i),
                        "result": {},
                    }
                )
            c.execute(insert(jobs), history)
            c.execute(
                insert(attempts),
                [
                    {
                        "id": str(uuid.uuid4()),
                        "job_id": row["id"],
                        "number": 1,
                        "worker_id": "retired-history",
                        "fence": 1,
                        "status": "failed",
                        "created_at": self.now,
                        "updated_at": self.now,
                        "upstream_stopped": 1,
                    }
                    for row in history
                ],
            )
        result = self.snapshot()
        self.assertTrue(result["complete"])
        self.assertEqual([row["id"] for row in result["jobs"]], [active["id"]])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(
            result["failures"][0]["kind"], "generation_failure_unclassified"
        )
        self.assertNotIn("private-unclassified", json.dumps(result))


class BudgetAlertSnapshotTests(budget_fixture.DailyTestBudgetTests):
    def snapshot(self):
        return read_snapshot(self.repo, now=self.now, tenant_id=self.scope.tenant_id)

    def test_batch_snapshot_preserves_order_latest_guard_admin_cap_and_explicit_guard(
        self,
    ):
        from studio_platform.repository import spending_settings
        from studio_platform.spending_limits import ADMIN_OWNER, admin_defaults

        with patch.object(
            self.service,
            'provider_inventory',
            side_effect=lambda backend: {
                'backend': backend,
                'complete': True,
                'observed_at': self.now,
                'instance_ids': [],
            },
        ):
            old_zeta = self._removed_projection_node(
                key='old-zeta', task='zeta', cost=0.1
            )
            alpha = self._removed_projection_node(key='alpha', task='alpha', cost=0.2)
            new_zeta = self._removed_projection_node(
                key='new-zeta', task='zeta', cost=0.3
            )
        limits = admin_defaults(self.service)
        limits['test_task_limit_microusd'] = 5_000_000
        with self.repo.transaction() as connection:
            for identity, limit in (
                (old_zeta, 3_000_000),
                (alpha, 4_000_000),
                (new_zeta, 6_000_000),
            ):
                payload = copy.deepcopy(
                    connection.execute(
                        select(operator_nodes.c.payload).where(
                            operator_nodes.c.intent_id == identity
                        )
                    ).scalar_one()
                )
                payload['lifecycle']['test_gpu_guard']['task_limit_microusd'] = limit
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == identity)
                    .values(payload=payload)
                )
            connection.execute(
                insert(spending_settings).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=ADMIN_OWNER,
                    version=1,
                    value=limits,
                    updated_at=self.now,
                    actor_id=self.owner.actor_id,
                )
            )
            before = self._projection_rows(connection)
        statements = []
        listener = lambda *args: statements.append(args[2])
        event.listen(self.repo.engine, 'before_cursor_execute', listener)
        try:
            result = self.snapshot()
        finally:
            event.remove(self.repo.engine, 'before_cursor_execute', listener)
        tasks = result['test_spend']['tasks']
        self.assertEqual([row['task_id'] for row in tasks], ['alpha', 'zeta'])
        self.assertEqual(
            [row['projected_microusd'] for row in tasks], [200_000, 400_000]
        )
        self.assertEqual(
            [row['limit_microusd'] for row in tasks], [4_000_000, 5_000_000]
        )
        self.assertEqual(
            sum(
                'FROM platform_budget_reservations LEFT OUTER JOIN' in sql
                for sql in statements
            ),
            1,
        )
        explicit = budget_fixture.task_guard(
            budget_fixture.GUARD, 'alpha', limit=9_000_000, max_runtime=7200
        )
        override = read_snapshot(
            self.repo, now=self.now, tenant_id=self.scope.tenant_id, test_guard=explicit
        )['test_spend']['tasks']
        self.assertEqual(override, [dict(tasks[0], limit_microusd=5_000_000)])
        with self.repo.engine.connect() as connection:
            self.assertEqual(self._projection_rows(connection), before)

    def test_alert_projection_uses_original_unknown_hold_once_across_sydney_midnight(
        self,
    ):
        self.start()
        with self.repo.engine.connect() as c:
            before = [
                dict(row) for row in c.execute(select(budget_reservations)).mappings()
            ]
        first = self.snapshot()
        self.assertEqual(first["test_spend"]["scope"], "acceptance_task")
        self.assertEqual(
            first["test_spend"]["tasks"][0]["projected_microusd"], 1_812_500
        )
        self.now = day_window(self.now)[1] + 1
        next_day = self.snapshot()
        with self.repo.engine.connect() as c:
            expected = projected_cost(
                c, self.rows()[1]['payload']['lifecycle']['test_gpu_guard'], self.now
            )
            self.assertEqual(
                [
                    dict(row)
                    for row in c.execute(select(budget_reservations)).mappings()
                ],
                before,
            )
        self.assertEqual(
            next_day["test_spend"]["tasks"][0]["projected_microusd"], expected
        )
        self.assertGreater(expected, 0)
        self.assertEqual(
            first["test_spend"]["tasks"][0]["resource_id"],
            next_day["test_spend"]["tasks"][0]["resource_id"],
        )

    def test_settled_task_actuals_do_not_reset_by_calendar(self):
        self.history(amount=1_812_500, actual=19_000_000)
        self.assertEqual(
            self.snapshot()["test_spend"]["tasks"][0]["projected_microusd"], 19_000_000
        )
        self.now = day_window(self.now)[1] + 1
        self.assertEqual(
            self.snapshot()["test_spend"]["tasks"][0]["projected_microusd"], 19_000_000
        )

    def test_legacy_guard_snapshot_remains_readable_until_scoped_tasks_exist(self):
        self.history(amount=1_812_500, actual=19_000_000)
        intent, node = self.rows()
        payload = copy.deepcopy(node['payload'])
        payload['lifecycle']['test_gpu_guard'] = copy.deepcopy(budget_fixture.GUARD)
        payload['request'].pop('test_task_id')
        with self.repo.transaction() as c:
            c.execute(update(operator_nodes).values(payload=payload))
        self.assertEqual(
            self.snapshot()['test_spend']['projected_microusd'], 19_000_000
        )
        self.now = day_window(self.now)[1] + 1
        self.assertEqual(self.snapshot()['test_spend']['projected_microusd'], 0)

    def test_gpu_original_idle_setting_and_unsafe_attempt_obligations(self):
        node_id = self.start()["node_id"]
        intent, node = self.rows()
        payload = copy.deepcopy(node["payload"])
        payload["dstack"]["first_runtime_ready_at"] = self.now
        with self.repo.transaction() as c:
            c.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node_id)
                .values(state="ready", provider_instance_id="original-123")
            )
            c.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(runtime_state="ready", payload=payload)
            )
        value = self.snapshot()["gpus"][0]
        self.assertEqual(value["idle_limit_seconds"], 600)
        self.assertFalse(value["has_obligations"])
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
            c.execute(
                insert(registered_workers).values(
                    id="original-worker",
                    pool=intent["pool"],
                    provider="vast",
                    instance_id="original-123",
                    spec={},
                    spec_hash="a" * 64,
                    state="retired",
                    current_job_id=None,
                    drain_requested=1,
                    fence=1,
                    expires_at=self.now,
                    updated_at=self.now,
                )
            )
            c.execute(
                insert(attempts).values(
                    id=__import__("uuid").uuid4().hex,
                    job_id=job["id"],
                    number=1,
                    worker_id="original-worker",
                    fence=1,
                    status="failed",
                    created_at=self.now,
                    updated_at=self.now,
                    submission_started_at=self.now,
                    upstream_task_id="original",
                    upstream_stopped=0,
                )
            )
        self.assertTrue(self.snapshot()["gpus"][0]["has_obligations"])


class TerminalSnapshot254Tests(LedgerCase):
    def test_terminal_hold_age_uses_original_event_not_reconciliation_updates(self):
        job = self.job()
        first = self.now
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(status="failed", updated_at=self.now)
            )
            self.repo._emit(c, "job.failed", job["id"], {})
        self.now += 7200
        with self.repo.transaction() as c:
            c.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(updated_at=self.now)
            )
        value = read_snapshot(self.repo, now=self.now, tenant_id=self.scope.tenant_id)
        self.assertEqual(
            value["terminal_reservations"], [{"id": job["id"], "terminal_since": first}]
        )
        with self.repo.transaction() as c:
            self.repo._settle(c, "job", job["id"], 0)
        self.assertEqual(
            read_snapshot(self.repo, now=self.now, tenant_id=self.scope.tenant_id)[
                "terminal_reservations"
            ],
            [],
        )
