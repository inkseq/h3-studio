"""Fenced, durable scale coordination. No cloud adapter is configured by default.

Provider creation is single-attempt: an intent and create_started_at commit before
calling a provider. A crash in that gap is deliberately ambiguous, never a reason
to rent another instance. Only provider facts confirm stopped capacity/billing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Protocol
import uuid

from sqlalchemy import case, func, insert, or_, select, update

from .autoscale import ScalePolicy, ScaleState, recommend
from .control import WorkerControl
from .repository import (
    BudgetExceeded,
    Conflict,
    LeaseLost,
    NotFound,
    attempts,
    canonical,
    instance_intents,
    jobs,
    money,
    registered_workers,
    request_hash,
    scaler_actions,
    scaler_leaders,
    scaler_observations,
    scaler_receipts,
    manually_reviewed_inactive,
)

REMOVAL_CHECK_INTERVAL_SECONDS = 60


def _safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.:\-]{1,200}", value
    ):
        raise ValueError("invalid_scaler_identifier")
    return value


@dataclass(frozen=True)
class LaunchSpec:
    """Operator manifest references, never credentials, auth URLs or arbitrary JSON."""

    provider: str
    configuration_id: str
    model_id: str
    region: str = ""
    offer_id: str = ""
    image_id: str = ""

    def __post_init__(self):
        for key, value in asdict(self).items():
            if value or key in ("provider", "configuration_id", "model_id"):
                _safe_id(value)
        if self.provider in ("unknown", "mock", "local-cpu"):
            raise ValueError("explicit_cloud_provider_required")


@dataclass(frozen=True)
class ProviderFact:
    """Small verified facts; running describes a VM, never a qualified model slot.

    idle_confirmed must include a fresh dedicated inference-queue inspection.
    absence_confirmed is authoritative operation reconciliation, not an empty
    eventually-consistent search. No raw response or download URL is persisted.
    """

    state: str
    instance_id: str | None = None
    actual_cost_microusd: int | None = None
    idle_confirmed: bool = False
    idle_since: float | None = None
    absence_confirmed: bool = False
    provider_status: str | None = None
    preparation_stage: str | None = None

    def __post_init__(self):
        if self.state not in {
            "unknown",
            "not_created",
            "starting",
            "running",
            "destroyed",
        }:
            raise ValueError("invalid_provider_fact")
        if self.instance_id is not None:
            _safe_id(self.instance_id)
        if self.actual_cost_microusd is not None:
            money(self.actual_cost_microusd)
        if (
            type(self.idle_confirmed) is not bool
            or type(self.absence_confirmed) is not bool
        ):
            raise ValueError("invalid_provider_proof")
        if self.idle_since is not None and (
            not isinstance(self.idle_since, (int, float))
            or not math.isfinite(self.idle_since)
        ):
            raise ValueError("invalid_provider_idle_time")
        if (
            self.state in ("starting", "running", "destroyed")
            and self.instance_id is None
        ):
            raise ValueError("provider_instance_id_required")
        if self.state == "not_created" and (
            not self.absence_confirmed
            or self.instance_id is not None
            or self.actual_cost_microusd != 0
        ):
            raise ValueError("authoritative_not_created_proof_required")
        if self.provider_status not in (
            None,
            "PENDING",
            "FAILED",
            "STOPPED",
            "RUNNING",
        ) or self.preparation_stage not in (
            None,
            "provider_preparing",
            "configuring_ssh",
        ):
            raise ValueError("invalid_provider_preparation_fact")


class ProviderProtocol(Protocol):
    enabled: bool
    provider_id: str

    def create(
        self, tag: str, launch: LaunchSpec, *, hard_deadline: float
    ) -> ProviderFact: ...
    def reconcile(self, tag: str, instance_id: str | None) -> ProviderFact: ...
    def destroy(self, tag: str, instance_id: str) -> ProviderFact: ...
    def billing(self, tag: str, instance_id: str | None) -> int | None: ...


class CreationNotSubmitted(Exception):
    """Trusted adapter proof that this sole invocation never sent a create."""


class DisabledProvider:
    enabled = False
    provider_id = "disabled"

    def create(self, *args, **kwargs):
        raise Conflict("cloud_provider_disabled")

    reconcile = destroy = billing = create


@dataclass(frozen=True)
class LeaderLease:
    pool: str
    leader_id: str
    fence: int


class ScaleCoordinator:
    def create_manual_once(
        self,
        leader_id,
        scope,
        pool,
        intent_key,
        *,
        launch,
        policy,
        budget_account_ids,
        authorize,
        on_reserved=None,
        selected_offer=None,
    ):
        """Explicit operator capacity, not a synthetic generation/queue demand.

        ``authorize(connection)`` is a trusted, pure application guard. It checks
        the durable command, expiry and cumulative operating limits under the
        same global ledger lock. The committed intent/action is the only create
        barrier: re-entry returns it, even after a crash before the provider call.
        """
        _safe_id(intent_key)
        if not self.enabled or self.provider.enabled is not True:
            return {"state": "disabled"}
        if (
            not isinstance(launch, LaunchSpec)
            or not isinstance(policy, ScalePolicy)
            or not callable(authorize)
        ):
            raise ValueError("manual_capacity_contract_required")
        if policy.dry_run or launch.provider != self.provider.provider_id:
            raise ValueError("manual_capacity_provider_mismatch")
        if selected_offer is not None:
            if (
                not isinstance(selected_offer, dict)
                or selected_offer.get("provider") != launch.provider
                or not callable(
                    getattr(self.provider, "create_selected_for_intent", None)
                )
            ):
                raise Conflict("operator_exact_offer_unsupported")
            _safe_id(selected_offer.get("offer_id"))
        lease = self.acquire(pool, leader_id)
        if lease is None:
            return {"state": "not_leader"}
        with self.repo.transaction() as connection:
            self._leader(connection, lease)
            self.repo._lock_capacity(connection)
            existing = (
                connection.execute(
                    select(instance_intents).where(
                        instance_intents.c.pool == pool,
                        instance_intents.c.intent_key == intent_key,
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                action = (
                    connection.execute(
                        select(scaler_actions).where(
                            scaler_actions.c.intent_id == existing["id"]
                        )
                    )
                    .mappings()
                    .first()
                )
                if action is None or action["launch_spec"] != canonical(asdict(launch)):
                    raise Conflict("manual_capacity_existing_binding_mismatch")
                return {"state": "existing_intent", "intent_id": existing["id"]}
            if authorize(connection) is not True:
                raise Conflict("manual_capacity_authority_changed")
            validate = getattr(self.provider, "validate_launch", None)
            if validate is not None:
                validate(
                    launch,
                    physical_gpus=policy.new_instance_physical_gpus,
                    slots=policy.new_instance_slots,
                    reserved_cost_microusd=policy.instance_reservation_microusd,
                    hard_deadline=policy.hard_deadline,
                )
            intent = self.repo.reserve_instance_intent(
                scope,
                pool,
                intent_key,
                physical_gpus=policy.new_instance_physical_gpus,
                slots=policy.new_instance_slots,
                reserved_cost_microusd=policy.instance_reservation_microusd,
                hard_deadline=policy.hard_deadline,
                budget_account_ids=budget_account_ids,
                dry_run=False,
                provider=launch.provider,
                connection=connection,
            )
            now = self.repo.clock()
            connection.execute(
                insert(scaler_actions).values(
                    intent_id=intent["id"],
                    pool=pool,
                    launch_spec=canonical(asdict(launch)),
                    create_started_at=now,
                )
            )
            if self.preparation_timeout_s is not None:
                self._preparation_receipt(connection, intent, "awaiting_provider")
            self.repo.update_instance(intent["id"], "creating", connection=connection)
            if on_reserved is not None:
                on_reserved(connection, intent)

        def still_authorized():
            try:
                with self.repo.transaction() as connection:
                    self._leader(connection, lease)
                    self.repo._lock_capacity(connection)
                    return authorize(connection) is True
            except Exception:
                # This invocation proves it has not sent its sole provider call.
                # A pure guard failure is NOT an ambiguous network submission.
                return False

        extra = {"selected_offer": selected_offer} if selected_offer is not None else {}
        fact, observed_at = self._call(
            intent, "create", launch=launch, before_create=still_authorized, **extra
        )
        self._apply(lease, intent["id"], fact, observed_at)
        return {
            "state": "creation_observed",
            "intent_id": intent["id"],
            "provider_state": fact.state,
        }

    def observe_manual_instance(
        self, leader_id, intent_id, *, policy, drain=False, stop=False
    ):
        """Reuse normal reconciliation/destruction proofs; never force-kill work.

        Drain only closes worker admission. Stop additionally requests the
        existing safe destruction path; pending/unknown/collection obligations
        can keep it waiting. Neither command declares provider billing stopped.
        """
        with self.repo.engine.connect() as connection:
            row = (
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
            review = manually_reviewed_inactive(connection, row)
        if review:
            return {"state": "manually_reviewed", "intent_id": intent_id}
        lease = self.acquire(row["pool"], leader_id)
        if lease is None:
            return {"state": "not_leader"}
        if row["state"] == "destroyed":
            self.settle(intent_id)
            return {"state": "destroyed", "intent_id": intent_id}
        self._reconcile(lease, row)
        with self.repo.transaction() as connection:
            self._leader(connection, lease)
            self.repo._lock_capacity(connection)
            row = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            if drain or stop:
                connection.execute(
                    update(registered_workers)
                    .where(
                        registered_workers.c.provider == row["provider"],
                        registered_workers.c.instance_id == row["provider_instance_id"],
                        registered_workers.c.state != "retired",
                    )
                    .values(
                        drain_requested=1,
                        state="draining",
                        updated_at=self.repo.clock(),
                    )
                )
            if stop and row["state"] in {"starting", "ready", "busy"}:
                self.repo.update_instance(intent_id, "draining", connection=connection)
                row = {**row, "state": "draining"}
        self._drain_or_destroy(lease, row, policy)
        with self.repo.engine.connect() as connection:
            final = (
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
        if final["state"] == "destroyed":
            self.settle(intent_id)
        return {"state": final["state"], "intent_id": intent_id}

    def __init__(
        self,
        repository,
        *,
        provider=None,
        enabled=False,
        leader_seconds=60,
        min_observation_s=15,
        unsubmitted_retirement_guard=None,
        preparation_timeout_s=None,
        preparation_binding=None,
        unused_preparation_guard=None,
    ):
        if (
            type(enabled) is not bool
            or not math.isfinite(leader_seconds)
            or not 0 < leader_seconds <= 3600
            or not math.isfinite(min_observation_s)
            or not 0 < min_observation_s <= 3600
        ):
            raise ValueError("invalid_scaler_settings")
        self.repo, self.provider, self.enabled = (
            repository,
            provider or DisabledProvider(),
            enabled,
        )
        self.leader_seconds, self.min_observation_s = leader_seconds, min_observation_s
        self.unsubmitted_retirement_guard = unsubmitted_retirement_guard or (
            lambda conn, intent: False
        )
        if preparation_timeout_s is not None and (
            type(preparation_timeout_s) is not int
            or not 120 <= preparation_timeout_s <= 7200
            or not isinstance(preparation_binding, str)
            or not re.fullmatch(r"[0-9a-f]{64}", preparation_binding)
        ):
            raise ValueError("invalid_provider_preparation_policy")
        self.preparation_timeout_s, self.preparation_binding = (
            preparation_timeout_s,
            preparation_binding,
        )
        self.unused_preparation_guard = unused_preparation_guard or (
            lambda conn, intent: False
        )

    def preparation(self, connection, intent):
        """Read one irreversible lifecycle from the existing rental receipt ledger.

        No receipt means historical/unbound, not proof that bootstrap never ran.
        The action row serializes all phase changes; timestamps never reset age.
        """
        rows = list(
            connection.execute(
                select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.intent_id == intent["id"],
                    scaler_receipts.c.operation == "provider_preparation",
                )
            ).scalars()
        )
        if not rows:
            return None
        if self.preparation_timeout_s is None or len(rows) > 2:
            raise Conflict("provider_preparation_binding_mismatch")
        phases = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("version") != 1
                or row.get("intent_id") != intent["id"]
                or row.get("created_at") != intent["created_at"]
                or row.get("timeout_s") != self.preparation_timeout_s
                or row.get("binding") != self.preparation_binding
                or row.get("phase")
                not in {"awaiting_provider", "bootstrap_started", "retiring_unused"}
                or row["phase"] in phases
            ):
                raise Conflict("provider_preparation_receipt_invalid")
            phases[row["phase"]] = row
        if "awaiting_provider" not in phases:
            raise Conflict("provider_preparation_receipt_invalid")
        value = phases.get(
            "retiring_unused",
            phases.get("bootstrap_started", phases["awaiting_provider"]),
        )
        if (
            value["phase"] != "awaiting_provider"
            and value.get("instance_id") != intent["provider_instance_id"]
        ):
            raise Conflict("provider_preparation_instance_mismatch")
        return value

    def _preparation_receipt(self, connection, intent, phase, **extra):
        value = {
            "version": 1,
            "intent_id": intent["id"],
            "created_at": intent["created_at"],
            "timeout_s": self.preparation_timeout_s,
            "binding": self.preparation_binding,
            "phase": phase,
            **extra,
        }
        connection.execute(
            insert(scaler_receipts).values(
                id=str(uuid.uuid4()),
                intent_id=intent["id"],
                operation="provider_preparation",
                observed_at=self.repo.clock(),
                facts=canonical(value),
            )
        )
        return value

    def claim_bootstrap(self, lease, intent_id):
        """Fence preparation BEFORE any SSH/bootstrap side effect, including uploads."""
        if self.preparation_timeout_s is None:
            return True
        with self.repo.transaction() as connection:
            self._leader(connection, lease)
            self.repo._lock_capacity(connection)
            row = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            action = self.repo._locked(
                connection,
                select(scaler_actions).where(scaler_actions.c.intent_id == intent_id),
            )
            if row is None or action is None:
                raise Conflict("provider_preparation_intent_unconfirmed")
            phase = self.preparation(connection, row)
            if phase is None:
                raise Conflict("provider_preparation_legacy_adoption_required")
            if (
                row["pool"] != lease.pool
                or row["state"] not in {"starting", "ready", "busy"}
                or action["destroy_started_at"] is not None
                or phase["phase"] == "retiring_unused"
            ):
                return False
            if phase["phase"] == "bootstrap_started":
                return True
            now = self.repo.clock()
            fact = (
                ProviderFact(**action["last_observation"])
                if action["last_observation"]
                else ProviderFact("unknown")
            )
            if (
                action["last_observed_at"] is None
                or not 0 <= now - action["last_observed_at"] <= 30
                or fact.state != "running"
                or fact.instance_id != row["provider_instance_id"]
            ):
                return False
            self._preparation_receipt(
                connection,
                row,
                "bootstrap_started",
                instance_id=row["provider_instance_id"],
            )
            return True

    def _retire_unused_preparation(self, lease, intent):
        if self.preparation_timeout_s is None:
            return
        destroy = False
        with self.repo.transaction() as connection:
            self._leader(connection, lease)
            self.repo._lock_capacity(connection)
            row = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent["id"]),
            )
            action = self.repo._locked(
                connection,
                select(scaler_actions).where(scaler_actions.c.intent_id == row["id"]),
            )
            if action is None:
                return
            phase = self.preparation(connection, row)
            if (
                phase is None
                or phase["phase"] != "awaiting_provider"
                or row["state"] not in {"starting", "destroyed"}
                or not row["provider_instance_id"]
                or action["destroy_started_at"] is not None
            ):
                return
            now = self.repo.clock()
            fact = (
                ProviderFact(**action["last_observation"])
                if action["last_observation"]
                else ProviderFact("unknown")
            )
            fresh = (
                action["last_observed_at"] is not None
                and 0 <= now - action["last_observed_at"] <= 30
            )
            failed = fact.provider_status in {"FAILED", "STOPPED"}
            expired = (
                fact.provider_status == "PENDING"
                and now - row["created_at"] >= self.preparation_timeout_s
            )
            # A provider TTL can remove an actual pod before our timeout. Its
            # immutable terminal fact still needs the positive unused barrier;
            # it permits backlog preservation, never another DELETE.
            removed = (
                row["state"] == "destroyed"
                and fact.state == "destroyed"
                and action["last_observed_at"] is not None
                and action["last_observed_at"] <= now
            )
            if (
                fact.instance_id != row["provider_instance_id"]
                or not removed
                and (not fresh or fact.state != "starting" or not (failed or expired))
            ):
                return
            if connection.execute(
                select(registered_workers.c.id).where(
                    registered_workers.c.provider == row["provider"],
                    registered_workers.c.instance_id == row["provider_instance_id"],
                )
            ).first():
                return
            # Application guard additionally proves exact cycle scope, no attempts,
            # and no runtime evidence. Status PENDING/STOPPED alone is never idle.
            if self.unused_preparation_guard(connection, dict(row)) is not True:
                return
            reason = (
                "provider_preparation_failed"
                if failed or removed
                else "provider_preparation_timeout"
            )
            self._preparation_receipt(
                connection,
                row,
                "retiring_unused",
                instance_id=row["provider_instance_id"],
                reason=reason,
                provider_status=fact.provider_status,
                observed_at=action["last_observed_at"],
            )
            if not removed:
                self.repo.update_instance(row["id"], "draining", connection=connection)
                self.repo.update_instance(
                    row["id"], "destroying", connection=connection
                )
                connection.execute(
                    update(scaler_actions)
                    .where(scaler_actions.c.intent_id == row["id"])
                    .values(destroy_started_at=now)
                )
                destroy = True
        if destroy:
            fact, at = self._call(intent, "destroy")
            self._apply(lease, intent["id"], fact, at)

    def acquire(self, pool, leader_id):
        """CAS leader; an expired lease grants a new fence, never frees capacity."""
        _safe_id(pool), _safe_id(leader_id)
        if not self.enabled:
            return None
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        with self.repo.transaction() as connection:
            put = (
                sqlite_insert
                if self.repo.engine.dialect.name == "sqlite"
                else pg_insert
            )
            connection.execute(
                put(scaler_leaders)
                .values(
                    pool=pool,
                    leader_id=leader_id,
                    fence=0,
                    expires_at=0,
                    consecutive_breaches=0,
                    sequence=0,
                )
                .on_conflict_do_nothing(index_elements=["pool"])
            )
            row = self.repo._locked(
                connection, select(scaler_leaders).where(scaler_leaders.c.pool == pool)
            )
            now = self.repo.clock()
            if row["expires_at"] > now and row["leader_id"] != leader_id:
                return None
            fence = row["fence"] + (1 if row["expires_at"] <= now else 0)
            connection.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == pool)
                .values(
                    leader_id=leader_id,
                    fence=fence,
                    expires_at=now + self.leader_seconds,
                )
            )
            return LeaderLease(pool, leader_id, fence)

    def _leader(self, connection, lease):
        row = self.repo._locked(
            connection,
            select(scaler_leaders).where(scaler_leaders.c.pool == lease.pool),
        )
        if (
            row is None
            or row["leader_id"] != lease.leader_id
            or row["fence"] != lease.fence
            or row["expires_at"] <= self.repo.clock()
        ):
            raise LeaseLost("scaler_leader_lease_lost")
        return row

    def _active(self, pool):
        return [
            row
            for row in self.repo.list_instance_intents(pool=pool)
            if row["state"] != "destroyed" and not row.get("manual_review")
        ]

    def _action(self, intent_id):
        with self.repo.engine.connect() as connection:
            row = (
                connection.execute(
                    select(scaler_actions).where(
                        scaler_actions.c.intent_id == intent_id
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def _receipt(self, intent_id, operation, fact):
        # A stale leader may record a response fact, but cannot apply it or begin
        # another operation. The next leader consumes the receipt/reconciles it.
        if not isinstance(fact, ProviderFact):
            fact = ProviderFact("unknown")
        now = self.repo.clock()
        with self.repo.transaction() as connection:
            connection.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=intent_id,
                    operation=operation,
                    observed_at=now,
                    facts=canonical(self._fact_payload(fact)),
                )
            )
        return fact, now

    def _fact_payload(self, fact):
        value = asdict(fact)
        for key in ("provider_status", "preparation_stage"):
            if self.preparation_timeout_s is None or value[key] is None:
                value.pop(key)
        return value

    def _call(
        self, intent, operation, *, launch=None, before_create=None, selected_offer=None
    ):
        tag = intent["id"]
        if getattr(self.provider, "provider_id", None) != intent["provider"]:
            raise Conflict("scaler_provider_identity_mismatch")
        try:
            if operation == "create":
                if (
                    intent["hard_deadline"] <= self.repo.clock()
                    or before_create is not None
                    and before_create() is not True
                ):
                    # This process proves it has not issued this sole create
                    # call. Passing an already expired TTL to a provider is unsafe.
                    fact = ProviderFact(
                        "not_created", actual_cost_microusd=0, absence_confirmed=True
                    )
                else:
                    bound_create = getattr(self.provider, "create_for_intent", None)
                    if selected_offer is not None:
                        fact = self.provider.create_selected_for_intent(
                            tag,
                            launch,
                            selected_offer=selected_offer,
                            hard_deadline=intent["hard_deadline"],
                            intent_created_at=intent["created_at"],
                        )
                    elif callable(bound_create):
                        fact = bound_create(
                            tag,
                            launch,
                            hard_deadline=intent["hard_deadline"],
                            intent_created_at=intent["created_at"],
                        )
                    else:
                        fact = self.provider.create(
                            tag, launch, hard_deadline=intent["hard_deadline"]
                        )
            elif operation == "destroy":
                fact = self.provider.destroy(tag, intent["provider_instance_id"])
            else:
                fact = self.provider.reconcile(tag, intent["provider_instance_id"])
        except CreationNotSubmitted:
            fact = (
                ProviderFact(
                    "not_created", actual_cost_microusd=0, absence_confirmed=True
                )
                if operation == "create"
                else ProviderFact("unknown")
            )
        except Exception:
            fact = ProviderFact("unknown")
        return self._receipt(tag, operation, fact)

    def _apply(self, lease, intent_id, fact, observed_at):
        with self.repo.transaction() as connection:
            self._leader(connection, lease)
            row = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            if row is None or row["pool"] != lease.pool:
                raise NotFound("instance_intent_not_found")
            if fact.instance_id is not None and row["provider_instance_id"] not in (
                None,
                fact.instance_id,
            ):
                raise Conflict("provider_instance_conflict")
            if manually_reviewed_inactive(connection, row):
                return  # An in-flight provider response cannot undo human closure or settle an estimate.
            if row["state"] == "destroyed":
                if fact.actual_cost_microusd is not None:
                    self.repo._settle(
                        connection, "instance", intent_id, fact.actual_cost_microusd
                    )
            elif fact.state == "not_created":
                if row["provider_instance_id"] is not None or row["state"] not in (
                    "creating",
                    "creation_unknown",
                ):
                    raise Conflict("not_created_fact_conflict")
                self.repo.update_instance(
                    intent_id,
                    "destroyed",
                    destruction_confirmed=True,
                    actual_cost_microusd=0,
                    connection=connection,
                )
            elif fact.state == "destroyed":
                # Independent provider TTL may stop a host. Record physical facts
                # even when its job remains unresolved; device ownership stays held.
                if row["state"] not in (
                    "creating",
                    "creation_unknown",
                    "destroying",
                    "reserved",
                ):
                    connection.execute(
                        update(instance_intents)
                        .where(instance_intents.c.id == intent_id)
                        .values(state="destroying")
                    )
                self.repo.update_instance(
                    intent_id,
                    "destroyed",
                    provider_instance_id=fact.instance_id,
                    destruction_confirmed=True,
                    actual_cost_microusd=fact.actual_cost_microusd,
                    connection=connection,
                )
            elif fact.state in ("starting", "running") and row["state"] in (
                "creating",
                "creation_unknown",
            ):
                self.repo.update_instance(
                    intent_id,
                    "starting",
                    provider_instance_id=fact.instance_id,
                    connection=connection,
                )
            elif fact.state == "unknown" and row["state"] == "creating":
                self.repo.update_instance(
                    intent_id, "creation_unknown", connection=connection
                )
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == intent_id)
                .values(
                    last_observation=canonical(self._fact_payload(fact)),
                    last_observed_at=observed_at,
                )
            )
        self._retire_confirmed_idle(intent_id)

    def _retire_confirmed_idle(self, intent_id):
        with self.repo.engine.connect() as connection:
            row = (
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
            if row["state"] != "destroyed" or not row["provider_instance_id"]:
                return
            workers = list(
                connection.execute(
                    select(registered_workers).where(
                        registered_workers.c.provider == row["provider"],
                        registered_workers.c.instance_id == row["provider_instance_id"],
                    )
                ).mappings()
            )
        control = WorkerControl(self.repo)
        for worker in workers:
            if worker["state"] != "retired" and worker["current_job_id"] is None:
                control.retire(worker["id"], upstream_idle_confirmed=True)

    def _reconcile(self, lease, intent):
        # Preserve a late success response before consulting an eventually-
        # consistent search; otherwise a handoff could lose the only known ID.
        with self.repo.engine.connect() as connection:
            state = scaler_receipts.c.facts["state"].as_string()
            wanted = (
                ("destroyed", "not_created")
                if intent["provider_instance_id"]
                else ("destroyed", "not_created", "starting", "running")
            )
            receipt = (
                connection.execute(
                    select(scaler_receipts)
                    .where(
                        scaler_receipts.c.intent_id == intent["id"],
                        state.in_(wanted),
                        scaler_receipts.c.operation.in_(
                            ("create", "reconcile", "destroy")
                        ),
                    )
                    .order_by(
                        case(
                            (state == "destroyed", 2),
                            (state == "not_created", 1),
                            else_=0,
                        ).desc(),
                        scaler_receipts.c.observed_at.desc(),
                        scaler_receipts.c.id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if receipt:
            # Consume only the strongest outstanding fact. Replaying every old
            # VM-running receipt each turn would grow DB work without bound.
            fact = ProviderFact(**receipt["facts"])
            self._apply(lease, intent["id"], fact, receipt["observed_at"])
        current = next(
            row
            for row in self.repo.list_instance_intents(pool=lease.pool)
            if row["id"] == intent["id"]
        )
        if current.get("manual_review"):
            return
        if current["state"] == "destroyed":
            return
        if current["state"] == "destroying" and not self._claim_removal_check(
            lease, current["id"]
        ):
            return
        fact, at = self._call(current, "reconcile")
        self._apply(lease, intent["id"], fact, at)

    def _claim_removal_check(self, lease, intent_id):
        """Persist the per-node cadence before I/O; a restart cannot poll again.

        A scheduling receipt is not a provider observation or deletion proof.
        Late terminal receipts are consumed above even during this cooldown.
        """
        with self.repo.transaction() as connection:
            self._leader(connection, lease)
            row = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            if row["state"] != "destroying":
                return False
            action = self.repo._locked(
                connection,
                select(scaler_actions).where(scaler_actions.c.intent_id == intent_id),
            )
            last_check = connection.execute(
                select(func.max(scaler_receipts.c.observed_at)).where(
                    scaler_receipts.c.intent_id == intent_id,
                    scaler_receipts.c.operation == "removal_check_started",
                )
            ).scalar_one()
            times = [
                value
                for value in (
                    last_check,
                    action["last_observed_at"],
                    action["destroy_started_at"],
                )
                if value is not None
            ]
            now = self.repo.clock()
            if times and now < max(times) + REMOVAL_CHECK_INTERVAL_SECONDS:
                return False
            connection.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=intent_id,
                    operation="removal_check_started",
                    observed_at=now,
                    facts=canonical({"state": "unknown"}),
                )
            )
            return True

    def _drain_or_destroy(self, lease, intent, policy):
        destroy = False
        with self.repo.transaction() as connection:
            self._leader(connection, lease)
            # Admission takes this same row lock before inserting/enqueueing.
            # A concurrent new job either keeps this instance alive, or sees
            # its committed drain and gets an explicit re-preflight conflict.
            self.repo._lock_capacity(connection)
            row = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent["id"]),
            )
            if row["state"] in (
                "destroyed",
                "destroying",
                "creating",
                "creation_unknown",
                "reserved",
            ):
                return
            action = self.repo._locked(
                connection,
                select(scaler_actions).where(scaler_actions.c.intent_id == row["id"]),
            )
            if action is None:
                return
            now = self.repo.clock()
            fact = (
                ProviderFact(**action["last_observation"])
                if action["last_observation"]
                else ProviderFact("unknown")
            )
            fresh = (
                action["last_observed_at"] is not None
                and 0 <= now - action["last_observed_at"] <= 30
            )
            idle = (
                fresh
                and fact.idle_confirmed
                and fact.state == "running"
                and fact.instance_id == row["provider_instance_id"]
            )
            ttl_due = row["hard_deadline"] <= now
            workers = list(
                connection.execute(
                    select(registered_workers)
                    .where(
                        registered_workers.c.provider == row["provider"],
                        registered_workers.c.instance_id == row["provider_instance_id"],
                    )
                    .order_by(registered_workers.c.id)
                    .with_for_update()
                ).mappings()
            )
            # A stale/dead worker is not an idle proof, even if VM state is running.
            blocked = any(
                w["current_job_id"]
                or w["state"] != "retired"
                and w["expires_at"] <= now
                for w in workers
            )
            if workers:
                related = connection.execute(
                    select(jobs.c.status)
                    .join(attempts, attempts.c.job_id == jobs.c.id)
                    .where(
                        attempts.c.worker_id.in_([w["id"] for w in workers]),
                        jobs.c.status.not_in(("succeeded", "failed", "cancelled")),
                        or_(
                            jobs.c.status != "queued",
                            attempts.c.submission_started_at.is_not(None),
                            attempts.c.upstream_task_id.is_not(None),
                        ),
                    )
                ).first()
                blocked = blocked or related is not None
            # The provider's idle_since can precede a lengthy download/decode
            # or pending queue. Start our durable timer only after the entire
            # business queue is clear; fresh work resets it. Planned/blocked
            # drafts have not been admitted and do not keep a GPU rented.
            pending = connection.execute(
                select(jobs.c.id)
                .where(
                    jobs.c.pool == row["pool"],
                    jobs.c.status.not_in(
                        ("succeeded", "failed", "cancelled", "planned", "blocked")
                    ),
                )
                .limit(1)
            ).first()
            latest_activity = connection.execute(
                select(func.max(jobs.c.updated_at)).where(
                    jobs.c.pool == row["pool"],
                    jobs.c.status.not_in(("planned", "blocked")),
                )
            ).scalar_one()
            application_idle_since = action["application_idle_since"]
            if not idle or blocked or pending is not None:
                application_idle_since = None
            elif (
                application_idle_since is None
                or application_idle_since > now
                or latest_activity is not None
                and latest_activity > application_idle_since
            ):
                # A fast job may be admitted and finished between controller
                # polls. Its committed timestamp still restarts the full wait.
                application_idle_since = now
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == row["id"])
                .values(application_idle_since=application_idle_since)
            )
            idle_due = (
                application_idle_since is not None
                and now - application_idle_since >= policy.idle_before_drain_s
            )
            if row["state"] != "draining" and not (ttl_due or idle_due):
                return
            connection.execute(
                update(registered_workers)
                .where(
                    registered_workers.c.provider == row["provider"],
                    registered_workers.c.instance_id == row["provider_instance_id"],
                    registered_workers.c.state != "retired",
                )
                .values(drain_requested=1, state="draining", updated_at=now)
            )
            if row["state"] != "draining":
                self.repo.update_instance(row["id"], "draining", connection=connection)
            if not ttl_due and pending is not None:
                # Preparation recovery can retire a demonstrably unused GPU
                # while keeping the original accepted backlog. Default deny;
                # this does not replace the fresh idle/attempt/worker proof.
                if self.unsubmitted_retirement_guard(connection, dict(row)) is not True:
                    return
            if not idle or blocked or action["destroy_started_at"] is not None:
                return
            self.repo.update_instance(row["id"], "destroying", connection=connection)
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == row["id"])
                .values(destroy_started_at=now)
            )
            destroy = True
        if destroy:
            fact, at = self._call(intent, "destroy")
            self._apply(lease, intent["id"], fact, at)

    def settle(self, intent_id):
        """Optional invoice poll; no live instance termination or duplicate spend."""
        if not self.enabled or self.provider.enabled is not True:
            return {"state": "disabled"}
        row = next(
            (
                row
                for row in self.repo.list_instance_intents()
                if row["id"] == intent_id
            ),
            None,
        )
        if row is None or self._action(intent_id) is None:
            raise NotFound("instance_intent_not_found")
        if row["state"] != "destroyed":
            raise Conflict("instance_not_destroyed")
        if getattr(self.provider, "provider_id", None) != row["provider"]:
            raise Conflict("scaler_provider_identity_mismatch")
        try:
            actual = self.provider.billing(intent_id, row["provider_instance_id"])
        except Exception:
            actual = None
        if actual is None:
            return {"intent_id": intent_id, "billing_status": "pending"}
        result = self.repo.settle_instance_cost(intent_id, actual_cost_microusd=actual)
        return {"intent_id": intent_id, "billing_status": result["billing_status"]}

    def tick(
        self,
        leader_id,
        scope,
        pool,
        demands,
        slots,
        *,
        policy=ScalePolicy(),
        launch=None,
        budget_account_ids=(),
        on_intent_reserved=None,
        before_create=None,
    ):
        """One bounded control turn; explicit caller-owned observations, not user payload.

        This method never supplies/changes capacity or budget approvals. All paid
        creation paths atomically use Repository's existing cross-pool ledger.
        """
        if not self.enabled:
            return {"state": "disabled"}
        if len(demands) > 4096 or len(slots) > 128:
            raise ValueError("scaler_observation_limit")
        for demand in demands:
            _safe_id(demand.job_id), _safe_id(demand.owner_id)
        for slot in slots:
            _safe_id(slot.worker_id)
        lease = self.acquire(pool, leader_id)
        if lease is None:
            return {"state": "not_leader"}
        try:
            if not policy.dry_run and self.provider.enabled is True:
                managed = [
                    intent
                    for intent in self._active(pool)
                    if self._action(intent["id"])
                ]
                if any(
                    intent["provider"] != getattr(self.provider, "provider_id", None)
                    for intent in managed
                ):
                    return {"state": "blocked", "reason": "provider_identity_mismatch"}
                for intent in managed:
                    if self._action(intent["id"]):
                        self._reconcile(lease, intent)
                for intent in self._active(pool):
                    self._retire_unused_preparation(lease, intent)
                if self.preparation_timeout_s is not None:
                    with self.repo.engine.connect() as connection:
                        removed = list(
                            connection.execute(
                                select(instance_intents)
                                .join(
                                    scaler_receipts,
                                    scaler_receipts.c.intent_id
                                    == instance_intents.c.id,
                                )
                                .where(
                                    instance_intents.c.pool == pool,
                                    instance_intents.c.state == "destroyed",
                                    instance_intents.c.provider_instance_id.is_not(
                                        None
                                    ),
                                    scaler_receipts.c.operation
                                    == "provider_preparation",
                                    scaler_receipts.c.facts["binding"].as_string()
                                    == self.preparation_binding,
                                    scaler_receipts.c.facts["phase"].as_string()
                                    == "awaiting_provider",
                                )
                            ).mappings()
                        )
                    for intent in removed:
                        self._retire_unused_preparation(lease, intent)
                for intent in self._active(pool):
                    self._drain_or_destroy(lease, intent, policy)
                # This cycle owns one preparation attempt. A fresh service cycle
                # may replace it only after its caller verifies removal/billing.
                with self.repo.engine.connect() as connection:
                    retiring = connection.execute(
                        select(scaler_receipts.c.intent_id)
                        .join(
                            instance_intents,
                            instance_intents.c.id == scaler_receipts.c.intent_id,
                        )
                        .where(
                            instance_intents.c.pool == pool,
                            scaler_receipts.c.operation == "provider_preparation",
                            scaler_receipts.c.facts["binding"].as_string()
                            == self.preparation_binding,
                            scaler_receipts.c.facts["phase"].as_string()
                            == "retiring_unused",
                        )
                    ).first()
                if retiring:
                    return {"state": "provider_preparation_retiring"}
            active = self._active(pool)
            spec = asdict(launch) if launch else None
            digest = request_hash(
                {
                    "policy": asdict(policy),
                    "launch": spec,
                    "budget_account_ids": sorted(set(budget_account_ids)),
                    "scope": asdict(scope),
                }
            )
            snapshot = canonical(
                {
                    "demands": [asdict(d) for d in demands],
                    "slots": [asdict(s) for s in slots],
                    "active_intent_ids": [r["id"] for r in active],
                }
            )
            availability_reason = None
            availability = getattr(self.provider, "preflight_availability", None)
            if (
                availability
                and demands
                and not active
                and launch is not None
                and not policy.dry_run
                and self.provider.enabled is True
            ):
                # HTTP must remain outside the ledger lock. The transaction
                # below rechecks leader and capacity after this bounded probe.
                try:
                    validate = getattr(self.provider, "validate_launch", None)
                    if validate is not None:
                        validate(
                            launch,
                            physical_gpus=policy.new_instance_physical_gpus,
                            slots=policy.new_instance_slots,
                            reserved_cost_microusd=policy.instance_reservation_microusd,
                            hard_deadline=policy.hard_deadline,
                        )
                    availability_reason = availability(launch)
                    if availability_reason not in (
                        None,
                        "provider_inventory_unavailable",
                        "provider_inventory_unconfirmed",
                    ):
                        availability_reason = "provider_inventory_unconfirmed"
                except Exception:
                    availability_reason = "provider_inventory_unconfirmed"
            with self.repo.transaction() as connection:
                leader = self._leader(connection, lease)
                now = self.repo.clock()
                if (
                    leader["policy_hash"] == digest
                    and leader["last_observed_at"] is not None
                    and now - leader["last_observed_at"] < self.min_observation_s
                ):
                    return {"state": "observation_throttled"}
                prior = ScaleState(
                    leader["consecutive_breaches"]
                    if leader["policy_hash"] == digest
                    else 0,
                    leader["last_scale_at"],
                )
                decision = recommend(
                    demands, slots, active, now=now, policy=policy, state=prior
                )
                sequence = leader["sequence"] + 1
                connection.execute(
                    insert(scaler_observations).values(
                        pool=pool,
                        sequence=sequence,
                        observed_at=now,
                        snapshot=snapshot,
                        recommendation=canonical(asdict(decision)),
                    )
                )
                connection.execute(
                    update(scaler_leaders)
                    .where(scaler_leaders.c.pool == pool)
                    .values(
                        policy_hash=digest,
                        consecutive_breaches=decision.state.consecutive_breaches,
                        sequence=sequence,
                        last_observed_at=now,
                    )
                )
                if decision.action != "propose":
                    return {
                        "state": decision.action,
                        "reason": decision.reason,
                        "observation": sequence,
                    }
                if self.provider.enabled is not True or launch is None:
                    return {
                        "state": "blocked",
                        "reason": "provider_or_launch_not_configured",
                        "observation": sequence,
                    }
                if launch.provider != getattr(self.provider, "provider_id", None):
                    return {
                        "state": "blocked",
                        "reason": "provider_identity_mismatch",
                        "observation": sequence,
                    }
                if any(
                    row["state"] in ("reserved", "creating", "creation_unknown")
                    for row in active
                ):
                    return {
                        "state": "blocked",
                        "reason": "creation_needs_reconciliation",
                        "observation": sequence,
                    }
                # Provider-specific approved manifests must not undercount
                # real GPUs/slots/TTL budget. This optional hook is PURE: no
                # key loading/network/side effects inside the ledger lock.
                validate_launch = getattr(self.provider, "validate_launch", None)
                if validate_launch is not None:
                    try:
                        validate_launch(
                            launch,
                            physical_gpus=policy.new_instance_physical_gpus,
                            slots=policy.new_instance_slots,
                            reserved_cost_microusd=policy.instance_reservation_microusd,
                            hard_deadline=policy.hard_deadline,
                        )
                    except Exception:
                        return {
                            "state": "blocked",
                            "reason": "provider_manifest_or_reservation_mismatch",
                            "observation": sequence,
                        }
                if availability_reason:
                    return {
                        "state": "blocked",
                        "reason": availability_reason,
                        "observation": sequence,
                        "retry_after_seconds": 60,
                    }
                intent = self.repo.reserve_instance_intent(
                    scope,
                    pool,
                    f"scaler-{sequence}",
                    physical_gpus=policy.new_instance_physical_gpus,
                    slots=policy.new_instance_slots,
                    reserved_cost_microusd=policy.instance_reservation_microusd,
                    hard_deadline=policy.hard_deadline,
                    budget_account_ids=budget_account_ids,
                    dry_run=False,
                    provider=launch.provider,
                    connection=connection,
                )
                connection.execute(
                    insert(scaler_actions).values(
                        intent_id=intent["id"],
                        pool=pool,
                        launch_spec=canonical(spec),
                        create_started_at=now,
                    )
                )
                if self.preparation_timeout_s is not None:
                    self._preparation_receipt(connection, intent, "awaiting_provider")
                self.repo.update_instance(
                    intent["id"], "creating", connection=connection
                )
                # Optional pure application linkage commits with the sole
                # create intent and reservations. Never call a provider here.
                if on_intent_reserved is not None:
                    on_intent_reserved(connection, intent)
                connection.execute(
                    update(scaler_leaders)
                    .where(scaler_leaders.c.pool == pool)
                    .values(last_scale_at=now)
                )
            fact, at = self._call(
                intent, "create", launch=launch, before_create=before_create
            )
            self._apply(lease, intent["id"], fact, at)
            return {
                "state": "creation_observed",
                "intent_id": intent["id"],
                "provider_state": fact.state,
            }
        except LeaseLost:
            return {"state": "leader_changed_reconcile_required"}
        except BudgetExceeded:
            return {"state": "blocked", "reason": "ledger_capacity_or_budget_limit"}
        except Conflict:
            return {"state": "blocked", "reason": "capacity_approval_or_cycle_conflict"}
