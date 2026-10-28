"""CPU-only business-idle and explicit L40S admission tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from studio_platform.modal_runtime import IdleWindow, device_profile, host_factory
from studio_platform.runtime_catalog import get_profile
from test_platform_wangp_host import HostFixture
from test_platform_wangp_receipts import manifest, prepared
from studio_platform.inference.protocol import NotReady

PROFILE = 'h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7'


class ModalRuntimeTests(unittest.TestCase):
    def test_l40s_variant_is_explicit_and_does_not_mutate_manifest_profile(self):
        original = get_profile(PROFILE)
        result = device_profile(original)
        self.assertNotIn('hardware_admission', original)
        self.assertEqual(result['hardware_admission']['gpu_models'], ['L40S'])
        self.assertEqual(result['hardware_admission']['compute_capability'], [8, 9])
        self.assertEqual(result['runtime'], original['runtime'])
        self.assertEqual(result['components'], original['components'])
        with self.assertRaises(ValueError):
            device_profile({**original, 'id': 'legacy'})

    def test_busy_or_unsettled_receipts_reset_idle_without_cancelling(self):
        clock = [0]
        host = SimpleNamespace(
            journal=SimpleNamespace(has_pending_delivery=lambda: False),
            readiness=Mock(return_value=SimpleNamespace(idle=False)),
        )
        idle = IdleWindow(host, clock=lambda: clock[0])
        clock[0] = 600
        self.assertFalse(idle.expired())
        host.readiness.return_value.idle = True
        clock[0] = 659
        self.assertFalse(idle.expired())
        clock[0] = 660
        self.assertTrue(idle.expired())


class ModalHostIdleTests(HostFixture, unittest.TestCase):
    def make_host(self, *, session=None, journal=None, fault_hook=None, **kwargs):
        host = host_factory(
            session=session or self.session,
            journal=journal or self.journal,
            manifest=manifest(),
            output_root=self.output,
            sealed_root=self.root / 'sealed',
            fault_hook=fault_hook,
            **kwargs,
        )
        self.addCleanup(host.close)
        return host

    def test_idle_closes_same_admission_lock_before_late_submit(self):
        self.host.idle_window.last_busy = 0
        self.host.idle_window.clock = lambda: 60
        self.assertTrue(self.host.drain_if_idle())
        with self.assertRaises(NotReady):
            self.host.submit(prepared())
        self.assertEqual(self.session.calls, 0)

    def test_running_or_uncollected_work_never_idle_stops(self):
        self.host.submit(prepared())
        self.host.idle_window.last_busy = 0
        self.host.idle_window.clock = lambda: 600
        self.assertFalse(self.host.drain_if_idle())
        self.complete()
        self.host.inspect(prepared().operation_id)
        self.host.idle_window.last_busy = 0
        self.assertFalse(self.host.drain_if_idle())
        self.assertEqual(self.session.handle.cancel_calls, 0)

    def test_input_upload_and_delivery_pause_idle_but_probes_do_not(self):
        clock = [0]
        host = SimpleNamespace(
            journal=SimpleNamespace(has_pending_delivery=lambda: False),
            readiness=Mock(return_value=SimpleNamespace(idle=True)),
        )
        idle = IdleWindow(host, clock=lambda: clock[0])
        idle.requests = 1
        clock[0] = 400
        self.assertFalse(idle.expired())
        idle.requests = 0
        clock[0] = 459
        self.assertFalse(idle.expired())
        clock[0] = 460
        self.assertTrue(idle.expired())

    def test_unknown_readiness_never_proves_idle(self):
        clock = [0]
        host = SimpleNamespace(
            journal=SimpleNamespace(has_pending_delivery=lambda: False),
            readiness=Mock(side_effect=RuntimeError('unknown')),
        )
        idle = IdleWindow(host, clock=lambda: clock[0])
        clock[0] = 600
        self.assertFalse(idle.expired())
