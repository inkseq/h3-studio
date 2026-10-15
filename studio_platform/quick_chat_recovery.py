"""Original assistant recovery and consented history retention maintenance.

Assistant/title recovery never resubmits a model call. History cleanup resumes
only durable original purge intents or expired consented trash; it never owns
generation, GPU capacity or billing reconciliation.
"""
from __future__ import annotations

import asyncio
from starlette.concurrency import run_in_threadpool


class QuickChatRecovery:
    def __init__(self, service, *, older_than_s=180, interval_s=30):
        # The bounded supplier request has a 120-second timeout. Recovery must
        # not fence another API process's fresh request during a rolling start.
        if older_than_s < 180 or not 1 <= interval_s <= 300:
            raise ValueError("invalid_assistant_recovery_window")
        self.service, self.older_than_s, self.interval_s = service, older_than_s, interval_s
        self.stop = asyncio.Event()
        self.task = None
        self.last_count = 0
        self.last_error_code = None
        self.last_history_cleanup = None
        self.last_history_error_code = None

    async def reconcile(self, *, include_history=True):
        try:
            self.last_count = await run_in_threadpool(self.service.recover_assistant_runs,
                                                      older_than_s=self.older_than_s)
            self.last_count += await run_in_threadpool(self.service.recover_title_runs,
                                                       older_than_s=self.older_than_s)
            self.last_error_code = None
        except Exception:
            # Database/driver exception text may contain private values.
            # No supplier request or automatic creation/retry occurs here.
            self.last_error_code = "quick_chat_recovery_failed"
        # Keep storage outcomes separate from assistant recovery diagnostics.
        # Cleanup commits its deletion intent before external object-store IO.
        if not include_history:
            return
        cleanup = getattr(self.service, "process_history_cleanup", None)
        if cleanup is not None:
            try:
                self.last_history_cleanup = await run_in_threadpool(cleanup, limit=10, max_objects=10)
                self.last_history_error_code = None
            except Exception:
                self.last_history_error_code = "quick_chat_history_cleanup_failed"

    async def _run(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                await self.reconcile()

    async def start(self):
        # Readiness must not wait on object-store streaming or deletion. The
        # existing background tick resumes consented original cleanup work.
        await self.reconcile(include_history=False)
        self.task = asyncio.create_task(self._run())

    async def close(self):
        self.stop.set()
        if self.task is not None:
            await self.task
