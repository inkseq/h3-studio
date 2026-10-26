"""No provider access: stalled/idle loop detection and content-free durable alerts."""

import json
import io
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from sqlalchemy import select, update
from studio_platform.controller_liveness import (
    Progress,
    inspect,
    key,
    fresh_state,
    healthy_without_pending,
    main,
)
from studio_platform.demand_wakeup import run_demand_loop
from studio_platform.operator_capacity import operator_heartbeats
from studio_platform.repository import (
    outbox,
    jobs,
    instance_intents,
    budget_reservations,
)
from studio_platform.error_monitoring import sanitized_event
from test_platform_repository import LedgerCase


class LivenessLedgerTests(LedgerCase):
    def test_postgres_fast_path_preserves_pending_delivery_and_heartbeat(self):
        if self.repo.engine.dialect.name != "postgresql":
            self.skipTest("native PostgreSQL probe")
        tenant = self.scope.tenant_id
        url = self.repo.engine.url.render_as_string(hide_password=False)
        probe = lambda: healthy_without_pending(url, tenant, clock=lambda: self.now)
        self.assertFalse(probe())
        Progress(self.repo, tenant).beat(False)
        self.assertTrue(probe())
        self.now += 181
        self.assertFalse(probe())
        inspect(self.repo, tenant)
        Progress(self.repo, tenant).beat(False)
        self.assertFalse(probe())  # Recovery does not silently drop the alert.
        from studio_platform.capacity_health import pending_reports
        from studio_platform.telemetry import NullTelemetry
        from types import SimpleNamespace

        service = SimpleNamespace(
            repo=self.repo, settings=SimpleNamespace(tenant_id=tenant)
        )
        sent = Mock(return_value="accepted")
        pending_reports(service, NullTelemetry(), sent)
        sent.assert_called_once()
        self.assertTrue(probe())
        inspect(self.repo, "other-tenant")
        self.assertTrue(probe())  # Tenant-scoped pending report check.
        with self.repo.engine.connect() as c:
            stamp = c.execute(
                select(operator_heartbeats.c.observed_at).where(
                    operator_heartbeats.c.id == key(tenant)
                )
            ).scalar_one()
        self.assertEqual(stamp, self.now)

    def test_missing_stale_dedup_recovery_and_separate_tenants(self):
        tenant = self.scope.tenant_id
        self.assertEqual(
            inspect(self.repo, tenant), {"state": "stale", "new_alert": True}
        )
        self.assertEqual(
            inspect(self.repo, tenant), {"state": "stale", "new_alert": False}
        )
        p = Progress(self.repo, tenant)
        p.beat(False, starting=True)
        self.assertEqual(inspect(self.repo, tenant)["state"], "starting")
        self.now += 60
        p.beat(False)
        self.assertEqual(inspect(self.repo, tenant)["state"], "healthy")
        self.now += 181
        self.assertEqual(
            inspect(self.repo, tenant), {"state": "stale", "new_alert": True}
        )
        self.assertFalse(inspect(self.repo, tenant)["new_alert"])
        p.beat(True)
        self.assertEqual(inspect(self.repo, tenant)["state"], "healthy")
        self.now += 181
        self.assertTrue(inspect(self.repo, tenant)["new_alert"])
        self.assertEqual(inspect(self.repo, "different-tenant")["state"], "stale")
        with self.repo.engine.connect() as c:
            for table in (jobs, instance_intents, budget_reservations):
                self.assertEqual(list(c.execute(select(table))), [])
            self.assertFalse(
                c.execute(
                    select(operator_heartbeats).where(
                        operator_heartbeats.c.id == "global"
                    )
                ).first()
            )

    def test_future_clock_invalid_state_and_abandoned_initialization_are_unhealthy(
        self,
    ):
        p = Progress(self.repo, self.scope.tenant_id)
        p.beat(False, starting=True)
        self.now += 181
        self.assertEqual(inspect(self.repo, self.scope.tenant_id)["state"], "stale")
        with self.repo.transaction() as c:
            c.execute(
                update(operator_heartbeats)
                .where(operator_heartbeats.c.id == p.key)
                .values(observed_at=self.now + 1, state="running")
            )
        self.assertEqual(inspect(self.repo, self.scope.tenant_id)["state"], "stale")
        with self.repo.transaction() as c:
            c.execute(
                update(operator_heartbeats)
                .where(operator_heartbeats.c.id == p.key)
                .values(observed_at=self.now, state="unknown")
            )
        self.assertEqual(inspect(self.repo, self.scope.tenant_id)["state"], "stale")

    def test_check_does_not_renew_loop_heartbeat_and_alert_has_no_content(self):
        p = Progress(self.repo, self.scope.tenant_id)
        p.beat(False)
        before = self.now
        self.now += 181
        inspect(self.repo, self.scope.tenant_id)
        with self.repo.engine.connect() as c:
            row = (
                c.execute(
                    select(operator_heartbeats).where(
                        operator_heartbeats.c.id == key(self.scope.tenant_id)
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(row["observed_at"], before)
            alert = c.execute(select(outbox)).mappings().one()["payload"]
        event = {
            "message": "capacity_health_alert",
            "event_id": alert["event_id"].replace("-", ""),
            "tags": {
                "code": alert["code"],
                "diagnosis": alert["diagnosis"],
                "secret": "private",
            },
            "user": {"email": "private"},
            "request": {"body": "private"},
        }
        safe = sanitized_event(event)
        self.assertEqual(
            safe["tags"],
            {"code": "capacity_controller_stale", "diagnosis": "controller_stale"},
        )
        self.assertNotIn("private", json.dumps(safe))

    def test_heartbeat_throttles_db_writes_but_keeps_incarnation(self):
        p = Progress(self.repo, self.scope.tenant_id)
        p.beat(False)
        before = self.now
        self.now += 59
        p.beat(True)
        with self.repo.engine.connect() as c:
            row = (
                c.execute(
                    select(operator_heartbeats).where(operator_heartbeats.c.id == p.key)
                )
                .mappings()
                .one()
            )
            self.assertEqual(row["observed_at"], before)
            self.assertEqual(row["controller_id"], p.identity)
        self.now += 1
        p.beat(True)
        self.assertEqual(inspect(self.repo, self.scope.tenant_id)["state"], "healthy")


class LivenessLoopTests(unittest.TestCase):
    def test_healthy_idle_checkpoints_do_not_repeat_provider_sweep(self):
        stop = threading.Event()
        clock = [0]
        beats, sweeps, waits = [], [], []

        class Wake:
            def subscribe(self):
                pass

            def wait(self, seconds):
                waits.append(seconds)
                clock[0] += seconds
                if len(waits) == 16:
                    stop.set()

        run_demand_loop(
            stop,
            Wake(),
            lambda: False,
            Mock(side_effect=AssertionError("idle work")),
            active_seconds=15,
            safety=lambda: sweeps.append(clock[0]),
            clock=lambda: clock[0],
            heartbeat=lambda active: beats.append((clock[0], active)),
        )
        self.assertEqual(waits, [60] * 16)
        self.assertEqual(sweeps, [0, 900])
        self.assertEqual(len(beats), 16)
        self.assertTrue(all(not active for _, active in beats))

    def test_stuck_or_failed_work_cannot_advance_heartbeat(self):
        beat = Mock()
        wake = Mock()

        def fail():
            raise TimeoutError("stalled call")

        with self.assertRaises(TimeoutError):
            run_demand_loop(
                threading.Event(),
                wake,
                lambda: True,
                fail,
                active_seconds=15,
                heartbeat=beat,
            )
        beat.assert_not_called()
        wake.wait.assert_not_called()


class CheapProbeTests(unittest.TestCase):
    def test_healthy_entrypoint_does_not_import_scheduler_orm_or_exporters(self):
        # Fresh interpreter: the parent ledger tests intentionally load the ORM.
        script = '''
import builtins, sys, time
from types import SimpleNamespace
from unittest.mock import MagicMock
blocked = ('sqlalchemy', 'studio_platform.repository', 'studio_platform.operator_capacity',
           'studio_platform.capacity_health', 'studio_platform.telemetry',
           'studio_platform.error_monitoring', 'sentry_sdk')
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if any(name == x or name.startswith(x + '.') for x in blocked):
        raise AssertionError('heavy import')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from studio_platform import controller_liveness as m
from studio_platform.settings import Settings
Settings.from_environment = lambda: SimpleNamespace(database_url='postgresql+psycopg://unused', tenant_id='test')
import psycopg
conn = MagicMock()
conn.execute.side_effect = [MagicMock(), SimpleNamespace(fetchone=lambda: (time.time(), 'idle')), SimpleNamespace(fetchone=lambda: None)]
context = MagicMock()
context.__enter__.return_value = conn
psycopg.connect = lambda *args, **kwargs: context
assert m.main() == 0
assert not any(x in sys.modules for x in blocked)
'''
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "healthy")

    def test_fast_path_is_read_only_bounded_and_parameterized(self):
        conn = Mock()
        conn.execute.side_effect = [
            Mock(),
            Mock(fetchone=lambda: (1000.0, "idle")),
            Mock(fetchone=lambda: None),
        ]
        connection = Mock()
        connection.__enter__ = Mock(return_value=conn)
        connection.__exit__ = Mock(return_value=False)
        with patch("psycopg.connect", return_value=connection) as connect:
            self.assertTrue(
                healthy_without_pending(
                    "postgresql+psycopg://unused", "tenant'private", clock=lambda: 1001
                )
            )
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 4)
        self.assertIn("statement_timeout=4000", connect.call_args.kwargs["options"])
        statements = [call.args[0] for call in conn.execute.call_args_list]
        self.assertIn("READ ONLY", statements[0])
        self.assertEqual(len(statements), 3)
        self.assertNotIn("tenant'private", " ".join(statements))
        self.assertEqual(conn.execute.call_args_list[-1].args[1], ("tenant'private",))

    def test_pending_or_expired_snapshot_does_not_take_fast_path(self):
        for pending, times in [(object(), [1001, 1001]), (None, [1001, 1181])]:
            conn = Mock()
            conn.execute.side_effect = [
                Mock(),
                Mock(fetchone=lambda: (1000.0, "running")),
                Mock(fetchone=lambda: pending),
            ]
            connection = Mock(
                __enter__=Mock(return_value=conn), __exit__=Mock(return_value=False)
            )
            with patch("psycopg.connect", return_value=connection):
                self.assertFalse(
                    healthy_without_pending(
                        "postgresql+psycopg://unused",
                        "test",
                        clock=Mock(side_effect=times),
                    )
                )

    def test_freshness_boundary_and_invalid_progress_remain_closed(self):
        self.assertEqual(
            fresh_state({"observed_at": 1000.0, "state": "idle"}, 1180), "healthy"
        )
        for row in (
            None,
            {"observed_at": float("nan"), "state": "running"},
            {"observed_at": 1182, "state": "running"},
            {"observed_at": 1000, "state": "running"},
            {"observed_at": 1180, "state": "unknown"},
        ):
            self.assertIsNone(fresh_state(row, 1181))
        self.assertEqual(
            fresh_state({"observed_at": 1180, "state": "starting"}, 1181), "starting"
        )

    def test_database_failure_is_unhealthy_without_private_exception(self):
        from types import SimpleNamespace

        output = io.StringIO()
        with (
            patch(
                "studio_platform.settings.Settings.from_environment",
                return_value=SimpleNamespace(
                    database_url="postgresql+psycopg://unused", tenant_id="test"
                ),
            ),
            patch(
                "psycopg.connect",
                side_effect=RuntimeError("private connection credential"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {"state": "unavailable"})

    def test_non_postgres_is_refused_before_connect(self):
        with patch("psycopg.connect") as connect:
            with self.assertRaises(ValueError):
                healthy_without_pending("sqlite://", "test")
        connect.assert_not_called()

    def test_pending_delivery_and_stale_progress_keep_original_observer(self):
        from types import SimpleNamespace

        settings = SimpleNamespace(
            database_url="postgresql+psycopg://unused", tenant_id="test"
        )
        for state, status in (("healthy", 0), ("stale", 1)):
            repo, telemetry, client = Mock(), Mock(), Mock(dsn=None)
            with (
                patch(
                    "studio_platform.settings.Settings.from_environment",
                    return_value=settings,
                ),
                patch(
                    "studio_platform.controller_liveness.healthy_without_pending",
                    return_value=False,
                ),
                patch("studio_platform.repository.Repository", return_value=repo),
                patch(
                    "studio_platform.controller_liveness.inspect",
                    return_value={"state": state, "new_alert": False},
                ) as observer,
                patch("studio_platform.capacity_health.pending_reports") as deliver,
                patch(
                    "studio_platform.error_monitoring.create_client",
                    return_value=client,
                ),
                patch(
                    "studio_platform.telemetry.configured_telemetry",
                    return_value=telemetry,
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), status)
            observer.assert_called_once_with(repo, "test")
            deliver.assert_called_once()
            self.assertIs(deliver.call_args.args[0].repo, repo)
            client.close.assert_called_once_with(timeout=1)
            telemetry.close.assert_called_once()
            repo.close.assert_called_once()
