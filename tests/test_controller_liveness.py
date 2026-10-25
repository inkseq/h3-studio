"""No provider access: stalled/idle loop detection and content-free durable alerts."""

import json
import threading
import unittest
from unittest.mock import Mock

from sqlalchemy import select, update
from studio_platform.controller_liveness import Progress, inspect, key
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
