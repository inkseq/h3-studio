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


def register_story_chat_routes(app, *, client=None, image_client=None, billing=None):
    settings = app.state.settings
    enabled = settings.story_chat_enabled
    holder = {"client": client, "image": image_client}
    from .story_chat_images import NoBilling

    # Replaced by the credit service charge once it exists (#422); until then pictures
    # are refused while credit charging is on, never given away.
    charges = billing or NoBilling(app)

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

    @app.get("/v1/story-chats")
    def list_story_chats(request: Request, limit: int = 50):
        principal = principal_of(request)
        if type(limit) is not int or not 1 <= limit <= 100:
            _fail("story_chat_invalid", 422, field="limit")
        rows = app.state.repository.list_documents(
            app.state.generation_access.project_scope(principal),
            "project",
            limit=limit,
            summary=True,
            exclude_managed=True,
            story_chat=True,
        )
        return {
            "story_chats": [
                {
                    "project_id": row["document_id"],
                    "title": row["payload"]["title"],
                    "version": row["version"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
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
                json_mode=True,
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

    @app.post("/v1/story-chats/{project_id}/images")
    def story_chat_image(project_id: str, request: Request, body: dict):
        import copy
        import io

        from .story_chat import now_iso
        from .story_chat_images import (
            ASPECT,
            KINDS,
            REQUEST_ID,
            ImageError,
            attach_actions,
            configured_image_client,
            prompt_for,
        )

        principal = principal_of(request)
        if (
            not isinstance(body, dict)
            or not {"request_id", "card_id"} <= set(body)
            or set(body) - {"request_id", "card_id", "instruction"}
        ):
            _fail("story_chat_invalid", 422, field="body")
        request_id, card_id = body["request_id"], body["card_id"]
        instruction = body.get("instruction", "")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            _fail("story_chat_invalid", 422, field="request_id")
        if (
            not isinstance(card_id, str)
            or not isinstance(instruction, str)
            or len(instruction) > 1000
        ):
            _fail("story_chat_invalid", 422, field="card_id")

        def card_in(project):
            card = next((e for e in project["entities"] if e["id"] == card_id), None)
            if (
                card is None
                or card["type"] not in KINDS
                or card["parentId"] is not None
            ):
                return None
            return card

        row = read(principal, project_id, "projects:write")
        done = chat_of(row["payload"]).get("images", {}).get(request_id)
        if done:
            # A replayed request returns the stored result and never draws or charges again.
            return {**view(row), "image": done}
        card = card_in(row["payload"])
        if card is None:
            _fail("story_chat_card_missing", 404, ref=card_id)
        if holder["image"] is None:
            holder["image"] = configured_image_client(settings)
        if holder["image"] is None:
            _fail("assistant_unavailable", 503)
        reference = "story_chat_image:" + request_id
        try:
            charges.hold(principal, reference)
        except ImageError as error:
            _fail(error.code, error.status, **error.detail)
        record = None
        try:
            mime, data = holder["image"].generate(
                prompt_for(row["payload"], card, instruction), ASPECT[card["type"]]
            )
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[
                mime
            ]
            asset = app.state.assets.upload(
                principal.owner,
                project_id,
                io.BytesIO(data),
                "story-picture" + suffix,
                client_asset_id="story-chat-image:" + request_id,
            )
            if asset.get("status") != "ready":
                raise ImageError("image_store_failed", 502)
            image_id = "image-" + request_id
            for _attempt in range(3):
                row = read(principal, project_id, "projects:write")
                project = row["payload"]
                chat = chat_of(project)
                record = chat.get("images", {}).get(request_id)
                if record:
                    break
                current = card_in(project)
                if current is None:
                    raise ImageError("story_chat_card_missing", 409, ref=card_id)
                record = {
                    "card_id": card_id,
                    "asset_id": asset["asset_id"],
                    "entity_id": image_id,
                    "at": now_iso(),
                }
                value = copy.deepcopy(chat)
                value["images"] = {**value.get("images", {}), request_id: record}
                try:
                    app.state.guided.mutate(
                        principal,
                        project_id,
                        {
                            "expected_version": row["version"],
                            "actions": attach_actions(
                                project, current, asset["asset_id"], image_id
                            )
                            + [{"op": "journey.update", "patch": {"storyChat": value}}],
                        },
                    )
                    break
                except Conflict as error:
                    if str(error) != "document_version_conflict":
                        raise
                    record = None
            if record is None:
                raise ImageError("document_version_conflict", 409)
        except Exception as error:
            # Nothing reached the card: return the held credits. Google's cost stays ours.
            charges.release(principal, reference, type(error).__name__)
            if isinstance(error, ImageError):
                _fail(error.code, error.status, nothing_charged=True, **error.detail)
            raise
        charges.capture(principal, reference)
        return {**view(read(principal, project_id)), "image": record}
