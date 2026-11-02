"""Durable assistant turn claims, results and interrupted-call recovery."""

from __future__ import annotations

import copy
import logging
import uuid

from sqlalchemy import (
    select,
)

from .auth import Principal
from .generation_draft import (
    input_entries,
    validate_controls_patch,
)
from .quick_chat_state import (
    QuickChatError,
    events,
    fields,
    objects,
)
from .repository import (
    canonical,
)

logger = logging.getLogger(__name__)


class QuickChatTurnsMixin:
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
                model = self._model_check(body["model_id"])
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
            logger.warning("quick_chat_assistant_failed")
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
