"""A finished run whose result collection fails: hold or reclaim, never stuck.

Staging job 27b6213f stayed collecting/collection_failed for hours after its
machine was destroyed. Disposable ledger only; no provider, storage or GPU.
"""

import json

from sqlalchemy import insert, select, update

from studio_platform import capacity_health
from studio_platform.artifact_writer import write_receipts
from studio_platform.capacity_collect import (
    COLLECT_FAILURE_LIMIT,
    COLLECT_STUCK_SECONDS,
    output_stored,
)
from studio_platform.capacity_watchdog import reclaim_node
from studio_platform.error_monitoring import sanitized_event
from studio_platform.operator_capacity import operator_nodes
from studio_platform.queue import TaskQueue
from studio_platform.repository import attempts, outbox, registered_workers
from test_platform_repository import LedgerCase
import test_dstack_controller as controllers
import test_dstack_operator as operators

WORKER = "dstack-test-slot"


class CollectionRecoveryTests(LedgerCase):
    preview = operators.DstackOperatorTests.preview
    start = operators.DstackOperatorTests.start
    running = operators.DstackOperatorTests.running
    controller = controllers.DstackControllerTests.controller
    ready = controllers.DstackControllerTests.ready
    consumer = controllers.DstackControllerTests.consumer

    def setUp(self):
        super().setUp()
        operators.DstackOperatorTests._setup_dstack(self)

    def rows(self, node_id):
        from studio_platform.repository import instance_intents

        with self.repo.engine.connect() as connection:
            node = dict(
                connection.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == node_id)
                )
                .mappings()
                .one()
            )
            intent = dict(
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == node_id)
                )
                .mappings()
                .one()
            )
        return node, intent

    def attempt(self, attempt_id):
        with self.repo.engine.connect() as connection:
            return dict(
                connection.execute(select(attempts).where(attempts.c.id == attempt_id))
                .mappings()
                .one()
            )

    def worker(self):
        with self.repo.engine.connect() as connection:
            return dict(
                connection.execute(
                    select(registered_workers).where(registered_workers.c.id == WORKER)
                )
                .mappings()
                .one()
            )

    def collecting(self, failures=1):
        """The run finished, collection began and failed; the worker keeps it."""
        node_id, controller = self.ready()
        job = self.job()
        queue = TaskQueue(self.repo)
        claim = queue.claim(WORKER, "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "original-task")
        queue.begin_collection(claim.lease)
        queue.collection_failed(claim.lease)
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == claim.lease.attempt_id)
                .values(collection_failures=failures, updated_at=self.now)
            )
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == WORKER)
                .values(state="busy", current_job_id=job["id"])
            )
        return node_id, controller, job, claim

    def remove(self, node_id):
        node, intent = self.rows(node_id)
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
        return self.rows(node_id)

    def receipt(self, job, attempt_id, *, statuses, phase="reserved"):
        write_receipts.metadata.create_all(self.repo.engine)
        record = {
            "phase": phase,
            "collection_route": "gpu-r2-v1",
            "expected_roles": sorted(statuses),
            "roles": {kind: {"status": value} for kind, value in statuses.items()},
        }
        with self.repo.transaction() as connection:
            connection.execute(
                insert(write_receipts).values(
                    id="r" * 64,
                    tenant=job["tenant_id"],
                    owner=job["owner_id"],
                    job_id=job["id"],
                    attempt_id=attempt_id,
                    version=1,
                    record=json.dumps(record),
                )
            )

    def assert_requeued(self, job, claim, reason):
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual((current["status"], current["error_code"]), ("queued", reason))
        self.assertIsNone(current["current_attempt_id"])
        self.assertEqual(current["result"]["capacity_reclaim_count"], 1)
        lost = self.attempt(claim.lease.attempt_id)
        self.assertEqual((lost["status"], lost["error_code"]), ("attempt_lost", reason))
        # The lost attempt's supplier cost stays unknown, never settled at zero.
        self.assertIsNone(lost["actual_cost_microusd"])
        self.assertIsNone(self.worker()["current_job_id"])
        with self.repo.engine.connect() as connection:
            events = list(
                connection.execute(
                    select(outbox.c.payload).where(
                        outbox.c.event_type == "capacity.attempt_lost",
                        outbox.c.aggregate_id == job["id"],
                    )
                ).scalars()
            )
        self.assertEqual([event["reason"] for event in events], [reason])
        self.assertEqual(events[0]["state"], "requeued")

    def test_removed_host_requeues_a_failed_collection_and_frees_the_worker(self):
        node_id, _, job, claim = self.collecting(failures=3)
        self.now += 60
        node, intent = self.remove(node_id)
        result = reclaim_node(self.service, node, intent)
        self.assertEqual(result["reason"], "capacity_provider_removed")
        self.assert_requeued(job, claim, "capacity_provider_removed")
        self.assertEqual(self.worker()["state"], "retired")

    def test_failures_on_a_live_machine_are_bounded_then_reclaimed_and_stopped(self):
        node_id, controller, job, claim = self.collecting(failures=3)
        self.now += 60
        controller.tick()
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["status"], "collecting"
        )
        self.assertEqual(self.client.stopped, [])
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == claim.lease.attempt_id)
                .values(collection_failures=COLLECT_FAILURE_LIMIT, updated_at=self.now)
            )
        result = controller.tick()["nodes"][0]
        self.assertEqual(result["code"], "capacity_collection_failed")
        self.assert_requeued(job, claim, "capacity_collection_failed")
        self.assertEqual(len(self.client.stopped), 1)

    def test_a_failed_collection_nobody_retries_is_reclaimed_after_ten_minutes(self):
        node_id, controller, job, claim = self.collecting(failures=1)
        self.now += 599
        controller.tick()
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["status"], "collecting"
        )
        self.now += 2
        self.assertEqual(
            controller.tick()["nodes"][0]["code"], "capacity_collection_failed"
        )
        self.assert_requeued(job, claim, "capacity_collection_failed")

    def test_a_running_collect_turn_is_never_interrupted(self):
        node_id, _, job, claim = self.collecting(failures=COLLECT_FAILURE_LIMIT)
        self.now += 31  # Past the collection retry backoff.
        turn = TaskQueue(self.repo).claim(
            WORKER, "test-pool", purpose="collect", lease_seconds=900
        )
        self.assertIsNotNone(turn)
        self.now += 60
        node, intent = self.remove(node_id)
        reclaim_node(self.service, node, intent)
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["status"], "collecting"
        )
        self.assertEqual(self.attempt(claim.lease.attempt_id)["status"], "collecting")

    def test_stored_output_is_held_and_alerted_never_regenerated(self):
        node_id, _, job, claim = self.collecting(failures=2)
        self.receipt(job, claim.lease.attempt_id, statuses={"video": "verified"})
        self.now += 60
        node, intent = self.remove(node_id)
        result = reclaim_node(self.service, node, intent)
        self.assertEqual(result["receipts"], [])
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(current["status"], "collecting")
        self.assertEqual(self.attempt(claim.lease.attempt_id)["status"], "collecting")
        # Visible: one alert per episode once it has been stuck for 15 minutes.
        self.now += COLLECT_STUCK_SECONDS
        with self.repo.engine.connect() as connection:
            findings, _ = capacity_health.snapshot(self.service, connection, self.now)
        stuck = [f for f in findings if f["code"] == "capacity_collect_stuck"]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(
            (stuck[0]["resource_id"], stuck[0]["detail"]),
            (job["id"], "collection_output_stored"),
        )
        self.assertIsNotNone(capacity_health.record(self.service, stuck[0]))
        self.assertIsNone(capacity_health.record(self.service, stuck[0]))
        event = sanitized_event(
            {
                "message": "capacity_health_alert",
                "event_id": "b" * 32,
                "tags": {
                    "code": "capacity_collect_stuck",
                    "diagnosis": "collect_stuck",
                    "detail": "collection_output_stored",
                },
            },
            {},
        )
        self.assertEqual(event["tags"]["code"], "capacity_collect_stuck")

    def test_only_fully_verified_or_collector_staged_output_counts_as_stored(self):
        _, _, job, claim = self.collecting()
        attempt_id = claim.lease.attempt_id
        with self.repo.engine.connect() as connection:
            self.assertFalse(output_stored(connection, job["id"], attempt_id))
        self.receipt(
            job, attempt_id, statuses={"video": "verified", "audio": "putting"}
        )
        with self.repo.engine.connect() as connection:
            # The host was the only writer of the unknown upload.
            self.assertFalse(output_stored(connection, job["id"], attempt_id))
        with self.repo.transaction() as connection:
            connection.execute(
                update(write_receipts).values(
                    record=json.dumps({"phase": "reserved", "roles": {}})
                )
            )
        with self.repo.engine.connect() as connection:
            # CPU delivery: verified files already staged on the collector.
            self.assertTrue(output_stored(connection, job["id"], attempt_id))

    def test_reclaim_sentry_message_accepts_the_new_reason(self):
        event = sanitized_event(
            {
                "message": "capacity_attempt_reclaimed",
                "event_id": "c" * 32,
                "tags": {"reason": "capacity_collection_failed", "reclaim_count": "1"},
            },
            {},
        )
        self.assertEqual(event["tags"]["reason"], "capacity_collection_failed")

    def test_alert_is_not_raised_for_a_healthy_or_young_collection(self):
        _, _, job, _ = self.collecting()
        self.now += COLLECT_STUCK_SECONDS - 5
        with self.repo.engine.connect() as connection:
            findings, _ = capacity_health.snapshot(self.service, connection, self.now)
        self.assertFalse([f for f in findings if f["code"] == "capacity_collect_stuck"])
