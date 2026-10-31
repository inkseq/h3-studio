"""Original DBOS turns prefer dedicated slots without another queue or lease."""

from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from studio_platform.dbos_transport import DurableSlots, _Slot


class ModalRoutingTests(unittest.TestCase):
    def execute(self, *, dedicated_idle=False, bound=None, locked=False):
        calls = []
        job = {
            'status': 'queued',
            'pool': 'pool',
            'not_before': 0,
            'lease_expires_at': None,
            'lease_worker_id': bound,
        }
        repo = Mock()
        repo.get_job.return_value = job
        repo.clock.return_value = 1
        slots = DurableSlots(repo, Mock(), Path('/offline'))
        for provider in ('modal', 'vast'):
            spec = SimpleNamespace(
                provider=provider,
                worker_id=provider,
                backend='wangp-worker',
                pool='pool',
            )
            runner = Mock()

            def turn(worker, pool):
                calls.append(worker)
                return {} if worker == 'vast' and dedicated_idle else {'job_id': 'job'}

            runner.run_once.side_effect = turn
            slots._slots[provider] = _Slot(spec, runner, Mock(), threading.Lock())
        if locked:
            slots._slots['vast'].lock.acquire()
        slots.turn('tenant', 'owner', 'project', 'job')
        return calls

    def test_new_work_prefers_dedicated_even_if_modal_registered_first(self):
        self.assertEqual(self.execute(), ['vast'])

    def test_ineligible_dedicated_yields_to_original_modal_runner(self):
        self.assertEqual(self.execute(dedicated_idle=True), ['vast', 'modal'])

    def test_busy_dedicated_does_not_block_available_modal(self):
        self.assertEqual(self.execute(locked=True), ['modal'])

    def test_existing_attempt_keeps_original_modal_affinity(self):
        self.assertEqual(self.execute(bound='modal'), ['modal'])
