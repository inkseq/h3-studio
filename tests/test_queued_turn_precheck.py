"""A queued job that cannot claim never takes the slot lock (#1199).

Every queued job polls every slot of its pool. Each poll used to run a full
worker turn under the slot lock, so with a long queue the one job that could
claim often lost the non-blocking lock to a job that could not, and a ready
GPU sat idle. The read-only check must agree with what the claim would pick.
"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import select, update

from studio_platform import capacity_reasons
from studio_platform.control import WorkerControl
from studio_platform.repository import instance_intents, registered_workers
from studio_platform.slot_turns import selected_job_turn
from test_operator_capacity import OperatorCase


class SpyLock:
    def __init__(self):
        self._lock, self.acquired = threading.Lock(), 0

    def acquire(self, blocking=True):
        taken = self._lock.acquire(blocking=blocking)
        self.acquired += bool(taken)
        return taken

    def release(self):
        self._lock.release()


class QueuedTurnPrecheckTests(OperatorCase):
    def prepare(self, count=3, *, duration=120):
        self.create()
        self.seed_running()
        self.control = WorkerControl(self.repo)
        with self.repo.engine.connect() as conn:
            self.worker = dict(
                conn.execute(select(registered_workers)).mappings().one()
            )
        self.jobs = []
        for index in range(count):
            plan = self.repo.create_plan(
                self.scope,
                {
                    "recipe_id": self.binding.recipe_ids[0],
                    "deployment_profile_id": self.binding.runtime_profile_id,
                    "request": {"model": self.binding.model_id, "seed": index},
                },
                {
                    "pool": self.binding.pool,
                    "backend": "wangp-worker",
                    "enabled": True,
                    "configuration_id": self.binding.configuration_id,
                    "engine_manifest_digest": self.binding.engine_manifest_digest,
                    "expected_runtime_s": duration,
                },
                expires_at=self.now + 9000,
                estimated_cost_microusd=100,
            )
            self.now += 1
            self.jobs.append(
                self.repo.create_job(
                    self.scope,
                    plan["id"],
                    "queued-%d" % index,
                    budget_account_ids=["owner-budget"],
                )
            )

    def refusal(self, job):
        with self.repo.engine.connect() as conn:
            return self.control.queued_turn_refusal(
                conn, self.worker["id"], self.repo.get_job(self.scope, job["id"])
            )

    def turn(self, job, run_selected):
        spec = SimpleNamespace(
            pool=self.binding.pool,
            worker_id=self.worker["id"],
            provider="lium",
            backend="wangp-worker",
            dispatch_backend="legacy",
        )
        slot = SimpleNamespace(
            spec=spec, lock=SpyLock(), runner=SimpleNamespace(control=self.control)
        )
        contexts = SimpleNamespace(
            repo=self.repo,
            _closing=False,
            _lock=threading.RLock(),
            _slots={"slot": slot},
            _retiring=set(),
        )
        result = selected_job_turn(
            contexts,
            self.scope.tenant_id,
            self.scope.owner_id,
            self.scope.project_id,
            job["id"],
            run_selected=run_selected,
        )
        return slot, result

    def test_only_the_job_the_claim_would_pick_takes_the_turn(self):
        self.prepare()
        reasons = [self.refusal(job) for job in self.jobs]
        self.assertEqual(reasons.count(None), 1)
        first = self.jobs[reasons.index(None)]
        for job, reason in zip(self.jobs, reasons):
            if job is not first:
                self.assertEqual(reason, "not_first_in_order")
                # The real claim agrees and writes nothing for a later job.
                self.assertIsNone(
                    self.control.claim(
                        self.worker["id"], self.binding.pool, selected_job_id=job["id"]
                    )
                )
        claim = self.control.claim(
            self.worker["id"], self.binding.pool, selected_job_id=first["id"]
        )
        self.assertEqual(claim.job["id"], first["id"])
        # The worker is now busy: everyone else is refused without a turn.
        for job in self.jobs:
            if job is not first:
                self.assertEqual(self.refusal(job), "matching_worker_unavailable")
        # A job that is no longer plainly queued always takes the real turn.
        self.assertIsNone(self.refusal(first))

    def test_a_refused_job_never_takes_the_slot_lock_or_runs_the_worker(self):
        self.prepare()
        capacity_reasons.drain()
        first = next(job for job in self.jobs if self.refusal(job) is None)
        later = next(job for job in self.jobs if job is not first)
        run_selected = Mock(return_value=False)
        slot, result = self.turn(later, run_selected)
        run_selected.assert_not_called()
        self.assertEqual(slot.lock.acquired, 0)
        self.assertEqual(result, {"state": "queued", "done": False, "wait_seconds": 10})
        self.assertEqual(capacity_reasons.drain(), {"claim:not_first_in_order": 1})
        slot, _ = self.turn(first, run_selected)
        run_selected.assert_called_once_with(slot, first["id"])
        self.assertEqual(slot.lock.acquired, 1)

    def test_window_refusal_is_the_claim_reason(self):
        self.prepare(1, duration=60)
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).values(hard_deadline=self.now + 299))
        self.assertEqual(self.refusal(self.jobs[0]), "worker_window_closing")
        self.assertIsNone(self.control.claim(self.worker["id"], self.binding.pool))

    def test_backoff_is_refused_without_a_turn(self):
        self.prepare(1)
        from studio_platform.repository import jobs

        with self.repo.transaction() as conn:
            conn.execute(update(jobs).values(not_before=self.now + 30))
        self.assertEqual(self.refusal(self.jobs[0]), "queue_backoff")
        self.assertIsNone(self.control.claim(self.worker["id"], self.binding.pool))

    def test_states_whose_turn_writes_always_take_the_real_turn(self):
        self.prepare(2)
        later = next(job for job in self.jobs if self.refusal(job) is not None)
        # A stopping machine: the claim itself records the drain request.
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).values(state="draining"))
        self.assertIsNone(self.refusal(later))
        self.assertIsNone(
            self.control.claim(
                self.worker["id"], self.binding.pool, selected_job_id=later["id"]
            )
        )
        self.assertEqual(self.refusal(later), "matching_worker_unavailable")
        # An expired registration: the turn marks it unknown for reconciliation.
        with self.repo.transaction() as conn:
            conn.execute(
                update(registered_workers).values(
                    expires_at=self.now - 1, drain_requested=0
                )
            )
        self.assertIsNone(self.refusal(later))

    def test_a_failing_check_falls_back_to_the_old_turn(self):
        self.prepare(2)
        later = next(job for job in self.jobs if self.refusal(job) is not None)
        self.control.queued_turn_refusal = Mock(side_effect=RuntimeError("synthetic"))
        run_selected = Mock(return_value=False)
        slot, _ = self.turn(later, run_selected)
        run_selected.assert_called_once_with(slot, later["id"])
