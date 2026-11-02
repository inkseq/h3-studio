"""Original-ledger native capacity lifecycle and compatibility exports.

Requests, provider transport and observation projections have separate owners.
The coordinator retains the same journal order, funding, unknowns and fences.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass as dataclass
import argparse as argparse
import copy as copy
import hashlib as hashlib
import importlib as importlib
import json as json
import math as math
from pathlib import Path as Path, PurePosixPath as PurePosixPath
import re as re
import random as random
import time as time
from typing import Protocol as Protocol
from urllib.parse import urlsplit as urlsplit
from uuid import UUID as UUID
import httpx as httpx
from .inference.wangp_contract import (
    HostReadiness as HostReadiness,
    safe_model_progress as safe_model_progress,
)
from .dstack_control_plane import (
    control_plane as control_plane,
    foreign_usage as foreign_usage,
    provider_retry_after as provider_retry_after,
)
from .worker_boot import (
    boot_commands as boot_commands,
    boot_env as boot_env,
    boot_record as boot_record,
    boot_valid as boot_valid,
)
from .capacity_memory import vast_ram_floor as vast_ram_floor
from .capacity_interface import CapacityTransport as CapacityTransport
from .providers.registry import RUNPOD as RUNPOD, VASTAI as VASTAI
from .dstack_inventory import (
    CLOUDRIFT_INSTANCES_URL as CLOUDRIFT_INSTANCES_URL,
    KEY_HEADER_URLS as KEY_HEADER_URLS,
    LIUM_PODS_URL as LIUM_PODS_URL,
    RUNPOD_GRAPHQL_URL as RUNPOD_GRAPHQL_URL,
    SUPPORTED as SUPPORTED,
    InventoryUnconfirmed as InventoryUnconfirmed,
    read_ids as read_ids,
)

from .dstack_request import (
    DSTACK_VERSION as DSTACK_VERSION,
    BACKENDS as BACKENDS,
    OWNER as OWNER,
    SHA256 as SHA256,
    IDENTIFIER as IDENTIFIER,
    TERMINAL as TERMINAL,
    RUN_STATUSES as RUN_STATUSES,
    VAST_INSTANCES_URL as VAST_INSTANCES_URL,
    PROVIDER_READ_ATTEMPTS as PROVIDER_READ_ATTEMPTS,
    PROVIDER_READ_MAX_DELAY as PROVIDER_READ_MAX_DELAY,
    NATIVE_REASON_CODES as NATIVE_REASON_CODES,
    native_preparation as native_preparation,
    image_preparation as image_preparation,
    DstackError as DstackError,
    DstackReadRateLimited as DstackReadRateLimited,
    _require as _require,
    _hash as _hash,
    _uuid as _uuid,
    CapacityRequest as CapacityRequest,
    request_from_document as request_from_document,
)

from .dstack_client import DstackClient as DstackClient
from .dstack_observations import (
    assert_run_owned as assert_run_owned,
    _owns as _owns,
    _range as _range,
    _resources as _resources,
    terminal_no_allocation_shape as terminal_no_allocation_shape,
    _terminal_no_allocation_shape as _terminal_no_allocation_shape,
    terminal_no_allocation as terminal_no_allocation,
    live_capacity_usage as live_capacity_usage,
    idle_action as idle_action,
)

_logger = logging.getLogger(__name__)


class DstackStore(Protocol):
    """Implement against original capacity intents under its transactional lock.

    begin_apply commits intent and reservation before HTTP and succeeds once.
    begin_stop commits once, checking original jobs/attempts/collection holds,
    worker retirement and matching immutable run identity in that transaction.
    record merges observations; it MUST retain run_id/incarnation and journals.
    Stop retries require a fresh exact-owned RUNNING observation and a bounded
    durable retry journal under the same original obligation lock. A false gate
    permits no network side effect. No missing row adoption.
    """

    def load(self, intent_id: str) -> dict: ...
    def begin_apply(self, intent_id: str, binding: dict) -> bool: ...
    def record(self, intent_id: str, observation: dict) -> None: ...
    def begin_stop(
        self, intent_id: str, run_id: str, *, retry: bool = False
    ) -> bool: ...


def terminal_review_candidate(run, binding, project):
    """Scheduling evidence only; an unknown allocator result stays unknown."""
    try:
        from .dstack_allocation_proof import ATTEMPT_FIELDS, PREFIX, _unique_object

        assert_run_owned(run, binding, project)
        if (
            run.get("id") != binding.get("run_id")
            or not terminal_no_allocation_shape(run, binding)
            or run["jobs"][0].get("job_connection_info") is not None
        ):
            return False
        message = run["latest_job_submission"].get("termination_reason_message")
        if (
            not isinstance(message, str)
            or len(message.encode()) > 8192
            or not message.startswith(PREFIX)
        ):
            return False
        marker = json.loads(message[len(PREFIX) :], object_pairs_hook=_unique_object)
        selected = {item["offer_id"] for item in binding["host_selection"]["offers"]}
        return (
            isinstance(marker, dict)
            and set(marker) == {"version", "outcome", "attempted_count", "attempts"}
            and type(marker["version"]) is int
            and marker["version"] == 1
            and marker["outcome"] == "unknown"
            and isinstance(marker["attempts"], list)
            and 1 <= len(marker["attempts"]) <= 25
            and type(marker["attempted_count"]) is int
            and marker["attempted_count"] == len(marker["attempts"])
            and all(
                isinstance(item, dict)
                and set(item) == ATTEMPT_FIELDS
                and item["ask_id"] in selected
                and item["contract_id"] is None
                for item in marker["attempts"]
            )
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False

class DstackCapacity:
    def __init__(
        self,
        client: CapacityTransport,
        store: DstackStore,
        *,
        clock=time.time,
        readiness=None,
    ):
        self.client, self.store, self.clock, self.readiness = (
            client,
            store,
            clock,
            readiness,
        )
        configure = getattr(store, "set_host_client", None)
        if callable(configure):
            configure(client)

    def plan(self, request, ssh_key_pub):
        _require(
            request.hard_deadline is None or self.clock() < request.hard_deadline,
            "dstack_window_expired",
        )
        value = self.client.plan(request.run_spec(ssh_key_pub))
        _require(isinstance(value, dict), "dstack_plan_invalid")
        # Offers are advisory, not allocation guarantees or permission to rent.
        return value

    def start(self, request, ssh_key_pub):
        _require(
            request.hard_deadline is None or self.clock() < request.hard_deadline,
            "dstack_window_expired",
        )
        binding = request.binding(ssh_key_pub)
        binding["control_plane"] = control_plane(self.client)
        prior = self.store.load(request.intent_id)
        _require(isinstance(prior, dict), "dstack_intent_missing")
        if prior.get("spec_digest"):
            _require(
                prior["spec_digest"] == binding["spec_digest"],
                "dstack_intent_binding_changed",
            )
        if prior.get("apply_started"):
            return self.reconcile(request.intent_id)
        # Exact pre-read refuses a collision; list absence is never proof.
        existing = self.client.get(run_name=request.run_name)
        _require(existing is None, "dstack_existing_run_requires_reconciliation")
        if not self.store.begin_apply(request.intent_id, binding):
            return self.reconcile(request.intent_id)
        try:
            run = self.client.apply(binding["run_spec"])
            assert_run_owned(run, binding, self.client.project)
            self.store.record(
                request.intent_id,
                {
                    "run_id": run["id"],
                    "state": "starting",
                    "observed_at": self.clock(),
                    "reason_code": "dstack_runtime_not_observed",
                },
            )
        except DstackError:
            self.store.record(
                request.intent_id,
                {
                    "state": "creation_unknown",
                    "observed_at": self.clock(),
                    "reason_code": "dstack_apply_requires_reconciliation",
                },
            )
        return self.observe(request.intent_id)

    def reconcile(self, intent_id):
        """Observe the same original binding; never create or choose a new lane.

        The existing funded-readiness and ledger rules still apply. Terminal
        history uses observe_terminal instead, which cannot bootstrap a worker.
        """
        return self.observe(intent_id)

    def observe_terminal(self, intent_id):
        """Refresh only an exact owned terminal run, without native activity.

        Continuing funding cannot reopen a draining historical intent. Its
        terminal upstream state can still be projected into the original row.
        Nonterminal, absent, failed or changed-identity reads grant no evidence
        and do not alter that row; ordinary funded observation remains separate.
        """
        binding = self.store.load(intent_id)
        _require(
            isinstance(binding, dict) and binding.get("apply_started") is True,
            "dstack_intent_not_submitted",
        )
        try:
            run = (
                self.client.get(run_id=binding["run_id"])
                if binding.get("run_id")
                else self.client.get(run_name=binding["run_name"])
            )
            if run is None:
                return None
            assert_run_owned(run, binding, self.client.project)
            if run.get("status") not in TERMINAL:
                return None
            value = self._observation(run, binding)
            self._confirm_provider_removal(run, binding, value)
        except Exception as _observation_error:
            _logger.warning(
                "dstack_capacity_observe_terminal_failed error_type=%s",
                type(_observation_error).__name__,
            )
            return None
        self.store.record(intent_id, value)
        return value

    def review_unallocated_start(self, intent_id, *, reviewed_by, source):
        """Trusted one-shot owner review of an older unallocated failed start.

        This is not an HTTP/automatic settlement rule. The operator supplies
        the explicit owner confirmation; current exact-run and complete empty
        same-account inventory are independently re-read before ledger commit.
        """
        _require(
            isinstance(reviewed_by, str)
            and IDENTIFIER.fullmatch(reviewed_by)
            and isinstance(source, str)
            and re.fullmatch(
                r"https://github\.com/inkseq/h3-studio/issues/[1-9][0-9]*(?:#issuecomment-[0-9]+)?",
                source,
            ),
            "dstack_no_allocation_review_invalid",
        )
        binding = self.store.load(intent_id)
        _require(
            binding.get("backend") == VASTAI.id and binding.get("run_id"),
            "dstack_no_allocation_review_unconfirmed",
        )
        run = self.client.get(run_id=binding["run_id"])
        assert_run_owned(run, binding, self.client.project)
        _require(
            terminal_no_allocation(run, binding),
            "dstack_no_allocation_review_unconfirmed",
        )
        inventory = self.client.provider_inventory(binding["backend"])
        at = self.clock()
        _require(
            isinstance(inventory, dict)
            and inventory.get("backend") == binding["backend"]
            and inventory.get("complete") is True
            and inventory.get("instance_ids") == []
            and type(inventory.get("observed_at")) in (int, float)
            and 0 <= at - inventory["observed_at"] <= 30,
            "dstack_no_allocation_review_unconfirmed",
        )
        latest = run["latest_job_submission"]
        message = latest.get("termination_reason_message")
        _require(
            isinstance(message, str) and 0 < len(message.encode()) <= 8192,
            "dstack_no_allocation_review_unconfirmed",
        )
        review = {
            "version": 1,
            "source": source,
            "reviewed_by": reviewed_by,
            "owner_confirmation": "no_machine_allocated",
            "run_id": run["id"],
            "submission_id": latest["id"],
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "observed_at": at,
            "inventory_observed_at": inventory["observed_at"],
            "provider": binding["backend"],
            "complete_account_empty": True,
        }
        return self.store.review_unallocated_start(intent_id, review)

    def observe(self, intent_id, *, _on_observed=None):
        binding = self.store.load(intent_id)
        _require(
            isinstance(binding, dict) and binding.get("apply_started") is True,
            "dstack_intent_not_submitted",
        )
        observed_run = None
        try:
            run = (
                self.client.get(run_id=binding["run_id"])
                if binding.get("run_id")
                else self.client.get(run_name=binding["run_name"])
            )
            if run is None:
                value = {
                    "state": "creation_unknown"
                    if not binding.get("stop_started")
                    else "removal_unknown",
                    "ready": False,
                    "reason_code": "dstack_exact_run_absent",
                    "observed_at": self.clock(),
                }
            else:
                assert_run_owned(run, binding, self.client.project)
                value = self._observation(run, binding)
                self._confirm_provider_removal(run, binding, value)
                observed_run = run
        except DstackError as error:
            value = {
                "state": "observation_unknown",
                "ready": False,
                "reason_code": str(error),
                "observed_at": self.clock(),
            }
        except Exception as _observation_error:
            _logger.warning(
                "dstack_capacity_observe_failed error_type=%s",
                type(_observation_error).__name__,
            )
            value = {
                "state": "observation_unknown",
                "ready": False,
                "reason_code": "dstack_response_invalid",
                "observed_at": self.clock(),
            }
        self.store.record(intent_id, value)
        # A controller may reuse this exact read for scheduling only. The raw
        # run never enters the ledger or changes the original observation.
        if observed_run is not None and _on_observed is not None:
            _on_observed(observed_run)
        return value

    def _confirm_provider_removal(self, run, binding, value):
        # Native terminal state alone is not physical removal. Consult
        # the existing credential-bound complete provider inventory only
        # for a known original instance, never an unknown allocation.
        identity = binding.get("provider_instance_id")
        if (
            value.get("state") == "stopped"
            and identity
            and not binding.get("provider_removal_proof")
        ):
            try:
                inventory = self.client.provider_inventory(binding["backend"])
                ids = inventory.get("instance_ids")
                at = self.clock()
                if (
                    inventory.get("backend") == binding["backend"]
                    and inventory.get("complete") is True
                    and isinstance(ids, list)
                    and len(ids) <= 4096
                    and all(isinstance(i, str) and IDENTIFIER.fullmatch(i) for i in ids)
                    and len(set(ids)) == len(ids)
                    and identity not in ids
                    and type(inventory.get("observed_at")) in (int, float)
                    and math.isfinite(inventory["observed_at"])
                    and 0 <= at - inventory["observed_at"] <= 30
                ):
                    value["provider_removal_proof"] = {
                        "version": 1,
                        "backend": binding["backend"],
                        "run_id": run["id"],
                        "provider_instance_id": identity,
                        "observed_at": at,
                        "inventory_observed_at": inventory["observed_at"],
                        "complete": True,
                        "absent": True,
                        "inventory_sha256": hashlib.sha256(
                            json.dumps(sorted(ids), separators=(",", ":")).encode()
                        ).hexdigest(),
                    }
            except Exception as _observation_error:
                _logger.warning(
                    "dstack_capacity_confirm_provider_removal_failed error_type=%s",
                    type(_observation_error).__name__,
                )
                pass  # Failed reads never become absence or free billing.

    def _observation(self, run, binding):
        status = run.get("status")
        _require(
            isinstance(status, str) and status in RUN_STATUSES,
            "dstack_run_status_invalid",
        )
        value = {
            "run_id": run["id"],
            "run_name": binding["run_name"],
            "dstack_status": status,
            "state": "starting",
            "ready": False,
            "model_load_state": "not_observed",
            "billing_state": "unsettled",
            "observed_at": self.clock(),
            "reason_code": "dstack_runtime_not_observed",
        }
        submission = run.get("latest_job_submission") or {}
        _require(isinstance(submission, dict), "dstack_response_invalid")
        image = image_preparation(submission)
        preparation_phase = (
            "image_pull" if submission.get("status") == "pulling" else "provisioning"
        )
        value["preparation"] = {
            "phase": preparation_phase,
            "phase_started_at": None,
            "updated_at": value["observed_at"],
            "download": None,
            "image": image,
            "model": None,
        }
        # The library's modeled cost is an estimate, not a supplier invoice.
        cost = run.get("cost")
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            value["estimated_cost_microusd"] = round(cost * 1_000_000)
        # Allocation evidence may exist before RUNNING (or survive terminal
        # failure). Never postpone its capture until a successful bootstrap.
        provisioning = submission.get("job_provisioning_data")
        if isinstance(provisioning, dict) and provisioning:
            try:
                physical = self._host_observation(
                    provisioning, binding, value["observed_at"]
                )
            except DstackError:
                # Physical telemetry cannot erase an exact owned upstream
                # terminal state when the supplier has already removed it.
                # This grants no host identity, readiness, removal or billing
                # proof; live-capacity exclusion still needs fresh complete
                # provider absence through its original independent guard.
                if status not in TERMINAL:
                    raise
                physical = None
            if physical is not None:
                value.update(
                    host_observation=physical,
                    provider_instance_id=physical["provider_instance_id"],
                )
            if (
                physical is not None
                and status == "failed"
                and not binding.get("stop_started")
                and not binding.get("first_runtime_ready_at")
                and submission.get("termination_reason")
                == "waiting_runner_limit_exceeded"
            ):
                value["host_failure"] = {
                    "version": 1,
                    "stage": "runtime_start",
                    "reason_code": "provider_runner_timeout",
                    "at": value["observed_at"],
                    "qualified_start_failure": True,
                }
        if status in TERMINAL:
            value.update(
                state="stopped", reason_code="dstack_run_terminal_billing_pending"
            )
            message = submission.get("termination_reason_message")
            if (
                binding.get("backend") == RUNPOD.id
                and isinstance(message, str)
                and message.startswith("sixnine_runpod_outcome_v1:")
            ):
                # The original create may have allocated. A terminal native job
                # is not provider absence, a free slot, or permission to reapply.
                value.update(
                    state="creation_unknown",
                    reason_code="dstack_allocation_requires_reconciliation",
                )
                return value
            if isinstance(message, str) and message.startswith(
                "sixnine_vast_attempts_v1:"
            ):
                from .dstack_allocation_proof import from_terminal_run

                if from_terminal_run(run, binding) is None:
                    value.update(
                        state="creation_unknown",
                        reason_code="dstack_allocation_requires_reconciliation",
                    )
                    return value
            if terminal_no_allocation(run, binding):
                value["reason_code"] = "dstack_no_capacity_billing_pending"
                from .dstack_allocation_proof import account_absence, from_terminal_run

                proof = from_terminal_run(run, binding) or account_absence(
                    self.client, run, binding
                )
                if proof is not None:
                    value["no_allocation_proof"] = proof
                    value.update(
                        billing_state="no_charge_confirmed",
                        reason_code="dstack_never_allocated",
                    )
            value["preparation"]["phase"] = "stopped"
            return value
        if status in {"terminating", "stopping"}:
            value.update(state="stopping", reason_code="dstack_stop_pending")
            value["preparation"]["phase"] = "stopping"
            return value
        if status != "running":
            if binding.get("stop_started"):
                value.update(state="stopping", reason_code="dstack_stop_pending")
                value["preparation"]["phase"] = "stopping"
            return value
        provisioning = submission.get("job_provisioning_data") or {}
        _require(isinstance(provisioning, dict), "dstack_response_invalid")
        _require(
            provisioning.get("backend") == binding["backend"],
            "dstack_backend_binding_mismatch",
        )
        resource = provisioning.get("instance_type", {}).get("resources", {})
        _require(
            isinstance(resource, dict)
            and isinstance(resource.get("gpus"), list)
            and len(resource["gpus"]) == 1,
            "dstack_allocated_gpu_count_unconfirmed",
        )
        instance = provisioning.get("instance_id")
        _require(
            isinstance(instance, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", instance),
            "dstack_instance_identity_unconfirmed",
        )
        if binding.get("provider_instance_id"):
            _require(
                instance == binding["provider_instance_id"],
                "dstack_instance_identity_changed",
            )
        value["provider_instance_id"] = instance
        if binding.get("stop_started"):
            # The original stop already fenced/retired inference. Keep fresh
            # provider identity evidence without bootstrapping/reopening it.
            value.update(state="stopping", reason_code=self._stop_retry_reason(binding))
            value["preparation"]["phase"] = "stopping"
            return value
        value["state"] = "runtime_unconfirmed"
        if self.readiness is None:
            return value
        # Cold bootstrap needs the exact provider identity journaled first.
        # A warm check must not transiently revoke its original qualified
        # proof: concurrent admission may use it only until its existing
        # timestamp expires. Neither a pending check nor its later success
        # moves this provider-read timestamp forward.
        incarnation = binding.get("runtime_incarnation")
        observed = binding.get("observed_at")
        warm = (
            binding.get("state") in {"ready", "busy"}
            and binding.get("dstack_status") == "running"
            and binding.get("run_id") == run["id"]
            and binding.get("provider_instance_id") == instance
            and isinstance(incarnation, str)
            and re.fullmatch(r"[0-9a-f]{32}", incarnation)
            and type(observed) in (int, float)
            and math.isfinite(observed)
            and 0 <= observed <= value["observed_at"]
        )
        if not warm:
            provider_observation = copy.deepcopy(value)
            # This identity journal precedes the native read, not a fresh native
            # phase transition. Do not reset an already observed download timer.
            provider_observation.pop("preparation", None)
            self.store.record(binding["intent_id"], provider_observation)
            binding = self.store.load(binding["intent_id"])
        try:
            native = self.readiness(copy.deepcopy(binding), copy.deepcopy(run))
        except DstackError as error:
            # Native setup reports normal cold-start progress through this
            # exact static code. Do not expose arbitrary callback text, or
            # infer readiness from the preparation observation.
            phases = {
                "dstack_runtime_preparing": "provisioning",
                "dstack_runtime_downloading_weights": "model_download",
                "dstack_runtime_starting_runtime": "runtime_start",
                "dstack_runtime_loading_model": "model_load",
            }
            code = (
                error.args[0]
                if len(error.args) == 1 and isinstance(error.args[0], str)
                else None
            )
            value["reason_code"] = (
                code
                if code in NATIVE_REASON_CODES
                else "dstack_native_observation_failed"
            )
            preparation = native_preparation(getattr(error, "preparation", None))
            if preparation is None:
                preparation = native_preparation(binding.get("preparation"))
            if preparation is not None:
                value["preparation"] = {**preparation, "image": image}
                if preparation.get("reason_code"):
                    value["reason_code"] = preparation["reason_code"]
            if code in phases:
                value["bootstrap_phase"] = phases[code]
            return value
        except Exception as _observation_error:
            _logger.warning(
                "dstack_capacity_observation_failed error_type=%s",
                type(_observation_error).__name__,
            )
            value["reason_code"] = "dstack_native_observation_failed"
            return value
        _require(
            isinstance(native, HostReadiness)
            and native.slot_key == binding["intent_id"]
            and native.manifest_digest == binding["manifest_digest"]
            and type(native.idle) is bool
            and isinstance(native.incarnation, str)
            and re.fullmatch(r"[0-9a-f]{32}", native.incarnation),
            "dstack_native_binding_mismatch",
        )
        if binding.get("runtime_incarnation"):
            _require(
                native.incarnation == binding["runtime_incarnation"],
                "dstack_native_incarnation_changed",
            )
        value.update(
            state="ready" if native.idle else "busy",
            ready=native.idle,
            runtime_incarnation=native.incarnation,
            bootstrap_phase="runtime_ready",
            reason_code="dstack_runtime_ready_model_load_unobserved"
            if native.idle
            else "dstack_runtime_busy",
        )
        preparation = native_preparation(native.preparation)
        if preparation is None:
            preparation = {
                "phase": "runtime_ready",
                "phase_started_at": None,
                "updated_at": value["observed_at"],
                "download": None,
                "model": None,
            }
        model = preparation.get("model")
        if model is not None:
            value["model_load_state"] = model["state"]
            if model["state"] == "loading":
                preparation["phase"] = "model_load"
                preparation["phase_started_at"] = model.get("phase_started_at")
                preparation["updated_at"] = model.get("updated_at")
            elif model["state"] == "loaded" and native.idle:
                value["reason_code"] = "dstack_runtime_ready"
            if model.get("reason_code"):
                preparation["reason_code"] = model["reason_code"]
                value["reason_code"] = model["reason_code"]
        value["preparation"] = {**preparation, "image": image}
        return value

    def _host_observation(self, provisioning, binding, now):
        """Original JPD resources plus one same-account physical identity.

        An expected/selected offer is not an allocated host. Old unknown runs
        remain compatible; new enforced selectors fail closed before readiness.
        """
        if binding.get("backend") != VASTAI.id:
            return None
        strict = binding.get("host_selection") is not None
        try:
            _require(
                provisioning.get("backend") == VASTAI.id,
                "dstack_host_allocation_unconfirmed",
            )
            instance = provisioning.get("instance_id")
            kind = provisioning.get("instance_type")
            _require(
                isinstance(instance, str)
                and re.fullmatch(r"[1-9][0-9]{0,17}", instance)
                and isinstance(kind, dict)
                and isinstance(kind.get("name"), str)
                and re.fullmatch(r"[1-9][0-9]{0,17}", kind["name"]),
                "dstack_host_allocation_unconfirmed",
            )
            prior = binding.get("host_observation")
            if prior is not None:
                from .host_policy import observation

                prior = observation(prior, now=now)
                _require(
                    prior["provider_instance_id"] == instance
                    and prior["offer_id"] == kind["name"],
                    "dstack_host_allocation_changed",
                )
                return prior
            reader = getattr(self.client, "vast_instance_identity", None)
            _require(callable(reader), "dstack_host_allocation_unconfirmed")
            identity = reader(instance)
            _require(
                isinstance(identity, dict)
                and identity.get("provider_instance_id") == instance,
                "dstack_host_allocation_unconfirmed",
            )
            if strict:
                approved = {
                    r["offer_id"]: r for r in binding["host_selection"]["offers"]
                }
                _require(
                    approved.get(kind["name"], {}).get("machine_id")
                    == identity.get("machine_id"),
                    "dstack_host_allocation_changed",
                )
            resource = kind.get("resources", {})
            gpus = resource.get("gpus")
            _require(
                isinstance(gpus, list) and len(gpus) == 1 and isinstance(gpus[0], dict),
                "dstack_host_allocation_unconfirmed",
            )
            price = provisioning.get("price")
            _require(
                type(price) in (int, float) and math.isfinite(price) and price > 0,
                "dstack_host_allocation_unconfirmed",
            )
            _require(
                not strict
                or price <= binding["run_spec"]["configuration"]["max_price"],
                "dstack_host_allocation_unconfirmed",
            )
            value = {
                "version": 1,
                "provider": "vastai",
                "machine_id": identity["machine_id"],
                "provider_instance_id": instance,
                "offer_id": kind["name"],
                "observed_at": now,
                "region": provisioning.get("region"),
                "gpu": gpus[0].get("name"),
                "gpu_count": 1,
                "allocated_ram_mib": resource.get("memory_mib"),
                "allocated_vram_mib": gpus[0].get("memory_mib"),
                "cpu": resource.get("cpus"),
                "disk_mib": resource.get("disk", {}).get("size_mib"),
                "reliability": identity.get("reliability"),
                "quoted_hourly_microusd": math.ceil(price * 1_000_000),
                "identity_confirmed": True,
                "physical_id_source": "vast_machine_id",
            }
            if strict:
                from .host_policy import offer_metadata

                advertised = offer_metadata(
                    {
                        key: approved[kind["name"]][key]
                        for key in ("country", "region", "advertised_download_mbps")
                        if key in approved[kind["name"]]
                    }
                )
                value.update(
                    country=advertised["country"],
                    advertised_download_mbps=advertised["advertised_download_mbps"],
                )
            from .host_policy import observation

            return observation(value, now=now)
        except Exception as _observation_error:
            _logger.warning(
                "dstack_capacity_host_observation_failed error_type=%s",
                type(_observation_error).__name__,
            )
            if strict:
                raise DstackError("dstack_host_allocation_unconfirmed") from None
            return None

    def _stop_retry_reason(self, binding):
        count, due = (
            binding.get("stop_attempt_count"),
            binding.get("stop_next_retry_at"),
        )
        if (
            type(count) is not int
            or count < 1
            or type(due) not in (int, float)
            or not math.isfinite(due)
            or due <= 0
        ):
            return "dstack_stop_retry_journal_unconfirmed"
        if count >= 5:
            return "dstack_stop_retry_exhausted"
        return (
            "dstack_stop_retry_waiting"
            if self.clock() < due
            else "dstack_stop_retry_due"
        )

    def stop(self, intent_id):
        binding = self.store.load(intent_id)
        _require(
            isinstance(binding, dict) and binding.get("run_id"),
            "dstack_owned_run_id_required",
        )
        if binding.get("stop_started"):
            # dstack 0.22.5 stop_runs only moves existing nonfinished runs to
            # TERMINATING under locks; repeating that stop never creates a run.
            # Source: dstack/_internal/server/services/runs/__init__.py:868-909
            # in upstream tag 0.22.5 (not the create/update apply endpoint).
            # An accepted lost response normally already reads TERMINATING.
            # Only a *new* exact-owned positive RUNNING read can retry a stop
            # that was never delivered, after the original ledger's due/count
            # CAS. Cached RUNNING, absent/unknown and terminal reads cannot.
            observation = self.observe(intent_id)
            if (
                observation.get("dstack_status") != "running"
                or observation.get("run_id") != binding["run_id"]
                or not binding.get("provider_instance_id")
                or observation.get("provider_instance_id")
                != binding["provider_instance_id"]
            ):
                return observation
            binding = self.store.load(intent_id)
            reason = self._stop_retry_reason(binding)
            if reason != "dstack_stop_retry_due":
                return {**observation, "reason_code": reason}
            # observe() has committed the fresh exact identity. This gate must
            # recheck original jobs, worker retirement and the retry journal
            # atomically; process-local counters do not survive restart.
            if not self.store.begin_stop(intent_id, binding["run_id"], retry=True):
                reason = self._stop_retry_reason(self.store.load(intent_id))
                return {
                    **observation,
                    "reason_code": reason
                    if reason != "dstack_stop_retry_due"
                    else "dstack_business_obligations_pending",
                }
        else:
            run = self.client.get(run_id=binding["run_id"])
            _require(run is not None, "dstack_stop_identity_unconfirmed")
            assert_run_owned(run, binding, self.client.project)
            if run.get("status") in TERMINAL | {"terminating", "stopping"}:
                value = self._observation(run, binding)
                self.store.record(intent_id, value)
                return value
            if not self.store.begin_stop(intent_id, binding["run_id"]):
                return {
                    "state": "draining",
                    "ready": False,
                    "reason_code": "dstack_business_obligations_pending",
                    "observed_at": self.clock(),
                }
        try:
            self.client.stop(binding["run_name"])
            value = {
                "state": "stopping",
                "ready": False,
                "reason_code": "dstack_stop_pending",
                "observed_at": self.clock(),
            }
        except DstackError:
            value = {
                "state": "removal_unknown",
                "ready": False,
                "reason_code": "dstack_stop_requires_reconciliation",
                "observed_at": self.clock(),
            }
        self.store.record(intent_id, value)
        return value


def native_bootstrap_config(
    request,
    *,
    source_bundle_sha256,
    prepared_root="/opt/workspace-internal/Wan2GP",
    model_root="/root/sixnine-cache/models",
):
    """Render the unchanged native bootstrap interface, without any credentials.

    The CPU controller must upload its exact bound bundle/manifest and a unique
    private runtime token, commit bootstrap intent, then invoke the original
    one-shot bootstrap once. A timeout re-observes that original operation.
    Source and environment checks remain in wangp_profile_bootstrap.prepare.
    """
    _require(
        isinstance(source_bundle_sha256, str)
        and SHA256.fullmatch(source_bundle_sha256),
        "dstack_source_bundle_digest_invalid",
    )
    for path in (prepared_root, model_root):
        _require(
            isinstance(path, str)
            and PurePosixPath(path).is_absolute()
            and not any(part in {"..", "."} for part in PurePosixPath(path).parts)
            and re.fullmatch(r"/[A-Za-z0-9_./-]+", path),
            "dstack_runtime_path_invalid",
        )
    base = "/workspace/h3-studio/profile-slot-0"
    return {
        "version": 1,
        "install_root": "/root/sixnine-cache/operator/profile-slot-0",
        "source_bundle_path": base + "/wangp-package.tar.gz",
        "source_bundle_sha256": source_bundle_sha256,
        "dependency_artifact_url": "",
        "dependency_artifact_path": "",
        "dependency_artifact_sha256": "0" * 64,
        "manifest_path": base + "/wangp-manifest.json",
        "model_root": model_root,
        "config_path": "/root/sixnine-cache/operator/profile-slot-0/wgp_config.json",
        "status_path": base + "/setup-status.json",
        "port": 8199,
        "prepared_root": prepared_root,
        "deployment_profile_id": request.profile_id,
        "profile_slot_index": 0,
        "expected_host_gpus": 1,
    }


def main(argv=None):
    """Trusted CPU operations CLI. Persistence is the original service factory."""
    parser = argparse.ArgumentParser(
        description="Sixnine dstack capacity operations (0.22.5)"
    )
    parser.add_argument(
        "action",
        choices=("spec", "bootstrap-config", "plan", "start", "observe", "stop"),
    )
    parser.add_argument(
        "--request",
        type=Path,
        required=True,
        help="Frozen, non-secret CapacityRequest JSON",
    )
    parser.add_argument("--ssh-public-key", type=Path)
    parser.add_argument("--source-bundle-sha256")
    parser.add_argument(
        "--factory",
        help="Existing-ledger studio_platform.module:factory; takes no arguments",
    )
    args = parser.parse_args(argv)
    try:
        _require(
            args.request.is_absolute()
            and not args.request.is_symlink()
            and args.request.stat().st_size <= 16384,
            "dstack_request_file_invalid",
        )
        values = json.loads(args.request.read_text(encoding="utf-8"))
        _require(isinstance(values, dict), "dstack_request_file_invalid")
        for key in ("gpu_names", "regions"):
            if key in values:
                _require(isinstance(values[key], list), "dstack_request_file_invalid")
                values[key] = tuple(values[key])
        request = CapacityRequest(**values)
        public_key = None
        if args.action in {"spec", "plan", "start"}:
            _require(
                args.ssh_public_key is not None
                and args.ssh_public_key.is_absolute()
                and not args.ssh_public_key.is_symlink()
                and args.ssh_public_key.stat().st_size <= 16384,
                "dstack_public_ssh_key_invalid",
            )
            public_key = args.ssh_public_key.read_text(encoding="utf-8").strip()
        if args.action == "spec":
            result = request.run_spec(public_key)
        elif args.action == "bootstrap-config":
            result = native_bootstrap_config(
                request, source_bundle_sha256=args.source_bundle_sha256
            )
        else:
            _require(
                isinstance(args.factory, str)
                and re.fullmatch(
                    r"studio_platform\.[A-Za-z0-9_]+:[A-Za-z0-9_]+", args.factory
                ),
                "dstack_existing_store_factory_required",
            )
            module, function = args.factory.split(":")
            capacity = getattr(importlib.import_module(module), function)()
            _require(
                isinstance(capacity, DstackCapacity), "dstack_capacity_factory_invalid"
            )
            if args.action == "plan":
                plan = capacity.plan(request, public_key)
                plans = plan.get("job_plans", [])
                result = {
                    "run_name": request.run_name,
                    "backend": request.backend,
                    "profile_id": request.profile_id,
                    "mode": request.mode,
                    "total_offers": sum(p.get("total_offers", 0) for p in plans),
                    "current_resource_exists": plan.get("current_resource") is not None,
                    "creation_started": False,
                }
            elif args.action == "start":
                result = capacity.start(request, public_key)
            else:
                result = getattr(capacity, args.action)(request.intent_id)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except DstackError as error:
        print(json.dumps({"state": "rejected", "reason_code": str(error)}))
    except Exception as _observation_error:
        _logger.warning(
            "dstack_capacity_main_failed error_type=%s",
            type(_observation_error).__name__,
        )
        print(json.dumps({"state": "rejected", "reason_code": "dstack_cli_failed"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
