"""Durable chat-native creation. Provider calls only through explicit hooks.

The project/shot document is an execution projection, never a second editable
source. All state uses the platform database; no timers, fake replies or GPUs.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import copy
import hashlib
import io
import json
import math
import secrets
import uuid

from sqlalchemy import (
    Column,
    Float,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    or_,
    insert,
    select,
    update,
    func,
)

from .auth import Principal
from .capabilities import RECIPES, VERSION, version_matches
from .content_policy import ContentPolicyError
from .credits import CreditError
from .generation_draft import (
    input_entries,
    prepare_patch,
    configure,
    validate_controls_patch,
    validate_range,
)
from .guided import empty_project, new_entity, now_iso
from .project_validation import ID, validate_project
from .project_activity import append_activity
from .repository import (
    Conflict,
    NotFound,
    Scope,
    BudgetExceeded,
    canonical,
    request_hash,
    documents,
    jobs,
    attempts,
    artifacts,
)
from .quick_chat_takes import QuickChatTakesMixin, INTEGER_MAX
from .quick_chat_titles import (
    DEFAULT_TITLE,
    TITLE_MODEL,
    QuickChatTitleMixin,
    public_title_state,
)
from .quick_chat_history import QuickChatHistoryMixin, CLEANUP_SCOPES
from .content_lifecycle import (
    owner_reference_gate,
    assert_project_writable,
    assert_reference_payload,
)


from .assistant_models import DEFAULT_MODEL, resolve as resolve_assistant

EMPTY_INPUTS = {
    "first_frame": None,
    "last_frame": None,
    "images": [],
    "videos": [],
    "audios": [],
    "guides": [],
}
DEFAULT_SETTINGS = {
    "recipe_id": "h3-base-fl2va-v1",
    "controls": {"duration": 5, "resolution": "480P"},
    "copies": 1,
}
ADMISSION_REFRESH_CODES = frozenset(
    {
        "execution_policy_changed_or_unavailable",
        "capacity_approval_unavailable",
        "configuration_output_delivery_conflict",
        "job_reservation_requires_reconciliation",
        "upstream_stop_unconfirmed",
        "shot_version_conflict",
        "plan_expired",
        "budget_exceeded",
        "quick_chat_execution_required",
        "plan_not_available",
        "storyboard_defaults_changed",
        "chapter_timeline_changed",
        "preflight_stale",
    }
)


def default_next_settings(settings):
    """New sessions only; changing the default never migrates authored history."""
    from .execution_profiles import default_profile_id
    from .inference.wangp_profile_dispatch import control_schema

    result = copy.deepcopy(DEFAULT_SETTINGS)
    profile_id = default_profile_id(settings)
    if profile_id is not None:
        result["deployment_profile_id"] = profile_id
        result["controls"] = {
            key: copy.deepcopy(value["default"])
            for key, value in control_schema(profile_id, "fl").items()
            if "default" in value and key not in {'guides', 'video_audio'}
        }
    return result


metadata = MetaData()
objects = Table(
    "platform_quick_chat_objects",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("tenant", String(200), nullable=False),
    Column("owner", String(200), nullable=False),
    Column("session_id", String(80), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("parent_id", String(80)),
    Column("business_key", String(200)),
    Column("version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("tenant", "owner", "kind", "business_key"),
)
operations = Table(
    "platform_quick_chat_operations",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("owner", String(200), primary_key=True),
    Column("namespace", String(200), primary_key=True),
    Column("key", String(160), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("object_id", String(80), nullable=False),
    Column("created_at", Float, nullable=False),
)
events = Table(
    "platform_quick_chat_events",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("tenant", String(200), nullable=False),
    Column("owner", String(200), nullable=False),
    Column("session_id", String(80), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("type", String(60), nullable=False),
    Column("resource_id", String(80), nullable=False),
    Column("actor_id", String(200), nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("tenant", "owner", "session_id", "seq"),
)


class QuickChatError(Exception):
    def __init__(
        self, code, message, status=409, *, retryable=False, field_errors=None
    ):
        self.code, self.message, self.status, self.retryable = (
            code,
            message,
            status,
            retryable,
        )
        self.field_errors = field_errors
        super().__init__(code)


@dataclass
class QuickChatHooks:
    preflight: object
    create_planned: object
    enqueue: object
    public_job: object
    cancel: object
    refresh_planned: object = None


def fields(body, allowed, required=()):
    if (
        not isinstance(body, dict)
        or set(body) - set(allowed)
        or not set(required) <= set(body)
    ):
        raise QuickChatError("invalid_request", "请求字段不符合创作契约。", 422)
    return canonical(body)


def key_check(key):
    if not isinstance(key, str) or not ID.fullmatch(key):
        raise QuickChatError(
            "invalid_idempotency_key", "请提供稳定的1–160字符Idempotency-Key。", 422
        )


def settings_check(value):
    fields(
        value,
        {"recipe_id", "controls", "copies", "deployment_profile_id"},
        {"recipe_id", "controls", "copies"},
    )
    if value.get("deployment_profile_id") is not None:
        from .runtime_catalog import get_profile

        get_profile(value["deployment_profile_id"])
    if (
        value["recipe_id"] not in RECIPES
        or type(value["copies"]) is not int
        or not 1 <= value["copies"] <= INTEGER_MAX
    ):
        raise QuickChatError("invalid_settings", "生成方式或份数无效。", 422)
    validate_controls_patch(value["controls"])
    return canonical(value)


def model_check(value):
    model = resolve_assistant(value)
    if model is None:
        raise QuickChatError(
            "model_not_allowed", "请选择已提供的助手选项；不会自动切换。", 422
        )
    return model


def cursor_encode(principal, session_id, boundary, direction):
    value = {
        "owner": principal.owner,
        "session": session_id,
        "boundary": boundary,
        "direction": direction,
    }
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def cursor_decode(value, principal, session_id, direction):
    try:
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError()
        obj = json.loads(base64.urlsafe_b64decode(value + "=" * ((-len(value)) % 4)))
        if (
            set(obj) != {"owner", "session", "boundary", "direction"}
            or obj["owner"] != principal.owner
            or obj["session"] != session_id
            or obj["direction"] != direction
        ):
            raise ValueError()
        return obj["boundary"]
    except Exception:
        raise QuickChatError(
            "invalid_cursor", "分页位置无效，请重新加载记录。", 422
        ) from None


class QuickChatService(QuickChatTakesMixin, QuickChatHistoryMixin, QuickChatTitleMixin):
    def __init__(
        self,
        repository,
        assets,
        settings,
        storage,
        *,
        hooks=None,
        assistant=None,
        assistant_enabled=False,
        title_generator=None,
    ):
        self.repo, self.assets, self.settings, self.storage = (
            repository,
            assets,
            settings,
            storage,
        )
        self.tenant, self.hooks = settings.tenant_id, hooks
        self.assistant, self.assistant_enabled = assistant, assistant_enabled
        self.title_generator = title_generator
        with repository.transaction() as conn:
            if repository.engine.dialect.name == "postgresql":
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(685939796868749721)")
            metadata.create_all(conn)

    def _where(self, principal, ident, session_id=None, kind=None):
        clauses = [
            objects.c.tenant == self.tenant,
            objects.c.owner == principal.owner,
            objects.c.id == ident,
        ]
        if session_id is not None:
            clauses.append(objects.c.session_id == session_id)
        if kind is not None:
            clauses.append(objects.c.kind == kind)
        return clauses

    def _get(self, conn, principal, ident, session_id=None, kind=None, *, lock=False):
        if lock:
            owner_reference_gate(conn, self.tenant, principal.owner)
        statement = select(objects).where(
            *self._where(principal, ident, session_id, kind)
        )
        row = (
            self.repo._locked(conn, statement)
            if lock
            else conn.execute(statement).mappings().first()
        )
        if row is None:
            raise NotFound("quick_chat_resource_not_found")
        return dict(row)

    def _new(
        self,
        conn,
        principal,
        session_id,
        kind,
        payload,
        *,
        ident=None,
        parent=None,
        business=None,
    ):
        owner_reference_gate(conn, self.tenant, principal.owner)
        if kind not in {
            "session",
            "history_preview",
            "history_action",
            "content_tombstone",
        }:
            session = self._get(conn, principal, session_id, session_id, "session")
            assert_project_writable(
                conn, self.tenant, principal.owner, session["payload"]["project_id"]
            )
            assert_reference_payload(conn, self.tenant, principal.owner, payload)
        now = self.repo.clock()
        row = dict(
            id=ident or kind + "-" + uuid.uuid4().hex,
            tenant=self.tenant,
            owner=principal.owner,
            session_id=session_id,
            kind=kind,
            parent_id=parent,
            business_key=business,
            version=1,
            payload=canonical(payload),
            created_at=now,
            updated_at=now,
        )
        conn.execute(insert(objects).values(**row))
        if kind in {'session', 'revision'}:
            from .agent_management import record_created, creator

            record_created(conn, self.tenant, principal, kind, row['id'], now)
            if kind == 'revision':
                for item in payload['items']:
                    record_created(
                        conn, self.tenant, principal, 'take', item['id'], now
                    )
            row['created_by'] = creator(
                conn, self.tenant, principal.owner, kind, row['id']
            )
        return row

    def _put(self, conn, row, payload, *, bump=False, allow_history=False):
        owner_reference_gate(conn, self.tenant, row["owner"])
        if (
            row["kind"]
            not in {"history_preview", "history_action", "content_tombstone"}
            and not allow_history
        ):
            session = (
                row
                if row["kind"] == "session"
                else conn.execute(
                    select(objects).where(
                        objects.c.id == row["session_id"],
                        objects.c.tenant == self.tenant,
                        objects.c.owner == row["owner"],
                        objects.c.kind == "session",
                    )
                )
                .mappings()
                .one()
            )
            assert_project_writable(
                conn, self.tenant, row["owner"], session["payload"]["project_id"]
            )
            assert_reference_payload(conn, self.tenant, row["owner"], payload)
        values = dict(
            payload=canonical(payload),
            updated_at=self.repo.clock(),
            version=row["version"] + int(bump),
        )
        conn.execute(update(objects).where(objects.c.id == row["id"]).values(**values))
        row.update(values)
        return row

    def _access(self, principal, session_id, *scopes, conn=None, lock=False):
        if conn is None:
            with self.repo.engine.connect() as read:
                return self._access(principal, session_id, *scopes, conn=read)
        mutating = any(scope.endswith(":write") for scope in scopes)
        if lock or mutating:
            owner_reference_gate(conn, self.tenant, principal.owner)
        row = self._get(conn, principal, session_id, session_id, "session", lock=lock)
        if any(
            not principal.allows(row["payload"]["project_id"], scope)
            for scope in scopes
        ):
            raise NotFound("quick_chat_session_not_found")
        if mutating:
            from .quick_chat_migration import assert_legacy_mutable

            assert_legacy_mutable(conn, self.tenant, principal.owner, (session_id,))
        state = self._history(row)["trash_state"]
        if state == "purged":
            raise QuickChatError(
                "session_purged", "创作记录已永久删除，原账本记录保留。", 410
            )
        if mutating and state != "active":
            raise QuickChatError("session_not_active", "请先从回收站恢复创作。")
        return row

    def _replay(self, conn, principal, namespace, key, body):
        key_check(key)
        row = (
            conn.execute(
                select(operations).where(
                    operations.c.tenant == self.tenant,
                    operations.c.owner == principal.owner,
                    operations.c.namespace == namespace,
                    operations.c.key == key,
                )
            )
            .mappings()
            .first()
        )
        if row:
            if row["fingerprint"] != request_hash(body):
                raise QuickChatError(
                    "idempotency_conflict", "同一操作Key不能使用不同内容。"
                )
            return row["object_id"]

    def _remember(self, conn, principal, namespace, key, body, ident):
        conn.execute(
            insert(operations).values(
                tenant=self.tenant,
                owner=principal.owner,
                namespace=namespace,
                key=key,
                fingerprint=request_hash(body),
                object_id=ident,
                created_at=self.repo.clock(),
            )
        )

    def _event(self, conn, principal, session, event_type, resource_id):
        from .agent_management import record_event

        record_event(conn, self.tenant, principal, event_type, self.repo.clock())
        payload = copy.deepcopy(session["payload"])
        payload["latest_seq"] += 1
        conn.execute(
            insert(events).values(
                id="event-" + uuid.uuid4().hex,
                tenant=self.tenant,
                owner=principal.owner,
                session_id=session["id"],
                seq=payload["latest_seq"],
                type=event_type,
                resource_id=resource_id,
                actor_id=principal.actor_id,
                created_at=self.repo.clock(),
            )
        )
        self._put(
            conn, session, payload, allow_history=event_type.startswith("history.")
        )
        return payload["latest_seq"]

    @staticmethod
    def _version(row, expected):
        if type(expected) is not int or expected != row["version"]:
            raise QuickChatError(
                "version_conflict", "内容已被修改；请保留本地草稿并读取当前版本。"
            )

    @staticmethod
    def _effective_bindings(bindings, recipe_id, deployment_profile_id=None):
        mode = RECIPES[recipe_id]
        from .h3_profile_support import full_controls

        native_roles = bool(
            deployment_profile_id and full_controls(deployment_profile_id)
        )
        values = []
        for binding in bindings:
            incompatible = not native_roles and (
                (mode == "fl" and binding["slot"] in {"images", "videos", "audios"})
                or (mode == "ref" and binding["slot"] in {"first_frame", "last_frame"})
            )
            enabled = binding["enabled"] and not incompatible
            values.append(
                {
                    **copy.deepcopy(binding),
                    "requested_enabled": binding["enabled"],
                    "enabled": enabled,
                    "inactive_reason": "mode_incompatible"
                    if binding["enabled"] and incompatible
                    else None
                    if enabled
                    else "not_selected",
                }
            )
        return values

    @staticmethod
    def _inputs(bindings, recipe_id=None, deployment_profile_id=None):
        if recipe_id is not None:
            bindings = QuickChatService._effective_bindings(
                bindings, recipe_id, deployment_profile_id
            )
        result = copy.deepcopy(EMPTY_INPUTS)
        for item in bindings:
            if not item["enabled"]:
                continue
            slot = item["slot"]
            value = {"media_id" if slot == "guides" else "asset_id": item["asset_id"]}
            for field in (
                ("time_seconds", "use_audio", "source_range")
                if slot == "guides"
                else ("purpose", "source_range", "include_audio")
                if slot == "videos"
                else ()
                if slot in {"first_frame", "last_frame"}
                else ("purpose", "source_range")
            ):
                if field in item:
                    value[field] = item[field]
            if slot in {"first_frame", "last_frame"}:
                if result[slot] is not None:
                    raise QuickChatError(
                        "reference_conflict", "首帧和尾帧分别只能使用一张图片。", 422
                    )
                result[slot] = value
            else:
                result[slot].append(value)
        input_entries(result)
        return result

    def _creation_public(self, row, kind):
        if 'created_by' in row:
            return row['created_by']
        from .agent_management import creator

        with self.repo.engine.connect() as conn:
            return creator(conn, self.tenant, row['owner'], kind, row['id'])

    def _session_public(self, row):
        p = row["payload"]
        return {
            "id": row["id"],
            "title": p["title"],
            "version": row["version"],
            "model_id": p["model_id"],
            **self._history_public(row),
            "title_generation": public_title_state(p),
            "composer_reset": p.get("composer_reset"),
            "next_settings": p["next_settings"],
            "input_refs": self._inputs(
                p["bindings"],
                p["next_settings"]["recipe_id"],
                p['next_settings'].get('deployment_profile_id'),
            ),
            "latest_seq": p["latest_seq"],
            "web_url": "/quick-chat?session=" + row["id"],
            "migrated_project_id": None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "created_by": self._creation_public(row, "session"),
        }

    def create_session(self, principal, body, key):
        fields(body, {"title", "model_id", "is_test"})
        key_check(key)
        if "is_test" in body and type(body["is_test"]) is not bool:
            raise QuickChatError("invalid_request", "测试标记需要布尔值。", 422)
        if principal.machine and not (
            principal.all_projects
            and all(
                s in principal.scopes
                for s in ("projects:create", "projects:read", "projects:write")
            )
        ):
            raise QuickChatError(
                "insufficient_scope", "创建会话需要本人全部项目的创作权限。", 403
            )
        title = body.get("title", DEFAULT_TITLE)
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise QuickChatError("invalid_title", "会话标题须为1–160字符。", 422)
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            prior = self._replay(conn, principal, "session-create", key, body)
            if prior:
                return {
                    "session": self._session_public(
                        self._access(principal, prior, "projects:read", conn=conn)
                    )
                }
            model = model_check(body.get("model_id", DEFAULT_MODEL))
            ident = "session-" + uuid.uuid4().hex
            project = empty_project(
                {
                    "id": "chat-workspace-" + uuid.uuid4().hex,
                    "title": title,
                    "workspace": "freestyle",
                }
            )
            project["integration_kind"] = "quick_chat"
            project["journey"].update(
                integration_kind="quick_chat", quickChatSessionId=ident
            )
            validate_project(project, self.settings.max_project_bytes)
            conn.execute(
                insert(documents).values(
                    tenant_id=self.tenant,
                    owner_id=principal.owner,
                    project_id="__projects",
                    kind="project",
                    document_id=project["id"],
                    payload=project,
                    version=1,
                    updated_at=self.repo.clock(),
                )
            )
            append_activity(
                conn,
                tenant_id=self.tenant,
                principal=principal,
                project_id=project["id"],
                version=1,
                occurred_at=self.repo.clock(),
                before=None,
                after=project,
                event_type="project.created",
            )
            session = self._new(
                conn,
                principal,
                ident,
                "session",
                {
                    "title": title,
                    "model_id": model,
                    "project_id": project["id"],
                    "next_settings": default_next_settings(self.settings),
                    "bindings": [],
                    "latest_seq": 0,
                    "active_turn_id": None,
                    "history": {"is_test": body.get("is_test", False)},
                    "title_generation": self._initial_title_state(
                        title_supplied="title" in body
                    ),
                },
                ident=ident,
            )
            self._remember(conn, principal, "session-create", key, body, ident)
            return {"session": self._session_public(session)}

    def get_session(self, principal, session_id):
        from .quick_chat_migration import migrated_project_id

        with self.repo.engine.connect() as connection:
            row = self._access(principal, session_id, "projects:read", conn=connection)
            return {
                "session": {
                    **self._session_public(row),
                    "migrated_project_id": migrated_project_id(
                        connection, self.tenant, principal.owner, row
                    ),
                }
            }

    def list_sessions(
        self, principal, *, limit=20, cursor=None, view="active", hide_tests=False
    ):
        if view not in {"active", "trash"} or type(hide_tests) is not bool:
            raise QuickChatError("invalid_request", "创作记录视图无效。", 422)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise QuickChatError("invalid_limit", "分页大小须为1–50。", 422)
        cursor_scope = (
            "__sessions"
            if view == "active" and not hide_tests
            else "__sessions:" + view + ":" + str(hide_tests)
        )
        boundary = (
            cursor_decode(cursor, principal, cursor_scope, "older") if cursor else None
        )
        stmt = select(objects).where(
            objects.c.tenant == self.tenant,
            objects.c.owner == principal.owner,
            objects.c.kind == "session",
        )
        state = func.coalesce(
            objects.c.payload["history"]["trash_state"].as_string(), "active"
        )
        stmt = stmt.where(
            state == "active" if view == "active" else state.in_(("trashed", "purging"))
        )
        if hide_tests:
            stmt = stmt.where(
                func.coalesce(
                    objects.c.payload["history"]["is_test"].as_boolean(), False
                )
                == False
            )
        if boundary:
            if not isinstance(boundary, list) or len(boundary) != 2:
                raise QuickChatError("invalid_cursor", "分页位置无效。", 422)
            from sqlalchemy import or_, and_

            stmt = stmt.where(
                or_(
                    objects.c.created_at < boundary[0],
                    and_(
                        objects.c.created_at == boundary[0], objects.c.id < boundary[1]
                    ),
                )
            )
        # Filter before pagination; do not let inaccessible rows create empty pages.
        if principal.machine:
            if "projects:read" not in principal.scopes:
                return {
                    "sessions": [],
                    "limit": limit,
                    "has_more": False,
                    "next_cursor": None,
                }
            if not principal.all_projects:
                stmt = stmt.where(
                    objects.c.payload["project_id"]
                    .as_string()
                    .in_(principal.project_ids)
                )
        with self.repo.engine.connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    stmt.order_by(
                        objects.c.created_at.desc(), objects.c.id.desc()
                    ).limit(limit + 1)
                ).mappings()
            ]
            values = [
                {
                    **{
                        k: v
                        for k, v in self._session_public(row).items()
                        if k not in {"input_refs", "next_settings"}
                    },
                    **self._history_public(row, principal, conn),
                }
                for row in rows[:limit]
            ]
        return {
            "sessions": values,
            "limit": limit,
            "has_more": len(rows) > limit,
            "as_of": self.repo.clock(),
            "next_cursor": cursor_encode(
                principal,
                cursor_scope,
                [rows[limit - 1]["created_at"], rows[limit - 1]["id"]],
                "older",
            )
            if len(rows) > limit
            else None,
        }

    def patch_session(self, principal, session_id, body, key):
        fields(
            body,
            {
                "expected_version",
                "title",
                "model_id",
                "next_settings",
                "is_test",
                "test_accepted",
                "review_hold",
            },
            {"expected_version"},
        )
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            session = self._get(
                conn, principal, session_id, session_id, "session", lock=True
            )
            self._history_authorize(principal, session)
            prior = self._replay(
                conn, principal, "session-patch:" + session_id, key, body
            )
            if prior:
                if self._history(session)["trash_state"] == "purged":
                    raise QuickChatError(
                        "session_purged", "创作记录已永久删除，原账本记录保留。", 410
                    )
                return {"session": self._session_public(session)}
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "projects:write",
                conn=conn,
                lock=True,
            )
            self._version(session, body["expected_version"])
            p = copy.deepcopy(session["payload"])
            if "title" in body:
                if (
                    not isinstance(body["title"], str)
                    or not body["title"].strip()
                    or len(body["title"]) > 160
                ):
                    raise QuickChatError("invalid_title", "会话标题无效。", 422)
                p["title"] = body["title"]
                # Even explicitly choosing the default label is a manual name.
                # Fence pending/late supplier responses without cancelling or
                # replaying the already claimed upstream request.
                p["title_generation"] = {
                    "status": "manual",
                    "model_id": TITLE_MODEL,
                    "error_code": None,
                }
            if "model_id" in body:
                p["model_id"] = model_check(body["model_id"])
            if "next_settings" in body:
                from .quality_authoring import normalize

                selected, _ = normalize(
                    self.settings, self.repo, principal, body["next_settings"]
                )
                p["next_settings"] = settings_check(selected)
                self._copies_allowed(principal, p["next_settings"]["copies"], conn)
            if any(
                name in body for name in ("is_test", "test_accepted", "review_hold")
            ):
                if any(
                    type(body[name]) is not bool
                    for name in ("is_test", "test_accepted", "review_hold")
                    if name in body
                ):
                    raise QuickChatError("invalid_request", "测试标记需要布尔值。", 422)
                history = self._history(session)
                if "is_test" in body:
                    history["is_test"] = body["is_test"]
                if "review_hold" in body:
                    if body["review_hold"] is not True:
                        raise QuickChatError(
                            "review_hold_requires_purge_confirmation",
                            "审核保留只能在明确永久删除确认时解除。",
                        )
                    history["review_hold"] = True
                if body.get("test_accepted"):
                    if not history["is_test"]:
                        raise QuickChatError("test_tag_required", "请先标记测试记录。")
                    history.update(
                        archive_pending=True,
                        acceptance_actor=principal.actor_id,
                        cleanup_consent=all(
                            principal.allows(p["project_id"], scope)
                            for scope in CLEANUP_SCOPES
                        ),
                    )
                p["history"] = history
            self._put(conn, session, p, bump=True)
            self._event(conn, principal, session, "session.updated", session_id)
            self._remember(
                conn, principal, "session-patch:" + session_id, key, body, session_id
            )
            return {"session": self._session_public(session)}

    def upload(self, principal, session_id, source, filename, client_asset_id):
        session = self._access(principal, session_id, "assets:write")
        value = self.assets.upload(
            principal.owner,
            session["payload"]["project_id"],
            source,
            filename,
            client_asset_id=client_asset_id,
        )
        return {**value, "session_id": session_id}

    def _asset(self, principal, session, asset_id):
        if not principal.allows(session["payload"]["project_id"], "assets:read"):
            raise NotFound("asset_not_found")
        value = self.assets.get(
            principal.owner, asset_id, session["payload"]["project_id"]
        )
        if value["status"] != "ready":
            raise QuickChatError(
                "reference_not_ready", "素材尚未校验完成；请恢复同一上传收据。"
            )
        return value

    def materials(self, principal, session_id):
        session = self._access(principal, session_id, "projects:read", "assets:read")
        catalog = {
            v["asset_id"]: v
            for v in self.assets.list(principal.owner, session["payload"]["project_id"])
        }
        bindings = [
            {**b, "asset": catalog.get(b["asset_id"])}
            for b in session["payload"]["bindings"]
        ]
        bound = {b["asset_id"] for b in bindings}
        for asset_id, asset in catalog.items():
            if asset_id not in bound:
                bindings.append(
                    {
                        "binding_id": "binding-"
                        + uuid.uuid5(
                            uuid.NAMESPACE_URL, session_id + ":" + asset_id
                        ).hex,
                        "version": 0,
                        "asset_id": asset_id,
                        "kind": asset["kind"],
                        "slot": {
                            "image": "images",
                            "video": "videos",
                            "audio": "audios",
                        }[asset["kind"]],
                        "purpose": {
                            "image": "reference",
                            "video": "motion",
                            "audio": "audio",
                        }[asset["kind"]],
                        "enabled": False,
                        "asset": asset,
                    }
                )
        recipe = session["payload"]["next_settings"]["recipe_id"]
        profile_id = session['payload']['next_settings'].get('deployment_profile_id')
        effective = self._effective_bindings(bindings, recipe, profile_id)
        for binding, participation in zip(bindings, effective):
            binding.update(
                effective_enabled=participation["enabled"],
                inactive_reason=participation["inactive_reason"],
            )
        return {
            "bindings": bindings,
            "session_version": session["version"],
            "active_recipe_id": recipe,
            "input_refs": self._inputs(
                session["payload"]["bindings"], recipe, profile_id
            ),
        }

    def put_materials(self, principal, session_id, body, key):
        fields(body, {"expected_version", "bindings"}, {"expected_version", "bindings"})
        session = self._access(
            principal, session_id, "projects:read", "projects:write", "assets:read"
        )
        if not isinstance(body["bindings"], list) or len(body["bindings"]) > 100:
            raise QuickChatError("invalid_bindings", "材料选择最多100个条目。", 422)
        prepared, seen = [], set()
        for item in body["bindings"]:
            fields(
                item,
                {
                    "binding_id",
                    "version",
                    "asset_id",
                    "kind",
                    "slot",
                    "purpose",
                    "enabled",
                    "source_range",
                    "include_audio",
                    "time_seconds",
                    "use_audio",
                },
                {"binding_id", "version", "asset_id", "kind", "slot", "enabled"},
            )
            if (
                not isinstance(item["binding_id"], str)
                or not ID.fullmatch(item["binding_id"])
                or item["binding_id"] in seen
            ):
                raise QuickChatError("invalid_bindings", "素材引用ID重复或无效。", 422)
            if (
                type(item["version"]) is not int
                or item["version"] < 0
                or type(item["enabled"]) is not bool
            ):
                raise QuickChatError(
                    "invalid_bindings", "素材引用版本或参与状态无效。", 422
                )
            value = self._asset(principal, session, item["asset_id"])
            if value["kind"] != item["kind"] or item["slot"] not in EMPTY_INPUTS:
                raise QuickChatError(
                    "invalid_bindings", "素材种类与上传收据不匹配。", 422
                )
            seen.add(item["binding_id"])
            # Validate syntax and type even for a currently disabled reference.
            candidate = {**item, "enabled": True}
            refs = self._inputs([candidate])
            prepare_patch({"inputs": refs}, lambda _ident: self._source(value))
            prepared.append(canonical(item))
        self._inputs(prepared)
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "projects:write",
                "assets:read",
                conn=conn,
                lock=True,
            )
            namespace = "materials:" + session_id
            prior = self._replay(conn, principal, namespace, key, body)
            if not prior:
                self._version(session, body["expected_version"])
                old = {v["binding_id"]: v for v in session["payload"]["bindings"]}
                values = []
                for value in prepared:
                    existing = old.pop(value["binding_id"], None)
                    if value["version"] != (existing["version"] if existing else 0):
                        raise QuickChatError(
                            "version_conflict", "素材引用已变化，请重新加载。"
                        )
                    if existing and existing["asset_id"] != value["asset_id"]:
                        raise QuickChatError(
                            "reference_conflict", "同一引用ID不能更换原始素材。"
                        )
                    values.append(
                        {
                            **value,
                            "version": value["version"]
                            + int(existing is None or value != existing),
                        }
                    )
                values.extend(
                    {**v, "enabled": False, "version": v["version"] + int(v["enabled"])}
                    for v in old.values()
                )
                p = {**session["payload"], "bindings": values}
                self._put(conn, session, p, bump=True)
                self._event(conn, principal, session, "materials.updated", session_id)
                self._remember(conn, principal, namespace, key, body, session_id)
        return self.materials(principal, session_id)

    @staticmethod
    def _source(asset):
        return {
            "type": asset["kind"],
            "title": asset["file_name"],
            "data": {
                "fileId": "cloud_asset_" + asset["asset_id"],
                "cloudAssetId": asset["asset_id"],
                "cloudContentPath": "/v1/assets/" + asset["asset_id"] + "/content",
                "fileName": asset["file_name"],
                "mime": asset["mime"],
                "metadata": asset["metadata"],
                "missingFile": False,
                "source": "upload",
            },
        }

    def _card_snapshot(self, principal, session, body):
        settings_check(
            {
                k: body[k]
                for k in ("recipe_id", "controls", "copies", "deployment_profile_id")
                if k in body
            }
        )
        if (
            not isinstance(body["prompt"], str)
            or not body["prompt"].strip()
            or len(body["prompt"]) > 12000
        ):
            raise QuickChatError(
                "invalid_prompt", "任务卡需要1–12000字符的完整提示词。", 422
            )
        sources, provenance = {}, []
        for slot, entry, ident in input_entries(body["inputs"]):
            # Card inputs are an independent explicit snapshot. Uploading a
            # receipt to this session is sufficient; enabling next-turn
            # bindings is not required and is never done as a side effect.
            asset = self._asset(principal, session, ident)
            sources[ident] = self._source(asset)
            provenance.append(
                {
                    "asset_id": ident,
                    "sha256": asset["original"]["sha256"],
                    "source_range": entry.get("source_range"),
                    "slot": slot,
                }
            )
        prepare_patch(body, lambda ident: sources[ident])
        inputs = {**copy.deepcopy(EMPTY_INPUTS), **canonical(body["inputs"])}
        # Intent may be incomplete, but mixing ordinary FL/REF references is never silently fixed.
        from .h3_profile_support import full_controls

        native_roles = bool(
            body.get('deployment_profile_id')
            and full_controls(body['deployment_profile_id'])
        )
        if (
            not native_roles
            and RECIPES[body["recipe_id"]] == "fl"
            and any(inputs[k] for k in ("images", "videos", "audios"))
        ):
            raise QuickChatError(
                "reference_conflict", "首尾帧方式不能同时使用普通参考。", 422
            )
        if (
            not native_roles
            and RECIPES[body["recipe_id"]] == "ref"
            and (inputs["first_frame"] or inputs["last_frame"])
        ):
            raise QuickChatError(
                "reference_conflict", "全能参考方式不能同时指定首尾帧。", 422
            )
        snapshot = {
            k: canonical(body[k]) for k in ("prompt", "recipe_id", "controls", "copies")
        }
        if body.get("deployment_profile_id") is not None:
            snapshot["deployment_profile_id"] = body["deployment_profile_id"]
        snapshot.update(
            inputs=inputs, title=body.get("title", "我的视频"), provenance=provenance
        )
        if (
            not isinstance(snapshot["title"], str)
            or not snapshot["title"].strip()
            or len(snapshot["title"]) > 160
        ):
            raise QuickChatError("invalid_title", "卡片标题须为1–160字符。", 422)
        return snapshot, sources

    @staticmethod
    def _composer_snapshot(
        bindings, *, inputs=None, recipe_id=None, deployment_profile_id=None
    ):
        """Freeze authoring versions, optionally only inputs of a legacy card.

        Legacy requests do not carry a composer origin. A new explicit edit or
        preflight can associate currently selected references, but must not
        claim unrelated inputs belonging to a newer round.
        """
        from .generation_draft import DEFAULT_ROLE

        def comparable(slot, entry):
            value = {k: copy.deepcopy(v) for k, v in entry.items() if v is not None}
            if slot not in {"first_frame", "last_frame", "guides"}:
                value.setdefault("purpose", DEFAULT_ROLE[slot])
            return value

        wanted = (
            [
                (slot, comparable(slot, entry))
                for slot, entry, _ in input_entries(inputs)
            ]
            if inputs is not None
            else None
        )
        values = []
        for binding in bindings:
            value = {
                k: copy.deepcopy(v)
                for k, v in binding.items()
                if k
                not in {
                    "requested_enabled",
                    "inactive_reason",
                    "effective_enabled",
                    "asset",
                }
            }
            value["enabled"] = binding.get("requested_enabled", binding["enabled"])
            if not value["enabled"]:
                continue
            if wanted is not None:
                entries = input_entries(
                    QuickChatService._inputs([value], recipe_id, deployment_profile_id)
                )
                if not entries or not all(
                    (slot, comparable(slot, entry)) in wanted
                    for slot, entry, _ in entries
                ):
                    continue
            values.append(value)
        return {"bindings": values}

    def _create_revision(
        self, conn, principal, session, card, snapshot, sources, source_revision=None
    ):
        owner_reference_gate(conn, self.tenant, principal.owner)
        self._copies_allowed(principal, snapshot["copies"], conn)
        assert_project_writable(
            conn, self.tenant, principal.owner, session["payload"]["project_id"]
        )
        assert_reference_payload(
            conn, self.tenant, principal.owner, (snapshot, sources)
        )
        # The composer is mutable; capture its origin separately from the
        # immutable generation inputs. Delayed assistant replies must never
        # capture material edits made after the original turn.
        origin = session["payload"]["bindings"]
        legacy = False
        if card["payload"].get("current_revision_id"):
            previous = self._get(
                conn,
                principal,
                card["payload"]["current_revision_id"],
                session["id"],
                "revision",
            )["payload"].get("composer_snapshot")
            if previous is None:
                legacy = True
            else:
                origin = previous["bindings"]
        elif card["payload"].get("turn_id"):
            origin = self._get(
                conn, principal, card["payload"]["turn_id"], session["id"], "turn"
            )["payload"]["bindings"]
        composer = self._composer_snapshot(
            origin,
            **(
                {
                    "inputs": snapshot["inputs"],
                    "recipe_id": snapshot["recipe_id"],
                    'deployment_profile_id': snapshot.get('deployment_profile_id'),
                }
                if legacy
                else {}
            ),
        )
        project_scope = Scope(self.tenant, principal.owner, "__projects")
        where = (
            self.repo._scope(documents, project_scope),
            documents.c.kind == "project",
            documents.c.document_id == session["payload"]["project_id"],
        )
        project_row = self.repo._locked(conn, select(documents).where(*where))
        if (
            project_row is None
            or project_row["payload"].get("integration_kind") != "quick_chat"
        ):
            raise QuickChatError(
                "projection_changed", "内部执行容器身份不匹配，未创建任务。"
            )
        # Every projected shot necessarily retains these bytes. Reject an
        # impossible document before allocating seeds/items proportional to copies.
        # This is the existing configured document byte quota, not a copies cap.
        minimum_shot_bytes = len(
            json.dumps(
                {"id": "shot-" + "0" * 32, "data": {"prompt": snapshot["prompt"]}},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        existing_bytes = len(
            json.dumps(
                project_row["payload"], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        if (
            existing_bytes + snapshot["copies"] * minimum_shot_bytes
            > self.settings.max_project_bytes
        ):
            raise QuickChatError(
                "project_size_exceeded",
                "Copies exceed the configured project byte quota. / 份数超过配置的项目字节限额。",
                422,
            )
        project = copy.deepcopy(project_row["payload"])
        scene = next(e for e in project["entities"] if e["type"] == "scene")
        revision_id = "revision-" + uuid.uuid4().hex
        version = (
            card["version"]
            if card["payload"].get("current_revision_id") is None
            else card["version"] + 1
        )
        # Choose seeds when authoring a new immutable revision, using the
        # selected runtime's domain. Reading/replaying old revisions never
        # rewrites their seeds, requests or identities.
        seed_bits = (
            32
            if (
                snapshot.get("deployment_profile_id") is not None
                or self.settings.execution_backend == "wangp-worker"
            )
            else 64
        )
        explicit = snapshot["controls"].get("seed")
        if explicit is not None and not 0 <= int(explicit) < (1 << seed_bits):
            raise QuickChatError(
                "wangp_invalid_seed", "当前生成服务的种子须在0至4294967295之间。", 422
            )
        previous_seeds = {
            int(item["seed"])
            for value in conn.execute(
                select(objects.c.payload).where(
                    objects.c.tenant == self.tenant,
                    objects.c.owner == principal.owner,
                    objects.c.kind == "revision",
                    objects.c.parent_id == card["id"],
                )
            ).scalars()
            for item in value["items"]
        }
        seeds, chosen = [], set(previous_seeds)
        for index in range(snapshot["copies"]):
            seed = (
                int(explicit)
                if index == 0 and explicit is not None
                else secrets.randbits(seed_bits)
            )
            if index != 0 or explicit is None:
                while seed in chosen:
                    seed = (seed + 1) % (1 << seed_bits)
            chosen.add(seed)
            seeds.append(str(seed))
        digest = request_hash({"snapshot": snapshot, "seeds": seeds})
        items = []
        for index, seed in enumerate(seeds):
            item_id = (
                "item-"
                + uuid.uuid5(uuid.NAMESPACE_URL, revision_id + ":" + str(index)).hex
            )
            shot_id = "shot-" + uuid.uuid5(uuid.NAMESPACE_URL, item_id).hex
            shot = new_entity(
                project,
                {
                    "id": shot_id,
                    "type": "shot",
                    "parentId": scene["id"],
                    "title": snapshot["title"] + f" · {index + 1}",
                    "data": {
                        "seconds": snapshot["controls"].get("duration", 5),
                        "prompt": snapshot["prompt"],
                        "quickChatProjection": {
                            "session_id": session["id"],
                            "revision_id": revision_id,
                            "item_id": item_id,
                            "requested_input_hash": digest,
                        },
                    },
                },
            )
            configure(
                project,
                {
                    "shot_id": shot_id,
                    "recipe_id": snapshot["recipe_id"],
                    "prompt": snapshot["prompt"],
                    **(
                        {"deployment_profile_id": snapshot["deployment_profile_id"]}
                        if snapshot.get("deployment_profile_id")
                        else {}
                    ),
                    "controls": {**snapshot["controls"], "seed": seed},
                    "inputs": snapshot["inputs"],
                },
                sources,
            )
            from .source_snapshot import source_snapshot

            items.append(
                {
                    "id": item_id,
                    "index": index,
                    "seed": seed,
                    "shot_id": shot_id,
                    "projection_hash": source_snapshot(project, shot_id),
                }
            )
        project["updatedAt"] = now_iso()
        validate_project(project, self.settings.max_project_bytes)
        conn.execute(
            update(documents)
            .where(*where)
            .values(
                payload=project,
                version=project_row["version"] + 1,
                updated_at=self.repo.clock(),
            )
        )
        append_activity(
            conn,
            tenant_id=self.tenant,
            principal=principal,
            project_id=project["id"],
            version=project_row["version"] + 1,
            occurred_at=self.repo.clock(),
            before=project_row["payload"],
            after=project,
            actions=[
                {"op": "entity.create", "entity": {"id": v["shot_id"]}} for v in items
            ],
            event_type="project.edited",
        )
        revision = self._new(
            conn,
            principal,
            session["id"],
            "revision",
            {
                **snapshot,
                "card_id": card["id"],
                "version": version,
                "turn_id": card["payload"].get("turn_id"),
                "source_revision_id": source_revision,
                "composer_snapshot": composer,
                "input_hash": digest,
                "requested_input_hash": digest,
                "items": items,
            },
            ident=revision_id,
            parent=card["id"],
            business=card["id"] + ":" + str(version),
        )
        self._put(
            conn,
            card,
            {
                **card["payload"],
                "current_revision_id": revision_id,
                "title": snapshot["title"],
            },
            bump=card["payload"].get("current_revision_id") is not None,
        )
        self._event(
            conn,
            principal,
            session,
            "card.created" if version == 1 else "card.revised",
            revision_id,
        )
        return revision

    def save_card(self, principal, session_id, body, key, *, card_id=None):
        from .quick_chat_schema import validate_card_request

        validate_card_request(body, revising=card_id is not None)
        session = self._access(principal, session_id, "projects:read", "projects:write")
        namespace = "card:" + (card_id or session_id)
        # Authorize before replay. A replay need not re-resolve changed references.
        with self.repo.engine.connect() as conn:
            prior = self._replay(conn, principal, namespace, key, body)
        if prior:
            revision = self.get_revision(principal, session_id, prior)
            return {
                "card": self.get_card(principal, session_id, revision["card_id"]),
                "revision": revision,
            }
        from .quality_authoring import (
            inherit_revision_mode,
            normalize,
            public_mode_edit,
        )

        selected = body
        if card_id and body.get("source_revision_id") and public_mode_edit(body):
            with self.repo.engine.connect() as conn:
                source = self._get(
                    conn, principal, body["source_revision_id"], session_id, "revision"
                )
            if (
                source["parent_id"] != card_id
                or source["payload"].get("card_id") != card_id
            ):
                raise NotFound("quick_chat_resource_not_found")
            selected = inherit_revision_mode(body, source["payload"])
        selected, _ = normalize(self.settings, self.repo, principal, selected)
        snapshot, sources = self._card_snapshot(principal, session, selected)
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "projects:write",
                conn=conn,
                lock=True,
            )
            prior = self._replay(conn, principal, namespace, key, body)
            if prior:
                revision = self._get(conn, principal, prior, session_id, "revision")
            else:
                if body.get("turn_id"):
                    self._get(conn, principal, body["turn_id"], session_id, "turn")
                if body.get("source_revision_id"):
                    self._get(
                        conn,
                        principal,
                        body["source_revision_id"],
                        session_id,
                        "revision",
                    )
                if card_id:
                    card = self._get(
                        conn, principal, card_id, session_id, "card", lock=True
                    )
                    if body["expected_card_version"] != card["version"]:
                        raise QuickChatError(
                            "version_conflict",
                            "内容已被修改；请保留本地草稿并读取当前版本。",
                            field_errors=[
                                {
                                    "path": ["body", "expected_card_version"],
                                    "reason": "stale_version",
                                }
                            ],
                        )
                else:
                    card = self._new(
                        conn,
                        principal,
                        session_id,
                        "card",
                        {
                            "title": snapshot["title"],
                            "turn_id": body.get("turn_id"),
                            "current_revision_id": None,
                        },
                    )
                revision = self._create_revision(
                    conn,
                    principal,
                    session,
                    card,
                    snapshot,
                    sources,
                    body.get("source_revision_id"),
                )
                self._remember(conn, principal, namespace, key, body, revision["id"])
                self._queue_title(conn, principal, session, revision)
        return {
            "card": self.get_card(
                principal, session_id, revision["payload"]["card_id"]
            ),
            "revision": self.get_revision(principal, session_id, revision["id"]),
        }

    def get_revision(self, principal, session_id, revision_id):
        self._access(principal, session_id, "projects:read")
        with self.repo.engine.connect() as conn:
            row = self._get(conn, principal, revision_id, session_id, "revision")
        return {
            **row["payload"],
            "id": row["id"],
            "created_at": row["created_at"],
            "web_url": "/quick-chat?session=" + session_id + "&revision=" + row["id"],
        }

    def get_card(self, principal, session_id, card_id):
        self._access(principal, session_id, "projects:read")
        with self.repo.engine.connect() as conn:
            card = self._get(conn, principal, card_id, session_id, "card")
            rows = conn.execute(
                select(objects.c.id, objects.c.payload, objects.c.created_at)
                .where(
                    objects.c.tenant == self.tenant,
                    objects.c.owner == principal.owner,
                    objects.c.session_id == session_id,
                    objects.c.kind == "revision",
                    objects.c.parent_id == card_id,
                )
                .order_by(objects.c.created_at, objects.c.id)
                .limit(100)
            ).mappings()
            summaries = [
                {
                    "id": r["id"],
                    "version": r["payload"]["version"],
                    "created_at": r["created_at"],
                    "input_hash": r["payload"]["input_hash"],
                }
                for r in rows
            ]
        return {
            **card["payload"],
            "id": card_id,
            "version": card["version"],
            "created_at": card["created_at"],
            "revisions": summaries,
            "web_url": "/quick-chat?session=" + session_id + "&card=" + card_id,
        }

    def list_cards(self, principal, session_id, *, limit=10, before_cursor=None):
        """Visible card history, independent of how many runtime events occurred."""
        session = self._access(principal, session_id, "projects:read")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise QuickChatError(
                "invalid_pagination",
                "Invalid card page size. / 任务卡分页大小无效。",
                422,
            )
        boundary = None
        if before_cursor is not None:
            boundary = cursor_decode(
                before_cursor, principal, session_id, "cards_older"
            )
            if (
                not isinstance(boundary, dict)
                or set(boundary) != {"tenant", "created_at", "id"}
                or boundary["tenant"] != self.tenant
                or type(boundary["created_at"]) not in (int, float)
                or not math.isfinite(boundary["created_at"])
                or not isinstance(boundary["id"], str)
                or len(boundary["id"]) > 80
                or not ID.fullmatch(boundary["id"])
            ):
                raise QuickChatError(
                    "invalid_cursor",
                    "Invalid card history position. / 任务卡分页位置无效。",
                    422,
                )
        query = select(objects.c.id, objects.c.created_at).where(
            objects.c.tenant == self.tenant,
            objects.c.owner == principal.owner,
            objects.c.session_id == session_id,
            objects.c.kind == "card",
            objects.c.payload["content_deleted"].as_boolean().is_not(True),
        )
        if boundary is not None:
            query = query.where(
                or_(
                    objects.c.created_at < boundary["created_at"],
                    and_(
                        objects.c.created_at == boundary["created_at"],
                        objects.c.id < boundary["id"],
                    ),
                )
            )
        with self.repo.engine.connect() as conn:
            rows = list(
                conn.execute(
                    query.order_by(
                        objects.c.created_at.desc(), objects.c.id.desc()
                    ).limit(limit + 1)
                ).mappings()
            )
        selected, has_more = rows[:limit], len(rows) > limit
        values = [
            self.get_card(principal, session_id, row["id"])
            for row in reversed(selected)
        ]
        if principal.allows(session["payload"]["project_id"], "jobs:read"):
            revision, submission = (
                objects.alias("history_revision"),
                objects.alias("history_submission"),
            )
            query = (
                select(revision.c.parent_id, submission.c.id)
                .join(submission, submission.c.parent_id == revision.c.id)
                .where(
                    revision.c.tenant == self.tenant,
                    revision.c.owner == principal.owner,
                    revision.c.session_id == session_id,
                    revision.c.kind == "revision",
                    revision.c.parent_id.in_([row["id"] for row in selected]),
                    submission.c.tenant == self.tenant,
                    submission.c.owner == principal.owner,
                    submission.c.session_id == session_id,
                    submission.c.kind == "submission",
                )
            )
            with self.repo.engine.connect() as conn:
                links = list(
                    conn.execute(
                        query.order_by(submission.c.created_at, submission.c.id)
                    )
                )
            for card in values:
                card["submission_ids"] = [
                    ident for parent, ident in links if parent == card["id"]
                ]
        else:
            for card in values:
                card.update(submission_ids=None, submissions_require_scope="jobs:read")
        next_cursor = (
            cursor_encode(
                principal,
                session_id,
                {
                    "tenant": self.tenant,
                    "created_at": selected[-1]["created_at"],
                    "id": selected[-1]["id"],
                },
                "cards_older",
            )
            if has_more
            else None
        )
        return {
            "session_id": session_id,
            "cards": values,
            "limit": limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "start_reached": not has_more,
        }

    def _projection(self, principal, session, revision, item):
        scope = Scope(self.tenant, principal.owner, "__projects")
        record = self.repo.get_document(
            scope, "project", session["payload"]["project_id"]
        )
        from .source_snapshot import source_snapshot

        if (
            record["payload"].get("integration_kind") != "quick_chat"
            or source_snapshot(record["payload"], item["shot_id"])
            != item["projection_hash"]
        ):
            raise QuickChatError(
                "projection_changed", "执行投影与保存版本不一致；没有提交或覆盖任务。"
            )
        return record["payload"]

    def preflight(self, principal, session_id, revision_id, body, key):
        fields(
            body,
            {
                "capabilities_version",
                "revision_hash",
                "item_ids",
                "retry_of_execution_id",
            },
            {"capabilities_version", "revision_hash"},
        )
        if not version_matches(body["capabilities_version"], VERSION):
            raise QuickChatError(
                "preflight_stale", "生成能力已更新，请重新加载后预检。"
            )
        session = self._access(principal, session_id, "projects:read", "jobs:write")
        revision = self.get_revision(principal, session_id, revision_id)
        if body["revision_hash"] != revision["input_hash"]:
            raise QuickChatError("revision_hash_conflict", "任务卡内容校验不匹配。")
        selected = body.get("item_ids", [i["id"] for i in revision["items"]])
        if (
            not isinstance(selected, list)
            or not selected
            or len(set(selected)) != len(selected)
            or set(selected) - {i["id"] for i in revision["items"]}
        ):
            raise QuickChatError(
                "invalid_item_selection", "请选择这个版本中的不同生成项。", 422
            )
        namespace = "preflight:" + revision_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "jobs:write",
                conn=conn,
                lock=True,
            )
            prior = self._replay(conn, principal, namespace, key, body)
            if prior:
                record = self._get(conn, principal, prior, session_id, "preflight")
            else:
                from .universal_trash import assert_take_active

                for item_id in selected:
                    assert_take_active(
                        conn, self.tenant, principal.owner, session_id, item_id
                    )
                existing = (
                    conn.execute(
                        select(objects).where(
                            objects.c.tenant == self.tenant,
                            objects.c.owner == principal.owner,
                            objects.c.kind == "submission",
                            objects.c.business_key == revision_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    for item_id in selected:
                        live = self._get(conn, principal, item_id, session_id, "item")
                        self._safe_unexecuted_or_failed(
                            conn, principal, live, allow_failed=True
                        )
                record = self._new(
                    conn,
                    principal,
                    session_id,
                    "preflight",
                    {
                        "revision_id": revision_id,
                        "revision_hash": revision["input_hash"],
                        "status": "checking",
                        "items": [
                            {
                                "item_id": i["id"],
                                "index": i["index"],
                                "seed": i["seed"],
                                "plan": None,
                                "error_code": None,
                            }
                            for i in revision["items"]
                            if i["id"] in selected
                        ],
                        **(
                            {
                                "composer_snapshot": self._composer_snapshot(
                                    session["payload"]["bindings"],
                                    inputs=revision["inputs"],
                                    recipe_id=revision["recipe_id"],
                                    deployment_profile_id=revision.get(
                                        'deployment_profile_id'
                                    ),
                                )
                            }
                            if revision.get("composer_snapshot") is None
                            else {}
                        ),
                        "retry_of_execution_id": body.get("retry_of_execution_id"),
                        "expires_at": None,
                    },
                    parent=revision_id,
                )
                self._remember(conn, principal, namespace, key, body, record["id"])
        if self.hooks is None:
            raise QuickChatError(
                "execution_adapter_unavailable", "执行预检尚未配置。", 503
            )
        for index, entry in enumerate(record["payload"]["items"]):
            if entry["plan"] or entry["error_code"]:
                continue
            item = next(i for i in revision["items"] if i["id"] == entry["item_id"])
            try:
                project = self._projection(principal, session, revision, item)
                plan = self.hooks.preflight(principal, project, item["shot_id"])
                digest = request_hash(
                    {
                        "effective_request": plan.get("effective_request"),
                        "output_spec": plan.get("output_spec"),
                        "request_hash": plan.get("request_hash"),
                        "plan_id": plan["plan_id"],
                    }
                )
                value = {
                    **entry,
                    "plan": canonical(plan),
                    "resolved_execution_hash": digest,
                }
            except (
                ValueError,
                Conflict,
                BudgetExceeded,
                QuickChatError,
                ContentPolicyError,
                CreditError,
            ) as exc:
                from .generation_diagnostics import preflight_diagnostic

                value = {**entry, **preflight_diagnostic(exc)}
                if isinstance(exc, QuickChatError):
                    value.update(error_code=exc.code, error_message=exc.message)
                elif isinstance(exc, CreditError):
                    value.update(error_code=exc.code, error_message=exc.detail)
            with self.repo.transaction() as conn:
                current = self._get(
                    conn, principal, record["id"], session_id, "preflight", lock=True
                )
                items = copy.deepcopy(current["payload"]["items"])
                if items[index]["plan"] is None and not items[index]["error_code"]:
                    items[index] = value
                    self._put(conn, current, {**current["payload"], "items": items})
        with self.repo.transaction() as conn:
            session = self._access(
                principal, session_id, "jobs:write", conn=conn, lock=True
            )
            current = self._get(
                conn, principal, record["id"], session_id, "preflight", lock=True
            )
            p = current["payload"]
            if p["status"] == "checking":
                plans = [i["plan"] for i in p["items"] if i["plan"]]
                ready = len(plans) == len(p["items"]) and all(
                    v["status"] == "ready" for v in plans
                )
                runtime_values = []
                from .repository import plans as saved_plans

                for public_plan in plans:
                    saved = conn.execute(
                        select(saved_plans.c.execution_plan).where(
                            saved_plans.c.id == public_plan["plan_id"],
                            saved_plans.c.tenant_id == self.tenant,
                            saved_plans.c.owner_id == principal.owner,
                        )
                    ).scalar_one_or_none()
                    runtime = (
                        saved.get("expected_runtime_s")
                        if saved and saved.get("quote_known") is True
                        else None
                    )
                    runtime_values.append(
                        runtime
                        if type(runtime) in (int, float) and 0 <= runtime < float("inf")
                        else None
                    )
                machine_seconds = (
                    sum(runtime_values)
                    if len(runtime_values) == len(p["items"])
                    and all(v is not None for v in runtime_values)
                    else None
                )
                p = {
                    **p,
                    "status": "ready" if ready else "blocked",
                    "expires_at": min(
                        (v["expires_at"] for v in plans), default=self.repo.clock()
                    ),
                    "estimate_available": len(plans) == len(p["items"])
                    and all(
                        v.get("estimate", {}).get("cost_microusd") is not None
                        for v in plans
                    ),
                    "estimate": {
                        "currency": "USD",
                        "cost_microusd": sum(
                            (v.get("estimate", {}).get("cost_microusd") or 0)
                            for v in plans
                        ),
                        "kind": "budget_reservation",
                        "final_bill": False,
                        "machine_seconds": machine_seconds,
                        "runtime_kind": "planned_machine_allowance",
                        "eta_seconds": None,
                    },
                }
                self._put(conn, current, p)
                self._event(
                    conn, principal, session, "preflight.completed", current["id"]
                )
        return self.get_preflight(principal, session_id, record["id"])

    def get_preflight(self, principal, session_id, ident):
        # Retain the original scope requirement without treating a historical
        # GET as authoring on an adopted, read-only legacy session.
        session = self._access(principal, session_id, "projects:read")
        if not principal.allows(session["payload"]["project_id"], "jobs:write"):
            raise NotFound("quick_chat_session_not_found")
        with self.repo.engine.connect() as conn:
            row = self._get(conn, principal, ident, session_id, "preflight")
        stale = (
            row["payload"].get("expires_at") is not None
            and row["payload"]["expires_at"] <= self.repo.clock()
        )
        return {
            **row["payload"],
            "id": ident,
            "created_at": row["created_at"],
            "stale": stale,
            "stale_reason": "expired" if stale else None,
        }

    def _check_preflight(
        self, conn, principal, session_id, ident, revision_id, item_ids
    ):
        row = self._get(conn, principal, ident, session_id, "preflight")
        p = row["payload"]
        if (
            p["revision_id"] != revision_id
            or p["status"] != "ready"
            or p["expires_at"] <= self.repo.clock()
            or {i["item_id"] for i in p["items"]} != set(item_ids)
        ):
            raise QuickChatError(
                "preflight_stale", "请对指定生成项重新预检并确认费用。"
            )
        return p

    def _execution(self, conn, principal, session_id, item, plan_id, *, retry_of=None):
        business = "retry:" + retry_of if retry_of else "initial:" + item["id"]
        execution = self._new(
            conn,
            principal,
            session_id,
            "execution",
            {
                "item_id": item["id"],
                "plan_id": plan_id,
                "job_id": None,
                "status": "pending_admission",
                "error_code": None,
                "retry_of_execution_id": retry_of,
                "actor_id": principal.actor_id,
            },
            parent=item["id"],
            business=business,
        )
        self._put(
            conn,
            item,
            {
                **item["payload"],
                "current_execution_id": execution["id"],
                "cancel_requested": False,
            },
        )
        return execution

    def submit(self, principal, session_id, revision_id, body, key):
        fields(
            body,
            {"preflight_id", "revision_hash", "confirmed"},
            {"preflight_id", "revision_hash", "confirmed"},
        )
        if body["confirmed"] is not True:
            raise QuickChatError(
                "confirmation_required", "请明确确认这份预检和整批费用。", 422
            )
        revision = self.get_revision(principal, session_id, revision_id)
        if body["revision_hash"] != revision["input_hash"]:
            raise QuickChatError("revision_hash_conflict", "任务卡校验不匹配。")
        namespace = "submit:" + revision_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "jobs:write",
                conn=conn,
                lock=True,
            )
            prior = self._replay(conn, principal, namespace, key, body)
            existing = (
                conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.kind == "submission",
                        objects.c.business_key == revision_id,
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                submission = dict(existing)
                if not prior:
                    self._remember(
                        conn, principal, namespace, key, body, submission["id"]
                    )
            else:
                from .universal_trash import assert_take_active

                for item in revision["items"]:
                    assert_take_active(
                        conn, self.tenant, principal.owner, session_id, item["id"]
                    )
                p = self._check_preflight(
                    conn,
                    principal,
                    session_id,
                    body["preflight_id"],
                    revision_id,
                    [i["id"] for i in revision["items"]],
                )
                submission = self._new(
                    conn,
                    principal,
                    session_id,
                    "submission",
                    {
                        "revision_id": revision_id,
                        "preflight_id": body["preflight_id"],
                        "actor_id": principal.actor_id,
                        "cancel_requested": False,
                        **(
                            {"composer_snapshot": copy.deepcopy(p["composer_snapshot"])}
                            if p.get("composer_snapshot") is not None
                            else {}
                        ),
                    },
                    parent=revision_id,
                    business=revision_id,
                )
                for entry in revision["items"]:
                    item = self._new(
                        conn,
                        principal,
                        session_id,
                        "item",
                        {
                            **entry,
                            "revision_id": revision_id,
                            "submission_id": submission["id"],
                            "current_execution_id": None,
                        },
                        ident=entry["id"],
                        parent=submission["id"],
                    )
                    plan = next(
                        v["plan"] for v in p["items"] if v["item_id"] == item["id"]
                    )
                    self._execution(conn, principal, session_id, item, plan["plan_id"])
                self._remember(conn, principal, namespace, key, body, submission["id"])
                self._event(
                    conn, principal, session, "submission.confirmed", submission["id"]
                )
        if not existing or prior:
            self._admit(principal, session_id, submission["id"])
        else:
            self._reset_accepted_composer(principal, session_id, submission["id"])
        return self.get_submission(principal, session_id, submission["id"])

    def _admit(
        self,
        principal,
        session_id,
        submission_id,
        selected=None,
        *,
        allow_blocked=False,
    ):
        session = self._access(principal, session_id, "jobs:write")
        if self.hooks is None:
            raise QuickChatError(
                "execution_adapter_unavailable", "执行入口尚未配置；确认记录保留。", 503
            )
        with self.repo.engine.connect() as conn:
            submission = self._get(
                conn, principal, submission_id, session_id, "submission"
            )
            items = [
                dict(r)
                for r in conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "item",
                        objects.c.parent_id == submission_id,
                    )
                ).mappings()
            ]
            pending_plans = []
            for item in items:
                if selected is not None and item["id"] not in selected:
                    continue
                execution = self._get(
                    conn,
                    principal,
                    item["payload"]["current_execution_id"],
                    session_id,
                    "execution",
                )
                if execution["payload"]["status"] in {
                    "pending_admission",
                    "admission_blocked",
                }:
                    job_id = execution["payload"].get("job_id")
                    if job_id:
                        status = conn.execute(
                            select(jobs.c.status).where(
                                jobs.c.id == job_id,
                                jobs.c.tenant_id == self.tenant,
                                jobs.c.owner_id == principal.owner,
                            )
                        ).scalar_one()
                        if status not in {"planned", "blocked"}:
                            continue  # reconcile the accepted original, no new grant
                    pending_plans.append(execution["payload"]["plan_id"])
        from .spending_limits import check_submission_confirmation

        check = check_submission_confirmation(
            self.repo,
            self.tenant,
            principal,
            [] if submission["payload"]["cancel_requested"] else pending_plans,
        )
        required = bool(check)
        if required:
            with self.repo.transaction() as conn:
                current = self._get(
                    conn, principal, submission_id, session_id, "submission", lock=True
                )
                self._put(
                    conn,
                    current,
                    {**current["payload"], "spending_confirmation_required": True},
                )
        required = required or submission["payload"].get(
            "spending_confirmation_required", False
        )
        for item in items:
            if selected is not None and item["id"] not in selected:
                continue
            # Cancellation is a durable obligation, checked on both sides of
            # job creation. The queue CAS handles a cancel/enqueue race once
            # the job is linked; never mark a linked job falsely cancelled.
            with self.repo.transaction() as conn:
                latest_submission = self._get(
                    conn, principal, submission_id, session_id, "submission", lock=True
                )
                item = self._get(
                    conn, principal, item["id"], session_id, "item", lock=True
                )
                execution = self._get(
                    conn,
                    principal,
                    item["payload"]["current_execution_id"],
                    session_id,
                    "execution",
                    lock=True,
                )
                cancel_requested = latest_submission["payload"][
                    "cancel_requested"
                ] or item["payload"].get("cancel_requested", False)
                if cancel_requested and not execution["payload"]["job_id"]:
                    self._put(
                        conn, execution, {**execution["payload"], "status": "cancelled"}
                    )
                    continue
            if execution["payload"]["status"] not in (
                {"pending_admission", "admission_blocked"}
                if allow_blocked
                else {"pending_admission"}
            ):
                continue
            try:
                p = execution["payload"]
                job = (
                    self.hooks.create_planned(principal, p["plan_id"], execution["id"])
                    if not p["job_id"]
                    else self.repo.get_job_for_owner(
                        self.tenant, principal.owner, p["job_id"]
                    )
                )
                with self.repo.transaction() as conn:
                    latest = self._get(
                        conn,
                        principal,
                        submission_id,
                        session_id,
                        "submission",
                        lock=True,
                    )
                    current_item = self._get(
                        conn, principal, item["id"], session_id, "item", lock=True
                    )
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    if current["payload"]["job_id"] not in (None, job["id"]):
                        raise QuickChatError(
                            "execution_identity_conflict", "已保存的任务身份不一致。"
                        )
                    self._put(
                        conn, current, {**current["payload"], "job_id": job["id"]}
                    )
                    cancel_requested = latest["payload"][
                        "cancel_requested"
                    ] or current_item["payload"].get("cancel_requested", False)
                if cancel_requested:
                    job = self.hooks.cancel(principal, job["id"])
                elif job["status"] in {"planned", "blocked"}:
                    try:
                        job = self.hooks.enqueue(
                            principal,
                            job,
                            confirmation_required=required,
                            spending_settings_version=check.version,
                        )
                    except Conflict:
                        job = self.repo.get_job_for_owner(
                            self.tenant, principal.owner, job["id"]
                        )
                        if job["status"] in {"planned", "blocked"}:
                            raise
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    self._put(
                        conn,
                        current,
                        {
                            **current["payload"],
                            "status": "admitted",
                            "error_code": None,
                        },
                    )
            except (
                Conflict,
                BudgetExceeded,
                ValueError,
                QuickChatError,
                ContentPolicyError,
                CreditError,
            ) as error:
                from .spending_limits import SpendingConfirmationRequired

                if isinstance(error, SpendingConfirmationRequired):
                    raise
                spending_codes = {
                    "user_daily_limit_reached",
                    "connection_daily_limit_reached",
                    "spending_usage_unconfirmed",
                    "connection_paused",
                    "connection_unavailable",
                    "connection_permission_denied",
                }
                code = (
                    error.code
                    if isinstance(
                        error, (QuickChatError, ContentPolicyError, CreditError)
                    )
                    else str(error)
                    if str(error) in spending_codes
                    else "budget_exceeded"
                    if isinstance(error, BudgetExceeded)
                    else "admission_blocked"
                )
                from .agent_management import record_refusal

                record_refusal(
                    self.repo.engine, self.tenant, principal, code, self.repo.clock()
                )
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    self._put(
                        conn,
                        current,
                        {
                            **current["payload"],
                            "status": "admission_blocked",
                            "error_code": code,
                        },
                    )
        self._reset_accepted_composer(principal, session_id, submission_id)

    def _reset_accepted_composer(self, principal, session_id, submission_id):
        """One durable reset after queue acceptance, never from a read/preflight.

        An interrupted enqueue is reconciled by replaying the original command;
        a receipt/batch alone does not prove that a video job was accepted.
        Session-first locking matches normal authoring/submission lock order.
        """
        with self.repo.transaction() as conn:
            session = self._access(
                principal, session_id, "jobs:write", conn=conn, lock=True
            )
            submission = self._get(
                conn, principal, submission_id, session_id, "submission", lock=True
            )
            if submission["payload"].get("composer_reset"):
                return
            revision = self._get(
                conn,
                principal,
                submission["payload"]["revision_id"],
                session_id,
                "revision",
            )
            snapshot = submission["payload"].get(
                "composer_snapshot", revision["payload"].get("composer_snapshot")
            )
            if snapshot is None:
                # A submission prepared before origin snapshots cannot safely
                # establish which later edits it owns. Never retrofit a read.
                return
            item_ids = select(objects.c.id).where(
                objects.c.tenant == self.tenant,
                objects.c.owner == principal.owner,
                objects.c.session_id == session_id,
                objects.c.kind == "item",
                objects.c.parent_id == submission_id,
            )
            executions = (
                conn.execute(
                    select(objects.c.payload).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "execution",
                        objects.c.parent_id.in_(item_ids),
                    )
                )
                .scalars()
                .all()
            )
            job_ids = [p["job_id"] for p in executions if p.get("job_id")]
            if not job_ids:
                return
            accepted = conn.execute(
                select(jobs.c.id).where(
                    jobs.c.tenant_id == self.tenant,
                    jobs.c.owner_id == principal.owner,
                    jobs.c.project_id == session["payload"]["project_id"],
                    jobs.c.id.in_(job_ids),
                    jobs.c.status.in_(
                        (
                            "queued",
                            "waiting_capacity",
                            "claimed",
                            "submitting",
                            "submission_unknown",
                            "running",
                            "collecting",
                            "succeeded",
                            "failed",
                            "cancel_requested",
                            "recovery_hold",
                        )
                    ),
                )
            ).first()
            if accepted is None:
                return
            captured = {b["binding_id"]: b for b in snapshot["bindings"]}
            values, cleared, preserved = [], [], []
            for binding in session["payload"]["bindings"]:
                ident = binding["binding_id"]
                if binding.get("enabled") and binding == captured.get(ident):
                    values.append(
                        {**binding, "enabled": False, "version": binding["version"] + 1}
                    )
                    cleared.append(ident)
                else:
                    values.append(binding)
                    if binding.get("enabled"):
                        preserved.append(ident)
            receipt = {
                "status": "accepted",
                "submission_id": submission_id,
                "revision_id": revision["id"],
                "turn_id": revision["payload"].get("turn_id"),
                "session_version": session["version"] + 1,
                "cleared_binding_ids": cleared,
                "preserved_binding_ids": preserved,
            }
            self._put(
                conn,
                session,
                {**session["payload"], "bindings": values, "composer_reset": receipt},
                bump=True,
            )
            self._put(
                conn, submission, {**submission["payload"], "composer_reset": receipt}
            )
            self._event(
                conn, principal, session, "submission.composer_reset", submission_id
            )

    def get_submission(self, principal, session_id, submission_id):
        self._access(principal, session_id, "projects:read", "jobs:read")
        with self.repo.engine.connect() as conn:
            row = self._get(conn, principal, submission_id, session_id, "submission")
            children = [
                dict(r)
                for r in conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "item",
                        objects.c.parent_id == submission_id,
                    )
                ).mappings()
            ]
            executions = [
                dict(r)
                for r in conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "execution",
                        objects.c.parent_id.in_([c["id"] for c in children]),
                    )
                ).mappings()
            ]
        values = []
        for child in sorted(children, key=lambda r: r["payload"]["index"]):
            history = [
                {**r["payload"], "id": r["id"], "created_at": r["created_at"]}
                for r in executions
                if r["parent_id"] == child["id"]
            ]
            current = next(
                e
                for e in history
                if e["id"] == child["payload"]["current_execution_id"]
            )
            job = (
                self.hooks.public_job(principal, current["job_id"])
                if current["job_id"] and self.hooks
                else None
            )
            status = job["status"] if job else current["status"]
            if current["status"] in {"unknown", "recovery_hold"}:
                status = current["status"]
            if job and status in {"planned", "blocked"} and current["error_code"]:
                status = "admission_blocked"
            retryable, resumable = False, False
            if principal.allows(
                self._access(principal, session_id)["payload"]["project_id"],
                "jobs:write",
            ):
                try:
                    with self.repo.engine.connect() as conn:
                        safe_execution, safe_job = self._safe_unexecuted_or_failed(
                            conn, principal, child, allow_failed=True, lock=False
                        )
                    retryable = bool(
                        safe_job and safe_job["status"] in {"failed", "cancelled"}
                    )
                    resumable = (
                        not row["payload"]["cancel_requested"]
                        and not child["payload"].get("cancel_requested", False)
                        and safe_execution["payload"]["status"]
                        in {"pending_admission", "admission_blocked"}
                        and (
                            safe_job is None
                            or safe_job["status"] in {"planned", "blocked"}
                        )
                    )
                except (QuickChatError, Conflict, NotFound):
                    pass
            values.append(
                {
                    **child["payload"],
                    "id": child["id"],
                    "status": status,
                    "error_code": (job or {}).get("error_code")
                    or current["error_code"],
                    "job_id": current["job_id"],
                    "job": job,
                    "executions": history,
                    "retryable": retryable,
                    "resume_admission_available": resumable,
                }
            )
        states = [v["status"] for v in values]
        terminal = {"succeeded", "failed", "cancelled"}
        status = (
            "completed"
            if states and all(s == "succeeded" for s in states)
            else (
                "finished_with_issues"
                if states and all(s in terminal for s in states)
                else "active"
            )
        )
        return {
            **row["payload"],
            "id": submission_id,
            "items": values,
            "status": status,
            "counts": {s: states.count(s) for s in sorted(set(states))},
            "created_at": row["created_at"],
            "updated_at": max(
                [
                    row["updated_at"],
                    *(
                        (i["job"] or {}).get("updated_at", row["updated_at"])
                        for i in values
                    ),
                ]
            ),
            "web_url": "/quick-chat?session="
            + session_id
            + "&submission="
            + submission_id,
        }

    def _safe_unexecuted_or_failed(
        self, conn, principal, item, *, allow_failed=False, lock=True
    ):
        from .universal_trash import assert_take_active

        assert_take_active(
            conn, self.tenant, principal.owner, item["session_id"], item["id"]
        )
        execution = self._get(
            conn,
            principal,
            item["payload"]["current_execution_id"],
            item["session_id"],
            "execution",
            lock=lock,
        )
        p = execution["payload"]
        if p["status"] in {"unknown", "recovery_hold"}:
            raise QuickChatError(
                "upstream_stop_unconfirmed",
                "恢复后的执行须先对账；不会根据缺失job ID再次生成。",
            )
        if not p["job_id"]:
            if p["status"] not in {
                "pending_admission",
                "admission_blocked",
                "cancelled",
            }:
                raise QuickChatError(
                    "upstream_stop_unconfirmed", "此项执行状态需核验；不会再次生成。"
                )
            return execution, None
        job = self.repo._job(
            conn,
            p["job_id"],
            Scope(
                self.tenant,
                principal.owner,
                self._get(
                    conn, principal, item["session_id"], item["session_id"], "session"
                )["payload"]["project_id"],
            ),
            lock=lock,
        )
        history = (
            conn.execute(select(attempts).where(attempts.c.job_id == job["id"]))
            .mappings()
            .all()
        )
        current_id = job.get("current_attempt_id")
        current = next((a for a in history if a["id"] == current_id), None)
        if current_id is not None and (
            current is None
            or job["attempt_no"] < 1
            or current["number"] != job["attempt_no"]
        ):
            # An orphan reference remains an obligation even when recovered
            # summary counters are empty or another historical row is stopped.
            raise QuickChatError(
                "upstream_stop_unconfirmed", "当前执行身份须先核验；不会再次生成。"
            )
        safe_unsubmitted = (
            job["status"] in {"planned", "blocked", "cancelled"}
            and not job["attempt_no"]
            and not history
        )
        safe_failed = (
            allow_failed
            and job["status"] in {"failed", "cancelled"}
            and len(history) >= job["attempt_no"]
            and all(a["upstream_stopped"] == 1 for a in history)
        )
        if (
            job.get("lease_worker_id")
            or job.get("lease_expires_at") is not None
            or not (safe_unsubmitted or safe_failed)
        ):
            raise QuickChatError(
                "upstream_stop_unconfirmed", "只能恢复未执行项或已证明停止的失败项。"
            )
        return execution, job

    def retry(self, principal, session_id, submission_id, item_id, body, key):
        fields(
            body,
            {"retry_of_execution_id", "fresh_preflight_id", "confirmed"},
            {"retry_of_execution_id", "fresh_preflight_id", "confirmed"},
        )
        if body["confirmed"] is not True:
            raise QuickChatError(
                "confirmation_required", "请确认此份重试预留费用。", 422
            )
        namespace = "retry:" + item_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "jobs:write",
                conn=conn,
                lock=True,
            )
            submission = self._get(
                conn, principal, submission_id, session_id, "submission", lock=True
            )
            item = self._get(conn, principal, item_id, session_id, "item", lock=True)
            if item["parent_id"] != submission_id:
                raise NotFound("item_not_found")
            prior = self._replay(conn, principal, namespace, key, body)
            source = self._get(
                conn, principal, body["retry_of_execution_id"], session_id, "execution"
            )
            if source["parent_id"] != item_id:
                raise NotFound("execution_not_found")
            existing = (
                conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.kind == "execution",
                        objects.c.business_key == "retry:" + source["id"],
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if not prior:
                    self._remember(
                        conn, principal, namespace, key, body, existing["id"]
                    )
            else:
                if item["payload"]["current_execution_id"] != source["id"]:
                    raise QuickChatError(
                        "version_conflict", "此份已经有新的执行，请加载当前状态。"
                    )
                if submission["payload"]["cancel_requested"]:
                    raise QuickChatError(
                        "submission_cancelled",
                        "整批已取消，请另建卡片版本；不会恢复已取消批次。",
                    )
                execution, job = self._safe_unexecuted_or_failed(
                    conn, principal, item, allow_failed=True
                )
                if job is None or job["status"] not in {"failed", "cancelled"}:
                    raise QuickChatError(
                        "use_resume_admission", "未准入项请恢复原项，不重新生成。"
                    )
                preflight = self._check_preflight(
                    conn,
                    principal,
                    session_id,
                    body["fresh_preflight_id"],
                    item["payload"]["revision_id"],
                    [item_id],
                )
                if preflight.get("retry_of_execution_id") != source["id"]:
                    raise QuickChatError(
                        "preflight_stale", "此预检不属于要重试的原执行。"
                    )
                new = self._execution(
                    conn,
                    principal,
                    session_id,
                    item,
                    preflight["items"][0]["plan"]["plan_id"],
                    retry_of=execution["id"],
                )
                self._remember(conn, principal, namespace, key, body, new["id"])
                self._event(
                    conn, principal, session, "item.retry_requested", submission_id
                )
        self._admit(principal, session_id, submission_id, [item_id])
        return self.get_submission(principal, session_id, submission_id)

    def resume_admission(self, principal, session_id, submission_id, body, key):
        fields(
            body,
            {"item_ids", "fresh_preflight_id", "confirmed"},
            {"item_ids", "fresh_preflight_id", "confirmed"},
        )
        if (
            body["confirmed"] is not True
            or not isinstance(body["item_ids"], list)
            or not body["item_ids"]
            or len(set(body["item_ids"])) != len(body["item_ids"])
        ):
            raise QuickChatError(
                "invalid_item_selection", "请明确确认要恢复的不同未准入项。", 422
            )
        refresh = []
        namespace = "resume:" + submission_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "jobs:write",
                conn=conn,
                lock=True,
            )
            submission = self._get(
                conn, principal, submission_id, session_id, "submission", lock=True
            )
            if submission["payload"]["cancel_requested"]:
                raise QuickChatError(
                    "cancel_requested", "已取消批次不能恢复准入；请创建新版本。"
                )
            prior = self._replay(conn, principal, namespace, key, body)
            if not prior:
                preflight = self._check_preflight(
                    conn,
                    principal,
                    session_id,
                    body["fresh_preflight_id"],
                    submission["payload"]["revision_id"],
                    body["item_ids"],
                )
                for item_id in body["item_ids"]:
                    item = self._get(
                        conn, principal, item_id, session_id, "item", lock=True
                    )
                    if item["parent_id"] != submission_id:
                        raise NotFound("item_not_found")
                    execution, job = self._safe_unexecuted_or_failed(
                        conn, principal, item
                    )
                    if item["payload"].get("cancel_requested", False):
                        raise QuickChatError(
                            "cancel_requested", "此项已取消，未恢复准入。"
                        )
                    if job and job["status"] not in {"planned", "blocked"}:
                        raise QuickChatError(
                            "use_failed_item_retry", "已取消项请使用单份重试。"
                        )
                    plan_id = next(
                        i["plan"]["plan_id"]
                        for i in preflight["items"]
                        if i["item_id"] == item_id
                    )
                    payload = {
                        **execution["payload"],
                        "resume_plan_id": plan_id,
                        "status": "pending_admission",
                        "error_code": None,
                    }
                    if not job:
                        payload["plan_id"] = plan_id
                        payload["resume_plan_id"] = None
                    elif execution["payload"].get("resume_plan_id"):
                        raise QuickChatError(
                            "admission_resume_active",
                            "原恢复命令尚未对账，请先查看或恢复该命令；不会叠加另一份计划。",
                        )
                    self._put(conn, execution, payload)
                self._remember(conn, principal, namespace, key, body, submission_id)
                self._event(
                    conn,
                    principal,
                    session,
                    "submission.admission_resumed",
                    submission_id,
                )
            for item_id in body["item_ids"]:
                item = self._get(conn, principal, item_id, session_id, "item")
                execution = self._get(
                    conn,
                    principal,
                    item["payload"]["current_execution_id"],
                    session_id,
                    "execution",
                )
                if (
                    execution["payload"].get("resume_plan_id")
                    and execution["payload"]["job_id"]
                ):
                    refresh.append(execution)
        blocked = set()
        for execution in refresh:
            if not self.hooks or self.hooks.refresh_planned is None:
                raise QuickChatError(
                    "preflight_stale", "此原计划已过期，安全刷新尚未配置；任务保留。"
                )
            p = execution["payload"]
            try:
                self.hooks.refresh_planned(principal, p["job_id"], p["resume_plan_id"])
            except (
                Conflict,
                BudgetExceeded,
                ValueError,
                QuickChatError,
                ContentPolicyError,
                CreditError,
            ) as error:
                # Only finite service codes are public; arbitrary exception
                # text may contain source inputs, paths or provider details.
                code = (
                    error.code
                    if isinstance(
                        error, (QuickChatError, ContentPolicyError, CreditError)
                    )
                    else (error.args[0] if len(error.args) == 1 else None)
                )
                if not isinstance(error, (ContentPolicyError, CreditError)) and (
                    not isinstance(code, str) or code not in ADMISSION_REFRESH_CODES
                ):
                    code = "admission_refresh_blocked"
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    if current["payload"].get("resume_plan_id") == p["resume_plan_id"]:
                        self._put(
                            conn,
                            current,
                            {
                                **current["payload"],
                                "resume_plan_id": None,
                                "status": "admission_blocked",
                                "error_code": code,
                            },
                        )
                blocked.add(p["item_id"])
                continue
            with self.repo.transaction() as conn:
                current = self._get(
                    conn, principal, execution["id"], session_id, "execution", lock=True
                )
                if current["payload"].get("resume_plan_id") == p["resume_plan_id"]:
                    self._put(
                        conn,
                        current,
                        {
                            **current["payload"],
                            "plan_id": p["resume_plan_id"],
                            "resume_plan_id": None,
                        },
                    )
        self._admit(
            principal,
            session_id,
            submission_id,
            [i for i in body["item_ids"] if i not in blocked],
        )
        return self.get_submission(principal, session_id, submission_id)

    def cancel(self, principal, session_id, submission_id, body, key):
        fields(body, {"item_ids"})
        with self.repo.transaction() as conn:
            session = self._access(
                principal, session_id, "jobs:write", conn=conn, lock=True
            )
            submission = self._get(
                conn, principal, submission_id, session_id, "submission", lock=True
            )
            items = [
                dict(r)
                for r in conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "item",
                        objects.c.parent_id == submission_id,
                    )
                ).mappings()
            ]
            selected = body.get("item_ids", [i["id"] for i in items])
            if (
                not isinstance(selected, list)
                or not selected
                or set(selected) - {i["id"] for i in items}
            ):
                raise QuickChatError(
                    "invalid_item_selection", "取消项不属于此批次。", 422
                )
            prior = self._replay(conn, principal, "cancel:" + submission_id, key, body)
            if not prior:
                if set(selected) == {i["id"] for i in items}:
                    self._put(
                        conn,
                        submission,
                        {**submission["payload"], "cancel_requested": True},
                    )
                for item in items:
                    if item["id"] in selected:
                        self._put(
                            conn, item, {**item["payload"], "cancel_requested": True}
                        )
                self._remember(
                    conn, principal, "cancel:" + submission_id, key, body, submission_id
                )
                self._event(
                    conn,
                    principal,
                    session,
                    "submission.cancel_requested",
                    submission_id,
                )
            executions = [
                self._get(
                    conn,
                    principal,
                    i["payload"]["current_execution_id"],
                    session_id,
                    "execution",
                )
                for i in items
                if i["id"] in selected
            ]
        for execution in executions:
            if execution["payload"]["job_id"]:
                self.hooks.cancel(principal, execution["payload"]["job_id"])
            else:
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    self._put(
                        conn, current, {**current["payload"], "status": "cancelled"}
                    )
        return self.get_submission(principal, session_id, submission_id)

    def create_turn(self, principal, session_id, body, key):
        fields(
            body,
            {"expected_version", "text", "model_id", "assistant_mode", "create_card"},
            {"expected_version", "text", "model_id"},
        )
        if (
            not isinstance(body["text"], str)
            or not body["text"].strip()
            or len(body["text"]) > 16000
        ):
            raise QuickChatError("invalid_text", "请输入1–16000字符。", 422)
        mode = body.get("assistant_mode", "assist")
        if mode not in {"none", "assist", "discuss"}:
            raise QuickChatError("invalid_assistant_mode", "助手方式无效。", 422)
        create_card = body.get("create_card", False)
        if type(create_card) is not bool or create_card and mode != "none":
            raise QuickChatError(
                "invalid_card_creation",
                "直接创建任务卡须明确使用assistant_mode=none、create_card=true。",
                422,
            )
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "projects:write",
                conn=conn,
                lock=True,
            )
            if (
                mode != "none"
                and principal.machine
                and not principal.allows(
                    session["payload"]["project_id"], "assistant:run"
                )
            ):
                raise QuickChatError(
                    "insufficient_scope",
                    "文本调用需要独立assistant:run授权；仍可直接保存任务卡。",
                    403,
                )
            prior = self._replay(conn, principal, "turn:" + session_id, key, body)
            if prior:
                turn = self._get(conn, principal, prior, session_id, "turn")
            else:
                model = model_check(body["model_id"])
                self._version(session, body["expected_version"])
                if mode != "none" and session["payload"]["active_turn_id"]:
                    raise QuickChatError(
                        "assistant_run_active",
                        "上一轮助手仍在运行或待核验，请先查看原记录。",
                    )
                latest_card = conn.execute(
                    select(events.c.resource_id)
                    .where(
                        events.c.tenant == self.tenant,
                        events.c.owner == principal.owner,
                        events.c.session_id == session_id,
                        events.c.type.in_(["card.created", "card.revised"]),
                    )
                    .order_by(events.c.seq.desc())
                    .limit(1)
                ).scalar_one_or_none()
                active_bindings = self._effective_bindings(
                    session["payload"]["bindings"],
                    session["payload"]["next_settings"]["recipe_id"],
                    session['payload']['next_settings'].get('deployment_profile_id'),
                )
                payload = {
                    "text": body["text"],
                    "model_id": model,
                    "assistant_mode": mode,
                    "input_refs": self._inputs(active_bindings),
                    "next_settings": session["payload"]["next_settings"],
                    "bindings": active_bindings,
                    "input_exclusions": [
                        {
                            "binding_id": b["binding_id"],
                            "asset_id": b["asset_id"],
                            "reason": b["inactive_reason"],
                        }
                        for b in active_bindings
                        if b["inactive_reason"]
                    ],
                    "status": "recorded" if mode == "none" else "pending",
                    "reply": None,
                    "error_code": None,
                    "card_id": None,
                    "context_revision_id": latest_card,
                    "assistant_run": {
                        "id": "run-" + uuid.uuid4().hex,
                        "status": "recorded" if mode == "none" else "pending",
                        "usage": {},
                        "media_input_manifest": [],
                        "fence": 0,
                    },
                }
                turn = self._new(conn, principal, session_id, "turn", payload)
                seq = self._event(conn, principal, session, "turn.created", turn["id"])
                self._put(conn, turn, {**payload, "seq": seq})
                if create_card:
                    # The explicit no-assistant command stores the turn and its
                    # first card atomically, using exactly the frozen inputs and
                    # settings. No preflight, provider or queue operation occurs.
                    snapshot, sources = self._card_snapshot(
                        principal,
                        session,
                        {
                            **payload["next_settings"],
                            "prompt": payload["text"],
                            "title": payload["text"].strip()[:40],
                            "inputs": payload["input_refs"],
                        },
                    )
                    card = self._new(
                        conn,
                        principal,
                        session_id,
                        "card",
                        {
                            "title": snapshot["title"],
                            "turn_id": turn["id"],
                            "current_revision_id": None,
                        },
                    )
                    self._create_revision(
                        conn, principal, session, card, snapshot, sources
                    )
                    self._put(conn, turn, {**turn["payload"], "card_id": card["id"]})
                self._put(
                    conn,
                    session,
                    {
                        **session["payload"],
                        "model_id": model,
                        "active_turn_id": turn["id"]
                        if mode != "none"
                        else session["payload"]["active_turn_id"],
                    },
                    bump=True,
                )
                self._remember(
                    conn, principal, "turn:" + session_id, key, body, turn["id"]
                )
                self._queue_title(conn, principal, session, turn)
        if not prior and mode != "none":
            self._run_assistant(principal, session_id, turn["id"])
        return self.get_turn(principal, session_id, turn["id"])

    def _run_assistant(self, principal, session_id, turn_id):
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "projects:write",
                conn=conn,
                lock=True,
            )
            turn = self._get(conn, principal, turn_id, session_id, "turn", lock=True)
            if turn["payload"]["status"] != "pending":
                return
            p = copy.deepcopy(turn["payload"])
            if not self.assistant_enabled or self.assistant is None:
                p.update(status="failed", error_code="assistant_disabled")
                p["assistant_run"]["status"] = "failed"
                self._put(conn, turn, p)
                self._put(conn, session, {**session["payload"], "active_turn_id": None})
                self._event(conn, principal, session, "assistant.failed", turn_id)
                return
            from .spending_limits import unquoted_spend_blocked

            if unquoted_spend_blocked(
                conn, self.tenant, principal, now=self.repo.clock()
            ):
                p.update(status="failed", error_code="assistant_quote_unavailable")
                p["assistant_run"]["status"] = "failed"
                self._put(conn, turn, p)
                self._put(conn, session, {**session["payload"], "active_turn_id": None})
                self._event(conn, principal, session, "assistant.failed", turn_id)
                return
            p["status"] = p["assistant_run"]["status"] = "running"
            p["assistant_run"].update(started_at=self.repo.clock(), fence=1)
            self._put(conn, turn, p)
            previous = [
                dict(r)
                for r in conn.execute(
                    select(objects)
                    .where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "turn",
                        objects.c.payload["seq"].as_integer() < p["seq"],
                        objects.c.payload["reply"].as_string().is_not(None),
                    )
                    .order_by(objects.c.payload["seq"].as_integer().desc())
                    .limit(20)
                ).mappings()
            ]
            previous.reverse()
        messages = []
        for old in previous:
            if old["payload"].get("reply"):
                messages.extend(
                    [
                        {"role": "user", "text": old["payload"]["text"]},
                        {"role": "model", "text": old["payload"]["reply"]},
                    ]
                )
        messages.append({"role": "user", "text": p["text"]})
        try:
            if any(
                b["enabled"] and b.get("source_range") for b in p["bindings"]
            ) and not principal.allows(
                session["payload"]["project_id"], "assets:write"
            ):
                raise QuickChatError(
                    "insufficient_scope",
                    "助手使用选段需要assets:write以创建受限派生收据；未请求上游。",
                    403,
                )
            if (
                len(previous) > 19
                or sum(len(m["text"]) for m in messages) > 45000
                or any(len(m["text"]) > 16000 for m in messages)
            ):
                raise QuickChatError(
                    "context_limit",
                    "已到完整上下文上限；输入已保存，未调用模型。请另开创作或显式整理上下文。",
                    422,
                )
            related = (
                self.get_revision(principal, session_id, p["context_revision_id"])
                if p.get("context_revision_id")
                else None
            )
            lineage = []
            parent = related.get("source_revision_id") if related else None
            for _ in range(4):
                if not parent:
                    break
                value = self.get_revision(principal, session_id, parent)
                lineage.append(
                    {
                        k: value.get(k)
                        for k in (
                            "id",
                            "card_id",
                            "version",
                            "source_revision_id",
                            "input_hash",
                        )
                    }
                )
                parent = value.get("source_revision_id")
            context = {
                "owner": principal.owner,
                "session_id": session_id,
                "turn_id": turn_id,
                "bindings": p["bindings"],
                "inputs": p["input_refs"],
                "history_turn_ids": [
                    r["id"] for r in previous if r["payload"].get("reply")
                ],
                "assistant_mode": p["assistant_mode"],
                "related_card": related,
                "lineage": lineage,
                "input_exclusions": p.get("input_exclusions", []),
                "assets": {
                    b["asset_id"]: self._asset(principal, session, b["asset_id"])
                    for b in p["bindings"]
                    if b["enabled"]
                },
            }

            def record_manifest(manifest):
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn, principal, turn_id, session_id, "turn", lock=True
                    )
                    if (
                        current["payload"]["status"] != "running"
                        or current["payload"]["assistant_run"]["fence"] != 1
                    ):
                        raise QuickChatError(
                            "assistant_run_fenced",
                            "原调用已被恢复流程隔离，未再次提交。",
                        )
                    payload = copy.deepcopy(current["payload"])
                    payload["assistant_run"].update(
                        media_input_manifest=canonical(manifest),
                        context_turn_ids=context["history_turn_ids"],
                        context_revision_id=p.get("context_revision_id"),
                        context_revision_hash=related.get("input_hash")
                        if related
                        else None,
                        context_lineage=lineage,
                        input_exclusions=context["input_exclusions"],
                        request_prepared_at=self.repo.clock(),
                    )
                    self._put(conn, current, payload)

            context["record_manifest"] = record_manifest
            # Even a text-only adapter receives a durable context receipt.
            record_manifest([])
            result = self.assistant.complete(
                p["model_id"], messages, context, p["next_settings"]
            )
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("reply"), str)
                or not result["reply"].strip()
                or len(result["reply"]) > 24000
            ):
                raise QuickChatError(
                    "assistant_invalid_response",
                    "助手响应格式无效，原输入已保留。",
                    502,
                )
            proposal = result.get("card")
            prepared = None
            if proposal and p["assistant_mode"] != "discuss":
                fields(
                    proposal,
                    {"title", "prompt", "controls", "recipe_id", "inputs", "copies"},
                    {"prompt"},
                )
                inherited = (
                    {
                        k: related[k]
                        for k in (
                            "recipe_id",
                            "controls",
                            "copies",
                            "deployment_profile_id",
                        )
                        if k in related
                    }
                    if related
                    else copy.deepcopy(p["next_settings"])
                )
                try:
                    controls_patch = proposal.get("controls", {})
                    validate_controls_patch(controls_patch)
                except (ValueError, TypeError):
                    raise QuickChatError(
                        "assistant_invalid_response",
                        "助手建议参数无效，未创建卡片。",
                        502,
                    ) from None
                inherited["controls"] = {**inherited["controls"], **controls_patch}
                proposed_inputs = proposal.get("inputs", p["input_refs"])
                active_ids = {ident for _, _, ident in input_entries(p["input_refs"])}
                if any(
                    ident not in active_ids
                    for _, _, ident in input_entries(proposed_inputs)
                ):
                    raise QuickChatError(
                        "reference_conflict",
                        "助手不能启用用户本轮未选择的素材；未创建卡片。",
                        422,
                    )
                prepared = self._card_snapshot(
                    principal,
                    session,
                    {
                        **inherited,
                        **{k: v for k, v in proposal.items() if k != "controls"},
                        "inputs": proposed_inputs,
                        "prompt": proposal["prompt"],
                        "title": proposal.get(
                            "title", related["title"] if related else "我的视频"
                        ),
                    },
                )
            with self.repo.transaction() as conn:
                session = self._access(
                    principal,
                    session_id,
                    "projects:read",
                    "projects:write",
                    conn=conn,
                    lock=True,
                )
                turn = self._get(
                    conn, principal, turn_id, session_id, "turn", lock=True
                )
                if (
                    turn["payload"]["status"] != "running"
                    or turn["payload"]["assistant_run"]["fence"] != 1
                ):
                    return
                p = copy.deepcopy(turn["payload"])
                p.update(status="completed", reply=result["reply"], error_code=None)
                p["assistant_run"].update(
                    status="completed",
                    completed_at=self.repo.clock(),
                    usage=canonical(result.get("usage", {})),
                    media_input_manifest=canonical(
                        result.get("media_input_manifest", [])
                    ),
                    context_turn_ids=context["history_turn_ids"],
                )
                if prepared:
                    card = self._new(
                        conn,
                        principal,
                        session_id,
                        "card",
                        {
                            "title": prepared[0]["title"],
                            "turn_id": turn_id,
                            "current_revision_id": None,
                        },
                    )
                    self._create_revision(
                        conn,
                        principal,
                        session,
                        card,
                        *prepared,
                        source_revision=p.get("context_revision_id"),
                    )
                    p["card_id"] = card["id"]
                self._put(conn, turn, p)
                self._put(conn, session, {**session["payload"], "active_turn_id": None})
                self._event(conn, principal, session, "assistant.completed", turn_id)
        except Exception as error:
            code = getattr(
                error,
                "code",
                "assistant_invalid_response"
                if isinstance(error, ValueError)
                else "assistant_call_unknown",
            )
            unknown = code in {
                "upstream_timeout",
                "connection_failed",
                "assistant_call_unknown",
            }
            with self.repo.transaction() as conn:
                session = self._access(
                    principal, session_id, "projects:write", conn=conn, lock=True
                )
                turn = self._get(
                    conn, principal, turn_id, session_id, "turn", lock=True
                )
                if (
                    turn["payload"]["status"] != "running"
                    or turn["payload"]["assistant_run"]["fence"] != 1
                ):
                    return
                p = copy.deepcopy(turn["payload"])
                p.update(
                    status="unknown" if unknown else "failed",
                    error_code="assistant_call_unknown" if unknown else code,
                )
                p["assistant_run"].update(
                    status=p["status"],
                    error_code=p["error_code"],
                    completed_at=self.repo.clock(),
                )
                self._put(conn, turn, p)
                if not unknown:
                    self._put(
                        conn, session, {**session["payload"], "active_turn_id": None}
                    )
                self._event(
                    conn,
                    principal,
                    session,
                    "assistant.unknown" if unknown else "assistant.failed",
                    turn_id,
                )

    def get_turn(self, principal, session_id, turn_id):
        self._access(principal, session_id, "projects:read")
        with self.repo.engine.connect() as conn:
            row = self._get(conn, principal, turn_id, session_id, "turn")
        return {
            k: v
            for k, v in {
                **row["payload"],
                "id": turn_id,
                "created_at": row["created_at"],
            }.items()
            if k != "bindings"
        }

    def acknowledge_unknown(self, principal, session_id, turn_id, body, key):
        fields(body, {"acknowledged"}, {"acknowledged"})
        if body["acknowledged"] is not True:
            raise QuickChatError(
                "acknowledgement_required", "请明确承认原调用可能已经执行或计费。", 422
            )
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "projects:write",
                conn=conn,
                lock=True,
            )
            turn = self._get(conn, principal, turn_id, session_id, "turn", lock=True)
            prior = self._replay(conn, principal, "ack:" + turn_id, key, body)
            if not prior:
                if turn["payload"]["status"] != "unknown":
                    raise QuickChatError(
                        "assistant_not_unknown", "只有未知调用需要此确认。"
                    )
                p = copy.deepcopy(turn["payload"])
                p["assistant_run"].update(
                    acknowledged_at=self.repo.clock(),
                    acknowledged_by=principal.actor_id,
                    fence=p["assistant_run"]["fence"] + 1,
                )
                self._put(conn, turn, p)
                if session["payload"]["active_turn_id"] == turn_id:
                    self._put(
                        conn,
                        session,
                        {**session["payload"], "active_turn_id": None},
                        bump=True,
                    )
                self._remember(conn, principal, "ack:" + turn_id, key, body, turn_id)
                self._event(
                    conn, principal, session, "assistant.unknown_acknowledged", turn_id
                )
        return self.get_turn(principal, session_id, turn_id)

    def recover_assistant_runs(self, *, older_than_s=180):
        """Explicit startup reconciliation; never called by GET, never posts upstream."""
        with self.repo.transaction() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    select(objects)
                    .where(
                        objects.c.tenant == self.tenant,
                        objects.c.kind == "turn",
                        objects.c.payload["status"]
                        .as_string()
                        .in_(["running", "pending"]),
                        objects.c.updated_at < self.repo.clock() - older_than_s,
                    )
                    .order_by(objects.c.owner, objects.c.id)
                    .limit(100)
                ).mappings()
            ]
            recovered = 0
            for row in rows:
                principal = Principal(row["owner"], "quick-chat-recovery")
                # Lock in the same session -> turn order as normal writes;
                # a late response or another recovery worker may have won.
                session = self._get(
                    conn,
                    principal,
                    row["session_id"],
                    row["session_id"],
                    "session",
                    lock=True,
                )
                row = self._get(
                    conn, principal, row["id"], row["session_id"], "turn", lock=True
                )
                if (
                    row["payload"]["status"] not in {"pending", "running"}
                    or row["updated_at"] >= self.repo.clock() - older_than_s
                ):
                    continue
                p = copy.deepcopy(row["payload"])
                p["status"] = "unknown" if p["status"] == "running" else "failed"
                p["error_code"] = (
                    "assistant_call_unknown"
                    if p["status"] == "unknown"
                    else "assistant_interrupted_before_call"
                )
                p["assistant_run"].update(
                    status=p["status"],
                    error_code=p["error_code"],
                    fence=p["assistant_run"]["fence"] + 1,
                )
                self._put(conn, row, p)
                if p["status"] == "failed":
                    if session["payload"].get("active_turn_id") == row["id"]:
                        self._put(
                            conn,
                            session,
                            {**session["payload"], "active_turn_id": None},
                            bump=True,
                        )
                self._event(conn, principal, session, "assistant.recovered", row["id"])
                recovered += 1
            return recovered

    def timeline(
        self, principal, session_id, *, limit=20, cursor=None, direction="older"
    ):
        session = self._access(principal, session_id, "projects:read")
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or direction not in {"older", "newer"}
        ):
            raise QuickChatError("invalid_pagination", "分页方向或大小无效。", 422)
        boundary = (
            cursor_decode(cursor, principal, session_id, direction)
            if cursor
            else (0 if direction == "newer" else 2147483647)
        )
        if type(boundary) is not int or boundary < 0:
            raise QuickChatError("invalid_cursor", "分页位置无效。", 422)
        stmt = select(events).where(
            events.c.tenant == self.tenant,
            events.c.owner == principal.owner,
            events.c.session_id == session_id,
            events.c.seq < boundary
            if direction == "older"
            else events.c.seq > boundary,
        )
        with self.repo.engine.connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    stmt.order_by(
                        events.c.seq.desc() if direction == "older" else events.c.seq
                    ).limit(limit + 1)
                ).mappings()
            ]
        selected = rows[:limit]
        values = []
        for event in sorted(selected, key=lambda e: e["seq"]):
            ident, kind = event["resource_id"], event["type"]
            if kind.startswith("turn.") or kind.startswith("assistant."):
                record = self.get_turn(principal, session_id, ident)
            elif kind.startswith("card."):
                record = self.get_revision(principal, session_id, ident)
            elif kind.startswith("preflight."):
                record = (
                    self.get_preflight(principal, session_id, ident)
                    if principal.allows(session["payload"]["project_id"], "jobs:write")
                    else {"id": ident, "requires_scope": "jobs:write"}
                )
            elif kind.startswith("submission.") or kind.startswith("item."):
                record = (
                    self.get_submission(principal, session_id, ident)
                    if principal.allows(session["payload"]["project_id"], "jobs:read")
                    else {"id": ident, "requires_scope": "jobs:read"}
                )
            elif kind.startswith("result_import."):
                record = self.get_result_import(principal, session_id, ident)
            else:
                record = self._session_public(session)
            values.append(
                {k: event[k] for k in ("id", "seq", "type", "actor_id", "created_at")}
                | {"record": record}
            )
        return {
            "events": values,
            "limit": limit,
            "has_more": len(rows) > limit,
            "next_cursor": cursor_encode(
                principal, session_id, selected[-1]["seq"], direction
            )
            if len(rows) > limit
            else None,
            "latest_seq": session["payload"]["latest_seq"],
            "after_cursor": cursor_encode(
                principal,
                session_id,
                max(
                    (r["seq"] for r in selected),
                    default=session["payload"]["latest_seq"],
                ),
                "newer",
            ),
        }

    def result_import(self, principal, session_id, body, key):
        fields(
            body,
            {"source_artifact_id", "purpose", "source_range"},
            {"source_artifact_id"},
        )
        session = self._access(
            principal, session_id, "projects:read", "assets:write", "jobs:read"
        )
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(
                        artifacts.c.id,
                        artifacts.c.job_id,
                        artifacts.c.metadata,
                        jobs.c.project_id,
                        jobs.c.status,
                        jobs.c.execution_plan,
                    )
                    .join(jobs, artifacts.c.job_id == jobs.c.id)
                    .where(
                        artifacts.c.id == body["source_artifact_id"],
                        jobs.c.tenant_id == self.tenant,
                        jobs.c.owner_id == principal.owner,
                    )
                )
                .mappings()
                .first()
            )
        if (
            row is None
            or not principal.allows(row["project_id"], "jobs:read")
            or row["status"] != "succeeded"
        ):
            raise NotFound("artifact_not_found")
        value = row["metadata"]
        if (
            value.get("kind") not in {"image", "video", "audio"}
            or value.get("validated") is not True
        ):
            raise QuickChatError("reference_not_ready", "产物尚未验证为可用媒体。")
        if row["execution_plan"].get("backend") == "mock":
            raise QuickChatError(
                "simulated_artifact", "模拟占位不能作为真实参考素材。", 422
            )
        if body.get("source_range"):
            validate_range(body["source_range"])
        namespace = "import:" + session_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal, session_id, "assets:write", "jobs:read", conn=conn, lock=True
            )
            prior = self._replay(conn, principal, namespace, key, body)
            if prior:
                record = self._get(conn, principal, prior, session_id, "import")
            else:
                record = self._new(
                    conn,
                    principal,
                    session_id,
                    "import",
                    {
                        "source_artifact_id": row["id"],
                        "source_job_id": row["job_id"],
                        "source_sha256": value["sha256"],
                        "source_range": body.get("source_range"),
                        "purpose": body.get("purpose", "reference"),
                        "status": "pending",
                        "asset_id": None,
                        "error_code": None,
                    },
                )
                self._remember(conn, principal, namespace, key, body, record["id"])
                self._event(
                    conn, principal, session, "result_import.created", record["id"]
                )
        if record["payload"]["status"] == "ready":
            return self.get_result_import(principal, session_id, record["id"])
        # Reconcile uploads by stable client asset identity, including lost response.
        client_id = "chat-result-" + record["id"]
        existing = next(
            (
                a
                for a in self.assets.list(
                    principal.owner, session["payload"]["project_id"]
                )
                if a.get("client_asset_id") == client_id
            ),
            None,
        )
        try:
            if existing:
                imported = (
                    self.assets.public(
                        self.assets.get(principal.owner, existing["asset_id"])
                    )
                    if existing["status"] == "ready"
                    else self.assets.resume(principal.owner, existing["asset_id"])
                )
            else:
                size = value.get("size_bytes")
                if type(size) is not int or not 0 < size <= self.assets.max_bytes:
                    raise QuickChatError(
                        "result_too_large", "产物超过素材导入限制。", 422
                    )
                # Stream through a checksum reader; no unbounded memory or external URL.
                with self.storage.open(value["object_key"]) as source:
                    out = io.BytesIO() if size <= 4 * 1024 * 1024 else None
                    import tempfile

                    with out or tempfile.TemporaryFile() as checked:
                        digest, count = hashlib.sha256(), 0
                        while True:
                            chunk = source.read(min(1024 * 1024, size - count + 1))
                            if not chunk:
                                break
                            count += len(chunk)
                            if count > size:
                                raise QuickChatError(
                                    "result_integrity_failed", "产物大小校验失败。"
                                )
                            digest.update(chunk)
                            checked.write(chunk)
                        if count != size or digest.hexdigest() != value["sha256"]:
                            raise QuickChatError(
                                "result_integrity_failed",
                                "产物校验失败，未创建参考素材。",
                            )
                        checked.seek(0)
                        suffix = {
                            "image/png": ".png",
                            "image/jpeg": ".jpg",
                            "image/webp": ".webp",
                            "video/mp4": ".mp4",
                            "audio/flac": ".flac",
                            "audio/x-flac": ".flac",
                            "audio/wav": ".wav",
                            "audio/x-wav": ".wav",
                            "audio/mpeg": ".mp3",
                        }.get(value.get("content_type", value.get("mime")))
                        if not suffix:
                            raise QuickChatError(
                                "result_media_unsupported",
                                "产物已验证的媒体格式尚未接入素材导入。",
                                422,
                            )
                        imported = self.assets.upload(
                            principal.owner,
                            session["payload"]["project_id"],
                            checked,
                            "result" + suffix,
                            client_asset_id=client_id,
                        )
            if imported["status"] != "ready":
                raise QuickChatError(
                    "reference_not_ready", "导入素材仍在处理，请恢复原收据。"
                )
            # Preserve the original receipt and immutable selection intent.
            # Derivation occurs at reference preflight, where the selected
            # range is already part of the card hash. This import is therefore
            # replay-safe even if the response is lost after upload completes.
            if body.get("source_range"):
                source_duration = imported.get("metadata", {}).get("source_duration")
                if (
                    type(source_duration) not in (int, float)
                    or body["source_range"]["end"] > source_duration
                ):
                    raise QuickChatError(
                        "invalid_source_range", "导入片段超出已验证素材时长。", 422
                    )
            with self.repo.transaction() as conn:
                current = self._get(
                    conn, principal, record["id"], session_id, "import", lock=True
                )
                self._put(
                    conn,
                    current,
                    {
                        **current["payload"],
                        "status": "ready",
                        "asset_id": imported["asset_id"],
                        "original_asset_id": imported["asset_id"],
                        "error_code": None,
                    },
                )
        except Exception as error:
            code = (
                error.code
                if isinstance(error, QuickChatError)
                else "result_import_unknown"
            )
            with self.repo.transaction() as conn:
                current = self._get(
                    conn, principal, record["id"], session_id, "import", lock=True
                )
                self._put(
                    conn,
                    current,
                    {
                        **current["payload"],
                        "status": "unknown"
                        if code == "result_import_unknown"
                        else "failed",
                        "error_code": code,
                    },
                )
        return self.get_result_import(principal, session_id, record["id"])

    def get_result_import(self, principal, session_id, ident):
        self._access(principal, session_id, "projects:read", "assets:read")
        with self.repo.engine.connect() as conn:
            row = self._get(conn, principal, ident, session_id, "import")
        return {**row["payload"], "id": ident, "created_at": row["created_at"]}
