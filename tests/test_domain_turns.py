"""Shutdown must drain real operations without waiting for durable sleeps."""

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from studio_platform.dbos_transport import DurableExecutor
from studio_platform.domain_turns import DomainTurns


class DomainTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_async_waiter_does_not_release_running_domain_call(self):
        turns = DomainTurns()
        started, finish, drained = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def operation():
            started.set()
            finish.wait(5)
            return "original receipt"

        task = asyncio.create_task(turns.call(operation))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        def close():
            turns.drain()
            drained.set()

        closer = threading.Thread(target=close)
        closer.start()
        try:
            self.assertFalse(await asyncio.to_thread(drained.wait, 0.05))
            finish.set()
            self.assertTrue(await asyncio.to_thread(drained.wait, 2))
        finally:
            finish.set()
            closer.join(2)
        refused = Mock()
        with self.assertRaises(asyncio.CancelledError):
            await turns.call(refused)
        refused.assert_not_called()

    async def test_errors_release_the_drain_and_preserve_original_exception(self):
        turns = DomainTurns()
        original = ValueError("domain failure")
        with self.assertRaises(ValueError) as raised:
            await turns.call(Mock(side_effect=original))
        self.assertIs(raised.exception, original)
        turns.drain()

    async def test_suspended_workflow_does_not_hold_a_domain_operation(self):
        turns = DomainTurns()
        sleeping = asyncio.Event()

        async def workflow():
            await turns.call(lambda: None)
            sleeping.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(workflow())
        await sleeping.wait()
        turns.drain()
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class ExecutorCloseTests(unittest.TestCase):
    def test_drain_then_sdk_stop_then_slot_fences_close_without_workflow_wait(self):
        order = []
        executor = DurableExecutor.__new__(DurableExecutor)
        executor.slots = SimpleNamespace(
            _closing=False, close=Mock(side_effect=lambda: order.append("slots"))
        )
        executor.domain_turns = SimpleNamespace(
            drain=Mock(side_effect=lambda: order.append("drain"))
        )
        runtime = Mock()
        runtime.destroy.side_effect = lambda **kwargs: order.append("sdk")
        executor._DBOS = runtime
        executor.close()
        self.assertEqual(order, ["drain", "sdk", "slots"])
        self.assertTrue(executor.slots._closing)
        runtime.destroy.assert_called_once_with(
            destroy_registry=True, workflow_completion_timeout_sec=0
        )
        self.assertIsNone(executor._DBOS)
