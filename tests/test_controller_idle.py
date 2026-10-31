"""Only proven empty capacity may slow historical cancellation maintenance."""

from unittest.mock import Mock

from sqlalchemy import insert, update

from studio_platform.controller_idle import IdleCadence
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import attempts, jobs
import test_dstack_passive_review as fixture


class IdleCadenceTests(fixture.PassiveReviewTests):
    def history(self):
        self.unknown()
        node_id = self.start(key="history-node")["node_id"]
        job = self.job()
        with self.repo.transaction() as connection:
            payload = self.node(node_id)["payload"]
            payload["dstack"].update(state="stopped", dstack_status="failed")
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    runtime_state="stopped", desired_state="stopped", payload=payload
                )
            )
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    status="cancel_requested",
                    current_attempt_id="original-attempt",
                    execution_plan={
                        **job["execution_plan"],
                        "dispatch_backend": "hatchet-v1",
                    },
                )
            )
            connection.execute(
                insert(attempts).values(
                    id="original-attempt",
                    job_id=job["id"],
                    number=1,
                    status="running",
                    fence=1,
                    worker_id="dstack-" + node_id.replace("-", ""),
                    created_at=self.now,
                    updated_at=self.now,
                    submission_started_at=self.now,
                    upstream_task_id="original-native",
                    upstream_stopped=0,
                )
            )
        return job, node_id

    def test_empty_capacity_slows_maintenance_without_settling_originals(self):
        self.history()
        before = self.ledger()
        cadence = IdleCadence()
        self.assertTrue(self.controller.has_demand())
        self.assertEqual(cadence.interval(self.controller), 60)
        self.assertEqual(self.ledger(), before)
        calls = self.client.provider_inventory.call_count
        self.assertEqual(cadence.interval(self.controller), 60)
        self.assertEqual(self.client.provider_inventory.call_count, calls)
        self.now += 60
        self.assertEqual(cadence.interval(self.controller), 60)
        self.assertEqual(self.client.provider_inventory.call_count, calls + 1)
        self.assertEqual(self.ledger(), before)

    def test_new_queued_work_bypasses_cached_empty_capacity_immediately(self):
        job, _ = self.history()
        cadence = IdleCadence()
        self.assertEqual(cadence.interval(self.controller), 60)
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="queued")
            )
        self.assertEqual(cadence.interval(self.controller), 15)

    def test_cached_inventory_never_extends_its_original_maintenance_deadline(self):
        self.history()
        cadence = IdleCadence()
        self.assertEqual(cadence.interval(self.controller), 60)
        self.now += 50
        self.assertEqual(cadence.interval(self.controller), 10)

    def test_every_represented_backend_must_have_confirmed_empty_inventory(self):
        _, node_id = self.history()
        with self.repo.transaction() as connection:
            payload = self.node(node_id)["payload"]
            payload["dstack"]["backend"] = "runpod"
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=payload)
            )

        def inventory(backend):
            if backend == "runpod":
                raise RuntimeError("unconfirmed")
            return {
                "backend": backend,
                "complete": True,
                "instance_ids": [],
                "observed_at": self.now,
            }

        self.client.provider_inventory = Mock(side_effect=inventory)
        self.assertEqual(IdleCadence().interval(self.controller), 15)
        self.client.provider_inventory.assert_called_once_with("runpod")

    def test_live_lease_node_or_cleanup_keeps_fast_stop_cadence(self):
        job, node_id = self.history()
        cadence = IdleCadence()
        self.assertEqual(cadence.interval(self.controller), 60)
        self.controller._cleanup_pending.add(node_id)
        self.assertEqual(cadence.interval(self.controller), 15)
        self.controller._cleanup_pending.clear()
        self.processes.children[node_id] = object()
        self.assertEqual(cadence.interval(self.controller), 15)
        self.processes.children.clear()
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(lease_expires_at=self.now + 30)
            )
        self.assertEqual(cadence.interval(self.controller), 15)
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(lease_expires_at=None)
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(runtime_state="ready")
            )
        self.assertEqual(cadence.interval(self.controller), 15)

    def test_unconfirmed_or_nonempty_provider_inventory_fails_fast(self):
        self.history()
        for inventory in (
            RuntimeError("private error"),
            {
                "backend": "vastai",
                "complete": False,
                "instance_ids": [],
                "observed_at": self.now,
            },
            {
                "backend": "vastai",
                "complete": True,
                "instance_ids": ["123"],
                "observed_at": self.now,
            },
            {
                "backend": "vastai",
                "complete": True,
                "instance_ids": [],
                "observed_at": self.now - 31,
            },
        ):
            self.client.provider_inventory = (
                Mock(side_effect=inventory)
                if isinstance(inventory, Exception)
                else Mock(return_value=inventory)
            )
            self.assertEqual(IdleCadence().interval(self.controller), 15)

    def test_unknown_unbound_attempt_cannot_back_off(self):
        self.history()
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == "original-attempt")
                .values(worker_id="missing")
            )
        self.assertEqual(IdleCadence().interval(self.controller), 15)

    def test_changed_command_invalidates_cached_terminal_inventory_evidence(self):
        self.history()
        cadence = IdleCadence()
        self.assertEqual(cadence.interval(self.controller), 60)
        node = next(
            n
            for n in self.controller._nodes()
            if n["runtime_state"] == "creation_unknown"
        )
        self.service.node_command(self.owner, node["intent_id"], {}, "new-stop")
        self.client.get = Mock(side_effect=RuntimeError("unconfirmed terminal run"))
        self.assertEqual(cadence.interval(self.controller), 15)
        self.assertIn(
            cadence.reason, {"node_evidence_unconfirmed", "run_evidence_unconfirmed"}
        )

    def test_health_deadline_is_not_postponed_by_maintenance_wait(self):
        self.history()
        self.controller._health_checked_at = self.now - 170
        self.assertEqual(IdleCadence().interval(self.controller), 10)
