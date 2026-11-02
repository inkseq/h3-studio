"""Fake process handles/local CPU injection only; never starts a GPU or daemon."""

from dataclasses import asdict, replace
import contextlib
import io
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from studio_platform.control import WorkerSpec
from studio_platform.fleet import (
    FleetConfig,
    SlotConfig,
    read_config,
    run_slot,
    slot_runner,
)
from studio_platform.repository import BudgetExceeded, Conflict
from studio_platform.settings import Settings
from studio_platform.worker import MockBackend
from test_platform_repository import LedgerCase


REVISION = "e9027f2b30f37bb3052714eb08fcf479542f4fc0"


class FleetTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.temp.name) / "fleet-work"
        self.config_path = Path(self.temp.name) / "operator-fleet.json"
        self.processes, self.commands = [], []
        self.elapsed = 0

    def real_slot(
        self, worker="worker0", *, gpu="fake-gpu0", endpoint="http://127.0.0.1:18000"
    ):
        return SlotConfig(
            WorkerSpec(
                worker,
                "qualified",
                "test-only",
                "fake-instance",
                (gpu,),
                ("h3-base-fl2va-v1",),
                "MiniMax-H3-Base-BF16",
                "test-only-manifest",
            ),
            enabled=True,
            endpoint=endpoint,
            allowed_origins=(endpoint,),
            comfy_revision=REVISION,
        )

    def mock_slot(self):
        return SlotConfig(
            WorkerSpec(
                "mock-test",
                "mock",
                "mock",
                "local-only",
                ("cpu",),
                (),
                "SIMULATION",
                "simulation-v1",
                backend="mock",
            ),
            enabled=True,
        )

    def cpu_slot(self):
        return SlotConfig(
            WorkerSpec(
                "chapter-cpu",
                "chapter",
                "local-cpu",
                "local-cpu-instance",
                (),
                ("chapter-roughcut-v1",),
                "sixnine-chapter-roughcut-v1",
                "cpu-render-v1",
                "cpu-render",
            ),
            enabled=True,
        )

    def test_cpu_slot_rejects_endpoint_and_accepts_only_real_fleet_peers(self):
        cpu = self.cpu_slot()
        with self.assertRaises(ValueError):
            replace(
                cpu,
                endpoint="http://127.0.0.1:8188",
                allowed_origins=("http://127.0.0.1:8188",),
            )
        self.config(cpu, self.real_slot())
        with self.assertRaises(ValueError):
            self.config(cpu, self.mock_slot())

    def test_postgres_slot_startup_preserves_explicit_idle_worker_drain(self):
        from studio_platform.control import WorkerControl

        slot = self.mock_slot()
        slot = replace(slot, spec=replace(slot.spec, dispatch_backend="hatchet-v1"))
        control = WorkerControl(self.repo)
        control.register(slot.spec)
        control.mark_ready(slot.spec.worker_id, upstream_idle_confirmed=True)
        original = control.drain(slot.spec.worker_id)
        result = run_slot(
            self.config(slot),
            slot.spec.worker_id,
            Settings(
                Path(self.temp.name), execution_backend="mock", generation_enabled=True
            ),
            once=True,
            repository=self.repo,
            backend_factory=lambda slot, directory: MockBackend(
                directory, enabled=True
            ),
        )
        current = control.get(slot.spec.worker_id)
        self.assertEqual(result["state"], "idle")
        self.assertEqual(
            (current["drain_requested"], current["state"], current["fence"]),
            (1, "draining", original["fence"]),
        )

    def config(self, *slots, enabled=True):
        return FleetConfig(
            self.directory,
            tuple(slots),
            enabled=enabled,
            max_children=len(slots),
            shutdown_grace_s=0.3,
        )

    def write_config(self, config):
        value = {
            "version": 1,
            "work_dir": str(config.work_dir),
            "enabled": config.enabled,
            "max_children": config.max_children,
            "shutdown_grace_s": config.shutdown_grace_s,
            "slots": [],
        }
        for slot in config.slots:
            value["slots"].append(
                {
                    **asdict(slot.spec),
                    "enabled": slot.enabled,
                    "endpoint": slot.endpoint,
                    "allowed_origins": slot.allowed_origins,
                    "comfy_revision": slot.comfy_revision,
                    "confirmed_idle": slot.confirmed_idle,
                }
            )
        self.config_path.write_text(json.dumps(value), encoding="utf-8")
        self.config_path.chmod(0o600)

    def test_manifest_rejects_duplicates_auth_urls_child_limits_and_simulation_mix(
        self,
    ):
        first = self.real_slot()
        duplicates = [
            first,
            replace(first, spec=replace(first.spec, worker_id="other")),
            self.real_slot("other", gpu="fake-other", endpoint=first.endpoint),
        ]
        for second in duplicates:
            with self.assertRaises(ValueError):
                self.config(first, second)
        with self.assertRaises(ValueError):
            self.config(first, self.mock_slot())
        with self.assertRaises(ValueError):
            replace(
                first,
                endpoint="https://test.invalid?token=fake",
                allowed_origins=("https://test.invalid?token=fake",),
            )
        with self.assertRaises(ValueError):
            replace(first, spec=replace(first.spec, worker_id="../escape"))
        with self.assertRaises(ValueError):
            replace(self.config(first), max_children=0)

    def test_injected_child_keeps_repo_store_on_cpu_and_passes_guard_stop_hook(self):
        config = self.config(self.mock_slot())
        settings = Settings(
            Path(self.temp.name),
            auth_mode="local-test",
            execution_backend="mock",
            generation_enabled=True,
        )
        marker, captures = object(), []

        class FakeRunner:
            def __init__(inner, repo, store, directory, **kwargs):
                captures.append((repo, store, directory, kwargs))

            def run_once(inner, worker, pool):
                return {"state": "idle", "simulation": True}

        result = run_slot(
            config,
            "mock-test",
            settings,
            repository=self.repo,
            store_factory=lambda _: marker,
            backend_factory=lambda slot, directory: MockBackend(
                directory / "simulation", enabled=True
            ),
            runner_factory=FakeRunner,
            once=True,
        )
        self.assertEqual(result["state"], "idle")
        repo, store, directory, kwargs = captures[0]
        self.assertIs(repo, self.repo)
        self.assertIs(store, marker)
        self.assertTrue(callable(kwargs["submission_guard"]))
        self.assertFalse(kwargs["stop_requested"]())
        directory.mkdir(parents=True)
        (directory / "drain.flag").touch()
        self.assertTrue(kwargs["stop_requested"]())

    def test_real_readiness_declaration_also_requires_current_empty_private_queue(self):
        slot = replace(self.real_slot(), confirmed_idle=True)
        config = self.config(slot)
        settings = Settings(
            Path(self.temp.name),
            auth_mode="local-test",
            execution_backend="comfy-worker",
            generation_enabled=True,
        )
        idle_checks = [False, True]
        stores, runners = [], []

        class FakeBackend:
            enabled, kind, slot_key = True, "comfy-worker", "fake-only-private-origin"

            def is_idle(inner):
                return idle_checks.pop(0)

            def _json(inner, *args, **kwargs):
                self.fail("fleet must not know an engine's private queue protocol")

            def close(inner):
                pass

        class FakeRunner:
            def __init__(inner, *args, **kwargs):
                runners.append(kwargs)

            def run_once(inner, *args):
                return {"state": "idle", "simulation": False}

        kwargs = dict(
            repository=self.repo,
            store_factory=lambda _: stores.append(True) or object(),
            backend_factory=lambda *_: FakeBackend(),
            runner_factory=FakeRunner,
            once=True,
        )
        with self.assertRaisesRegex(ValueError, "fleet_upstream_idle_not_confirmed"):
            run_slot(config, "worker0", settings, **kwargs)
        self.assertEqual(stores, [])
        result = run_slot(config, "worker0", settings, **kwargs)
        self.assertEqual(result["state"], "idle")
        self.assertEqual(len(stores), 1)
        self.assertTrue(callable(runners[0]["submission_guard"]))

    def test_real_readiness_fails_closed_on_truthy_malformed_or_failed_probe(self):
        from studio_platform.inference.protocol import BackendError

        slot = replace(self.real_slot(), confirmed_idle=True)
        config = self.config(slot)
        settings = Settings(
            Path(self.temp.name),
            auth_mode="local-test",
            execution_backend="comfy-worker",
            generation_enabled=True,
        )
        stores, runners, closed = [], [], []
        for evidence in (
            None,
            1,
            "ready",
            {"ready": True},
            [True],
            BackendError("probe_unavailable"),
        ):
            with self.subTest(evidence=evidence):

                class FakeBackend:
                    enabled, kind, slot_key = (
                        True,
                        "comfy-worker",
                        "fake-only-private-origin",
                    )

                    def is_idle(inner):
                        if isinstance(evidence, Exception):
                            raise evidence
                        return evidence

                    def close(inner):
                        closed.append(True)

                expected = (
                    BackendError if isinstance(evidence, Exception) else ValueError
                )
                with self.assertRaises(expected):
                    run_slot(
                        config,
                        "worker0",
                        settings,
                        repository=self.repo,
                        store_factory=lambda _: stores.append(True),
                        backend_factory=lambda *_: FakeBackend(),
                        runner_factory=lambda *args, **kwargs: runners.append(True),
                        once=True,
                    )
                from studio_platform.control import WorkerControl

                self.assertNotEqual(
                    WorkerControl(self.repo).get("worker0")["state"], "ready"
                )
        self.assertEqual(stores, [])
        self.assertEqual(runners, [])
        self.assertEqual(len(closed), 6)

    def test_config_file_rejects_secret_or_misspelled_fields_without_echo(self):
        self.write_config(self.config(self.mock_slot()))
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        value["slots"][0]["api_key"] = "synthetic-not-a-real-key"
        self.config_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid_fleet_slot_fields"):
            read_config(self.config_path)

    def test_continuous_execution_is_retired_before_loading_a_slot(self):
        with self.assertRaisesRegex(ValueError, 'dbos_required'):
            run_slot(None, 'unused', None, once=False)

    def test_default_adapter_constructors_keep_arguments_stores_and_slot_identity(self):
        cpu = self.cpu_slot()
        enhance = replace(
            cpu,
            spec=WorkerSpec(
                "enhance",
                "enhance",
                "runpod",
                "enhance",
                (),
                ("enhance-v1",),
                "open-h3-regenerate-2k",
                "runpod-enhance-v1",
                "runpod-enhance",
            ),
        )
        hosted = replace(
            cpu,
            spec=WorkerSpec(
                "hosted",
                "everyframe",
                "everyframe",
                "hosted",
                (),
                ("h3-base-fl2va-v1", "h3-base-ref2va-v1"),
                "h3-max-turbo",
                "everyframe-h3-max-turbo-v1",
                "everyframe-render",
            ),
        )
        comfy = replace(self.real_slot(), confirmed_idle=True)
        wangp = replace(
            comfy,
            spec=replace(
                comfy.spec, backend="wangp-worker", engine_manifest_digest="a" * 64
            ),
            comfy_revision="",
            runtime_config_file=str(self.directory / "runtime.json"),
        )
        for slot, target in (
            (self.mock_slot(), "studio_platform.fleet.MockBackend"),
            (cpu, "studio_platform.render_backend.CPURenderBackend"),
            (enhance, "studio_platform.enhance_backend.create"),
            (hosted, "studio_platform.providers.everyframe.create"),
            (comfy, "studio_platform.fleet.ComfyBackend"),
            (wangp, "studio_platform.inference.wangp_factory.create_backend"),
        ):
            with self.subTest(backend=slot.spec.backend):
                settings = Settings(
                    Path(self.temp.name),
                    execution_backend=slot.spec.backend
                    if slot is comfy or slot is wangp
                    else "mock",
                    generation_enabled=True,
                    render_enabled=True,
                    enhance_enabled=True,
                    everyframe_enabled=True,
                )
                backend = SimpleNamespace(
                    kind=slot.spec.backend,
                    enabled=True,
                    slot_key="constructor-owned-key",
                    close=Mock(),
                    is_idle=Mock(return_value=True),
                    manifest=SimpleNamespace(digest="a" * 64),
                )
                control = Mock()
                control.register.return_value = dict(
                    current_job_id=None,
                    state="ready",
                    expires_at=self.now + 120,
                    drain_requested=False,
                )
                stores = [object(), object()]
                store_factory = Mock(side_effect=stores)
                runner_factory = Mock()
                directory = self.directory / slot.spec.worker_id
                with (
                    patch(target, return_value=backend) as constructor,
                    patch("studio_platform.fleet.WorkerControl", return_value=control),
                ):
                    with slot_runner(
                        self.config(slot),
                        slot.spec.worker_id,
                        settings,
                        repository=self.repo,
                        store_factory=store_factory,
                        runner_factory=runner_factory,
                    ):
                        self.assertEqual(backend.slot_key, "constructor-owned-key")
                    if slot is enhance or slot is hosted:
                        constructor.assert_called_once_with(
                            stores[0], settings, slot_key=slot.spec.worker_id
                        )
                        self.assertEqual(store_factory.call_count, 2)
                        self.assertIs(runner_factory.call_args.args[1], stores[1])
                    else:
                        self.assertEqual(store_factory.call_count, 1)
                        self.assertIs(runner_factory.call_args.args[1], stores[0])
                        if slot is comfy:
                            constructor.assert_called_once_with(
                                endpoint=slot.endpoint,
                                enabled=True,
                                allowed_origins=slot.allowed_origins,
                                comfy_revision=slot.comfy_revision,
                            )
                        elif slot is wangp:
                            constructor.assert_called_once_with(slot, directory)
                        else:
                            constructor.assert_called_once_with(
                                directory / ("render" if slot is cpu else "simulation"),
                                enabled=True,
                            )
                backend.close.assert_called_once_with()

    def test_constructor_failure_preserves_original_registration_without_runner(self):
        from studio_platform.control import WorkerControl

        slot = self.cpu_slot()
        settings = Settings(Path(self.temp.name), render_enabled=True)
        runner = Mock()
        with (
            patch(
                "studio_platform.render_backend.CPURenderBackend",
                side_effect=ValueError("constructor-failed"),
            ),
            self.assertRaisesRegex(ValueError, "constructor-failed"),
        ):
            with slot_runner(
                self.config(slot),
                slot.spec.worker_id,
                settings,
                repository=self.repo,
                runner_factory=runner,
            ):
                self.fail("constructor failure must propagate")
        self.assertEqual(
            WorkerControl(self.repo).get(slot.spec.worker_id)["state"], "registered"
        )
        runner.assert_not_called()

    def test_injected_factory_still_obeys_gate_and_identity_cleanup(self):
        slot = self.cpu_slot()
        factory = Mock(
            return_value=SimpleNamespace(kind="mock", enabled=True, close=Mock())
        )
        runner, store = Mock(), Mock()
        for enabled, code in (
            (False, "explicit_service_settings"),
            (True, "identity_mismatch"),
        ):
            settings = Settings(Path(self.temp.name), render_enabled=enabled)
            with self.assertRaisesRegex(ValueError, code):
                with slot_runner(
                    self.config(slot),
                    slot.spec.worker_id,
                    settings,
                    repository=self.repo,
                    backend_factory=factory,
                    runner_factory=runner,
                    store_factory=store,
                ):
                    self.fail("invalid adapter must not create a runner")
            self.assertEqual(factory.call_count, int(enabled))
        factory.return_value.close.assert_called_once_with()
        runner.assert_not_called()
        store.assert_not_called()
