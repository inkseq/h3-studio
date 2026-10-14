"""Hatchet carries identity-only wakeups; the existing ledger owns execution.

Imports perform no network requests or secret reads. Broker delivery is at least
once. Its timeout/cancellation never proves that the GPU stopped and never
changes a business job into failed/cancelled. Existing fenced attempts, physical
slot ownership, submission intents and artifact receipts remain authoritative.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time
from urllib.parse import urlsplit
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select, update

from .control import WorkerControl
from .drain_safe_runner import collection_slot
from .queue import TaskQueue
from .repository import (Repository, attempts, canonical, dispatch_receipts,
                         identifier, jobs, outbox, registered_workers, request_hash)
from .telemetry import configured_telemetry
from .worker import WorkerRunner

ROUTE = "hatchet-v1"
EVENT = "job.dispatch_requested"
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
RUNNABLE = frozenset({"queued", "running", "submission_unknown", "collecting", "cancel_requested"})


class DispatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(default=1, ge=1, le=1)
    event_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    job_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def route(job):
    value = job["execution_plan"].get("dispatch_backend", "legacy")
    if value not in {"legacy", ROUTE}:
        raise ValueError("invalid_dispatch_backend")
    return value


def binding(job):
    plan = job["execution_plan"]
    stored = job["request"]
    request = stored.get("request", stored)
    # An immutable configuration is the scheduling boundary. The business
    # WorkerControl remains the final engine/model/manifest authority.
    value = {"pool": job["pool"], "configuration_id": plan.get("configuration_id", ""),
        "engine_manifest_digest": plan.get("engine_manifest_digest", ""),
        "deployment_profile_id": stored.get("deployment_profile_id", ""),
        "mode": request.get("mode", ""), "backend": plan.get("backend", "")}
    return canonical(value)


def workflow_name(job):
    return "sixnine-generation-" + request_hash(binding(job))[:24]


def labels(value):
    return {"sixnine_dispatch": ROUTE,
        "sixnine_binding": request_hash(value),
        "sixnine_mode": value["mode"],
        "sixnine_profile": value["deployment_profile_id"]}


def worker_labels(value, worker_id):
    """Registered identity is immutable to its physical slot and manifest."""
    identifier(worker_id)
    return {**labels(value), "sixnine_worker": worker_id}


def _original_worker(connection, job):
    """Recover from the authoritative attempt, even after its job lease clears."""
    if not job["current_attempt_id"]:
        if job["status"] == "queued" and job["lease_worker_id"] is None:
            return None
        raise ValueError("hatchet_original_attempt_unconfirmed")
    attempt = connection.execute(select(attempts).where(
        attempts.c.id == job["current_attempt_id"], attempts.c.job_id == job["id"])).mappings().first()
    if attempt is None:
        raise ValueError("hatchet_original_attempt_unconfirmed")
    if (job["status"] == "queued" and job["lease_worker_id"] is None
            and attempt["submission_started_at"] is None and attempt["upstream_task_id"] is None):
        # Original defer_unsubmitted/recover_expired proof permits another
        # preparation claim. No inference intent exists to recover or repeat.
        return None
    worker = connection.execute(select(registered_workers).where(
        registered_workers.c.id == attempt["worker_id"])).mappings().first()
    if (worker is None or worker["current_job_id"] != job["id"]
            or worker["state"] == "retired" or not WorkerControl.matches(worker, job)
            or job["lease_worker_id"] not in (None, worker["id"])):
        raise ValueError("hatchet_original_worker_unconfirmed")
    return worker["id"]


def _input_matches(job, message):
    return (route(job) == ROUTE and job["id"] == message.job_id
        and job["request_hash"] == message.request_hash
        and request_hash(job["execution_plan"]) == message.plan_hash)


class OutboxDispatcher:
    """A leased delivery receipt is not a second business task state machine."""
    def __init__(self, repository, publisher, *, publisher_id, lease_seconds=90, retry_seconds=30):
        identifier(publisher_id)
        if (not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600
                or not math.isfinite(retry_seconds) or not 1 <= retry_seconds <= 3600):
            raise ValueError("invalid_dispatch_delivery_policy")
        self.repo, self.publisher = repository, publisher
        self.publisher_id, self.lease_seconds, self.retry_seconds = publisher_id, lease_seconds, retry_seconds
        self._recovery_cursor = None

    def _claim_delivery(self):
        with self.repo.transaction() as connection:
            # A lease serializes concurrent CPU publishers. A lost HTTP/gRPC
            # response is retried with the same event key, never a new job.
            row = connection.execute(select(dispatch_receipts, outbox.c.payload)
                .join(outbox, outbox.c.id == dispatch_receipts.c.event_id)
                .where(outbox.c.event_type == EVENT, outbox.c.delivered_at.is_(None),
                    dispatch_receipts.c.not_before <= self.repo.clock(),
                    ((dispatch_receipts.c.lease_expires_at.is_(None)) |
                     (dispatch_receipts.c.lease_expires_at <= self.repo.clock())))
                .order_by(outbox.c.created_at, outbox.c.id).limit(1)
                .with_for_update(skip_locked=True, of=dispatch_receipts)).mappings().first()
            if row is None:
                return None
            job = self.repo._job(connection, row["job_id"], lock=True)
            message = DispatchInput(event_id=row["event_id"], **row["payload"])
            if not _input_matches(job, message):
                connection.execute(update(dispatch_receipts).where(dispatch_receipts.c.event_id == row["event_id"])
                    .values(state="blocked", not_before=self.repo.clock()+3600,
                        error_code="dispatch_identity_mismatch", updated_at=self.repo.clock()))
                return {"state": "blocked", "event_id": row["event_id"]}
            if job["status"] in TERMINAL:
                connection.execute(update(outbox).where(outbox.c.id == row["event_id"])
                    .values(delivered_at=self.repo.clock()))
                connection.execute(update(dispatch_receipts).where(dispatch_receipts.c.event_id == row["event_id"])
                    .values(state="obsolete", updated_at=self.repo.clock()))
                return {"state": "obsolete", "event_id": row["event_id"]}
            connection.execute(update(dispatch_receipts).where(dispatch_receipts.c.event_id == row["event_id"])
                .values(state="publishing", publisher_id=self.publisher_id,
                    lease_expires_at=self.repo.clock()+self.lease_seconds,
                    publish_attempts=dispatch_receipts.c.publish_attempts+1, updated_at=self.repo.clock()))
            return {"state": "publishing", "message": message, "job": job,
                "attempt": row["publish_attempts"]+1}

    def publish_once(self):
        item = self._claim_delivery()
        if item is None:
            return {"state": "idle"}
        if item["state"] != "publishing":
            return item
        message = item["message"]
        try:
            external_run_id = self.publisher.publish(message, item["job"])
            identifier(external_run_id)
        except Exception:
            # Never persist raw broker responses; they can contain credentials.
            with self.repo.transaction() as connection:
                connection.execute(update(dispatch_receipts).where(
                    dispatch_receipts.c.event_id == message.event_id,
                    dispatch_receipts.c.publisher_id == self.publisher_id,
                    dispatch_receipts.c.publish_attempts == item["attempt"])
                    .values(state="unknown", lease_expires_at=None,
                        not_before=self.repo.clock()+self.retry_seconds,
                        error_code="dispatch_response_unknown", updated_at=self.repo.clock()))
            return {"state": "unknown", "event_id": message.event_id, "job_id": message.job_id}
        with self.repo.transaction() as connection:
            receipt = self.repo._locked(connection, select(dispatch_receipts).where(
                dispatch_receipts.c.event_id == message.event_id))
            if (receipt["publisher_id"] != self.publisher_id or receipt["publish_attempts"] != item["attempt"]):
                return {"state": "delivery_lease_lost", "event_id": message.event_id}
            connection.execute(update(dispatch_receipts).where(dispatch_receipts.c.event_id == message.event_id)
                .values(state="published", external_run_id=external_run_id, lease_expires_at=None,
                    error_code=None, updated_at=self.repo.clock()))
            connection.execute(update(outbox).where(outbox.c.id == message.event_id,
                outbox.c.event_type == EVENT, outbox.c.delivered_at.is_(None)).values(delivered_at=self.repo.clock()))
        return {"state": "published", "job_id": message.job_id, "event_id": message.event_id,
            "external_run_id": external_run_id}

    def recover_wakeups(self, *, limit=100):
        """A broker run ending is a wakeup condition, never GPU stop evidence."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_dispatch_recovery_limit")
        TaskQueue(self.repo).recover_expired(limit=limit, summary=True)
        # Cursor through the ledger rather than repeatedly scanning its oldest
        # active deliveries. A running prefix must not starve later recovery.
        query = select(jobs.c.id, jobs.c.updated_at).where(
            jobs.c.status.in_(RUNNABLE), jobs.c.lease_worker_id.is_(None),
            jobs.c.not_before <= self.repo.clock(),
            jobs.c.execution_plan["dispatch_backend"].as_string() == ROUTE)
        def page(connection, cursor):
            selected = query
            if cursor:
                updated, job_id = cursor
                selected = selected.where(or_(jobs.c.updated_at > updated,
                    and_(jobs.c.updated_at == updated, jobs.c.id > job_id)))
            return list(connection.execute(selected.order_by(jobs.c.updated_at, jobs.c.id).limit(limit)).mappings())
        with self.repo.engine.connect() as connection:
            rows = page(connection, self._recovery_cursor)
            if not rows and self._recovery_cursor:
                rows = page(connection, None)
            self._recovery_cursor = (rows[-1]["updated_at"], rows[-1]["id"]) if rows else None
            ids = [row["id"] for row in rows]
        count = 0
        for job_id in ids:
            with self.repo.engine.connect() as connection:
                latest = connection.execute(select(dispatch_receipts)
                    .where(dispatch_receipts.c.job_id == job_id)
                    .order_by(dispatch_receipts.c.sequence.desc())
                    .limit(1)).mappings().first()
            if latest:
                if latest["state"] != "published":
                    continue
                try:
                    # Unknown broker state blocks another wakeup. It does not
                    # erase the accepted job, slot or outstanding reservation.
                    status = self.publisher.status(latest["external_run_id"])
                except Exception:
                    continue
                if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
                    continue
            with self.repo.transaction() as connection:
                job = self.repo._job(connection, job_id, lock=True)
                if (job["status"] in RUNNABLE and job["lease_worker_id"] is None
                        and job["not_before"] <= self.repo.clock() and route(job) == ROUTE):
                    if latest:
                        connection.execute(update(dispatch_receipts).where(
                            dispatch_receipts.c.event_id == latest["event_id"],
                            dispatch_receipts.c.state == "published").values(state="finished", updated_at=self.repo.clock()))
                    self.repo._dispatch_wakeup(connection, job_id)
                    count += 1
        return {"state": "recovered", "wakeups": count}


class ExactJobRunner(WorkerRunner):
    def __init__(self, *args, message, collection_lock_dir, **kwargs):
        self.message = message if isinstance(message, DispatchInput) else DispatchInput.model_validate(message)
        self.collection_lock_dir = Path(collection_lock_dir)
        if not self.collection_lock_dir.is_absolute():
            raise ValueError("absolute_shared_collection_lock_required")
        super().__init__(*args, **kwargs)
        if self.control is None:
            raise ValueError("hatchet_requires_durable_worker_control")

    def _claim(self, worker_id, pool, *, purpose):
        return self.control.claim(worker_id, pool, lease_seconds=900, purpose=purpose,
            job_filter=jobs.c.execution_plan["dispatch_backend"].as_string() == ROUTE,
            selected_job_id=self.message.job_id,
            selected_job_allowed=lambda job: _input_matches(job, self.message))

    def _collect(self, job, lease, tag, task_id, heartbeat):
        with collection_slot(self.collection_lock_dir, heartbeat):
            return super()._collect(job, lease, tag, task_id, heartbeat)


@dataclass(frozen=True)
class BrokerConfig:
    token_file: Path
    server_url: str
    host_port: str
    namespace: str = "sixnine-"
    tls: bool = True
    schedule_timeout_s: int = 3600
    execution_timeout_s: int = 7200
    poll_interval_s: float = 2
    recovery_interval_s: float = 30

    def __post_init__(self):
        if not Path(self.token_file).is_absolute():
            raise ValueError("hatchet_absolute_token_file_required")
        parts = urlsplit(self.server_url)
        if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in {"", "/"} or parts.scheme not in {"http", "https"}
                or parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("invalid_hatchet_server_url")
        host = urlsplit("//" + self.host_port)
        try:
            port = host.port
        except ValueError:
            raise ValueError("invalid_hatchet_host_port") from None
        if (not host.hostname or host.username or host.password or host.path or host.query or host.fragment
                or port is None or not 1 <= port <= 65535
                or not self.tls and host.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("invalid_hatchet_host_port")
        if (type(self.tls) is not bool or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.namespace)
                or type(self.schedule_timeout_s) is not int or not 60 <= self.schedule_timeout_s <= 86400
                or type(self.execution_timeout_s) is not int or not 600 <= self.execution_timeout_s <= 86400
                or not math.isfinite(self.poll_interval_s) or not .1 <= self.poll_interval_s <= 60
                or not math.isfinite(self.recovery_interval_s) or not 5 <= self.recovery_interval_s <= 300):
            raise ValueError("invalid_hatchet_timeouts")


def read_broker_config(path):
    from .inference.wangp_factory import read_document
    value = read_document(path, maximum=16384)
    if value.pop("version", None) != 1:
        raise ValueError("invalid_hatchet_config_version")
    try:
        return BrokerConfig(**value)
    except TypeError:
        raise ValueError("invalid_hatchet_config_fields") from None


def create_client(config):
    """Explicit protected credential; disable SDK dotenv and broad log capture."""
    from hatchet_sdk import ClientConfig, Hatchet
    from hatchet_sdk.config import ClientTLSConfig, HealthcheckConfig, OpenTelemetryConfig, OTelAttribute
    from .runtime_hosts.wangp_receipts import checked_reader
    with checked_reader(config.token_file, Path(config.token_file).parent) as source:
        info = os.fstat(source.fileno())
        if os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("hatchet_token_permissions")
        token = source.read(16385).strip()
    if not 32 <= len(token) <= 16384 or not token.isascii() or token.count(b".") != 2:
        raise ValueError("hatchet_token_invalid")
    try:
        # Init arguments take precedence over environment. Nested settings also
        # explicitly disable their default .env discovery.
        sdk_config = ClientConfig(_env_file=None, token=token.decode("ascii"), debug=False,
            server_url=config.server_url, host_port=config.host_port, namespace=config.namespace,
            tls_config=ClientTLSConfig(_env_file=None, strategy="tls" if config.tls else "none",
                server_name=urlsplit("//"+config.host_port).hostname, cert_file=None,
                key_file=None, root_ca_file=None),
            healthcheck=HealthcheckConfig(_env_file=None, enabled=False, bind_address="127.0.0.1"),
            otel=OpenTelemetryConfig(_env_file=None, excluded_attributes=list(OTelAttribute),
                include_task_name_in_start_step_run_span_name=False, individual_run_spans_for_bulk_run=False),
            disable_log_capture=True,
            enable_force_kill_sync_threads=False, force_shutdown_on_shutdown_signal=False)
        return Hatchet(config=sdk_config)
    except Exception:
        raise ValueError("hatchet_client_configuration_failed") from None


class SDKPublisher:
    def __init__(self, client, repository):
        self.client, self.repo = client, repository

    def publish(self, message, job):
        from hatchet_sdk import DesiredWorkerLabel, IdempotencyCollisionError
        # A dispatch snapshot or lease field cannot establish recovery affinity.
        # Read the original attempt and still-bound physical worker immediately
        # before publication. The callback rechecks it before a fenced claim.
        with self.repo.engine.connect() as connection:
            current = self.repo._job(connection, message.job_id)
            if not _input_matches(current, message):
                raise ValueError("hatchet_dispatch_identity_mismatch")
            original_worker = _original_worker(connection, current)
        desired = worker_labels(binding(current), original_worker) if original_worker else labels(binding(current))
        stub = self.client.stubs.task(name=workflow_name(current), input_validator=DispatchInput)
        try:
            result = stub.run_no_wait(input=message,
                desired_worker_labels=[DesiredWorkerLabel(key=key, value=value, required=True)
                    for key, value in desired.items()],
                additional_metadata={"sixnine_event_id": message.event_id, "sixnine_job_id": message.job_id})
        except IdempotencyCollisionError as collision:
            # A lost successful publish response is reconciled against the
            # engine's original run, not left in a perpetual unknown loop.
            return collision.existing_run_external_id
        return result.workflow_run_id

    def status(self, run_id):
        value = self.client.runs.get_status(run_id)
        return str(getattr(value, "value", value))


def _host_telemetry_context(repo, *, node_id, worker_id, spec, job_id):
    """One bounded original-node read per callback; diagnostics never gate work.

    A slot, owner, configuration and physical instance must all match before a
    provider identity is attached. Select only the job's closed binding fields,
    never its prompt/materials. No provider/Cloud request is made here.
    """
    try:
        if (not isinstance(node_id, str) or str(uuid.UUID(node_id)) != node_id
                or worker_id != "dstack-" + node_id.replace("-", "")
                or not isinstance(spec, dict) or spec.get("backend") != "wangp-worker"
                or spec.get("provider") != "vast"):
            return {}
        from .host_policy import bound_observation, finite
        from .operator_capacity import operator_nodes
        with repo.engine.connect() as connection:
            node = connection.execute(select(operator_nodes).where(
                operator_nodes.c.intent_id == node_id)).mappings().first()
            job = connection.execute(select(jobs.c.owner_id, jobs.c.tenant_id,
                jobs.c.request["deployment_profile_id"].as_string().label("profile_id"),
                jobs.c.request["request"]["mode"].as_string().label("mode"))
                .where(jobs.c.id == job_id)).mappings().first()
        if node is None or job is None:
            return {}
        payload, binding = node["payload"], node["payload"].get("dstack", {})
        now = repo.clock()
        observed = bound_observation(dict(node), now=now)
        if (observed is None or payload.get("tenant_id") != job["tenant_id"]
                or payload.get("owner_id") != job["owner_id"]
                or binding.get("profile_id") != job["profile_id"]
                or binding.get("mode") != job["mode"]
                or binding.get("configuration_id") != spec.get("configuration_id")
                or binding.get("manifest_digest") != spec.get("engine_manifest_digest")
                or observed["provider_instance_id"] != spec.get("instance_id")):
            return {}
        result = {key: observed[key] for key in ("machine_id", "region", "reliability",
            "quoted_hourly_microusd", "allocated_ram_mib", "allocated_vram_mib", "cpu")}
        result["host_identity_source"] = "vast_machine_id"
        gpu = "".join(c.lower() for c in observed["gpu"] if c.isalnum())
        result["gpu_type"] = ("rtx5090" if gpu in {"rtx5090", "nvidiageforcertx5090"}
            else "rtx-pro6000" if "pro6000" in gpu else "unknown")
        result["dstack_run"] = binding.get("run_name")
        model = binding.get("preparation", {}).get("model", {})
        stamp = binding.get("observed_at")
        result["warmth"] = "unknown"
        if (binding.get("runtime_incarnation") and finite(stamp, minimum=max(0, now-30), maximum=now)
                and isinstance(model, dict)):
            if model.get("state") == "loaded":
                result["warmth"] = "warm"
            elif model.get("state") == "not_loaded":
                result["warmth"] = "cold"
        return result
    except Exception:
        # Optional observation cannot replace original claim/recovery errors.
        return {}


class HatchetSlotRunner(WorkerRunner):
    """CPU process per physical GPU slot; no business credentials on the GPU."""
    def __init__(self, *args, broker_config, collection_lock_dir, client_factory=create_client, **kwargs):
        super().__init__(*args, **kwargs)
        if self.control is None:
            raise ValueError("hatchet_requires_durable_worker_control")
        self.broker_config = broker_config
        self.collection_lock_dir = Path(collection_lock_dir)
        self.client_factory = client_factory

    def run_forever(self, worker_id, pool, *, poll_interval_s=1):
        if os.name == "nt":
            raise ValueError("hatchet_worker_requires_linux_runtime")
        from hatchet_sdk import DesiredWorkerLabel, TTLBasedIdempotencyConfig
        registration = self.control.get(worker_id)
        spec = registration["spec"]
        if spec.get("dispatch_backend", "legacy") != ROUTE or len(spec["physical_gpu_ids"]) != 1:
            raise ValueError("hatchet_requires_one_registered_gpu_slot")
        manifest = getattr(self.backend, "manifest", None)
        document = getattr(manifest, "document", {})
        value = {"pool": pool, "configuration_id": spec["configuration_id"],
            "engine_manifest_digest": spec.get("engine_manifest_digest", ""),
            "deployment_profile_id": document.get("deployment_profile_id", ""),
            "mode": document.get("mode", ""), "backend": spec["backend"]}
        sdk = self.client_factory(self.broker_config)
        task_labels = labels(value)
        registration_labels = worker_labels(value, worker_id)
        task_name = "sixnine-generation-"+request_hash(value)[:24]
        stop = threading.Event()

        @sdk.task(name=task_name, input_validator=DispatchInput,
            schedule_timeout=timedelta(seconds=self.broker_config.schedule_timeout_s),
            execution_timeout=timedelta(seconds=self.broker_config.execution_timeout_s), retries=0,
            desired_worker_labels=[DesiredWorkerLabel(key=k, value=v, required=True) for k,v in task_labels.items()],
            idempotency=TTLBasedIdempotencyConfig(key_expression="input.event_id", ttl=timedelta(hours=24)))
        def execute(message: DispatchInput, context) -> dict:
            telemetry_context = dict(self.telemetry_context)
            telemetry_context.update(provider="local" if spec["provider"] == "mock" else spec["provider"],
                node_id=getattr(self.backend, "slot_key", ""))
            telemetry_context.update(_host_telemetry_context(self.repo,
                node_id=getattr(self.backend, "slot_key", ""), worker_id=worker_id,
                spec=spec, job_id=message.job_id))
            try:
                # This pinned SDK property is a public identity, never its
                # input/output/metadata/baggage. The exporter applies the
                # closed identifier projection before emitting logs/traces.
                telemetry_context["hatchet_run"] = context.workflow_run_id
            except Exception:
                pass  # Missing diagnostics cannot interrupt business work.
            runner = ExactJobRunner(self.repo, self.store, self.work_dir, message=message,
                collection_lock_dir=self.collection_lock_dir, backend=self.backend, control=self.control,
                submission_guard=self.submission_guard, stop_requested=self.stop_requested,
                telemetry=self.telemetry, telemetry_context=telemetry_context)
            deadline = time.monotonic()+self.broker_config.execution_timeout_s-30
            while not stop.is_set() and not context.is_cancelled and time.monotonic() < deadline:
                with self.repo.engine.connect() as connection:
                    job = self.repo._job(connection, message.job_id)
                if not _input_matches(job, message):
                    raise ValueError("hatchet_dispatch_identity_mismatch")
                if job["status"] in TERMINAL:
                    return {"job_id": message.job_id, "state": job["status"]}
                if binding(job) != value:
                    return {"job_id": message.job_id, "state": "reconciliation_required",
                        "reason_code": "hatchet_worker_incompatible"}
                with self.repo.engine.connect() as connection:
                    current_worker = self.control._worker(connection, worker_id)
                    try:
                        original_worker = _original_worker(connection, job)
                    except ValueError:
                        return {"job_id": message.job_id, "state": "reconciliation_required",
                            "reason_code": "hatchet_original_binding_unconfirmed"}
                if (not self.control.matches(current_worker, job)
                        or original_worker not in (None, worker_id)):
                    return {"job_id": message.job_id, "state": "reconciliation_required",
                        "reason_code": "hatchet_worker_incompatible"}
                if current_worker["current_job_id"] not in (None, message.job_id):
                    return {"job_id": message.job_id, "state": "reconciliation_required",
                        "reason_code": "hatchet_worker_busy"}
                result = runner.run_once(worker_id, pool)
                if result.get("state") in TERMINAL:
                    return {"job_id": message.job_id, "state": result["state"]}
                if result.get("state") in {"idle", "draining"}:
                    # Another replica, busy slot, admission or higher-priority
                    # candidate may win. Yield this broker slot now; its original
                    # ledger/outbox wakeup can retry later without inference.
                    return {"job_id": message.job_id, "state": "reconciliation_required",
                        "reason_code": "hatchet_claim_unavailable"}
                time.sleep(self.broker_config.poll_interval_s)
            # Broker interruption is not business cancellation. The durable
            # attempt/slot survives and the dispatch recovery scan reconciles.
            return {"job_id": message.job_id, "state": "reconciliation_required"}

        worker = sdk.worker(name=worker_id, slots=1, labels=registration_labels, workflows=[execute])

        def maintain_registration():
            while not stop.wait(30):
                try:
                    current = self.control.get(worker_id)
                    if current["state"] not in {"unknown", "retired"} and current["expires_at"] > self.repo.clock():
                        self.control.heartbeat(worker_id, current["fence"])
                except Exception:
                    # Registration failure makes new claims unavailable; never
                    # fabricate readiness or release an unresolved GPU slot.
                    pass

        keepalive = threading.Thread(target=maintain_registration, name="sixnine-hatchet-slot-lease", daemon=True)
        keepalive.start()
        try:
            worker.start()
        finally:
            stop.set()
            keepalive.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Identity-only Sixnine Hatchet bridge")
    parser.add_argument("role", choices=("dispatcher", "worker"))
    parser.add_argument("--broker-config", required=True)
    parser.add_argument("--fleet-config")
    parser.add_argument("--worker-id")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    from .settings import Settings
    settings = Settings.from_environment()
    config = read_broker_config(args.broker_config)
    if args.role == "worker":
        if not args.fleet_config or not args.worker_id or args.once:
            parser.error("worker requires --fleet-config and --worker-id; --once is dispatcher-only")
        from .fleet import read_config, run_slot
        # Read only the explicit protected SIXNINE_TELEMETRY_CONFIG_FILE mount.
        # It is optional and failsoft; no token is passed through CLI arguments
        # or Hatchet's automatic payload/log instrumentation.
        telemetry = configured_telemetry()
        try:
            fleet = read_config(args.fleet_config)
            return run_slot(fleet, args.worker_id, settings,
                runner_factory=lambda *a, **kw: HatchetSlotRunner(*a, broker_config=config,
                    collection_lock_dir=fleet.work_dir/"collection-lock", telemetry=telemetry, **kw))
        finally:
            try:
                telemetry.force_flush(timeout_millis=1000)
            except Exception:
                pass
            try:
                telemetry.close()
            except Exception:
                pass
    repo = Repository(settings.database_url)
    try:
        repo.create_schema()
        dispatcher = OutboxDispatcher(repo, SDKPublisher(create_client(config), repo), publisher_id="dispatcher-"+uuid.uuid4().hex)
        stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
        next_recovery = 0
        while not stop.is_set():
            if time.monotonic() >= next_recovery:
                dispatcher.recover_wakeups()
                next_recovery = time.monotonic()+config.recovery_interval_s
            result = dispatcher.publish_once()
            # Only public identity/status fields are written, no raw exceptions.
            print(json.dumps(result), flush=True)
            if args.once:
                return result
            stop.wait(config.poll_interval_s)
    finally:
        repo.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("hatchet_bridge_failed; inspect protected service diagnostics") from None
