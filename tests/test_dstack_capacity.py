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

from studio_platform.dstack_capacity import (
    CapacityRequest,
    DstackCapacity,
    DstackClient,
    DstackError,
    DstackReadRateLimited,
    idle_action,
    live_capacity_usage,
    native_bootstrap_config,
    terminal_no_allocation,
    main,
)
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.worker_admission import managed_window_reason

INTENT = "a1111111-1111-4111-8111-111111111111"
RUN = "b2222222-2222-4222-8222-222222222222"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey"


def request(**changes):
    return replace(
        CapacityRequest(
            INTENT,
            "vastai",
            "h3-pruned-int8-v1",
            "fl",
            "h3-int8",
            "config-1",
            "a" * 64,
            "example/runtime@sha256:" + "b" * 64,
            ("RTX5090",),
            30,
            160,
            850000,
            10.0,
            3610.0,
            3600,
        ),
        **changes,
    )


def no_capacity_run(run):
    """Pinned 0.22.3 terminal single-task response, no provider allocation."""
    submission = {
        "id": "c3333333-3333-4333-8333-333333333333",
        "status": "failed",
        "termination_reason": "failed_to_start_due_to_no_capacity",
        "termination_reason_message": "PRIVATE-PROVIDER-MESSAGE no offers",
        "job_provisioning_data": None,
        "job_runtime_data": None,
    }
    return {
        **copy.deepcopy(run),
        "status": "failed",
        "termination_reason": "job_failed",
        "latest_job_submission": copy.deepcopy(submission),
        "jobs": [{"job_submissions": [submission], "job_connection_info": None}],
    }


class Store:
    def __init__(self):
        self.value = {"intent_id": INTENT}
        self.lock = threading.Lock()
        self.obligations = False
        self.events = []
        self.clock = lambda: 20.0

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
                if (
                    not self.value.get("stop_started")
                    or self.value.get("stop_attempt_count", 0) >= 5
                    or now < self.value.get("stop_next_retry_at", float("inf"))
                    or self.value.get("dstack_status") != "running"
                    or not 0 <= now - self.value.get("observed_at", 0) <= 30
                    or not self.value.get("provider_instance_id")
                ):
                    return False
            elif self.value.get("stop_started"):
                return False
            count = self.value.get("stop_attempt_count", 0) + 1
            self.value.update(
                stop_started=True,
                stop_attempt_count=count,
                stop_last_attempt_at=now,
                stop_next_retry_at=now + min(30 * 2 ** (count - 1), 300),
            )
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
        self.run = {
            "id": RUN,
            "project_name": self.project,
            "run_spec": copy.deepcopy(spec),
            "status": "submitted",
            "cost": 0.1,
        }
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
        self.run.update(
            status="running",
            latest_job_submission={
                "job_provisioning_data": {
                    "backend": "vastai",
                    "instance_id": "1234567",
                    "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}},
                }
            },
        )


class DstackCapacityTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.client = FakeClient(self.store)
        self.native = Mock(return_value=HostReadiness("a" * 64, INTENT, "c" * 32, True))
        self.now = 20.0
        self.store.clock = lambda: self.now
        self.capacity = DstackCapacity(
            self.client, self.store, clock=self.store.clock, readiness=self.native
        )

    def start(self):
        return self.capacity.start(request(), PUB)

    def test_allocated_offer_telemetry_is_frozen_and_requires_matching_machine(self):
        selected = {
            "version": 1,
            "observed_at": 10.0,
            "expires_at": 130.0,
            "policy_snapshot": "d" * 64,
            "offers": [
                {
                    "offer_id": "12345",
                    "machine_id": "19493",
                    "country": "US",
                    "region": "us-ashburn",
                    "advertised_download_mbps": 1000,
                }
            ],
        }
        binding = request(host_selection=selected).binding(PUB)
        self.client.vast_instance_identity = lambda identity: {
            "provider_instance_id": identity,
            "machine_id": "19493",
            "reliability": 0.99,
        }
        provisioning = {
            "backend": "vastai",
            "instance_id": "1234567",
            "region": "us-ashburn",
            "price": 0.7,
            "instance_type": {
                "name": "12345",
                "resources": {
                    "cpus": 12,
                    "memory_mib": 96 * 1024,
                    "disk": {"size_mib": 128 * 1024},
                    "gpus": [{"name": "RTX5090", "memory_mib": 32 * 1024}],
                },
            },
        }
        before = copy.deepcopy(binding)
        physical = self.capacity._host_observation(provisioning, binding, 20.0)
        self.assertEqual(
            (
                physical["country"],
                physical["region"],
                physical["advertised_download_mbps"],
            ),
            ("US", "us-ashburn", 1000),
        )
        self.assertEqual(binding, before)
        old = copy.deepcopy(binding)
        old["host_selection"]["offers"] = [{"offer_id": "12345", "machine_id": "19493"}]
        legacy = self.capacity._host_observation(provisioning, old, 20.0)
        self.assertIsNone(legacy["country"])
        self.assertIsNone(legacy["advertised_download_mbps"])
        changed = copy.deepcopy(binding)
        changed["host_selection"]["offers"][0]["machine_id"] = "99999"
        with self.assertRaisesRegex(DstackError, "dstack_host_allocation_unconfirmed"):
            self.capacity._host_observation(provisioning, changed, 20.0)

    def test_journal_precedes_apply_and_repeat_never_reapplies(self):
        self.start()
        self.capacity.start(request(), PUB)
        self.assertEqual(self.client.apply_count, 1)
        self.assertEqual(self.store.events, ["apply_committed"])

    def test_terminal_only_refresh_refuses_nonterminal_absent_failed_and_foreign_reads(
        self,
    ):
        self.start()
        before = self.store.load(INTENT)
        original = copy.deepcopy(self.client.run)
        cases = [
            None,
            original,
            {**original, 'status': 'running'},
            {**original, 'status': 'failed', 'project_name': 'foreign-project'},
            {**original, 'status': 'failed', 'run_spec': {}},
            {**original, 'status': 'failed', 'latest_job_submission': 'malformed'},
        ]
        for run in cases:
            with self.subTest(status=run.get('status') if run else None):
                self.client.run = run
                self.assertIsNone(self.capacity.observe_terminal(INTENT))
                self.assertEqual(self.store.load(INTENT), before)
        self.client.get_failed = True
        self.assertIsNone(self.capacity.observe_terminal(INTENT))
        self.assertEqual(self.store.load(INTENT), before)
        self.native.assert_not_called()
        self.assertEqual((self.client.apply_count, self.client.stop_count), (1, 0))

    def test_terminal_only_refresh_reuses_owned_terminal_projection_without_native_call(
        self,
    ):
        self.start()
        self.client.run = no_capacity_run(self.client.run)
        result = self.capacity.observe_terminal(INTENT)
        self.assertEqual(result['state'], 'stopped')
        self.assertFalse(result['ready'])
        self.assertEqual(result['reason_code'], 'dstack_no_capacity_billing_pending')
        self.assertEqual(result['billing_state'], 'unsettled')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.native.assert_not_called()
        self.assertEqual((self.client.apply_count, self.client.stop_count), (1, 0))

    def test_no_capacity_observation_ends_without_native_start_or_settling_billing(
        self,
    ):
        self.start()
        self.client.run = no_capacity_run(self.client.run)
        value = self.capacity.observe(INTENT)
        self.assertEqual(
            (value["state"], value["ready"], value["billing_state"]),
            ("stopped", False, "unsettled"),
        )
        self.assertEqual(value["reason_code"], "dstack_no_capacity_billing_pending")
        self.assertEqual(value["preparation"]["phase"], "stopped")
        self.assertNotIn("PRIVATE-PROVIDER-MESSAGE", json.dumps(value))
        self.native.assert_not_called()
        self.assertEqual(self.client.apply_count, 1)
        self.assertEqual(self.client.stop_count, 0)

    def test_terminal_runpod_create_unknown_never_reapplies_or_settles(self):
        original = request(backend="runpod")
        self.capacity.start(original, PUB)
        self.client.run = no_capacity_run(self.client.run)
        submission = self.client.run["latest_job_submission"]
        submission["termination_reason"] = "terminated_by_server"
        for suffix in (
            '{"outcome":"unknown","pod_id":"original_pod"}',
            'invalid SECRET',
        ):
            submission["termination_reason_message"] = (
                "sixnine_runpod_outcome_v1:" + suffix
            )
            value = self.capacity.start(original, PUB)
            self.assertEqual(
                (value["state"], value["ready"], value["billing_state"]),
                ("creation_unknown", False, "unsettled"),
            )
            self.assertEqual(
                value["reason_code"], "dstack_allocation_requires_reconciliation"
            )
            self.assertNotIn("no_allocation_proof", value)
            self.assertNotIn("provider_instance_id", value)
            self.assertNotIn("SECRET", json.dumps(value))
            self.assertFalse(terminal_no_allocation(self.client.run, self.store.value))
        self.native.assert_not_called()
        self.assertEqual((self.client.apply_count, self.client.stop_count), (1, 0))

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
            self.assertEqual(
                self.capacity.start(request(), PUB)["state"], "creation_unknown"
            )
        self.assertEqual(self.client.apply_count, 1)

    def test_binding_changes_cannot_replay_original_intent(self):
        self.start()
        with self.assertRaisesRegex(DstackError, "dstack_intent_binding_changed"):
            self.capacity.start(request(mode="ref"), PUB)
        self.assertEqual(self.client.apply_count, 1)

    def test_foreign_named_run_is_not_adopted_or_stopped(self):
        self.client.run = {"id": RUN, "project_name": "other"}
        with self.assertRaisesRegex(
            DstackError, "dstack_existing_run_requires_reconciliation"
        ):
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
        self.assertEqual(
            self.capacity.observe(INTENT)["reason_code"],
            "dstack_native_binding_mismatch",
        )

    def test_busy_model_retains_identity_and_disables_admission(self):
        self.start()
        self.client.running()
        self.native.return_value = HostReadiness("a" * 64, INTENT, "c" * 32, False)
        self.assertEqual(self.capacity.observe(INTENT)["state"], "busy")

    def managed_reason(self):
        binding = self.store.load(INTENT)
        intent = {
            "state": "ready",
            "hard_deadline": request().hard_deadline,
            "provider_instance_id": binding.get("provider_instance_id"),
        }
        node = {
            "desired_state": "running",
            "runtime_state": binding.get("state"),
            "payload": {
                "capacity_backend": "dstack-v1",
                "observation_fresh_seconds": 30,
                "selection": {"runtime_profile_id": request().profile_id},
                "dstack": binding,
                "dstack_observation": {
                    "run_id": binding.get("run_id"),
                    "instance_id": binding.get("provider_instance_id"),
                    "status": binding.get("dstack_status"),
                    "observed_at": binding.get("observed_at"),
                },
            },
        }
        return managed_window_reason(
            intent, node, self.now, deployment_profile_id=request().profile_id
        )

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
            self.assertEqual(
                self.managed_reason(), "managed_dstack_readiness_unconfirmed"
            )
            return HostReadiness("a" * 64, INTENT, "c" * 32, True)

        self.native.side_effect = native
        self.assertTrue(self.capacity.observe(INTENT)["ready"])
        self.assertIsNone(self.managed_reason())

    def test_warm_check_preserves_proof_but_does_not_extend_stale_freshness(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        prior = self.store.load(INTENT)
        self.now = 25.0
        entered, release = threading.Event(), threading.Event()
        results = []

        def native(binding, run):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test callback not released")
            return HostReadiness("a" * 64, INTENT, "c" * 32, True)

        self.native.side_effect = native
        thread = threading.Thread(
            target=lambda: results.append(self.capacity.observe(INTENT))
        )
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.assertEqual(self.store.load(INTENT), prior)
            self.assertIsNone(self.managed_reason())
            self.now = 51.0
            self.assertEqual(
                self.managed_reason(), "managed_dstack_readiness_unconfirmed"
            )
            self.assertEqual(self.store.load(INTENT)["observed_at"], 20.0)
            self.now = 60.0
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]["ready"])
        self.assertEqual(
            results[0]["observed_at"],
            25.0,
            "Native completion cannot renew an older provider read",
        )
        self.assertEqual(self.managed_reason(), "managed_dstack_readiness_unconfirmed")
        self.native.side_effect = None
        self.now = 61.0
        self.capacity.observe(INTENT)
        self.assertIsNone(self.managed_reason())
        self.assertEqual(
            self.store.load(INTENT)["runtime_incarnation"], prior["runtime_incarnation"]
        )
        self.assertEqual(self.client.apply_count, 1)

    def test_pending_warm_busy_check_preserves_busy_then_actual_failure_revokes(self):
        self.start()
        self.client.running()
        self.native.return_value = HostReadiness("a" * 64, INTENT, "c" * 32, False)
        self.capacity.observe(INTENT)
        prior = self.store.load(INTENT)
        self.now = 25.0

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
        self.assertEqual(
            self.store.load(INTENT)["runtime_incarnation"], prior["runtime_incarnation"]
        )

    def test_changed_warm_provider_is_revoked_before_native_callback(self):
        self.start()
        self.client.running()
        self.capacity.observe(INTENT)
        self.native.reset_mock()
        self.client.run["latest_job_submission"]["job_provisioning_data"][
            "instance_id"
        ] = "changed-instance"
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
        for error in (
            RuntimeError("secret prompt and token"),
            DstackError("secret prompt and token"),
            DstackError("dstack_runtime_preparing", "secret prompt and token"),
        ):
            with self.subTest(
                error_type=type(error).__name__, argument_count=len(error.args)
            ):
                self.native.side_effect = error
                value = self.capacity.observe(INTENT)
                self.assertEqual(
                    value["reason_code"], "dstack_native_observation_failed"
                )
                self.assertEqual(value["state"], "runtime_unconfirmed")
                self.assertFalse(value["ready"])
                self.assertNotIn("secret", json.dumps(value))

    def test_native_factory_port_and_source_failures_keep_specific_diagnostics(self):
        self.start()
        self.client.running()
        for code in (
            "dstack_runtime_ports_exhausted",
            "dstack_runtime_port_conflict",
            "dstack_runtime_port_changed",
            "dstack_runtime_retained_port_invalid",
            "dstack_runtime_stopped_port_unallocated",
            "dstack_runtime_source_selection_missing",
            "dstack_runtime_source_binding_invalid",
            "dstack_runtime_source_set_invalid",
            "dstack_runtime_configuration_missing",
        ):
            with self.subTest(code=code):
                self.native.side_effect = DstackError(code)
                value = self.capacity.observe(INTENT)
                self.assertEqual(value["reason_code"], code)
                self.assertEqual(value["state"], "runtime_unconfirmed")
                self.assertFalse(value["ready"])
                self.assertEqual(value["provider_instance_id"], "1234567")
                self.assertEqual(value["billing_state"], "unsettled")
        self.assertEqual((self.client.apply_count, self.client.stop_count), (1, 0))

    def test_native_progress_is_allowlisted_and_never_admits_capacity(self):
        self.start()
        self.client.running()
        for code, phase in (
            ("downloading_weights", "model_download"),
            ("starting_runtime", "runtime_start"),
            ("loading_model", "model_load"),
        ):
            self.native.side_effect = DstackError("dstack_runtime_" + code)
            value = self.capacity.observe(INTENT)
            self.assertEqual(value["bootstrap_phase"], phase)
            self.assertFalse(value["ready"])
            self.assertEqual(value["model_load_state"], "not_observed")
        self.assertEqual(self.client.apply_count, 1)

    def test_changed_resource_configuration_is_rejected(self):
        self.start()
        self.client.run["run_spec"]["configuration"]["resources"]["gpu"]["count"] = 2
        self.assertEqual(
            self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch"
        )

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
        self.now = 49.0
        self.assertEqual(
            self.capacity.stop(INTENT)["reason_code"], "dstack_stop_retry_waiting"
        )
        self.assertEqual(self.client.stop_count, 1)
        self.now = 50.0
        # A new transport/controller must honor the same original retry journal.
        restarted = DstackCapacity(
            self.client, self.store, clock=self.store.clock, readiness=self.native
        )
        self.client.stop_not_delivered = False
        self.assertEqual(restarted.stop(INTENT)["state"], "stopping")
        self.assertEqual(self.client.stop_count, 2)
        self.assertEqual(self.client.apply_count, 1)
        self.assertEqual(self.store.value["run_id"], RUN)
        self.assertEqual(self.store.value["stop_attempt_count"], 2)
        self.assertEqual(self.store.value["stop_next_retry_at"], 110.0)
        self.assertEqual(self.store.events.count("stop_committed"), 1)
        self.assertEqual(self.store.events.count("stop_retry_committed"), 1)
        self.assertEqual(
            self.native.call_count,
            1,
            "A stopped binding must not bootstrap native runtime",
        )

    def test_stop_retry_unknown_absent_changed_or_nonrunning_is_read_only(self):
        self.interrupted_stop()
        self.now = 100.0
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
                    self.client.run["latest_job_submission"]["job_provisioning_data"][
                        "instance_id"
                    ] = "changed-instance"
                else:
                    self.client.run["latest_job_submission"]["job_provisioning_data"][
                        "instance_type"
                    ]["resources"]["gpus"] *= 2
                self.assertEqual(
                    self.capacity.stop(INTENT)["state"], "observation_unknown"
                )
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
        for now, count, due in (
            (50.0, 2, 110.0),
            (110.0, 3, 230.0),
            (230.0, 4, 470.0),
            (470.0, 5, 770.0),
        ):
            self.now = now
            self.assertEqual(self.capacity.stop(INTENT)["state"], "removal_unknown")
            self.assertEqual(self.store.value["stop_attempt_count"], count)
            self.assertEqual(self.store.value["stop_next_retry_at"], due)
        self.now = 10000.0
        value = self.capacity.stop(INTENT)
        self.assertEqual(value["reason_code"], "dstack_stop_retry_exhausted")
        self.assertFalse(value["ready"])
        self.assertEqual(value["billing_state"], "unsettled")
        self.assertEqual(self.client.stop_count, 5)
        self.assertEqual(self.client.apply_count, 1)

    def test_stop_retry_missing_journal_and_new_obligations_fail_closed(self):
        self.interrupted_stop()
        self.now = 50.0
        self.store.obligations = True
        self.assertEqual(
            self.capacity.stop(INTENT)["reason_code"],
            "dstack_business_obligations_pending",
        )
        self.store.obligations = False
        del self.store.value["stop_next_retry_at"]
        self.assertEqual(
            self.capacity.stop(INTENT)["reason_code"],
            "dstack_stop_retry_journal_unconfirmed",
        )
        self.assertEqual(self.client.stop_count, 1)

    def test_concurrent_stop_recovery_consumes_one_original_retry_per_window(self):
        self.interrupted_stop()
        self.now = 50.0
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
                DstackCapacity(self.client, self.store, clock=self.store.clock).stop(
                    INTENT
                )
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
        self.assertIsNone(
            config["regions"], "An empty region whitelist excludes every offer"
        )
        self.assertEqual(
            request(regions=("us-ca",)).run_spec(PUB)["configuration"]["regions"],
            ["us-ca"],
        )

    def test_parsed_cpu_floor_preserves_ownership_and_rejects_changed_requirement(self):
        self.start()
        cpu = self.client.run["run_spec"]["configuration"]["resources"]["cpu"]
        cpu["count"] = {"min": 4, "max": None}
        self.assertEqual(self.capacity.observe(INTENT)["state"], "starting")
        for changed in ({"min": 3, "max": None}, {"min": 4, "max": 4}):
            with self.subTest(count=changed):
                cpu["count"] = changed
                self.assertEqual(
                    self.capacity.observe(INTENT)["reason_code"],
                    "dstack_run_binding_mismatch",
                )

    def test_server_added_region_restriction_cannot_be_adopted(self):
        self.start()
        self.client.run["run_spec"]["configuration"]["regions"] = []
        self.assertEqual(
            self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch"
        )

    def test_pinned_homogeneous_single_node_default_preserves_binding(self):
        self.start()
        configuration = self.client.run["run_spec"]["configuration"]
        configuration["nodes"] = 1
        self.assertEqual(self.capacity.observe(INTENT)["state"], "starting")
        self.assertNotIn("nodes", self.store.value["run_spec"]["configuration"])
        for value in (0, 2, True, False, "1", {"min": 1, "max": 1}):
            with self.subTest(nodes=value):
                configuration["nodes"] = value
                self.assertEqual(
                    self.capacity.observe(INTENT)["reason_code"],
                    "dstack_run_binding_mismatch",
                )
        configuration["nodes"] = 1
        configuration["groups"] = [{"nodes": 1}]
        self.assertEqual(
            self.capacity.observe(INTENT)["reason_code"], "dstack_run_binding_mismatch"
        )

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
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(failures)
        self.assertEqual(self.client.apply_count, 1)

    def test_multi_gpu_allocation_cannot_be_marked_ready(self):
        self.start()
        self.client.running()
        self.client.run["latest_job_submission"]["job_provisioning_data"][
            "instance_type"
        ]["resources"]["gpus"] *= 2
        value = self.capacity.observe(INTENT)
        self.assertFalse(value["ready"])
        self.assertEqual(value["reason_code"], "dstack_allocated_gpu_count_unconfirmed")

    def test_expired_window_rejects_before_network(self):
        with self.assertRaisesRegex(DstackError, "dstack_window_expired"):
            self.capacity.start(request(hard_deadline=19), PUB)
        self.assertEqual(self.client.apply_count, 0)

    def test_continuing_request_has_explicit_off_and_preserves_finite_binding(self):
        finite = request().binding(PUB)
        self.assertEqual(finite["run_spec"]["configuration"]["max_duration"], 3600)
        self.assertNotIn("lifecycle_version", finite)
        continuing = request(
            lifecycle_version=2, hard_deadline=None, max_duration_s=None
        )
        binding = continuing.binding(PUB)
        self.assertEqual(binding["lifecycle_version"], 2)
        self.assertIsNone(binding["hard_deadline"])
        self.assertEqual(binding["run_spec"]["configuration"]["max_duration"], "off")
        self.capacity.start(continuing, PUB)
        self.now = 100000.0
        self.capacity.start(continuing, PUB)
        self.assertEqual(self.client.apply_count, 1)
        self.now = 20.0
        with self.assertRaisesRegex(DstackError, "dstack_intent_binding_changed"):
            self.capacity.start(request(), PUB)

    def test_missing_deadline_requires_explicit_continuing_version(self):
        for fields in (
            {"hard_deadline": None},
            {"max_duration_s": None},
            {"hard_deadline": None, "max_duration_s": None},
            {"lifecycle_version": 2},
            {"lifecycle_version": True},
            {"lifecycle_version": 2, "hard_deadline": None},
            {"lifecycle_version": 2, "max_duration_s": None},
        ):
            with self.subTest(fields=fields), self.assertRaises(DstackError):
                request(**fields)

    def test_pinned_model_parsed_response_is_compatible(self):
        # Optional SDK check, also run explicitly with the pinned 0.22.5 image.
        try:
            from dstack._internal.core.models.configurations import TaskConfiguration
        except ImportError:
            self.skipTest("optional pinned dstack SDK absent")
        self.start()
        config = self.client.run["run_spec"]["configuration"]
        self.client.run["run_spec"]["configuration"] = TaskConfiguration.model_validate(
            config
        ).model_dump(mode="json")
        self.assertEqual(self.capacity.observe(INTENT)["state"], "starting")

    def test_pinned_model_preserves_explicit_continuing_off(self):
        try:
            from dstack._internal.core.models.configurations import TaskConfiguration
        except ImportError:
            self.skipTest("optional pinned dstack SDK absent")
        continuing = request(
            lifecycle_version=2, hard_deadline=None, max_duration_s=None
        )
        self.capacity.start(continuing, PUB)
        config = self.client.run["run_spec"]["configuration"]
        self.client.run["run_spec"]["configuration"] = TaskConfiguration.model_validate(
            config
        ).model_dump(mode="json")
        self.assertEqual(
            self.client.run["run_spec"]["configuration"]["max_duration"], "off"
        )
        self.assertEqual(self.capacity.observe(INTENT)["state"], "starting")

    def test_idle_policy_distinguishes_holds_and_unknown_obligations(self):
        policy = dict(
            now=1000,
            hard_deadline=2000,
            hold_until=0,
            last_business_activity=10,
            idle_seconds=600,
            active_jobs=0,
            unsafe_attempts=0,
            collection_holds=0,
            observations_fresh=True,
        )
        self.assertEqual(idle_action(**policy), "stop")
        self.assertEqual(idle_action(**{**policy, "hold_until": 1500}), "retain")
        self.assertEqual(idle_action(**{**policy, "collection_holds": 1}), "retain")
        self.assertEqual(
            idle_action(**{**policy, "now": 2100, "unsafe_attempts": 1}), "drain"
        )
        self.assertEqual(
            idle_action(**{**policy, "observations_fresh": False}), "reconcile"
        )

    def test_continuing_idle_and_keep_on_use_funding_not_a_fixed_deadline(self):
        policy = dict(
            now=1000,
            hard_deadline=None,
            hold_until=0,
            last_business_activity=10,
            idle_seconds=600,
            active_jobs=0,
            unsafe_attempts=0,
            collection_holds=0,
            observations_fresh=True,
            lifecycle_version=2,
        )
        self.assertEqual(idle_action(**policy), "stop")
        self.assertEqual(idle_action(**policy, keep_on="until_stopped"), "retain")
        self.assertEqual(
            idle_action(**policy, keep_on="until_stopped", budget_available=False),
            "stop",
        )
        for obligation in ("active_jobs", "unsafe_attempts", "collection_holds"):
            self.assertEqual(idle_action(**{**policy, obligation: 1}), "retain")
            self.assertEqual(
                idle_action(**{**policy, obligation: 1}, budget_available=False),
                "drain",
            )
        self.assertEqual(
            idle_action(
                **{**policy, "observations_fresh": False}, keep_on="until_stopped"
            ),
            "reconcile",
        )

    def test_continuing_hold_expiry_begins_a_full_idle_interval(self):
        policy = dict(
            now=1000,
            hard_deadline=None,
            hold_until=1500,
            last_business_activity=10,
            idle_seconds=600,
            active_jobs=0,
            unsafe_attempts=0,
            collection_holds=0,
            observations_fresh=True,
            lifecycle_version=2,
            keep_on="until",
        )
        for now, expected in (
            (1000, "retain"),
            (1500, "retain"),
            (2099, "retain"),
            (2100, "stop"),
        ):
            self.assertEqual(idle_action(**{**policy, "now": now}), expected)
        with self.assertRaisesRegex(DstackError, "dstack_idle_policy_invalid"):
            idle_action(**{**policy, "lifecycle_version": 1})

    @unittest.skipUnless(
        os.name == "posix", "GPU bootstrap validates Linux absolute paths"
    )
    def test_native_bootstrap_keeps_original_one_slot_interface(self):
        path = Path(__file__).resolve().parents[1] / "deploy" / "wangp" / "bootstrap.py"
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
            native_bootstrap_config(
                request(), source_bundle_sha256="d" * 64, model_root="/root/../private"
            )


class DstackLiveUsageTests(unittest.TestCase):
    def setUp(self):
        self.now = 20.0
        self.binding = {
            **request().binding(PUB),
            "apply_started": True,
            "run_id": RUN,
            "provider_instance_id": "1234567",
        }
        self.node = {
            "intent_id": INTENT,
            "desired_state": "stopped",
            "runtime_state": "stopped",
            "payload": {
                "capacity_backend": "dstack-v1",
                "hourly_cost_microusd": 850000,
                "dstack": self.binding,
                "historical_bill": {"actual_cost_microusd": None},
            },
        }
        self.run = {
            "id": RUN,
            "project_name": "sixnine",
            "run_spec": request().run_spec(PUB),
            "status": "terminated",
            "latest_job_submission": {
                "job_provisioning_data": {
                    "backend": "vastai",
                    "instance_id": "1234567",
                    "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}},
                }
            },
        }
        self.client = Mock(
            project="sixnine", get=Mock(side_effect=lambda **_: copy.deepcopy(self.run))
        )
        self.inventory = Mock(
            return_value={
                "backend": "vastai",
                "complete": True,
                "observed_at": 20.0,
                "instance_ids": [],
            }
        )

    def usage(self, nodes=None):
        return live_capacity_usage(
            self.client,
            [self.node] if nodes is None else nodes,
            provider_inventory=self.inventory,
            clock=lambda: self.now,
        )

    def test_nodes_from_another_control_plane_settle_only_by_provider_absence(self):
        # A Sky client reads history that ran on the local server (no binding field).
        self.client.endpoint = "https://sky.dstack.ai"
        self.client.get = Mock(
            side_effect=AssertionError("foreign run must not be read")
        )
        result = self.usage()
        self.assertEqual(
            result["observations"][0]["reason_code"], "dstack_foreign_provider_absent"
        )
        self.assertEqual(result["instances"], 0)
        self.inventory.return_value = {
            **self.inventory.return_value,
            "instance_ids": [self.node["payload"]["dstack"]["provider_instance_id"]],
        }
        result = self.usage()
        self.assertEqual(
            result["observations"][0]["reason_code"], "dstack_foreign_unconfirmed"
        )
        self.assertTrue(result["observations"][0]["counted"])
        # Its own control plane reads the run as before.
        self.node["payload"]["dstack"]["control_plane"] = "https://sky.dstack.ai"
        self.client.get = Mock(side_effect=lambda **_: copy.deepcopy(self.run))
        self.inventory.return_value = {
            **self.inventory.return_value,
            "instance_ids": [],
        }
        self.assertEqual(
            self.usage()["observations"][0]["reason_code"],
            "dstack_terminal_provider_absent",
        )

    def test_owned_terminal_and_fresh_provider_absence_free_capacity_not_billing(self):
        original = copy.deepcopy(self.node)
        legacy = {
            "intent_id": "old-lium",
            "payload": {"capacity_backend": "legacy", "provider": "lium"},
        }
        result = self.usage([self.node, legacy])
        self.assertEqual(result["instances"], 0)
        self.assertEqual(result["physical_gpus"], 0)
        self.assertEqual(result["hourly_cost_microusd"], 0)
        self.assertEqual(result["active_instance_ids"], ())
        self.assertEqual(
            result["observations"][0]["reason_code"], "dstack_terminal_provider_absent"
        )
        self.assertEqual(self.node, original)
        self.client.get.assert_called_once_with(run_id=RUN)
        self.inventory.assert_called_once_with("vastai")

    def no_allocation(self):
        self.binding.pop("provider_instance_id")
        self.run = no_capacity_run(self.run)

    def unknown_no_allocation(self):
        self.no_allocation()
        self.binding["billing_state"] = "unsettled"
        message = "sixnine_vast_attempts_v1:" + json.dumps(
            {
                "version": 1,
                "outcome": "unknown",
                "attempted_count": 1,
                "attempts": [
                    {
                        "ask_id": "46211052",
                        "http_status": 400,
                        "code": "mutation_unknown",
                        "outcome": "unknown",
                        "contract_id": None,
                    }
                ],
            }
        )
        self.run["latest_job_submission"]["termination_reason_message"] = message
        self.run["jobs"][0]["job_submissions"][0]["termination_reason_message"] = (
            message
        )

    def test_failed_unknown_put_and_complete_empty_account_free_only_current_capacity(
        self,
    ):
        self.unknown_no_allocation()
        original = copy.deepcopy((self.node, self.run))
        self.assertFalse(terminal_no_allocation(self.run, self.binding))
        result = self.usage()
        self.assertEqual(
            (
                result["instances"],
                result["physical_gpus"],
                result["logical_slots"],
                result["hourly_cost_microusd"],
                result["active_instance_ids"],
            ),
            (0, 0, 0, 0, ()),
        )
        self.assertEqual(
            result["observations"][0]["reason_code"],
            "dstack_terminal_provider_accounted",
        )
        self.assertFalse(terminal_no_allocation(self.run, self.binding))
        self.assertEqual((self.node, self.run), original)
        self.assertEqual(self.binding["billing_state"], "unsettled")
        self.assertIsNone(
            self.node["payload"]["historical_bill"]["actual_cost_microusd"]
        )
        self.inventory.assert_called_once_with("vastai")

    def test_failed_unknown_put_needs_current_complete_empty_same_account(self):
        self.unknown_no_allocation()
        original = copy.deepcopy((self.node, self.run))
        good = self.inventory.return_value
        for bad in (
            None,
            {**good, "backend": "runpod"},
            {**good, "complete": False},
            {**good, "observed_at": -1},
            {**good, "observed_at": True},
            {**good, "observed_at": 20.1},
            {**good, "instance_ids": "1234567"},
            {**good, "instance_ids": ["1234567", "1234567"]},
            {**good, "instance_ids": ["1234567"]},
        ):
            with self.subTest(bad=bad):
                self.inventory.return_value = bad
                self.assertEqual(self.usage()["active_instance_ids"], (INTENT,))
                self.assertEqual((self.node, self.run), original)
        self.inventory.return_value = {**good, "observed_at": 1.0}
        self.now = 32.0
        self.assertEqual(self.usage()["instances"], 1)
        self.inventory.side_effect = ValueError("private supplier failure")
        result = self.usage()
        self.assertEqual(result["instances"], 1)
        self.assertNotIn("private supplier failure", json.dumps(result))

    def test_empty_account_never_frees_unknown_active_or_ambiguous_native_run(self):
        self.unknown_no_allocation()
        original = copy.deepcopy(self.run)
        cases = [
            {**original, "status": state}
            for state in ("running", "provisioning", "terminating")
        ]
        cases += [{**original, "project_name": "another"}, {**original, "jobs": []}]
        missing = copy.deepcopy(original)
        missing["latest_job_submission"].pop("job_runtime_data")
        missing["jobs"][0]["job_submissions"][0].pop("job_runtime_data")
        cases.append(missing)
        for value in cases:
            with self.subTest(status=value.get("status")):
                self.run = value
                self.inventory.reset_mock()
                self.assertEqual(self.usage()["instances"], 1)
                self.inventory.assert_not_called()

    def test_empty_account_does_not_erase_prior_runtime_or_host_evidence(self):
        self.unknown_no_allocation()
        for key, value in (
            ("runtime_incarnation", "a" * 32),
            ("bootstrap_started", True),
            ("host_observation", {}),
            ("host_failure", {}),
        ):
            with self.subTest(key=key):
                self.binding[key] = value
                self.inventory.reset_mock()
                self.assertEqual(self.usage()["instances"], 1)
                self.inventory.assert_not_called()
                self.binding.pop(key)

    def test_later_read_expiry_restores_unknown_failed_capacity_without_another_account_read(
        self,
    ):
        self.unknown_no_allocation()
        other_id = "a3333333-3333-4333-8333-333333333333"
        other_run_id = "b3333333-3333-4333-8333-333333333333"
        other = copy.deepcopy(self.node)
        other["intent_id"] = other_id
        other["payload"]["dstack"] = {
            **request(intent_id=other_id).binding(PUB),
            "apply_started": True,
            "run_id": other_run_id,
        }
        other_run = {
            **copy.deepcopy(self.run),
            "id": other_run_id,
            "status": "running",
            "run_spec": request(intent_id=other_id).run_spec(PUB),
        }

        def read_run(**identity):
            if identity["run_id"] == RUN:
                return copy.deepcopy(self.run)
            self.now = 51.0
            return copy.deepcopy(other_run)

        self.client.get.side_effect = read_run
        original = copy.deepcopy([self.node, other])
        result = self.usage([self.node, other])
        self.assertEqual(set(result["active_instance_ids"]), {INTENT, other_id})
        self.assertEqual(result["instances"], 2)
        self.assertEqual(
            result["observations"][0]["reason_code"],
            "dstack_terminal_provider_unconfirmed",
        )
        self.assertEqual([self.node, other], original)
        self.inventory.assert_called_once_with("vastai")

    def test_unknown_failed_slot_stays_excluded_when_another_owned_allocation_appears(
        self,
    ):
        self.unknown_no_allocation()
        other_id = "a3333333-3333-4333-8333-333333333333"
        other_run_id = "b3333333-3333-4333-8333-333333333333"
        other = copy.deepcopy(self.node)
        other["intent_id"] = other_id
        other["payload"]["dstack"] = {
            **request(intent_id=other_id).binding(PUB),
            "apply_started": True,
            "run_id": other_run_id,
            "provider_instance_id": "56042378",
        }
        other_run = {
            "id": other_run_id,
            "project_name": "sixnine",
            "status": "running",
            "run_spec": request(intent_id=other_id).run_spec(PUB),
            "latest_job_submission": {
                "job_provisioning_data": {
                    "backend": "vastai",
                    "instance_id": "56042378",
                }
            },
        }
        self.client.get.side_effect = lambda **identity: copy.deepcopy(
            self.run if identity["run_id"] == RUN else other_run
        )
        self.inventory.return_value["instance_ids"] = ["56042378"]
        original = copy.deepcopy([self.node, other])
        for nodes in ([self.node, other], [other, self.node]):
            with self.subTest(order=[node["intent_id"] for node in nodes]):
                self.inventory.reset_mock()
                result = self.usage(nodes)
                self.assertEqual(result["instances"], 1)
                self.assertEqual(result["hourly_cost_microusd"], 850000)
                self.assertEqual(result["active_instance_ids"], (other_id,))
                observed = next(
                    row for row in result["observations"] if row["intent_id"] == INTENT
                )
                self.assertEqual(
                    observed["reason_code"], "dstack_terminal_provider_accounted"
                )
                self.assertEqual([self.node, other], original)
                self.inventory.assert_called_once_with("vastai")
        # An additional unidentified provider allocation could be the old PUT.
        self.inventory.return_value["instance_ids"].append("56042379")
        self.assertIn(INTENT, self.usage([self.node, other])["active_instance_ids"])
        # A conflicting or foreign other run never accounts for that provider ID.
        self.inventory.return_value["instance_ids"] = ["56042378"]
        other["payload"]["dstack"]["provider_instance_id"] = "conflicting-id"
        self.assertIn(INTENT, self.usage([self.node, other])["active_instance_ids"])
        other["payload"]["dstack"]["provider_instance_id"] = "56042378"
        other_run["project_name"] = "foreign"
        self.assertIn(INTENT, self.usage([self.node, other])["active_instance_ids"])
        other_run["project_name"] = "sixnine"
        original_spec = copy.deepcopy(other_run["run_spec"])
        other_run["run_spec"]["configuration"]["max_price"] = 0.84
        self.assertIn(INTENT, self.usage([self.node, other])["active_instance_ids"])
        other_run["run_spec"] = original_spec
        original_provisioning = copy.deepcopy(
            other_run["latest_job_submission"]["job_provisioning_data"]
        )
        other_run["latest_job_submission"]["job_provisioning_data"]["instance_type"] = (
            None
        )
        self.assertIn(INTENT, self.usage([self.node, other])["active_instance_ids"])
        other_run["latest_job_submission"]["job_provisioning_data"] = (
            original_provisioning
        )
        original_read = self.client.get.side_effect

        def failed_witness(**identity):
            if identity["run_id"] == RUN:
                return copy.deepcopy(self.run)
            raise DstackError("dstack_api_unavailable")

        self.client.get.side_effect = failed_witness
        self.assertIn(INTENT, self.usage([self.node, other])["active_instance_ids"])
        self.client.get.side_effect = original_read
        duplicate_id = "a4444444-4444-4444-8444-444444444444"
        duplicate_run_id = "b4444444-4444-4444-8444-444444444444"
        duplicate = copy.deepcopy(other)
        duplicate["intent_id"] = duplicate_id
        duplicate["payload"]["dstack"] = {
            **request(intent_id=duplicate_id).binding(PUB),
            "apply_started": True,
            "run_id": duplicate_run_id,
            "provider_instance_id": "56042378",
        }
        duplicate_run = {
            **copy.deepcopy(other_run),
            "id": duplicate_run_id,
            "run_spec": request(intent_id=duplicate_id).run_spec(PUB),
        }
        observed_runs = {
            RUN: self.run,
            other_run_id: other_run,
            duplicate_run_id: duplicate_run,
        }
        self.client.get.side_effect = lambda **identity: copy.deepcopy(
            observed_runs[identity["run_id"]]
        )
        self.assertIn(
            INTENT, self.usage([self.node, other, duplicate])["active_instance_ids"]
        )

    def test_complete_no_capacity_submission_releases_capacity_only(self):
        self.no_allocation()
        self.node["desired_state"] = "running"
        original = copy.deepcopy((self.node, self.run))
        value = self.usage()
        self.assertEqual(
            (
                value["instances"],
                value["physical_gpus"],
                value["logical_slots"],
                value["hourly_cost_microusd"],
                value["active_instance_ids"],
            ),
            (0, 0, 0, 0, ()),
        )
        self.assertEqual(
            value["observations"][0]["reason_code"], "dstack_terminal_no_allocation"
        )
        self.assertEqual((self.node, self.run), original)
        self.assertNotIn("PRIVATE-PROVIDER-MESSAGE", json.dumps(value))
        self.inventory.assert_not_called()

    def test_incomplete_ambiguous_or_other_terminal_failure_retains_capacity(self):
        self.no_allocation()
        original = copy.deepcopy(self.run)
        cases = []
        for field in ("jobs", "latest_job_submission", "termination_reason"):
            value = copy.deepcopy(original)
            value.pop(field)
            cases.append(value)
        for jobs in (
            None,
            [],
            [{}],
            [{"job_submissions": []}],
            [{"job_submissions": "PRIVATE-PROVIDER-MESSAGE"}],
        ):
            cases.append({**original, "jobs": jobs})
        for field in ("job_provisioning_data", "job_runtime_data"):
            value = copy.deepcopy(original)
            value["latest_job_submission"].pop(field)
            value["jobs"][0]["job_submissions"][0].pop(field)
            cases.append(value)
            for malformed in ({}, "PRIVATE-PROVIDER-MESSAGE"):
                value = copy.deepcopy(original)
                value["latest_job_submission"][field] = malformed
                value["jobs"][0]["job_submissions"][0][field] = malformed
                cases.append(value)
        value = copy.deepcopy(original)
        value["latest_job_submission"]["termination_reason"] = "job_failed"
        cases.append(value)
        for identity in (None, "PRIVATE-PROVIDER-MESSAGE", ""):
            value = copy.deepcopy(original)
            value["latest_job_submission"]["id"] = identity
            value["jobs"][0]["job_submissions"][0]["id"] = identity
            cases.append(value)
        for status in ("provisioning", "running", "terminated", "unexpected"):
            cases.append({**original, "status": status})
        cases.append({**original, "project_name": "foreign"})
        for value in cases:
            with self.subTest(fields=list(value)):
                self.run = value
                result = self.usage()
                self.assertEqual(result["instances"], 1)
                self.assertEqual(result["active_instance_ids"], (INTENT,))
                self.assertNotIn("PRIVATE-PROVIDER-MESSAGE", json.dumps(result))
        self.inventory.assert_not_called()

    def test_prior_allocation_or_native_receipts_prevent_no_allocation_exemption(self):
        self.no_allocation()
        original_binding = copy.deepcopy(self.binding)
        for key, value in (
            ("provider_instance_id", "previous-instance"),
            ("runtime_incarnation", "c" * 32),
            ("first_runtime_ready_at", 10.0),
            ("bootstrap_started", True),
            ("bootstrap_launch_started", True),
            ("runtime_launch_started", True),
        ):
            with self.subTest(key=key):
                self.binding[key] = value
                self.assertEqual(self.usage()["instances"], 1)
                self.binding.clear()
                self.binding.update(original_binding)
        previous = {
            "status": "failed",
            "job_provisioning_data": {
                "backend": "vastai",
                "instance_id": "previous-instance",
            },
            "job_runtime_data": None,
        }
        self.run["jobs"][0]["job_submissions"].insert(0, previous)
        self.assertEqual(self.usage()["instances"], 1)
        self.inventory.assert_not_called()

    def test_allocated_failure_still_needs_existing_fresh_provider_absence(self):
        provisioning = copy.deepcopy(
            self.run["latest_job_submission"]["job_provisioning_data"]
        )
        self.run = no_capacity_run(self.run)
        self.run["latest_job_submission"]["job_provisioning_data"] = provisioning
        self.run["jobs"][0]["job_submissions"][0]["job_provisioning_data"] = (
            provisioning
        )
        self.inventory.return_value = None
        self.assertEqual(self.usage()["instances"], 1)
        self.inventory.return_value = {
            "backend": "vastai",
            "complete": True,
            "observed_at": self.now,
            "instance_ids": [],
        }
        self.assertEqual(
            self.usage()["observations"][0]["reason_code"],
            "dstack_terminal_provider_absent",
        )

    def test_pending_new_intent_counts_before_any_apply_and_legacy_never_counts(self):
        pending_id = "a3333333-3333-4333-8333-333333333333"
        pending = {
            "intent_id": pending_id,
            "desired_state": "running",
            "payload": {
                "capacity_backend": "dstack-v1",
                "hourly_cost_microusd": 850000,
                "dstack": request(intent_id=pending_id).binding(PUB),
            },
        }
        result = self.usage(
            [
                self.node,
                pending,
                {"payload": {"provider": "targon", "hourly_cost_microusd": 50000000}},
            ]
        )
        self.assertEqual(result["active_instance_ids"], (pending_id,))
        self.assertEqual(result["instances"], 1)
        self.assertEqual(result["hourly_cost_microusd"], 850000)
        self.client.get.assert_called_once_with(run_id=RUN)
        self.assertNotIn("apply_started", pending["payload"]["dstack"])

    def test_exact_terminal_requires_complete_same_backend_fresh_provider_absence(self):
        good = self.inventory.return_value
        for bad in (
            None,
            {**good, "backend": "runpod"},
            {**good, "complete": False},
            {**good, "observed_at": -1},
            {**good, "observed_at": 20.1},
            {**good, "observed_at": True},
            {**good, "instance_ids": "1234567"},
            {**good, "instance_ids": ["1234567", "1234567"]},
            {**good, "instance_ids": ["1234567"]},
        ):
            with self.subTest(bad=bad):
                self.inventory.return_value = bad
                self.assertEqual(self.usage()["instances"], 1)
        self.inventory.return_value = {**good, "observed_at": 1.0}
        self.now = 32.0
        self.assertEqual(self.usage()["instances"], 1)
        self.inventory.side_effect = ValueError("secret supplier token")
        self.assertEqual(
            self.usage()["observations"][0]["reason_code"],
            "dstack_terminal_provider_unconfirmed",
        )

    def test_unknown_or_unowned_run_never_frees_reserved_capacity(self):
        original = copy.deepcopy(self.run)
        for bad in (
            None,
            {**original, "project_name": "another"},
            {**original, "id": "b3333333-3333-4333-8333-333333333333"},
            {**original, "status": "unexpected secret supplier token"},
            {**original, "latest_job_submission": None},
        ):
            with self.subTest(bad=bad):
                self.run = bad
                self.inventory.reset_mock()
                result = self.usage()
                self.assertEqual(result["instances"], 1)
                self.assertEqual(result["physical_gpus"], 1)
                self.assertEqual(result["active_instance_ids"], (INTENT,))
                self.assertNotIn("secret supplier token", json.dumps(result))
                self.inventory.assert_not_called()
        self.client.get.side_effect = DstackError("dstack_api_unavailable")
        self.assertEqual(
            self.usage()["observations"][0]["reason_code"],
            "dstack_capacity_unconfirmed",
        )

    def test_later_slow_run_read_restores_capacity_when_original_absence_expires(self):
        other_id = "a3333333-3333-4333-8333-333333333333"
        other_run_id = "b3333333-3333-4333-8333-333333333333"
        other = copy.deepcopy(self.node)
        other["intent_id"] = other_id
        other["payload"]["dstack"] = {
            **request(intent_id=other_id).binding(PUB),
            "apply_started": True,
            "run_id": other_run_id,
            "provider_instance_id": "7654321",
        }
        other_run = {
            **copy.deepcopy(self.run),
            "id": other_run_id,
            "status": "running",
            "run_spec": request(intent_id=other_id).run_spec(PUB),
        }
        other_run["latest_job_submission"]["job_provisioning_data"]["instance_id"] = (
            "7654321"
        )
        original_nodes = copy.deepcopy([self.node, other])

        def read_run(**kw):
            if kw["run_id"] == RUN:
                return copy.deepcopy(self.run)
            self.now = 51.0  # First account receipt was fresh before this read.
            return copy.deepcopy(other_run)

        self.client.get.side_effect = read_run
        result = self.usage([self.node, other])
        self.assertEqual(result["instances"], 2)
        self.assertEqual(result["physical_gpus"], 2)
        self.assertEqual(result["hourly_cost_microusd"], 1700000)
        self.assertEqual(set(result["active_instance_ids"]), {INTENT, other_id})
        self.assertEqual(
            result["observations"][0]["reason_code"],
            "dstack_terminal_provider_unconfirmed",
        )
        self.assertEqual([self.node, other], original_nodes)
        self.inventory.assert_called_once_with("vastai")
        self.assertEqual(self.client.get.call_count, 2)

    def test_active_run_counts_even_if_provider_snapshot_says_absent(self):
        for state in ("running", "provisioning", "terminating"):
            self.run["status"] = state
            self.assertEqual(self.usage()["instances"], 1)
        self.inventory.assert_not_called()

    def test_physical_identity_deduplicates_and_counts_actual_gpu_allocation(self):
        other_id = "a3333333-3333-4333-8333-333333333333"
        other_run_id = "b3333333-3333-4333-8333-333333333333"
        other = copy.deepcopy(self.node)
        other["intent_id"] = other_id
        other["payload"]["dstack"] = {
            **request(intent_id=other_id).binding(PUB),
            "apply_started": True,
            "run_id": other_run_id,
            "provider_instance_id": "1234567",
        }
        self.run["status"] = "running"
        self.run["latest_job_submission"]["job_provisioning_data"]["instance_type"][
            "resources"
        ]["gpus"] *= 2
        other_run = {
            **copy.deepcopy(self.run),
            "id": other_run_id,
            "run_spec": request(intent_id=other_id).run_spec(PUB),
        }
        self.client.get.side_effect = lambda **kw: copy.deepcopy(
            self.run if kw["run_id"] == RUN else other_run
        )
        result = self.usage([self.node, other])
        self.assertEqual(result["instances"], 1)
        self.assertEqual(result["physical_gpus"], 2)
        self.assertEqual(result["logical_slots"], 2)
        self.assertEqual(set(result["active_instance_ids"]), {INTENT, other_id})

    def test_only_confirmed_never_applied_cancel_frees_pending_capacity(self):
        binding = request().binding(PUB)
        self.node["payload"]["dstack"] = {
            **binding,
            "state": "stopped",
            "reason_code": "dstack_never_applied",
            "billing_state": "no_charge_confirmed",
        }
        self.assertEqual(self.usage()["instances"], 0)
        self.client.get.assert_not_called()
        self.node["payload"]["dstack"]["billing_state"] = "unsettled"
        self.assertEqual(self.usage()["instances"], 1)

    def test_invalid_current_cost_blocks_admission_without_erasing_history(self):
        self.node["payload"]["hourly_cost_microusd"] = None
        self.run["status"] = "running"
        with self.assertRaisesRegex(DstackError, "dstack_active_cost_unconfirmed"):
            self.usage()
        self.run["status"] = "terminated"
        self.assertEqual(self.usage()["instances"], 0)


class DstackRESTTests(unittest.TestCase):
    def test_inventory_429_retries_closed_original_read_with_remaining_timeout(self):
        seen, responses, sleeps, clock = [], [], [], [10.0]

        def provider(req):
            seen.append(req)
            clock[0] += 0.1
            response = (
                httpx.Response(429, headers={"Retry-After": "2"}, text="PRIVATE")
                if len(seen) == 1
                else httpx.Response(
                    200,
                    json={
                        "success": True,
                        "instances": [],
                        "instances_found": 0,
                        "total_instances": 0,
                        "next_token": None,
                    },
                )
            )
            responses.append(response)
            return response

        def sleep(delay):
            self.assertTrue(all(response.is_closed for response in responses))
            sleeps.append(delay)
            clock[0] += delay

        with (
            patch(
                "studio_platform.dstack_capacity.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch("studio_platform.dstack_capacity.time.sleep", side_effect=sleep),
            patch("studio_platform.dstack_capacity.random.uniform", return_value=0.125),
        ):
            result = self.provider_client("vastai", provider).provider_inventory(
                "vastai", clock=lambda: 1000.0
            )
        self.assertTrue(result["complete"])
        self.assertEqual(result["instance_ids"], [])
        self.assertEqual(sleeps, [2.125])
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].url, seen[1].url)
        self.assertEqual(seen[0].content, seen[1].content)
        self.assertLess(
            seen[1].extensions["timeout"]["read"], seen[0].extensions["timeout"]["read"]
        )

    def test_search_429_http_date_preserves_exact_query_then_maps_original_offer(self):
        seen = []

        def provider(req):
            seen.append(req)
            if len(seen) == 1:
                return httpx.Response(
                    429, headers={"Retry-After": "Thu, 01 Jan 1970 00:00:02 GMT"}
                )
            return httpx.Response(
                200,
                json={
                    "offers": [
                        {
                            "id": 12345,
                            "machine_id": 19493,
                            "verified": "verified",
                            "inet_down": 1000,
                        }
                    ]
                },
            )

        with (
            patch("studio_platform.dstack_capacity.time.time", return_value=0),
            patch("studio_platform.dstack_capacity.time.sleep") as sleep,
            patch("studio_platform.dstack_capacity.random.uniform", return_value=0.125),
        ):
            result = self.provider_client("vastai", provider).vast_offer_mapping(
                request(), clock=lambda: 1000.0
            )
        self.assertEqual(
            result["offers"],
            [
                {
                    "offer_id": "12345",
                    "machine_id": "19493",
                    "country": None,
                    "region": None,
                    "advertised_download_mbps": 1000,
                }
            ],
        )
        sleep.assert_called_once_with(2.125)
        self.assertEqual([req.method for req in seen], ["POST", "POST"])
        self.assertEqual(seen[0].content, seen[1].content)
        self.assertEqual(json.loads(seen[0].content)["verified"], {"eq": True})
        self.assertEqual(json.loads(seen[0].content)["inet_down"], {"gte": 1000})
        self.assertEqual(json.loads(seen[0].content)["cuda_max_good"], {"gte": 13.0})

    def test_vast_bandwidth_floor_refuses_unknown_and_preserves_advertised_location(
        self,
    ):
        seen = []
        rates = [999, None, True, "2000", 1000, 2500]
        rows = [
            {
                "id": i + 1,
                "machine_id": i + 100,
                "inet_down": rate,
                "geolocation": "Ashburn, US" if i == 4 else "Seoul, South Korea",
            }
            for i, rate in enumerate(rates)
        ]

        def provider(req):
            seen.append(json.loads(req.content))
            return httpx.Response(200, json={"offers": rows})

        result = self.provider_client("vastai", provider).vast_offer_mapping(
            request(), clock=lambda: 1000.0
        )
        self.assertEqual([row["offer_id"] for row in result["offers"]], ["5", "6"])
        self.assertEqual(result["offers"][0]["country"], "US")
        self.assertEqual(result["offers"][0]["region"], "us-ashburn")
        self.assertEqual(result["offers"][1]["country"], "South Korea")
        self.assertEqual(result["offers"][1]["region"], "southkorea-seoul")
        self.assertEqual(seen[0]["inet_down"], {"gte": 1000})
        self.assertNotIn("region", seen[0])
        self.assertNotIn("country", seen[0])

    def test_429_exhaustion_or_long_retry_after_never_returns_empty_inventory(self):
        for header, calls, delays, retry_after in (
            (None, 3, [1.125, 2.125], 1),
            ("120", 1, [], 120),
            ("PRIVATE invalid header", 3, [1.125, 2.125], 1),
        ):
            with self.subTest(header=header):
                seen = []

                def provider(req):
                    seen.append(req)
                    return httpx.Response(
                        429,
                        headers={} if header is None else {"Retry-After": header},
                        json={"instances": [], "PRIVATE": "never emitted"},
                    )

                with (
                    patch("studio_platform.dstack_capacity.time.sleep") as sleep,
                    patch(
                        "studio_platform.dstack_capacity.random.uniform",
                        return_value=0.125,
                    ),
                    self.assertRaises(DstackReadRateLimited) as error,
                ):
                    self.provider_client("vastai", provider).provider_inventory(
                        "vastai"
                    )
                self.assertEqual(str(error.exception), "dstack_provider_rate_limited")
                self.assertEqual(error.exception.retry_after, retry_after)
                self.assertEqual(len(seen), calls)
                self.assertEqual(
                    [call.args[0] for call in sleep.call_args_list], delays
                )

    def test_other_read_errors_and_uncertain_apply_are_not_retried(self):
        for outcome in (
            httpx.Response(503, text="PRIVATE"),
            httpx.ReadTimeout("PRIVATE"),
        ):
            with self.subTest(outcome=type(outcome).__name__):
                seen = []

                def provider(req):
                    seen.append(req)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                with (
                    patch("studio_platform.dstack_capacity.time.sleep") as sleep,
                    self.assertRaises(DstackError),
                ):
                    self.provider_client("vastai", provider).provider_inventory(
                        "vastai"
                    )
                self.assertEqual(len(seen), 1)
                sleep.assert_not_called()
        seen = []

        def rejected(req):
            seen.append(req)
            return httpx.Response(429, text="PRIVATE")

        client = DstackClient(
            "http://127.0.0.1:3000",
            "dstack-secret-token-123456",
            "sixnine",
            session=httpx.Client(transport=httpx.MockTransport(rejected)),
        )
        with (
            patch("studio_platform.dstack_capacity.time.sleep") as sleep,
            self.assertRaises(DstackError),
        ):
            client.apply({"run_name": "original"})
        self.assertEqual(len(seen), 1)
        sleep.assert_not_called()

    def provider_client(self, backend, handler, *, config_changes=None):
        key = "provider-secret-token-123456"
        config = {"type": backend, "creds": {"type": "api_key", "api_key": key}}
        config.update(config_changes or {})

        def dstack_read(req):
            self.assertEqual(req.method, "POST")
            self.assertEqual(
                req.url.path, f"/api/project/sixnine/backends/{backend}/config_info"
            )
            self.assertEqual(
                req.headers["Authorization"], "Bearer dstack-secret-token-123456"
            )
            self.assertEqual(json.loads(req.content), {})
            return httpx.Response(200, json=config)

        def provider_read(req):
            self.assertEqual(req.headers["Authorization"], "Bearer " + key)
            self.assertNotIn("dstack-secret", str(req.headers))
            self.assertIsNone(req.url.params.get("api_key"))
            return handler(req)

        return DstackClient(
            "http://127.0.0.1:3000",
            "dstack-secret-token-123456",
            "sixnine",
            session=httpx.Client(transport=httpx.MockTransport(dstack_read)),
            provider_session=httpx.Client(
                transport=httpx.MockTransport(provider_read), follow_redirects=True
            ),
        )

    def test_provider_inventory_uses_same_backend_config_and_complete_vast_v1_pages(
        self,
    ):
        seen = []

        def provider(req):
            seen.append(req)
            self.assertEqual(req.method, "GET")
            self.assertEqual(
                str(req.url).split("?")[0], "https://console.vast.ai/api/v1/instances/"
            )
            self.assertEqual(req.url.params["select_cols"], '["id"]')
            self.assertEqual(req.url.params["limit"], "25")
            if len(seen) == 1:
                self.assertNotIn("after_token", req.url.params)
                rows, cursor = [{"id": 123}, {"id": 456}], "next-page-token"
            else:
                self.assertEqual(req.url.params["after_token"], "next-page-token")
                rows, cursor = [{"id": 789}], None
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "instances": rows,
                    "instances_found": len(rows),
                    "total_instances": 3,
                    "next_token": cursor,
                },
            )

        result = self.provider_client("vastai", provider).provider_inventory(
            "vastai", clock=lambda: 20.0
        )
        self.assertEqual(
            result,
            {
                "backend": "vastai",
                "complete": True,
                "observed_at": 20.0,
                "instance_ids": ["123", "456", "789"],
            },
        )
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(len(seen), 2)

    def test_vast_v1_empty_is_complete_only_with_a_matching_terminal_tally(self):
        good = {
            "success": True,
            "instances": [],
            "instances_found": 0,
            "total_instances": 0,
            "next_token": None,
        }
        result = self.provider_client(
            "vastai", lambda _: httpx.Response(200, json=good)
        ).provider_inventory("vastai")
        self.assertEqual(result["instance_ids"], [])
        for bad in (
            {"instances": []},
            {**good, "total_instances": 1},
            {**good, "success": False},
            {**good, "instances_found": True},
            {**good, "has_more": True},
            {**good, "next_token": "cursor"},
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(
                    DstackError, "dstack_provider_inventory_unconfirmed"
                ),
            ):
                self.provider_client(
                    "vastai", lambda _, bad=bad: httpx.Response(200, json=bad)
                ).provider_inventory("vastai")

    def test_vast_duplicate_rows_or_repeated_cursor_never_imply_complete_absence(self):
        for second in (
            {"instances": [{"id": 123}], "next_token": None},
            {"instances": [{"id": 456}], "next_token": "cursor"},
        ):
            pages = iter([{"instances": [{"id": 123}], "next_token": "cursor"}, second])

            def read(_):
                page = next(pages)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "instances_found": 1,
                        "total_instances": 2,
                        **page,
                    },
                )

            with (
                self.subTest(second=second),
                self.assertRaisesRegex(
                    DstackError, "dstack_provider_inventory_unconfirmed"
                ),
            ):
                self.provider_client("vastai", read).provider_inventory("vastai")

    def test_runpod_unpaginated_account_query_projects_ids_only(self):
        def read(req):
            self.assertEqual(req.method, "POST")
            self.assertEqual(str(req.url), "https://api.runpod.io/graphql")
            self.assertEqual(
                json.loads(req.content), {"query": "query { myself { pods { id } } }"}
            )
            return httpx.Response(
                200, json={"data": {"myself": {"pods": [{"id": "pod-123"}]}}}
            )

        result = self.provider_client("runpod", read).provider_inventory("runpod")
        self.assertEqual(result["instance_ids"], ["pod-123"])
        for bad in (
            {
                "data": {"myself": {"pods": []}},
                "errors": [{"message": "secret provider token"}],
            },
            {"data": {"myself": {"pods": [], "pageInfo": {"hasNextPage": True}}}},
            {"data": {"myself": {"pods": [{"id": "same"}, {"id": "same"}]}}},
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(
                    DstackError, "^dstack_provider_inventory_unconfirmed$"
                ),
            ):
                self.provider_client(
                    "runpod", lambda _, bad=bad: httpx.Response(200, json=bad)
                ).provider_inventory("runpod")

    def test_provider_redirect_auth_error_invalid_json_and_duplicate_keys_are_closed(
        self,
    ):
        for response, code in (
            (
                httpx.Response(302, headers={"Location": "https://other.example/"}),
                "rejected",
            ),
            (httpx.Response(403, text="secret provider token"), "rejected"),
            (httpx.Response(200, text="secret provider token"), "unavailable"),
            (
                httpx.Response(
                    200, content=b'{"success":true,"instances":[],"instances":[]}'
                ),
                "unconfirmed",
            ),
            (httpx.Response(200, content=b"x" * (4 * 1024**2 + 1)), "response_limit"),
        ):
            seen = []

            def read(req):
                seen.append(req)
                return response

            with (
                self.subTest(code=code),
                self.assertRaisesRegex(
                    DstackError, "^dstack_provider_inventory_" + code + "$"
                ),
            ):
                self.provider_client("vastai", read).provider_inventory("vastai")
            self.assertEqual(len(seen), 1)

    def test_backend_mismatch_never_sends_credentials_to_a_provider(self):
        read = Mock()
        client = self.provider_client("vastai", read, config_changes={"type": "runpod"})
        with self.assertRaisesRegex(
            DstackError, "dstack_provider_inventory_unconfirmed"
        ):
            client.provider_inventory("vastai")
        read.assert_not_called()

    def test_exact_get_accepts_only_pinned_not_found_errors(self):
        cases = [
            (404, {}, True),
            (
                400,
                {"detail": [{"code": "resource_not_exists", "msg": "Run not found"}]},
                True,
            ),
            (400, {"detail": [{"code": "other_error"}]}, False),
            (
                400,
                {"detail": [{"code": "resource_not_exists"}, {"code": "other_error"}]},
                False,
            ),
            (400, {"detail": []}, False),
            (400, [], False),
        ]
        for status, body, missing in cases:
            with self.subTest(status=status, body=body):
                session = httpx.Client(
                    transport=httpx.MockTransport(
                        lambda _, status=status, body=body: httpx.Response(
                            status, json=body
                        )
                    )
                )
                client = DstackClient(
                    "http://127.0.0.1:3000",
                    "secret-token-123456",
                    "sixnine",
                    session=session,
                )
                if missing:
                    self.assertIsNone(client.get(run_id=RUN))
                else:
                    with self.assertRaisesRegex(DstackError, "^dstack_api_rejected$"):
                        client.get(run_id=RUN)

    def test_mutations_do_not_treat_not_found_as_success(self):
        session = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    400,
                    json={
                        "detail": [
                            {"code": "resource_not_exists", "msg": "Run not found"}
                        ]
                    },
                )
            )
        )
        client = DstackClient(
            "http://127.0.0.1:3000", "secret-token-123456", "sixnine", session=session
        )
        for method, argument in (
            (client.apply, request().run_spec(PUB)),
            (client.stop, request().run_name),
        ):
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(DstackError, "^dstack_api_rejected$"):
                    method(argument)

    def test_exact_paths_and_create_only_apply(self):
        seen = []

        def handler(req):
            seen.append(
                (req.url.path, json.loads(req.content), req.headers["Authorization"])
            )
            return httpx.Response(200, json={})

        client = DstackClient(
            "http://127.0.0.1:3000",
            "secret-token-123456",
            "sixnine",
            session=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        client.plan(request().run_spec(PUB))
        client.apply(request().run_spec(PUB))
        client.get(run_id=RUN)
        client.stop(request().run_name)
        self.assertEqual(
            [row[0] for row in seen],
            [
                "/api/project/sixnine/runs/get_plan",
                "/api/project/sixnine/runs/apply",
                "/api/project/sixnine/runs/get",
                "/api/project/sixnine/runs/stop",
            ],
        )
        self.assertFalse(seen[1][1]["force"])
        self.assertFalse(seen[0][1]["for_offers_only"])
        self.assertNotIn("for_offers_only", seen[1][1])
        self.assertIsNone(seen[1][1]["plan"]["current_resource"])
        self.assertFalse(seen[3][1]["abort"])

    def test_server_errors_never_expose_response_secrets(self):
        session = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(500, text="secret prompt and token")
            )
        )
        client = DstackClient(
            "http://localhost:3000", "secret-token-123456", "sixnine", session=session
        )
        with self.assertRaisesRegex(DstackError, "^dstack_api_rejected$"):
            client.get(run_id=RUN)

    def test_remote_plain_http_and_url_credentials_are_refused(self):
        for endpoint in (
            "http://remote.example",
            "https://user:password@example.com",
            "https://example.com/?key=a",
        ):
            with self.assertRaisesRegex(DstackError, "dstack_endpoint_invalid"):
                DstackClient(endpoint, "secret-token-123456", "sixnine")


if __name__ == "__main__":
    unittest.main()
