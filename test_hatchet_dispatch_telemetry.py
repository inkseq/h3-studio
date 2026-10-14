"""Actual Hatchet callback/ledger stages, fake broker, no Cloud/GPU calls."""
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
import uuid
from sqlalchemy import insert, select, update

from studio_platform.control import WorkerControl
from studio_platform.hatchet_dispatch import BrokerConfig, HatchetSlotRunner, main, _host_telemetry_context
from studio_platform.operator_capacity import operator_commands, operator_nodes
from studio_platform.storage import LocalObjectStore
from studio_platform.telemetry import StageTelemetry
import test_hatchet_dispatch as dispatch_fixture
from test_platform_repository import LedgerCase
from test_platform_telemetry import RecordingLogger, RecordingMeter


class HatchetTelemetryTests(LedgerCase):
    make_job = dispatch_fixture.HatchetDispatchTests.make_job
    ready = dispatch_fixture.HatchetDispatchTests.ready
    message = dispatch_fixture.HatchetDispatchTests.message

    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)
        self.directory = Path(self.temp.name)
        self.store = LocalObjectStore(self.directory / "objects")
        self.config = BrokerConfig(token_file=self.directory / "token", server_url="http://127.0.0.1:8080",
            host_port="127.0.0.1:7070", tls=False, poll_interval_s=.1)

    def sdk(self, message, context):
        outer = self
        class SDK:
            def task(self, **_options):
                def decorate(operation):
                    self.operation = operation
                    return operation
                return decorate
            def worker(self, **options):
                outer.assertEqual(options["slots"], 1)
                outer.assertEqual(options["workflows"], [self.operation])
                return SimpleNamespace(start=lambda: setattr(self, "result", self.operation(message, context)))
        return SDK()

    def invoke_callback(self, *, telemetry, context=None):
        job = self.make_job(audio=True)
        self.ready()
        backend = dispatch_fixture.CountingMock(self.directory / "mock", enabled=True)
        context = context or SimpleNamespace(is_cancelled=False, workflow_run_id=str(uuid.uuid4()))
        sdk = self.sdk(self.message(job), context)
        # The production Linux guard stays enabled. Replace only this module's
        # os reference; stdlib Path and the real filesystem keep their platform.
        fake_sdk_module = ModuleType("hatchet_sdk")
        fake_sdk_module.DesiredWorkerLabel = lambda **options: SimpleNamespace(**options)
        fake_sdk_module.TTLBasedIdempotencyConfig = lambda **options: SimpleNamespace(**options)
        runner = HatchetSlotRunner(self.repo, self.store, self.directory / "worker", backend=backend,
            control=self.control, broker_config=self.config, collection_lock_dir=self.directory / "collection-lock",
            client_factory=lambda _config: sdk, submission_guard=lambda _job: True, telemetry=telemetry,
            telemetry_context={"dstack_run": "sixnine-test-runtime", "gpu_type": "rtx5090",
                "prompt": "SECRET prompt", "authorization": "SECRET token", "url": "SECRET signed URL"})
        original_sdk = sys.modules.get("hatchet_sdk")
        sys.modules["hatchet_sdk"] = fake_sdk_module
        try:
            with patch("studio_platform.hatchet_dispatch.os", SimpleNamespace(name="posix")):
                runner.run_forever("worker", "hatchet-test")
        finally:
            # Restore only the seam we own, preserving ordinary lazy ledger
            # imports and their SQLAlchemy metadata from the real callback.
            if original_sdk is None:
                sys.modules.pop("hatchet_sdk", None)
            else:
                sys.modules["hatchet_sdk"] = original_sdk
        self.assertEqual(sdk.result, {"job_id": job["id"], "state": "succeeded"})
        self.assertEqual(backend.submits, 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(len(self.repo.list_artifacts(self.scope, job["id"])), 2)
        return job, context

    def test_actual_callback_emits_original_stage_boundaries_and_closed_run_context(self):
        from opentelemetry.trace import NonRecordingSpan, SpanContext
        logger = RecordingLogger()
        tracer = SimpleNamespace(start_span=lambda *_args, **_kwargs: NonRecordingSpan(SpanContext(1, 2, False)))
        telemetry = StageTelemetry(logger=logger, tracer=tracer, meter=RecordingMeter())
        job, context = self.invoke_callback(telemetry=telemetry)
        finished = [event for event in logger.events if event["event"] == "stage_finished"]
        self.assertEqual([event["stage"] for event in finished], ["queue_wait", "input_transfer", "generate",
            "collect", "validate_output", "upload", "commit_result", "end_to_end"])
        self.assertTrue(all(event["job_id"] == job["id"] and event["hatchet_run"] == context.workflow_run_id
            and event["dstack_run"] == "sixnine-test-runtime" and event["provider"] == "local"
            and event["gpu_type"] == "rtx5090" for event in finished))
        self.assertTrue(all(event["simulation"] for event in finished))
        self.assertNotIn("SECRET", json.dumps(logger.events))

    def test_broken_exporter_or_run_identity_getter_cannot_interrupt_original_job(self):
        class Context:
            is_cancelled = False
            @property
            def workflow_run_id(self):
                raise RuntimeError("SECRET SDK context detail")
        telemetry = Mock()
        telemetry.start.side_effect = RuntimeError("SECRET exporter unavailable")
        self.invoke_callback(telemetry=telemetry, context=Context())
        self.assertTrue(telemetry.start.called)

    def test_worker_entrypoint_passes_optional_exporter_and_flushes_closes_on_return_and_failure(self):
        for fails in (False, True):
            telemetry = Mock()
            fleet = SimpleNamespace(work_dir=self.directory)
            def run_slot(_fleet, _worker_id, _settings, *, runner_factory):
                runner_factory()
                if fails:
                    raise RuntimeError("original worker failure")
                return {"state": "stopped"}
            with patch("studio_platform.settings.Settings.from_environment", return_value=SimpleNamespace()), \
                    patch("studio_platform.hatchet_dispatch.read_broker_config", return_value=self.config), \
                    patch("studio_platform.hatchet_dispatch.configured_telemetry", return_value=telemetry) as load, \
                    patch("studio_platform.hatchet_dispatch.HatchetSlotRunner") as factory, \
                    patch("studio_platform.fleet.read_config", return_value=fleet), \
                    patch("studio_platform.fleet.run_slot", side_effect=run_slot), \
                    patch.dict(os.environ, {"SIXNINE_TELEMETRY_CONFIG_FILE": str(self.directory / "protected-cloud.json")}):
                args = ["worker", "--broker-config", "broker.json", "--fleet-config", "fleet.json", "--worker-id", "worker"]
                if fails:
                    with self.assertRaisesRegex(RuntimeError, "^original worker failure$"):
                        main(args)
                else:
                    self.assertEqual(main(args), {"state": "stopped"})
                load.assert_called_once_with()
                self.assertIs(factory.call_args.kwargs["telemetry"], telemetry)
            telemetry.force_flush.assert_called_once_with(timeout_millis=1000)
            telemetry.close.assert_called_once_with()

    def test_shutdown_export_failure_preserves_worker_result(self):
        telemetry = Mock()
        telemetry.force_flush.side_effect = RuntimeError("SECRET flush diagnostic")
        telemetry.close.side_effect = RuntimeError("SECRET close diagnostic")
        with patch("studio_platform.settings.Settings.from_environment", return_value=SimpleNamespace()), \
                patch("studio_platform.hatchet_dispatch.read_broker_config", return_value=self.config), \
                patch("studio_platform.hatchet_dispatch.configured_telemetry", return_value=telemetry), \
                patch("studio_platform.fleet.read_config", return_value=SimpleNamespace(work_dir=self.directory)), \
                patch("studio_platform.fleet.run_slot", return_value={"state": "stopped"}):
            self.assertEqual(main(["worker", "--broker-config", "broker.json", "--fleet-config", "fleet.json",
                "--worker-id", "worker"]), {"state": "stopped"})
        telemetry.close.assert_called_once_with()

    def test_real_optional_loader_reads_only_explicit_protected_file_at_worker_entrypoint(self):
        import base64
        document = {"schema_version": 1, "enabled": True,
            "endpoint": "https://otlp-gateway-prod-au-southeast-1.grafana.net/otlp",
            "authorization": "Basic " + base64.b64encode(b"100:offline-test-token").decode(),
            "service_name": "sixnine-worker", "service_version": "a" * 40, "environment": "local"}
        protected = self.directory / "protected-cloud.json"
        protected.write_text(json.dumps(document), encoding="utf-8")
        protected.chmod(0o600)
        telemetry = Mock()
        with patch("studio_platform.settings.Settings.from_environment", return_value=SimpleNamespace()), \
                patch("studio_platform.hatchet_dispatch.read_broker_config", return_value=self.config), \
                patch("studio_platform.telemetry.create_cloud_telemetry", return_value=telemetry) as create, \
                patch("studio_platform.fleet.read_config", return_value=SimpleNamespace(work_dir=self.directory)), \
                patch("studio_platform.fleet.run_slot", return_value={"state": "stopped"}), \
                patch.dict(os.environ, {"SIXNINE_TELEMETRY_CONFIG_FILE": str(protected)}):
            self.assertEqual(main(["worker", "--broker-config", "broker.json", "--fleet-config", "fleet.json",
                "--worker-id", "worker"]), {"state": "stopped"})
            self.assertEqual(create.call_args.args[0].service_name, "sixnine-worker")
            self.assertNotIn("offline-test-token", repr(create.call_args.args[0]))
        telemetry.force_flush.assert_called_once_with(timeout_millis=1000)
        telemetry.close.assert_called_once_with()


class HostContextTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.repo.configure_pool("host-context-test", max_instances=2, max_physical_gpus=2)
        self.node = self.repo.reserve_instance_intent(self.scope, "host-context-test", "host-context-one",
            reserved_cost_microusd=100, hard_deadline=self.now+1000,
            budget_account_ids=("owner-budget",), provider="vast", dry_run=False)["id"]
        self.worker = "dstack-" + self.node.replace("-", "")
        self.profile = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
        self.spec = {"backend": "wangp-worker", "provider": "vast", "instance_id": "123",
            "configuration_id": "context-test-native", "engine_manifest_digest": "a"*64}
        host = {"version": 1, "provider": "vastai", "machine_id": "19492", "provider_instance_id": "123",
            "offer_id": "42", "observed_at": self.now, "region": "kr-southkorea", "gpu": "RTX5090",
            "gpu_count": 1, "allocated_ram_mib": 29*1024, "allocated_vram_mib": 32*1024,
            "cpu": 8, "disk_mib": 128*1024, "reliability": .99, "quoted_hourly_microusd": 600000,
            "identity_confirmed": True, "physical_id_source": "vast_machine_id"}
        payload = {"tenant_id": self.scope.tenant_id, "owner_id": self.scope.owner_id,
            "capacity_backend": "dstack-v1", "host_observation": host,
            "private_endpoint": "SECRET private endpoint", "dstack": {"backend": "vastai",
                "profile_id": self.profile, "mode": "fl", "provider_instance_id": "123",
                "configuration_id": "context-test-native", "manifest_digest": "a"*64,
                "run_name": "sixnine-" + self.node.replace("-", ""),
                "observed_at": self.now, "runtime_incarnation": "c"*32,
                "preparation": {"model": {"state": "loaded"}}}}
        command = str(uuid.uuid4())
        with self.repo.transaction() as connection:
            connection.execute(insert(operator_commands).values(id=command, actor="test-operator",
                idempotency_key="context-original-key", request_hash="a"*64, kind="dstack_start",
                state="accepted", payload={}, created_at=self.now, updated_at=self.now))
            connection.execute(insert(operator_nodes).values(intent_id=self.node, command_id=command,
                ordinal=0, binding_id="test-original-binding", binding_hash="a"*64, payload=payload,
                desired_state="running", runtime_state="ready", updated_at=self.now))
        self.job_id = self.context_job(self.scope, "original-context-job")

    def context_job(self, scope, key):
        plan = self.repo.create_plan(scope, {"deployment_profile_id": self.profile,
            "request": {"mode": "fl", "prompt": "SECRET private prompt"}},
            {"configuration_id": "context-test-native"}, expires_at=self.now+1000,
            estimated_cost_microusd=0)
        return self.repo.create_job(scope, plan["id"], key)["id"]

    def context(self, **overrides):
        return _host_telemetry_context(self.repo, **{"node_id": self.node,
            "worker_id": self.worker, "spec": self.spec, "job_id": self.job_id, **overrides})

    def test_exact_host_context_is_closed_and_preserves_billing(self):
        budget = self.repo.get_budget("owner-budget")
        value = self.context()
        self.assertEqual(value["machine_id"], "19492")
        self.assertEqual(value["host_identity_source"], "vast_machine_id")
        self.assertEqual(value["quoted_hourly_microusd"], 600000)
        self.assertEqual(value["gpu_type"], "rtx5090")
        self.assertEqual(value["warmth"], "warm")
        self.assertNotIn("SECRET", json.dumps(value))
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)

    def test_foreign_owner_slot_configuration_or_instance_never_gets_host_context(self):
        foreign = self.context_job(self.other, "foreign-context-job")
        self.assertEqual(self.context(job_id=foreign), {})
        self.assertEqual(self.context(worker_id="different-worker"), {})
        for field, value in (("instance_id", "456"), ("configuration_id", "changed"),
                ("engine_manifest_digest", "b"*64), ("provider", "runpod")):
            self.assertEqual(self.context(spec={**self.spec, field: value}), {})
        self.assertEqual(self.context(node_id="missing-or-unknown"), {})

    def test_stale_model_observation_is_unknown_not_warm_and_read_failure_is_failsoft(self):
        self.now += 31
        self.assertEqual(self.context()["warmth"], "unknown")
        with patch.object(self.repo.engine, "connect", side_effect=RuntimeError("SECRET database detail")):
            self.assertEqual(self.context(), {})


if __name__ == "__main__":
    unittest.main()
