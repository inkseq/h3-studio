"""Durable chat-native creation. Provider calls only through explicit hooks.

The project/shot document is an execution projection, never a second editable
source. All state uses the platform database; no timers, fake replies or GPUs.
"""

from __future__ import annotations

import copy
import secrets as secrets
import uuid

from sqlalchemy import (
    insert,
    select,
    update,
    func,
)

from .quick_chat_state import (
    DEFAULT_SETTINGS as DEFAULT_SETTINGS,
    EMPTY_INPUTS as EMPTY_INPUTS,
    QuickChatError as QuickChatError,
    QuickChatHooks as QuickChatHooks,
    cursor_decode as cursor_decode,
    cursor_encode as cursor_encode,
    events as events,
    fields as fields,
    key_check as key_check,
    metadata as metadata,
    objects as objects,
    operations as operations,
)
from .quick_chat_cards import QuickChatCardsMixin
from .quick_chat_execution import (
    QuickChatExecutionMixin,
    ADMISSION_REFRESH_CODES as ADMISSION_REFRESH_CODES,
)
from .quick_chat_turns import QuickChatTurnsMixin

from .capabilities import RECIPES, VERSION as VERSION
from .creator_controls import default_chat_profile
from .generation_draft import (
    input_entries,
    prepare_patch,
    validate_controls_patch,
)
from .guided import empty_project
from .project_validation import validate_project
from .project_activity import append_activity
from .repository import (
    NotFound,
    canonical,
    request_hash,
    documents,
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


class QuickChatService(
    QuickChatTakesMixin,
    QuickChatHistoryMixin,
    QuickChatTitleMixin,
    QuickChatCardsMixin,
    QuickChatExecutionMixin,
    QuickChatTurnsMixin,
):
    # Keep original module-level model resolution available to callers/tests.
    _model_check = staticmethod(model_check)

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

    def _card_snapshot(self, principal, session, body):
        body = default_chat_profile(self.settings, body)
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
