"""Canonical human-admin account operations over existing jobs and ledgers."""

import base64
import json
import math
import re
import uuid

from sqlalchemy import and_, func, insert, or_, select, update

from .auth import Principal, accounts
from .identity import lock_account_gate
from .identity_schema import (
    IdentitySchemaError,
    admin_audit,
    users,
    validate_connection,
)
from .job_cancellation import (
    _history_identity,
    attempt_history,
    cancel_eligibility,
    metadata_for,
)
from .repository import (
    budget_accounts,
    budget_reservations,
    documents,
    jobs,
    outbox,
    request_hash,
)
from .schema_version import IDENTITY_REVISIONS, SchemaVersionError, read_schema_version

PROJECT = "__account_admin"
KEY_KIND = "admin_user_change"
FINANCIAL_EVENTS = (
    "operator.budget_limit_updated",
    "operator.profile_price_updated",
    "operator.billing_applied",
)


class AdminError(Exception):
    def __init__(self, detail, status=422):
        self.detail, self.status = detail, status
        super().__init__(detail)


def _bounded(value, maximum):
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and not any(ord(c) < 32 for c in value)
    )


def _encode(kind, binding, position):
    return (
        base64.urlsafe_b64encode(
            json.dumps(
                {"kind": kind, "binding": binding, "position": position},
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )


def _decode(cursor, kind, binding):
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,800}", cursor
        ):
            raise ValueError
        value = json.loads(
            base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
        )
        if (
            type(value) is not dict
            or set(value) != {"kind", "binding", "position"}
            or value["kind"] != kind
            or value["binding"] != binding
        ):
            raise ValueError
        return value["position"]
    except (ValueError, TypeError, UnicodeError):
        raise AdminError("Invalid admin cursor") from None


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


class IdentityAdmin:
    def __init__(self, repository, settings):
        self.repo, self.settings = repository, settings

    def _actor(self, connection, principal):
        if not isinstance(principal, Principal) or principal.machine:
            raise AdminError("admin_required", 403)
        try:
            if read_schema_version(
                connection
            ) not in IDENTITY_REVISIONS or not validate_connection(connection):
                raise AdminError("Account administration is not ready", 503)
        except (SchemaVersionError, IdentitySchemaError):
            raise AdminError("Account administration is not ready", 503) from None
        actor = (
            connection.execute(
                select(users).where(
                    users.c.tenant == self.settings.tenant_id,
                    users.c.owner == principal.owner,
                )
            )
            .mappings()
            .first()
        )
        if actor is None or actor["role"] != "admin" or actor["status"] != "active":
            raise AdminError("admin_required", 403)
        return dict(actor)

    def _credit_summary(self, connection, user_ids):
        from .credit_schema import ready, accounts as credit_accounts, entries

        if not user_ids or not ready(connection):
            return {}
        from .credits import _aggregate_integer

        result = {ident: {'balance': 0, 'used_30d': 0} for ident in user_ids}
        for row in connection.execute(
            select(credit_accounts.c.user_id, credit_accounts.c.balance).where(
                credit_accounts.c.tenant == self.settings.tenant_id,
                credit_accounts.c.user_id.in_(user_ids),
            )
        ):
            result[row.user_id]['balance'] = _aggregate_integer(row.balance)
        for row in connection.execute(
            select(entries.c.user_id, func.sum(-entries.c.amount).label('used'))
            .where(
                entries.c.tenant == self.settings.tenant_id,
                entries.c.user_id.in_(user_ids),
                entries.c.kind == 'capture',
                entries.c.created_at >= self.repo.clock() - 30 * 86400,
            )
            .group_by(entries.c.user_id)
        ):
            result[row.user_id]['used_30d'] = _aggregate_integer(row.used)
        return result

    def _public(self, connection, row, *, credit_summary=None):
        if credit_summary is None:
            credit_summary = self._credit_summary(connection, [row['id']])
        providers = ["workos"] if row["workos_user_id"] else []
        if connection.execute(
            select(accounts.c.username).where(
                accounts.c.tenant == row["tenant"],
                accounts.c.username == row["owner"],
            )
        ).first():
            providers.append("password")
        return (
            {
                key: row[key]
                for key in (
                    "id",
                    "display_name",
                    "email",
                    "role",
                    "status",
                    "created_at",
                )
            }
            | {"providers": providers, "balance": None, "used_30d": None}
            | credit_summary.get(row['id'], {})
        )

    def list_users(self, principal, *, q="", cursor=None, limit=50):
        with self.repo.engine.connect() as connection:
            self._actor(connection, principal)
            if not _bounded(q, 200) or type(limit) is not int or not 1 <= limit <= 100:
                raise AdminError("Invalid user search")
            q = q.strip().lower()
            binding = request_hash({"tenant": self.settings.tenant_id, "q": q})
            after = _decode(cursor, "users", binding)
            if after is not None and not _uuid(after):
                raise AdminError("Invalid admin cursor")
            statement = select(users).where(users.c.tenant == self.settings.tenant_id)
            if q:
                statement = statement.where(
                    or_(
                        *[
                            func.lower(column).contains(q, autoescape=True)
                            for column in (
                                users.c.owner,
                                users.c.display_name,
                                users.c.email,
                            )
                        ]
                    )
                )
            if after is not None:
                statement = statement.where(users.c.id > after)
            rows = list(
                connection.execute(
                    statement.order_by(users.c.id).limit(limit + 1)
                ).mappings()
            )
            credit_summary = self._credit_summary(
                connection, [row['id'] for row in rows[:limit]]
            )
            return {
                "items": [
                    self._public(connection, row, credit_summary=credit_summary)
                    for row in rows[:limit]
                ],
                "next_cursor": _encode("users", binding, rows[limit - 1]["id"])
                if len(rows) > limit
                else None,
            }

    def _cancel_unsubmitted(self, connection, owner, actor):
        # Account gate is already exclusive. Lock the whole job set before any
        # account so concurrent collection (job -> budget) cannot invert locks.
        rows = list(
            connection.execute(
                select(jobs)
                .where(
                    jobs.c.tenant_id == self.settings.tenant_id,
                    jobs.c.owner_id == owner,
                    jobs.c.status.in_(("queued", "waiting_capacity", "claimed")),
                )
                .order_by(jobs.c.id)
                .with_for_update()
            ).mappings()
        )
        safe = []
        for row in rows:
            if row["status"] in {"queued", "waiting_capacity"}:
                proven = cancel_eligibility(connection, row)["cancellable"]
            else:
                history = attempt_history(connection, row)
                proven = bool(
                    history
                    and _history_identity(row, history)
                    and history[-1]["status"] == "claimed"
                    and history[-1]["fence"] == row["fence"]
                    and all(
                        item["submission_started_at"] is None
                        and item["upstream_task_id"] is None
                        for item in history
                    )
                    and all(
                        item["status"]
                        in {"lease_expired", "deferred", "cancelled", "failed"}
                        for item in history[:-1]
                    )
                )
            if proven:
                safe.append(dict(row))
        if not safe:
            return
        account_ids = list(
            connection.execute(
                select(budget_reservations.c.account_id)
                .where(
                    budget_reservations.c.reference_type == "job",
                    budget_reservations.c.reference_id.in_([row["id"] for row in safe]),
                    budget_reservations.c.state == "reserved",
                )
                .distinct()
                .order_by(budget_reservations.c.account_id)
            ).scalars()
        )
        for account_id in account_ids:
            self.repo._locked(
                connection,
                select(budget_accounts).where(budget_accounts.c.id == account_id),
            )
        cancellation = metadata_for(
            "operator", "account_suspended", actor, self.repo.clock()
        )
        for row in safe:
            self.repo._cancel_locked(connection, row, cancellation)

    def patch(self, principal, user_id, body, *, key=None):
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            actor = self._actor(connection, principal)
            if (
                not _uuid(user_id)
                or type(body) is not dict
                or not {"reason"} < set(body)
                or set(body) - {"role", "status", "reason"}
            ):
                raise AdminError("Provide role or status and a reason")
            if not _bounded(body["reason"], 500) or not body["reason"].strip():
                raise AdminError("A reason of 1 to 500 characters is required")
            if (
                "role" in body
                and (
                    not isinstance(body["role"], str)
                    or body["role"] not in {"user", "admin"}
                )
                or "status" in body
                and (
                    not isinstance(body["status"], str)
                    or body["status"] not in {"active", "suspended"}
                )
            ):
                raise AdminError("Invalid role or account status")
            if key is not None and (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key)
            ):
                raise AdminError("Invalid Idempotency-Key")
            fingerprint = request_hash({"user_id": user_id, "body": body})
            journal = and_(
                documents.c.tenant_id == self.settings.tenant_id,
                documents.c.owner_id == principal.owner,
                documents.c.project_id == PROJECT,
                documents.c.kind == KEY_KIND,
                documents.c.document_id == request_hash({"key": key}),
            )
            if key is not None:
                saved = connection.execute(
                    select(documents.c.payload).where(journal)
                ).scalar_one_or_none()
                if saved is not None:
                    if saved["fingerprint"] != fingerprint:
                        raise AdminError(
                            "Idempotency-Key was used with another request", 409
                        )
                    return saved["result"]
            target = (
                connection.execute(
                    select(users)
                    .where(
                        users.c.tenant == self.settings.tenant_id,
                        users.c.id == user_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if target is None:
                raise AdminError("User not found", 404)
            before = {name: target[name] for name in ("role", "status")}
            after = {name: body.get(name, target[name]) for name in before}
            changed = before != after
            if changed:
                if before == {"role": "admin", "status": "active"} and after != before:
                    active_admins = connection.execute(
                        select(func.count())
                        .select_from(users)
                        .where(
                            users.c.tenant == self.settings.tenant_id,
                            users.c.role == "admin",
                            users.c.status == "active",
                        )
                    ).scalar_one()
                    if active_admins <= 1:
                        raise AdminError("Cannot remove the last active admin", 409)
                now = self.repo.clock()
                connection.execute(
                    update(users)
                    .where(
                        users.c.id == user_id, users.c.tenant == self.settings.tenant_id
                    )
                    .values(**after, updated_at=now)
                )
                if after["status"] == "suspended" and before["status"] != "suspended":
                    self._cancel_unsubmitted(
                        connection, target["owner"], principal.actor_id
                    )
                action = "; ".join(
                    f"{field} → {after[field]}"
                    for field in before
                    if before[field] != after[field]
                )
                connection.execute(
                    insert(admin_audit).values(
                        id=str(uuid.uuid4()),
                        tenant=self.settings.tenant_id,
                        actor_user_id=actor["id"],
                        target_user_id=user_id,
                        action=action,
                        reason=body["reason"].strip(),
                        before=before,
                        after={
                            **after,
                            "actor": actor["display_name"] or actor["owner"],
                            "target": target["email"]
                            or target["display_name"]
                            or target["owner"],
                        },
                        created_at=now,
                    )
                )
            result = self._public(connection, dict(target, **after))
            if key is not None:
                connection.execute(
                    insert(documents).values(
                        tenant_id=self.settings.tenant_id,
                        owner_id=principal.owner,
                        project_id=PROJECT,
                        kind=KEY_KIND,
                        document_id=request_hash({"key": key}),
                        version=1,
                        payload={"fingerprint": fingerprint, "result": result},
                        updated_at=self.repo.clock(),
                    )
                )
            return result

    def audit(self, principal, *, cursor=None, limit=50):
        with self.repo.engine.connect() as connection:
            self._actor(connection, principal)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise AdminError("Invalid audit page size")
            binding = request_hash({"tenant": self.settings.tenant_id})
            after = _decode(cursor, "audit", binding)
            if after is not None and (
                type(after) is not dict
                or set(after) != {"time", "source", "id"}
                or type(after["time"]) not in (int, float)
                or not math.isfinite(after["time"])
                or not isinstance(after["source"], str)
                or not isinstance(after["source"], str)
                or after["source"] not in {"admin", "financial"}
                or not _uuid(after["id"])
            ):
                raise AdminError("Invalid admin cursor")

            def page(statement, table, source):
                if after is not None:
                    older_id = (
                        True
                        if source < after["source"]
                        else False
                        if source > after["source"]
                        else table.c.id < after["id"]
                    )
                    statement = statement.where(
                        or_(
                            table.c.created_at < after["time"],
                            and_(table.c.created_at == after["time"], older_id),
                        )
                    )
                return connection.execute(
                    statement.order_by(
                        table.c.created_at.desc(), table.c.id.desc()
                    ).limit(limit + 1)
                ).mappings()

            merged = []
            for row in page(
                select(admin_audit).where(
                    admin_audit.c.tenant == self.settings.tenant_id
                ),
                admin_audit,
                "admin",
            ):
                value = row["after"]
                merged.append(
                    {
                        "id": "admin:" + row["id"],
                        "actor": value.get("actor", row["actor_user_id"]),
                        "action": row["action"],
                        "target": value.get("target", row["target_user_id"]),
                        "reason": row["reason"],
                        "created_at": row["created_at"],
                        "_source": "admin",
                        "_id": row["id"],
                    }
                )
            payload = outbox.c.payload
            statement = select(
                outbox.c.id,
                outbox.c.event_type,
                outbox.c.created_at,
                payload["actor_owner"].as_string().label("actor_owner"),
                payload["actor"]["owner_id"].as_string().label("budget_actor"),
                payload["owner_id"].as_string().label("owner"),
                payload["profile_id"].as_string().label("profile"),
                payload["account_id"].as_string().label("account"),
                payload["request"]["action"].as_string().label("billing_action"),
                payload["request"]["owner_id"].as_string().label("billing_owner"),
                payload["request"]["reason"].as_string().label("billing_reason"),
            ).where(
                outbox.c.event_type.in_(FINANCIAL_EVENTS),
                payload["tenant_id"].as_string() == self.settings.tenant_id,
            )
            for row in page(statement, outbox, "financial"):
                if row["event_type"] == "operator.budget_limit_updated":
                    actor, action, target, reason = (
                        row["budget_actor"],
                        "budget limit changed",
                        row["owner"] or row["account"],
                        "",
                    )
                elif row["event_type"] == "operator.profile_price_updated":
                    actor, action, target, reason = (
                        row["actor_owner"],
                        "machine price changed",
                        row["profile"],
                        "",
                    )
                else:
                    if row["billing_action"] not in {"settle", "waive"}:
                        continue
                    actor, action, target, reason = (
                        row["actor_owner"],
                        "settled" if row["billing_action"] == "settle" else "waived",
                        row["billing_owner"],
                        row["billing_reason"] or "",
                    )
                merged.append(
                    {
                        "id": "financial:" + row["id"],
                        "actor": actor,
                        "action": action,
                        "target": target,
                        "reason": reason[:500],
                        "created_at": row["created_at"],
                        "_source": "financial",
                        "_id": row["id"],
                    }
                )
            merged.sort(
                key=lambda item: (item["created_at"], item["_source"], item["_id"]),
                reverse=True,
            )
            result = merged[:limit]
            next_cursor = (
                _encode(
                    "audit",
                    binding,
                    {
                        "time": result[-1]["created_at"],
                        "source": result[-1]["_source"],
                        "id": result[-1]["_id"],
                    },
                )
                if len(merged) > limit
                else None
            )
            return {
                "items": [
                    {
                        key: value
                        for key, value in row.items()
                        if not key.startswith("_")
                    }
                    for row in result
                ],
                "next_cursor": next_cursor,
            }
