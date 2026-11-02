"""Original capacity ledger methods on the composed Repository transaction owner."""

from __future__ import annotations
from contextlib import nullcontext
import math
import uuid
from sqlalchemy import (
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from .providers.registry import DSTACK_V1_PROVIDERS

from .repository_schema import (
    NotFound,
    Conflict,
    BudgetExceeded,
    InvalidTransition,
    request_hash,
    money,
    identifier,
    budget_reservations,
    pool_limits,
    instance_intents,
    capacity_gate,
    registered_devices,
    scaler_receipts,
    capacity_approvals,
)


class CapacityLedger:
    def approve_capacity(self, approval_id, **kwargs):
        """Trusted operator only; creates an immutable approval, not a cloud instance."""
        from .capacity import approve_capacity

        return approve_capacity(self, approval_id, **kwargs)

    def reserve_capacity_member(self, approval_id, member_id, *, connection=None):
        from .capacity import reserve_capacity_member

        return reserve_capacity_member(
            self, approval_id, member_id, connection=connection
        )

    def set_capacity_approval_enabled(self, approval_id, *, enabled):
        if type(enabled) is not bool:
            raise ValueError("invalid_capacity_approval_switch")
        with self.transaction() as connection:
            row = self._locked(
                connection,
                select(capacity_approvals).where(
                    capacity_approvals.c.id == approval_id
                ),
            )
            if row is None:
                raise NotFound("capacity_approval_not_found")
            if enabled and row["expires_at"] <= self.clock():
                raise Conflict("capacity_approval_expired")
            connection.execute(
                update(capacity_approvals)
                .where(capacity_approvals.c.id == approval_id)
                .values(enabled=int(enabled))
            )

    def find_capacity_approval(
        self, scope, *, pool, model_id, configuration_id, recipe_id, policy_hash
    ):
        from .capacity import find_capacity_approval

        return find_capacity_approval(
            self,
            scope,
            pool=pool,
            model_id=model_id,
            configuration_id=configuration_id,
            recipe_id=recipe_id,
            policy_hash=policy_hash,
        )

    def configure_pool(self, pool, *, max_instances=0, max_physical_gpus=0):
        identifier(pool)
        if (
            type(max_instances) is not int
            or type(max_physical_gpus) is not int
            or min(max_instances, max_physical_gpus) < 0
        ):
            raise ValueError("invalid_capacity_limit")
        with self.transaction() as connection:
            row = self._locked(
                connection, select(pool_limits).where(pool_limits.c.pool == pool)
            )
            if row:
                connection.execute(
                    update(pool_limits)
                    .where(pool_limits.c.pool == pool)
                    .values(
                        max_instances=max_instances, max_physical_gpus=max_physical_gpus
                    )
                )
            else:
                connection.execute(
                    insert(pool_limits).values(
                        pool=pool,
                        max_instances=max_instances,
                        max_physical_gpus=max_physical_gpus,
                    )
                )

    def configure_capacity(self, *, max_instances=0, max_physical_gpus=0):
        """Explicit approved global ceiling; does not create or release any GPU."""
        if (
            type(max_instances) is not int
            or type(max_physical_gpus) is not int
            or min(max_instances, max_physical_gpus) < 0
        ):
            raise ValueError("invalid_capacity_limit")
        with self.transaction() as connection:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            put = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
            connection.execute(
                put(capacity_gate)
                .values(id="global", max_instances=0, max_physical_gpus=0)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            self._locked(
                connection, select(capacity_gate).where(capacity_gate.c.id == "global")
            )
            connection.execute(
                update(capacity_gate)
                .where(capacity_gate.c.id == "global")
                .values(
                    max_instances=max_instances, max_physical_gpus=max_physical_gpus
                )
            )

    def _global_usage(self, connection):
        resources = {}
        for row in connection.execute(
            select(instance_intents).where(instance_intents.c.state != "destroyed")
        ).mappings():
            if manually_reviewed_inactive(connection, row):
                continue
            key = (
                row["provider"],
                row["provider_instance_id"] or "intent:" + row["id"],
            )
            resources[key] = resources.get(key, 0) + row["physical_gpus"]
        device_counts = {}
        for row in connection.execute(
            select(registered_devices).where(
                registered_devices.c.state != "released",
                registered_devices.c.provider != "mock",
            )
        ).mappings():
            key = (row["provider"], row["instance_id"])
            device_counts[key] = device_counts.get(key, 0) + 1
        for key, count in device_counts.items():
            resources[key] = max(resources.get(key, 0), count)
        return {
            "instances": len(resources),
            "physical_gpus": sum(resources.values()),
            "resources": resources,
        }

    def _lock_capacity(self, connection):
        row = self._locked(
            connection, select(capacity_gate).where(capacity_gate.c.id == "global")
        )
        if row is None:
            raise BudgetExceeded("global_capacity_disabled")
        return row

    def reserve_instance_intent(
        self,
        scope,
        pool,
        intent_key,
        *,
        physical_gpus=1,
        slots=1,
        reserved_cost_microusd=0,
        hard_deadline,
        budget_account_ids=(),
        dry_run=True,
        provider="unknown",
        connection=None,
        continuing=False,
        capacity_usage=None,
        active_instance_ids=None,
        capacity_policy_snapshot=None,
        capacity_provenance=None,
    ):
        """Reserve an intent, NEVER create an instance. Default dry-run makes no writes."""
        identifier(pool)
        identifier(intent_key)
        identifier(provider)
        cost = money(reserved_cost_microusd)
        if (
            type(physical_gpus) is not int
            or type(slots) is not int
            or min(physical_gpus, slots) < 1
        ):
            raise ValueError("invalid_instance_capacity")
        if type(continuing) is not bool or (
            continuing
            and (
                provider not in DSTACK_V1_PROVIDERS
                or hard_deadline is not None
                or capacity_usage is None
            )
        ):
            raise ValueError("invalid_continuing_capacity")
        if not continuing and (
            not isinstance(hard_deadline, (int, float))
            or not math.isfinite(hard_deadline)
        ):
            raise ValueError("invalid_instance_deadline")
        if hard_deadline is not None and hard_deadline <= self.clock():
            raise Conflict("instance_deadline_expired")
        admitted_capacity = None
        if capacity_policy_snapshot is not None or capacity_provenance is not None:
            from .capacity_settings import reservation_provenance

            admitted_capacity = reservation_provenance(
                capacity_policy_snapshot,
                capacity_provenance,
                scope,
                pool,
                provider,
                continuing=continuing,
                gpus=physical_gpus,
                slots=slots,
                usage=capacity_usage,
            )
        request = dict(
            physical_gpus=physical_gpus,
            slots=slots,
            reserved_cost_microusd=cost,
            hard_deadline=hard_deadline,
            budget_account_ids=sorted(set(budget_account_ids)),
            scope=scope.__dict__,
            provider=provider,
        )
        if admitted_capacity is not None:
            request.update(
                capacity_policy_snapshot=capacity_policy_snapshot,
                capacity_provenance=capacity_provenance,
            )
        digest = request_hash(request)
        if dry_run:
            return {
                "dry_run": True,
                "created": False,
                "pool": pool,
                "intent_key": intent_key,
            }
        if not cost or not budget_account_ids:
            raise BudgetExceeded("instance_budget_not_configured")
        try:
            with (
                self.transaction()
                if connection is None
                else nullcontext(connection) as connection
            ):
                # All controllers serialize capacity reservations on the same durable pool row.
                global_limits = self._lock_capacity(connection)
                limits = self._locked(
                    connection, select(pool_limits).where(pool_limits.c.pool == pool)
                )
                if limits is None:
                    raise BudgetExceeded("pool_disabled")
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
                if existing:
                    if existing["request_hash"] != digest:
                        raise Conflict("instance_intent_conflict")
                    return {**dict(existing), "created": False, "dry_run": False}
                if admitted_capacity is not None:
                    from .capacity_settings import admission_snapshot, read_policy

                    if capacity_policy_snapshot != admission_snapshot(
                        read_policy(connection, scope.tenant_id)
                    ):
                        raise Conflict("capacity_policy_snapshot_changed")
                    # Adopted operating counts replace old positive numeric
                    # ceilings, never the explicit global/pool disabled gates.
                    if (
                        min(
                            global_limits["max_instances"],
                            global_limits["max_physical_gpus"],
                        )
                        <= 0
                    ):
                        raise BudgetExceeded("global_capacity_disabled")
                    if min(limits["max_instances"], limits["max_physical_gpus"]) <= 0:
                        raise BudgetExceeded("pool_disabled")
                global_max_instances = (
                    admitted_capacity["max_machines"]
                    if admitted_capacity is not None
                    else global_limits["max_instances"]
                )
                global_max_gpus = (
                    admitted_capacity["max_machines"]
                    if admitted_capacity is not None
                    else global_limits["max_physical_gpus"]
                )
                usage = (
                    self._global_usage(connection)
                    if capacity_usage is None
                    else capacity_usage
                )
                if any(
                    type(usage.get(k)) is not int or usage[k] < 0
                    for k in ("instances", "physical_gpus")
                ):
                    raise ValueError("invalid_capacity_observation")
                if (
                    usage["instances"] + 1 > global_max_instances
                    or usage["physical_gpus"] + physical_gpus > global_max_gpus
                ):
                    raise BudgetExceeded("global_capacity_exceeded")
                active = list(
                    connection.execute(
                        select(instance_intents).where(
                            instance_intents.c.pool == pool,
                            instance_intents.c.state != "destroyed",
                        )
                    ).mappings()
                )
                active = [
                    row
                    for row in active
                    if not manually_reviewed_inactive(connection, row)
                ]
                if active_instance_ids is not None:
                    active = [row for row in active if row["id"] in active_instance_ids]
                if len(active) + 1 > (
                    admitted_capacity["max_machines"]
                    if admitted_capacity is not None
                    else limits["max_instances"]
                ) or sum(r["physical_gpus"] for r in active) + physical_gpus > (
                    admitted_capacity["max_machines"]
                    if admitted_capacity is not None
                    else limits["max_physical_gpus"]
                ):
                    raise BudgetExceeded("instance_capacity_exceeded")
                row = dict(
                    id=str(uuid.uuid4()),
                    intent_key=intent_key,
                    pool=pool,
                    request_hash=digest,
                    state="reserved",
                    physical_gpus=physical_gpus,
                    slots=slots,
                    reserved_cost_microusd=cost,
                    provider=provider,
                    hard_deadline=float(hard_deadline)
                    if hard_deadline is not None
                    else None,
                    created_at=self.clock(),
                    updated_at=self.clock(),
                )
                connection.execute(insert(instance_intents).values(**row))
                self._reserve(
                    connection, scope, budget_account_ids, "instance", row["id"], cost
                )
                event = {"intent_id": row["id"], "pool": pool}
                if admitted_capacity is not None:
                    event.update(
                        capacity_policy_snapshot=capacity_policy_snapshot,
                        capacity_provenance=capacity_provenance,
                    )
                self._emit(
                    connection,
                    "instance.reserved",
                    row["id"],
                    event,
                )
                return {**row, "created": True, "dry_run": False}
        except IntegrityError:
            raise Conflict("instance_intent_conflict") from None

    def update_instance(
        self,
        intent_id,
        state,
        *,
        provider_instance_id=None,
        destruction_confirmed=False,
        actual_cost_microusd=None,
        connection=None,
    ):
        """Trusted controller reconciles facts; unknown/expired instances retain reservations."""
        allowed = {
            "reserved": {"creating", "destroyed"},
            "creating": {"creation_unknown", "starting", "destroyed"},
            "creation_unknown": {"starting", "destroying", "destroyed"},
            "starting": {"ready", "draining", "destroying"},
            "ready": {"busy", "draining", "destroying"},
            "busy": {"ready", "draining"},
            "draining": {"destroying"},
            "destroying": {"destroyed"},
            "destroyed": set(),
        }
        with (
            self.transaction()
            if connection is None
            else nullcontext(connection) as connection
        ):
            row = self._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            if row is None:
                raise NotFound("instance_intent_not_found")
            values = dict(state=state, updated_at=self.clock())
            if provider_instance_id is not None:
                identifier(provider_instance_id)
                if row["provider_instance_id"] not in (None, provider_instance_id):
                    raise Conflict("provider_instance_conflict")
                values["provider_instance_id"] = provider_instance_id
            if state == row["state"]:
                if state == "destroyed" and actual_cost_microusd is not None:
                    self._settle(
                        connection, "instance", intent_id, actual_cost_microusd
                    )
                return dict(row)
            if state not in allowed[row["state"]]:
                raise InvalidTransition("invalid_instance_transition")
            if state in ("starting", "ready", "busy") and not (
                provider_instance_id or row["provider_instance_id"]
            ):
                raise Conflict("missing_provider_instance_id")
            if state == "destroyed":
                if not destruction_confirmed:
                    raise Conflict("destruction_not_confirmed")
                if actual_cost_microusd is not None:
                    self._settle(
                        connection, "instance", intent_id, actual_cost_microusd
                    )
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == intent_id)
                .values(**values)
            )
            return dict(
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )

    def settle_instance_cost(self, intent_id, *, actual_cost_microusd, connection=None):
        """Invoice reconciliation is independent of confirmed physical destruction."""
        money(actual_cost_microusd)
        with (
            self.transaction()
            if connection is None
            else nullcontext(connection) as connection
        ):
            row = self._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            if row is None:
                raise NotFound("instance_intent_not_found")
            if row["state"] != "destroyed":
                raise Conflict("instance_not_destroyed")
            self._settle(connection, "instance", intent_id, actual_cost_microusd)
            return self._instance_billing(connection, dict(row))

    @staticmethod
    def _instance_billing(connection, row):
        reservations = list(
            connection.execute(
                select(budget_reservations).where(
                    budget_reservations.c.reference_type == "instance",
                    budget_reservations.c.reference_id == row["id"],
                )
            ).mappings()
        )
        pending = any(
            r["state"] in {"reserved", "invoice_pending"} for r in reservations
        )
        actual = (
            None
            if pending or not reservations
            else reservations[0]["actual_cost_microusd"]
        )
        review = manually_reviewed_inactive(connection, row)
        return {
            **row,
            "billing_status": "pending"
            if pending
            else "settled"
            if reservations
            else "unknown",
            "actual_cost_microusd": actual,
            **({"manual_review": review} if review else {}),
        }

    def list_instance_intents(self, *, pool=None):
        statement = select(instance_intents)
        if pool is not None:
            statement = statement.where(instance_intents.c.pool == pool)
        with self.engine.connect() as connection:
            return [
                self._instance_billing(connection, dict(r))
                for r in connection.execute(
                    statement.order_by(instance_intents.c.created_at)
                ).mappings()
            ]


def manually_reviewed_inactive(connection, intent):
    """Explicit operator risk acceptance, never provider removal or settlement.

    Exact Lium/Targon account absence can be attested after a dashboard stop.
    Preserve the underlying intent state, so review cannot become physical
    removal evidence for replacement, settlement or recovery code.
    """
    if (
        intent["provider"] not in {"lium", "targon"}
        or intent["state"]
        not in {
            "creating",
            "creation_unknown",
            "starting",
            "ready",
            "busy",
            "draining",
            "destroying",
        }
        or intent["provider_instance_id"] is None
        and intent["state"] not in {"creating", "creation_unknown"}
        or intent["provider_instance_id"] is not None
        and not intent["provider_instance_id"]
    ):
        return None
    row = (
        connection.execute(
            select(scaler_receipts)
            .where(
                scaler_receipts.c.intent_id == intent["id"],
                scaler_receipts.c.operation == "manual_review",
            )
            .order_by(scaler_receipts.c.observed_at.desc(), scaler_receipts.c.id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    facts = row["facts"] if row else None
    if (
        not isinstance(facts, dict)
        or facts.get("schema_version") != 1
        or facts.get("state") != "manually_reviewed"
        or facts.get("intent_id") != intent["id"]
        or "instance_id" not in facts
        or facts.get("instance_id") != intent["provider_instance_id"]
        or facts.get("account_absent") is not True
        or facts.get("no_continuing_charge") is not True
        or not isinstance(facts.get("actor"), str)
        or not isinstance(facts.get("operation_id"), str)
    ):
        return None
    if "provider" in facts or "original_intent_state" in facts:
        if (
            facts.get("provider") != intent["provider"]
            or facts.get("original_intent_state") != intent["state"]
        ):
            return None
    elif (
        intent["provider"] != "targon"
        or intent["state"] != "destroying"
        or intent["provider_instance_id"] is None
    ):
        # Preserve the previously accepted Targon-only receipt format. It must
        # not become new Lium or active-state authority by omission.
        return None
    return {**facts, "observed_at": row["observed_at"]}


def paused_capacity_pools(connection):
    """Project explicit zero ceilings only; not free capacity or runtime approval.

    Missing limits still fail at reservation, but are not an operator pause.
    The reserve barrier remains authoritative for races and occupied pools.
    """
    return frozenset(
        connection.execute(
            select(pool_limits.c.pool).where(
                or_(
                    pool_limits.c.max_instances == 0,
                    pool_limits.c.max_physical_gpus == 0,
                )
            )
        ).scalars()
    )
