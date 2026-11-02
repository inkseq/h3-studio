"""Immutable card authoring and original material import receipts."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import math
import secrets
import uuid

from sqlalchemy import (
    and_,
    or_,
    select,
    update,
)

from .content_lifecycle import (
    assert_project_writable,
    assert_reference_payload,
    owner_reference_gate,
)
from .generation_draft import (
    configure,
    prepare_patch,
    validate_range,
)
from .guided import new_entity, now_iso
from .project_activity import append_activity
from .project_validation import ID, validate_project
from .quick_chat_state import (
    EMPTY_INPUTS,
    QuickChatError,
    cursor_decode,
    cursor_encode,
    fields,
    objects,
)
from .repository import (
    NotFound,
    Scope,
    artifacts,
    canonical,
    documents,
    jobs,
    request_hash,
)

logger = logging.getLogger(__name__)


class QuickChatCardsMixin:
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
        for binding, participation in zip(bindings, effective, strict=False):
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
            prepare_patch({"inputs": refs}, lambda _ident: self._source(value))  # noqa: B023 -- prepare_patch calls the resolver synchronously
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
            new_entity(
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
            logger.warning("quick_chat_result_import_failed")
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
