"""Durable GPU-slot ownership. Trusted registration only; no provider mutations.

Expiration means UNKNOWN, never idle. Device ownership is retained until an
operator reconciles the current upstream attempt or confirms retirement. A TP2
worker owns two physical devices but executes only one job at a time.
"""
from dataclasses import asdict, dataclass
import math
import re

from sqlalchemy import and_, case, func, insert, inspect, select, update

from .queue import TaskQueue
from .inference.outputs import validate_delivery_policy
from .repository import (
    BudgetExceeded, Conflict, NotFound, attempts, canonical, identifier, jobs,
    cpu_slots, instance_intents, registered_devices, registered_workers, request_hash, capacity_approvals,
)


TERMINAL = {"succeeded", "failed", "cancelled"}
REAL_GPU_BACKENDS = frozenset({"comfy-worker", "wangp-worker"})


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    pool: str
    provider: str
    instance_id: str
    physical_gpu_ids: tuple[str, ...]
    recipe_ids: tuple[str, ...]
    model_id: str
    configuration_id: str
    backend: str = "comfy-worker"
    engine_manifest_digest: str = ""
    output_delivery: str = ""
    dispatch_backend: str = "legacy"

    def __post_init__(self):
        if self.dispatch_backend not in {"legacy", "hatchet-v1"}:
            raise ValueError("invalid_dispatch_backend")
        validate_delivery_policy(self.backend, self.output_delivery)
        for value in (self.worker_id, self.pool, self.provider, self.instance_id, self.model_id, self.configuration_id):
            identifier(value)
        if self.backend not in {"mock", "cpu-render", *REAL_GPU_BACKENDS}:
            raise ValueError("invalid_worker_backend")
        if self.backend == "wangp-worker":
            if not isinstance(self.engine_manifest_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.engine_manifest_digest):
                raise ValueError("explicit_engine_manifest_required")
        elif self.engine_manifest_digest != "":
            raise ValueError("unexpected_engine_manifest")
        if (not isinstance(self.physical_gpu_ids, tuple) or (not self.physical_gpu_ids and self.backend != "cpu-render")
            or len(set(self.physical_gpu_ids)) != len(self.physical_gpu_ids)
            or len(self.physical_gpu_ids) > 8):
            raise ValueError("invalid_physical_gpu_ids")
        for gpu_id in self.physical_gpu_ids:
            identifier(gpu_id)
        if (not isinstance(self.recipe_ids, tuple) or len(self.recipe_ids) > 32
            or len(set(self.recipe_ids)) != len(self.recipe_ids)):
            raise ValueError("invalid_recipe_bindings")
        if self.backend == "mock":
            if self.provider != "mock" or self.model_id != "SIMULATION":
                raise ValueError("simulation_identity_required")
        elif self.backend == "cpu-render":
            if (self.provider != "local-cpu" or self.physical_gpu_ids or self.model_id != "sixnine-chapter-roughcut-v1"
                or self.configuration_id not in {"cpu-render-v1", "cpu-render-v2", "cpu-render-v3"}
                or self.recipe_ids != ("chapter-roughcut-v1",)):
                raise ValueError("cpu_render_identity_required")
        elif self.provider in {"mock", "local-cpu"} or not self.recipe_ids:
            raise ValueError("real_worker_requires_recipe_bindings")
        for recipe in self.recipe_ids:
            identifier(recipe)


def worker_spec_payload(spec):
    """Keep historical registration hashes unchanged; bind new engines explicitly."""
    value = asdict(spec)
    if spec.backend != "wangp-worker":
        value.pop("engine_manifest_digest")
    if not spec.output_delivery:
        value.pop("output_delivery")
    # Preserve all historical registration hashes for the original route.
    if spec.dispatch_backend == "legacy":
        value.pop("dispatch_backend")
    return canonical(value)


def require_delivery_configuration(connection, backend, configuration_id, output_delivery):
    """A configuration's recorded delivery identity cannot be reused or upgraded.

    Historical registrations, immutable approvals and accepted jobs all matter,
    including expired/revoked/terminal rows. An old bootstrap need not have
    registered yet. Writers must hold the shared global capacity lock before
    checking and recording the binding. IDs remain opaque.
    """
    rows = connection.execute(select(registered_workers.c.spec).where(
        registered_workers.c.spec["backend"].as_string() == backend,
        registered_workers.c.spec["configuration_id"].as_string() == configuration_id)).scalars()
    if any(row.get("output_delivery", "") != output_delivery for row in rows):
        raise Conflict("configuration_output_delivery_conflict")
    approvals = connection.execute(select(capacity_approvals.c.payload).where(
        capacity_approvals.c.configuration_id == configuration_id,
        func.coalesce(capacity_approvals.c.payload["backend"].as_string(), "comfy-worker") == backend)).scalars()
    if any(row.get("output_delivery", "") != output_delivery for row in approvals):
        raise Conflict("configuration_output_delivery_conflict")
    accepted = connection.execute(select(jobs.c.execution_plan).where(
        jobs.c.execution_plan["backend"].as_string() == backend,
        jobs.c.execution_plan["configuration_id"].as_string() == configuration_id)).scalars()
    if any(row.get("output_delivery", "") != output_delivery for row in accepted):
        raise Conflict("configuration_output_delivery_conflict")


class WorkerControl:
    def __init__(self, repository, *, registration_seconds=120, capacity_usage=None):
        if not math.isfinite(registration_seconds) or registration_seconds <= 0:
            raise ValueError("invalid_registration_lease")
        self.repo = repository
        self.queue = TaskQueue(repository)
        self.registration_seconds = registration_seconds
        # Trusted controller-only read, called inside the existing capacity
        # transaction. HTTP callers cannot supply counts or exclusions. The
        # default remains the conservative original-ledger observation.
        if capacity_usage is not None and not callable(capacity_usage):
            raise ValueError("invalid_registration_capacity_reader")
        self._capacity_usage = capacity_usage

    def _worker(self, connection, worker_id, *, lock=False):
        statement = select(registered_workers).where(registered_workers.c.id == worker_id)
        row = self.repo._locked(connection, statement) if lock else connection.execute(statement).mappings().first()
        if row is None:
            raise NotFound("worker_not_registered")
        return dict(row)

    def get(self, worker_id):
        with self.repo.engine.connect() as connection:
            return self._worker(connection, worker_id)

    @staticmethod
    def _proven_unsubmitted_queue(connection, job):
        """A status label alone cannot prove this bound slot stopped executing."""
        if job["status"] != "queued" or not job["current_attempt_id"]:
            return False
        previous = connection.execute(select(attempts.c.submission_started_at, attempts.c.upstream_task_id).where(
            attempts.c.id == job["current_attempt_id"], attempts.c.job_id == job["id"])).first()
        return (previous is not None and previous.submission_started_at is None
                and previous.upstream_task_id is None)

    def pool_status(self, pool, *, model_id, configuration_id, recipe_id=None,
                    backend="comfy-worker", engine_manifest_digest=None, output_delivery="",
                    expected_runtime_s=None, deployment_profile_id=None, dispatch_backend="legacy"):
        """Read-only readiness of exact operator-bound slots, not a GPU probe.

        An expired registration is reported as unknown without modifying its
        durable state or releasing physical device ownership. The caller must
        authenticate separately; this is a trusted control-plane query.
        """
        for value in (pool, model_id, configuration_id):
            identifier(value)
        if dispatch_backend not in {"legacy", "hatchet-v1"}:
            raise ValueError("invalid_dispatch_backend")
        validate_delivery_policy(backend, output_delivery)
        if recipe_id is not None:
            identifier(recipe_id)
        if backend not in {"mock", "cpu-render", *REAL_GPU_BACKENDS}:
            raise ValueError("invalid_worker_backend")
        if backend == "wangp-worker" and (not isinstance(engine_manifest_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", engine_manifest_digest)):
            raise ValueError("explicit_engine_manifest_required")
        now = self.repo.clock()
        counts = {state: 0 for state in ("ready", "busy", "unknown", "registered", "draining", "retired")}
        matched = 0
        reasons = {}
        with self.repo.engine.connect() as connection:
            from .operator_capacity import operator_nodes
            stopping = set(connection.execute(select(instance_intents.c.provider, instance_intents.c.provider_instance_id)
                .where((instance_intents.c.state.in_(("draining", "destroying", "destroyed")))
                    | (instance_intents.c.hard_deadline <= now),
                    instance_intents.c.provider_instance_id.is_not(None))).tuples())
            # Catalog enumeration stays SELECT-only on SQLite/PostgreSQL;
            # has_table() uses SQLite PRAGMA, outside the dry-run read contract.
            if operator_nodes.name in inspect(connection).get_table_names():
                stopping.update(connection.execute(select(instance_intents.c.provider, instance_intents.c.provider_instance_id)
                    .join(operator_nodes, operator_nodes.c.intent_id == instance_intents.c.id)
                    .where(operator_nodes.c.desired_state != "running",
                        instance_intents.c.provider_instance_id.is_not(None))).tuples())
            rows = connection.execute(select(registered_workers).where(registered_workers.c.pool == pool)).mappings()
            for row in rows:
                spec = row["spec"]
                if (spec["backend"] != backend or spec["model_id"] != model_id
                    or spec.get("dispatch_backend", "legacy") != dispatch_backend
                    or spec["configuration_id"] != configuration_id
                    or backend == "wangp-worker" and spec.get("engine_manifest_digest") != engine_manifest_digest
                    or spec.get("output_delivery", "") != output_delivery
                    or recipe_id is not None and recipe_id not in spec["recipe_ids"]):
                    continue
                matched += 1
                state = row["state"]
                if state == "retired":
                    observed = "retired"
                elif row["drain_requested"] or (row["provider"], row["instance_id"]) in stopping:
                    observed = "draining"
                elif row["expires_at"] <= now or row["updated_at"] > now or row["spec_hash"] != request_hash(spec):
                    observed = "unknown"
                elif state == "ready" and row["current_job_id"] is None:
                    observed = "ready"
                elif state in ("registered", "draining", "unknown"):
                    observed = state
                else:
                    observed = "busy"
                if observed in {"ready", "busy"}:
                    from .worker_admission import worker_window_reason
                    reason = worker_window_reason(connection, row, now,
                        expected_runtime_s=expected_runtime_s, deployment_profile_id=deployment_profile_id)
                    if reason:
                        observed = "unknown" if reason == "managed_provider_lifetime_unverified" else "draining"
                        reasons[reason] = reasons.get(reason, 0) + 1
                counts[observed] += 1
        return {"pool": pool, "model_id": model_id, "configuration_id": configuration_id,
                "backend": backend, "recipe_id": recipe_id, "observed_at": now,
                "matched_slots": matched, "reason_counts": reasons, **counts}

    def register(self, spec: WorkerSpec):
        """Operator assigns concrete provider/instance/device identities, not users."""
        payload = worker_spec_payload(spec)
        digest = request_hash(payload)
        repo = self.repo
        with repo.transaction() as connection:
            limits = None if spec.backend == "cpu-render" else repo._lock_capacity(connection)
            require_delivery_configuration(connection, spec.backend, spec.configuration_id, spec.output_delivery)
            existing = repo._locked(connection, select(registered_workers).where(registered_workers.c.id == spec.worker_id))
            if existing:
                if existing["spec_hash"] != digest or existing["state"] == "retired":
                    raise Conflict("worker_registration_conflict")
                return dict(existing)
            bindings = list(connection.execute(select(registered_devices).where(
                registered_devices.c.provider == spec.provider,
                registered_devices.c.instance_id == spec.instance_id,
                registered_devices.c.gpu_id.in_(spec.physical_gpu_ids)).with_for_update()).mappings())
            if any(row["state"] != "released" for row in bindings):
                raise Conflict("physical_gpu_already_owned")
            if spec.backend in REAL_GPU_BACKENDS:
                usage = (self._capacity_usage(connection, spec) if self._capacity_usage
                    else repo._global_usage(connection))
                resources = usage.get("resources") if isinstance(usage, dict) else None
                if (not isinstance(resources, dict)
                        or any(not isinstance(key, tuple) or len(key) != 2
                            or any(not isinstance(value, str) or not value for value in key)
                            or type(count) is not int or count < 1 for key, count in resources.items())
                        or type(usage.get("instances")) is not int or usage["instances"] != len(resources)
                        or type(usage.get("physical_gpus")) is not int
                        or usage["physical_gpus"] != sum(resources.values())):
                    raise ValueError("invalid_registration_capacity_observation")
                key = (spec.provider, spec.instance_id)
                existing_count = sum(1 for r in connection.execute(select(registered_devices).where(
                    registered_devices.c.provider == spec.provider,
                    registered_devices.c.instance_id == spec.instance_id,
                    registered_devices.c.state != "released")).mappings())
                proposed_count = max(usage["resources"].get(key, 0), existing_count + len(spec.physical_gpu_ids))
                gpu_total = usage["physical_gpus"] - usage["resources"].get(key, 0) + proposed_count
                instances = usage["instances"] + (0 if key in usage["resources"] else 1)
                if instances > limits["max_instances"] or gpu_total > limits["max_physical_gpus"]:
                    raise BudgetExceeded("global_capacity_exceeded")
            row = dict(id=spec.worker_id, pool=spec.pool, provider=spec.provider, instance_id=spec.instance_id,
                spec=payload, spec_hash=digest, state="registered", current_job_id=None, fence=0,
                drain_requested=0,
                expires_at=repo.clock()+self.registration_seconds, updated_at=repo.clock())
            connection.execute(insert(registered_workers).values(**row))
            if spec.backend == "cpu-render":
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                put = sqlite_insert if repo.engine.dialect.name == "sqlite" else pg_insert
                connection.execute(put(cpu_slots).values(instance_id=spec.instance_id, worker_id=spec.worker_id, state="owned")
                    .on_conflict_do_nothing(index_elements=["instance_id"]))
                owned = repo._locked(connection, select(cpu_slots).where(cpu_slots.c.instance_id == spec.instance_id))
                if owned["state"] != "released" and owned["worker_id"] != spec.worker_id:
                    raise Conflict("cpu_instance_already_owned")
                connection.execute(update(cpu_slots).where(cpu_slots.c.instance_id == spec.instance_id)
                    .values(worker_id=spec.worker_id, state="owned"))
            bound = {r["gpu_id"] for r in bindings}
            for gpu_id in spec.physical_gpu_ids:
                values = dict(worker_id=spec.worker_id, state="owned")
                if gpu_id in bound:
                    connection.execute(update(registered_devices).where(registered_devices.c.provider == spec.provider,
                        registered_devices.c.instance_id == spec.instance_id, registered_devices.c.gpu_id == gpu_id).values(**values))
                else:
                    connection.execute(insert(registered_devices).values(provider=spec.provider,
                        instance_id=spec.instance_id, gpu_id=gpu_id, **values))
            return row

    def require_recovery_binding(self, spec):
        """Validate an existing obligation without registering/reviving a slot."""
        if spec.backend not in REAL_GPU_BACKENDS:
            raise Conflict("recovery_requires_real_engine")
        worker = self.get(spec.worker_id)
        if (worker["spec_hash"] != request_hash(worker_spec_payload(spec))
                or worker["state"] == "retired" or not worker["current_job_id"]):
            raise Conflict("recovery_worker_binding_required")
        with self.repo.engine.connect() as connection:
            job = self.repo._job(connection, worker["current_job_id"])
            if not self.matches(worker, job):
                raise Conflict("recovery_worker_binding_conflict")
        return worker

    def mark_ready(self, worker_id, *, upstream_idle_confirmed=False):
        if not upstream_idle_confirmed:
            raise Conflict("worker_readiness_not_confirmed")
        with self.repo.transaction() as connection:
            worker = self._worker(connection, worker_id, lock=True)
            if worker["state"] == "retired":
                raise Conflict("worker_not_admitting")
            if connection.execute(select(instance_intents.c.id).where(
                instance_intents.c.provider == worker["provider"],
                instance_intents.c.provider_instance_id == worker["instance_id"],
                instance_intents.c.state.in_(("draining", "destroying", "destroyed")))).first():
                raise Conflict("worker_instance_not_admitting")
            if worker["current_job_id"]:
                job = self.repo._job(connection, worker["current_job_id"], lock=True)
                safe_unsubmitted = self._proven_unsubmitted_queue(connection, job)
                if job["status"] not in TERMINAL and not safe_unsubmitted:
                    raise Conflict("current_attempt_still_unresolved")
            connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id).values(
                state="ready", current_job_id=None, drain_requested=0, expires_at=self.repo.clock()+self.registration_seconds,
                fence=worker["fence"]+1, updated_at=self.repo.clock()))
            return self._worker(connection, worker_id)

    @staticmethod
    def matches(worker, job):
        spec = worker["spec"]
        execution = job["execution_plan"]
        request = job["request"]
        if not isinstance(execution, dict) or not isinstance(request, dict):
            return False
        effective = request.get("request", request)
        if not isinstance(effective, dict):
            return False
        if (job["pool"] != spec["pool"] or execution.get("backend") != spec["backend"]
            or execution.get("dispatch_backend", "legacy") != spec.get("dispatch_backend", "legacy")
            or execution.get("enabled") is not True
            or execution.get("output_delivery", "") != spec.get("output_delivery", "")):
            return False
        if spec["recipe_ids"] and request.get("recipe_id") not in spec["recipe_ids"]:
            return False
        if spec["backend"] == "mock":
            return spec["provider"] == "mock" and spec["model_id"] == "SIMULATION"
        if spec["backend"] == "wangp-worker" and execution.get("engine_manifest_digest") != spec.get("engine_manifest_digest"):
            return False
        return (effective.get("model") == spec["model_id"]
                and execution.get("configuration_id") == spec["configuration_id"])

    def claim(self, worker_id, pool, *, purpose="generate", lease_seconds=90, job_filter=None, job_allowed=None,
              selected_job_id=None, selected_job_allowed=None):
        """Claim and bind slot atomically in the same ledger transaction."""
        with self.repo.transaction() as connection:
            worker = self._worker(connection, worker_id, lock=True)
            if worker["pool"] != pool or worker["state"] in ("registered", "retired"):
                return None
            if worker["expires_at"] <= self.repo.clock():
                connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id)
                    .values(state="unknown", updated_at=self.repo.clock()))
                worker["state"] = "unknown"
            if purpose == "generate" and connection.execute(select(instance_intents.c.id).where(
                    instance_intents.c.provider == worker["provider"],
                    instance_intents.c.provider_instance_id == worker["instance_id"],
                    instance_intents.c.state.in_(("draining", "destroying", "destroyed")))).first():
                # Defensive against a stale worker-ready record. Existing
                # attempts still reconcile/collect, but a stopping host cannot
                # be revived by an idle observation or take another job.
                connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id).values(
                    drain_requested=1, state="unknown" if worker["state"] == "unknown" else "draining",
                    updated_at=self.repo.clock()))
                return None
            if purpose == "generate" and (worker["state"] != "ready" or worker["current_job_id"] or worker["drain_requested"]):
                return None
            if purpose != "generate" and not worker["current_job_id"]:
                return None
            spec = worker["spec"]
            bindings = [jobs.c.pool == pool,
                func.coalesce(jobs.c.execution_plan["dispatch_backend"].as_string(), "legacy")
                    == spec.get("dispatch_backend", "legacy"),
                jobs.c.execution_plan["backend"].as_string() == spec["backend"],
                # JSON boolean extraction is text on PG and integer on SQLite.
                # Avoid casting arbitrary legacy strings to PG BOOLEAN, which
                # would fail the whole queue instead of excluding that job.
                jobs.c.execution_plan["enabled"].as_string() == ("true" if self.repo.engine.dialect.name == "postgresql" else 1)]
            if worker["current_job_id"]:
                bindings.append(jobs.c.id == worker["current_job_id"])
            if spec["recipe_ids"]:
                bindings.append(jobs.c.request["recipe_id"].as_string().in_(spec["recipe_ids"]))
            if spec["backend"] != "mock":
                # Preserve nested request precedence rather than coalescing a
                # missing nested model with an unrelated outer display field.
                model = case((jobs.c.request["request"].as_string().is_not(None),
                    jobs.c.request["request"]["model"].as_string()), else_=jobs.c.request["model"].as_string())
                bindings.extend((model == spec["model_id"],
                    jobs.c.execution_plan["configuration_id"].as_string() == spec["configuration_id"]))
            if spec["backend"] == "wangp-worker":
                bindings.append(jobs.c.execution_plan["engine_manifest_digest"].as_string() == spec["engine_manifest_digest"])
            # Do not claim unsupported jobs then fail them: old slot identities
            # cannot acquire native-delivery work, including collection/recovery.
            bindings.append(func.coalesce(jobs.c.execution_plan["output_delivery"].as_string(), "")
                            == spec.get("output_delivery", ""))
            if purpose == "generate":
                deadlines = list(connection.execute(select(instance_intents.c.hard_deadline).where(
                    instance_intents.c.provider == worker["provider"],
                    instance_intents.c.provider_instance_id == worker["instance_id"],
                    instance_intents.c.state != "destroyed",
                    instance_intents.c.hard_deadline.is_not(None))).scalars())
                if deadlines:
                    # A shorter job may fit even when a long queued job does
                    # not. Existing attempts still reconcile/collect past this
                    # gate; this must never cause another paid submission.
                    from .worker_admission import COMPLETION_MARGIN_SECONDS
                    remaining = min(deadlines)-self.repo.clock()-COMPLETION_MARGIN_SECONDS
                    bindings.append(jobs.c.expected_runtime_s < remaining)
            if job_filter is not None:
                bindings.append(job_filter)
            from .capacity import capacity_member_claim_allowed
            from .worker_admission import worker_window_reason
            claim = self.queue.claim(worker_id, pool, purpose=purpose, lease_seconds=lease_seconds,
                dispatch_backend=spec.get("dispatch_backend", "legacy"),
                selected_job_id=selected_job_id, selected_validator=selected_job_allowed,
                connection=connection, job_filter=and_(*bindings), validator=lambda job: self.matches(worker, job)
                    and (purpose != "generate" or capacity_member_claim_allowed(self.repo, connection, job, worker))
                    and (purpose != "generate" or worker_window_reason(connection, worker, self.repo.clock(),
                        expected_runtime_s=job["expected_runtime_s"],
                        deployment_profile_id=job["request"].get("deployment_profile_id")) is None)
                    and (job_allowed is None or job_allowed(job) is True))
            if claim is None:
                return None
            connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id).values(
                state="draining" if worker["drain_requested"] else "leased" if purpose == "generate" else "reconciling",
                current_job_id=claim.job["id"],
                fence=worker["fence"]+1, expires_at=self.repo.clock()+lease_seconds, updated_at=self.repo.clock()))
            return claim

    def observe(self, worker_id, job_id, *, quarantine_failures=False):
        """Read committed ledger facts after a turn; a running/unknown task holds its slot."""
        if type(quarantine_failures) is not bool:
            raise ValueError("invalid_failure_quarantine_option")
        with self.repo.transaction() as connection:
            worker = self._worker(connection, worker_id, lock=True)
            if worker["current_job_id"] != job_id:
                raise Conflict("worker_job_binding_conflict")
            job = self.repo._job(connection, job_id, lock=True)
            terminal = job["status"] in TERMINAL
            # A genuine deferred preparation has no submission intent. An
            # erroneous/manual requeue still holds its slot and cost evidence.
            unsafe_queued = False
            if job["status"] == "queued":
                terminal = self._proven_unsubmitted_queue(connection, job)
                unsafe_queued = not terminal
            state = "ready" if terminal else "unknown" if unsafe_queued or job["status"] in ("submission_unknown", "recovery_hold") else "busy"
            # Opt-in queued-task qualification must never briefly advertise a
            # failing runtime as ready between separate observe/drain writes.
            # Unknown submissions retain their binding for reconciliation;
            # cancellation by the user is not evidence of a broken runtime.
            quarantined = quarantine_failures and (job["status"] == "failed"
                or job["status"] == "queued" and job.get("error_code") in {
                    "worker_preparation_not_ready", "worker_preparation_failed"}
                or job["status"] == "collecting" and job.get("error_code") == "collection_failed")
            draining = bool(worker["drain_requested"] or quarantined)
            if draining:
                state = "draining"
            connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id).values(
                state=state, current_job_id=None if terminal else job_id,
                drain_requested=int(draining),
                expires_at=self.repo.clock()+self.registration_seconds, updated_at=self.repo.clock()))
            return self._worker(connection, worker_id)

    def submission_allowed(self, job):
        """Recheck slot/TTL immediately before POST, after slow media uploads."""
        worker_id = job.get("lease_worker_id")
        if not worker_id:
            return False
        with self.repo.engine.connect() as connection:
            worker = self._worker(connection, worker_id)
            now = self.repo.clock()
            if (worker["current_job_id"] != job["id"] or worker["drain_requested"]
                    or worker["expires_at"] <= now or worker["state"] not in {"leased", "busy"}):
                return False
            from .worker_admission import worker_window_reason
            return worker_window_reason(connection, worker, now, expected_runtime_s=job["expected_runtime_s"],
                deployment_profile_id=job["request"].get("deployment_profile_id")) is None

    def queued_diagnostic(self, job):
        """Owner-facing advisory of this accepted job, with no scheduler writes."""
        now = self.repo.clock()
        result = {"advisory_only": True, "observed_at": now, "reason_code": "awaiting_worker_claim"}
        if job.get("lease_worker_id") or job.get("current_attempt_id"):
            result["reason_code"] = "original_attempt_requires_reconciliation"
            return result
        if job.get("not_before", 0) > now:
            result.update(reason_code="queue_backoff", retry_after_seconds=math.ceil(job["not_before"]-now))
            return result
        plan, stored = job["execution_plan"], job["request"]
        if not plan.get("enabled"):
            result["reason_code"] = "generation_disabled"
            return result
        request = stored.get("request", stored)
        try:
            counts = self.pool_status(job["pool"], model_id=request["model"],
                configuration_id=plan["configuration_id"], backend=plan["backend"], recipe_id=stored.get("recipe_id"),
                engine_manifest_digest=plan.get("engine_manifest_digest"), output_delivery=plan.get("output_delivery", ""),
                expected_runtime_s=job.get("expected_runtime_s"), deployment_profile_id=stored.get("deployment_profile_id"),
                dispatch_backend=plan.get("dispatch_backend", "legacy"))
        except (KeyError, ValueError):
            result["reason_code"] = "queue_configuration_unconfirmed"
            return result
        result["capacity"] = {key: counts[key] for key in ("ready", "busy", "unknown", "registered", "draining", "matched_slots", "reason_counts")}
        if counts["ready"]:
            # A read cannot prove controller liveness or reserve the next claim.
            result["reason_code"] = "awaiting_worker_claim"
        elif counts["busy"]:
            result["reason_code"] = "matching_worker_busy"
        elif counts["reason_counts"]:
            result["reason_code"] = sorted(counts["reason_counts"])[0]
        elif counts["unknown"]:
            result["reason_code"] = "worker_readiness_unconfirmed"
        elif counts["registered"]:
            result["reason_code"] = "matching_worker_starting"
        else:
            result["reason_code"] = "matching_capacity_unavailable"
        return result

    def heartbeat(self, worker_id, fence, *, lease_seconds=None):
        seconds = self.registration_seconds if lease_seconds is None else lease_seconds
        if not math.isfinite(seconds) or not 0 < seconds <= 3600:
            raise ValueError("invalid_registration_lease")
        with self.repo.transaction() as connection:
            worker = self._worker(connection, worker_id, lock=True)
            if worker["fence"] != fence or worker["expires_at"] <= self.repo.clock() or worker["state"] in ("unknown", "retired"):
                raise Conflict("worker_registration_lease_lost")
            connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id).values(
                expires_at=self.repo.clock()+seconds, updated_at=self.repo.clock()))
            return self._worker(connection, worker_id)

    def recover_expired(self):
        with self.repo.transaction() as connection:
            rows = list(connection.execute(select(registered_workers).where(
                registered_workers.c.expires_at <= self.repo.clock(),
                registered_workers.c.state.not_in(("unknown", "retired"))).with_for_update(skip_locked=True)).mappings())
            for row in rows:
                connection.execute(update(registered_workers).where(registered_workers.c.id == row["id"])
                    .values(state="unknown", fence=row["fence"]+1, updated_at=self.repo.clock()))
            return [r["id"] for r in rows]

    def drain(self, worker_id):
        with self.repo.transaction() as connection:
            worker = self._worker(connection, worker_id, lock=True)
            if worker["state"] == "retired":
                return worker
            connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id)
                .values(state="draining", drain_requested=1, updated_at=self.repo.clock()))
            return self._worker(connection, worker_id)

    def retire(self, worker_id, *, upstream_idle_confirmed=False):
        if not upstream_idle_confirmed:
            raise Conflict("upstream_still_unconfirmed")
        with self.repo.transaction() as connection:
            # CPU rendering is not a GPU lease and needs no cloud capacity gate.
            peek = self._worker(connection, worker_id)
            if peek["spec"]["backend"] != "cpu-render":
                self.repo._lock_capacity(connection)
            worker = self._worker(connection, worker_id, lock=True)
            if worker["current_job_id"]:
                job = self.repo._job(connection, worker["current_job_id"], lock=True)
                if job["status"] not in TERMINAL and not self._proven_unsubmitted_queue(connection, job):
                    raise Conflict("current_attempt_still_unresolved")
            return self._retire_locked(connection, worker)

    def _retire_locked(self, connection, worker):
        """Transition only; caller owns the capacity/worker locks and stop proof.

        Keep joined retirement checks on the caller's connection: opening a
        second Repository.transaction here can deadlock the capacity row.
        """
        if worker["state"] == "retired":
            return worker
        worker_id = worker["id"]
        connection.execute(update(registered_workers).where(registered_workers.c.id == worker_id).values(
            state="retired", current_job_id=None, fence=worker["fence"]+1, updated_at=self.repo.clock()))
        connection.execute(update(registered_devices).where(registered_devices.c.worker_id == worker_id)
            .values(state="released"))
        connection.execute(update(cpu_slots).where(cpu_slots.c.worker_id == worker_id).values(state="released"))
        return self._worker(connection, worker_id)

    def capacity(self):
        with self.repo.engine.connect() as connection:
            usage = self.repo._global_usage(connection)
            return {key: usage[key] for key in ("instances", "physical_gpus")}
