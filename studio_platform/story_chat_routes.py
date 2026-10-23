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
    open_actions,
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
    from .story_chat_images import ServiceCreditBilling

    # Audited story-image credits (#463): hold before Google, capture after attachment.
    charges = billing or ServiceCreditBilling(app)

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

    def pictures_enabled():
        from .story_chat_settings import read as read_settings

        with app.state.repository.engine.connect() as connection:
            return read_settings(connection, settings.tenant_id)["pictures_enabled"]

    def require_admin(request):
        from .identity import is_admin

        principal = request.state.principal
        if not is_admin(principal, app.state.repository, settings):
            _fail("admin_required", 403)
        return principal

    @app.get("/v1/admin/story-chat/settings")
    def get_story_chat_settings(request: Request):
        from .story_chat_settings import read as read_settings

        require_admin(request)
        with app.state.repository.engine.connect() as connection:
            return read_settings(connection, settings.tenant_id)

    @app.put("/v1/admin/story-chat/settings")
    def put_story_chat_settings(request: Request, body: dict):
        from .story_chat_settings import StoryChatSettingsError, write

        principal = require_admin(request)
        repo = app.state.repository
        try:
            with repo.transaction() as connection:
                return write(
                    connection, settings.tenant_id, principal, body, repo.clock()
                )
        except StoryChatSettingsError as error:
            _fail(error.code, error.status)

    @app.get("/v1/story-chats/config")
    def story_chat_config(request: Request):
        if holder["client"] is None and enabled:
            holder["client"] = configured_client(settings)
        return {
            "enabled": enabled and not request.state.principal.machine,
            # False means the text credential is missing: turns would answer 503.
            "assistant_ready": holder["client"] is not None,
            # Admin switch (default off): whether card pictures may be drawn here.
            "pictures_enabled": enabled and pictures_enabled(),
            # Credits per card picture from the server's price revision; null while
            # charging is off or no image price is published (then pictures are free/refused).
            "image_credits": (charges.price(settings.tenant_id) or {}).get("credits")
            if hasattr(charges, "price")
            else None,
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

    @app.post("/v1/story-chats/{project_id}/open")
    def open_story_chat(project_id: str, request: Request, body: dict | None = None):
        """Continue an existing story in the chat (owner, 2026-10-23). Idempotent."""
        principal = principal_of(request)
        body = body or {}
        language = body.get("language", "zh")
        if set(body) - {"language"} or language not in LANGUAGES:
            _fail("story_chat_invalid", 422, field="body")
        for _attempt in range(3):
            row = app.state.authorized_project(principal, project_id, "projects:write")
            project = row["payload"]
            if project.get("journey", {}).get("storyChat") is not None:
                try:
                    chat_of(project)
                except StoryChatError:
                    # Present but not a chat this version reads: never replace it.
                    _fail("story_chat_unsupported", 409)
                return view(row)
            from .generation_admission import reject_managed

            reject_managed(project)
            if project.get("schemaVersion") != 4:
                _fail("story_chat_unsupported_story", 422)
            try:
                app.state.guided.mutate(
                    principal,
                    project_id,
                    {
                        "expected_version": row["version"],
                        "actions": open_actions(project, language),
                    },
                )
            except Conflict as error:
                if str(error) != "document_version_conflict":
                    raise
                continue
            return view(read(principal, project_id))
        _fail("story_chat_busy", 409)

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

        from sqlalchemy import select

        from .credits import CreditError
        from .guided import receipts
        from .story_chat_images import RELEASE_REASON, input_hash

        def credit_fail(error):
            _fail(error.code, error.status, **getattr(error, "data", {}))

        start_key = "story-chat-image-start-" + request_id
        attach_key = "story-chat-image-" + request_id

        def receipt(key):
            # Immutable edit receipts survive a later whole-project save; the editable
            # journey map alone is never proof that a request did or did not run.
            with app.state.repository.engine.connect() as connection:
                return connection.execute(
                    select(receipts.c.response).where(
                        receipts.c.tenant == settings.tenant_id,
                        receipts.c.owner == principal.owner,
                        receipts.c.actor == principal.actor_id,
                        receipts.c.project_id == project_id,
                        receipts.c.key == key,
                    )
                ).scalar_one_or_none()

        def recorded_image(response):
            try:
                return response["project"]["journey"]["storyChat"]["images"][request_id]
            except (KeyError, TypeError):
                return None

        row = read(principal, project_id, "projects:write")
        card = card_in(row["payload"])
        started, attached = receipt(start_key), receipt(attach_key)
        if card is None and attached is None:
            _fail("story_chat_card_missing", 404, ref=card_id)
        if started is None and attached is None and not pictures_enabled():
            # Admin switch off: no new picture, before any hold or provider call. Requests
            # that already started still settle their original outcome below.
            _fail("story_chat_pictures_disabled", 403)
        reference = "story_chat_image:" + request_id
        scope = app.state.scope(principal, project_id)
        original = None
        if started is not None or attached is not None:
            # Any recovery needs the validated original decision. An attachment without a
            # valid start receipt (legacy or incomplete history) is never charged anew.
            try:
                original = started["project"]["journey"]["storyChat"]["imageRequests"][
                    request_id
                ]
                snapshot_card = card_in(started["project"])
            except (KeyError, TypeError):
                original, snapshot_card = None, None
            if (
                not isinstance(original, dict)
                or type(original.get("charged")) is not bool
                or not isinstance(original.get("input_hash"), str)
                or len(original["input_hash"]) != 64
                or original.get("card_id") != card_id
                or snapshot_card is None
            ):
                if isinstance(original, dict) and original.get("card_id") not in (
                    None,
                    card_id,
                ):
                    _fail("story_chat_image_request_conflict", 409, field="card_id")
                _fail("story_chat_image_outcome_unconfirmed", 502)
            # Same request id, same request: the incoming instruction must reproduce the
            # original hash against the IMMUTABLE start snapshot (not today's edited card).
            if (
                input_hash(started["project"], snapshot_card, instruction)
                != original["input_hash"]
            ):
                _fail("story_chat_image_request_conflict", 409, field="instruction")
        binding = {
            "tenant": scope.tenant_id,
            "owner": scope.owner_id,
            "project_id": project_id,
            "card_id": card_id,
            "request_id": request_id,
            # The material the original request froze; replays never re-derive it.
            "input_hash": original["input_hash"]
            if original
            else input_hash(row["payload"], card or {}, instruction),
            "actor_id": scope.actor_id,
        }

        def finish(response):
            # Charge only once the picture is durably on the card (attachment receipt).
            if held is not None:
                try:
                    charges.capture(scope, reference, binding)
                except CreditError as error:
                    credit_fail(error)
            current = read(principal, project_id)
            return {**view(current), "image": recorded_image(response)}

        # The original request's charging decision is final: a request that started while
        # charging was off is never charged later, and a charged one is always settled.
        held = None
        if (
            original is not None
            and original["charged"]
            and not charges.has_hold(scope, reference)
        ):
            # A start marker is project data a client can also write; only real ledger
            # evidence may be recovered. Never admit a new paid hold on a replay.
            _fail("story_chat_image_outcome_unconfirmed", 502)
        if original is None or original["charged"]:
            try:
                held = charges.hold(scope, reference, binding)
            except CreditError as error:
                credit_fail(error)
            if original is not None and (held is None or held["created"]):
                _fail("story_chat_image_outcome_unconfirmed", 502)
        if attached is not None:
            # A replay of a delivered picture never draws again; it settles the original hold.
            return finish(attached)
        if held is not None and not held["created"]:
            if held["terminal"] is not None:
                _fail("story_chat_image_released", 409)
            # Another request owns this open hold: it may be drawing right now. Never
            # draw again and never release a hold this request did not create.
            _fail("story_chat_image_in_progress", 409)
        if started is not None:
            # Running now (another tab, a double click) or an unknown outcome such as an
            # upstream timeout: never resubmit the same request to the provider.
            _fail("story_chat_image_in_progress", 409)

        def release(reason):
            # Only the request that created the hold may release it.
            if held is None or not held["created"]:
                return
            try:
                charges.release(scope, reference, binding, reason)
            except CreditError:
                # An unconfirmed attachment outcome keeps the hold for receipt recovery.
                pass

        if holder["image"] is None:
            holder["image"] = configured_image_client(settings)
        if holder["image"] is None:
            release("provider_failed")
            _fail("assistant_unavailable", 503)
        # Fence the request durably BEFORE the provider call: whoever commits this keyed
        # receipt first owns the request; everyone else sees it in progress.
        fenced = False
        for _attempt in range(2):
            chat_now = chat_of(row["payload"])
            try:
                app.state.guided.mutate(
                    principal,
                    project_id,
                    {
                        "expected_version": row["version"],
                        "actions": [
                            {
                                "op": "journey.update",
                                "patch": {
                                    "storyChat": {
                                        **chat_now,
                                        "imageRequests": {
                                            **chat_now.get("imageRequests", {}),
                                            request_id: {
                                                "card_id": card_id,
                                                "at": now_iso(),
                                                "input_hash": binding["input_hash"],
                                                "charged": held is not None,
                                            },
                                        },
                                    }
                                },
                            }
                        ],
                    },
                    start_key,
                )
                fenced = True
                break
            except Conflict as error:
                if str(error) != "document_version_conflict" or receipt(start_key):
                    break  # another request committed the fence first
                row = read(principal, project_id, "projects:write")
                card = card_in(row["payload"])
                if card is None:
                    release("card_missing")
                    _fail(
                        "story_chat_card_missing",
                        409,
                        ref=card_id,
                        nothing_charged=True,
                    )
                if (
                    input_hash(row["payload"], card, instruction)
                    != binding["input_hash"]
                ):
                    # The card changed after the price was frozen: draw nothing rather
                    # than a picture of different material than was agreed.
                    release("version_conflict")
                    _fail(
                        "story_chat_card_changed",
                        409,
                        cards=[card_id],
                        nothing_charged=True,
                    )
        if not fenced:
            # Lost the race or could not fence: never call the provider on a guess.
            release("version_conflict")
            _fail("story_chat_image_in_progress", 409)
        try:
            mime, data = holder["image"].generate(
                prompt_for(row["payload"], card, instruction), ASPECT[card["type"]]
            )
        except ImageError as error:
            release(RELEASE_REASON.get(error.code, "provider_failed"))
            _fail(error.code, error.status, nothing_charged=True, **error.detail)
        try:
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
        except Exception as error:
            release("store_failed")
            if isinstance(error, ImageError):
                _fail(error.code, error.status, nothing_charged=True)
            raise
        image_id = "image-" + request_id
        for _attempt in range(3):
            row = read(principal, project_id, "projects:write")
            project = row["payload"]
            chat = chat_of(project)
            current = card_in(project)
            if current is None:
                release("card_missing")
                _fail("story_chat_card_missing", 409, ref=card_id, nothing_charged=True)
            # Exactly the receipt shape the credit capture verifies.
            record = {
                "card_id": card_id,
                "asset_id": asset["asset_id"],
                "entity_id": image_id,
                "at": now_iso(),
            }
            value = copy.deepcopy(chat)
            value["images"] = {**value.get("images", {}), request_id: record}
            try:
                response = app.state.guided.mutate(
                    principal,
                    project_id,
                    {
                        "expected_version": row["version"],
                        "actions": attach_actions(
                            project, current, asset["asset_id"], image_id
                        )
                        + [{"op": "journey.update", "patch": {"storyChat": value}}],
                    },
                    # The immutable edit receipt is the attachment proof capture verifies.
                    attach_key,
                )
            except Conflict as error:
                if str(error) != "document_version_conflict":
                    _fail("story_chat_image_outcome_unconfirmed", 502)
                continue
            except Exception:
                # Unknown commit outcome: never release blindly; a replay reads the receipt.
                _fail("story_chat_image_outcome_unconfirmed", 502)
            return finish(response)
        release("version_conflict")
        _fail("document_version_conflict", 409, nothing_charged=True)
