"""HTTP routes for chat-first story building (#422). See story_chat.py for the rules."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request

from .google_chat import ChatError, GoogleChatClient
from .repository import Conflict
from .story_chat import (
    INSTRUCTION,
    LANGUAGES,
    MAX_TEXT_CHARS,
    MAX_TURNS,
    MODEL,
    TURN_ID,
    StoryChatError,
    append_turn,
    apply_plan,
    cards,
    chat_of,
    new_story,
    prompt_messages,
    snapshot,
    stored_turn,
    _parse,
)


def configured_client(settings):
    """Reuse the existing Google text credential (titles); no new secret."""
    import httpx

    timeout = httpx.Timeout(90, connect=10)
    if settings.title_use_central:
        return GoogleChatClient(timeout=timeout)
    if settings.title_config_file is None:
        return None
    from .google_title_config import GoogleTitleConfigError, runtime_config

    try:
        config = runtime_config(settings.title_config_file)
    except GoogleTitleConfigError:
        return None
    if config is None:
        return None
    return GoogleChatClient(loader=lambda: config, timeout=timeout)


def _fail(code, status, **detail):
    raise HTTPException(status, {"code": code, **detail})


def register_story_chat_routes(app, *, client=None):
    settings = app.state.settings
    enabled = settings.story_chat_enabled
    holder = {"client": client}

    def model_client():
        if holder["client"] is None:
            holder["client"] = configured_client(settings)
        if holder["client"] is None:
            _fail("assistant_unavailable", 503)
        return holder["client"]

    def principal_of(request):
        if not enabled:
            _fail("story_chat_disabled", 404)
        principal = request.state.principal
        if principal.machine:
            # Agents already edit projects through the public actions API.
            _fail("story_chat_browser_only", 403)
        return principal

    def view(row):
        project = row["payload"]
        chat = chat_of(project)
        return {
            "project_id": project["id"],
            "version": row["version"],
            "title": project.get("title"),
            "logline": project.get("logline", ""),
            "language": chat.get("language", "zh"),
            "turns": chat.get("turns", 0),
            "max_turns": MAX_TURNS,
            "messages": chat.get("messages", []),
            "cards": cards(project, chat),
        }

    def read(principal, project_id, scope="projects:read"):
        row = app.state.authorized_project(principal, project_id, scope)
        try:
            chat_of(row["payload"])
        except StoryChatError as error:
            _fail(error.code, error.status)
        if scope == "projects:write":
            # Same gate as /v1/projects/{id}/actions: managed projects are not edited here.
            from .generation_admission import reject_managed

            reject_managed(row["payload"])
        return row

    @app.get("/v1/story-chats/config")
    def story_chat_config(request: Request):
        if holder["client"] is None and enabled:
            holder["client"] = configured_client(settings)
        return {
            "enabled": enabled and not request.state.principal.machine,
            # False means the text credential is missing: turns would answer 503.
            "assistant_ready": holder["client"] is not None,
            "model": MODEL,
            "max_text_chars": MAX_TEXT_CHARS,
            "max_turns": MAX_TURNS,
        }

    @app.post("/v1/story-chats", status_code=201)
    def create_story_chat(request: Request, body: dict | None = None):
        principal = principal_of(request)
        body = body or {}
        if set(body) - {"title", "language"}:
            _fail("story_chat_invalid", 422, field="body")
        language = body.get("language", "zh")
        if language not in LANGUAGES:
            _fail("story_chat_invalid", 422, field="language")
        title = body.get("title") or ("New story" if language == "en" else "新故事")
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            _fail("story_chat_invalid", 422, field="title")
        project = new_story("project-" + uuid.uuid4().hex, title.strip(), language)
        app.state.guided.mutate(principal, project["id"], project, None, create=True)
        return view(read(principal, project["id"]))

    @app.get("/v1/story-chats/{project_id}")
    def get_story_chat(project_id: str, request: Request):
        principal = principal_of(request)
        return view(read(principal, project_id))

    @app.post("/v1/story-chats/{project_id}/turns")
    def story_chat_turn(project_id: str, request: Request, body: dict):
        principal = principal_of(request)
        if not isinstance(body, dict) or set(body) != {"turn_id", "text"}:
            _fail("story_chat_invalid", 422, field="body")
        turn_id, text = body["turn_id"], body["text"]
        if not isinstance(turn_id, str) or not TURN_ID.fullmatch(turn_id):
            _fail("story_chat_invalid", 422, field="turn_id")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            _fail("story_chat_invalid", 422, field="text", max=MAX_TEXT_CHARS)
        text = text.strip()

        def answer(row, messages):
            result = view(row)
            assistant = next(m for m in messages if m["role"] == "assistant")
            return {
                **result,
                "turn": {
                    "turn_id": turn_id,
                    "messages": messages,
                    "changes": assistant.get("changes", []),
                    "proposal": assistant.get("proposal"),
                    "chips": assistant.get("chips", []),
                },
            }

        row = read(principal, project_id, "projects:write")
        chat = chat_of(row["payload"])
        seen, seen_version = snapshot(row["payload"]), row["version"]
        # A replayed turn returns what was stored and never calls the model twice.
        prior = stored_turn(chat, turn_id)
        if prior:
            return answer(row, prior)
        if chat.get("turns", 0) >= MAX_TURNS:
            _fail("story_chat_turn_limit", 429, max_turns=MAX_TURNS)
        try:
            result = model_client().generate(
                MODEL,
                prompt_messages(row["payload"], chat, text),
                INSTRUCTION,
                max_tokens=4096,
            )
            plan = _parse(result["text"])
        except ChatError as error:
            # Nothing was written; the person can simply send again.
            _fail("assistant_" + error.code, error.http_status, message=error.message)
        except StoryChatError as error:
            _fail(error.code, error.status, **error.detail)
        # A card edit during the model call bumps the version: re-apply the same plan once
        # on the new version, or refuse if a card it names is gone.
        for _attempt in range(2):
            project, version = row["payload"], row["version"]
            chat = chat_of(project)
            prior = stored_turn(chat, turn_id)
            if prior:
                return answer(row, prior)
            try:
                actions, changes, proposal, touched = apply_plan(project, chat, plan)
            except StoryChatError as error:
                _fail(error.code, error.status, nothing_written=True, **error.detail)
            if version != seen_version:
                # The person edited during the model call. Re-apply only if none of the
                # cards this plan writes changed; never overwrite their newer edit.
                now = snapshot(project)
                changed = sorted(i for i in touched if now.get(i) != seen.get(i))
                if changed:
                    _fail(
                        "story_chat_card_changed",
                        409,
                        nothing_written=True,
                        cards=changed,
                    )
            value = append_turn(chat, turn_id, text, plan, changes, proposal)
            try:
                app.state.guided.mutate(
                    principal,
                    project_id,
                    {
                        "expected_version": version,
                        "actions": actions
                        + [{"op": "journey.update", "patch": {"storyChat": value}}],
                    },
                )
            except Conflict as error:
                if str(error) != "document_version_conflict":
                    raise
                row = read(principal, project_id, "projects:write")
                continue
            except ValueError as error:
                # The plan produced a document the existing validator refuses.
                _fail(
                    "story_chat_plan_rejected",
                    502,
                    nothing_written=True,
                    reason=str(error)[:300],
                )
            row = read(principal, project_id)
            return answer(row, stored_turn(chat_of(row["payload"]), turn_id))
        _fail("document_version_conflict", 409, nothing_written=True)
