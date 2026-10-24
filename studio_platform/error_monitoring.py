"""Opt-in API errors with a content-free, closed outbound event shape.

Never initialize from worker imports. DSN and target come from the reviewed app
configuration; local/unset targets cannot send. Exception messages, request and
scope data are deliberately discarded, rather than filtered by sensitive keys.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager


DSN = (
    "https://7339f473c737f7b493b03da92a2bb925@"
    "o4512306171019264.ingest.us.sentry.io/4512306334728192"
)
_FRAME = re.compile(
    r"(?:[A-Za-z]:)?(?:[^\n\r]*/)?(studio_platform/[a-z_][a-z0-9_/]*\.py)\Z"
)
_ERROR_TYPES = frozenset(
    {
        "Exception",
        "ExceptionGroup",
        "BaseExceptionGroup",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "AssertionError",
        "LookupError",
        "IndexError",
        "NameError",
        "ImportError",
        "ModuleNotFoundError",
        "OSError",
        "TimeoutError",
        "ConnectionError",
        "MemoryError",
        "RecursionError",
        "ZeroDivisionError",
        "OverflowError",
        "UnicodeDecodeError",
        "JSONDecodeError",
        "HTTPException",
        "SQLAlchemyError",
        "StatementError",
        "IntegrityError",
        "OperationalError",
        "PermissionError",
        "FileNotFoundError",
    }
)


def valid_dsn(value):
    # A public DSN still selects an outbound destination. Do not accept an
    # arbitrary collector, URL credentials, query or lookalike host.
    return type(value) is str and value in {"", DSN}


def _frames(value):
    if type(value) is not dict or type(value.get("frames")) is not list:
        return []
    result = []
    for frame in value["frames"][-100:]:
        if type(frame) is not dict or type(frame.get("filename")) is not str:
            continue
        matched = _FRAME.fullmatch(frame["filename"].replace("\\", "/"))
        if matched is None or ".." in matched[1].split("/"):
            continue
        clean = {"filename": matched[1], "in_app": True}
        line = frame.get("lineno")
        if type(line) is int and 1 <= line <= 1_000_000:
            clean["lineno"] = line
        result.append(clean)
    return result


def sanitized_event(event, hint=None):
    """Rebuild errors, including nested exception groups, without free text.

    Never recurse/copy arbitrary nested input, call repr(), or trust hints.
    Non-exception events (including log messages/transactions) are dropped.
    """
    if type(hint) is dict:
        # Scope attachments are sent outside the event JSON by the SDK.
        hint.pop("attachments", None)
    if type(event) is dict and event.get("message") == "capacity_attempt_reclaimed":
        tags = event.get("tags")
        if (
            type(tags) is not dict
            or type(tags.get("reason")) is not str
            or tags.get("reason") not in {
                "capacity_no_progress", "capacity_runtime_exceeded", "capacity_owner_recovery"
            }
            or type(tags.get("reclaim_count")) is not str
            or tags.get("reclaim_count") not in {"0", "1", "2"}
            or not isinstance(event.get("event_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", event["event_id"]) is None
        ):
            return None
        clean = {
            "platform": "python", "level": "warning",
            "message": "capacity_attempt_reclaimed", "event_id": event["event_id"],
            "tags": {key: tags[key] for key in ("reason", "reclaim_count")},
        }
        if event.get("environment") in ("staging", "production"):
            clean["environment"] = event["environment"]
        return clean
    if type(event) is not dict or type(event.get("exception")) is not dict:
        return None
    values = event["exception"].get("values")
    if type(values) is not list:
        return None
    exceptions = []
    for value in values[-10:]:
        if type(value) is not dict:
            continue
        kind = value.get("type")
        clean = {
            "type": kind if type(kind) is str and kind in _ERROR_TYPES else "Error",
            "value": "[message omitted]",
        }
        frames = _frames(value.get("stacktrace"))
        if frames:
            clean["stacktrace"] = {"frames": frames}
        mechanism = value.get("mechanism")
        if type(mechanism) is dict and type(mechanism.get("handled")) is bool:
            clean["mechanism"] = {"type": "generic", "handled": mechanism["handled"]}
        exceptions.append(clean)
    if not exceptions:
        return None
    # Only code metadata survives. In particular there is no request URL,
    # transaction, user, contexts, extra, tags, logging message or breadcrumbs.
    clean = {
        "platform": "python",
        "level": "error",
        "exception": {"values": exceptions},
    }
    event_id = event.get("event_id")
    if type(event_id) is str and re.fullmatch(r"[0-9a-f]{32}", event_id):
        clean["event_id"] = event_id
    environment = event.get("environment")
    if environment in ("staging", "production"):
        clean["environment"] = environment
    return clean


def report_capacity_reclaim(client, receipt):
    """The only non-exception message; immutable ID dedupes at the collector."""
    import uuid

    return client.capture_event({
        "message": "capacity_attempt_reclaimed", "level": "warning",
        "event_id": uuid.UUID(receipt["event_id"]).hex,
        "tags": {"reason": receipt["reason"], "reclaim_count": str(receipt["count"])},
    })


def create_client(settings):
    """An app-owned client, never bound to the process-global SDK scope.

    Even disabled apps need an explicit inert client: NonRecordingClient is
    skipped by SDK get_client(), which would fall through to a foreign global
    client. Client(dsn="") instead masks that fallback without a transport.
    """
    enabled = bool(settings.sentry_dsn) and settings.sentry_environment in {
        "staging",
        "production",
    }
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    return sentry_sdk.Client(
        dsn=settings.sentry_dsn if enabled else "",
        environment=settings.sentry_environment if enabled else "disabled",
        # No reviewed runtime source-SHA field exists. Do not infer a release
        # from process environment/Git or label a mutable checkout as a release.
        release="",
        server_name="inkseq-api",
        send_default_pii=False,
        include_local_variables=False,
        include_source_context=False,
        max_request_body_size="never",
        attach_stacktrace=False,
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[StarletteIntegration(), FastApiIntegration()] if enabled else [],
        traces_sample_rate=0.0,
        propagate_traces=False,
        trace_propagation_targets=[],
        profiles_sample_rate=0.0,
        profile_session_sample_rate=0.0,
        auto_session_tracking=False,
        send_client_reports=False,
        enable_logs=False,
        enable_metrics=False,
        max_breadcrumbs=0,
        before_breadcrumb=lambda breadcrumb, hint: None,
        before_send=sanitized_event,
        before_send_transaction=lambda event, hint: None,
        before_send_log=lambda event, hint: None,
        before_send_metric=lambda event, hint: None,
    )


def monitored_app(settings, **kwargs):
    """Keep capture outside Starlette's globally patched call and app middleware.

    Both SDK scopes are forked/restored around every ASGI invocation, including
    lifespan and exceptions. Closing one app never closes or replaces another
    app's client, and a closed/disabled app always masks an inherited client.
    """
    import sentry_sdk
    from fastapi import FastAPI
    from types import SimpleNamespace

    client = create_client(settings)
    inert = (
        client
        if not client.dsn
        else create_client(SimpleNamespace(sentry_dsn="", sentry_environment=""))
    )
    original_lifespan = kwargs.pop("lifespan", None)

    class App(FastAPI):
        _error_monitoring_closed = False

        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan" and self._error_monitoring_closed:
                self.state.error_monitoring_client = create_client(settings)
                self._error_monitoring_closed = False
            # User middleware is too late: Starlette's patched __call__ reads
            # the client first. Current scope wins over isolation/global.
            with sentry_sdk.isolation_scope() as isolated:
                selected = (
                    inert
                    if self._error_monitoring_closed
                    else self.state.error_monitoring_client
                )
                isolated.set_client(selected)
                sentry_sdk.get_current_scope().set_client(selected)
                await super().__call__(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if original_lifespan is None:
                yield
            else:
                async with original_lifespan(app) as state:
                    yield state
        finally:
            app._error_monitoring_closed = True
            app.state.error_monitoring_client.close()

    app = App(lifespan=lifespan, **kwargs)
    app.state.error_monitoring_client = client
    return app
