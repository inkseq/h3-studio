"""Tier projections at explicit JSON HTTP boundaries, after normal validation.

Internal services retain their immutable recipes. This wrapper has the actual
authenticated principal, including for response-model serialization and replay
branches. It never consumes media/streaming responses or changes stored data.
"""

import json
from dataclasses import replace

from fastapi.routing import APIRoute, request_response
from starlette.responses import JSONResponse
from starlette.routing import Route


def _selected(path):
    return path in {
        "/v1/capabilities",
        "/v1/generation-availability",
        "/v1/guided-schema",
        "/v1/generation-plans",
        "/v1/render-plans",
        "/v1/generations/preflight",
        "/v1/generations",
        "/v1/generations/queue-position",
        "/v1/quick-chat/schema",
        "/v1/quick-chat/preferences",
        "/v1/storyboard/preferences",
        "/v1/job-events",
    } or path.startswith(
        (
            "/v1/quick-chat/sessions",
            "/v1/batches",
            "/v1/projects",
            "/v1/jobs",
            "/v1/artifacts/",
        )
    )


def principal_for(request, app):
    principal = getattr(request.state, "principal", None)
    if (
        principal is not None
        and not principal.machine
        and principal.identity_source == "legacy"
    ):
        # Auth.hydrate has already checked the identity schema/row for this
        # request. Only its attested pre-adoption result can use configured
        # legacy admins; canonical accounts use their freshly hydrated role.
        # Do not repeat the identity probes for every JSON serialization.
        principal = replace(
            principal,
            role="admin"
            if principal.owner in app.state.settings.operator_admin_owners
            else "user",
        )
    return principal


def error_response(request, app, value, *, status_code, headers=None):
    from .quality_views import error

    if _selected(request.url.path):
        value = error(value, principal_for(request, app))
    return JSONResponse(value, status_code=status_code, headers=headers)


def install(app):
    from . import quality_views

    for route in app.routes:
        if (
            isinstance(route, Route)
            and not isinstance(route, APIRoute)
            and route.path == "/openapi.json"
        ):
            # FastAPI registers its schema as a builtin Starlette Route. Read
            # the cached original without changing it; only this JSON response
            # is projected for the authenticated request's actual principal.
            async def openapi_response(request):
                return JSONResponse(
                    quality_views.schema(
                        app.openapi(),
                        principal_for(request, app),
                        settings=app.state.settings,
                    ),
                    headers={"Cache-Control": "no-store"},
                )

            route.app = request_response(openapi_response)
            continue
        if not isinstance(route, APIRoute) or not _selected(route.path):
            continue
        original = route.get_route_handler()

        def handler(original=original, path=route.path):
            async def project(request):
                response = await original(request)
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type != "application/json" or not hasattr(response, "body"):
                    return response
                principal = principal_for(request, app)
                value = json.loads(response.body)
                if path == "/v1/capabilities":
                    visible = quality_views.capabilities(
                        value, principal, settings=app.state.settings
                    )
                elif path == "/v1/generation-availability":
                    visible = quality_views.availability(
                        value, principal, settings=app.state.settings
                    )
                elif path in {"/v1/guided-schema", "/v1/quick-chat/schema"}:
                    visible = quality_views.schema(
                        value, principal, settings=app.state.settings
                    )
                else:
                    visible = quality_views.envelope(
                        value, principal, settings=app.state.settings
                    )
                headers = {
                    key: item
                    for key, item in response.headers.items()
                    if key.lower() not in {"content-length", "content-type"}
                }
                return JSONResponse(
                    visible,
                    status_code=response.status_code,
                    headers=headers,
                    background=response.background,
                )

            return project

        route.app = request_response(handler())
