"""Closed no-contract evidence from the pinned native allocator's original run.

A failed run or empty provider list alone is not evidence of a zero-cost start.
Complete typed provider rejections or the pinned pre-allocation fleet-selection
failure qualify. Generic capacity failures remain financially unresolved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid

PREFIX = "sixnine_vast_attempts_v1:"
NO_FLEET_MESSAGE = (
    "No matching fleet found. Possible reasons: "
    "https://dstack.ai/docs/guides/troubleshooting/#no-fleets"
)
NO_FLEET_SOURCE = "native_dstack_0225_no_fleet_v1"
SAFE_REJECTIONS = {
    (400, "invalid_args"),
    (404, "no_such_ask"),
    (410, "no_such_ask"),
    (429, "rate_limited"),
}
ATTEMPT_FIELDS = {"ask_id", "http_status", "code", "outcome", "contract_id"}
PROOF_FIELDS = {
    "version",
    "source",
    "run_id",
    "submission_id",
    "attempted_count",
    "attempts",
    "evidence_sha256",
}


def terminal_no_allocation(run, binding, *, shape_check):
    """Pinned no-capacity failure, not missing-identity or billing evidence.

    A task has one job and retry is disabled. Require its complete original
    submission as well as explicit null allocation/runtime fields. Historical
    allocation, omitted fields or another submission cannot use this exemption.
    Call only after the exact owned run/spec has been validated.
    """
    if not shape_check(run, binding):
        return False
    latest = run["latest_job_submission"]
    message = latest.get("termination_reason_message")
    if isinstance(message, str) and message.startswith(PREFIX):
        # Unknown PUT/returned-contract outcomes never establish zero-charge proof.
        return from_terminal_run(run, binding) is not None
    return True


def _uuid(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_proof_field")
        value[key] = item
    return value


def validate_proof(value, binding):
    """Validate evidence against the immutable selection, not today's stock."""
    try:
        if (
            type(value) is not dict
            or set(value) != PROOF_FIELDS
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["source"] not in {"native_vast_rejections_v1", NO_FLEET_SOURCE}
            or not _uuid(value["run_id"])
            or value["run_id"] != binding.get("run_id")
            or not _uuid(value["submission_id"])
            or binding.get("backend") != "vastai"
            or any(
                binding.get(key) is not None
                for key in (
                    "provider_instance_id",
                    "runtime_incarnation",
                    "first_runtime_ready_at",
                )
            )
            or any(
                binding.get(key) not in (None, False)
                for key in (
                    "bootstrap_started",
                    "bootstrap_launch_started",
                    "runtime_launch_started",
                )
            )
        ):
            return None
        attempts = value["attempts"]
        if value["source"] == NO_FLEET_SOURCE:
            # The pinned scheduler rejected fleet selection before a provider
            # attempt. This is distinct from an empty provider-rejection list.
            if (
                type(value["attempted_count"]) is not int
                or value["attempted_count"] != 0
                or attempts != []
                or type(attempts) is not list
            ):
                return None
            payload = {key: value[key] for key in PROOF_FIELDS - {"evidence_sha256"}}
            return (
                copy.deepcopy(value)
                if value["evidence_sha256"] == _digest(payload)
                else None
            )
        if (
            type(attempts) is not list
            or not 1 <= len(attempts) <= 25
            or type(value["attempted_count"]) is not int
            or value["attempted_count"] != len(attempts)
        ):
            return None
        selected = {item["offer_id"] for item in binding["host_selection"]["offers"]}
        seen = set()
        for item in attempts:
            if (
                type(item) is not dict
                or set(item) != ATTEMPT_FIELDS
                or not isinstance(item["ask_id"], str)
                or re.fullmatch(r"[1-9][0-9]{0,17}", item["ask_id"]) is None
                or item["ask_id"] not in selected
                or item["ask_id"] in seen
                or type(item["http_status"]) is not int
                or (item["http_status"], item["code"]) not in SAFE_REJECTIONS
                or item["outcome"] != "rejected"
                or item["contract_id"] is not None
            ):
                return None
            seen.add(item["ask_id"])
        payload = {key: value[key] for key in PROOF_FIELDS - {"evidence_sha256"}}
        if value["evidence_sha256"] != _digest(payload):
            return None
        return copy.deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def from_terminal_run(run, binding):
    """Call after exact owned-run/spec and terminal_no_allocation validation."""
    try:
        submission = run["latest_job_submission"]
        message = submission.get("termination_reason_message")
        if message == NO_FLEET_MESSAGE:
            from .dstack_capacity import terminal_no_allocation_shape

            # dstack0.22.5 jobs_submitted._apply_no_fleet_selection emits this
            # exact message before provisioning. Require the complete original
            # singleton history and explicit null runtime/allocation fields;
            # a substring, generic failure or current empty inventory is not proof.
            if not terminal_no_allocation_shape(run, binding):
                return None
            proof = {
                "version": 1,
                "source": NO_FLEET_SOURCE,
                "run_id": run["id"],
                "submission_id": submission["id"],
                "attempted_count": 0,
                "attempts": [],
            }
            proof["evidence_sha256"] = _digest(proof)
            return validate_proof(proof, binding)
        if (
            not isinstance(message, str)
            or len(message.encode()) > 8192
            or not message.startswith(PREFIX)
        ):
            return None
        document = json.loads(message[len(PREFIX) :], object_pairs_hook=_unique_object)
        if (
            type(document) is not dict
            or set(document) != {"version", "outcome", "attempted_count", "attempts"}
            or type(document["version"]) is not int
            or document["version"] != 1
            or document["outcome"] != "no_allocation"
        ):
            return None
        proof = {
            "version": 1,
            "source": "native_vast_rejections_v1",
            "run_id": run["id"],
            "submission_id": submission["id"],
            "attempted_count": document["attempted_count"],
            "attempts": document["attempts"],
        }
        proof["evidence_sha256"] = _digest(proof)
        return validate_proof(proof, binding)
    except (ValueError, TypeError, KeyError):
        return None
