"""Slow only historical cancellation maintenance with positively empty capacity.

This is cadence evidence, never job completion, removal or settlement authority.
Live/queued work and uncertain reads retain the existing fast controller loop.
"""

from sqlalchemy import select

from .repository import attempts, jobs

MAINTENANCE_SECONDS = 60
FAST_SECONDS = 15
ACTIVE = (
    "waiting_capacity",
    "queued",
    "claimed",
    "submitting",
    "running",
    "collecting",
    "submission_unknown",
    "cancel_requested",
    "recovery_hold",
)


class IdleCadence:
    def __init__(self):
        self._stamp = None
        self._identity = None
        self.reason = "not_checked"

    def _fast(self, reason):
        self.reason = reason
        return FAST_SECONDS

    def interval(self, controller):
        try:
            seconds = self._interval(controller)
            if seconds == FAST_SECONDS:
                self._stamp = self._identity = None
            checked = getattr(controller, "_health_checked_at", None)
            if seconds > FAST_SECONDS and checked is not None:
                seconds = min(seconds, max(0, checked + 180 - controller.repo.clock()))
            return seconds
        except Exception:
            self._stamp = self._identity = None
            return self._fast("observation_unconfirmed")

    def _interval(self, controller):
        if controller._cleanup_pending or getattr(
            controller.worker_process, "children", None
        ):
            return self._fast("local_runtime_present")
        with controller.repo.engine.connect() as connection:
            rows = list(
                connection.execute(
                    select(
                        jobs.c.id,
                        jobs.c.status,
                        jobs.c.fence,
                        jobs.c.current_attempt_id,
                        jobs.c.lease_worker_id,
                        jobs.c.lease_expires_at,
                    ).where(
                        jobs.c.tenant_id == controller.service.settings.tenant_id,
                        jobs.c.execution_plan["dispatch_backend"].as_string()
                        == "hatchet-v1",
                        jobs.c.status.in_(ACTIVE),
                    )
                ).mappings()
            )
            if not rows or any(
                row["status"] != "cancel_requested"
                or row["lease_worker_id"] is not None
                or row["lease_expires_at"] is not None
                for row in rows
            ):
                return self._fast("not_historical_cancellation_only")
            unresolved = list(
                connection.execute(
                    select(
                        attempts.c.id,
                        attempts.c.job_id,
                        attempts.c.worker_id,
                    )
                    .join(jobs, jobs.c.id == attempts.c.job_id)
                    .where(
                        jobs.c.tenant_id == controller.service.settings.tenant_id,
                        jobs.c.execution_plan["dispatch_backend"].as_string()
                        == "hatchet-v1",
                        (
                            attempts.c.submission_started_at.is_not(None)
                            | attempts.c.upstream_task_id.is_not(None)
                        ),
                        (
                            attempts.c.upstream_stopped.is_(None)
                            | (attempts.c.upstream_stopped != 1)
                        ),
                    )
                ).mappings()
            )
        if any(row["job_id"] not in {job["id"] for job in rows} for row in unresolved):
            return self._fast("other_unresolved_attempt")
        nodes = controller._nodes()
        backends = {node["payload"].get("dstack", {}).get("backend") for node in nodes}
        from .dstack_capacity import BACKENDS

        if not backends or not backends <= BACKENDS:
            return self._fast("node_backend_unconfirmed")
        node_ids = {
            "dstack-" + node["intent_id"].replace("-", "")
            for node in nodes
            if node["runtime_state"] == "stopped"
        }
        if any(row["worker_id"] not in node_ids for row in unresolved):
            return self._fast("unbound_attempt")
        uncertain = []
        for node in nodes:
            if node["runtime_state"] == "stopped":
                binding = node["payload"].get("dstack", {})
                if binding.get("dstack_status") not in {
                    "done",
                    "failed",
                    "terminated",
                    "aborted",
                }:
                    return self._fast("node_not_terminal")
                continue
            fingerprint = controller._passive_fingerprint(node)
            if fingerprint is None:
                return self._fast("node_evidence_unconfirmed")
            uncertain.append((node, fingerprint))
        identity = (
            sorted(tuple(row.values()) for row in rows),
            sorted(tuple(row.values()) for row in unresolved),
            sorted(
                (
                    n["intent_id"],
                    n["command_id"],
                    n["desired_state"],
                    n["runtime_state"],
                    n["payload"].get("command_revision"),
                    n["payload"].get("dstack", {}).get("run_id"),
                    n["payload"].get("dstack", {}).get("backend"),
                )
                for n in nodes
            ),
            [(node["intent_id"], fingerprint) for node, fingerprint in uncertain],
        )
        now = controller.repo.clock()
        if (
            self._identity == identity
            and self._stamp is not None
            and 0 <= now - self._stamp < MAINTENANCE_SECONDS
        ):
            self.reason = "historical_cancellation_only"
            return max(0, self._stamp + MAINTENANCE_SECONDS - now)
        self._stamp = self._identity = None
        client = controller.capacity.client
        from .dstack_controller import _terminal_review_candidate

        # Each ambiguous creation must be an owned TERMINAL run with no runtime
        # evidence. Unknown provider allocation is not a reason to sleep longer.
        for node, fingerprint in uncertain:
            binding = node["payload"]["dstack"]
            if not _terminal_review_candidate(
                client.get(run_id=binding["run_id"]), binding, client.project
            ):
                return self._fast("run_evidence_unconfirmed")
            if fingerprint != controller._passive_fingerprint(node):
                return self._fast("node_evidence_changed")
        # Cover every represented backend, including stopped nodes. An unrelated
        # unconfigured backend is not evidence about these original attempts.
        # The existing full-account safety sweep remains unchanged.
        for backend in sorted(backends):
            if not controller._empty_inventory(
                client.provider_inventory(backend), backend
            ):
                return self._fast("provider_inventory_unconfirmed")
        self._identity, self._stamp = identity, controller.repo.clock()
        self.reason = "historical_cancellation_only"
        return MAINTENANCE_SECONDS
