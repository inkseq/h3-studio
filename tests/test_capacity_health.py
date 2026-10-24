"""Pure health projection; no provider, credential, database or rental effects."""

import copy
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import insert, select, update

from studio_platform.capacity_health import evaluate
from studio_platform.capacity_health import (
    check,
    record,
    snapshot,
    pending_reports,
    _event,
)
from studio_platform.dstack_demand_policy import policy_key, read_runtime
from studio_platform.dstack_controller import DstackController
from studio_platform.operator_capacity import operator_nodes, operator_policy
from studio_platform.repository import outbox
from studio_platform.telemetry import NullTelemetry
from studio_platform.error_monitoring import report_capacity_health, sanitized_event
from test_platform_repository import LedgerCase
import test_dstack_operator as operators


class CapacityHealthTests(unittest.TestCase):
    def setUp(self):
        self.node = {
            "intent_id": "a" * 32,
            "pool": "p",
            "provider": "vast",
            "provider_instance_id": "123",
            "desired_state": "running",
            "runtime_state": "ready",
            "payload": {
                "dstack": {
                    "apply_started": True,
                    "apply_started_at": 100,
                    "first_runtime_ready_at": 200,
                    "observed_at": 1000,
                    "profile_id": "profile",
                    "mode": "ref",
                    "model_id": "model",
                    "configuration_id": "config",
                    "manifest_digest": "digest",
                }
            },
        }
        self.job = {
            "id": "job",
            "status": "queued",
            "created_at": 100,
            "updated_at": 1000,
            "current_attempt_id": None,
            "request": {
                "deployment_profile_id": "profile",
                "request": {"mode": "ref", "model": "model"},
            },
            "execution_plan": {
                "pool": "p",
                "model_id": "model",
                "configuration_id": "config",
                "engine_manifest_digest": "digest",
            },
        }

    def evaluate(
        self, *, now=1000, nodes=None, jobs=None, attempts=(), workers=(), **kwargs
    ):
        return evaluate(
            nodes or [self.node],
            jobs or [self.job],
            attempts,
            workers,
            now=now,
            **kwargs,
        )

    def test_queue_age_uses_original_episode_not_refreshed_updated_at(self):
        findings = self.evaluate(now=2001, queue_ages={"job": 100})
        self.assertEqual(
            [row["code"] for row in findings if row["resource_id"] == "job"],
            ["capacity_queue_age"],
        )
        self.job["updated_at"] = 2001
        self.assertEqual(self.evaluate(now=2001, queue_ages={"job": 100}), findings)

    def test_registration_refusal_alerts_without_replacing_healthy_machine(self):
        self.node["payload"]["dstack"]["registration_observation"] = {
            "reason_code": "global_capacity_exceeded"
        }
        finding = self.evaluate()[0]
        self.assertEqual(
            (finding["code"], finding["diagnosis"], finding["stop_replace"]),
            ("capacity_paid_idle_queue", "registration_capacity", False),
        )

    def test_newly_queued_work_does_not_make_previously_idle_node_look_stalled(self):
        self.job["created_at"] = 950
        self.assertEqual(self.evaluate(now=1000), [])
        self.job["created_at"] = 100
        self.assertEqual(self.evaluate(now=1000, queue_ages={"job": 950}), [])

    def test_queued_deferred_attempts_without_submission_or_live_lease_do_not_hide_paid_idle(
        self,
    ):
        self.node["runtime_state"] = "runtime_unconfirmed"
        self.node["payload"]["dstack"].update(
            reason_code="dstack_runtime_not_observed", state="runtime_unconfirmed"
        )
        deferred = [
            {
                "id": "deferred-" + str(index),
                "job_id": "job",
                "worker_id": "dstack-" + "a" * 32,
                "status": "deferred",
                "submission_started_at": None,
                "upstream_task_id": None,
                "updated_at": 1000,
            }
            for index in range(9)
        ]
        self.job.update(
            current_attempt_id=deferred[-1]["id"],
            lease_worker_id=None,
            lease_expires_at=None,
        )
        finding = self.evaluate(attempts=deferred)[0]
        self.assertEqual(
            (finding["code"], finding["diagnosis"], finding["stop_replace"]),
            ("capacity_paid_idle_queue", "dispatch_unobserved", False),
        )
        self.job.update(
            status="claimed",
            lease_worker_id=deferred[-1]["worker_id"],
            lease_expires_at=1030,
        )
        queued = copy.deepcopy(self.job)
        queued.update(
            id="next-queued",
            status="queued",
            current_attempt_id=None,
            lease_worker_id=None,
            lease_expires_at=None,
        )
        self.assertEqual(self.evaluate(jobs=[self.job, queued], attempts=deferred), [])

    def test_only_fresh_positive_host_fault_requests_stop_and_replace(self):
        self.node["payload"]["dstack"]["reason_code"] = "runtime_process_exited"
        self.assertTrue(self.evaluate()[0]["stop_replace"])
        self.node["payload"]["dstack"]["observed_at"] = 1
        self.assertFalse(self.evaluate()[0]["stop_replace"])

    def test_active_attempt_and_incompatible_queue_never_become_idle_stop(self):
        active = {
            "id": "attempt",
            "job_id": "job",
            "worker_id": "dstack-" + "a" * 32,
            "status": "running",
            "submission_started_at": 300,
        }
        self.job.update(status="running", current_attempt_id="attempt")
        queued = copy.deepcopy(self.job)
        queued.update(id="other", status="queued", current_attempt_id=None)
        self.assertEqual(self.evaluate(jobs=[self.job, queued], attempts=[active]), [])
        self.job.update(status="queued", current_attempt_id=None)
        self.job["request"]["request"]["mode"] = "fl"
        self.assertEqual(self.evaluate(), [])

    def test_two_empty_start_stop_cycles_pause_only_current_enable_window(self):
        nodes = []
        for identity, started, ended in (("a" * 32, 100, 200), ("b" * 32, 300, 400)):
            node = copy.deepcopy(self.node)
            node.update(
                intent_id=identity, runtime_state="stopped", desired_state="stopped"
            )
            node["payload"]["dstack"].update(
                apply_started_at=started, first_stopped_at=ended
            )
            nodes.append(node)
        findings = self.evaluate(
            nodes=nodes, demand_enabled=True, demand_enabled_since=50
        )
        self.assertEqual(findings[0]["code"], "capacity_demand_start_loop")
        self.assertEqual(len(findings[0]["node_ids"]), 2)
        self.assertEqual(
            self.evaluate(nodes=nodes, demand_enabled=True, demand_enabled_since=250),
            [],
        )
        self.assertEqual(self.evaluate(nodes=nodes, demand_enabled=False), [])
        attempt = {
            "id": "attempt",
            "job_id": "job",
            "worker_id": "dstack-" + "a" * 32,
            "status": "succeeded",
            "submission_started_at": 150,
            "updated_at": 170,
        }
        self.assertEqual(
            self.evaluate(nodes=nodes, demand_enabled=True, attempts=[attempt]), []
        )

    def test_repeated_poll_and_single_original_cycle_cannot_create_loop(self):
        node = copy.deepcopy(self.node)
        node.update(runtime_state="stopped", desired_state="stopped")
        node["payload"]["dstack"]["first_stopped_at"] = 300
        self.assertEqual(self.evaluate(nodes=[node, node], demand_enabled=True), [])

    def test_health_sentry_event_is_closed_and_content_free(self):
        client = Mock()
        report_capacity_health(
            client,
            {
                "event_id": "00000000-0000-4000-8000-000000000000",
                "code": "capacity_queue_age",
                "diagnosis": "queue_age",
            },
        )
        event = client.capture_event.call_args.args[0]
        event.update(
            request={"cookie": "secret"},
            user={"email": "secret"},
            extra={"prompt": "secret"},
        )
        clean = sanitized_event(event)
        self.assertEqual(
            set(clean), {"platform", "level", "message", "event_id", "tags"}
        )
        self.assertNotIn("secret", str(clean))
        event["tags"]["diagnosis"] = ["malformed"]
        self.assertIsNone(sanitized_event(event))


class HealthLedgerTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.service = SimpleNamespace(
            repo=self.repo, settings=SimpleNamespace(tenant_id=self.scope.tenant_id)
        )

    def events(self, event_type):
        with self.repo.engine.connect() as connection:
            return list(
                connection.execute(
                    select(outbox).where(outbox.c.event_type == event_type)
                ).mappings()
            )

    def test_queue_alert_is_deduplicated_without_mutating_job_or_hold(self):
        original = self.job()
        before = self.repo.get_job(self.scope, original["id"])
        budget = self.repo.get_budget("owner-budget")
        self.now += 1801
        self.assertEqual(check(self.service, NullTelemetry())["new_alerts"], 1)
        self.assertEqual(check(self.service, NullTelemetry())["new_alerts"], 0)
        self.assertEqual(len(self.events("capacity.health_alert")), 1)
        self.assertEqual(len(self.events("capacity.health_alert_reported")), 1)
        self.assertEqual(self.repo.get_job(self.scope, original["id"]), before)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)

    def test_failed_export_remains_pending_until_same_event_is_accepted(self):
        self.job()
        self.now += 1801
        telemetry = Mock(enabled=True)
        telemetry.start.return_value.finish.side_effect = [None, 0.0]
        telemetry.force_flush.return_value = True
        reporter = Mock(return_value="accepted")
        check(self.service, telemetry, reporter)
        self.assertEqual(len(self.events("capacity.health_alert_reported")), 0)
        check(self.service, telemetry, reporter)
        self.assertEqual(len(self.events("capacity.health_alert")), 1)
        self.assertEqual(len(self.events("capacity.health_alert_reported")), 1)

    def test_hundred_alerts_share_one_flush_and_failed_flush_keeps_original_ids_pending(
        self,
    ):
        with self.repo.transaction() as connection:
            original_ids = {
                _event(
                    connection,
                    self.scope.tenant_id,
                    {
                        "version": 1,
                        "resource_id": str(uuid.uuid4()),
                        "code": "capacity_queue_age",
                        "diagnosis": "queue_age",
                    },
                    self.now,
                )["event_id"]
                for _ in range(100)
            }
        telemetry = Mock(enabled=True)
        telemetry.start.return_value.finish.return_value = 0.0
        telemetry.force_flush.side_effect = [False, True]
        reporter = Mock(return_value="accepted")
        pending_reports(self.service, telemetry, reporter)
        self.assertEqual(telemetry.force_flush.call_count, 1)
        self.assertEqual(len(self.events("capacity.health_alert_reported")), 0)
        pending_reports(self.service, telemetry, reporter)
        self.assertEqual(telemetry.force_flush.call_count, 2)
        acknowledgements = self.events("capacity.health_alert_reported")
        self.assertEqual(
            {row["payload"]["alert_event_id"] for row in acknowledgements}, original_ids
        )
        self.assertEqual(
            {call.args[0]["event_id"] for call in reporter.call_args_list}, original_ids
        )


class DemandLoopLedgerTests(LedgerCase):
    preview = operators.DstackOperatorTests.preview
    start = operators.DstackOperatorTests.start

    def setUp(self):
        super().setUp()
        operators.DstackOperatorTests._setup_dstack(self)
        with self.repo.transaction() as connection:
            connection.execute(
                insert(operator_policy).values(
                    id=policy_key(self.scope.tenant_id),
                    version=1,
                    payload={"tenant_id": self.scope.tenant_id, "enabled": True},
                    actor="qa-admin",
                    updated_at=self.now - 1,
                )
            )
        for index in range(2):
            node = self.start(key="empty-cycle-" + str(index))["node_id"]
            self.now += 10
            with self.repo.transaction() as connection:
                row = (
                    connection.execute(
                        select(operator_nodes).where(operator_nodes.c.intent_id == node)
                    )
                    .mappings()
                    .one()
                )
                payload = copy.deepcopy(row["payload"])
                payload["dstack"].update(first_stopped_at=self.now, state="stopped")
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node)
                    .values(
                        payload=payload,
                        desired_state="stopped",
                        runtime_state="stopped",
                    )
                )

    def test_loop_pauses_existing_policy_once_and_manual_enable_starts_new_window(self):
        result = check(self.service, NullTelemetry())
        self.assertTrue(result["demand_paused"])
        with self.repo.engine.connect() as connection:
            self.assertFalse(read_runtime(connection, self.scope.tenant_id)["enabled"])
        self.assertFalse(check(self.service, NullTelemetry())["demand_paused"])
        self.assertEqual(len(self.events()), 1)
        self.now += 1
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_policy)
                .where(operator_policy.c.id == policy_key(self.scope.tenant_id))
                .values(
                    version=3,
                    updated_at=self.now,
                    actor="qa-admin",
                    payload={"tenant_id": self.scope.tenant_id, "enabled": True},
                )
            )
        self.assertFalse(check(self.service, NullTelemetry())["demand_paused"])

    def events(self):
        with self.repo.engine.connect() as connection:
            return list(
                connection.execute(
                    select(outbox).where(outbox.c.event_type == "capacity.health_alert")
                ).mappings()
            )

    def test_second_stop_forces_pause_before_next_demand_decision_not_after_cadence(
        self,
    ):
        controller = DstackController(
            self.service, SimpleNamespace(readiness=Mock(), close=Mock())
        )
        controller._health_checked_at = self.now
        self.now += 1
        with self.repo.transaction() as connection:
            row = list(connection.execute(select(operator_nodes)).mappings())[-1]
            payload = copy.deepcopy(row["payload"])
            payload["dstack"]["first_stopped_at"] = self.now
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == row["intent_id"])
                .values(payload=payload)
            )
        with patch.object(controller, "_refresh_pending_removal"):
            result = controller.tick()
        self.assertTrue(result["health_check"]["demand_paused"])
        with self.repo.engine.connect() as connection:
            self.assertFalse(read_runtime(connection, self.scope.tenant_id)["enabled"])
