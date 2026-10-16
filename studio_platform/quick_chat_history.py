"""Non-expiring creation trash over the original Quick Chat authority.

Metadata actions are atomic. Irreversible storage work is journaled first and
reconciles only its original immutable keys, outside every database transaction.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid

from sqlalchemy import delete, inspect, select, update

from .content_lifecycle import (owner_reference_gate, put_tombstone, history_cleanup_activated,
                                assert_history_cleanup_activated)
from .content_references import cleanup_plan, history_blocker
from .repository import Conflict, NotFound, artifacts, documents, jobs, plans, request_hash
from .storage import ObjectNotFound


CLEANUP_SCOPES = ("projects:read", "projects:write", "assets:read", "assets:write", "jobs:read", "jobs:write")
ZERO = {key: 0 for key in ("sessions", "turns", "cards", "assets", "artifacts", "objects", "bytes")}


def _tables():
    from .quick_chat import objects, operations, events
    return objects, operations, events


def _error(code, message="创作记录状态已变化，请重新预览。", status=409):
    from .quick_chat import QuickChatError
    return QuickChatError(code, message, status)


def _redact_execution(value):
    """Keep original execution identities while removing content snapshots."""
    if isinstance(value, dict):
        return {key: {"content_deleted": True} if key in {"request", "workflow", "assets", "inputs",
                    "input_refs", "source_snapshot", "render_snapshot", "timeline"} else
                None if key in {"prompt", "negative_prompt", "text", "caption"} else _redact_execution(child)
                for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_execution(child) for child in value]
    return value


class QuickChatHistoryMixin:
    @staticmethod
    def _history(row):
        return {"trash_state": "active", "is_test": False, "review_hold": False,
            "trashed_at": None, "purge_at": None, "cleanup_consent": False,
            **row["payload"].get("history", {})}

    def _history_public(self, row, principal=None, connection=None):
        history = self._history(row)
        reason = None
        if principal is not None and connection is not None:
            reason = history_blocker(connection, self, principal.owner, row)
        if history["trash_state"] in {"purging", "purged"}:
            reason = "purge_already_started"
        return {**{key: history[key] for key in ("trash_state", "is_test", "review_hold", "trashed_at", "purge_at")},
            "archive_pending": bool(history.get("archive_pending", False)),
            "trashable": history["trash_state"] == "active" and reason is None,
            "blocked_reason": reason,
            "purge_at": None,  # Historical stored deadlines remain audit data only.
            "auto_purge_enabled": False,
            "purge_blocked_reason": "owner_review_required" if history["review_hold"] else
                "cleanup_scope_required" if not history["cleanup_consent"] else None}

    def _history_authorize(self, principal, row, *, cleanup=False):
        scopes = CLEANUP_SCOPES if cleanup else ("projects:read", "projects:write")
        if any(not principal.allows(row["payload"]["project_id"], scope) for scope in scopes):
            raise _error("insufficient_scope", "此操作需要当前创作的项目、素材及任务清理权限。", 403)

    def _history_select(self, connection, principal, body):
        objects, _, _ = _tables()
        if not isinstance(body, dict):
            raise _error("invalid_request", status=422)
        action = body.get("action")
        if not isinstance(action, str) or action not in {"trash", "restore", "purge", "empty_trash"}:
            raise _error("invalid_request", status=422)
        if set(body) - {"action", "sessions"}:
            raise _error("invalid_request", status=422)
        if action == "empty_trash":
            if body.get("sessions") not in (None, []):
                raise _error("invalid_request", status=422)
            # Complete SQL result, never a list endpoint's page/default limit.
            selected = [dict(row) for row in connection.execute(select(objects).where(
                objects.c.tenant == self.tenant, objects.c.owner == principal.owner,
                objects.c.kind == "session").order_by(objects.c.id)).mappings()
                if self._history(row)["trash_state"] == "trashed" and
                    all(principal.allows(row["payload"]["project_id"], scope) for scope in CLEANUP_SCOPES)]
            for row in selected:
                self._history_authorize(principal, row, cleanup=True)
            return selected
        requested = body.get("sessions")
        if not isinstance(requested, list) or not 1 <= len(requested) <= 100:
            raise _error("invalid_request", status=422)
        seen, selected = set(), []
        for item in sorted(requested, key=lambda value: str(value.get("id", "")) if isinstance(value, dict) else ""):
            if (not isinstance(item, dict) or set(item) != {"id", "expected_version"}
                    or not isinstance(item["id"], str) or item["id"] in seen
                    or type(item["expected_version"]) is not int):
                raise _error("invalid_request", status=422)
            seen.add(item["id"])
            row = self._get(connection, principal, item["id"], item["id"], "session", lock=True)
            self._version(row, item["expected_version"])
            self._history_authorize(principal, row, cleanup=action == "purge")
            selected.append(row)
        return selected

    def _history_snapshot(self, connection, principal, body, sessions):
        action = body["action"]
        items = []
        for session in sessions:
            state = self._history(session)["trash_state"]
            reason = ("session_not_active" if action == "trash" and state != "active" else
                "session_not_in_trash" if action != "trash" and state != "trashed" else
                "history_cleanup_not_enabled" if action in {"purge", "empty_trash"}
                    and not history_cleanup_activated(connection, self.tenant) else
                history_blocker(connection, self, principal.owner, session))
            items.append({"id": session["id"], "expected_version": session["version"],
                          "eligible": reason is None, "blocked_reason": reason})
        plan = cleanup_plan(connection, self, principal, sessions) if action in {"purge", "empty_trash"} else None
        result = {"action": action, "items": items, "removed": plan["counts"] if plan else dict(ZERO),
            "kept": plan["kept"] if plan else {key: 0 for key in ("assets", "artifacts", "objects", "bytes")},
            "warnings": plan["warnings"] if plan else [], "ledger_retained": True,
            "review_hold_release_required": action in {"purge", "empty_trash"} and
                any(self._history(row)["review_hold"] for row in sessions),
            "all_content_retained": plan is None, "counts_scope": "purge_only", "plan": plan}
        # Snapshot includes the exact dependency/object plan, never URLs or
        # storage credentials. A newly attached persisted reference conflicts.
        result["snapshot"] = request_hash(result)
        return result

    def preview_history(self, principal, body):
        with self.repo.transaction() as connection:
            owner_reference_gate(connection, self.tenant, principal.owner)
            sessions = self._history_select(connection, principal, body)
            snapshot = self._history_snapshot(connection, principal, body, sessions)
            now = self.repo.clock()
            row = self._new(connection, principal, "__history", "history_preview", {
                "body": body, "snapshot": snapshot, "expires_at": now+600})
            return {"preview_id": row["id"], "expires_at": now+600,
                **{key: value for key, value in snapshot.items() if key not in {"plan", "snapshot"}}}

    def _action_response(self, row, principal):
        payload = row["payload"]
        warnings = [{key: value for key, value in warning.items() if key not in {"project_id", "shot_id", "label"}
                     or principal.allows(warning.get("project_id"), "projects:read")}
                    for warning in payload.get("warnings", [])]
        return {"action_id": row["id"], "action": payload["action"], "state": payload["state"],
            "items": payload["items"], "removed": payload["removed"], "kept": payload["kept"],
            "warnings": warnings, "ledger_retained": True,
            "all_content_retained": payload.get("plan") is None, "counts_scope": "purge_only"}

    def apply_history(self, principal, body, key):
        if (not isinstance(body, dict) or set(body) != {"preview_id", "confirmed"}
                or not isinstance(body["preview_id"], str) or body["confirmed"] is not True):
            raise _error("confirmation_required", status=422)
        # Quota initialization/backfill is separate from the reference gate;
        # storage callbacks must never open nested write transactions under it.
        with self.repo.engine.connect() as read:
            requested_preview = self._get(read, principal, body["preview_id"], kind="history_preview")
            needs_usage = requested_preview["payload"]["body"]["action"] in {"purge", "empty_trash"}
            if needs_usage:
                assert_history_cleanup_activated(read, self.tenant)
                for item in requested_preview["payload"]["snapshot"]["items"]:
                    self._history_authorize(principal, self._get(read, principal, item["id"], item["id"], "session"), cleanup=True)
        if needs_usage:
            self.assets.usage(principal.owner)
        with self.repo.transaction() as connection:
            owner_reference_gate(connection, self.tenant, principal.owner)
            prior = self._replay(connection, principal, "history-action", key, body)
            if prior:
                previous = self._get(connection, principal, prior, kind="history_action")
                for item in previous["payload"]["items"]:
                    session = self._get(connection, principal, item["id"], item["id"], "session")
                    self._history_authorize(principal, session, cleanup=previous["payload"]["action"] in {"purge", "empty_trash"})
                return self._action_response(previous, principal)
            preview = self._get(connection, principal, body["preview_id"], kind="history_preview", lock=True)
            if preview["payload"]["expires_at"] <= self.repo.clock():
                raise _error("history_preview_expired")
            requested = preview["payload"]["body"]
            if requested["action"] in {"purge", "empty_trash"}:
                assert_history_cleanup_activated(connection, self.tenant)
            sessions = self._history_select(connection, principal, requested)
            snapshot = self._history_snapshot(connection, principal, requested, sessions)
            if snapshot["snapshot"] != preview["payload"]["snapshot"]["snapshot"]:
                raise _error("history_preview_changed")
            if any(not item["eligible"] for item in snapshot["items"]):
                raise _error("history_action_blocked", "仍有任务或未决操作，未移动或删除任何创作。")
            action, now = requested["action"], self.repo.clock()
            purging = action in {"purge", "empty_trash"}
            payload = {**snapshot, "state": "pending" if purging else "completed",
                "actor_id": principal.actor_id, "authorized_at": now, "accounting_released": False,
                "items": [{"id": row["id"], "trash_state": "purging" if purging else
                    "trashed" if action == "trash" else "active", "state": "pending" if purging else "completed",
                    "blocked_reason": None} for row in sessions]}
            record = self._new(connection, principal, "__history", "history_action", payload)
            for session in sessions:
                history = self._history(session)
                if action == "trash":
                    history.update(trash_state="trashed", trashed_at=now, purge_at=None,
                        trashed_by=principal.actor_id,
                        cleanup_consent=all(principal.allows(session["payload"]["project_id"], scope) for scope in CLEANUP_SCOPES))
                elif action == "restore":
                    history.update(trash_state="active", trashed_at=None, purge_at=None)
                else:
                    history.update(trash_state="purging", review_hold=False, purge_action_id=record["id"])
                self._put(connection, session, {**session["payload"], "history": history}, bump=True, allow_history=True)
                self._event(connection, principal, session, "history."+action, record["id"])
            if purging:
                plan = payload["plan"]
                for kind, values in (("asset", plan["asset_ids"]), ("artifact", plan["artifact_ids"])):
                    for value in values:
                        put_tombstone(connection, self.tenant, principal.owner, kind=kind, value=value,
                            action_id=record["id"], now=now)
                for spec in plan["objects"]:
                    for binding in spec["bindings"]:
                        put_tombstone(connection, self.tenant, principal.owner, kind="key", value=spec["object_key"],
                            storage_binding=binding, action_id=record["id"], now=now)
            self._remember(connection, principal, "history-action", key, body, record["id"])
            return self._action_response(record, principal)

    def get_history_action(self, principal, action_id):
        with self.repo.engine.connect() as connection:
            row = self._get(connection, principal, action_id, kind="history_action")
            for item in row["payload"]["items"]:
                session = self._get(connection, principal, item["id"], item["id"], "session")
                self._history_authorize(principal, session, cleanup=row["payload"]["action"] in {"purge", "empty_trash"})
            return self._action_response(row, principal)

    def _cleanup_one_object(self, record, index):
        ident = hashlib.sha256(("history-delete-v1:"+record["id"]+":"+str(index)).encode()).hexdigest()[:32]
        # Same existing shared asset operation directory, always OS lock first.
        # Acquiring it proves the previous local handler no longer owns the IO.
        with self.assets._operation(ident):
            return self._cleanup_one_object_locked(record, index)

    def _cleanup_one_object_locked(self, record, index):
        """External IO over one durable intent. Unknown results never restore."""
        from .quick_chat import objects
        owner = record["owner"]
        with self.repo.transaction() as connection:
            owner_reference_gate(connection, self.tenant, owner)
            assert_history_cleanup_activated(connection, self.tenant)
            current = self.repo._locked(connection, select(objects).where(objects.c.id == record["id"]))
            payload = copy.deepcopy(current["payload"])
            spec = payload["plan"]["objects"][index]
            if spec["state"] == "deleted":
                return
            identity = ({"provider": "local", "root": str(self.storage.root)} if hasattr(self.storage, "root") else
                {key: getattr(self.storage.config, key) for key in ("provider", "endpoint_url", "region", "bucket", "service", "profile")})
            bindings = {hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
                hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            if set(spec["bindings"]) != bindings:
                raise Conflict("purge_storage_binding_changed")
            spec.update(busy=True, state="deleting")
            connection.execute(update(objects).where(objects.c.id == current["id"]).values(payload=payload))
        deleted, code = False, None
        try:
            # A bound upload/write receipt and never-reused tombstone key own
            # this exact object. A different current object is retained.
            try:
                info = self.storage.stat(spec["object_key"])
            except ObjectNotFound:
                deleted = True
            else:
                if info.size_bytes != spec["size_bytes"]:
                    code = "storage_identity_changed"
                else:
                    # HEAD metadata is optional and is not a byte proof. Read
                    # the exact receipt-bound immutable key in bounded chunks.
                    digest, size = hashlib.sha256(), 0
                    with self.storage.open(spec["object_key"]) as stream:
                        while True:
                            chunk = stream.read(min(1024*1024, spec["size_bytes"]-size+1))
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > spec["size_bytes"]:
                                break
                            digest.update(chunk)
                    if size != spec["size_bytes"] or digest.hexdigest() != spec["sha256"]:
                        code = "storage_identity_changed"
                    else:
                        fresh = self.storage.stat(spec["object_key"])
                        if (fresh.size_bytes != info.size_bytes or
                                (info.version_id is not None and fresh.version_id != info.version_id) or
                                (info.etag is not None and fresh.etag != info.etag)):
                            code = "storage_identity_changed"
                        else:
                            try:
                                self.storage.delete(spec["object_key"])
                            except Exception:
                                code = "delete_response_unknown"
                            try:
                                self.storage.stat(spec["object_key"])
                            except ObjectNotFound:
                                deleted = True
            if not deleted and code is None:
                code = "delete_not_confirmed"
        except Exception:
            code = "storage_read_unknown"
        with self.repo.transaction() as connection:
            owner_reference_gate(connection, self.tenant, owner)
            current = self.repo._locked(connection, select(objects).where(objects.c.id == record["id"]))
            payload = copy.deepcopy(current["payload"])
            if payload["state"] == "completed":
                return
            value = payload["plan"]["objects"][index]
            value.update(busy=False, state="deleted" if deleted else "unknown", error_code=code,
                checked_at=self.repo.clock())
            connection.execute(update(objects).where(objects.c.id == current["id"]).values(payload=payload))

    def _finish_history_purge(self, record):
        from .quick_chat import objects
        owner = record["owner"]
        with self.repo.transaction() as connection:
            owner_reference_gate(connection, self.tenant, owner)
            assert_history_cleanup_activated(connection, self.tenant)
            current = self.repo._locked(connection, select(objects).where(objects.c.id == record["id"]))
            payload = copy.deepcopy(current["payload"])
            if payload["state"] == "completed" or any(spec["state"] != "deleted" for spec in payload["plan"]["objects"]):
                return
            plan, now = payload["plan"], self.repo.clock()
            if not payload["accounting_released"]:
                artifact_specs = [spec for spec in plan["objects"] if spec["storage_binding"] != self.assets.storage_binding]
                self.assets.finalize_purge(connection, owner, asset_ids=plan["asset_ids"], artifact_specs=artifact_specs, now=now)
                payload["accounting_released"] = True
            # Keep immutable job/attempt/budget IDs, hashes and accounting.
            # Public content snapshots and authoring payloads are redacted.
            for job in connection.execute(select(jobs).where(jobs.c.id.in_(plan["job_ids"]))).mappings():
                connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(
                    request={"content_deleted": True}, execution_plan=_redact_execution(job["execution_plan"]),
                    result={**(job["result"] or {}), "content_deleted": True}))
            for row in connection.execute(select(plans).where(plans.c.tenant_id == self.tenant,
                    plans.c.owner_id == owner, plans.c.project_id.in_(plan["project_ids"]))).mappings():
                connection.execute(update(plans).where(plans.c.id == row["id"]).values(
                    request={"content_deleted": True}, execution_plan=_redact_execution(row["execution_plan"])))
            for ident in plan["artifact_ids"]:
                row = connection.execute(select(artifacts).where(artifacts.c.id == ident)).mappings().one()
                connection.execute(update(artifacts).where(artifacts.c.id == ident).values(
                    metadata={**row["metadata"], "content_deleted": True}))
            connection.execute(delete(objects).where(objects.c.tenant == self.tenant, objects.c.owner == owner,
                objects.c.session_id.in_(plan["session_ids"]), objects.c.kind != "session"))
            for ident in plan["session_ids"]:
                row = dict(connection.execute(select(objects).where(objects.c.id == ident)).mappings().one())
                history = {**self._history(row), "trash_state": "purged", "purged_at": now}
                connection.execute(update(objects).where(objects.c.id == ident).values(
                    payload={"project_id": row["payload"]["project_id"], "history": history, "latest_seq": row["payload"]["latest_seq"]},
                    updated_at=now))
            connection.execute(update(documents).where(documents.c.tenant_id == self.tenant,
                documents.c.owner_id == owner, ((documents.c.project_id == "__projects") &
                    documents.c.document_id.in_(plan["project_ids"])) | documents.c.project_id.in_(plan["project_ids"])).values(
                    payload={"content_deleted": True}))
            from .guided import receipts as edit_receipts
            if inspect(connection).has_table(edit_receipts.name):
                connection.execute(update(edit_receipts).where(edit_receipts.c.tenant == self.tenant,
                    edit_receipts.c.owner == owner, edit_receipts.c.project_id.in_(plan["project_ids"])).values(
                        response={"content_deleted": True}))
            payload.update(state="completed", completed_at=now)
            payload["items"] = [{**item, "state": "completed", "trash_state": "purged"} for item in payload["items"]]
            connection.execute(update(objects).where(objects.c.id == current["id"]).values(payload=payload, updated_at=now))

    def process_history_cleanup(self, *, limit=10, max_objects=10):
        from .quick_chat import objects
        if type(limit) is not int or type(max_objects) is not int or not 1 <= limit <= 100 or not 1 <= max_objects <= 100:
            raise ValueError("invalid_history_cleanup_limit")
        with self.repo.transaction() as connection:
            # Explicitly accepted test records wait for genuine terminal work,
            # then archive once; review holds remain attached after archiving.
            archive = [dict(row) for row in connection.execute(select(objects).where(objects.c.tenant == self.tenant,
                objects.c.kind == "session").order_by(objects.c.owner, objects.c.id)).mappings() if self._history(row)["trash_state"] == "active"
                and self._history(row).get("archive_pending")][:limit]
            for row in archive:
                owner_reference_gate(connection, self.tenant, row["owner"])
                current = dict(self.repo._locked(connection, select(objects).where(objects.c.id == row["id"])))
                if (self._history(current)["trash_state"] == "active" and self._history(current).get("archive_pending")
                        and history_blocker(connection, self, row["owner"], current) is None):
                    now = self.repo.clock()
                    history = {**self._history(current), "archive_pending": False, "trash_state": "trashed",
                        "trashed_at": now, "purge_at": None}
                    self._put(connection, current, {**current["payload"], "history": history}, bump=True, allow_history=True)
                    from .auth import Principal
                    self._event(connection, Principal(row["owner"], "history-test-archive"), current,
                        "history.test_archive", current["id"])
        with self.repo.engine.connect() as connection:
            if not history_cleanup_activated(connection, self.tenant):
                return {"actions_checked": 0, "objects_checked": 0}
        # Never create deletion intents from elapsed time or historical consent.
        # Continue only previously confirmed durable actions below.
        with self.repo.engine.connect() as connection:
            pending = [dict(row) for row in connection.execute(select(objects).where(objects.c.tenant == self.tenant,
                objects.c.kind == "history_action", objects.c.payload["state"].as_string() == "pending").order_by(objects.c.id).limit(limit)).mappings()]
        performed = 0
        for row in pending:
            for index, spec in enumerate(row["payload"]["plan"]["objects"]):
                if performed >= max_objects:
                    break
                if spec["state"] != "deleted":
                    self._cleanup_one_object(row, index)
                    performed += 1
            self._finish_history_purge(row)
        return {"actions_checked": len(pending), "objects_checked": performed}
