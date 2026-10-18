"""Read-only member readiness; never admission, stop proof or rental authority."""

from __future__ import annotations

from .repository import request_hash


STATES = ("ready", "busy", "preparing", "unavailable", "held", "unknown")
ACTIVE_JOBS = {"claimed", "submitting", "running", "collecting", "cancel_requested"}
PREPARING_REASONS = {
    "provider_preparing",
    "provider_configuring_ssh",
    "gpu_starting",
    "searching",
    "gpu_busy",
    "gpu_ready",
    "worker_readiness_unconfirmed",
    "capacity_sufficient",
}


def project_member(
    config,
    member_id,
    intent,
    worker,
    *,
    now,
    model_id,
    held=False,
    preparation=None,
    job=None,
    unsafe_jobs=(),
    stopping=False,
):
    """Project one *current* binding from bounded ledger facts supplied by caller.

    A fresh ready Worker is positive slot evidence, not a claim that any given
    queued request fits its recipe, remaining lifetime or other admission gates.
    Historical successful outputs and provider RUNNING are not readiness proof.
    """
    value = {
        "member_id": member_id,
        "intent_id": intent["id"] if intent else None,
        "instance_state": intent["state"] if intent else None,
        "state": "preparing",
        "reason": "member_not_bound",
        "worker_id": None,
        "current_job_id": None,
    }

    def result(state, reason):
        return {**value, "state": state, "reason": reason}

    if intent is None:
        return result("unavailable" if stopping else "preparing", "member_not_bound")
    if held:
        return result("held", "member_repair_required")
    if intent["state"] == "creation_unknown":
        return result("unknown", "creation_outcome_unknown")
    if stopping or intent["state"] in {"draining", "destroying", "destroyed"}:
        return result("unavailable", "member_retiring")
    if intent["hard_deadline"] <= now + config.drain_margin_s:
        return result("unavailable", "member_deadline_margin")
    if worker is None:
        phase = (preparation or {}).get("phase")
        if phase == "retiring_unused":
            return result("held", "member_repair_required")
        if phase == "awaiting_provider":
            return result("preparing", "provider_preparing")
        return result("preparing", "worker_not_registered")

    spec = worker["spec"]
    expected_worker = "lium-" + intent["id"].replace("-", "")
    if (
        worker["id"] != expected_worker
        or worker["pool"] != config.pool
        or worker["provider"] != intent["provider"]
        or not intent["provider_instance_id"]
        or worker["instance_id"] != intent["provider_instance_id"]
        or spec.get("worker_id") != worker["id"]
        or spec.get("pool") != worker["pool"]
        or spec.get("provider") != worker["provider"]
        or spec.get("instance_id") != worker["instance_id"]
        or spec.get("backend") != config.execution_backend
        or spec.get("model_id") != model_id
        or spec.get("configuration_id") != config.configuration_id
        or spec.get("engine_manifest_digest", "") != config.engine_manifest_digest
        or spec.get("output_delivery", "") != config.output_delivery
        or spec.get("recipe_ids") != list(config.recipe_ids)
        or len(spec.get("physical_gpu_ids", ())) != 1
        or worker["spec_hash"] != request_hash(spec)
        or intent["state"] not in {"starting", "ready", "busy"}
    ):
        return result("unknown", "worker_identity_unconfirmed")
    value.update(
        worker_id=worker["id"],
        current_job_id=worker["current_job_id"],
        worker_state=worker["state"],
        expires_at=worker["expires_at"],
    )
    if (
        worker["state"] == "retired"
        or worker["drain_requested"]
        or worker["state"] == "draining"
    ):
        return result("unavailable", "worker_draining_or_retired")
    if worker["expires_at"] <= now or worker["updated_at"] > now:
        return result("unknown", "worker_heartbeat_unconfirmed")
    if worker["state"] == "unknown":
        return result("unknown", "worker_outcome_unknown")
    if worker["current_job_id"] is not None:
        if (
            job is None
            or job["id"] != worker["current_job_id"]
            or job["pool"] != config.pool
            or job["status"] not in ACTIVE_JOBS
            or worker["state"] not in {"leased", "busy", "reconciling"}
            or any(jid != job["id"] for jid in unsafe_jobs)
        ):
            return result("unknown", "worker_job_unconfirmed")
        return result("busy", "worker_has_current_job")
    if unsafe_jobs:
        return result("unknown", "worker_attempt_unresolved")
    if worker["state"] == "ready":
        return result("ready", "worker_ready")
    if worker["state"] == "registered":
        return result("preparing", "worker_not_ready")
    return result("unknown", "worker_state_unconfirmed")


def readiness_reason(existing, members):
    """Keep explicit blockers; an idle slot never proves per-job eligibility."""
    if existing is not None and existing not in PREPARING_REASONS:
        return existing
    states = {member["state"] for member in members}
    if "ready" in states:
        return "gpu_ready"
    if "busy" in states:
        return "gpu_busy"
    if "unknown" in states:
        return "worker_readiness_unconfirmed"
    return existing
