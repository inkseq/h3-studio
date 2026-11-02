"""Characterize the mixed slot order and finite-turn ownership before moving it."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from studio_platform.dbos_transport import DurableSlots, _Slot
from studio_platform.repository import Conflict


class LocalSlotBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.job = dict(
            status="queued",
            pool="shared",
            lease_worker_id=None,
            not_before=100,
            lease_expires_at=None,
        )
        self.repo = Mock()
        self.repo.clock.return_value = 100
        self.repo.get_job.side_effect = lambda *_: dict(self.job)
        self.slots = DurableSlots(self.repo, Mock(), Path(self.folder.name))
        self.calls = []

    def slot(self, name, backend="wangp-worker", provider="vast"):
        spec = SimpleNamespace(
            worker_id=name,
            backend=backend,
            provider=provider,
            pool="shared",
            dispatch_backend="legacy",
        )
        runner = Mock()
        runner.selected_job_id = None
        runner.backend._operation_fence_fd = None
        runner.control.get.return_value = dict(
            expires_at=1000,
            state="ready",
            fence=7,
        )
        runner.run_once.side_effect = lambda *_: self.calls.append(name) or {}
        context = Mock()
        context.__exit__ = Mock()
        item = _Slot(spec, runner, context, threading.Lock())
        self.slots._slots[name] = item
        return item

    def turn(self):
        return self.slots.turn("tenant", "owner", "project", "original-job")

    def test_mixed_insertion_order_is_not_regrouped_by_owner(self):
        self.slot("local-1", "runpod-enhance", "runpod")
        self.slot("native-1")
        self.slot("modal", provider="modal")
        self.slot("local-2", "everyframe-render", "everyframe")
        self.slot("native-2")
        self.assertEqual(self.turn(), dict(state="queued", done=False, wait_seconds=10))
        self.assertEqual(
            self.calls, ["local-1", "native-1", "local-2", "native-2", "modal"]
        )
        self.assertEqual(self.repo.get_job.call_count, 2)
        self.assertEqual(list(self.slots.children), ["native-1", "modal", "native-2"])

    def test_original_local_worker_runs_first_and_stops_at_same_job(self):
        self.slot("native")
        self.slot("local-1", "runpod-enhance", "runpod")
        original = self.slot("local-2", "everyframe-render", "everyframe")
        self.job["lease_worker_id"] = "local-2"
        original.runner.run_once.side_effect = lambda *_: (
            self.calls.append("local-2") or {"job_id": "original-job"}
        )
        self.turn()
        self.assertEqual(self.calls, ["local-2"])
        self.assertEqual(self.repo.get_job.call_count, 2)
        self.assertIsNone(original.runner.selected_job_id)

    def test_local_record_injected_in_live_map_is_heartbeaten_and_removed(self):
        local = self.slot("local", "everyframe-render", "everyframe")
        native = self.slot("native")
        self.slots.heartbeat_cpu()
        local.runner.control.heartbeat.assert_called_once_with(
            "local", 7, lease_seconds=3600
        )
        native.runner.control.get.assert_not_called()
        self.slots.stop("local")
        local.context.__exit__.assert_called_once_with(None, None, None)
        self.assertEqual(list(self.slots._slots), ["native"])

    def test_cpu_failures_clear_selection_and_fences_in_original_order(self):
        for fault in ("runner", "heartbeat", "wrong-job"):
            with self.subTest(fault=fault):
                self.slots._slots.clear()
                item = self.slot("cpu", "cpu-render", "local-cpu")
                events = []

                @contextmanager
                def fence():
                    events.append("fence-open")
                    try:
                        yield True, 991
                    finally:
                        self.assertIsNone(item.runner.backend._operation_fence_fd)
                        events.append("fence-close")

                def run(*_):
                    self.assertEqual(item.runner.selected_job_id, "original-job")
                    self.assertEqual(item.runner.backend._operation_fence_fd, 991)
                    self.assertTrue(self.slots.local.operation_lock.locked())
                    self.assertTrue(item.lock.locked())
                    events.append("run")
                    if fault == "runner":
                        raise OSError("synthetic-turn-failure")
                    return {"job_id": "different-job"}

                def heartbeat(*_, **__):
                    self.assertIsNone(item.runner.backend._operation_fence_fd)
                    self.assertEqual(item.runner.selected_job_id, "original-job")
                    self.assertTrue(self.slots.local.operation_lock.locked())
                    self.assertTrue(item.lock.locked())
                    events.append("heartbeat")
                    if fault == "heartbeat":
                        raise OSError("synthetic-heartbeat-failure")

                item.runner.run_once.side_effect = run
                item.runner.control.heartbeat.side_effect = heartbeat
                with patch.object(self.slots.local, "cpu_fence", side_effect=fence):
                    with self.assertRaises(
                        Conflict if fault == "wrong-job" else OSError
                    ):
                        self.turn()
                self.assertEqual(
                    events,
                    ["fence-open", "run", "fence-close"]
                    + ([] if fault == "runner" else ["heartbeat"]),
                )
                self.assertIsNone(item.runner.selected_job_id)
                self.assertIsNone(item.runner.backend._operation_fence_fd)
                self.assertFalse(self.slots.local.operation_lock.locked())
                self.assertFalse(item.lock.locked())

    def test_mixed_close_failure_keeps_remaining_order_and_supervisor_fence(self):
        with self.slots:
            first = self.slot("local", "runpod-enhance", "runpod")
            second = self.slot("native")
            third = self.slot("last", "everyframe-render", "everyframe")
            events = []
            first.context.__exit__.side_effect = lambda *_: events.append("local")
            second.context.__exit__.side_effect = TimeoutError("synthetic-close")
            third.context.__exit__.side_effect = lambda *_: events.append("last")
            with self.assertRaises(TimeoutError):
                self.slots.close()
            self.assertEqual(events, ["local"])
            self.assertEqual(list(self.slots._slots), ["native", "last"])
            self.assertIsNotNone(self.slots._fence)
            self.assertFalse(second.lock.locked())
            self.assertTrue(self.slots._closing)
            self.assertEqual(
                self.turn(),
                dict(state="executor_stopping", done=False, wait_seconds=10),
            )
            second.context.__exit__.side_effect = lambda *_: events.append("native")
        self.assertEqual(events, ["local", "native", "last"])
        self.assertEqual(self.slots._slots, {})
        self.assertIsNone(self.slots._fence)

    def test_local_owner_runs_with_original_contexts_without_dbos_executor(self):
        from studio_platform.local_slots import LocalSlotOwner
        from studio_platform.slot_turns import SlotContexts, selected_job_turn

        self.job["pool"] = "cpu-render"
        runners = []

        @contextmanager
        def runner_context(config, worker_id, settings, *, repository):
            self.assertIs(repository, self.repo)
            self.assertEqual(config.work_dir, Path(self.folder.name))
            runner = Mock()
            runner.selected_job_id = None
            runner.control.get.return_value = dict(
                expires_at=1000, state="ready", fence=7
            )
            runner.run_once.return_value = {"job_id": "original-job"}
            runners.append(runner)
            yield runner

        settings = SimpleNamespace(render_enabled=True)
        with (
            patch.dict("sys.modules", {"dbos": None}),
            SlotContexts(
                self.repo, settings, self.folder.name, runner_context=runner_context
            ) as contexts,
        ):
            local = LocalSlotOwner(contexts)
            local.add_slots()
            self.assertEqual(len(contexts._slots), 4)
            self.assertIs(local.contexts._slots, contexts._slots)
            result = selected_job_turn(
                contexts,
                "tenant",
                "owner",
                "project",
                "original-job",
                run_selected=local.run_selected_slot,
            )
            self.assertFalse(result["done"])
            self.assertEqual(sum(r.run_once.call_count for r in runners), 1)
            self.assertEqual(self.repo.get_job.call_count, 2)
        self.assertEqual(contexts._slots, {})


if __name__ == "__main__":
    unittest.main()
