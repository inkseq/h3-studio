"""Lease-fenced task transitions. Only authenticated control-plane workers use this API.

An upstream task is never recreated by reconciliation or collection. A submission
intent is persisted before sending; an ambiguous response requires reconciliation.
No method in this module sends requests, reads secrets, or starts a thread.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import math
import json
import hashlib
import uuid

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .repository import (
    Conflict, InvalidTransition, LeaseLost, NotFound, artifacts, attempts,
    canonical, identifier, jobs, money, owner_usage, scheduler_state,
)


@dataclass(frozen=True)
class Lease:
    job_id: str
    attempt_id: str
    worker_id: str
    fence: int
    expires_at: float


@dataclass(frozen=True)
class Claim:
    job: dict
    lease: Lease


class TaskQueue:
    def __init__(self, repository):
        self.repository = repository

    def _scheduler_lock(self, connection, pool):
        table_insert = sqlite_insert if self.repository.engine.dialect.name == "sqlite" else pg_insert
        connection.execute(table_insert(scheduler_state).values(pool=pool)
                           .on_conflict_do_nothing(index_elements=["pool"]))
        return self.repository._locked(connection, select(scheduler_state).where(scheduler_state.c.pool == pool))

    def claim(self, worker_id, pool, *, lease_seconds=90, purpose="generate", connection=None, job_ids=None,
              job_filter=None, validator=None, dispatch_backend="legacy", selected_job_id=None,
              selected_validator=None):
        """Claim one task; returns Claim or None. Collection reuses its original attempt.

        Per-pool row locking serializes short scheduling transactions on PostgreSQL.
        Eligible tasks waiting at least 15 minutes are FIFO before newer work.
        Newer work favors less occupied owners, then estimated cumulative work.
        Aging is a priority, not a start-time guarantee or permission to release
        held attempts, budgets or physical slots. Running work is not preempted.
        """
        identifier(worker_id)
        identifier(pool)
        if selected_job_id is not None:
            identifier(selected_job_id)
        if dispatch_backend not in {"legacy", "hatchet-v1"}:
            raise ValueError("invalid_dispatch_backend")
        if not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 3600:
            raise ValueError("invalid_lease_duration")
        states = {"generate": ("queued",), "collect": ("collecting",),
                  "reconcile": ("submission_unknown", "running", "cancel_requested")}
        if purpose not in states:
            raise ValueError("invalid_claim_purpose")
        repo = self.repository
        with repo.transaction() if connection is None else nullcontext(connection) as connection:
            self._scheduler_lock(connection, pool)
            # Prompt/source snapshots can be MiB each. Scheduling candidates
            # contain scalars only; load and lock one selected job at a time.
            ordered, usage = self.ordered_candidates(connection, pool, purpose=purpose,
                dispatch_backend=dispatch_backend, job_ids=job_ids, job_filter=job_filter)
            job = None
            for candidate in ordered:
                selected = repo._job(connection, candidate["id"], lock=True)
                # An API cancellation may commit after candidate selection.
                # Eligibility must be checked again under the selected row lock.
                if (selected["status"] not in states[purpose] or selected["not_before"] > repo.clock()
                    or selected["lease_worker_id"] is not None):
                    continue
                if validator is not None and validator(selected) is not True:
                    continue
                if purpose == "generate" and selected["current_attempt_id"]:
                    previous = connection.execute(select(attempts.c.submission_started_at, attempts.c.upstream_task_id)
                        .where(attempts.c.id == selected["current_attempt_id"], attempts.c.job_id == selected["id"])).first()
                    if previous is None or previous.submission_started_at is not None or previous.upstream_task_id is not None:
                        # A malformed/manual requeue must not turn uncertain
                        # execution into a second paid submission. Keep original
                        # attempt and budget evidence for operator reconciliation.
                        connection.execute(update(jobs).where(jobs.c.id == selected["id"]).values(
                            status="recovery_hold", fence=selected["fence"]+1,
                            error_code="generation_retry_requires_reconciliation", updated_at=repo.clock()))
                        repo._emit(connection, "job.recovery_hold", selected["id"],
                            {"job_id": selected["id"], "status": "recovery_hold"})
                        continue
                job = selected
                break
            if job is None:
                return None
            # Identity-only broker delivery cannot choose a lower-priority
            # business job or steal the selected job. Yield without an attempt;
            # recovery will publish another wakeup after the broker run ends.
            if (selected_job_id is not None and job["id"] != selected_job_id
                    or selected_validator is not None and selected_validator(job) is not True):
                return None
            fence = job["fence"] + 1
            expires = repo.clock() + float(lease_seconds)
            values = dict(fence=fence, lease_worker_id=worker_id, lease_expires_at=expires,
                          updated_at=repo.clock())
            if purpose == "generate":
                attempt_id = str(uuid.uuid4())
                values.update(status="claimed", current_attempt_id=attempt_id, attempt_no=job["attempt_no"] + 1)
                connection.execute(insert(attempts).values(id=attempt_id, job_id=job["id"],
                    number=values["attempt_no"], status="claimed", fence=fence, worker_id=worker_id,
                    created_at=repo.clock(), updated_at=repo.clock(), upstream_stopped=0, collection_failures=0))
                key = self._owner_key(job)
                if key in usage:
                    connection.execute(update(owner_usage).where(owner_usage.c.pool == pool,
                        owner_usage.c.owner_key == key).values(work_s=owner_usage.c.work_s + job["expected_runtime_s"]))
                else:
                    connection.execute(insert(owner_usage).values(pool=pool, owner_key=key,
                        work_s=job["expected_runtime_s"]))
            else:
                attempt_id = job["current_attempt_id"]
                if not attempt_id:
                    raise Conflict("missing_attempt")
                connection.execute(update(attempts).where(attempts.c.id == attempt_id)
                    .values(fence=fence, worker_id=worker_id, updated_at=repo.clock()))
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(**values))
            repo._emit(connection, "job.leased", job["id"],
                       {"job_id": job["id"], "attempt_id": attempt_id, "fence": fence, "purpose": purpose})
            return Claim(repo._job(connection, job["id"]), Lease(job["id"], attempt_id, worker_id, fence, expires))

    def ordered_candidates(self, connection, pool, *, purpose, dispatch_backend,
                           job_ids=None, job_filter=None):
        """Read the same candidate order for claims and advisory broker wakeups."""
        states = {"generate": ("queued",), "collect": ("collecting",),
                  "reconcile": ("submission_unknown", "running", "cancel_requested")}
        statement = select(jobs.c.id, jobs.c.tenant_id, jobs.c.owner_id, jobs.c.created_at).where(
            jobs.c.pool == pool, jobs.c.status.in_(states[purpose]),
            jobs.c.not_before <= self.repository.clock(), jobs.c.lease_worker_id.is_(None),
            func.coalesce(jobs.c.execution_plan["dispatch_backend"].as_string(), "legacy") == dispatch_backend)
        if job_ids is not None:
            statement = statement.where(jobs.c.id.in_(job_ids))
        if job_filter is not None:
            statement = statement.where(job_filter)
        candidates = [dict(row) for row in connection.execute(statement
            .order_by(jobs.c.created_at, jobs.c.id)).mappings()]
        if not candidates:
            return [], {}
        return self.schedule_order(connection, candidates, pool, purpose=purpose)

    def schedule_order(self, connection, candidates, pool, *, purpose="generate"):
        """The shared business priority; reading it neither claims nor bills."""
        repo = self.repository
        usage = {r["owner_key"]: r["work_s"] for r in connection.execute(
            select(owner_usage).where(owner_usage.c.pool == pool)).mappings()}
        active = {}
        if purpose == "generate":
            for row in connection.execute(select(jobs.c.tenant_id, jobs.c.owner_id).where(
                    jobs.c.pool == pool, jobs.c.status.in_(
                    ("claimed", "submitting", "running", "submission_unknown", "cancel_requested", "recovery_hold")))).mappings():
                key = self._owner_key(row)
                active[key] = active.get(key, 0) + 1
        def priority(job):
            key = self._owner_key(job)
            age = max(0, repo.clock() - job["created_at"])
            if age >= 900:
                return (0, job["created_at"], job["id"])
            return (1, active.get(key, 0), usage.get(key, 0), job["created_at"], job["id"])
        return sorted(candidates, key=priority), usage

    @staticmethod
    def _owner_key(row):
        # Tuple encoding avoids tenant/owner delimiter collisions.
        encoded = json.dumps([row["tenant_id"], row["owner_id"]], separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _leased(self, connection, lease):
        job = self.repository._job(connection, lease.job_id, lock=True)
        if job["status"] == "recovery_hold":
            raise LeaseLost("recovery_hold_requires_operator_review")
        if (job["current_attempt_id"] != lease.attempt_id or job["fence"] != lease.fence or
            job["lease_worker_id"] != lease.worker_id or job["lease_expires_at"] is None or
            job["lease_expires_at"] <= self.repository.clock()):
            raise LeaseLost("lease_lost")
        return job

    def heartbeat(self, lease, *, lease_seconds=90):
        if not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 3600:
            raise ValueError("invalid_lease_duration")
        repo = self.repository
        with repo.transaction() as connection:
            self._leased(connection, lease)
            expires = repo.clock() + float(lease_seconds)
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(
                lease_expires_at=expires, updated_at=repo.clock()))
            return Lease(lease.job_id, lease.attempt_id, lease.worker_id, lease.fence, expires)

    def release(self, lease, *, retry_after_s=5, error_code=None):
        """Yield a running/reconciliation/collection lease without regenerating."""
        if not math.isfinite(retry_after_s) or not 0 <= retry_after_s <= 86400:
            raise ValueError("invalid_retry_delay")
        if error_code is not None:
            identifier(error_code)
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] not in ("running", "submission_unknown", "collecting", "cancel_requested"):
                raise InvalidTransition("cannot_release_this_phase")
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(
                fence=job["fence"] + 1, lease_worker_id=None, lease_expires_at=None,
                not_before=repo.clock() + retry_after_s, error_code=error_code, updated_at=repo.clock()))
            repo._dispatch_wakeup(connection, lease.job_id)
            return repo._job(connection, lease.job_id)

    def defer_unsubmitted(self, lease, *, retry_after_s=30, error_code="worker_not_ready"):
        """No submission intent exists: safely retry preparation without releasing funds."""
        if not math.isfinite(retry_after_s) or not 0 <= retry_after_s <= 86400:
            raise ValueError("invalid_retry_delay")
        identifier(error_code)
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] != "claimed":
                raise InvalidTransition("submission_already_intended")
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(status="queued",
                fence=job["fence"] + 1, lease_worker_id=None, lease_expires_at=None,
                not_before=repo.clock() + retry_after_s, error_code=error_code, updated_at=repo.clock()))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id)
                .values(status="deferred", error_code=error_code, updated_at=repo.clock()))
            repo._emit(connection, "job.queued", lease.job_id, {"job_id": lease.job_id})
            return repo._job(connection, lease.job_id)

    def _transition(self, lease, allowed, status, *, attempt_values=None, job_values=None):
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] not in allowed:
                raise InvalidTransition("invalid_job_transition")
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id)
                .values(status=status, updated_at=repo.clock(), **(job_values or {})))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id)
                .values(status=status, updated_at=repo.clock(), **(attempt_values or {})))
            repo._emit(connection, "job." + status, lease.job_id, {"job_id": lease.job_id})
            return repo._job(connection, lease.job_id)

    def begin_submission(self, lease):
        """Commit this BEFORE a non-idempotent upstream POST; a second call is rejected."""
        return self._transition(lease, {"claimed"}, "submitting",
                                attempt_values={"submission_started_at": self.repository.clock()})

    def mark_submission_unknown(self, lease, *, error_code="submission_response_unknown"):
        identifier(error_code)
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] not in ("submitting", "cancel_requested"):
                raise InvalidTransition("invalid_job_transition")
            status = "cancel_requested" if job["status"] == "cancel_requested" else "submission_unknown"
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(
                status=status, error_code=error_code, cancel_from_status="submission_unknown" if status == "cancel_requested" else None,
                fence=job["fence"] + 1, lease_worker_id=None, lease_expires_at=None, updated_at=repo.clock()))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id)
                .values(status="submission_unknown", error_code=error_code, updated_at=repo.clock()))
            repo._emit(connection, "job.submission_unknown", lease.job_id, {"job_id": lease.job_id})
            return repo._job(connection, lease.job_id)

    def record_submitted(self, lease, upstream_task_id):
        """Persist an accepted task or reconciled proof. Does not submit anything."""
        identifier(upstream_task_id)
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] not in ("submitting", "submission_unknown", "cancel_requested"):
                raise InvalidTransition("invalid_job_transition")
            attempt = connection.execute(select(attempts).where(attempts.c.id == lease.attempt_id)).mappings().one()
            if attempt["submission_started_at"] is None:
                raise InvalidTransition("missing_submission_intent")
            if attempt["upstream_task_id"] not in (None, upstream_task_id):
                raise Conflict("upstream_task_conflict")
            status = "cancel_requested" if job["status"] == "cancel_requested" else "running"
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(status=status,
                cancel_from_status="running" if status == "cancel_requested" else None, updated_at=repo.clock()))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id)
                .values(status="running", upstream_task_id=upstream_task_id, updated_at=repo.clock()))
            repo._emit(connection, "job.running", lease.job_id, {"job_id": lease.job_id})
            return repo._job(connection, lease.job_id)

    def begin_collection(self, lease):
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] not in ("running", "submission_unknown", "cancel_requested"):
                raise InvalidTransition("invalid_job_transition")
            attempt = connection.execute(select(attempts).where(attempts.c.id == lease.attempt_id)).mappings().one()
            if not attempt["upstream_task_id"]:
                raise InvalidTransition("missing_upstream_task")
            status = "cancel_requested" if job["status"] == "cancel_requested" else "collecting"
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(status=status,
                cancel_from_status="collecting" if status == "cancel_requested" else None, updated_at=repo.clock()))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id)
                .values(status="collecting", upstream_stopped=1, updated_at=repo.clock()))
            repo._emit(connection, "job.collecting", lease.job_id, {"job_id": lease.job_id})
            return repo._job(connection, lease.job_id)

    def collection_failed(self, lease, *, error_code="collection_failed", retry_after_s=30):
        identifier(error_code)
        if not math.isfinite(retry_after_s) or not 0 <= retry_after_s <= 86400:
            raise ValueError("invalid_retry_delay")
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] != "collecting" and not (
                job["status"] == "cancel_requested" and job["cancel_from_status"] == "collecting"):
                raise InvalidTransition("not_collecting")
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(
                error_code=error_code, not_before=repo.clock() + retry_after_s,
                fence=job["fence"] + 1, lease_worker_id=None, lease_expires_at=None, updated_at=repo.clock()))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id).values(
                collection_failures=attempts.c.collection_failures + 1, error_code=error_code, updated_at=repo.clock()))
            repo._dispatch_wakeup(connection, lease.job_id)
            return repo._job(connection, lease.job_id)

    def complete(self, lease, artifact_specs, *, actual_cost_microusd, settlement=None):
        """Complete only AFTER controlled storage and media verification.

        Specs contain kind/object_key/size_bytes/sha256/validated=True, never signed
        URLs. Validation happens in the collector; this verifies its evidence fields.
        Late completion after cancel remains a succeeded, billable result with a
        cancellation marker, rather than claiming the upstream was stopped in time.
        Optional settlement(connection, specs) atomically attributes existing
        storage reservations; it must perform only database work, never IO.
        """
        cost = None if actual_cost_microusd is None else money(actual_cost_microusd)
        specs = canonical(artifact_specs)
        from .inference.outputs import NATIVE_EVIDENCE_FIELDS, validate_delivery_evidence
        if not isinstance(specs, list) or not specs:
            raise ValueError("missing_artifacts")
        for spec in specs:
            if not isinstance(spec, dict) or spec.get("validated") is not True:
                raise ValueError("artifact_not_validated")
            allowed_keys = {"kind", "object_key", "size_bytes", "sha256", "validated", "content_type",
                            "width", "height", "duration_s", "fps", "has_audio"} | NATIVE_EVIDENCE_FIELDS
            if set(spec) - allowed_keys:
                raise ValueError("artifact_metadata_not_allowed")
            identifier(spec.get("kind"))
            key = spec.get("object_key", "")
            digest = spec.get("sha256", "")
            if (not isinstance(key, str) or not key or "://" in key or key.startswith(("/", "\\"))
                or "\\" in key or ".." in key.split("/") or "?" in key or "#" in key
                or type(spec.get("size_bytes")) is not int or spec["size_bytes"] <= 0
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
                raise ValueError("invalid_artifact_evidence")
        repo = self.repository
        with repo.transaction() as connection:
            # Match the content-cleanup lock order: owner fence, then job lock.
            # The unlocked read identifies the owner only; _leased revalidates
            # the original lease after we hold the fence.
            from .content_lifecycle import owner_reference_gate, assert_content_available, assert_reference_payload
            owner = repo._job(connection, lease.job_id)
            owner_reference_gate(connection, owner["tenant_id"], owner["owner_id"])
            job = self._leased(connection, lease)
            if job["status"] != "collecting" and not (
                job["status"] == "cancel_requested" and job["cancel_from_status"] == "collecting"):
                raise InvalidTransition("not_collecting")
            for spec in specs:
                validate_delivery_evidence(job, spec, spec["kind"])
            assert_content_available(connection, job["tenant_id"], job["owner_id"],
                object_keys=[spec["object_key"] for spec in specs])
            assert_reference_payload(connection, job["tenant_id"], job["owner_id"], specs)
            if settlement is not None:
                settlement(connection, specs)
            artifact_ids = []
            for spec in specs:
                artifact_id = str(uuid.uuid4())
                artifact_ids.append(artifact_id)
                connection.execute(insert(artifacts).values(id=artifact_id, job_id=lease.job_id,
                    attempt_id=lease.attempt_id, metadata=spec, created_at=repo.clock()))
            result = {"artifact_ids": artifact_ids, "actual_cost_microusd": cost,
                      "billing_status": "pending" if cost is None else "settled",
                      "completed_after_cancel_request": job["status"] == "cancel_requested"}
            if cost is not None:
                repo._settle(connection, "job", lease.job_id, cost)
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(status="succeeded",
                result=result, error_code=None, fence=job["fence"] + 1,
                lease_worker_id=None, lease_expires_at=None, updated_at=repo.clock()))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id).values(
                status="succeeded", upstream_stopped=1, actual_cost_microusd=cost, updated_at=repo.clock()))
            repo._emit(connection, "job.succeeded", lease.job_id, {"job_id": lease.job_id, "artifact_ids": artifact_ids})
            return repo._job(connection, lease.job_id)

    def fail(self, lease, error_code, *, actual_cost_microusd, upstream_stopped=False, failure_context=None):
        identifier(error_code)
        cost = None if actual_cost_microusd is None else money(actual_cost_microusd)
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] in ("succeeded", "cancelled", "failed"):
                raise InvalidTransition("job_terminal")
            if job["status"] != "claimed" and not upstream_stopped:
                raise Conflict("upstream_still_uncertain")
            if job["status"] == "claimed" and cost != 0:
                raise Conflict("unsubmitted_job_has_cost")
            if cost is not None:
                repo._settle(connection, "job", lease.job_id, cost)
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(status="failed",
                result={"actual_cost_microusd": cost, "billing_status": "pending" if cost is None else "settled"},
                error_code=error_code, fence=job["fence"] + 1, lease_worker_id=None,
                lease_expires_at=None, updated_at=repo.clock()))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id).values(status="failed",
                error_code=error_code, upstream_stopped=1, actual_cost_microusd=cost, updated_at=repo.clock()))
            repo._emit(connection, "job.failed", lease.job_id,
                {"job_id": lease.job_id, "failure_context": failure_context})
            return repo._job(connection, lease.job_id)

    def confirm_cancel(self, lease, *, upstream_stopped=False, actual_cost_microusd=None):
        if not upstream_stopped:
            raise Conflict("cancellation_not_confirmed")
        cost = None if actual_cost_microusd is None else money(actual_cost_microusd)
        repo = self.repository
        with repo.transaction() as connection:
            job = self._leased(connection, lease)
            if job["status"] != "cancel_requested":
                raise InvalidTransition("cancel_not_requested")
            if cost is not None:
                repo._settle(connection, "job", lease.job_id, cost)
            connection.execute(update(jobs).where(jobs.c.id == lease.job_id).values(status="cancelled",
                result={"actual_cost_microusd": cost, "billing_status": "pending" if cost is None else "settled"},
                fence=job["fence"] + 1, lease_worker_id=None, lease_expires_at=None, updated_at=repo.clock()))
            connection.execute(update(attempts).where(attempts.c.id == lease.attempt_id).values(status="cancelled",
                upstream_stopped=1, actual_cost_microusd=cost, updated_at=repo.clock()))
            repo._emit(connection, "job.cancelled", lease.job_id, {"job_id": lease.job_id})
            return repo._job(connection, lease.job_id)

    def recover_expired(self, *, limit=100, summary=False):
        """Fence old workers; only a proven unsubmitted claim returns to generate.

        summary=True returns scalar recovery facts, without loading prompt/source
        snapshots. The worker ignores these details and uses that bounded path;
        default complete job records preserve the existing trusted Python API.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_limit")
        if type(summary) is not bool:
            raise ValueError("invalid_recovery_summary_option")
        repo = self.repository
        recovered = []
        with repo.transaction() as connection:
            expired = list(connection.execute(select(jobs.c.id, jobs.c.status, jobs.c.fence,
                jobs.c.cancel_from_status, jobs.c.current_attempt_id,
                attempts.c.id.label("_previous_attempt_id"),
                attempts.c.submission_started_at.label("_previous_submission_started"),
                attempts.c.upstream_task_id.label("_previous_upstream_task_id"))
                .outerjoin(attempts, (attempts.c.id == jobs.c.current_attempt_id) & (attempts.c.job_id == jobs.c.id))
                .where(jobs.c.lease_worker_id.is_not(None),
                jobs.c.lease_expires_at <= repo.clock()).order_by(jobs.c.lease_expires_at)
                .limit(limit).with_for_update(skip_locked=True, of=jobs)).mappings())
            for job in expired:
                status = {"claimed": "queued", "submitting": "submission_unknown"}.get(job["status"], job["status"])
                error = "worker_lease_expired"
                if job["status"] == "claimed":
                    if job["_previous_attempt_id"] is None:
                        status, error = "recovery_hold", "worker_attempt_evidence_missing"
                    elif job["_previous_submission_started"] is not None or job["_previous_upstream_task_id"] is not None:
                        status, error = "submission_unknown", "worker_submission_evidence_requires_reconciliation"
                values = dict(status=status, fence=job["fence"] + 1, lease_worker_id=None,
                              lease_expires_at=None, updated_at=repo.clock(), error_code=error)
                if job["status"] == "cancel_requested" and job["cancel_from_status"] == "submitting":
                    values["cancel_from_status"] = "submission_unknown"
                connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(**values))
                if job["status"] in ("claimed", "submitting"):
                    connection.execute(update(attempts).where(attempts.c.id == job["current_attempt_id"])
                        .values(status="lease_expired" if status == "queued" else status,
                                error_code=error, updated_at=repo.clock()))
                repo._emit(connection, "job.lease_expired", job["id"], {"job_id": job["id"], "status": status})
                recovered.append({**{key: value for key, value in job.items() if not key.startswith("_previous_")},
                                  **values} if summary else repo._job(connection, job["id"]))
        return recovered

    def get_attempt(self, scope, job_id):
        repo = self.repository
        with repo.engine.connect() as connection:
            job = repo._job(connection, job_id, scope)
            if not job["current_attempt_id"]:
                return None
            row = connection.execute(select(attempts).where(attempts.c.id == job["current_attempt_id"])).mappings().one()
            return dict(row)
