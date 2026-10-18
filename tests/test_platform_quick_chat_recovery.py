"""Assistant recovery and consented retention keep separate bounded outcomes."""

import asyncio
import unittest
from unittest.mock import Mock

from studio_platform.quick_chat_recovery import QuickChatRecovery


class QuickChatRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_shutdown_and_explicit_tick_only_use_reconciliation(self):
        service = Mock()
        service.recover_assistant_runs.return_value = 2
        service.recover_title_runs.return_value = 0
        service.process_history_cleanup.return_value = {"processed": 0}
        worker = QuickChatRecovery(service)
        await worker.start()
        self.assertEqual(worker.last_count, 2)
        service.recover_assistant_runs.assert_called_once_with(older_than_s=180)
        service.recover_title_runs.assert_called_once_with(older_than_s=180)
        service.process_history_cleanup.assert_not_called()
        service.recover_assistant_runs.return_value = 0
        await worker.reconcile()
        self.assertEqual(worker.last_count, 0)
        await asyncio.wait_for(worker.close(), timeout=1)
        self.assertTrue(worker.task.done())
        self.assertEqual(
            [v[0] for v in service.method_calls],
            ["recover_assistant_runs", "recover_title_runs"] * 2
            + ["process_history_cleanup"],
        )
        service.process_history_cleanup.assert_called_with(limit=10, max_objects=10)

    async def test_failed_sql_is_safe_and_next_tick_can_recover(self):
        service = Mock()
        service.recover_assistant_runs.side_effect = [RuntimeError("PRIVATE"), 1]
        service.recover_title_runs.return_value = 0
        worker = QuickChatRecovery(service)
        await worker.reconcile()
        self.assertEqual(worker.last_error_code, "quick_chat_recovery_failed")
        await worker.reconcile()
        self.assertIsNone(worker.last_error_code)
        self.assertEqual(worker.last_count, 1)

    def test_fresh_remote_calls_cannot_be_fenced_with_short_threshold(self):
        with self.assertRaises(ValueError):
            QuickChatRecovery(Mock(), older_than_s=1)

    async def test_storage_failure_does_not_reclassify_assistant_recovery(self):
        service = Mock()
        service.recover_assistant_runs.return_value = 0
        service.recover_title_runs.return_value = 0
        service.process_history_cleanup.side_effect = [
            RuntimeError("PRIVATE object location"),
            {"processed": 1},
        ]
        worker = QuickChatRecovery(service)
        await worker.reconcile()
        self.assertIsNone(worker.last_error_code)
        self.assertEqual(
            worker.last_history_error_code, "quick_chat_history_cleanup_failed"
        )
        self.assertNotIn("PRIVATE", worker.last_history_error_code)
        await worker.reconcile()
        self.assertIsNone(worker.last_history_error_code)
        self.assertEqual(worker.last_history_cleanup, {"processed": 1})

    async def test_consented_cleanup_can_progress_when_assistant_sql_recovery_fails(
        self,
    ):
        service = Mock()
        service.recover_assistant_runs.side_effect = RuntimeError(
            "PRIVATE assistant context"
        )
        service.process_history_cleanup.return_value = {"processed": 1}
        worker = QuickChatRecovery(service)
        await worker.reconcile()
        self.assertEqual(worker.last_error_code, "quick_chat_recovery_failed")
        self.assertIsNone(worker.last_history_error_code)
        self.assertEqual(worker.last_history_cleanup, {"processed": 1})
        service.process_history_cleanup.assert_called_once_with(
            limit=10, max_objects=10
        )


if __name__ == "__main__":
    unittest.main()
