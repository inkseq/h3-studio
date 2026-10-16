"""Fake process handles/local CPU injection only; never starts a GPU or daemon."""
from dataclasses import asdict, replace
import contextlib
import io
import json
import os
from pathlib import Path
import signal
from unittest.mock import patch

from studio_platform.control import WorkerSpec
from studio_platform.fleet import FleetConfig, FleetSupervisor, SlotConfig, main, read_config, request_drain, run_slot
from studio_platform.repository import BudgetExceeded, Conflict
from studio_platform.settings import Settings
from studio_platform.worker import MockBackend
from test_platform_repository import LedgerCase


REVISION = "e9027f2b30f37bb3052714eb08fcf479542f4fc0"


class FakeProcess:
    def __init__(self, pid, flag, *, drain_exits=True):
        self.pid, self.flag = pid, flag
        self.drain_exits, self.code, self.signals = drain_exits, None, []

    def poll(self):
        if self.drain_exits and self.flag.exists():
            self.code = 0
        return self.code

    def send_signal(self, signum):
        self.signals.append(signum)


class FleetTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.temp.name)/"fleet-work"
        self.config_path = Path(self.temp.name)/"operator-fleet.json"
        self.processes, self.commands = [], []
        self.elapsed = 0

    def real_slot(self, worker="worker0", *, gpu="fake-gpu0", endpoint="http://127.0.0.1:18000"):
        return SlotConfig(WorkerSpec(worker, "qualified", "test-only", "fake-instance", (gpu,),
            ("h3-base-fl2va-v1",), "MiniMax-H3-Base-BF16", "test-only-manifest"), enabled=True,
            endpoint=endpoint, allowed_origins=(endpoint,), comfy_revision=REVISION)

    def mock_slot(self):
        return SlotConfig(WorkerSpec("mock-test", "mock", "mock", "local-only", ("cpu",), (),
            "SIMULATION", "simulation-v1", backend="mock"), enabled=True)

    def cpu_slot(self):
        return SlotConfig(WorkerSpec("chapter-cpu", "chapter", "local-cpu", "local-cpu-instance", (),
            ("chapter-roughcut-v1",), "sixnine-chapter-roughcut-v1", "cpu-render-v1", "cpu-render"), enabled=True)

    def test_postgres_slot_startup_preserves_explicit_idle_worker_drain(self):
        from studio_platform.control import WorkerControl
        slot=self.mock_slot()
        slot=replace(slot,spec=replace(slot.spec,dispatch_backend="hatchet-v1"))
        control=WorkerControl(self.repo)
        control.register(slot.spec)
        control.mark_ready(slot.spec.worker_id,upstream_idle_confirmed=True)
        original=control.drain(slot.spec.worker_id)
        result=run_slot(self.config(slot),slot.spec.worker_id,
            Settings(Path(self.temp.name),execution_backend="mock",generation_enabled=True),
            once=True,repository=self.repo,
            backend_factory=lambda slot,directory:MockBackend(directory,enabled=True))
        current=control.get(slot.spec.worker_id)
        self.assertEqual(result["state"],"idle")
        self.assertEqual((current["drain_requested"],current["state"],current["fence"]),
            (1,"draining",original["fence"]))

    def test_cpu_fleet_has_no_gpu_endpoint_or_capacity_reservation(self):
        cpu = self.cpu_slot()
        self.repo.configure_capacity()
        supervisor = self.supervisor(self.config(cpu))
        self.assertEqual(supervisor.start()["state"], "running")
        self.assertEqual(supervisor.control.capacity(), {"instances": 0, "physical_gpus": 0})
        self.assertEqual(len(self.commands), 1)
        supervisor.shutdown()
        with self.assertRaises(ValueError):
            replace(cpu, endpoint="http://127.0.0.1:8188", allowed_origins=("http://127.0.0.1:8188",))
        self.config(cpu, self.real_slot())  # CPU+real GPU can share control host.
        with self.assertRaises(ValueError):
            self.config(cpu, self.mock_slot())

    def config(self, *slots, enabled=True):
        return FleetConfig(self.directory, tuple(slots), enabled=enabled, max_children=len(slots), shutdown_grace_s=.3)

    def popen(self, command, **kwargs):
        self.commands.append((command, kwargs))
        worker_id = command[command.index("--slot")+1]
        proc = FakeProcess(70000+len(self.processes), self.directory/worker_id/"drain.flag")
        self.processes.append(proc)
        return proc

    def supervisor(self, config):
        return FleetSupervisor(config, self.repo, self.config_path, popen=self.popen,
            clock=lambda: self.elapsed, sleeper=lambda seconds: setattr(self, "elapsed", self.elapsed+seconds))

    def write_config(self, config):
        value = {"version": 1, "work_dir": str(config.work_dir), "enabled": config.enabled,
            "max_children": config.max_children, "shutdown_grace_s": config.shutdown_grace_s, "slots": []}
        for slot in config.slots:
            value["slots"].append({**asdict(slot.spec), "enabled": slot.enabled, "endpoint": slot.endpoint,
                "allowed_origins": slot.allowed_origins, "comfy_revision": slot.comfy_revision,
                "confirmed_idle": slot.confirmed_idle})
        self.config_path.write_text(json.dumps(value), encoding="utf-8")
        self.config_path.chmod(0o600)

    def test_default_disabled_does_not_register_create_files_or_start_processes(self):
        config = FleetConfig(self.directory)
        supervisor = self.supervisor(config)
        with patch.object(self.repo, "create_schema", side_effect=AssertionError("no DB mutation")):
            self.assertEqual(supervisor.start()["state"], "disabled")
            self.assertEqual(supervisor.tick()["state"], "disabled")
            self.assertEqual(supervisor.shutdown()["state"], "disabled")
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.commands, [])

    def test_manifest_rejects_duplicates_auth_urls_child_limits_and_simulation_mix(self):
        first = self.real_slot()
        duplicates = [first, replace(first, spec=replace(first.spec, worker_id="other")),
            self.real_slot("other", gpu="fake-other", endpoint=first.endpoint)]
        for second in duplicates:
            with self.assertRaises(ValueError):
                self.config(first, second)
        with self.assertRaises(ValueError):
            self.config(first, self.mock_slot())
        with self.assertRaises(ValueError):
            replace(first, endpoint="https://test.invalid?token=fake", allowed_origins=("https://test.invalid?token=fake",))
        with self.assertRaises(ValueError):
            replace(first, spec=replace(first.spec, worker_id="../escape"))
        with self.assertRaises(ValueError):
            replace(self.config(first), max_children=0)

    def test_global_zero_blocks_real_child_launch_before_spawning(self):
        self.repo.configure_capacity()
        with self.assertRaises(BudgetExceeded):
            self.supervisor(self.config(self.real_slot())).start()
        self.assertEqual(self.commands, [])

    def test_two_slots_register_real_capacity_and_spawn_only_safe_owned_commands(self):
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=2)
        slots = [self.real_slot(), self.real_slot("worker1", gpu="fake-gpu1", endpoint="http://127.0.0.1:18001")]
        supervisor = self.supervisor(self.config(*slots))
        state = supervisor.start()
        self.assertEqual(len(state["children"]), 2)
        self.assertEqual(supervisor.control.capacity(), {"instances": 1, "physical_gpus": 2})
        for command, kwargs in self.commands:
            self.assertNotIn("--endpoint", command)
            self.assertNotIn("--database-url", command)
            self.assertNotIn("env", kwargs)
            self.assertEqual(kwargs["stdout"], __import__("subprocess").DEVNULL)
            self.assertIn("--config-hash", command)
        result = supervisor.shutdown()
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(supervisor.control.capacity()["physical_gpus"], 2)
        for slot in slots:
            self.assertEqual(supervisor.control.get(slot.spec.worker_id)["drain_requested"], 1)
        text = (self.directory/"fleet-state.json").read_text(encoding="utf-8")
        self.assertNotIn("endpoint", text)
        self.assertNotIn("model", text)

    def test_failed_or_stuck_process_is_not_restarted_or_assumed_to_free_gpu(self):
        supervisor = self.supervisor(self.config(self.real_slot()))
        supervisor.start()
        self.processes[0].drain_exits = False
        result = supervisor.shutdown()
        self.assertEqual(result["state"], "drain_pending")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(supervisor.control.capacity()["physical_gpus"], 1)
        self.processes[0].code = 9
        supervisor.tick()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(supervisor.control.get("worker0")["drain_requested"], 1)

    def test_start_failure_drains_only_current_owned_children(self):
        slots = [self.real_slot(), self.real_slot("worker1", gpu="fake-gpu1", endpoint="http://127.0.0.1:18001")]
        supervisor = self.supervisor(self.config(*slots))
        original = supervisor.popen
        def fail_second(command, **kwargs):
            if self.processes:
                raise OSError("synthetic spawn failure")
            return original(command, **kwargs)
        supervisor.popen = fail_second
        with self.assertRaises(RuntimeError):
            supervisor.start()
        self.assertTrue((self.directory/"worker0"/"drain.flag").exists())
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(supervisor.control.capacity()["physical_gpus"], 2)

    def test_disabled_cli_never_loads_runtime_settings_or_credentials(self):
        self.write_config(FleetConfig(self.directory))
        output = io.StringIO()
        with patch("studio_platform.settings.Settings.from_environment", side_effect=AssertionError("no runtime loading")), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--config", str(self.config_path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "disabled")
        self.assertFalse(self.directory.exists())
        self.assertEqual(read_config(self.config_path).fingerprint(), FleetConfig(self.directory).fingerprint())

    def test_child_rejects_changed_config_before_loading_settings(self):
        config = self.config(self.mock_slot())
        self.write_config(config)
        output = io.StringIO()
        with patch("studio_platform.settings.Settings.from_environment", side_effect=AssertionError("not allowed")), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--config", str(self.config_path), "--slot", "mock-test", "--config-hash", "0"*64]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"state": "fleet_configuration_or_runtime_error"})

    def test_portable_stop_request_uses_marker_not_historical_pid_or_runtime_credentials(self):
        config = self.config(self.mock_slot())
        self.write_config(config)
        supervisor = self.supervisor(config)
        supervisor.start()
        output = io.StringIO()
        with patch("studio_platform.settings.Settings.from_environment", side_effect=AssertionError("no credential load")), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--config", str(self.config_path), "--request-drain"]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "drain_requested")
        supervisor.tick()
        self.assertEqual(supervisor.shutdown()["state"], "stopped")
        with self.assertRaisesRegex(ValueError, "fleet_drain_marker_requires_explicit_reset"):
            self.supervisor(config).start()

    def test_injected_child_keeps_repo_store_on_cpu_and_passes_guard_stop_hook(self):
        config = self.config(self.mock_slot())
        settings = Settings(Path(self.temp.name), auth_mode="local-test", execution_backend="mock", generation_enabled=True)
        marker, captures = object(), []
        class FakeRunner:
            def __init__(inner, repo, store, directory, **kwargs):
                captures.append((repo, store, directory, kwargs))
            def run_once(inner, worker, pool):
                return {"state": "idle", "simulation": True}
        result = run_slot(config, "mock-test", settings, repository=self.repo,
            store_factory=lambda _: marker, backend_factory=lambda slot, directory: MockBackend(directory/"simulation", enabled=True),
            runner_factory=FakeRunner, once=True)
        self.assertEqual(result["state"], "idle")
        repo, store, directory, kwargs = captures[0]
        self.assertIs(repo, self.repo)
        self.assertIs(store, marker)
        self.assertTrue(callable(kwargs["submission_guard"]))
        self.assertFalse(kwargs["stop_requested"]())
        directory.mkdir(parents=True)
        (directory/"drain.flag").touch()
        self.assertTrue(kwargs["stop_requested"]())

    def test_real_readiness_declaration_also_requires_current_empty_private_queue(self):
        slot = replace(self.real_slot(), confirmed_idle=True)
        config = self.config(slot)
        settings = Settings(Path(self.temp.name), auth_mode="local-test", execution_backend="comfy-worker", generation_enabled=True)
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
        kwargs = dict(repository=self.repo, store_factory=lambda _: stores.append(True) or object(),
            backend_factory=lambda *_: FakeBackend(), runner_factory=FakeRunner, once=True)
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
        settings = Settings(Path(self.temp.name), auth_mode="local-test", execution_backend="comfy-worker", generation_enabled=True)
        stores, runners, closed = [], [], []
        for evidence in (None, 1, "ready", {"ready": True}, [True], BackendError("probe_unavailable")):
            with self.subTest(evidence=evidence):
                class FakeBackend:
                    enabled, kind, slot_key = True, "comfy-worker", "fake-only-private-origin"
                    def is_idle(inner):
                        if isinstance(evidence, Exception):
                            raise evidence
                        return evidence
                    def close(inner):
                        closed.append(True)
                expected = BackendError if isinstance(evidence, Exception) else ValueError
                with self.assertRaises(expected):
                    run_slot(config, "worker0", settings, repository=self.repo,
                        store_factory=lambda _: stores.append(True),
                        backend_factory=lambda *_: FakeBackend(),
                        runner_factory=lambda *args, **kwargs: runners.append(True), once=True)
                from studio_platform.control import WorkerControl
                self.assertNotEqual(WorkerControl(self.repo).get("worker0")["state"], "ready")
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

    def test_supervisor_restores_signal_handlers_after_known_children_exit(self):
        supervisor = self.supervisor(self.config(self.mock_slot()))
        original = supervisor.popen
        def exits(command, **kwargs):
            proc = original(command, **kwargs)
            proc.code = 0
            return proc
        supervisor.popen = exits
        prior = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        result = supervisor.run_forever(poll_interval_s=.01)
        self.assertEqual(result["state"], "stopped")
        for signum, handler in prior.items():
            self.assertEqual(signal.getsignal(signum), handler)
