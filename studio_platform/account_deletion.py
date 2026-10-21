"""Staging-only original-owner deletion preview and durable cleanup journal.

Production is unconditionally disabled. This is not production legal approval,
nor authority to erase accepted money history or shared/unknown media.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import re
import uuid

from sqlalchemy import delete, inspect, insert, select, update

from .auth import (
    Principal,
    USERS,
    clients,
    personal_keys,
    sessions,
    WORKOS_PRIVATE_OWNER,
    WORKOS_PRIVATE_PROJECT,
    WORKOS_SESSION_KIND,
)
from .content_lifecycle import owner_reference_gate, put_tombstone
from .identity import lock_account_gate
from .identity_schema import users, admin_audit
from .repository import (
    Conflict,
    artifacts,
    attempts,
    budget_accounts,
    budget_reservations,
    documents,
    jobs,
    plans,
    registered_workers,
    instance_intents,
    request_hash,
)
from .session_authority import read, write, advance

CONTEXT = ContextVar("original_account_cleanup", default=None)
PREVIEW_SECONDS = 600
RETENTION = {
    "status": "draft",
    "financial_years": 7,
    "legal_hold": "extended",
    "production_approved": False,
    "backup_restore": "tombstone_enforcement_requires_production_review",
}


class DeletionError(Exception):
    def __init__(self, code, status=409, **data):
        self.code, self.status, self.data = code, status, data
        super().__init__(code)


def enabled(settings, owner=None):
    return (
        getattr(settings, "deployment_target", "production") == "staging"
        and settings.public_origin == "https://staging.inkseq.com"
        and not getattr(settings, "public_origin_aliases", ())
        and bool(getattr(settings, "account_deletion_test_owners", ()))
        and (owner is None or owner in settings.account_deletion_test_owners)
    )


def assert_writable(connection, tenant, owner):
    if not inspect(connection).has_table(documents.name):
        return
    marker = read(connection, tenant, "deletion_owner", owner)
    if marker is not None and (
        type(marker) is not dict
        or not isinstance(marker.get("operation_id"), str)
        or CONTEXT.get() != (tenant, owner, marker["operation_id"])
    ):
        raise Conflict("account_deletion_pending")


@contextmanager
def original_cleanup(tenant, owner, ident):
    token = CONTEXT.set((tenant, owner, ident))
    try:
        yield
    finally:
        CONTEXT.reset(token)


class AccountDeletion:
    def __init__(self, repo, auth, settings, workos, quick_chat):
        self.repo, self.auth, self.settings, self.workos, self.chat = (
            repo,
            auth,
            settings,
            workos,
            quick_chat,
        )
        self.tenant = settings.tenant_id

    def _protected(self, user):
        return user["role"] != "user" or user["owner"] in {
            *USERS,
            "qa-admin",
            "qa-user",
            *getattr(self.settings, "operator_admin_owners", ()),
            *getattr(self.settings, "operator_capacity_owners", ()),
        }

    def capability(self, principal):
        if not enabled(self.settings, getattr(principal, "owner", None)):
            return False
        with self.repo.engine.connect() as conn:
            try:
                self._actor(conn, principal)
            except DeletionError:
                return False
        return True

    def _bound_user(self, conn, record):
        value = record["payload"]
        user = (
            conn.execute(
                select(users).where(
                    users.c.tenant == self.tenant,
                    users.c.id == value["user_id"],
                    users.c.owner == record["owner"],
                    users.c.status == "suspended",
                )
            )
            .mappings()
            .first()
        )
        if (
            user is None
            or self._protected(user)
            or user["workos_user_id"] != value["provider"].get("subject")
            or not enabled(self.settings, record["owner"])
        ):
            raise DeletionError("account_deletion_binding_changed")
        return dict(user)

    def _audit(self, conn, user_id, ident, action, now):
        conn.execute(
            insert(admin_audit).values(
                id=str(uuid.uuid4()),
                tenant=self.tenant,
                actor_user_id=user_id,
                target_user_id=user_id,
                action=action,
                reason="Disposable staging account self-deletion",
                before={"operation_id": ident},
                after={"operation_id": ident, "production_enabled": False},
                created_at=now,
            )
        )

    def _actor(self, conn, principal):
        if (
            not isinstance(principal, Principal)
            or principal.machine
            or not principal.session_hash
        ):
            raise DeletionError("browser_required", 403)
        from .identity_schema import validate_connection, IdentitySchemaError
        from .schema_version import (
            read_schema_version,
            IDENTITY_REVISIONS,
            SchemaVersionError,
        )

        try:
            if read_schema_version(
                conn
            ) not in IDENTITY_REVISIONS or not validate_connection(conn):
                raise DeletionError("account_deletion_schema_unavailable", 503)
        except (IdentitySchemaError, SchemaVersionError):
            raise DeletionError("account_deletion_schema_unavailable", 503) from None
        row = (
            conn.execute(
                select(users).where(
                    users.c.tenant == self.tenant,
                    users.c.owner == principal.owner,
                    users.c.id == principal.user_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["status"] != "active":
            raise DeletionError("account_unavailable", 403)
        if self._protected(row):
            raise DeletionError("account_deletion_protected", 403)
        if not enabled(self.settings, row["owner"]):
            raise DeletionError("account_deletion_not_enabled", 403)
        session = conn.execute(
            select(sessions.c.token_hash).where(
                sessions.c.tenant == self.tenant,
                sessions.c.username == row["owner"],
                sessions.c.token_hash == principal.session_hash,
                sessions.c.expires > self.auth.clock(),
            )
        ).first()
        if session is None:
            raise DeletionError("account_unavailable", 403)
        return dict(row)

    def _rows(self, conn, table, *where):
        return (
            sorted(
                (
                    dict(row)
                    for row in conn.execute(select(table).where(*where)).mappings()
                ),
                key=request_hash,
            )
            if inspect(conn).has_table(table.name)
            else []
        )

    def _census(self, conn, user):
        from .assets import asset_table
        from .quick_chat import objects
        from .credit_schema import accounts as credits, entries
        from .credit_schema import ready as credits_ready
        from .payment_schema import payments, events
        from .payment_schema import ready as payments_ready
        from .storage_asset_journal import receipts
        from .artifact_writer import write_receipts
        from .content_references import cleanup_plan, history_blocker
        from .operator_capacity import operator_nodes

        owner = user["owner"]
        scoped = lambda table: (
            table.c.tenant_id == self.tenant,
            table.c.owner_id == owner,
        )
        owned_docs = self._rows(conn, documents, *scoped(documents))
        owned_jobs = self._rows(conn, jobs, *scoped(jobs))
        job_ids = {row["id"] for row in owned_jobs}
        owned_plans = self._rows(conn, plans, *scoped(plans))
        owned_assets = self._rows(
            conn,
            asset_table,
            asset_table.c.tenant == self.tenant,
            asset_table.c.owner == owner,
        )
        owned_objects = self._rows(
            conn, objects, objects.c.tenant == self.tenant, objects.c.owner == owner
        )
        owned_attempts = self._rows(conn, attempts, attempts.c.job_id.in_(job_ids))
        owned_nodes = self._rows(
            conn,
            operator_nodes,
            operator_nodes.c.payload["tenant_id"].as_string() == self.tenant,
            operator_nodes.c.payload["owner_id"].as_string() == owner,
        )
        node_intents = self._rows(
            conn,
            instance_intents,
            instance_intents.c.id.in_([row["intent_id"] for row in owned_nodes]),
        )
        owned_budgets = self._rows(conn, budget_accounts, *scoped(budget_accounts))
        reservations = self._rows(
            conn,
            budget_reservations,
            budget_reservations.c.account_id.in_([r["id"] for r in owned_budgets]),
        )
        credit_rows = self._rows(
            conn,
            credits,
            credits.c.tenant == self.tenant,
            credits.c.user_id == user["id"],
        )
        credit_entries = self._rows(
            conn,
            entries,
            entries.c.tenant == self.tenant,
            entries.c.user_id == user["id"],
        )
        payment_rows = self._rows(
            conn,
            payments,
            payments.c.tenant == self.tenant,
            payments.c.user_id == user["id"],
        )
        payment_ids = {row["id"] for row in payment_rows}
        event_rows = [
            row
            for row in self._rows(conn, events, events.c.tenant == self.tenant)
            if row["payment_id"] in payment_ids
            or row["verified_payload"].get("object", {}).get("user_id") == user["id"]
        ]
        blockers = []

        def block(code, count):
            if count:
                blockers.append({"code": code, "count": int(count)})

        ended_intents = {r["id"] for r in node_intents if r["state"] == "destroyed"}
        block(
            "machine_obligations_unresolved",
            sum(
                r["runtime_state"] != "stopped" or r["intent_id"] not in ended_intents
                for r in owned_nodes
            ),
        )

        block(
            "jobs_active_or_uncertain",
            sum(
                r["status"] not in {"succeeded", "failed", "cancelled"}
                or r["lease_worker_id"] is not None
                or r["lease_expires_at"] is not None
                for r in owned_jobs
            ),
        )
        block(
            "upstream_stop_unconfirmed",
            sum(
                (
                    r["submission_started_at"] is not None
                    or r["upstream_task_id"] is not None
                )
                and r["upstream_stopped"] != 1
                for r in owned_attempts
            ),
        )
        block(
            "worker_obligation_unresolved",
            len(
                self._rows(
                    conn,
                    registered_workers,
                    registered_workers.c.current_job_id.in_(job_ids),
                    registered_workers.c.state != "retired",
                )
            ),
        )
        block(
            "unsettled_reservations",
            sum(
                r["state"] == "reserved"
                or r["state"] == "charged"
                and r["settled_at"] is None
                for r in reservations
            ),
        )
        block("credit_holds_unsettled", sum(r["held"] > 0 for r in credit_rows))
        block(
            "checkout_unresolved", sum(r["status"] == "created" for r in payment_rows)
        )
        block("stripe_dispute_open", sum(r["dispute_open"] != 0 for r in payment_rows))
        block(
            "stripe_events_unresolved",
            sum(r["status"] != "processed" for r in event_rows),
        )
        block(
            "paid_credit_balance_requires_review",
            bool(payment_rows) and any(r["balance"] > 0 for r in credit_rows),
        )
        block(
            "financial_schema_unavailable",
            not credits_ready(conn) or not payments_ready(conn),
        )
        from .content_lifecycle import history_cleanup_activated

        block(
            "history_cleanup_not_enabled",
            not history_cleanup_activated(conn, self.tenant),
        )
        if user["workos_user_id"]:
            try:
                self.workos.config()
            except Exception:
                block("workos_provider_unavailable", 1)
        uploads = self._rows(
            conn, receipts, receipts.c.tenant == self.tenant, receipts.c.owner == owner
        )
        writes = self._rows(
            conn,
            write_receipts,
            write_receipts.c.tenant == self.tenant,
            write_receipts.c.owner == owner,
        )
        block(
            "upload_unresolved",
            sum(
                r["status"] not in {"ready", "duplicate", "purged"}
                for r in owned_assets
            ),
        )
        block(
            "storage_operation_unresolved",
            sum(json.loads(r["record"]).get("busy", False) for r in uploads)
            + sum(json.loads(r["record"]).get("phase") != "settled" for r in writes),
        )
        actual_sessions = [r for r in owned_objects if r["kind"] == "session"]
        for row in actual_sessions:
            reason = history_blocker(conn, self.chat, owner, row)
            if reason:
                block(reason, 1)
        block(
            "authoring_operation_unresolved",
            sum(
                r["kind"] == "library_import"
                and r["payload"].get("state") not in {"completed", "blocked"}
                for r in owned_docs
            ),
        )
        block(
            "film_run_unresolved",
            sum(
                r["kind"] == "film_run"
                and r["payload"].get("phase") not in {"completed", "cancelled"}
                for r in owned_objects
            ),
        )
        project_ids = {
            r["project_id"]
            for r in (*owned_docs, *owned_jobs, *owned_assets, *owned_plans)
        }
        project_ids |= {
            r["document_id"]
            for r in owned_docs
            if r["project_id"] == "__projects" and r["kind"] == "project"
        }
        covered = {r["payload"]["project_id"] for r in actual_sessions}
        closure_sessions = [
            *actual_sessions,
            *(
                {
                    "id": "deletion-project-" + request_hash(p)[:30],
                    "payload": {"project_id": p},
                }
                for p in sorted(project_ids - covered)
            ),
        ]
        artifact_ids = [
            r["id"]
            for r in self._rows(conn, artifacts, artifacts.c.job_id.in_(job_ids))
            if not r["metadata"].get("content_deleted")
        ]
        plan = cleanup_plan(
            conn,
            self.chat,
            Principal(owner, "account-deletion"),
            closure_sessions,
            media_targets={
                "asset_ids": [r["id"] for r in owned_assets if r["status"] != "purged"],
                "artifact_ids": artifact_ids,
            },
            ignored_trash_ids=[
                r["document_id"] for r in owned_docs if r["kind"] == "trash_item"
            ],
            purged_projects=project_ids,
        )
        plan["warnings"].sort(key=request_hash)
        block(
            "storage_identity_unresolved",
            sum(
                w["reason"]
                in {
                    "unknown_storage_binding",
                    "original_upload_unconfirmed",
                    "original_poster_unconfirmed",
                }
                for w in plan["warnings"]
            ),
        )
        source = {
            "user": user,
            "documents": owned_docs,
            "jobs": owned_jobs,
            "plans": owned_plans,
            "objects": owned_objects,
            "assets": owned_assets,
            "attempts": owned_attempts,
            "machine_nodes": owned_nodes,
            "machine_intents": node_intents,
            "uploads": uploads,
            "writes": writes,
            "financial": [
                owned_budgets,
                reservations,
                credit_rows,
                credit_entries,
                payment_rows,
                event_rows,
            ],
            "plan": plan,
        }
        # Stable ordering comes from explicit full-row hashes rather than SQL
        # planner/insertion order; no raw source payload appears in previews.
        fingerprint = request_hash(
            {
                key: sorted(value, key=request_hash)
                if isinstance(value, list)
                else value
                for key, value in source.items()
            }
        )
        return {
            "fingerprint": fingerprint,
            "plan": plan,
            "blockers": blockers,
            "counts": {
                "stories": sum(
                    r["kind"] == "project" and r["project_id"] == "__projects"
                    for r in owned_docs
                ),
                "sessions": len(actual_sessions),
                "assets": len(owned_assets),
                "artifacts": len(artifact_ids),
            },
            "financial_fingerprint": request_hash(source["financial"]),
        }

    def preview(self, principal, body):
        if not enabled(self.settings):
            raise DeletionError("account_deletion_not_enabled", 403)
        if type(body) is not dict or body:
            raise DeletionError("account_deletion_request_invalid", 422)
        with self.repo.transaction() as conn:
            lock_account_gate(conn, exclusive=True)
            user = self._actor(conn, principal)
            owner_reference_gate(conn, self.tenant, user["owner"])
            census = self._census(conn, user)
            ident = str(uuid.uuid4())
            now = self.auth.clock()
            value = {
                "preview_id": ident,
                "user_id": user["id"],
                "owner": user["owner"],
                "session_hash": principal.session_hash,
                "expires_at": now + PREVIEW_SECONDS,
                "fingerprint": census["fingerprint"],
                "policy": request_hash(RETENTION),
            }
            write(conn, self.tenant, "deletion_preview", ident, value, now)
        return {
            "preview_id": ident,
            "expires_at": value["expires_at"],
            "can_delete": not census["blockers"],
            "blockers": census["blockers"],
            "removed": census["counts"],
            "retained": {
                "financial_records": True,
                "shared_or_unknown_media": census["plan"]["kept"],
                "accepted_audit_identifiers": True,
                "backup_expiry": "not_approved_for_production",
            },
            "retention_policy": deepcopy(RETENTION),
            "production_enabled": False,
        }

    def confirm(self, principal, body, key):
        from .quick_chat import objects
        from .agent_connect import connections

        if not enabled(self.settings):
            raise DeletionError("account_deletion_not_enabled", 403)
        if (
            type(body) is not dict
            or set(body) != {"preview_id", "confirmed"}
            or body["confirmed"] is not True
            or not isinstance(body["preview_id"], str)
            or not re.fullmatch(r"[0-9a-f-]{36}", body["preview_id"])
            or not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key)
        ):
            raise DeletionError("account_deletion_request_invalid", 422)
        with self.repo.transaction() as conn:
            lock_account_gate(conn, exclusive=True)
            user = self._actor(conn, principal)
            owner = user["owner"]
            journal = request_hash({"user_id": user["id"], "key": key})
            previous = read(conn, self.tenant, "deletion_key", journal)
            if previous:
                if previous["fingerprint"] != request_hash(body):
                    raise DeletionError("account_deletion_idempotency_conflict")
                return {"operation_id": previous["operation_id"], "state": "pending"}
            owner_reference_gate(conn, self.tenant, owner)
            preview = read(conn, self.tenant, "deletion_preview", body["preview_id"])
            if (
                preview is None
                or preview["owner"] != owner
                or preview["user_id"] != user["id"]
                or preview["session_hash"] != principal.session_hash
                or preview["expires_at"] <= self.auth.clock()
                or preview["policy"] != request_hash(RETENTION)
            ):
                raise DeletionError("account_deletion_preview_changed")
            census = self._census(conn, user)
            if census["fingerprint"] != preview["fingerprint"]:
                raise DeletionError("account_deletion_preview_changed")
            if census["blockers"]:
                raise DeletionError(
                    "account_deletion_blocked", blockers=census["blockers"]
                )
            now = self.auth.clock()
            ident = str(uuid.uuid4())
            payload = {
                "action": "account_delete",
                "operation_id": ident,
                "user_id": user["id"],
                "owner": owner,
                "state": "pending",
                "phase": "media",
                "plan": census["plan"],
                "created_at": now,
                "accounting_released": False,
                "financial_before": census["financial_fingerprint"],
                "error_code": None,
                "provider": {
                    "subject": user["workos_user_id"],
                    "client_id": self.workos.config()["client_id"]
                    if user["workos_user_id"]
                    else None,
                    "state": "unrequested" if user["workos_user_id"] else "deleted",
                },
            }
            record = {
                "id": ident,
                "tenant": self.tenant,
                "owner": owner,
                "session_id": ident,
                "kind": "account_deletion",
                "parent_id": None,
                "business_key": user["id"],
                "version": 1,
                "payload": payload,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(insert(objects).values(**record))
            write(
                conn,
                self.tenant,
                "deletion",
                ident,
                {"owner": owner, "user_id": user["id"]},
                now,
            )
            write(
                conn,
                self.tenant,
                "deletion_owner",
                owner,
                {"operation_id": ident, "user_id": user["id"]},
                now,
            )
            write(
                conn,
                self.tenant,
                "deletion_key",
                journal,
                {"operation_id": ident, "fingerprint": request_hash(body)},
                now,
            )
            advance(conn, self.tenant, owner, now, operation_id=ident)
            # No identity_admin suspension: it would cancel queued work. Clean
            # source guards above are mandatory and no job is canceled here.
            conn.execute(
                update(users)
                .where(users.c.tenant == self.tenant, users.c.id == user["id"])
                .values(status="suspended", updated_at=now)
            )
            conn.execute(
                delete(sessions).where(
                    sessions.c.tenant == self.tenant, sessions.c.username == owner
                )
            )
            conn.execute(
                delete(documents).where(
                    documents.c.tenant_id == self.tenant,
                    documents.c.owner_id == WORKOS_PRIVATE_OWNER,
                    documents.c.project_id == WORKOS_PRIVATE_PROJECT,
                    documents.c.kind == WORKOS_SESSION_KIND,
                    documents.c.payload["owner"].as_string() == owner,
                )
            )
            conn.execute(
                update(personal_keys)
                .where(
                    personal_keys.c.tenant == self.tenant,
                    personal_keys.c.owner == owner,
                    personal_keys.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            conn.execute(
                update(clients)
                .where(clients.c.tenant == self.tenant, clients.c.owner == owner)
                .values(disabled=1)
            )
            conn.execute(
                update(connections)
                .where(
                    connections.c.tenant == self.tenant,
                    connections.c.owner == owner,
                    connections.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            with original_cleanup(self.tenant, owner, ident):
                for kind, ids in (
                    ("asset", payload["plan"]["asset_ids"]),
                    ("artifact", payload["plan"]["artifact_ids"]),
                ):
                    for item in ids:
                        put_tombstone(
                            conn,
                            self.tenant,
                            owner,
                            kind=kind,
                            value=item,
                            action_id=ident,
                            now=now,
                        )
                for spec in payload["plan"]["objects"]:
                    put_tombstone(
                        conn,
                        self.tenant,
                        owner,
                        kind="key",
                        value=spec["object_key"],
                        storage_binding=spec["storage_binding"],
                        action_id=ident,
                        now=now,
                    )
            if self.repo.generation_transport is None:
                raise DeletionError("account_deletion_worker_unavailable", 503)
            self.repo.generation_transport.enqueue_account_operation(
                conn, self.tenant, ident
            )
            self._audit(conn, user["id"], ident, "account_deletion_requested", now)
        return {"operation_id": ident, "state": "pending"}

    def _record(self, conn, ident):
        from .quick_chat import objects

        pointer = read(conn, self.tenant, "deletion", ident)
        if pointer is None:
            return None
        row = (
            conn.execute(
                select(objects).where(
                    objects.c.id == ident,
                    objects.c.tenant == self.tenant,
                    objects.c.owner == pointer["owner"],
                    objects.c.kind == "account_deletion",
                )
            )
            .mappings()
            .first()
        )
        if (
            row is None
            or row["session_id"] != ident
            or row["business_key"] != pointer["user_id"]
            or row["payload"].get("operation_id") != ident
            or row["payload"].get("owner") != pointer["owner"]
            or row["payload"].get("user_id") != pointer["user_id"]
            or read(conn, self.tenant, "deletion_owner", pointer["owner"])
            != {"operation_id": ident, "user_id": pointer["user_id"]}
        ):
            raise DeletionError("account_deletion_binding_changed")
        return dict(row)

    def _save(self, ident, change, *, require_user=False):
        from .quick_chat import objects

        with self.repo.transaction() as conn:
            lock_account_gate(conn, exclusive=True)
            row = self._record(conn, ident)
            if row is None:
                raise DeletionError("account_deletion_binding_changed")
            if require_user:
                self._bound_user(conn, row)
            with original_cleanup(self.tenant, row["owner"], ident):
                owner_reference_gate(conn, self.tenant, row["owner"])
                value = deepcopy(row["payload"])
                change(value)
                conn.execute(
                    update(objects)
                    .where(objects.c.id == ident)
                    .values(
                        payload=value,
                        version=objects.c.version + 1,
                        updated_at=self.auth.clock(),
                    )
                )
        return value

    def _provider_turn(self, record):
        from workos._errors import NotFoundError

        provider = record["payload"]["provider"]
        ident = record["id"]
        if provider["state"] == "deleted":
            return True
        client = None
        try:
            config = self.workos.config()
            if config["client_id"] != provider["client_id"]:
                raise DeletionError("account_deletion_provider_changed")
            client = self.workos.sdk_factory(config)
            try:
                identity = client.user_management.get_user(provider["subject"])
            except NotFoundError:
                self._save(
                    ident, lambda value: value["provider"].update(state="deleted")
                )
                return True
            if getattr(identity, "id", None) != provider["subject"]:
                raise DeletionError("account_deletion_provider_changed")
            self._save(
                ident,
                lambda value: value["provider"].update(state="requested"),
                require_user=True,
            )
            client.user_management.delete_user(provider["subject"])
        except DeletionError as error:
            self._save(
                ident, lambda value, code=error.code: value.update(error_code=code)
            )
        except Exception:
            self._save(
                ident,
                lambda value: value.update(
                    error_code="account_deletion_provider_unavailable"
                ),
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        return False  # Even acknowledged deletion is confirmed by original-ID GET.

    def turn(self, tenant, ident):
        from .asset_operation import asset_operation_lock

        if tenant != self.tenant:
            return {"done": True, "state": "unavailable"}
        with self.repo.engine.connect() as conn:
            record = self._record(conn, ident)
        if record is None:
            return None
        # The existing executor has one shared operation directory. Serialize
        # transport epochs for this ORIGINAL journal, without a SQL transaction
        # across provider/storage IO. A busy/crashed turn never gets a new ID.
        lock = request_hash({"tenant": tenant, "account_deletion": ident})[:32]
        with asset_operation_lock(self.chat.assets.operation_dir, lock) as acquired:
            if not acquired:
                return {"done": False, "state": "pending", "wait_seconds": 10}
            return self._turn(tenant, ident)

    def _turn(self, tenant, ident):
        if tenant != self.tenant:
            return {"done": True, "state": "unavailable"}
        with self.repo.engine.connect() as conn:
            record = self._record(conn, ident)
        if record is None:
            return None
        value = record["payload"]
        if value["state"] == "completed":
            return {"done": True, "state": "completed"}
        if not enabled(self.settings, record["owner"]):
            return {"done": False, "state": "blocked", "wait_seconds": 60}
        try:
            with self.repo.transaction() as conn:
                lock_account_gate(conn, exclusive=True)
                current = self._record(conn, ident)
                self._bound_user(conn, current)
            if value["phase"] == "media":
                with original_cleanup(self.tenant, record["owner"], ident):
                    for index, spec in enumerate(value["plan"]["objects"]):
                        if spec["state"] != "deleted":
                            self.chat._cleanup_one_object(record, index)
                            return {
                                "done": False,
                                "state": "pending",
                                "wait_seconds": 10,
                            }
                self._save(
                    ident,
                    lambda current: current.update(phase="provider", error_code=None),
                )
            elif value["phase"] == "provider":
                if self._provider_turn(record):
                    self._save(
                        ident,
                        lambda current: current.update(
                            phase="finalize", error_code=None
                        ),
                    )
            elif value["phase"] == "finalize":
                self._finalize(ident)
                return {"done": True, "state": "completed"}
        except DeletionError as error:
            self._save(
                ident, lambda current, code=error.code: current.update(error_code=code)
            )
        except Exception:
            self._save(
                ident,
                lambda current: current.update(
                    error_code="account_deletion_reconciliation_required"
                ),
            )
        return {"done": False, "state": "pending", "wait_seconds": 10}

    def _finalize(self, ident):
        from .quick_chat import objects
        from .quick_chat_history import _redact_execution
        from .guided import receipts as edits

        with self.repo.transaction() as conn:
            lock_account_gate(conn, exclusive=True)
            record = self._record(conn, ident)
            owner = record["owner"]
            value = deepcopy(record["payload"])
            with original_cleanup(self.tenant, owner, ident):
                owner_reference_gate(conn, self.tenant, owner)
                if (
                    value["phase"] != "finalize"
                    or value["provider"]["state"] != "deleted"
                    or any(s["state"] != "deleted" for s in value["plan"]["objects"])
                ):
                    raise DeletionError("account_deletion_reconciliation_required")
                # Re-read late financial obligations; never cancel, grant,
                # settle, refund, or rewrite numerical ledgers to proceed.
                user = self._bound_user(conn, record)
                census = self._census(conn, user)
                financial_codes = {
                    "machine_obligations_unresolved",
                    "unsettled_reservations",
                    "credit_holds_unsettled",
                    "checkout_unresolved",
                    "stripe_dispute_open",
                    "stripe_events_unresolved",
                    "paid_credit_balance_requires_review",
                    "financial_schema_unavailable",
                }
                if any(b["code"] in financial_codes for b in census["blockers"]):
                    raise DeletionError("account_deletion_blocked")
                now = self.auth.clock()
                if not value["accounting_released"]:
                    self.chat.assets.finalize_purge(
                        conn,
                        owner,
                        asset_ids=value["plan"]["asset_ids"],
                        artifact_specs=[
                            s
                            for s in value["plan"]["objects"]
                            if s["storage_binding"] != self.chat.assets.storage_binding
                        ],
                        now=now,
                    )
                    value["accounting_released"] = True
                for table in (jobs, plans):
                    for row in self._rows(
                        conn,
                        table,
                        table.c.tenant_id == self.tenant,
                        table.c.owner_id == owner,
                    ):
                        patch = {
                            "request": {"content_deleted": True},
                            "execution_plan": _redact_execution(row["execution_plan"]),
                        }
                        if table is jobs:
                            patch["result"] = {"content_deleted": True}
                        conn.execute(
                            update(table).where(table.c.id == row["id"]).values(**patch)
                        )
                conn.execute(
                    update(documents)
                    .where(
                        documents.c.tenant_id == self.tenant,
                        documents.c.owner_id == owner,
                    )
                    .values(
                        payload={"content_deleted": True},
                        version=documents.c.version + 1,
                        updated_at=now,
                    )
                )
                conn.execute(
                    update(objects)
                    .where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == owner,
                        objects.c.id != ident,
                        objects.c.kind != "content_tombstone",
                    )
                    .values(
                        payload={"content_deleted": True},
                        version=objects.c.version + 1,
                        updated_at=now,
                    )
                )
                if inspect(conn).has_table(edits.name):
                    conn.execute(
                        update(edits)
                        .where(edits.c.tenant == self.tenant, edits.c.owner == owner)
                        .values(response={"content_deleted": True})
                    )
                for artifact_id in value["plan"]["artifact_ids"]:
                    row = (
                        conn.execute(
                            select(artifacts).where(artifacts.c.id == artifact_id)
                        )
                        .mappings()
                        .one()
                    )
                    conn.execute(
                        update(artifacts)
                        .where(artifacts.c.id == artifact_id)
                        .values(metadata={**row["metadata"], "content_deleted": True})
                    )
                changed = conn.execute(
                    update(users)
                    .where(
                        users.c.tenant == self.tenant,
                        users.c.id == value["user_id"],
                        users.c.owner == owner,
                        users.c.status == "suspended",
                        users.c.role == "user",
                    )
                    .values(
                        owner="deleted_" + uuid.UUID(value["user_id"]).hex,
                        display_name="Deleted account",
                        email=None,
                        email_verified=0,
                        workos_user_id=None,
                        terms_version=None,
                        terms_accepted_at=None,
                        status="suspended",
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    raise DeletionError("account_deletion_binding_changed")
                value.update(
                    state="completed",
                    phase="completed",
                    completed_at=now,
                    error_code=None,
                    financial_after=census["financial_fingerprint"],
                )
                value["provider"] = {"state": "deleted"}
                value["plan"]["warnings"] = [
                    {
                        key: warning[key]
                        for key in ("kind", "id", "reason")
                        if key in warning
                    }
                    for warning in value["plan"]["warnings"]
                ]
                self._audit(conn, user["id"], ident, "account_deletion_completed", now)
                conn.execute(
                    update(objects)
                    .where(objects.c.id == ident)
                    .values(
                        payload=value, version=objects.c.version + 1, updated_at=now
                    )
                )
