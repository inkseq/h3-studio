"""Pinned REST and journal semantics with fake capacity; never leases a GPU."""
from dataclasses import replace
import copy
import json
import importlib.util
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from studio_platform.dstack_capacity import (CapacityRequest, DstackCapacity,
    DstackClient, DstackError, idle_action, native_bootstrap_config, main)
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.worker_admission import managed_window_reason

INTENT = "a1111111-1111-4111-8111-111111111111"
RUN = "b2222222-2222-4222-8222-222222222222"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey"


def request(**changes):
    return replace(CapacityRequest(INTENT, "vastai", "h3-pruned-int8-v1", "fl", "h3-int8",
        "config-1", "a" * 64, "example/runtime@sha256:" + "b" * 64, ("RTX5090",),
        30, 160, 850000, 10., 3610., 3600), **changes)


class Store:
    def __init__(self):
        self.value = {"intent_id": INTENT}
        self.lock = threading.Lock()
        self.obligations = False
        self.events = []
        self.clock = lambda: 20.

    def load(self, intent_id):
        self.assert_identity(intent_id)
        with self.lock:
            return copy.deepcopy(self.value)

    def assert_identity(self, intent_id):
        assert intent_id == INTENT

    def begin_apply(self, intent_id, binding):
        self.assert_identity(intent_id)
        with self.lock:
            if self.value.get("apply_started"):
                return False
            self.value.update(copy.deepcopy(binding), apply_started=True)
            self.events.append("apply_committed")
            return True

    def record(self, intent_id, observation):
        self.assert_identity(intent_id)
        with self.lock:
            self.value.update(copy.deepcopy(observation))

    def begin_stop(self, intent_id, run_id, *, retry=False):
        self.assert_identity(intent_id)
        with self.lock:
            assert self.value["run_id"] == run_id
            now = self.clock()
            if self.obligations:
                return False
            if retry:
                if (not self.value.get("stop_started") or self.value.get("stop_attempt_count", 0) >= 5
                        or now < self.value.get("stop_next_retry_at", float("inf"))
                        or self.value.get("dstack_status") != "running"
                        or not 0 <= now - self.value.get("observed_at", 0) <= 30
                        or not self.value.get("provider_instance_id")):
                    return False
            elif self.value.get("stop_started"):
                return False
            count = self.value.get("stop_attempt_count", 0) + 1
            self.value.update(stop_started=True, stop_attempt_count=count,
                stop_last_attempt_at=now, stop_next_retry_at=now + min(30 * 2**(count-1), 300))
            self.events.append("stop_retry_committed" if retry else "stop_committed")
            return True


class FakeClient:
    project = "sixnine"

    def __init__(self, store):
        self.store = store
        self.run = None
        self.apply_count = self.stop_count = 0
        self.apply_lost = self.stop_lost = False
        self.stop_not_delivered = False
        self.get_failed = False

    def get(self, **kw):
        if self.get_failed:
            raise DstackError("dstack_api_unavailable")
        return copy.deepcopy(self.run)

    def apply(self, spec):
        assert self.store.value["apply_started"]
        self.apply_count += 1
        self.run = {"id": RUN, "project_name": self.project, "run_spec": copy.deepcopy(spec),
            "status": "submitted", "cost": 0.1}
        if self.apply_lost:
            raise DstackError("dstack_api_unavailable")
        return copy.deepcopy(self.run)

    def stop(self, name):
        assert self.store.value["stop_started"]
        assert name == self.run["run_spec"]["run_name"]
        self.stop_count += 1
        if self.stop_not_delivered:
            raise DstackError("dstack_api_unavailable")
        self.run["status"] = "terminating"
        if self.stop_lost:
            raise DstackError("dstack_api_unavailable")

    def running(self):
        self.run.update(status="running", latest_job_submission={"job_provisioning_data": {
            "backend": "vastai", "instance_id": "1234567", "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}}}})


class DstackCapacityTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.client = FakeClient(self.store)
        self.native = Mock(return_value=HostReadiness("a" * 64, INTENT, "c" * 32, True))
        self.now = 20.
        self.store.clock = lambda: self.now
        self.capacity = DstackCapacity(self.client, self.store, clock=self.store.clock, readiness=self.native)

    def start(self):
        return self.capacity.start(request(), PUB)

    def test_journal_precedes_apply_and_repeat_never_reapplies(self):
        self.start()
        self.capacity.start(request(), PUB)
        self.assertEqual(self.client.apply_count, 1)
        self.assertEqual(self.store.events, ["apply_committed"])

    def test_lost_apply_response_reconciles_same_run(self):
        self.client.apply_lost = True
        observed = self.start()
        self.assertEqual(observed["run_id"], RUN)
        self.capacity.start(request(), PUB)
        self.assertEqual(self.client.apply_count, 1)

    def test_absent_exact_get_is_unknown_not_create_proof(self):
        self.start()
        self.client.run = None
        for _ in range(3):
            self.assertEqual(self.capacity.start(request(), PUB)["state"], "creation_unknown")
        self.assertEqual(self.client.apply_count, 1)

    def test_binding_changes_cannot_replay_original_intent(self):
        self.start()
        with self.assertRaisesRegex(DstackError, "dstack_intent_binding_changed"):
            self.capacity.start(request(mode="ref"), PUB)
        self.assertEqual(self.client.apply_count, 1)

    def test_foreign_named_run_is_not_adopted_or_stopped(self):
        self.client.run = {"id": RUN, "project_name": "other"}
        with self.assertRaisesRegex(DstackError, "dstack_existing_run_requires_reconciliation"):
            self.start()
        self.assertEqual(self.client.apply_count, 0)

    def test_running_requires_native_identity_and_first_load_is_unknown(self):
        self.start()
        self.client.running()
        self.capacity.readiness = None
        value = self.capacity.observe(INTENT)
        self.assertFalse(value["ready"])
        self.capacity.readiness = self.native
        value = self.capacity.observe(INTENT)
        self.assertTrue(value["ready"])
        self.assertEqual(value["model_load_state"], "not_observed")
        self.assertEqual(value["estimated_cost_microusd"], 100000)
        self.assertEqual(value["billing_state"], "unsettled")

    def test_manifest_and_incarnation_changes_hold_slot(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        self.native.return_value = HostReadiness("a" * 64, INTENT, "d" * 32, True)
        value = self.capacity.observe(INTENT)
        self.assertEqual(value["reason_code"], "dstack_native_incarnation_changed")
        self.assertFalse(value["ready"])
        self.native.return_value = HostReadiness("b" * 64, INTENT, "c" * 32, True)
        self.assertEqual(self.capacity.observe(INTENT)["reason_code"], "dstack_native_binding_mismatch")

    def test_busy_model_retains_identity_and_disables_admission(self):
        self.start()
        self.client.running()
        self.native.return_value = HostReadiness("a" * 64, INTENT, "c" * 32, False)
        self.assertEqual(self.capacity.observe(INTENT)["state"], "busy")

    def managed_reason(self):
        binding = self.store.load(INTENT)
        intent = {"state": "ready", "hard_deadline": request().hard_deadline,
            "provider_instance_id": binding.get("provider_instance_id")}
        node = {"desired_state": "running", "runtime_state": binding.get("state"), "payload": {
            "capacity_backend": "dstack-v1", "observation_fresh_seconds": 30,
            "selection": {"runtime_profile_id": request().profile_id}, "dstack": binding,
            "dstack_observation": {"run_id": binding.get("run_id"),
                "instance_id": binding.get("provider_instance_id"),
                "status": binding.get("dstack_status"), "observed_at": binding.get("observed_at")}}}
        return managed_window_reason(intent, node, self.now, deployment_profile_id=request().profile_id)

    def test_cold_callback_still_journals_exact_instance_before_bootstrap(self):
        self.start()
        self.client.running()
        def native(binding, run):
            recorded = self.store.load(INTENT)
            self.assertEqual(recorded["provider_instance_id"], "1234567")
            self.assertEqual(recorded["run_id"], RUN)
            self.assertEqual(recorded["state"], "runtime_unconfirmed")
            self.assertFalse(recorded["ready"])
            self.assertNotIn("runtime_incarnation", recorded)
            self.assertEqual(self.managed_reason(), "managed_dstack_readiness_unconfirmed")
            return HostReadiness("a" * 64, INTENT, "c" * 32, True)
        self.native.side_effect = native
        self.assertTrue(self.capacity.observe(INTENT)["ready"])
        self.assertIsNone(self.managed_reason())

    def test_warm_check_preserves_proof_but_does_not_extend_stale_freshness(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        prior = self.store.load(INTENT)
        self.now = 25.
        entered, release = threading.Event(), threading.Event()
        results = []
        def native(binding, run):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test callback not released")
            return HostReadiness("a" * 64, INTENT, "c" * 32, True)
        self.native.side_effect = native
        thread = threading.Thread(target=lambda: results.append(self.capacity.observe(INTENT)))
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.assertEqual(self.store.load(INTENT), prior)
            self.assertIsNone(self.managed_reason())
            self.now = 51.
            self.assertEqual(self.managed_reason(), "managed_dstack_readiness_unconfirmed")
            self.assertEqual(self.store.load(INTENT)["observed_at"], 20.)
            self.now = 60.
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]["ready"])
        self.assertEqual(results[0]["observed_at"], 25., "Native completion cannot renew an older provider read")
        self.assertEqual(self.managed_reason(), "managed_dstack_readiness_unconfirmed")
        self.native.side_effect = None
        self.now = 61.
        self.capacity.observe(INTENT)
        self.assertIsNone(self.managed_reason())
        self.assertEqual(self.store.load(INTENT)["runtime_incarnation"], prior["runtime_incarnation"])
        self.assertEqual(self.client.apply_count, 1)

    def test_pending_warm_busy_check_preserves_busy_then_actual_failure_revokes(self):
        self.start()
        self.client.running()
        self.native.return_value = HostReadiness("a" * 64, INTENT, "c" * 32, False)
        self.capacity.observe(INTENT)
        prior = self.store.load(INTENT)
        self.now = 25.
        def native(binding, run):
            self.assertEqual(self.store.load(INTENT), prior)
            self.assertEqual(binding["state"], "busy")
            self.assertIsNone(self.managed_reason())
            raise DstackError("dstack_runtime_preparing")
        self.native.side_effect = native
        value = self.capacity.observe(INTENT)
        self.assertFalse(value["ready"])
        self.assertEqual(value["state"], "runtime_unconfirmed")
        self.assertEqual(value["reason_code"], "dstack_runtime_preparing")
        self.assertEqual(self.managed_reason(), "managed_dstack_readiness_unconfirmed")
        self.assertEqual(self.store.load(INTENT)["runtime_incarnation"], prior["runtime_incarnation"])

    def test_changed_warm_provider_is_revoked_before_native_callback(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        self.native.reset_mock()
        self.client.run["latest_job_submission"]["job_provisioning_data"]["instance_id"] = "changed-instance"
        value = self.capacity.observe(INTENT)
        self.assertFalse(value["ready"])
        self.assertEqual(value["reason_code"], "dstack_instance_identity_changed")
        self.assertEqual(self.managed_reason(), "managed_dstack_readiness_unconfirmed")
        self.native.assert_not_called()
        self.assertEqual(self.store.load(INTENT)["provider_instance_id"], "1234567")

    def test_failed_observation_revokes_cached_ready(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        self.client.get_failed = True
        self.assertFalse(self.capacity.observe(INTENT)["ready"])

    def test_native_preparing_retains_safe_progress_and_revokes_ready(self):
        self.start()
        self.client.running()
        self.assertTrue(self.capacity.observe(INTENT)["ready"])
        self.native.side_effect = DstackError("dstack_runtime_preparing")
        value = self.capacity.observe(INTENT)
        self.assertEqual(value["reason_code"], "dstack_runtime_preparing")
        self.assertEqual(value["state"], "runtime_unconfirmed")
        self.assertFalse(value["ready"])
        self.assertFalse(self.store.value["ready"])
        self.assertEqual(value["provider_instance_id"], "1234567")
        self.assertEqual(self.client.apply_count, 1)

    def test_native_errors_never_expose_arbitrary_callback_text(self):
        self.start()
        self.client.running()
        for error in (RuntimeError("secret prompt and token"),
                      DstackError("secret prompt and token"),
                      DstackError("dstack_runtime_preparing", "secret prompt and token")):
            with self.subTest(error_type=type(error).__name__, argument_count=len(error.args)):
                self.native.side_effect = error
                value = self.capacity.observe(INTENT)
                self.assertEqual(value["reason_code"], "dstack_native_observation_failed")
                self.assertEqual(value["state"], "runtime_unconfirmed")
                self.assertFalse(value["ready"])
                self.assertNotIn("secret", json.dumps(value))

    def test_changed_resource_configuration_is_rejected(self):
        self.start()
        self.client.run["run_spec"]["configuration"]["resources"]["gpu"]["count"] = 2
        self.assertEqual(self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch")

    def test_stop_preserves_business_obligations(self):
        self.start()
        self.store.obligations = True
        self.assertEqual(self.capacity.stop(INTENT)["state"], "draining")
        self.assertEqual(self.client.stop_count, 0)
        self.assertNotIn("stop_started", self.store.value)

    def test_accepted_lost_stop_ack_is_not_replayed_or_billed_as_zero(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        self.client.stop_lost = True
        self.assertEqual(self.capacity.stop(INTENT)["state"], "removal_unknown")
        self.now += 120
        self.capacity.stop(INTENT)
        self.assertEqual(self.client.stop_count, 1)
        self.client.run["status"] = "terminated"
        value = self.capacity.observe(INTENT)
        self.assertEqual(value["state"], "stopped")
        self.assertEqual(value["billing_state"], "unsettled")

    def interrupted_stop(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        self.client.stop_not_delivered = True
        self.assertEqual(self.capacity.stop(INTENT)["state"], "removal_unknown")

    def test_stop_not_delivered_retries_original_run_after_durable_backoff(self):
        self.interrupted_stop()
        self.now = 49.
        self.assertEqual(self.capacity.stop(INTENT)["reason_code"], "dstack_stop_retry_waiting")
        self.assertEqual(self.client.stop_count, 1)
        self.now = 50.
        # A new transport/controller must honor the same original retry journal.
        restarted = DstackCapacity(self.client, self.store, clock=self.store.clock, readiness=self.native)
        self.client.stop_not_delivered = False
        self.assertEqual(restarted.stop(INTENT)["state"], "stopping")
        self.assertEqual(self.client.stop_count, 2)
        self.assertEqual(self.client.apply_count, 1)
        self.assertEqual(self.store.value["run_id"], RUN)
        self.assertEqual(self.store.value["stop_attempt_count"], 2)
        self.assertEqual(self.store.value["stop_next_retry_at"], 110.)
        self.assertEqual(self.store.events.count("stop_committed"), 1)
        self.assertEqual(self.store.events.count("stop_retry_committed"), 1)
        self.assertEqual(self.native.call_count, 1, "A stopped binding must not bootstrap native runtime")

    def test_stop_retry_unknown_absent_changed_or_nonrunning_is_read_only(self):
        self.interrupted_stop()
        self.now = 100.
        original = copy.deepcopy(self.client.run)
        for status in ("submitted", "provisioning", "terminating", "terminated"):
            with self.subTest(status=status):
                self.client.run = {**copy.deepcopy(original), "status": status}
                self.capacity.stop(INTENT)
                self.assertEqual(self.client.stop_count, 1)
        for mutation in ("run_id", "project", "instance", "gpu_count"):
            with self.subTest(mutation=mutation):
                self.client.run = copy.deepcopy(original)
                if mutation == "run_id":
                    self.client.run["id"] = "c3333333-3333-4333-8333-333333333333"
                elif mutation == "project":
                    self.client.run["project_name"] = "foreign"
                elif mutation == "instance":
                    self.client.run["latest_job_submission"]["job_provisioning_data"]["instance_id"] = "changed-instance"
                else:
                    self.client.run["latest_job_submission"]["job_provisioning_data"]["instance_type"]["resources"]["gpus"] *= 2
                self.assertEqual(self.capacity.stop(INTENT)["state"], "observation_unknown")
                self.assertEqual(self.client.stop_count, 1)
        self.client.run = None
        self.assertEqual(self.capacity.stop(INTENT)["state"], "removal_unknown")
        self.client.run = original
        self.client.get_failed = True
        self.assertEqual(self.capacity.stop(INTENT)["state"], "observation_unknown")
        self.assertEqual(self.client.stop_count, 1)
        self.assertEqual(self.client.apply_count, 1)

    def test_stop_retry_cap_preserves_explicit_manual_reconciliation(self):
        self.interrupted_stop()
        for now, count, due in ((50., 2, 110.), (110., 3, 230.), (230., 4, 470.), (470., 5, 770.)):
            self.now = now
            self.assertEqual(self.capacity.stop(INTENT)["state"], "removal_unknown")
            self.assertEqual(self.store.value["stop_attempt_count"], count)
            self.assertEqual(self.store.value["stop_next_retry_at"], due)
        self.now = 10000.
        value = self.capacity.stop(INTENT)
        self.assertEqual(value["reason_code"], "dstack_stop_retry_exhausted")
        self.assertFalse(value["ready"])
        self.assertEqual(value["billing_state"], "unsettled")
        self.assertEqual(self.client.stop_count, 5)
        self.assertEqual(self.client.apply_count, 1)

    def test_stop_retry_missing_journal_and_new_obligations_fail_closed(self):
        self.interrupted_stop()
        self.now = 50.
        self.store.obligations = True
        self.assertEqual(self.capacity.stop(INTENT)["reason_code"], "dstack_business_obligations_pending")
        self.store.obligations = False
        del self.store.value["stop_next_retry_at"]
        self.assertEqual(self.capacity.stop(INTENT)["reason_code"], "dstack_stop_retry_journal_unconfirmed")
        self.assertEqual(self.client.stop_count, 1)

    def test_concurrent_stop_recovery_consumes_one_original_retry_per_window(self):
        self.interrupted_stop()
        self.now = 50.
        barrier = threading.Barrier(2)
        original_get = self.client.get
        def get(**kw):
            value = original_get(**kw)
            barrier.wait(timeout=5)
            return value
        self.client.get = get
        failures = []
        def stop():
            try:
                DstackCapacity(self.client, self.store, clock=self.store.clock).stop(INTENT)
            except Exception as error:
                failures.append(type(error).__name__)
        threads = [threading.Thread(target=stop) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures)
        self.assertEqual(self.client.stop_count, 2)
        self.assertEqual(self.client.apply_count, 1)
        self.assertEqual(self.store.value["stop_attempt_count"], 2)

    def test_single_gpu_and_digest_are_required(self):
        with self.assertRaisesRegex(DstackError, "dstack_single_gpu_required"):
            request(gpu_count=2)
        with self.assertRaisesRegex(DstackError, "dstack_image_digest_required"):
            request(image="example/runtime:latest")

    def test_supported_backend_never_falls_back(self):
        config = request(backend="runpod").run_spec(PUB)["configuration"]
        self.assertEqual(config["backends"], ["runpod"])
        with self.assertRaisesRegex(DstackError, "dstack_backend_unsupported"):
            request(backend="lium")

    def test_resource_floors_and_optional_regions_use_pinned_range_semantics(self):
        config = request().run_spec(PUB)["configuration"]
        self.assertEqual(config["resources"]["cpu"], {"arch": "x86", "count": "4.."})
        self.assertIsNone(config["regions"], "An empty region whitelist excludes every offer")
        self.assertEqual(request(regions=("us-ca",)).run_spec(PUB)["configuration"]["regions"], ["us-ca"])

    def test_parsed_cpu_floor_preserves_ownership_and_rejects_changed_requirement(self):
        self.start()
        cpu = self.client.run["run_spec"]["configuration"]["resources"]["cpu"]
        cpu["count"] = {"min": 4, "max": None}
        self.assertEqual(self.capacity.observe(INTENT)["state"], "starting")
        for changed in ({"min": 3, "max": None}, {"min": 4, "max": 4}):
            with self.subTest(count=changed):
                cpu["count"] = changed
                self.assertEqual(self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch")

    def test_server_added_region_restriction_cannot_be_adopted(self):
        self.start()
        self.client.run["run_spec"]["configuration"]["regions"] = []
        self.assertEqual(self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch")

    def test_pinned_homogeneous_single_node_default_preserves_binding(self):
        self.start()
        configuration = self.client.run["run_spec"]["configuration"]
        configuration["nodes"] = 1
        self.assertEqual(self.capacity.observe(INTENT)["state"], "starting")
        self.assertNotIn("nodes", self.store.value["run_spec"]["configuration"])
        for value in (0, 2, True, False, "1", {"min": 1, "max": 1}):
            with self.subTest(nodes=value):
                configuration["nodes"] = value
                self.assertEqual(self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch")
        configuration["nodes"] = 1
        configuration["groups"] = [{"nodes": 1}]
        self.assertEqual(self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch")

    def test_concurrent_start_commits_only_one_paid_apply(self):
        barrier = threading.Barrier(2)
        original_get = self.client.get
        first_reads = []
        def get(**kw):
            if "run_name" in kw and not self.store.value.get("apply_started"):
                first_reads.append(1)
                barrier.wait(timeout=5)
                return None
            return original_get(**kw)
        self.client.get = get
        failures = []
        def start():
            try:
                self.start()
            except Exception as exc:
                failures.append(type(exc).__name__)
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(failures)
        self.assertEqual(self.client.apply_count, 1)

    def test_multi_gpu_allocation_cannot_be_marked_ready(self):
        self.start()
        self.client.running()
        self.client.run["latest_job_submission"]["job_provisioning_data"]["instance_type"]["resources"]["gpus"] *= 2
        value = self.capacity.observe(INTENT)
        self.assertFalse(value["ready"])
        self.assertEqual(value["reason_code"], "dstack_allocated_gpu_count_unconfirmed")

    def test_expired_window_rejects_before_network(self):
        with self.assertRaisesRegex(DstackError, "dstack_window_expired"):
            self.capacity.start(request(hard_deadline=19), PUB)
        self.assertEqual(self.client.apply_count, 0)

    def test_pinned_model_parsed_response_is_compatible(self):
        # Optional SDK check, also run explicitly with dstack==0.22.3 locally.
        try:
            from dstack._internal.core.models.configurations import TaskConfiguration
        except ImportError:
            self.skipTest("optional pinned dstack SDK absent")
        self.start()
        config = self.client.run["run_spec"]["configuration"]
        self.client.run["run_spec"]["configuration"] = TaskConfiguration.model_validate(config).model_dump(mode="json")
        self.assertEqual(self.capacity.observe(INTENT)["state"], "starting")

    def test_idle_policy_distinguishes_holds_and_unknown_obligations(self):
        policy = dict(now=1000, hard_deadline=2000, hold_until=0, last_business_activity=10,
            idle_seconds=600, active_jobs=0, unsafe_attempts=0, collection_holds=0, observations_fresh=True)
        self.assertEqual(idle_action(**policy), "stop")
        self.assertEqual(idle_action(**{**policy, "hold_until": 1500}), "retain")
        self.assertEqual(idle_action(**{**policy, "collection_holds": 1}), "retain")
        self.assertEqual(idle_action(**{**policy, "now": 2100, "unsafe_attempts": 1}), "drain")
        self.assertEqual(idle_action(**{**policy, "observations_fresh": False}), "reconcile")

    @unittest.skipUnless(os.name == "posix", "GPU bootstrap validates Linux absolute paths")
    def test_native_bootstrap_keeps_original_one_slot_interface(self):
        path = Path(__file__).parent / "deploy" / "wangp" / "bootstrap.py"
        spec = importlib.util.spec_from_file_location("sixnine_inert_bootstrap", path)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        value = native_bootstrap_config(request(), source_bundle_sha256="d" * 64)
        # Validate grammar/interface without reading this CPU user's /root.
        # Actual GPU filesystem ownership remains the original bootstrap guard.
        with patch.object(bootstrap.Path, "is_symlink", return_value=False):
            self.assertEqual(bootstrap.validate_config(value), value)
        self.assertEqual(value["expected_host_gpus"], 1)
        self.assertEqual(value["profile_slot_index"], 0)
        self.assertFalse(value["dependency_artifact_url"])
        self.assertNotIn("token", json.dumps(value))

    def test_bootstrap_refuses_path_traversal(self):
        with self.assertRaisesRegex(DstackError, "dstack_runtime_path_invalid"):
            native_bootstrap_config(request(), source_bundle_sha256="d" * 64, model_root="/root/../private")


class DstackRESTTests(unittest.TestCase):
    def test_exact_get_accepts_only_pinned_not_found_errors(self):
        cases = [(404, {}, True),
            (400, {"detail": [{"code": "resource_not_exists", "msg": "Run not found"}]}, True),
            (400, {"detail": [{"code": "other_error"}]}, False),
            (400, {"detail": [{"code": "resource_not_exists"}, {"code": "other_error"}]}, False),
            (400, {"detail": []}, False), (400, [], False)]
        for status, body, missing in cases:
            with self.subTest(status=status, body=body):
                session = httpx.Client(transport=httpx.MockTransport(
                    lambda _, status=status, body=body: httpx.Response(status, json=body)))
                client = DstackClient("http://127.0.0.1:3000", "secret-token-123456", "sixnine",
                    session=session)
                if missing:
                    self.assertIsNone(client.get(run_id=RUN))
                else:
                    with self.assertRaisesRegex(DstackError, "^dstack_api_rejected$"):
                        client.get(run_id=RUN)

    def test_mutations_do_not_treat_not_found_as_success(self):
        session = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400,
            json={"detail": [{"code": "resource_not_exists", "msg": "Run not found"}]})))
        client = DstackClient("http://127.0.0.1:3000", "secret-token-123456", "sixnine", session=session)
        for method, argument in ((client.apply, request().run_spec(PUB)),
                                 (client.stop, request().run_name)):
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(DstackError, "^dstack_api_rejected$"):
                    method(argument)

    def test_exact_paths_and_create_only_apply(self):
        seen = []
        def handler(req):
            seen.append((req.url.path, json.loads(req.content), req.headers["Authorization"]))
            return httpx.Response(200, json={})
        client = DstackClient("http://127.0.0.1:3000", "secret-token-123456", "sixnine",
            session=httpx.Client(transport=httpx.MockTransport(handler)))
        client.plan(request().run_spec(PUB))
        client.apply(request().run_spec(PUB))
        client.get(run_id=RUN)
        client.stop(request().run_name)
        self.assertEqual([row[0] for row in seen], ["/api/project/sixnine/runs/get_plan",
            "/api/project/sixnine/runs/apply", "/api/project/sixnine/runs/get", "/api/project/sixnine/runs/stop"])
        self.assertFalse(seen[1][1]["force"])
        self.assertTrue(seen[0][1]["for_offers_only"])
        self.assertNotIn("for_offers_only", seen[1][1])
        self.assertIsNone(seen[1][1]["plan"]["current_resource"])
        self.assertFalse(seen[3][1]["abort"])

    def test_server_errors_never_expose_response_secrets(self):
        session = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(500, text="secret prompt and token")))
        client = DstackClient("http://localhost:3000", "secret-token-123456", "sixnine", session=session)
        with self.assertRaisesRegex(DstackError, "^dstack_api_rejected$"):
            client.get(run_id=RUN)

    def test_remote_plain_http_and_url_credentials_are_refused(self):
        for endpoint in ("http://remote.example", "https://user:password@example.com", "https://example.com/?key=a"):
            with self.assertRaisesRegex(DstackError, "dstack_endpoint_invalid"):
                DstackClient(endpoint, "secret-token-123456", "sixnine")


if __name__ == "__main__":
    unittest.main()
