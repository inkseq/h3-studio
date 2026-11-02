"""Original local registration identities and optional failure boundaries."""

from contextlib import redirect_stdout
from dataclasses import asdict
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from studio_platform.control import WorkerControl
from studio_platform.dbos_transport import DurableSlots, LOCAL_SLOT_FAMILIES, _Slot
from studio_platform.fleet import ADAPTER_FACTORIES
from studio_platform.providers.registry import LOCAL_BACKENDS, WORKER_BACKENDS
from studio_platform.settings import Settings
from test_platform_repository import LedgerCase


def register_local(slots):
    slots.add_local_slots()


class LocalRegistrationTests(unittest.TestCase):
    def test_execution_tables_cover_the_registry_without_rental_backend_ids(self):
        self.assertEqual(set(ADAPTER_FACTORIES), WORKER_BACKENDS)
        self.assertEqual({row[0].id for row in LOCAL_SLOT_FAMILIES}, LOCAL_BACKENDS)

    def slots(self, **switches):
        settings = SimpleNamespace(
            **dict(
                render_enabled=False, enhance_enabled=False, everyframe_enabled=False
            )
            | switches
        )
        return DurableSlots(Mock(), settings, Path.cwd())

    def capture(self, slots, *, failure=None):
        seen, beats = [], []

        def ensure(worker, slot):
            seen.append(slot)
            self.assertEqual(worker, slot.spec.worker_id)
            if failure is not None and failure(slot):
                raise ValueError("private-provider-error-sentinel")

        slots.local.ensure = Mock(side_effect=ensure)
        slots.local.heartbeat = Mock(side_effect=lambda: beats.append(len(seen)))
        return seen, beats

    def test_exact_local_worker_identities_order_and_heartbeat_boundaries(self):
        slots = self.slots(
            render_enabled=True, enhance_enabled=True, everyframe_enabled=True
        )
        seen, beats = self.capture(slots)
        register_local(slots)
        render = [f"dbos-cpu-render-v{i}" for i in (1, 2, 3, 5)]
        enhance = [
            "dbos-runpod-enhance-v1",
            "dbos-runpod-enhance-v1-1",
            "dbos-runpod-enhance-v1-2",
        ]
        hosted = [
            f"dbos-everyframe-{model}-v1-{index}"
            for model, count in (
                ("h3-max-turbo", 4),
                ("pika-h3-flex", 4),
                ("h3-max-fast", 4),
                ("vivix-w1", 1),
            )
            for index in range(count)
        ]
        self.assertEqual(
            [slot.spec.worker_id for slot in seen], render + enhance + hosted
        )
        self.assertEqual(beats, [4, 7, 20])
        for slot in seen:
            value = asdict(slot)
            spec = value.pop("spec")
            self.assertEqual(
                value,
                dict(
                    enabled=True,
                    endpoint="",
                    allowed_origins=(),
                    comfy_revision="",
                    confirmed_idle=False,
                    recovery_only=False,
                    runtime_config_file="",
                ),
            )
            self.assertEqual(spec["worker_id"], spec["instance_id"])
            self.assertEqual(spec["physical_gpu_ids"], ())
            self.assertEqual(
                (
                    spec["engine_manifest_digest"],
                    spec["output_delivery"],
                    spec["dispatch_backend"],
                ),
                ("", "", "legacy"),
            )
            if spec["backend"] == "cpu-render":
                self.assertEqual(
                    (
                        spec["pool"],
                        spec["provider"],
                        spec["recipe_ids"],
                        spec["model_id"],
                    ),
                    (
                        "cpu-render",
                        "local-cpu",
                        ("chapter-roughcut-v1",),
                        "sixnine-chapter-roughcut-v1",
                    ),
                )
            elif spec["backend"] == "runpod-enhance":
                self.assertEqual(
                    (
                        spec["pool"],
                        spec["provider"],
                        spec["recipe_ids"],
                        spec["model_id"],
                        spec["configuration_id"],
                    ),
                    (
                        "enhance",
                        "runpod",
                        ("enhance-v1",),
                        "open-h3-regenerate-2k",
                        "runpod-enhance-v1",
                    ),
                )
            else:
                self.assertEqual(
                    (spec["pool"], spec["provider"], spec["recipe_ids"]),
                    (
                        "everyframe",
                        "everyframe",
                        ("h3-base-fl2va-v1", "h3-base-ref2va-v1"),
                    ),
                )
                self.assertEqual(
                    spec["configuration_id"], f"everyframe-{spec['model_id']}-v1"
                )

    def test_optional_partial_failure_keeps_prior_slots_and_continues_next_family(self):
        for backend, failed_worker, count, beats, code in (
            (
                "runpod-enhance",
                "dbos-runpod-enhance-v1-1",
                19,
                [4, 19],
                "dbos_enhance_slot_unavailable",
            ),
            (
                "everyframe-render",
                "dbos-everyframe-pika-h3-flex-v1-1",
                13,
                [4, 7],
                "dbos_everyframe_slot_unavailable",
            ),
        ):
            with self.subTest(backend=backend):
                slots = self.slots(
                    render_enabled=True, enhance_enabled=True, everyframe_enabled=True
                )
                seen, actual_beats = self.capture(
                    slots, failure=lambda slot: slot.spec.worker_id == failed_worker
                )
                out = io.StringIO()
                with redirect_stdout(out):
                    register_local(slots)
                self.assertEqual(len(seen), count)
                self.assertEqual(actual_beats, beats)
                self.assertEqual(out.getvalue(), code + " error_type=ValueError\n")
                self.assertEqual(
                    sum(s.spec.backend == backend for s in seen),
                    2 if backend == "runpod-enhance" else 6,
                )

    def test_render_failure_propagates_before_later_families(self):
        slots = self.slots(render_enabled=True, enhance_enabled=True)
        seen, beats = self.capture(slots, failure=lambda _: True)
        with self.assertRaisesRegex(ValueError, "sentinel"):
            register_local(slots)
        self.assertEqual(len(seen), 1)
        self.assertEqual(beats, [])

    def test_disabled_and_missing_optional_switches_do_not_register_or_heartbeat(self):
        slots = self.slots()
        slots.settings = SimpleNamespace(render_enabled=False)
        seen, beats = self.capture(slots)
        with patch(
            "studio_platform.control.WorkerSpec",
            side_effect=AssertionError("spec while disabled"),
        ):
            register_local(slots)
        self.assertEqual((seen, beats), ([], []))
        del slots.settings.render_enabled
        with self.assertRaises(AttributeError):
            register_local(slots)

    def test_optional_spec_failure_is_not_swallowed_or_eagerly_constructed(self):
        from studio_platform.control import WorkerSpec

        for switch in ("enhance_enabled", "everyframe_enabled"):
            with self.subTest(switch=switch):
                slots = self.slots(**{switch: True})
                seen, beats = self.capture(slots)

                def spec(*args, **kwargs):
                    if len(seen) == 1:
                        raise ValueError("invalid-later-spec")
                    return WorkerSpec(*args, **kwargs)

                with (
                    patch("studio_platform.control.WorkerSpec", side_effect=spec),
                    self.assertRaisesRegex(ValueError, "invalid-later-spec"),
                ):
                    register_local(slots)
                self.assertEqual(len(seen), 1)
                self.assertEqual(beats, [])

    def test_optional_heartbeat_failure_and_base_exception_propagate(self):
        for switch in ("enhance_enabled", "everyframe_enabled"):
            with self.subTest(switch=switch):
                slots = self.slots(**{switch: True})
                self.capture(slots)
                slots.local.heartbeat.side_effect = RuntimeError(
                    "heartbeat-unavailable"
                )
                with self.assertRaisesRegex(RuntimeError, "heartbeat-unavailable"):
                    register_local(slots)
                slots.local.ensure.side_effect = KeyboardInterrupt
                with self.assertRaises(KeyboardInterrupt):
                    register_local(slots)

    def test_local_heartbeat_keeps_fences_and_does_not_revive_unknown_or_expired(self):
        slots = self.slots()
        slots.repo.clock.return_value = 1000
        for index, (backend, state, expires) in enumerate(
            (
                ("cpu-render", "busy", 1001),
                ("runpod-enhance", "ready", 1000),
                ("everyframe-render", "unknown", 1001),
                ("everyframe-render", "retired", 1001),
                ("wangp-worker", "ready", 1001),
            )
        ):
            worker = f"worker-{index}"
            control = Mock()
            control.get.return_value = dict(
                state=state,
                expires_at=expires,
                fence=7,
                current_job_id="original-job",
                drain_requested=True,
            )
            slots._slots[worker] = _Slot(
                SimpleNamespace(backend=backend, worker_id=worker),
                SimpleNamespace(control=control),
                None,
                None,
            )
        slots.heartbeat_cpu()
        for index, slot in enumerate(slots._slots.values()):
            if index == 0:
                slot.runner.control.heartbeat.assert_called_once_with(
                    "worker-0", 7, lease_seconds=3600
                )
            else:
                slot.runner.control.heartbeat.assert_not_called()
            self.assertEqual(
                slot.runner.control.get.return_value["current_job_id"], "original-job"
            )
            self.assertTrue(slot.runner.control.get.return_value["drain_requested"])
        slots._slots["worker-4"].runner.control.get.assert_not_called()


class HostedRegistrationTests(LedgerCase):
    def test_failed_second_optional_adapter_preserves_rows_and_other_families(self):
        from studio_platform import enhance_backend

        directory = Path(self.temp.name)
        settings = Settings(
            directory,
            auth_mode="local-test",
            render_enabled=True,
            enhance_enabled=True,
            everyframe_enabled=True,
        )
        create = enhance_backend.create
        constructed = []

        def fail_second(*args, **kwargs):
            constructed.append(kwargs["slot_key"])
            if len(constructed) == 2:
                raise ValueError("private-provider-error-sentinel")
            return create(*args, **kwargs)

        out = io.StringIO()
        with DurableSlots(self.repo, settings, directory / "executor") as slots:
            with (
                patch.object(enhance_backend, "create", side_effect=fail_second),
                redirect_stdout(out),
            ):
                register_local(slots)
            self.assertEqual(len(slots._slots), 18)
            self.assertEqual(slots.children, {})
            control = WorkerControl(self.repo)
            self.assertEqual(
                control.get("dbos-runpod-enhance-v1")["expires_at"], self.now + 3600
            )
            self.assertNotIn("dbos-runpod-enhance-v1-1", slots._slots)
            self.assertEqual(
                control.get("dbos-runpod-enhance-v1-1")["state"], "registered"
            )
            self.assertEqual(
                constructed, ["dbos-runpod-enhance-v1", "dbos-runpod-enhance-v1-1"]
            )
        self.assertEqual(
            out.getvalue(), "dbos_enhance_slot_unavailable error_type=ValueError\n"
        )

    def test_everyframe_registers_ready_leases_with_distinct_slot_keys(self):
        directory = Path(self.temp.name)
        settings = Settings(directory, auth_mode="local-test", everyframe_enabled=True)
        with DurableSlots(self.repo, settings, directory / "executor") as slots:
            register_local(slots)
            self.assertEqual(len(slots._slots), 13)
            self.assertEqual(slots.children, {})
            for slot in slots._slots.values():
                self.assertEqual(slot.runner.backend.slot_key, slot.spec.worker_id)
                worker = WorkerControl(self.repo).get(slot.spec.worker_id)
                self.assertEqual(worker["state"], "ready")
                self.assertEqual(worker["expires_at"], self.now + 3600)


if __name__ == "__main__":
    unittest.main()
