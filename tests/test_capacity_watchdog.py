"""Disposable-ledger reclaim/owned-stop regressions; no live providers or GPU."""

import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import insert, select, update

from studio_platform.capacity_watchdog import (
    fenced_node_stop,
    reclaim_node,
    record_engine_progress,
    report_reclaims,
    timeout_reason,
    stop_unserved_node,
)
from studio_platform.dstack_demand_start import (
    _ended_for_replacement,
    _ended_with_proof,
    start_once,
)
from studio_platform.error_monitoring import report_capacity_reclaim, sanitized_event
from studio_platform.operator_capacity import operator_nodes
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    LeaseLost,
    attempts,
    instance_intents,
    jobs,
    outbox,
    registered_devices,
    registered_workers,
)
from studio_platform.settings import Settings
from test_platform_repository import LedgerCase
import test_dstack_controller as controllers
import test_dstack_operator as operators
import test_dstack_demand_start as demand


class ProgressTests(LedgerCase):
    def running(self):
        job = self.job()
        queue = TaskQueue(self.repo)
        claim = queue.claim("worker", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "task")
        return queue, claim

    def rows(self, claim):
        with self.repo.engine.connect() as connection:
            attempt = dict(
                connection.execute(
                    select(attempts).where(attempts.c.id == claim.lease.attempt_id)
                )
                .mappings()
                .one()
            )
        return self.repo.get_job(self.scope, claim.lease.job_id), attempt

    def test_controller_lease_and_updated_at_cannot_hide_ten_minute_silence(self):
        queue, claim = self.running()
        self.now += 601
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == claim.lease.job_id)
                .values(updated_at=self.now, lease_expires_at=self.now + 90)
            )
        job, attempt = self.rows(claim)
        self.assertEqual(
            timeout_reason(job, attempt, self.now), ("capacity_no_progress", 1000)
        )

    def test_positive_engine_heartbeat_is_attempt_bound_and_cas_fenced(self):
        queue, claim = self.running()
        record_engine_progress(self.repo, queue, claim.lease)
        job, attempt = self.rows(claim)
        self.assertEqual(
            job["result"]["capacity_progress"],
            {"attempt_id": attempt["id"], "at": 1000, "kind": "engine_running"},
        )
        self.now += 601
        queue.reclaim_capacity(
            self.scope,
            job["id"],
            expected_attempt_id=attempt["id"],
            expected_fence=job["fence"],
            reason="capacity_no_progress",
            evidence_id="timed-out",
            last_progress_at=1000,
        )
        with self.assertRaises(LeaseLost):
            record_engine_progress(self.repo, queue, claim.lease)

    def test_runtime_uses_two_times_expected_with_thirty_minute_floor(self):
        job = {
            "expected_runtime_s": 2000,
            "result": {
                "capacity_progress": {
                    "attempt_id": "a",
                    "kind": "engine_running",
                    "at": 4999,
                }
            },
        }
        attempt = {"id": "a", "submission_started_at": 1000}
        self.assertIsNone(timeout_reason(job, attempt, 4999))
        self.assertIsNone(timeout_reason(job, attempt, 5000))
        self.assertEqual(
            timeout_reason(job, attempt, 5001)[0], "capacity_runtime_exceeded"
        )
        job["expected_runtime_s"] = 10
        job["result"]["capacity_progress"]["at"] = 2800
        self.assertIsNone(timeout_reason(job, attempt, 2800))
        self.assertEqual(
            timeout_reason(job, attempt, 2801)[0], "capacity_runtime_exceeded"
        )

    def test_progress_for_another_attempt_or_future_clock_is_ignored(self):
        job = {
            "expected_runtime_s": 1000,
            "result": {
                "capacity_progress": {
                    "attempt_id": "old",
                    "kind": "engine_running",
                    "at": 1600,
                }
            },
        }
        attempt = {"id": "current", "submission_started_at": 1000}
        self.assertEqual(
            timeout_reason(job, attempt, 1600), ("capacity_no_progress", 1000)
        )

    def test_settings_validate_and_defaults_are_ten_thirty_twenty_minutes(self):
        settings = Settings(Path(self.temp.name))
        self.assertEqual(
            (
                settings.capacity_no_progress_seconds,
                settings.capacity_runtime_floor_seconds,
                settings.capacity_startup_timeout_seconds,
            ),
            (600, 1800, 1200),
        )
        for value in (0, -1, True, 86401):
            with self.assertRaises(ValueError):
                replace(settings, capacity_no_progress_seconds=value)

    def test_closed_sentry_message_discards_all_unreviewed_text(self):
        receipt = {
            "event_id": "df8d957a-75df-4d48-a420-786b0f218563",
            "reason": "capacity_no_progress",
            "count": 1,
        }
        client = Mock()
        report_capacity_reclaim(client, receipt)
        event = client.capture_event.call_args.args[0]
        event.update(
            request={"cookies": "secret"},
            extra={"prompt": "secret"},
            user={"email": "secret"},
        )
        clean = sanitized_event(event)
        self.assertEqual(
            set(clean), {"platform", "level", "message", "event_id", "tags"}
        )
        self.assertNotIn("secret", str(clean))
        event["tags"]["reason"] = "arbitrary text"
        self.assertIsNone(sanitized_event(event))

    def test_repeated_reclaims_deny_only_positively_bound_distinct_physical_rentals(
        self,
    ):
        from studio_platform.host_policy import automatic_deny
        from test_host_policy import physical

        nodes = []
        for identity, instance, at in (
            ("one", "1234567", 1000),
            ("two", "1234568", 1100),
        ):
            nodes.append(
                {
                    "intent_id": identity,
                    "desired_state": "stopped",
                    "payload": {
                        "capacity_backend": "dstack-v1",
                        "host_observation": physical(instance=instance, at=at),
                        "dstack": {
                            "backend": "vastai",
                            "provider_instance_id": instance,
                        },
                        "capacity_watchdog": {
                            "version": 1,
                            "reason": "capacity_no_progress",
                            "at": at,
                        },
                    },
                }
            )
        self.assertIsNone(automatic_deny([nodes[0], nodes[0]], "19492", now=1200))
        self.assertEqual(automatic_deny(nodes, "19492", now=1200), 1100 + 7 * 86400)
        for node in nodes:
            node["payload"].pop("host_observation")
        self.assertIsNone(automatic_deny(nodes, "1234567", now=1200))


class NodeWatchdogTests(LedgerCase):
    preview = operators.DstackOperatorTests.preview
    start = operators.DstackOperatorTests.start
    running = operators.DstackOperatorTests.running
    controller = controllers.DstackControllerTests.controller
    ready = controllers.DstackControllerTests.ready
    consumer = controllers.DstackControllerTests.consumer
    _continuing_capacity = controllers.DstackControllerTests._continuing_capacity
    _continuing_ready = controllers.DstackControllerTests._continuing_ready
    _queued_native = controllers.DstackControllerTests._queued_native

    def setUp(self):
        super().setUp()
        operators.DstackOperatorTests._setup_dstack(self)

    def node(self, node_id):
        with self.repo.engine.connect() as connection:
            return dict(
                connection.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == node_id)
                )
                .mappings()
                .one()
            ), dict(
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == node_id)
                )
                .mappings()
                .one()
            )

    def lost(self):
        node_id, controller = self.ready()
        job = self.job()
        queue = TaskQueue(self.repo)
        claim = queue.claim("dstack-test-slot", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "original-task")
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == "dstack-test-slot")
                .values(state="leased", current_job_id=job["id"])
            )
        self.now += 601
        return node_id, controller, job, claim

    def test_lost_attempt_requeues_then_owned_stop_without_fake_removal_or_idle(self):
        node_id, controller, original, claim = self.lost()
        result = controller.tick()["nodes"][0]
        self.assertEqual(result["code"], "capacity_no_progress")
        self.assertEqual(len(self.client.stopped), 1)
        job = self.repo.get_job(self.scope, original["id"])
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["created_at"], original["created_at"])
        node, intent = self.node(node_id)
        binding = node["payload"]["dstack"]
        self.assertTrue(binding["stop_started"])
        self.assertIsNone(binding.get("provider_removal_proof"))
        with self.repo.engine.connect() as connection:
            old = (
                connection.execute(
                    select(attempts).where(attempts.c.id == claim.lease.attempt_id)
                )
                .mappings()
                .one()
            )
            self.assertEqual(
                (old["status"], old["upstream_stopped"]), ("attempt_lost", 0)
            )
            self.assertTrue(fenced_node_stop(connection, node, intent))
            self.assertTrue(_ended_for_replacement(connection, node))
        self.assertFalse(_ended_with_proof(node))

    def test_alerts_and_node_stop_event_are_not_repeated_by_polling(self):
        node_id, controller, original, claim = self.lost()
        report = Mock()
        telemetry = Mock()
        controller.reclaim_reporter = report
        controller.telemetry = telemetry
        controller.tick()
        controller.tick()
        self.assertEqual(report.call_count, 1)
        self.assertEqual(telemetry.start.call_count, 1)
        with self.repo.engine.connect() as connection:
            events = list(
                connection.execute(
                    select(outbox).where(
                        outbox.c.event_type == "capacity.node_replacement_requested"
                    )
                ).mappings()
            )
        self.assertEqual(len(events), 1)
        self.assertEqual(len(self.client.stopped), 1)

    def test_startup_timeout_stops_unready_node_without_waiting_for_native_ready(self):
        node_id = self.start()["node_id"]
        self.now += 1201
        controller = self.controller()
        result = controller.tick()["nodes"][0]
        self.assertEqual(result["code"], "capacity_startup_timeout")
        self.assertEqual(len(self.client.stopped), 1)
        node, intent = self.node(node_id)
        self.assertIsNone(node["payload"]["dstack"].get("first_runtime_ready_at"))
        self.assertIsNone(node["payload"]["dstack"].get("provider_removal_proof"))

    def test_marker_alone_cannot_authorize_stop_of_unfenced_attempt(self):
        node_id, controller, original, claim = self.lost()
        node, intent = self.node(node_id)
        node["payload"]["capacity_watchdog"] = {
            "version": 1,
            "reason": "capacity_no_progress",
            "at": self.now,
            "event_ids": ["invented"],
        }
        with self.repo.engine.connect() as connection:
            self.assertFalse(fenced_node_stop(connection, node, intent))
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "running"
        )

    def test_retirement_releases_device_epoch_without_freeing_present_paid_node(self):
        node_id, controller, original, claim = self.lost()
        with self.repo.engine.connect() as connection:
            before = connection.execute(select(registered_workers)).mappings().one()
        controller.tick()
        with self.repo.engine.connect() as connection:
            worker = connection.execute(select(registered_workers)).mappings().one()
            self.assertEqual(worker["state"], "retired")
            self.assertEqual(worker["fence"], before["fence"] + 1)
            self.assertEqual(
                set(connection.execute(select(registered_devices.c.state)).scalars()),
                {"released"},
            )
            # Supplier liability and the physical intent remain until removal.
            self.assertEqual(self.repo._global_usage(connection)["instances"], 1)
        with self.repo.transaction() as connection:
            # Test-only positive original-node destruction; no provider call.
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node_id)
                .values(state="destroyed")
            )
        with self.repo.engine.connect() as connection:
            self.assertEqual(self.repo._global_usage(connection)["instances"], 0)
            self.assertEqual(self.repo._global_usage(connection)["physical_gpus"], 0)

    def test_worker_is_locked_before_guard_and_winning_claim_defers_stop(self):
        node_id, controller, original, claim = self.lost()
        other = self.job(key="claim-won")
        locked = []
        original_lock = self.repo._locked
        original_guard = fenced_node_stop

        def lock(connection, query):
            row = original_lock(connection, query)
            if "FROM platform_registered_workers" in str(query) and row:
                locked.append(row["id"])
            return row

        def competing_claim(connection, node, intent, **kwargs):
            self.assertEqual(locked, ["dstack-test-slot"])
            connection.execute(
                update(jobs)
                .where(jobs.c.id == other["id"])
                .values(
                    status="claimed",
                    lease_worker_id="dstack-test-slot",
                    lease_expires_at=self.now + 90,
                )
            )
            return original_guard(connection, node, intent, **kwargs)

        node, intent = self.node(node_id)
        with (
            patch.object(self.repo, "_locked", side_effect=lock),
            patch(
                "studio_platform.capacity_watchdog.fenced_node_stop",
                side_effect=competing_claim,
            ),
        ):
            result = reclaim_node(self.service, node, intent)
        self.assertFalse(result["stop_requested"])
        self.assertEqual(self.node(node_id)[0]["desired_state"], "running")
        self.assertEqual(self.client.stopped, [])

    def test_progress_race_skips_one_observation_but_history_errors_remain_visible(
        self,
    ):
        node_id, controller, original, claim = self.lost()
        node, intent = self.node(node_id)
        with patch.object(
            TaskQueue,
            "reclaim_capacity",
            side_effect=Conflict("capacity_reclaim_progress_changed"),
        ):
            self.assertFalse(reclaim_node(self.service, node, intent)["stop_requested"])
        with patch.object(
            TaskQueue,
            "reclaim_capacity",
            side_effect=Conflict("capacity_reclaim_history_invalid"),
        ):
            with self.assertRaisesRegex(Conflict, "history_invalid"):
                reclaim_node(self.service, node, intent)

    def test_failed_export_stays_pending_and_stopped_node_retries_original_event(self):
        node_id, controller, original, claim = self.lost()
        reporter = Mock(side_effect=[RuntimeError("export unavailable"), "accepted-id"])
        controller.reclaim_reporter = reporter
        controller.tick()
        marker = self.node(node_id)[0]["payload"]["capacity_watchdog"]
        self.assertEqual(marker.get("reported_event_ids", []), [])
        original_event = marker["event_ids"][0]
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(runtime_state="stopped")
            )
        controller.tick()
        marker = self.node(node_id)[0]["payload"]["capacity_watchdog"]
        self.assertEqual(marker["reported_event_ids"], [original_event])
        self.assertEqual(reporter.call_count, 2)
        self.assertEqual(reporter.call_args.args[0]["event_id"], original_event)
        self.assertEqual(len(self.client.stopped), 1)
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "queued"
        )

    def test_enabled_grafana_failure_does_not_ack_and_disabled_exporters_are_inert(
        self,
    ):
        node_id, controller, original, claim = self.lost()
        telemetry = Mock(enabled=True)
        telemetry.start.return_value.finish.side_effect = [None, 0.0]
        telemetry.force_flush.return_value = True
        controller.telemetry = telemetry
        controller.tick()
        self.assertEqual(
            self.node(node_id)[0]["payload"]["capacity_watchdog"].get(
                "reported_event_ids", []
            ),
            [],
        )
        report_reclaims(self.service, node_id, None, telemetry)
        marker = self.node(node_id)[0]["payload"]["capacity_watchdog"]
        self.assertEqual(marker["reported_event_ids"], marker["event_ids"])
        self.assertEqual(telemetry.start.call_count, 2)

    def test_fresh_exact_removed_node_requeues_before_timeout_without_settling_cost(
        self,
    ):
        node_id, controller, original, claim = self.lost()
        self.now = 1001
        node, intent = self.node(node_id)
        binding = node["payload"]["dstack"]
        binding.update(
            state="stopped",
            dstack_status="terminated",
            provider_removal_proof={
                "version": 1,
                "backend": binding["backend"],
                "run_id": binding["run_id"],
                "provider_instance_id": intent["provider_instance_id"],
                "observed_at": self.now,
                "inventory_observed_at": self.now,
                "complete": True,
                "absent": True,
                "inventory_sha256": "a" * 64,
            },
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=node["payload"], runtime_state="stopped")
            )
        result = reclaim_node(self.service, node, intent)
        self.assertEqual(result["reason"], "capacity_provider_removed")
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "queued"
        )
        with self.repo.engine.connect() as connection:
            old = (
                connection.execute(
                    select(attempts).where(attempts.c.id == claim.lease.attempt_id)
                )
                .mappings()
                .one()
            )
        self.assertEqual(
            (old["upstream_stopped"], old["actual_cost_microusd"]), (0, None)
        )

    def test_paid_idle_host_failure_uses_owned_stop_and_keeps_original_queue(self):
        from studio_platform.capacity_health import check
        from studio_platform.telemetry import NullTelemetry

        node_id, controller = self._continuing_ready()
        queued = self._queued_native("fl")
        original = self.repo.get_job(self.scope, queued["id"])
        self.now += 601
        node, intent = self.node(node_id)
        node["payload"]["dstack"].update(
            reason_code="runtime_process_exited",
            observed_at=self.now,
            state="runtime_unconfirmed",
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=node["payload"])
            )
        result = check(self.service, NullTelemetry())
        self.assertEqual(result["stop_candidates"], [node_id])
        self.assertTrue(stop_unserved_node(self.service, node_id))
        self.assertEqual(self.node(node_id)[0]["desired_state"], "stopped")
        controller.tick()
        self.assertEqual(len(self.client.stopped), 1)
        self.assertEqual(self.repo.get_job(self.scope, queued["id"]), original)

    def test_removed_idle_retired_worker_releases_only_its_ghost_device_once(self):
        node_id, controller = self.ready()
        self.now += 1
        node, intent = self.node(node_id)
        binding = node["payload"]["dstack"]
        binding.update(
            state="stopped",
            dstack_status="terminated",
            provider_removal_proof={
                "version": 1,
                "backend": binding["backend"],
                "run_id": binding["run_id"],
                "provider_instance_id": intent["provider_instance_id"],
                "observed_at": self.now,
                "inventory_observed_at": self.now,
                "complete": True,
                "absent": True,
                "inventory_sha256": "a" * 64,
            },
        )
        with self.repo.transaction() as connection:
            worker = dict(
                connection.execute(select(registered_workers)).mappings().one()
            )
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == worker["id"])
                .values(state="retired")
            )
            other = {
                **worker,
                "id": "unrelated-worker",
                "instance_id": "7654321",
                "state": "unknown",
            }
            connection.execute(insert(registered_workers).values(**other))
            connection.execute(
                insert(registered_devices).values(
                    provider="vast",
                    instance_id="7654321",
                    gpu_id="other-gpu",
                    worker_id="unrelated-worker",
                    state="owned",
                )
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=node["payload"], runtime_state="stopped")
            )
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node_id)
                .values(state="destroyed")
            )
        budget = self.repo.get_budget("owner-budget")
        first = reclaim_node(self.service, node, intent)
        self.assertTrue(first["stop_requested"])
        reclaim_node(self.service, self.node(node_id)[0], intent)
        with self.repo.engine.connect() as connection:
            after = (
                connection.execute(
                    select(registered_workers).where(
                        registered_workers.c.id == worker["id"]
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(after["fence"], worker["fence"])
            self.assertEqual(after["updated_at"], worker["updated_at"])
            devices = {
                row.worker_id: row.state
                for row in connection.execute(select(registered_devices))
            }
            self.assertEqual(
                devices, {worker["id"]: "released", "unrelated-worker": "owned"}
            )
            self.assertEqual(self.repo._global_usage(connection)["instances"], 1)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)

    def test_old_running_host_fault_marker_cannot_authorize_new_stop(self):
        from studio_platform.capacity_health import check
        from studio_platform.telemetry import NullTelemetry

        node_id, controller = self._continuing_ready()
        self._queued_native("fl")
        self.now += 601
        node, intent = self.node(node_id)
        node["payload"]["dstack"].update(
            reason_code="runtime_process_exited",
            observed_at=self.now,
            state="runtime_unconfirmed",
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=node["payload"])
            )
        self.assertEqual(
            check(self.service, NullTelemetry())["stop_candidates"], [node_id]
        )
        node, intent = self.node(node_id)
        event = node["payload"]["capacity_health"]["capacity_paid_idle_queue"][
            "event_id"
        ]
        node["payload"]["capacity_watchdog"] = {
            "version": 1,
            "reason": "capacity_dispatch_unavailable",
            "at": self.now,
            "event_ids": [],
            "health_event_id": event,
        }
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=node["payload"])
            )
        self.now += 121
        self.assertFalse(stop_unserved_node(self.service, node_id))
        self.assertEqual(self.node(node_id)[0]["desired_state"], "running")
        self.assertEqual(self.client.stopped, [])
