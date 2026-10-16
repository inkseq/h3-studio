"""Explicit browser authorization and a single anonymous JSON exchange route."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .agent_connect import AgentConnect, ConnectError
from .auth import AuthenticationError, Principal
from .http_limits import AGENT_CONNECT_BODY_BYTES

EXCHANGE_PATH = "/v1/agent-connect/exchange"
HELPER_PATH = "/for-agents/connect.py"
MANIFEST_PATH = "/for-agents/connect-manifest.json"
PUBLIC_GET_PATHS = frozenset({HELPER_PATH, MANIFEST_PATH})
HELPER_SOURCE = Path(__file__).resolve().parent.parent / "skills" / "sixnine-yingxu" / "scripts" / "connect.py"
MAX_BODY_BYTES = AGENT_CONNECT_BODY_BYTES
CONNECTION_STORAGE_REQUIREMENTS = {
    "windows-user-dpapi": "Current Windows user DPAPI and access to that user's encrypted connection store.",
    "linux-secret-service": "Existing secret-tool and an accessible user Secret Service; no dependency is installed automatically.",
    "macos-user-keychain": "Native Security framework and one existing default file-based user Keychain, already unlocked and accessible to this process. Reads and writes bind to that same Keychain. Authentication UI is suppressed; locked, denied or interaction-required access fails closed. Any unlock/authorization is a separate action through trusted system UI; the helper never collects a Keychain password or changes access controls.",
}
CONNECTION_VERIFICATION_CHECKLIST = (
    "Inspect the helper and compare its SHA-256 with the manifest on the exact user-supplied origin; a match proves release consistency, not independent trust.",
    "Check the applicable OS store and calling process access before exchange; no automatic dependency installation, Keychain creation or plaintext fallback.",
    "Use the owner's explicit grant and five-minute code through the hidden prompt or stdin, never a permanent key or code in command arguments.",
    "After connecting, use whoami to confirm the expected account and exact origin; recover a lost response only with the same saved token/verifier and connection ID.",
    "Verify owner revocation separately without deleting drafts, accepted jobs or assets; connection does not authorize a particular paid task.",
)
CONNECTION_VERIFICATION_LIMITS = "These are implementation requirements and verification steps, not per-machine test results. Fake OS tests do not prove native store access or live external onboarding; actual OS access and frontend/production publication require separate evidence."


def helper_manifest(raw=None):
    import hashlib
    raw = HELPER_SOURCE.read_bytes() if raw is None else raw
    return {"version": 1, "helper": HELPER_PATH, "sha256": hashlib.sha256(raw).hexdigest(),
        "protocol": "client-held-pat-v1", "credential_storage": list(CONNECTION_STORAGE_REQUIREMENTS),
        "missing_storage_behavior": "stop_before_exchange", "manual_pat": "advanced_fallback",
        "exchange": EXCHANGE_PATH, "raw_api_key_response": False, "discovery_is_authorization": False,
        "credential_storage_requirements": dict(CONNECTION_STORAGE_REQUIREMENTS),
        "verification_checklist": list(CONNECTION_VERIFICATION_CHECKLIST),
        "verification_limits": CONNECTION_VERIFICATION_LIMITS}


def register_routes(app, *, service=None):
    service = service or AgentConnect(app.state.auth)
    app.state.agent_connect = service
    settings = app.state.settings

    def origin(request):
        return service.origin(settings.public_origin_for_host(request.headers.get("host", ""))
                              or settings.public_origin or str(request.base_url).rstrip("/"))

    def source_guard(request, *, browser=False):
        expected = origin(request)
        permitted = {expected, *settings.local_ui_origins}
        provided = request.headers.get("origin")
        if provided is not None and provided not in permitted:
            raise ConnectError("connection_cross_site_rejected", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ConnectError("connection_cross_site_rejected", 403)
        if browser and not provided:
            # Browsers always supply Origin on these credential-issuing writes.
            # Keep their proof stronger than the existing legacy API fallback.
            raise ConnectError("connection_origin_required", 403)
        return expected

    def browser(request):
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            raise ConnectError("connection_login_required", 401)
        if principal.machine:
            raise ConnectError("connection_browser_required", 403)
        return principal

    async def body(request, *, allowed, required=()):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ConnectError("connection_json_required", 415)
        data = await request.body()
        if len(data) > MAX_BODY_BYTES:
            raise ConnectError("connection_body_too_large", 413)
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise ConnectError("connection_body_invalid", 422) from None
        if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
            raise ConnectError("connection_fields_invalid", 422)
        return value

    @app.exception_handler(ConnectError)
    async def connection_error(_request, error):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if error.retry_after:
            headers["Retry-After"] = str(error.retry_after)
        return JSONResponse({"code": error.code, "message": error.code,
            "retryable": error.status_code in {429, 503}}, status_code=error.status_code, headers=headers)

    @app.post("/v1/account/agent-connections", status_code=201)
    async def issue(request: Request):
        source_guard(request, browser=True)
        principal = browser(request)
        value = await body(request, allowed={"name", "authorization_profile_id", "authorization_profile_version", "daily_limit_microusd"},
            required={"authorization_profile_id", "authorization_profile_version"})
        try:
            return await run_in_threadpool(service.issue, principal, name=value.get("name", "Codex"),
                authorization_profile_id=value["authorization_profile_id"],
                authorization_profile_version=value["authorization_profile_version"],
                idempotency_key=request.headers.get("idempotency-key"), origin=origin(request),
                daily_limit_microusd=value.get("daily_limit_microusd", 0))
        except AuthenticationError:
            raise ConnectError("connection_login_required", 401) from None

    @app.get("/v1/account/agent-connections")
    def list_connections(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        try:
            return service.list(browser(request), limit=limit, offset=offset)
        except AuthenticationError:
            raise ConnectError("connection_login_required", 401) from None

    @app.get("/v1/account/agent-connections/{connection_id}")
    def get_connection(connection_id: str, request: Request):
        try:
            return service.get(browser(request), connection_id)
        except AuthenticationError:
            raise ConnectError("connection_login_required", 401) from None

    @app.delete("/v1/account/agent-connections/{connection_id}")
    def revoke(connection_id: str, request: Request):
        source_guard(request, browser=True)
        try:
            return service.revoke(browser(request), connection_id)
        except AuthenticationError:
            raise ConnectError("connection_login_required", 401) from None

    @app.post(EXCHANGE_PATH)
    async def exchange(request: Request):
        expected_origin = source_guard(request)
        value = await body(request, allowed={"code", "client_challenge", "token_hash", "key_prefix",
            "expected_authorization_fingerprint", "recovery_verifier"},
            required={"code", "client_challenge", "token_hash", "key_prefix", "expected_authorization_fingerprint"})
        # ASGI client address is supplied by the configured trusted proxy layer.
        # Never directly trust a request's X-Forwarded-For string here.
        return await run_in_threadpool(service.exchange, **value, origin=expected_origin,
            source=request.client.host if request.client else "unknown")
