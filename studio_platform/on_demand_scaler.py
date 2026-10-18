"""Budget-bounded, repeatable zero-to-one GPU service for real queued work.

Each rental retains the existing single-use capacity approval, durable intent,
qualification, draining and billing contracts. A fresh approval is created only
after the previous instance is authoritatively removed and its work is settled.
No synthetic demand, minimum warm capacity, budget increases or TTL renewals.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields, replace
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time
import uuid

from sqlalchemy import func, select, update

from .autoscale import ScalePolicy
from .capacity import (
    proven_unadmitted_capacity_draft,
    proven_unsubmitted_capacity_job,
    transfer_unsubmitted_capacity,
)
from .lium_provider import LiumManifest, LiumProvider
from .lium_runtime_aws import AwsLiumLoader
from .production_scaler import (
    FiniteConfig,
    FiniteController,
    MODEL,
    RECIPE,
    ScalerError,
    save,
    stdin_loader,
    unique,
    validate_settings,
    verify_identity_files,
    verify_sources,
)
from .repository import (
    Repository,
    capacity_approvals,
    capacity_cycles,
    capacity_waiters,
    attempts,
    jobs,
    registered_workers,
    scaler_actions,
    scaler_leaders,
    instance_intents,
)
from .qualification_profiles import QUEUED_TASK_PROFILE
from .scaler import LaunchSpec, ScaleCoordinator
from .settings import Settings
from .service_policy import (
    cycle_sequence_allowed,
    service_member_ids,
    validate_service_config,
)
from .worker import _slot_lock


@dataclass(frozen=True)
class OnDemandConfig(FiniteConfig):
    service_mode: str = "on-demand"
    max_cycles: int | None = 8

    def __post_init__(self):
        super().__post_init__()
        if getattr(self, "service_policy", None) is not None:
            validate_service_config(self)
            if self.service_mode != "on-demand":
                raise ScalerError("ondemand_service_mode_invalid")
            return
        if (
            self.service_mode != "on-demand"
            or type(self.max_cycles) is not int
            or not 1 <= self.max_cycles <= 8
            or getattr(self, "allowed_owners", None) != ["superdan", "supervan"]
            or self.scale_policy["max_instances"] != 1
            or self.scale_policy["max_physical_gpus"] != 1
            or self.scale_policy["idle_before_drain_s"] != 600
            or self.max_cycles * self.scale_policy["instance_reservation_microusd"]
            > self.scale_policy["approved_remaining_microusd"]
        ):
            raise ScalerError("ondemand_explicit_single_gpu_budget_and_idle_required")


def json_config(config):
    value = asdict(config)
    if value.get("qualification_profile") == "fl50":
        value.pop("qualification_profile")
    if value.get("allowed_owners") is None:
        value.pop("allowed_owners")
    if value.get("authorization_extension_s") == 0:
        value.pop("authorization_extension_s")
    if value.get("service_policy") is None:
        value.pop("service_policy", None)
    if not value.get("output_delivery"):
        value.pop("output_delivery", None)
    if value.get("provider_preparation_timeout_s") is None:
        value.pop("provider_preparation_timeout_s", None)
    if value.get("provider_preparation_failure_limit") == 2:
        value.pop("provider_preparation_failure_limit", None)
    if not value.get("execution_recipe_id"):
        value.pop("execution_recipe_id", None)
    if value.get("execution_backend", "comfy-worker") == "comfy-worker":
        value.pop("execution_backend", None)
        value.pop("engine_manifest_digest", None)
    for key in (
        "work_dir",
        "data_dir",
        "source_dir",
        "ssh_key_file",
        "known_hosts_file",
    ):
        value[key] = str(value[key])
    return json.loads(json.dumps(value))


def read_config(path):
    try:
        path = Path(path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or os.name != "nt"
                and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError
        return OnDemandConfig(**json.loads(raw, object_pairs_hook=unique))
    except Exception:
        raise ScalerError("ondemand_config_unavailable_or_invalid") from None


def verified_service_receipt(config):
    """Only an exact protected prior service identity enables cleanup recovery.

    Missing state means a fresh start, which must validate today's policy.
    Mismatched/malformed state never downgrades into fresh-start behavior.
    """
    path = config.work_dir / "service-state.json"
    try:
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_symlink():
            raise ValueError
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or os.name != "nt"
                and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=unique)
        required = {"version", "config_hash", "sequence", "created_at"}
        if (
            not isinstance(value, dict)
            or not required <= set(value)
            or set(value) - required - {"transfer_from"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["config_hash"] != config.fingerprint()
            or value["created_at"] != config.created_at
            or not cycle_sequence_allowed(config, value["sequence"])
        ):
            raise ValueError
        previous = value.get("transfer_from")
        if previous is not None and (
            value["sequence"] <= 1
            or previous
            != cycle_config(config, value["sequence"] - 1).capacity_approval_id
        ):
            raise ValueError
        return True
    except Exception:
        raise ScalerError("ondemand_service_configuration_changed") from None


def cycle_config(config, sequence):
    if not cycle_sequence_allowed(config, sequence):
        raise ScalerError("ondemand_cycle_limit")
    values = {f.name: getattr(config, f.name) for f in fields(FiniteConfig)}
    suffix = "-" + str(sequence).zfill(3)
    work = config.work_dir / "cycles" / str(sequence).zfill(3)
    values.update(
        cycle_id=config.cycle_id + suffix,
        capacity_approval_id=config.capacity_approval_id + suffix,
        work_dir=work,
        known_hosts_file=work / "known_hosts",
    )
    return FiniteConfig(**values)


class ServiceCycle(FiniteController):
    """The existing finite controller scoped to one approved rental cycle."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scaler.unsubmitted_retirement_guard = self._preparation_retirement_allowed
        self.scaler.unused_preparation_guard = self._unused_provider_preparation

    def provider_retirement(self):
        rows, _ = self._managed()
        with self.repo.engine.connect() as conn:
            for row in rows:
                value = self.scaler.preparation(conn, row)
                if value and value["phase"] == "retiring_unused":
                    return value
        return None

    def _unused_provider_preparation(self, conn, intent):
        """Called under leader/capacity/action locks, before durable retirement.

        The awaiting_provider receipt, committed before rental creation, is the
        positive start barrier. Missing files alone can never create that proof.
        """
        if (
            not getattr(self, "preserve_rollover", lambda: False)()
            or intent["id"] in self.boots
            or intent["pool"] != self.config.pool
        ):
            return False
        cycle = (
            conn.execute(
                select(capacity_cycles).where(
                    capacity_cycles.c.approval_id == self.config.capacity_approval_id
                )
            )
            .mappings()
            .first()
        )
        grant = (
            conn.execute(
                select(capacity_approvals).where(
                    capacity_approvals.c.id == self.config.capacity_approval_id
                )
            )
            .mappings()
            .first()
        )
        if (
            not cycle
            or cycle["intent_id"] != intent["id"]
            or not grant
            or not grant["enabled"]
        ):
            return False
        # A conflicting historical/bootstrap footprint requires explicit recovery.
        for path in (
            self.config.work_dir / "boot" / intent["id"],
            self.config.work_dir / ("lifetime-" + intent["id"] + ".json"),
        ):
            if path.exists() or path.is_symlink():
                return False
        pending = list(
            conn.execute(
                select(jobs)
                .where(
                    jobs.c.pool == self.config.pool,
                    jobs.c.status.not_in(
                        ("succeeded", "failed", "cancelled", "planned", "blocked")
                    ),
                )
                .order_by(jobs.c.id)
                .with_for_update()
                .limit(4097)
            ).mappings()
        )
        if len(pending) > 4096 or any(
            j["execution_plan"].get("capacity_approval_id") != grant["id"]
            or not proven_unsubmitted_capacity_job(conn, j)
            for j in pending
        ):
            return False
        # Freeze new admissions atomically with the start fence and destroy intent.
        conn.execute(
            update(capacity_approvals)
            .where(capacity_approvals.c.id == grant["id"])
            .values(enabled=0)
        )
        return True

    def _sync_provider_preparation(self):
        value = self.provider_retirement()
        if value is not None:
            self.request_rollover()
            self._expire_held_waiters()
            self.cold.record_wait_reason(
                self.config.capacity_approval_id, self.provider_wait_reason(value)
            )
        elif not self._capacity_advance_allowed():
            self._expire_held_waiters()
            self.cold.record_wait_reason(
                self.config.capacity_approval_id, "creation_needs_reconciliation"
            )

    def _capacity_advance_allowed(self):
        if self.config.provider_preparation_timeout_s is None:
            return True
        rows, _ = self._managed()
        with self.repo.engine.connect() as conn:
            for row in rows:
                if row["state"] == "destroyed" and row["provider_instance_id"]:
                    phase = self.scaler.preparation(conn, row)
                    if phase is None or phase["phase"] == "awaiting_provider":
                        return False  # Removed, but conflicting unused evidence is still unresolved.
        return True

    def provider_wait_reason(self, retirement):
        return (
            "provider_preparation_retry_limit"
            if getattr(self, "provider_retry_limited", lambda: False)()
            else retirement["reason"]
        )

    def _preparation_retirement_allowed(self, conn, intent):
        hold = self.preparation_hold()
        if (
            not hold
            or intent["state"] != "draining"
            or hold["intent_id"] != intent["id"]
            or hold["instance_id"] != intent["provider_instance_id"]
        ):
            return False
        grant = (
            conn.execute(
                select(capacity_approvals).where(
                    capacity_approvals.c.id == self.config.capacity_approval_id
                )
            )
            .mappings()
            .first()
        )
        pending = list(
            conn.execute(
                select(jobs)
                .where(
                    jobs.c.pool == self.config.pool,
                    jobs.c.status.not_in(
                        ("succeeded", "failed", "cancelled", "planned", "blocked")
                    ),
                )
                .limit(4097)
            ).mappings()
        )
        return bool(
            grant
            and grant["enabled"] == 0
            and len(pending) <= 4096
            and all(
                j["execution_plan"].get("capacity_approval_id") == grant["id"]
                and proven_unsubmitted_capacity_job(conn, j)
                for j in pending
            )
        )

    def _managed(self):
        with self.repo.engine.connect() as conn:
            intent_id = conn.execute(
                select(capacity_cycles.c.intent_id).where(
                    capacity_cycles.c.approval_id == self.config.capacity_approval_id
                )
            ).scalar_one_or_none()
            rows = self.repo.list_instance_intents(pool=self.config.pool)
            actions = {
                r["intent_id"]: dict(r)
                for r in conn.execute(
                    select(scaler_actions).where(
                        scaler_actions.c.pool == self.config.pool
                    )
                ).mappings()
            }
        selected = [r for r in rows if r["id"] == intent_id]
        if (
            intent_id
            and len(selected) != 1
            or any(r["state"] != "destroyed" and r["id"] != intent_id for r in rows)
            or any(
                r["provider"] != "lium"
                or r["id"] not in actions
                or actions[r["id"]]["launch_spec"] not in self.config.launches
                for r in selected
            )
        ):
            raise ScalerError("ondemand_other_instance_requires_reconciliation")
        return selected, {r["id"]: actions[r["id"]] for r in selected}

    def initialize(self):
        c = self.config
        c.work_dir.mkdir(parents=True, exist_ok=True)
        receipt = c.work_dir / "cycle-state.json"
        if receipt.exists():
            if json.loads(receipt.read_text())["config_hash"] != c.fingerprint():
                raise ScalerError("ondemand_cycle_configuration_changed")
        else:
            self._managed()  # Reject an unrelated/unknown live lease, never hide it.
            if not c.created_at <= self.repo.clock() < c.stop_claiming_at:
                raise ScalerError("ondemand_service_approval_expired")
            save(
                receipt,
                {
                    "config_hash": c.fingerprint(),
                    "ports": {},
                    "created_at": self.repo.clock(),
                },
            )
        self.remaining_budget()
        from .execution_policy import read_policy

        with self.repo.engine.connect() as conn:
            existing = (
                conn.execute(
                    select(capacity_approvals).where(
                        capacity_approvals.c.id == c.capacity_approval_id
                    )
                )
                .mappings()
                .first()
            )
        if existing:
            # Expired/revoked grants retain their reconciliation path; calling
            # approve_capacity again would reject an expired deadline before
            # the old pod could be reconciled. Never re-enable the old grant.
            if existing["enabled"] != 1 or not self.approval_current(
                existing["payload"]
            ):
                self.request_drain()
            return
        policy = read_policy(self.settings.execution_policy_file)
        engine = (
            {
                "backend": c.execution_backend,
                "engine_manifest_digest": c.engine_manifest_digest,
            }
            if getattr(c, "execution_backend", "comfy-worker") == "wangp-worker"
            else {}
        )
        if c.output_delivery:
            engine["output_delivery"] = c.output_delivery
        self.repo.approve_capacity(
            c.capacity_approval_id,
            tenant_id=c.tenant,
            pool=c.pool,
            model_id=MODEL,
            configuration_id=c.configuration_id,
            recipe_ids=list(c.recipe_ids),
            policy_hash=c.execution_policy_sha256,
            qualification_evidence_id=c.qualification_evidence_id,
            qualification_expires_at=policy["qualification"]["expires_at"],
            quote_expires_at=policy["reservation"]["expires_at"],
            expires_at=min(
                c.stop_claiming_at,
                policy["qualification"]["expires_at"],
                policy["reservation"]["expires_at"],
            ),
            launch=LaunchSpec(**c.launches[0]),
            scale_policy=ScalePolicy(**c.scale_policy),
            budget_scope=c.scope,
            budget_account_ids=c.budget_account_ids,
            enabled=True,
            **engine,
        )

    def request_rollover(self):
        # Separate from final service shutdown: admitted jobs retain their
        # IDs and reservations while the old GPU finishes existing work.
        (self.config.work_dir / "rollover.flag").touch()
        self.request_drain()

    def preparation_hold(self):
        path = self.config.work_dir / "preparation-hold.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        if (
            value.get("version") != 1
            or value.get("config_hash") != self.config.fingerprint()
            or value.get("sources") != self.config.source_sha256
            or value.get("reason")
            not in {"bootstrap_repair_required", "queued_task_repair_required"}
            or value.get("reason") == "queued_task_repair_required"
            and self.config.qualification_profile != QUEUED_TASK_PROFILE
        ):
            raise ScalerError("ondemand_preparation_hold_identity_mismatch")
        return value

    def _queued_task_failure_hold(self, intent, *, allow_unconfirmed=False):
        """Quarantine a proven failing worker without cancelling unrelated work.

        Startup identity and durable attempts are authoritative, including a
        crash between SQL quarantine and its small evidence-file write. This
        hold grants no replacement rental or idle proof: reconciliation,
        collection, original waiter deadlines and provider billing still apply.
        """
        if self.config.qualification_profile != QUEUED_TASK_PROFILE:
            return False
        directory = self.config.work_dir / "boot" / intent["id"]
        expected = self._bootstrap_identity(intent)
        evidence = json.loads((directory / "bootstrap-state.json").read_text())
        if (
            evidence.get("identity") != expected
            or evidence.get("qualification_profile") != QUEUED_TASK_PROFILE
            or evidence.get("phase") not in {"fleet_starting", "fleet_started"}
            or evidence.get("runtime_validation")
            != {
                "profile": QUEUED_TASK_PROFILE,
                "state": "runtime_ready",
                "generation_verified": False,
            }
            or evidence.get("smoke_submission_started") is not None
            or evidence.get("smoke_task_id") is not None
            or evidence.get("evidence") is not None
        ):
            return False
        worker_id = "lium-" + intent["id"].replace("-", "")
        proven = []
        with self.repo.transaction() as conn:
            worker = self.repo._locked(
                conn,
                select(registered_workers).where(registered_workers.c.id == worker_id),
            )
            if (
                not worker
                or worker["provider"] != "lium"
                or worker["instance_id"] != intent["provider_instance_id"]
                or worker["pool"] != self.config.pool
                or worker["spec"].get("configuration_id")
                != self.config.configuration_id
                or worker["spec"].get("model_id") != MODEL
            ):
                return False
            rows = list(
                conn.execute(
                    select(
                        attempts,
                        jobs.c.status.label("job_status"),
                        jobs.c.error_code.label("job_error"),
                        jobs.c.execution_plan,
                    )
                    .join(
                        jobs,
                        (jobs.c.id == attempts.c.job_id)
                        & (jobs.c.current_attempt_id == attempts.c.id),
                    )
                    .where(
                        attempts.c.worker_id == worker_id,
                        jobs.c.pool == self.config.pool,
                    )
                    .limit(1025)
                ).mappings()
            )
            if len(rows) > 1024:
                return False
            for row in rows:
                if (
                    row["execution_plan"].get("configuration_id")
                    != self.config.configuration_id
                    or row["execution_plan"].get("policy_hash")
                    != self.config.execution_policy_sha256
                ):
                    continue
                failed = (
                    row["job_status"] == "failed"
                    and row["status"] == "failed"
                    and row["upstream_stopped"] == 1
                )
                preparation = (
                    row["job_status"] == "queued"
                    and row["status"] == "deferred"
                    and row["job_error"]
                    in {"worker_preparation_not_ready", "worker_preparation_failed"}
                    and row["submission_started_at"] is None
                    and row["upstream_task_id"] is None
                )
                collecting = (
                    row["job_status"] == "collecting"
                    and row["status"] == "collecting"
                    and row["job_error"] == "collection_failed"
                    and row["collection_failures"] > 0
                    and bool(row["upstream_task_id"])
                    and row["upstream_stopped"] == 1
                )
                if failed or preparation or collecting:
                    proven.append({"job_id": row["job_id"], "attempt_id": row["id"]})
            if not proven and not (
                allow_unconfirmed and worker["drain_requested"] == 1
            ):
                return False  # Unknown/claimed/running/cancelled is not failure proof.
            conn.execute(
                update(registered_workers)
                .where(registered_workers.c.id == worker_id)
                .values(
                    drain_requested=1, state="draining", updated_at=self.repo.clock()
                )
            )
        save(
            self.config.work_dir / "preparation-hold.json",
            {
                "version": 1,
                "config_hash": self.config.fingerprint(),
                **{k: expected[k] for k in ("intent_id", "instance_id", "sources")},
                "reason": "queued_task_repair_required",
                "worker_id": worker_id,
                "failed_attempts": proven,
                "verification_state": "failed_attempt_confirmed"
                if proven
                else "evidence_unconfirmed_not_upstream_stopped",
                "observed_at": self.repo.clock(),
            },
        )
        self._record_repair_wait_reason()
        self.request_drain()
        return True

    def _record_repair_wait_reason(self):
        hold = self.preparation_hold()
        if hold is None:
            return
        reason = hold["reason"]
        self.cold.record_wait_reason(self.config.capacity_approval_id, reason)
        if reason == "queued_task_repair_required":
            self.hold_queued_task_backlog()

    def _bootstrap_identity(self, intent):
        """Match the boot receipt without weakening historical Comfy identity."""
        identity = {
            "intent_id": intent["id"],
            "instance_id": intent["provider_instance_id"],
            "configuration_id": self.config.configuration_id,
            "sources": self.config.source_sha256,
        }
        if getattr(self.config, "execution_backend", "comfy-worker") == "wangp-worker":
            identity.update(
                backend="wangp-worker",
                engine_manifest_digest=self.config.engine_manifest_digest,
            )
        if self.config.output_delivery:
            identity["output_delivery"] = self.config.output_delivery
        return identity

    def _boot_failure(self, intent, state):
        if self.config.qualification_profile == QUEUED_TASK_PROFILE and state.get(
            "state"
        ) in {"fleet_attention_required", "fleet_recovery_required"}:
            try:
                if self._queued_task_failure_hold(
                    intent,
                    allow_unconfirmed=state.get("error_code")
                    == "finite_real_task_evidence_unconfirmed",
                ):
                    return
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Never infer failure/idle from unavailable evidence.
        if state.get("state") not in {"bootstrap_failed", "staging_failed"}:
            return super()._boot_failure(intent, state)
        # Only a retained, identity-bound failure BEFORE model qualification
        # can hold backlog. A failed inference or lost submission stays under
        # the existing reconciliation/shutdown contract.
        path = self.config.work_dir / "boot" / intent["id"] / "bootstrap-state.json"
        try:
            evidence = json.loads(path.read_text())
            expected = self._bootstrap_identity(intent)
            boot = self.boots[intent["id"]]
            if (
                evidence.get("identity") != expected
                or evidence.get("phase") != state.get("state")
                or evidence.get("smoke_submission_started") is not None
                or getattr(boot, "fleet", None) is not None
                or any(
                    (path.parent / name / "state.json").exists()
                    for name in (
                        "firstlast4-768p-5s-v1",
                        "ref4-bounded-768p-5s-v1",
                        "reference-smoke",
                        "reference-full-smoke",
                    )
                )
            ):
                return super()._boot_failure(intent, state)
            with self.repo.engine.connect() as conn:
                pending = list(
                    conn.execute(
                        select(jobs)
                        .where(
                            self.scope_filter(),
                            jobs.c.status.not_in(("succeeded", "failed", "cancelled")),
                        )
                        .limit(4097)
                    ).mappings()
                )
                if (
                    len(pending) > 4096
                    or any(
                        not proven_unsubmitted_capacity_job(conn, j) for j in pending
                    )
                    or conn.execute(
                        select(registered_workers.c.id).where(
                            registered_workers.c.pool == self.config.pool,
                            registered_workers.c.state != "retired",
                        )
                    ).first()
                ):
                    return super()._boot_failure(intent, state)
            save(
                self.config.work_dir / "preparation-hold.json",
                {
                    "version": 1,
                    "config_hash": self.config.fingerprint(),
                    "intent_id": intent["id"],
                    "instance_id": intent["provider_instance_id"],
                    "sources": self.config.source_sha256,
                    "reason": "bootstrap_repair_required",
                    "observed_at": self.repo.clock(),
                },
            )
            self.cold.record_wait_reason(
                self.config.capacity_approval_id, "bootstrap_repair_required"
            )
            self.request_drain()  # Retire the failed GPU, not the user work.
        except (OSError, ValueError, KeyError, TypeError):
            super()._boot_failure(intent, state)

    def _expire_held_waiters(self):
        # A repair hold is bounded by the ORIGINAL accepted wait deadline.
        # It does not create a new confirmation window or ignore cancellation.
        with self.repo.transaction() as conn:
            ids = list(
                conn.execute(
                    select(capacity_waiters.c.job_id)
                    .where(
                        capacity_waiters.c.approval_id
                        == self.config.capacity_approval_id,
                        capacity_waiters.c.state == "waiting_capacity",
                        capacity_waiters.c.deadline <= self.repo.clock(),
                    )
                    .order_by(capacity_waiters.c.job_id)
                ).scalars()
            )
            for jid in ids:
                job = self.repo._job(conn, jid, lock=True)
                if job["status"] not in (
                    "waiting_capacity",
                    "queued",
                ) or not proven_unsubmitted_capacity_job(conn, job):
                    continue
                self.repo._settle(conn, "job", jid, 0)
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == jid)
                    .values(
                        status="failed",
                        error_code="capacity_wait_deadline_expired",
                        fence=job["fence"] + 1,
                        updated_at=self.repo.clock(),
                    )
                )
                conn.execute(
                    update(capacity_waiters)
                    .where(capacity_waiters.c.job_id == jid)
                    .values(state="failed")
                )
                self.repo._emit(
                    conn,
                    "job.failed",
                    jid,
                    {
                        "job_id": jid,
                        "status": "failed",
                        "error_code": "capacity_wait_deadline_expired",
                    },
                )

    def _close_unsubmitted(self):
        if (
            self.preparation_hold()
            or self.provider_retirement()
            or (self.config.work_dir / "rollover.flag").exists()
        ) and getattr(self, "preserve_rollover", lambda: False)():
            self.repo.set_capacity_approval_enabled(
                self.config.capacity_approval_id, enabled=False
            )
            if self.provider_retirement():
                self._expire_held_waiters()
                self.cold.record_wait_reason(
                    self.config.capacity_approval_id,
                    self.provider_wait_reason(self.provider_retirement()),
                )
            if self.preparation_hold():
                if self.preparation_hold()["reason"] == "bootstrap_repair_required":
                    self._expire_held_waiters()
                self._record_repair_wait_reason()
            return
        super()._close_unsubmitted()


class OnDemandController:
    def __init__(self, repo, settings, config, *, provider, boot_factory=None):
        self.repo, self.settings, self.config, self.provider = (
            repo,
            settings,
            config,
            provider,
        )
        self.boot_factory = boot_factory
        self.sequence = 1
        self.current = None
        self.leader_id = "on-demand-" + uuid.uuid4().hex
        self.transfer_from = None

    @property
    def receipt_path(self):
        return self.config.work_dir / "service-state.json"

    def stopping(self):
        return (
            self.config.work_dir / "drain.flag"
        ).exists() or self.repo.clock() >= self.config.stop_claiming_at

    def request_drain(self):
        (self.config.work_dir / "drain.flag").touch()
        if self.current:
            self.current.request_drain()

    def _persist(self):
        save(
            self.receipt_path,
            {
                "version": 1,
                "config_hash": self.config.fingerprint(),
                "sequence": self.sequence,
                "created_at": self.config.created_at,
                "transfer_from": self.transfer_from,
            },
        )

    def initialize(self):
        c = self.config
        c.work_dir.mkdir(parents=True, exist_ok=True)
        if self.receipt_path.exists():
            value = json.loads(self.receipt_path.read_text())
            if value.get("config_hash") != c.fingerprint():
                raise ScalerError("ondemand_service_configuration_changed")
            self.sequence = value["sequence"]
            self.transfer_from = value.get("transfer_from")
        else:
            if self.repo.list_instance_intents(pool=c.pool):
                raise ScalerError("ondemand_new_service_requires_unused_pool")
            self._persist()
        self._open_cycle()

    def _open_cycle(self):
        config = cycle_config(self.config, self.sequence)
        cycle_type = ServiceCycle
        if service_member_ids(config):
            from .pool_member_controller import PoolServiceCycle

            cycle_type = PoolServiceCycle
        self.current = cycle_type(
            self.repo,
            self.settings,
            config,
            provider=self.provider,
            boot_factory=self.boot_factory,
        )
        self.current.leader_id = self.leader_id
        self.current.preserve_rollover = lambda: not self.stopping()
        self.current.provider_retry_limited = self.provider_retry_limited
        config.work_dir.mkdir(parents=True, exist_ok=True)
        self.current.config_path = config.work_dir / "operator-cycle.json"
        expected = json_config(config)
        if self.current.config_path.exists():
            if json.loads(self.current.config_path.read_text()) != expected:
                raise ScalerError("ondemand_saved_cycle_mismatch")
        else:
            save(self.current.config_path, expected)
        self.current.initialize()
        if self.transfer_from:
            expected_previous = cycle_config(
                self.config, self.sequence - 1
            ).capacity_approval_id
            if self.transfer_from != expected_previous:
                raise ScalerError("ondemand_transfer_identity_mismatch")
            transfer_unsubmitted_capacity(
                self.repo,
                self.transfer_from,
                config.capacity_approval_id,
                allowed_owners=self.config.allowed_owners,
                children_done_confirmed=True,
            )
            self.transfer_from = None
            self._persist()

    def idle_probe(self, tag, instance_id):
        if not self.current:
            raise ScalerError("ondemand_cycle_not_initialized")
        return self.current.idle_probe(tag, instance_id)

    def _can_rotate(self, value):
        # Empty pool is the initial waiting state, not a completed rental.
        if (
            not value["instances"]
            or not value["all_destroyed"]
            or value["active_jobs_truncated"]
            or not all(b.children_done() for b in self.current.boots.values())
        ):
            return False
        if not self.current._capacity_advance_allowed():
            return False
        if self.current.provider_retirement() and value["billing_pending"]:
            return False  # Provider DELETE uncertainty and outstanding charges retain this cycle.
        if self.provider_retry_limited():
            return False
        # An inventory race must not consume all approved cycles in one tight
        # loop. No-rent attempts retain the same job during this bounded pause.
        rows, _ = self.current._managed()
        if any(
            r["provider_instance_id"] is None
            and self.repo.clock() - r["updated_at"] < 60
            for r in rows
        ):
            return False
        retired_path = self.current.config.work_dir / "children-retired.json"
        retired = {
            "config_hash": self.current.config.fingerprint(),
            "intent_ids": sorted(row["id"] for row in value["instances"]),
        }
        if self.current.boots:
            # Persist the real process-handle observation before committing
            # rotation. A restart cannot infer natural exit from lost handles.
            save(retired_path, retired)
        else:
            started = False
            try:
                for row in value["instances"]:
                    path = (
                        self.current.config.work_dir
                        / "boot"
                        / row["id"]
                        / "bootstrap-state.json"
                    )
                    if path.exists() and json.loads(path.read_text()).get("phase") in (
                        "fleet_starting",
                        "fleet_started",
                    ):
                        started = True
                if started and (
                    not retired_path.exists()
                    or json.loads(retired_path.read_text()) != retired
                ):
                    return False
            except (OSError, ValueError, TypeError):
                return False
        # Only provably never-submitted backlog may outlive a GPU rental.
        # A completed job with an unresolved paid attempt is included by the
        # finite status audit and fails this proof too.
        with self.repo.transaction() as conn:
            # Serialize with admission/enqueue, then inspect locked jobs. Drafts
            # do not keep a GPU alive, but their labels alone cannot prove that
            # no earlier inference/lease still needs reconciliation.
            self.repo._lock_capacity(conn)
            for jid in sorted(value["active_job_ids"]):
                job = self.repo._job(conn, jid, lock=True)
                if job["status"] in ("planned", "blocked"):
                    if not proven_unadmitted_capacity_draft(conn, job):
                        return False
                    continue
                if not proven_unsubmitted_capacity_job(conn, job):
                    return False
        return True

    def provider_failure_count(self):
        """Read a bounded streak from the existing cycle/start-barrier ledger.

        No process-local counter can reset this on restart. Only actual owned
        provider pods count; inventory refusals/authoritative no-rent cycles do
        not count or reset the streak. Read at most the limit plus current pod.
        """
        if self.config.provider_preparation_timeout_s is None:
            return 0
        count = 0
        prefix = self.config.capacity_approval_id + "-"
        with self.repo.engine.connect() as conn:
            rows = conn.execute(
                select(instance_intents, capacity_cycles.c.approval_id)
                .join(
                    capacity_cycles,
                    capacity_cycles.c.intent_id == instance_intents.c.id,
                )
                .where(
                    instance_intents.c.pool == self.config.pool,
                    instance_intents.c.provider_instance_id.is_not(None),
                    capacity_cycles.c.approval_id.startswith(prefix, autoescape=True),
                )
                .order_by(
                    func.length(capacity_cycles.c.approval_id).desc(),
                    capacity_cycles.c.approval_id.desc(),
                )
                .limit(self.config.provider_preparation_failure_limit + 1)
            ).mappings()
            for row in rows:
                sequence = int(row["approval_id"][len(prefix) :])
                config = cycle_config(self.config, sequence)
                if (
                    config.capacity_approval_id != row["approval_id"]
                    or sequence > self.sequence
                ):
                    raise ScalerError("provider_preparation_history_unconfirmed")
                reader = ScaleCoordinator(
                    self.repo,
                    preparation_timeout_s=config.provider_preparation_timeout_s,
                    preparation_binding=config.fingerprint(),
                )
                phase = reader.preparation(conn, row)
                if phase is None:
                    raise ScalerError("provider_preparation_history_unconfirmed")
                if phase["phase"] == "bootstrap_started":
                    return count
                if phase["phase"] == "retiring_unused" or row["state"] == "destroyed":
                    count += 1
                    if count >= self.config.provider_preparation_failure_limit:
                        return count
                elif sequence != self.sequence:
                    raise ScalerError("provider_preparation_history_unconfirmed")
        return count

    def provider_retry_limited(self):
        return bool(
            self.current
            and self.current.provider_retirement()
            and self.provider_failure_count()
            >= self.config.provider_preparation_failure_limit
        )

    def tick(self):
        if service_member_ids(self.config):
            return self._tick_members()
        # A failed qualification or explicit grant revocation ends the service.
        # Only our durable TTL-rollover marker authorizes preserving backlog
        # and opening a replacement cycle under the original service budget.
        self.current._sync_provider_preparation()
        if (
            self.current.stopping()
            and not self.current.preparation_hold()
            and not (self.current.config.work_dir / "rollover.flag").exists()
        ):
            self.request_drain()
        if self.stopping():
            self.current.request_drain()
        else:
            rows, _ = self.current._managed()
            if any(
                row["state"] != "destroyed"
                and self.repo.clock()
                >= row["hard_deadline"] - self.config.drain_margin_s
                for row in rows
            ):
                self.current.request_rollover()
        value = self.current.tick()
        if (
            self.current.stopping()
            and not self.current.preparation_hold()
            and not (self.current.config.work_dir / "rollover.flag").exists()
        ):
            self.request_drain()
        if "instances" not in value:
            # A competing leader/lease must never become a shutdown exception.
            value = self.current.status(decision=value.get("phase", "not_leader"))
        if self.current.preparation_hold() and not self.stopping():
            self.current._record_repair_wait_reason()
        if (
            self._can_rotate(value)
            and not self.stopping()
            and not self.current.preparation_hold()
        ):
            self.repo.set_capacity_approval_enabled(
                self.current.config.capacity_approval_id, enabled=False
            )
            # Keep unsettled invoice reservations. A cycle limit never resets spend.
            enough = (
                self.current.remaining_budget()
                >= self.config.scale_policy["instance_reservation_microusd"]
            )
            if not cycle_sequence_allowed(self.config, self.sequence + 1) or not enough:
                self.request_drain()
                value = self.current.tick()
            else:
                self.transfer_from = self.current.config.capacity_approval_id
                self.sequence += 1
                self._persist()
                self._open_cycle()
                value = self.current.status()
        return self.status(value=value)

    def _tick_members(self):
        """Original-pair lifecycle; a peer's failure/TTL never drains the pool."""
        if self.stopping():
            self.current.request_drain()
        value = self.current.tick()
        if self.current.stopping() and not self.stopping():
            # Only whole-authority revocation/expiry enters this branch.
            self.request_drain()
        if "instances" not in value:
            value = self.current.status(decision=value.get("phase", "not_leader"))
        if (
            not self.stopping()
            and self.current.rotation_allowed()
            and self._can_rotate(value)
        ):
            self.repo.set_capacity_approval_enabled(
                self.current.config.capacity_approval_id, enabled=False
            )
            enough = (
                self.current.remaining_budget()
                >= self.config.scale_policy["instance_reservation_microusd"]
            )
            if not enough or not cycle_sequence_allowed(self.config, self.sequence + 1):
                self.request_drain()
                value = self.current.tick()
            else:
                self.transfer_from = self.current.config.capacity_approval_id
                self.sequence += 1
                self._persist()
                self._open_cycle()
                value = self.current.status()
        return self.status(value=value)

    def status(self, *, value=None, fresh_ledger_only=False):
        c = self.config
        if self.current is None:
            record = json.loads(self.receipt_path.read_text())
            if record.get("config_hash") != c.fingerprint():
                raise ScalerError("ondemand_service_configuration_changed")
            self.sequence = record["sequence"]
            cycle_type = ServiceCycle
            if service_member_ids(c):
                from .pool_member_controller import PoolServiceCycle

                cycle_type = PoolServiceCycle
            self.current = cycle_type(
                self.repo, self.settings, cycle_config(c, self.sequence), provider=None
            )
        value = value or self.current.status(fresh_ledger_only=fresh_ledger_only)
        rows = self.repo.list_instance_intents(pool=c.pool)
        all_destroyed = all(r["state"] == "destroyed" for r in rows)
        drained = bool(self.stopping() and value["drained"] and all_destroyed)
        admission_ready = False
        if not self.stopping():
            try:
                preview = self.current.cold.preview(
                    self.current.config.capacity_approval_id
                )
                with self.repo.engine.connect() as conn:
                    leader = (
                        conn.execute(
                            select(scaler_leaders).where(
                                scaler_leaders.c.pool == c.pool
                            )
                        )
                        .mappings()
                        .first()
                    )
                admission_ready = bool(
                    preview["approval_current"]
                    and leader
                    and leader["expires_at"] > self.repo.clock()
                )
            except Exception:
                pass
        result = {
            **value,
            "cycle_id": c.cycle_id,
            "config_hash": c.fingerprint(),
            "service_mode": "on-demand",
            "sequence": self.sequence,
            "max_cycles": c.max_cycles,
            "idle_shutdown_seconds": c.scale_policy["idle_before_drain_s"],
            "minimum_gpu_instances": 0,
            "admission_ready": admission_ready,
            "phase": "drained"
            if drained
            else "draining"
            if self.stopping()
            else "awaiting_provider_repair"
            if self.provider_retry_limited()
            or not self.current._capacity_advance_allowed()
            else "awaiting_repair"
            if self.current.preparation_hold()
            else ("waiting_capacity" if value["active_job_ids"] else "awaiting_jobs")
            if not value["instances"]
            else value["phase"],
            "drained": drained,
            "all_destroyed": all_destroyed,
            "billing_pending": sum(r["billing_status"] != "settled" for r in rows),
            "instances": [
                {
                    k: r[k]
                    for k in (
                        "id",
                        "state",
                        "provider_instance_id",
                        "hard_deadline",
                        "billing_status",
                    )
                }
                for r in rows
            ],
        }
        if self.provider_retry_limited():
            result["recovery"] = {
                "code": "provider_preparation_retry_limit",
                "consecutive_failures": self.provider_failure_count(),
                "failure_limit": c.provider_preparation_failure_limit,
                "automatic_rerent_allowed": False,
            }
        elif not self.current._capacity_advance_allowed():
            result["recovery"] = {
                "code": "provider_preparation_evidence_unconfirmed",
                "automatic_rerent_allowed": False,
            }
        if service_member_ids(c):
            result["target_gpu_instances"] = 2
            result["member_replacement_enabled"] = (
                "member_replacement" in c.service_policy
            )
        if not fresh_ledger_only:
            save(c.work_dir / "status.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--credential-stdin", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--request-drain", action="store_true")
    actions.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    repo = provider = loader = controller = None
    handlers = {}
    try:
        config = read_config(args.config)
        if not args.enabled and not args.request_drain and not args.status:
            print(
                json.dumps(
                    {
                        "phase": "disabled",
                        "config_valid": True,
                        "config_hash": config.fingerprint(),
                        "provider_calls_enabled": False,
                    }
                )
            )
            return 0
        if args.request_drain:
            if not verified_service_receipt(config):
                raise ScalerError("ondemand_service_configuration_changed")
            (config.work_dir / "drain.flag").touch()
            print(json.dumps({"phase": "drain_requested", "drained": False}))
            return 0
        recovering = verified_service_receipt(config)
        settings = Settings.from_environment()
        # Recovery may reconcile an existing rental after policy revocation.
        # The exact prior receipt, production Settings, pinned sources and SSH
        # identity remain required. The controller rechecks policy and drains;
        # skipping this one preflight never authorizes another rental.
        validate_settings(
            config, settings, require_policy=not args.status and not recovering
        )
        repo = Repository(settings.database_url)
        if args.status:
            print(
                json.dumps(
                    OnDemandController(repo, settings, config, provider=None).status(
                        fresh_ledger_only=True
                    )
                )
            )
            return 0
        if not config.enabled:
            raise ScalerError("ondemand_config_disabled")
        verify_sources(config)
        verify_identity_files(config)
        loader = (
            stdin_loader(config, sys.stdin.buffer)
            if args.credential_stdin
            else AwsLiumLoader(config.secret_arn, config.secret_version_id)
        )
        provider = LiumProvider(
            enabled=True,
            manifests=[LiumManifest(**v) for v in config.manifests],
            loader=loader,
            idle_probe=lambda tag, instance: controller.idle_probe(tag, instance),
            clock=repo.clock,
            journal_dir=config.work_dir / "rent-journal",
        )
        config.work_dir.mkdir(parents=True, exist_ok=True)
        with _slot_lock(config.work_dir, "on-demand-production-scaler") as acquired:
            if not acquired:
                raise ScalerError("ondemand_controller_already_running")
            controller = OnDemandController(repo, settings, config, provider=provider)
            controller.initialize()
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: controller.request_drain())
            last = None
            while True:
                try:
                    value = controller.tick()
                    compact = {
                        k: value.get(k)
                        for k in ("phase", "sequence", "drained", "billing_pending")
                    }
                    if compact != last:
                        print(json.dumps(compact), flush=True)
                        last = compact
                    if value.get("drained"):
                        return 0
                except Exception:
                    if not (
                        controller.current and controller.current.preparation_hold()
                    ):
                        controller.request_drain()
                    print(
                        json.dumps(
                            {"phase": "observation_unconfirmed", "drained": False}
                        ),
                        flush=True,
                    )
                time.sleep(config.interval_s)
    except Exception:
        print(
            json.dumps(
                {
                    "phase": "ondemand_configuration_or_recovery_required",
                    "drained": False,
                    "provider_calls_enabled": False,
                }
            )
        )
        return 1
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        for resource in (provider, loader, repo):
            if resource:
                try:
                    resource.close()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
