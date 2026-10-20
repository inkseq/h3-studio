"""Thin authenticated chat routes. State and side effects live in one service."""

from fastapi import BackgroundTasks, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

from .quick_chat import QuickChatService, QuickChatError, default_next_settings
from .quick_chat_assistant import QuickChatAssistant, model_schema
from .quick_chat_titles import TITLE_MODEL
from .capabilities import capabilities
from .repository import request_hash
from .upload_route import QuickChatAssetUploadRoute
from .quick_chat_schema import (
    CreateCardRequest,
    ReviseCardRequest,
    safe_field_errors,
    TakePreferencesRequest,
    TakeStarRequest,
    TakeAgainRequest,
    TakeStoryRequest,
)
from .quick_chat_response_schema import AUTHORING_ERRORS, CardAuthoringResponse

PREFIX = "/v1/quick-chat/sessions"


class QuickChatAuthoringRoute(APIRoute):
    """Keep typed authoring errors inside the safe public service envelope."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                raise QuickChatError(
                    "invalid_request",
                    "请按字段诊断修正创作请求。",
                    422,
                    field_errors=safe_field_errors(error.errors()),
                ) from None
            except ValueError:
                # Existing asset/model-dependent checks remain authoritative.
                # Their arbitrary exception text is not a public field value.
                raise QuickChatError(
                    "invalid_request",
                    "请求未满足素材或生成参数契约。",
                    422,
                    field_errors=[{"path": ["body"], "reason": "invalid_value"}],
                ) from None

        return validated


def agent_contract():
    """Static same-origin authoring instructions; no credential or live-state read."""
    return {
        "schema_url": "/v1/quick-chat/schema",
        "sessions_url": PREFIX,
        "authentication": "Use an existing owner PAT in the Authorization: Bearer header. Never put it in URLs or prompts.",
        "new_session_scopes": ["projects:create", "projects:read", "projects:write"],
        "new_session_all_projects": True,
        "media_scopes": ["assets:read", "assets:write"],
        "generation_scopes": ["jobs:read", "jobs:write"],
        "generation_authority": "A card or preflight does not start generation. Confirm only within the user's authorization; an HTTP202 is not inference success.",
        "same_session": "Materials and next_settings are visible authoring state. Revisions freeze their own explicit inputs and seeds. Different sessions are isolated.",
        "history_titles": "Omit title when creating a session to allow optional Gemma 4 short history naming from the first meaningful accepted description. Any explicit title is manual, even the default label. Naming is independent of the conversational assistant and video engine; consult schema.history_titles.enabled. It sends only bounded first text, never media/full history, and failure keeps the original label without automatic retry.",
        "deployment_profiles": "New video requests use tier standard/refine/fast and mode fl/ref. Fast is currently unavailable. Public deployment_profile_id aliases are quality:standard/refine/fast; recipe aliases are video-fl-v1/video-ref-v1. Read capabilities.deployment_profiles and complete session next_settings, including the selected tier/mode's generation_support controls, input_support, limits, supported_constraints and execution_policy_constraints. Historical joint_cases are timing examples, not an admission allowlist; supported unmeasured combinations need no extra qualification confirmation. Turn-to-card inherits next_settings. Direct cards/revisions require their complete selection and controls; omission/null does not grant legacy routing to Agents. Session changes use PATCH with expected_version and complete next_settings. Preflight checks current matching workers; catalog measurements are historical. Preserve user choice/references; never switch to bypass refusal or rent a GPU. Conversation model selection is separate and unchanged.",
        "take_groups": {
            "preferences_url": "/v1/quick-chat/preferences",
            "preferences": "Owner CAS preferences default copies_max=4 and takes_fold_count=8. Both are editable positive integers; existing document byte and model limits still apply. Lowering the preference affects new revisions only, never historical reads or original submission replay.",
            "list": PREFIX
            + "/{session_id}/cards/{card_id}/takes?limit=20&before_cursor={cursor}",
            "session_list": PREFIX
            + "/{session_id}/takes?limit=40&before_cursor={cursor}",
            "session_list_contract": "Owner/session-scoped keyset pages of original take summaries, including card_id/revision_id/status/annotation and advertised thumbnail renditions. Includes trashed rows with explicit trash_state; creator grids hide them. total_count covers all original takes. No generation or story export is performed.",
            "labels": "group_number is a stable display ordinal across revisions; item.index and immutable hashes remain unchanged. Rows preserve original submission/job/execution actions and include full recipe and mutable annotation.",
            "again": {
                "method": "POST",
                "path": PREFIX + "/{session_id}/cards/{card_id}/again",
                "body": {
                    "expected_card_version": "{card.version}",
                    "source_revision_id": "{revision.id}",
                    "item_id": "{take.id}",
                    "copies": 1,
                },
            },
            "seeds": "A supplied fixed seed belongs only to the first new take; other takes receive distinct random seeds. Again creates a fresh revision in the original group and still requires the original paid preflight and confirmation. It is not execution retry.",
            "star": {
                "method": "PATCH",
                "path": PREFIX + "/{session_id}/takes/{item_id}",
                "body": {
                    "expected_version": "{annotation.version}",
                    "starred": "optional boolean",
                    "title": "optional display title; at least one field",
                },
            },
            "story": {
                "method": "POST",
                "path": PREFIX + "/{session_id}/takes/{item_id}/story",
                "body": {
                    "target_project_id": "{owned_project_id}",
                    "expected_project_version": "{project.version}",
                    "parent_id": "{scene.id}",
                },
                "status": PREFIX + "/{session_id}/story-transfers/{transfer_id}",
                "require": "Both source and destination scopes/ownership are required before copying original bytes. This creates an editable shot with complete roles/ranges/audio/profile/controls and selected seed lock; never enqueues generation. Preserve key/body for unknown status, read original operation, and only explicitly resume that same POST. A blocked receipt with no_shot_committed=true and target_version_conflict proves no shot was committed; retain it and copied assets, then only explicit user selection may create a new intent. Unknown storage remains on the original key. Never clean copied assets automatically.",
            },
            "queue": "/v1/generations/queue-position?job_id={owned_job_id}",
            "estimates": "Queue position is advisory original-pool fairness, never another owner's identity or an ETA. estimate.machine_seconds is the saved known planned machine allowance or null; cost readiness is the original preflight truth.",
        },
        "composer_reset": "Only durable accepted submission clears the submitted composer snapshot. Inspect composer_reset; newer selections, persistent settings, stored assets and earlier revisions remain. Rejections and unknown outcomes preserve the draft.",
        "creation_history": {
            "list": {
                "method": "GET",
                "path": PREFIX,
                "query": {
                    "view": "active|trash",
                    "hide_tests": "boolean",
                    "limit": "1..50",
                    "cursor": "{next_cursor}",
                },
            },
            "preview": {
                "method": "POST",
                "path": PREFIX + "/history/preview",
                "body": {
                    "action": "trash|restore|purge|empty_trash",
                    "sessions": [
                        {"id": "{session_id}", "expected_version": "{session.version}"}
                    ],
                },
            },
            "empty_trash_preview": {
                "method": "POST",
                "path": PREFIX + "/history/preview",
                "body": {"action": "empty_trash", "sessions": []},
                "require": "Review the returned complete target set. Do not send selected session IDs for empty_trash.",
            },
            "confirm": {
                "method": "POST",
                "path": PREFIX + "/history/actions",
                "body": {"preview_id": "{fresh_preview_id}", "confirmed": True},
                "require": "Read the exact target list, removed/kept counts, shared/chosen-shot warnings and eligibility before confirming. Retain the exact body and Idempotency-Key. A stale preview needs a fresh preview, never a force flag.",
            },
            "observe": {
                "method": "GET",
                "path": PREFIX + "/history/actions/{action_id}",
            },
            "retention": "Trash hides a session and retains content without automatic expiry. Restore is unavailable once irreversible purge intent exists. Curated older acceptance records marked review_hold are not automatically purged; explicit purge preview and confirmation are required. Other restorable trash remains a reference.",
            "guards": "Queued, running, collecting, uncertain admission/collection and unfinished authoring operations block deletion. Shared assets and storyboard-chosen versions stay. Job/attempt identities, receipts and billing are retained; deletion does not cancel generation or imply a refund.",
            "unknown": "A pending/unknown action is not a completed deletion. Poll its original action_id or replay its original identical confirmation key/body; never issue a replacement destructive action. Permanent deletion may finish over several maintenance passes.",
            "test_records": "Create acceptance sessions with is_test=true. After verifying results, PATCH the same session with expected_version and test_accepted=true to request safe archival. Active/uncertain work still blocks archival. hide_tests affects only the history view; it does not delete anything.",
            "scopes": {
                "list": ["projects:read"],
                "trash_restore": ["projects:read", "projects:write"],
                "purge": [
                    "projects:read",
                    "projects:write",
                    "assets:read",
                    "assets:write",
                    "jobs:read",
                    "jobs:write",
                ],
            },
            "cleanup_consent": "Automatic purge is disabled, including for old consented deadlines. Public purge_at is null and auto_purge_enabled is false. Stored old deadlines remain audit metadata. Only explicit authorized purge/empty_trash confirmation creates new deletion intent; original durable intents still reconcile.",
            "visibility": "Reference checks cover saved server records, not unsaved browser drafts. Empty-trash preview captures the complete authorized trash set, including review-held entries, and confirmation rechecks it.",
        },
        "card_revisions": "Read the current card and its current_revision_id revision before editing. Use expected_card_version=card.version, not session expected_version. Copy known tier/mode (or consistent neutral aliases), controls, inputs and copies; unknown historical quality needs explicit new selection. source_revision_id is optional same-session provenance, not card selection, concurrency or input inheritance. Omit it when no lineage is wanted. title is optional and defaults to 我的视频; copy it explicitly to preserve the prior title. Revisions preserve old snapshots/submissions and do not generate. Missing/invalid fields return HTTP422 invalid_request with safe field_errors; only a stale card version returns HTTP409 version_conflict. Reconcile stale edits and use a new key for changed bodies; uncertain writes retain their original identical body/key.",
        "replay": "Persist each write body and Idempotency-Key. Replaying the same revision's initial confirmation across Agents returns its original submission/items/jobs, not new variations.",
        "job_changes": {
            "path": "/v1/job-events",
            "scope": "jobs:read",
            "limit": "1..100 (default 100)",
            "cursor": "Persist next_cursor after processing every returned event. Drain has_more pages, then wait at least poll_after_seconds. An empty page retains the previous cursor. Cursors authorize only the current owner and this PAT's allowed projects; HTTP410 requires status resynchronization with GET /v1/jobs, not a new generation.",
            "history": "Only events emitted after activation are available. Earlier job history remains in GET /v1/jobs and submission/execution reads; no historical times are invented. Event status/advice is a historical snapshot; refresh the original job and submission before any retry.",
            "fields": ["cancelled_by", "reason_code", "message", "retry_advice"],
            "retry_advice": "safe_to_retry is explicit; charged and upstream_still_running are bool or null when unknown. A terminal status, accepted cancellation, empty provider list or absent worker alone does not prove safe/free retry. Safe retry still requires current preflight, permission, user generation authorization, budget and explicit confirmation.",
            "resume_admission": {
                "observe": PREFIX + "/{session_id}/submissions/{submission_id}",
                "revision": PREFIX + "/{session_id}/revisions/{revision_id}",
                "preflight": PREFIX
                + "/{session_id}/revisions/{revision_id}/preflights",
                "preflight_body": {
                    "revision_hash": "{original_revision.input_hash}",
                    "capabilities_version": "{current_capabilities_version}",
                    "item_ids": ["{original_resumable_item_id}"],
                },
                "observe_preflight": PREFIX + "/{session_id}/preflights/{preflight_id}",
                "method": "POST",
                "path": PREFIX
                + "/{session_id}/submissions/{submission_id}/resume-admission",
                "body": {
                    "item_ids": ["{original_resumable_item_id}"],
                    "fresh_preflight_id": "{ready_selected_item_preflight_id}",
                    "confirmed": True,
                },
                "require": "GET the original submission first; select only its items with resume_admission_available=true. Read submission.revision_id and that immutable revision's input_hash; refresh capabilities and preflight the exact selected item IDs on that same revision, without retry_of_execution_id for never-admitted items. Inspect the ready preflight and current spending/permission requirements. After the owner resolves the blocker and within explicit generation authorization, confirm the selected original items. Never raise caps or approve spending as the Agent. This resumes admission of original planned/blocked work; it is not a new submission, new take or execution retry. Existing job/execution/attempt identities and accounting remain authoritative; missing jobs are not proof of a free retry.",
                "recovery": "Persist the exact preflight body/key and the separate resume body/Idempotency-Key before each write. GET the original preflight/submission after response loss; only explicitly replay the original identical POST/body/key when reconciliation is needed. Replaying initial submission confirmation only recovers its receipt, not a fresh admission attempt. Do not create another card, submission or retry to escape a cap, unknown outcome or stale plan; unknown/recovery_hold stays blocked.",
            },
            "retry": {
                "preflight": PREFIX
                + "/{session_id}/revisions/{revision_id}/preflights",
                "preflight_body": {
                    "revision_hash": "{original_revision.input_hash}",
                    "capabilities_version": "{current_capabilities_version}",
                    "item_ids": ["{item_id}"],
                    "retry_of_execution_id": "{original_execution_id}",
                },
                "method": "POST",
                "path": PREFIX
                + "/{session_id}/submissions/{submission_id}/items/{item_id}/retry",
                "body": {
                    "retry_of_execution_id": "{original_execution_id}",
                    "fresh_preflight_id": "{ready_retry_preflight_id}",
                    "confirmed": True,
                },
                "require": "Read items[].retryable and latest job.retry_advice.safe_to_retry first. Reuse the original immutable revision, prompt, input assets, controls and seed; submit one explicit linked retry with one durable key/body. Replaying initial confirmation returns the original cancelled job. Do not duplicate cards or auto-loop retries; preserve original execution, cancellation and accounting.",
            },
        },
        "examples": {
            "session": {
                "method": "POST",
                "path": PREFIX,
                "body": {},
                "require": "Omit title for optional automatic history naming. Supply title only when intentionally choosing a manual name.",
            },
            "upload": {
                "method": "POST",
                "path": PREFIX + "/{session_id}/assets",
                "multipart": {
                    "client_asset_id": "stable-input-001",
                    "file": "{local_file}",
                },
                "require": "Use returned ready asset_id. A lost response retains client_asset_id; query this session's assets and resume the same receipt.",
            },
            "turn_to_card": {
                "method": "POST",
                "path": PREFIX + "/{session_id}/turns",
                "body": {
                    "expected_version": "{current_session_version}",
                    "model_id": "{session_model_id}",
                    "assistant_mode": "none",
                    "create_card": True,
                    "text": "{complete_prompt}",
                },
                "require": "For external Agents writing their own prompt; no website conversational assistant call. Optional Gemma history naming can still run once for an eligible new session. Inherits the session's complete next_settings including deployment_profile_id and selected materials. Read card_id then the card's current_revision_id. Model selection here does not select or call a video engine.",
            },
            "card": {
                "method": "POST",
                "path": PREFIX + "/{session_id}/cards",
                "body": {
                    "tier": "standard",
                    "mode": "fl",
                    "prompt": "{complete_prompt}",
                    "controls": "{selected_controls_object}",
                    "inputs": {},
                    "copies": 1,
                },
                "require": "Alternative to turn_to_card: supply explicit tier/mode plus controls and inputs as JSON objects, copies as integer. Copy the chosen quality from next_settings or the current revision; do not infer missing historical quality. Read actual selected tier/mode input_support and policy limits before preflight. No generation or capacity authority is granted by authoring.",
            },
            "read_card": {
                "method": "GET",
                "path": PREFIX + "/{session_id}/cards/{card_id}",
                "read_response": "Save this card's version and current_revision_id. It is a card version, not a session or revision counter.",
            },
            "card_history": {
                "method": "GET",
                "path": PREFIX
                + "/{session_id}/cards?limit=10&before_cursor={next_cursor}",
                "read_response": "Omit before_cursor for the latest cards. Each page is chronological by immutable creation time and ID; next_cursor reads the preceding page. has_more=false and start_reached=true mean the beginning. Runtime event polling remains on timeline.",
            },
            "read_revision": {
                "method": "GET",
                "path": PREFIX + "/{session_id}/revisions/{current_revision_id}",
                "read_response": "Read the full immutable snapshot whose fields will be preserved in the new revision.",
            },
            "revision": {
                "method": "POST",
                "path": PREFIX + "/{session_id}/cards/{card_id}/revisions",
                "body": {
                    "expected_card_version": "{current_card_version}",
                    "tier": "{current_revision.tier}",
                    "mode": "{current_revision.mode}",
                    "prompt": "{new_complete_prompt}",
                    "controls": "{current_revision.controls}",
                    "inputs": "{current_revision.inputs}",
                    "copies": "{current_revision.copies}",
                    "title": "{current_revision.title}",
                    "source_revision_id": "{current_revision.id}",
                },
                "optional_fields": [
                    "deployment_profile_id",
                    "title",
                    "turn_id",
                    "source_revision_id",
                ],
                "require": "Use integer expected_card_version=card.version and preserve controls/inputs JSON objects and copies integer. This is a complete snapshot, not a PATCH. Supply known tier/mode or consistent neutral aliases; unknown historical quality needs explicit user choice. title/source_revision_id are optional; title omission uses the default label, source_revision_id is same-session provenance only. Save body/key before POST. No GPU job is submitted. Read the returned card/revision and preflight/confirm separately only within generation authority; uncertain writes keep the exact original body/key.",
            },
            "preflight": {
                "method": "POST",
                "path": PREFIX + "/{session_id}/revisions/{revision_id}/preflights",
                "body": {
                    "revision_hash": "{revision.input_hash}",
                    "capabilities_version": "{current_capabilities_version}",
                },
            },
            "confirm": {
                "method": "POST",
                "path": PREFIX + "/{session_id}/revisions/{revision_id}/submissions",
                "body": {
                    "revision_hash": "{revision.input_hash}",
                    "preflight_id": "{ready_preflight_id}",
                    "confirmed": True,
                },
            },
            "observe": {
                "method": "GET",
                "path": PREFIX + "/{session_id}/submissions/{submission_id}",
                "read_response": "items[].job_id and items[].job are the existing shared jobs. Download returned artifacts[].content_url/download_url with the same owner authentication; verify size_bytes and sha256.",
            },
        },
        "placeholder_rules": "Replace placeholders with returned IDs/values, retaining their JSON types, and use a stable Idempotency-Key for every POST. Session expected_version and revision expected_card_version are distinct positive integers. Never edit hidden project/shot projections or submit them through legacy routes.",
        "release_notice": "This is the backend contract. Quick Chat frontend integration and public onboarding have separate release gates; discovery does not prove a deployed UI, enabled conversational assistant or GPU readiness.",
    }


def register_routes(
    app, *, hooks=None, assistant=None, assistant_enabled=False, title_generator=None
):
    settings = app.state.settings
    service = QuickChatService(
        app.state.repository,
        app.state.assets,
        settings,
        app.state.storage,
        hooks=hooks,
        assistant=assistant or QuickChatAssistant(app.state.assets, app.state.storage),
        assistant_enabled=assistant_enabled,
        title_generator=title_generator,
    )
    app.state.quick_chat = service

    @app.exception_handler(QuickChatError)
    async def error(_request, value):
        from .quality_http import error_response

        body = {
            "code": value.code,
            "message": value.message,
            "retryable": value.retryable,
        }
        if value.field_errors:
            body["field_errors"] = value.field_errors
        return error_response(_request, app, body, status_code=value.status)

    @app.get("/v1/quick-chat/preferences")
    def take_preferences(request: Request):
        return service.preferences(request.state.principal)

    def update_take_preferences(
        request: Request,
        body: TakePreferencesRequest,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        value = body.model_dump(exclude_unset=True)
        try:
            return service.set_preferences(request.state.principal, value, key)
        except QuickChatError as exc:
            # This service CAS is checked under the owner lock before any write.
            if exc.code != "version_conflict":
                raise
            return JSONResponse(
                {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "resource": "quick_chat_preferences",
                    "no_mutation_committed": True,
                    "request_hash": request_hash(value),
                },
                status_code=exc.status,
            )

    app.router.add_api_route(
        "/v1/quick-chat/preferences",
        update_take_preferences,
        methods=["PATCH"],
        status_code=200,
        route_class_override=QuickChatAuthoringRoute,
        responses=AUTHORING_ERRORS,
    )

    @app.get("/v1/generations/queue-position")
    def queue_position(request: Request, job_id: str):
        return service.queue_position(request.state.principal, job_id)

    @app.get(PREFIX + "/{session_id}/cards/{card_id}/takes")
    def takes(
        session_id: str,
        card_id: str,
        request: Request,
        limit: int = 20,
        before_cursor: str | None = None,
    ):
        return service.group_takes(
            request.state.principal,
            session_id,
            card_id,
            limit=limit,
            before_cursor=before_cursor,
        )

    @app.get(PREFIX + "/{session_id}/takes")
    def session_takes(
        session_id: str,
        request: Request,
        limit: int = 40,
        before_cursor: str | None = None,
    ):
        return service.session_takes(
            request.state.principal,
            session_id,
            limit=limit,
            before_cursor=before_cursor,
        )

    def star(
        session_id: str,
        item_id: str,
        request: Request,
        body: TakeStarRequest,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        payload = body.model_dump(exclude_unset=True)
        try:
            return service.star_take(
                request.state.principal, session_id, item_id, payload, key
            )
        except QuickChatError as exc:
            if exc.code == "version_conflict":
                return JSONResponse(
                    {
                        "code": exc.code,
                        "resource": "quick_chat_take",
                        "session_id": session_id,
                        "take_id": item_id,
                        "request_hash": request_hash(payload),
                        "no_mutation_committed": True,
                        "message": "Annotation changed; reload before editing. / 标注已更改，请刷新后编辑。",
                    },
                    status_code=409,
                )
            raise

    app.router.add_api_route(
        PREFIX + "/{session_id}/takes/{item_id}",
        star,
        methods=["PATCH"],
        status_code=200,
        route_class_override=QuickChatAuthoringRoute,
        responses=AUTHORING_ERRORS,
    )

    def again(
        session_id: str,
        card_id: str,
        request: Request,
        body: TakeAgainRequest,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.again(
            request.state.principal,
            session_id,
            card_id,
            body.model_dump(exclude_unset=True),
            key,
        )

    app.router.add_api_route(
        PREFIX + "/{session_id}/cards/{card_id}/again",
        again,
        methods=["POST"],
        status_code=201,
        route_class_override=QuickChatAuthoringRoute,
        responses=AUTHORING_ERRORS,
    )

    def to_story(
        session_id: str,
        item_id: str,
        request: Request,
        body: TakeStoryRequest,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.transfer_take(
            request.state.principal,
            session_id,
            item_id,
            body.model_dump(exclude_unset=True),
            key,
        )

    app.router.add_api_route(
        PREFIX + "/{session_id}/takes/{item_id}/story",
        to_story,
        methods=["POST"],
        status_code=201,
        route_class_override=QuickChatAuthoringRoute,
        responses=AUTHORING_ERRORS,
    )

    @app.get(PREFIX + "/{session_id}/story-transfers/{transfer_id}")
    def transfer_status(session_id: str, transfer_id: str, request: Request):
        return service.get_transfer(request.state.principal, session_id, transfer_id)

    @app.get("/v1/quick-chat/schema")
    def schema():
        return {
            "version": 1,
            "agent_contract": agent_contract(),
            "models": model_schema(enabled=assistant_enabled),
            "capabilities": capabilities(settings),
            "assistant_enabled": assistant_enabled,
            "copies": {
                "minimum": 1,
                "maximum": 2147483647,
                "default_maximum": 4,
                "preference_url": "/v1/quick-chat/preferences",
            },
            "history_titles": {
                "enabled": title_generator is not None,
                "model_id": TITLE_MODEL,
                "input": "first meaningful accepted turn or card prompt; at most 2000 characters; no media or full history",
                "timing": "background after authoring response",
                "maximum_calls_per_session": 1,
                "manual_title_preserved": True,
                "authoring_version_bump": False,
                "failure": "keep the original title; no automatic paid retry",
                "existing_sessions_backfilled": False,
            },
            "default_next_settings": default_next_settings(settings),
            "card_authoring": {
                "create_request": CreateCardRequest.model_json_schema(),
                "revision_request": ReviseCardRequest.model_json_schema(),
                "response": CardAuthoringResponse.model_json_schema(),
                "optimistic_concurrency": {
                    "revision_field": "expected_card_version",
                    "read_from": "GET card response.version",
                    "session_field": "expected_version",
                },
                "field_errors": {
                    "path": "known request field path; unknown names collapse to their safe parent",
                    "reasons": [
                        "missing_field",
                        "type_mismatch",
                        "unknown_field",
                        "invalid_value",
                        "stale_version",
                    ],
                    "includes_submitted_values": False,
                },
            },
            "turn_creation": {
                "create_card": {
                    "type": "boolean",
                    "default": False,
                    "allowed_assistant_modes": ["none"],
                    "atomic_with_turn": True,
                    "starts_generation": False,
                    "result_field": "card_id",
                }
            },
            "material_participation": {
                "basis": "next_settings.recipe_id",
                "catalog_selection": "enabled",
                "actual_participation": "effective_enabled",
                "inactive_reasons": ["mode_incompatible", "not_selected"],
            },
            "timeline": {
                "events": "creation_commands_only",
                "execution_polling": "active_submissions",
            },
        }

    @app.get(PREFIX)
    def sessions(
        request: Request,
        limit: int = Query(20, ge=1, le=50),
        cursor: str | None = None,
        view: str = Query("active", pattern="^(active|trash)$"),
        hide_tests: bool = False,
    ):
        return service.list_sessions(
            request.state.principal,
            limit=limit,
            cursor=cursor,
            view=view,
            hide_tests=hide_tests,
        )

    @app.post(PREFIX + "/history/preview")
    def history_preview(request: Request, body: dict):
        return service.preview_history(request.state.principal, body)

    @app.post(PREFIX + "/history/actions")
    def history_action(
        request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")
    ):
        try:
            value = service.apply_history(request.state.principal, body, key)
        except QuickChatError as exc:
            # These failures occur after original-key replay lookup, before any
            # action write, and leave the original transaction rolled back.
            if exc.code in {
                "history_preview_expired",
                "history_preview_changed",
                "history_action_blocked",
            }:
                return JSONResponse(
                    {
                        "detail": {
                            "code": exc.code,
                            "resource": "quick_chat_history",
                            "action": "confirm",
                            "request_hash": request_hash(body),
                            "no_mutation_committed": True,
                            "message": "No history action committed; request a fresh preview. / 未提交回收操作，请重新预览。",
                        }
                    },
                    status_code=409,
                )
            raise
        return JSONResponse(
            value, status_code=202 if value["state"] == "pending" else 200
        )

    @app.get(PREFIX + "/history/actions/{action_id}")
    def history_action_status(action_id: str, request: Request):
        return service.get_history_action(request.state.principal, action_id)

    @app.post(PREFIX, status_code=201)
    def create_session(
        request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")
    ):
        return service.create_session(request.state.principal, body, key)

    @app.get(PREFIX + "/{session_id}")
    def session(session_id: str, request: Request):
        return service.get_session(request.state.principal, session_id)

    @app.patch(PREFIX + "/{session_id}")
    def patch(
        session_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.patch_session(request.state.principal, session_id, body, key)

    def upload(
        session_id: str,
        request: Request,
        file: UploadFile = File(...),
        client_asset_id: str = Form(...),
    ):
        return service.upload(
            request.state.principal,
            session_id,
            file.file,
            file.filename or "file",
            client_asset_id,
        )

    app.router.add_api_route(
        PREFIX + "/{session_id}/assets",
        upload,
        methods=["POST"],
        status_code=201,
        route_class_override=QuickChatAssetUploadRoute,
    )

    @app.get(PREFIX + "/{session_id}/assets")
    def asset_catalog(
        session_id: str,
        request: Request,
        client_asset_id: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0, le=500),
    ):
        session = service._access(request.state.principal, session_id, "assets:read")
        values = app.state.assets.list(
            request.state.principal.owner, session["payload"]["project_id"]
        )
        if client_asset_id is not None:
            values = [v for v in values if v.get("client_asset_id") == client_asset_id]
        return {
            "assets": values[offset : offset + limit],
            "session_id": session_id,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < len(values),
        }

    @app.post(PREFIX + "/{session_id}/assets/{asset_id}/resume")
    def resume_asset(session_id: str, asset_id: str, request: Request):
        session = service._access(request.state.principal, session_id, "assets:write")
        app.state.assets.get(
            request.state.principal.owner, asset_id, session["payload"]["project_id"]
        )
        return app.state.assets.resume(request.state.principal.owner, asset_id)

    @app.get(PREFIX + "/{session_id}/materials")
    def materials(session_id: str, request: Request):
        return service.materials(request.state.principal, session_id)

    @app.put(PREFIX + "/{session_id}/materials")
    def set_materials(
        session_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.put_materials(request.state.principal, session_id, body, key)

    @app.get(PREFIX + "/{session_id}/timeline")
    def timeline(
        session_id: str,
        request: Request,
        limit: int = Query(20, ge=1, le=50),
        cursor: str | None = None,
        direction: str = "older",
        before_cursor: str | None = None,
        after_cursor: str | None = None,
    ):
        if sum(v is not None for v in (cursor, before_cursor, after_cursor)) > 1:
            raise QuickChatError("invalid_cursor", "只能指定一个分页位置。", 422)
        return service.timeline(
            request.state.principal,
            session_id,
            limit=limit,
            cursor=after_cursor or before_cursor or cursor,
            direction="newer"
            if after_cursor
            else "older"
            if before_cursor
            else direction,
        )

    @app.post(PREFIX + "/{session_id}/turns", status_code=201)
    def create_turn(
        session_id: str,
        request: Request,
        body: dict,
        background_tasks: BackgroundTasks,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        value = service.create_turn(request.state.principal, session_id, body, key)
        background_tasks.add_task(
            service.generate_title, request.state.principal, session_id
        )
        return value

    @app.get(PREFIX + "/{session_id}/turns/{turn_id}")
    def turn(session_id: str, turn_id: str, request: Request):
        return service.get_turn(request.state.principal, session_id, turn_id)

    @app.post(PREFIX + "/{session_id}/turns/{turn_id}/acknowledge-unknown")
    def acknowledge(
        session_id: str,
        turn_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.acknowledge_unknown(
            request.state.principal, session_id, turn_id, body, key
        )

    def create_card(
        session_id: str,
        request: Request,
        body: CreateCardRequest,
        background_tasks: BackgroundTasks,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        value = service.save_card(
            request.state.principal,
            session_id,
            body.model_dump(exclude_unset=True),
            key,
        )
        background_tasks.add_task(
            service.generate_title, request.state.principal, session_id
        )
        return value

    app.router.add_api_route(
        PREFIX + "/{session_id}/cards",
        create_card,
        methods=["POST"],
        status_code=201,
        response_model=CardAuthoringResponse,
        response_model_exclude_unset=True,
        responses=AUTHORING_ERRORS,
        route_class_override=QuickChatAuthoringRoute,
    )

    @app.get(PREFIX + "/{session_id}/cards")
    def card_history(
        session_id: str,
        request: Request,
        limit: int = Query(10, ge=1, le=50),
        before_cursor: str | None = None,
    ):
        return service.list_cards(
            request.state.principal,
            session_id,
            limit=limit,
            before_cursor=before_cursor,
        )

    @app.get(PREFIX + "/{session_id}/cards/{card_id}")
    def card(session_id: str, card_id: str, request: Request):
        return service.get_card(request.state.principal, session_id, card_id)

    def revision(
        session_id: str,
        card_id: str,
        request: Request,
        body: ReviseCardRequest,
        background_tasks: BackgroundTasks,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        value = service.save_card(
            request.state.principal,
            session_id,
            body.model_dump(exclude_unset=True),
            key,
            card_id=card_id,
        )
        background_tasks.add_task(
            service.generate_title, request.state.principal, session_id
        )
        return value

    app.router.add_api_route(
        PREFIX + "/{session_id}/cards/{card_id}/revisions",
        revision,
        methods=["POST"],
        status_code=201,
        response_model=CardAuthoringResponse,
        response_model_exclude_unset=True,
        responses=AUTHORING_ERRORS,
        route_class_override=QuickChatAuthoringRoute,
    )

    @app.get(PREFIX + "/{session_id}/revisions/{revision_id}")
    def get_revision(session_id: str, revision_id: str, request: Request):
        return service.get_revision(request.state.principal, session_id, revision_id)

    @app.post(
        PREFIX + "/{session_id}/revisions/{revision_id}/preflights", status_code=201
    )
    def preflight(
        session_id: str,
        revision_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.preflight(
            request.state.principal, session_id, revision_id, body, key
        )

    @app.get(PREFIX + "/{session_id}/preflights/{preflight_id}")
    def get_preflight(session_id: str, preflight_id: str, request: Request):
        return service.get_preflight(request.state.principal, session_id, preflight_id)

    @app.post(
        PREFIX + "/{session_id}/revisions/{revision_id}/submissions", status_code=202
    )
    def submit(
        session_id: str,
        revision_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.submit(
            request.state.principal, session_id, revision_id, body, key
        )

    @app.get(PREFIX + "/{session_id}/submissions/{submission_id}")
    def submission(session_id: str, submission_id: str, request: Request):
        return service.get_submission(
            request.state.principal, session_id, submission_id
        )

    @app.post(PREFIX + "/{session_id}/submissions/{submission_id}/cancel")
    def cancel(
        session_id: str,
        submission_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.cancel(
            request.state.principal, session_id, submission_id, body, key
        )

    @app.post(PREFIX + "/{session_id}/submissions/{submission_id}/resume-admission")
    def resume(
        session_id: str,
        submission_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.resume_admission(
            request.state.principal, session_id, submission_id, body, key
        )

    @app.post(
        PREFIX + "/{session_id}/submissions/{submission_id}/items/{item_id}/retry",
        status_code=202,
    )
    def retry(
        session_id: str,
        submission_id: str,
        item_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.retry(
            request.state.principal, session_id, submission_id, item_id, body, key
        )

    @app.post(PREFIX + "/{session_id}/result-imports", status_code=201)
    def result_import(
        session_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.result_import(request.state.principal, session_id, body, key)

    @app.get(PREFIX + "/{session_id}/result-imports/{import_id}")
    def get_import(session_id: str, import_id: str, request: Request):
        return service.get_result_import(request.state.principal, session_id, import_id)

    return service
