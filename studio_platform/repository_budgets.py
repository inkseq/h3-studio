"""Original budgets ledger methods on the composed Repository transaction owner."""

from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import math
import re
import uuid
from sqlalchemy import (
    and_,
    func,
    insert,
    or_,
    select,
    update,
)
from .budget_policy import effective_budget_limit, fixed_cap_retired
from .providers.registry import DSTACK_V1_PROVIDERS

from .repository_schema import (
    NotFound,
    Conflict,
    BudgetExceeded,
    Scope,
    canonical,
    money,
    identifier,
    budget_snapshot,
    budget_accounts,
    jobs,
    budget_reservations,
    spending_origins,
    outbox,
    instance_intents,
    _NullLock,
)


class BudgetLedger:
    def configure_budget(
        self, account_id, *, tenant_id, limit_microusd, owner_id=None, project_id=None
    ):
        identifier(account_id)
        identifier(tenant_id)
        limit_microusd = money(limit_microusd)
        if project_id is not None and owner_id is None:
            raise ValueError("project_budget_requires_owner")
        with self.transaction() as connection:
            row = self._locked(
                connection,
                select(budget_accounts).where(budget_accounts.c.id == account_id),
            )
            if row:
                if (row["tenant_id"], row["owner_id"], row["project_id"]) != (
                    tenant_id,
                    owner_id,
                    project_id,
                ):
                    raise Conflict("budget_scope_conflict")
                if row["reserved_microusd"] + row["spent_microusd"] > limit_microusd:
                    raise BudgetExceeded("budget_below_committed")
                connection.execute(
                    update(budget_accounts)
                    .where(budget_accounts.c.id == account_id)
                    .values(limit_microusd=limit_microusd)
                )
            else:
                connection.execute(
                    insert(budget_accounts).values(
                        id=account_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        project_id=project_id,
                        limit_microusd=limit_microusd,
                        reserved_microusd=0,
                        spent_microusd=0,
                    )
                )

    def get_budget(self, account_id):
        """Trusted control-plane operation; do not expose this without authorization."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(budget_accounts).where(budget_accounts.c.id == account_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("budget_not_found")
            return dict(row)

    @contextmanager
    def _budget_read(self):
        """Account balances and their settlement dates share one read snapshot."""
        guard = self._memory_lock if self._is_memory else _NullLock()
        with guard, self.engine.connect() as connection:
            if self.engine.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
                connection.begin()
            else:
                connection.exec_driver_sql("BEGIN")
            try:
                yield connection
            finally:
                connection.rollback()

    def _budget_now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("invalid_budget_clock")
        return float(now)

    def _budget_projection(self, connection, row, now):
        today = datetime.fromtimestamp(now, timezone.utc).date()
        start = datetime.combine(
            today - timedelta(days=6), datetime.min.time(), timezone.utc
        ).timestamp()
        confirmed = and_(
            budget_reservations.c.account_id == row["id"],
            budget_reservations.c.state.in_(("settled", "released")),
            budget_reservations.c.actual_cost_microusd.is_not(None),
        )
        # Settlement date is the confirmed ledger event, never reservation
        # creation or a guessed supplier-invoice date. Legacy NULLs stay apart.
        day = (
            func.date(
                func.timezone(
                    "UTC", func.to_timestamp(budget_reservations.c.settled_at)
                )
            )
            if connection.dialect.name == "postgresql"
            else func.date(budget_reservations.c.settled_at, "unixepoch")
        )
        daily = connection.execute(
            select(
                day.label("date"),
                func.sum(budget_reservations.c.actual_cost_microusd).label(
                    "cost_microusd"
                ),
            )
            .where(
                confirmed,
                budget_reservations.c.settled_at >= start,
                budget_reservations.c.settled_at <= now,
            )
            .group_by(day)
            .order_by(day)
        ).mappings()
        legacy = (
            connection.execute(
                select(
                    func.coalesce(
                        func.sum(budget_reservations.c.actual_cost_microusd), 0
                    ).label("cost_microusd"),
                    func.count().label("reservation_count"),
                ).where(confirmed, budget_reservations.c.settled_at.is_(None))
            )
            .mappings()
            .one()
        )
        invoice_pending = (
            connection.execute(
                select(
                    func.count().label("reservation_count"),
                    func.coalesce(
                        func.sum(budget_reservations.c.amount_microusd), 0
                    ).label("reserved_estimate_microusd"),
                ).where(
                    budget_reservations.c.account_id == row["id"],
                    budget_reservations.c.state == "invoice_pending",
                )
            )
            .mappings()
            .one()
        )
        # Read the original account layer only. Rental invoices are not
        # per-video invoices, and estimate-only jobs are never free generation.
        costs = {
            kind: {
                "confirmed_cost_microusd": 0,
                "confirmed_reservation_count": 0,
                "pending_invoice_count": 0,
                "pending_estimate_microusd": 0,
                "pending_actual_cost_microusd": None,
            }
            for kind in ("job", "instance")
        }
        groups = connection.execute(
            select(
                budget_reservations.c.reference_type,
                budget_reservations.c.state,
                func.count().label("count"),
                func.coalesce(func.sum(budget_reservations.c.amount_microusd), 0).label(
                    "estimate"
                ),
                func.coalesce(
                    func.sum(budget_reservations.c.actual_cost_microusd), 0
                ).label("actual"),
            )
            .where(
                budget_reservations.c.account_id == row["id"],
                or_(
                    budget_reservations.c.state == "invoice_pending",
                    and_(
                        budget_reservations.c.state.in_(("settled", "released")),
                        budget_reservations.c.actual_cost_microusd.is_not(None),
                    ),
                ),
            )
            .group_by(budget_reservations.c.reference_type, budget_reservations.c.state)
        ).mappings()
        for group in groups:
            cost = costs.get(group["reference_type"])
            if cost is None:
                continue
            if group["state"] == "invoice_pending":
                cost["pending_invoice_count"] += int(group["count"])
                cost["pending_estimate_microusd"] += int(group["estimate"])
            else:
                cost["confirmed_reservation_count"] += int(group["count"])
                cost["confirmed_cost_microusd"] += int(group["actual"])
        limit, spent, reserved = (
            row[name]
            for name in ("limit_microusd", "spent_microusd", "reserved_microusd")
        )
        retired = fixed_cap_retired(row)
        committed = spent + reserved
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "owner_id": row["owner_id"],
            "project_id": row["project_id"],
            "basis": "cumulative",
            "currency": "USD",
            "limit_microusd": limit,
            "limit_mode": "settings" if retired else "cumulative",
            "legacy_projection_only": retired,
            "cost_breakdown": {
                "basis": "original_account_reservations",
                "account_layers_not_additive": True,
                **costs,
            },
            "invoice_pending": {
                "reservation_count": int(invoice_pending["reservation_count"]),
                "reserved_estimate_microusd": int(
                    invoice_pending["reserved_estimate_microusd"]
                ),
                "actual_cost_microusd": None,
                "included_in_settings_exposure": True,
            },
            "legacy_limit_microusd": row["limit_microusd"]
            if fixed_cap_retired(row)
            else None,
            "spent_microusd": spent,
            "reserved_microusd": reserved,
            "remaining_microusd": max(0, limit - committed),
            "usage_percent": min(100, committed * 100 / limit) if limit else 100,
            "status": "exhausted"
            if committed >= limit
            else "warning"
            if committed * 5 >= limit * 4
            else "ok",
            "snapshot": budget_snapshot(row),
            "as_of": now,
            "window": {
                "timezone": "UTC",
                "start_date": (today - timedelta(days=6)).isoformat(),
                "end_date": today.isoformat(),
            },
            "daily7": [
                {
                    "date": str(value["date"]),
                    "cost_microusd": int(value["cost_microusd"]),
                }
                for value in daily
            ],
            "unattributed_legacy": {
                "cost_microusd": int(legacy["cost_microusd"]),
                "reservation_count": int(legacy["reservation_count"]),
            },
        }

    def list_budget_projections(
        self,
        tenant_id,
        *,
        owner_id=None,
        account_ids=None,
        admin=False,
        limit=100,
        after_id=None,
    ):
        """Authorized service supplies tenant, owner and trusted configured IDs.

        A non-admin can never obtain a global or another owner's account. An
        optional trusted configured-ID selection can only narrow that scope.
        """
        identifier(tenant_id)
        if type(admin) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_budget_pagination")
        if after_id is not None:
            identifier(after_id)
        statement = select(budget_accounts).where(
            budget_accounts.c.tenant_id == tenant_id
        )
        if not admin:
            identifier(owner_id)
            statement = statement.where(budget_accounts.c.owner_id == owner_id)
            if account_ids is not None:
                if (
                    not isinstance(account_ids, (tuple, list, set, frozenset))
                    or len(account_ids) > 128
                ):
                    raise ValueError("invalid_budget_selection")
                allowed = tuple(sorted({identifier(value) for value in account_ids}))
                statement = statement.where(budget_accounts.c.id.in_(allowed))
        if after_id is not None:
            statement = statement.where(budget_accounts.c.id > after_id)
        now = self._budget_now()
        with self._budget_read() as connection:
            rows = list(
                connection.execute(
                    statement.order_by(budget_accounts.c.id).limit(limit + 1)
                ).mappings()
            )
            values = [
                self._budget_projection(connection, row, now) for row in rows[:limit]
            ]
        return {
            "budgets": values,
            "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
            "as_of": now,
        }

    def compare_set_budget_limit(
        self,
        account_id,
        *,
        tenant_id,
        expected_snapshot,
        limit_microusd,
        actor_id,
        actor_owner,
    ):
        """Change only an existing original limit and audit it atomically."""
        for value in (account_id, tenant_id, actor_id, actor_owner):
            identifier(value)
        if (
            not isinstance(expected_snapshot, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_snapshot) is None
        ):
            raise ValueError("invalid_budget_snapshot")
        limit_microusd = money(limit_microusd)
        now = self._budget_now()
        with self.transaction() as connection:
            row = self._locked(
                connection,
                select(budget_accounts).where(
                    budget_accounts.c.id == account_id,
                    budget_accounts.c.tenant_id == tenant_id,
                ),
            )
            if row is None:
                raise NotFound("budget_not_found")
            old_snapshot = budget_snapshot(row)
            if old_snapshot != expected_snapshot:
                raise Conflict("budget_snapshot_changed")
            if limit_microusd < row["spent_microusd"] + row["reserved_microusd"]:
                raise BudgetExceeded("budget_below_committed")
            changed = limit_microusd != row["limit_microusd"]
            audit_event_id = None
            if changed:
                connection.execute(
                    update(budget_accounts)
                    .where(
                        budget_accounts.c.id == account_id,
                        budget_accounts.c.tenant_id == tenant_id,
                    )
                    .values(limit_microusd=limit_microusd)
                )
                new = dict(row, limit_microusd=limit_microusd)
                audit_event_id = str(uuid.uuid4())
                names = ("limit_microusd", "spent_microusd", "reserved_microusd")
                payload = {
                    "version": 1,
                    "tenant_id": tenant_id,
                    "account_id": account_id,
                    "actor": {"owner_id": actor_owner, "actor_id": actor_id},
                    "owner_id": row["owner_id"],
                    "project_id": row["project_id"],
                    "old": {name: row[name] for name in names},
                    "new": {name: new[name] for name in names},
                    "old_snapshot": old_snapshot,
                    "new_snapshot": budget_snapshot(new),
                }
                connection.execute(
                    insert(outbox).values(
                        id=audit_event_id,
                        event_type="operator.budget_limit_updated",
                        aggregate_id=account_id,
                        payload=canonical(payload),
                        created_at=now,
                    )
                )
                row = new
            return {
                "budget": self._budget_projection(connection, row, now),
                "changed": changed,
                "audit_event_id": audit_event_id,
            }

    def list_budget_audit(self, tenant_id, *, limit=100, before=None):
        identifier(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_budget_pagination")
        statement = select(outbox).where(
            outbox.c.event_type == "operator.budget_limit_updated",
            outbox.c.payload["tenant_id"].as_string() == tenant_id,
        )
        if before is not None:
            if not isinstance(before, str) or len(before) > 80:
                raise ValueError("invalid_budget_audit_cursor")
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?):([0-9a-f-]{36})", before)
            if not match:
                raise ValueError("invalid_budget_audit_cursor")
            stamp = float(match[1])
            if not math.isfinite(stamp) or str(uuid.UUID(match[2])) != match[2]:
                raise ValueError("invalid_budget_audit_cursor")
            statement = statement.where(
                or_(
                    outbox.c.created_at < stamp,
                    and_(outbox.c.created_at == stamp, outbox.c.id < match[2]),
                )
            )
        with self._budget_read() as connection:
            rows = list(
                connection.execute(
                    statement.order_by(
                        outbox.c.created_at.desc(), outbox.c.id.desc()
                    ).limit(limit + 1)
                ).mappings()
            )
        events = [
            {
                "id": row["id"],
                "account_id": row["aggregate_id"],
                "created_at": row["created_at"],
                **{
                    key: row["payload"][key]
                    for key in (
                        "actor",
                        "owner_id",
                        "project_id",
                        "old",
                        "new",
                        "old_snapshot",
                        "new_snapshot",
                    )
                },
            }
            for row in rows[:limit]
        ]
        cursor = (
            f'{rows[limit - 1]["created_at"]}:{rows[limit - 1]["id"]}'
            if len(rows) > limit
            else None
        )
        return {"events": events, "next_before": cursor}

    def _reserve(
        self,
        connection,
        scope,
        account_ids,
        reference_type,
        reference_id,
        amount,
        *,
        spending_actor=None,
        confirmation_required=False,
        spending_settings_version=None,
    ):
        from .spending_limits import admit_spending

        admit_spending(
            self,
            connection,
            scope,
            reference_type,
            reference_id,
            amount,
            actor_id=spending_actor or scope.actor_id,
            confirmation_required=confirmation_required,
            spending_settings_version=spending_settings_version,
        )
        account_ids = sorted(set(account_ids))
        role_map = {}
        if reference_type == "job":
            from .credit_admission import validate

            job = (
                connection.execute(
                    select(jobs).where(
                        jobs.c.id == reference_id,
                        self._scope(jobs, scope),
                    )
                )
                .mappings()
                .first()
            )
            # Legacy/manual accounting references can predate a job row. They
            # retain enforced USD admission; absence can never confer a role.
            if job is not None:
                role_map = validate(connection, scope, job, account_ids)
        if amount and not account_ids:
            raise BudgetExceeded("budget_not_configured")
        for account_id in account_ids:
            row = self._locked(
                connection,
                select(budget_accounts).where(budget_accounts.c.id == account_id),
            )
            if (
                not row
                or row["tenant_id"] != scope.tenant_id
                or (row["owner_id"] is not None and row["owner_id"] != scope.owner_id)
                or (
                    row["project_id"] is not None
                    and row["project_id"] != scope.project_id
                )
            ):
                raise NotFound("budget_not_found")
            if role_map.get(account_id) != "accounting" and row["spent_microusd"] + row[
                "reserved_microusd"
            ] + amount > effective_budget_limit(row):
                raise BudgetExceeded("budget_exceeded")
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == account_id)
                .values(reserved_microusd=budget_accounts.c.reserved_microusd + amount)
            )
            reservation_id = str(uuid.uuid4())
            connection.execute(
                insert(budget_reservations).values(
                    id=reservation_id,
                    account_id=account_id,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    amount_microusd=amount,
                    state="reserved",
                    created_at=self.clock(),
                )
            )
            if role_map:
                from .credit_admission import store_role

                store_role(connection, reservation_id, role_map[account_id])
        return role_map

    def _settle(self, connection, reference_type, reference_id, actual_cost):
        actual_cost = money(actual_cost)
        settled_at = self._budget_now()
        # Settlement records authoritative actuals under the original lock
        # order. Admission caps must not add an owner/account lock inversion or
        # prevent late costs from being recorded truthfully.
        rows = list(
            connection.execute(
                select(budget_reservations)
                .where(
                    budget_reservations.c.reference_type == reference_type,
                    budget_reservations.c.reference_id == reference_id,
                )
                .order_by(budget_reservations.c.account_id)
                .with_for_update()
            ).mappings()
        )
        for row in rows:
            if row["state"] not in {"reserved", "invoice_pending"}:
                if row["actual_cost_microusd"] != actual_cost:
                    raise Conflict("budget_already_settled")
                continue
            if (
                row["state"] == "invoice_pending"
                and row["actual_cost_microusd"] is not None
            ):
                raise Conflict("budget_pending_invoice_invalid")
            # Account locks use a stable order for concurrent tenant/global budgets.
            self._locked(
                connection,
                select(budget_accounts).where(
                    budget_accounts.c.id == row["account_id"]
                ),
            )
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == row["account_id"])
                .values(
                    reserved_microusd=budget_accounts.c.reserved_microusd
                    - (row["amount_microusd"] if row["state"] == "reserved" else 0),
                    spent_microusd=budget_accounts.c.spent_microusd + actual_cost,
                )
            )
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.id == row["id"])
                .values(
                    state="released" if actual_cost == 0 else "settled",
                    actual_cost_microusd=actual_cost,
                    settled_at=settled_at,
                )
            )

    def fund_instance_window(
        self, connection, intent_id, minimum_reserved_cost_microusd, budget_account_ids
    ):
        """Extend an explicit continuing machine's original money reservation.

        This grants funded runway, not another rental, deadline or invoice. All
        accounts and reservation rows are checked before any counter changes;
        a caller catches BudgetExceeded only outside this transaction.
        """
        from .operator_capacity import operator_nodes

        minimum = money(minimum_reserved_cost_microusd)
        self._lock_capacity(connection)
        intent = self._locked(
            connection,
            select(instance_intents).where(instance_intents.c.id == intent_id),
        )
        node = self._locked(
            connection,
            select(operator_nodes).where(operator_nodes.c.intent_id == intent_id),
        )
        if not intent or not node:
            raise NotFound("instance_intent_not_found")
        payload = node["payload"]
        lifecycle = payload.get("lifecycle", {})
        request, binding = payload.get("request", {}), payload.get("dstack", {})
        if (
            payload.get("capacity_backend") != "dstack-v1"
            or lifecycle.get("version") != 2
            or request.get("lifecycle_version") != 2
            or binding.get("lifecycle_version") != 2
            or intent["provider"] not in DSTACK_V1_PROVIDERS
            or intent["hard_deadline"] is not None
            or payload.get("hard_deadline") is not None
            or request.get("hard_deadline") is not None
            or request.get("max_duration_s") is not None
            or node["desired_state"] != "running"
            or intent["state"]
            not in {
                "reserved",
                "creating",
                "creation_unknown",
                "starting",
                "ready",
                "busy",
            }
        ):
            raise Conflict("instance_continuing_policy_unconfirmed")
        ids = sorted(set(budget_account_ids))
        if (
            not ids
            or len(ids) != len(budget_account_ids)
            or ids != sorted(lifecycle.get("budget_account_ids", []))
        ):
            raise BudgetExceeded("instance_budget_binding_changed")
        from .spending_limits import admit_spending

        origin = (
            connection.execute(
                select(spending_origins).where(
                    spending_origins.c.reference_type == "instance",
                    spending_origins.c.reference_id == intent_id,
                )
            )
            .mappings()
            .first()
        )
        admit_spending(
            self,
            connection,
            Scope(payload["tenant_id"], payload["owner_id"], payload["project_id"]),
            "instance",
            intent_id,
            max(0, minimum - intent["reserved_cost_microusd"]),
            actor_id=origin["actor_id"] if origin else "legacy-instance",
            record_origin=False,
        )
        accounts = []
        for account_id in ids:
            account = self._locked(
                connection,
                select(budget_accounts).where(budget_accounts.c.id == account_id),
            )
            if (
                not account
                or account["tenant_id"] != payload.get("tenant_id")
                or account["owner_id"] is not None
                and account["owner_id"] != payload.get("owner_id")
                or account["project_id"] is not None
                and account["project_id"] != payload.get("project_id")
            ):
                raise BudgetExceeded("instance_budget_binding_changed")
            accounts.append(account)
        if not any(
            account["owner_id"] == payload.get("owner_id")
            and account["owner_id"] is not None
            for account in accounts
        ):
            raise BudgetExceeded("instance_owner_budget_required")
        rows = list(
            connection.execute(
                select(budget_reservations)
                .where(
                    budget_reservations.c.reference_type == "instance",
                    budget_reservations.c.reference_id == intent_id,
                )
                .order_by(budget_reservations.c.account_id)
                .with_for_update()
            ).mappings()
        )
        current = intent["reserved_cost_microusd"]
        if (
            len(rows) != len(ids)
            or [row["account_id"] for row in rows] != ids
            or any(
                row["state"] != "reserved"
                or row["amount_microusd"] != current
                or row["actual_cost_microusd"] is not None
                for row in rows
            )
        ):
            raise BudgetExceeded("instance_reservation_unconfirmed")
        total = max(minimum, current)
        extra = total - current
        if any(
            account["reserved_microusd"] < current
            or account["spent_microusd"] + account["reserved_microusd"] + extra
            > effective_budget_limit(account)
            for account in accounts
        ):
            raise BudgetExceeded("instance_spend_limit_reached")
        if not extra:
            return current
        for account_id in ids:
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == account_id)
                .values(reserved_microusd=budget_accounts.c.reserved_microusd + extra)
            )
        for row in rows:
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.id == row["id"])
                .values(amount_microusd=total)
            )
        connection.execute(
            update(instance_intents)
            .where(instance_intents.c.id == intent_id)
            .values(reserved_cost_microusd=total, updated_at=self.clock())
        )
        self._emit(
            connection,
            "instance.reservation_extended",
            intent_id,
            {
                "intent_id": intent_id,
                "previous_microusd": current,
                "reserved_microusd": total,
                "additional_microusd": extra,
                "account_ids": ids,
            },
        )
        return total
