"""Cookie-only operator endpoints. Shared application middleware owns CSRF."""

import json
from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse

from .operator_capacity import (
    OperatorCapacity,
    OperatorError,
    OperatorRegistry,
    selection,
)
from . import capacity_market  # Register additive observation table before startup DDL.
from .capacity_candidates import candidates_projection

PREFIX = "/v1/operator/capacity"


def register_routes(app, *, registry=None, service=None):
    service = service or OperatorCapacity(
        app.state.repository,
        app.state.settings,
        registry
        if registry is not None
        else OperatorRegistry.from_environment(repository=app.state.repository),
    )
    app.state.operator_capacity = service

    def principal(request):
        return getattr(request.state, "principal", None)

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.exception_handler(OperatorError)
    async def operator_error(_request, error):
        return JSONResponse(
            {"code": error.code, "message": error.code},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.get(PREFIX + "/state")
    def state(request: Request):
        return response(service.state(principal(request)))

    @app.get(PREFIX + "/catalog")
    def catalog(request: Request):
        service.authorize(principal(request))
        return response(service.registry.catalog())

    @app.get(PREFIX + "/offers")
    def offers(
        request: Request,
        runtime_profile_id: str,
        gpu_type: str,
        mode: str,
        gpu_count: int = Query(1, ge=1, le=8),
        provider: str = "lium",
        node_count: int = Query(1, ge=1, le=32),
        ttl_seconds: int = Query(120, ge=120, le=14400),
        filters: str = Query("{}", max_length=4096),
    ):
        service.authorize(principal(request))
        try:
            parsed_filters = json.loads(filters)
        except ValueError:
            raise OperatorError("operator_filters_invalid", 422) from None
        chosen = selection(
            {
                "runtime_profile_id": runtime_profile_id,
                "gpu_type": gpu_type,
                "node_count": node_count,
                "gpu_count": gpu_count,
                "mode": mode,
                "ttl_seconds": ttl_seconds,
                "filters": parsed_filters,
                "provider": provider,
            }
        )
        return response(service.offers(principal(request), chosen))

    @app.get(PREFIX + "/candidates")
    def candidates(
        request: Request,
        model_id: str = Query(..., min_length=1, max_length=200),
        mode: str = Query(...),
        ttl_seconds: int = Query(120, ge=120, le=14400),
    ):
        service.authorize(principal(request))
        with service.repo.engine.connect() as connection:
            value = candidates_projection(
                connection,
                service.registry,
                model_id,
                mode,
                ttl_seconds,
                service.repo.clock(),
            )
        return response(value)

    @app.post(PREFIX + "/market-refreshes", status_code=202)
    def market_refreshes(request: Request, body: dict):
        service.authorize(principal(request))
        if body:
            raise OperatorError("operator_inventory_refresh_invalid", 422)
        return JSONResponse(
            capacity_market.request_refresh(service.repo),
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.post(PREFIX + "/previews")
    def previews(request: Request, body: dict):
        return response(service.preview(principal(request), body))

    @app.post(PREFIX + "/starts", status_code=202)
    def starts(
        request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")
    ):
        return JSONResponse(
            service.start(principal(request), body, key),
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.post(PREFIX + "/nodes/{node_id}/drain", status_code=202)
    def drain(
        node_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return JSONResponse(
            service.node_command(principal(request), node_id, body, key, "drain"),
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.post(PREFIX + "/nodes/{node_id}/stop", status_code=202)
    def stop(
        node_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return JSONResponse(
            service.node_command(principal(request), node_id, body, key, "stop"),
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.post(PREFIX + "/nodes/{node_id}/extension-previews")
    def extension_previews(node_id: str, request: Request, body: dict):
        return response(service.extension_preview(principal(request), node_id, body))

    @app.post(PREFIX + "/nodes/{node_id}/extensions", status_code=202)
    def extensions(
        node_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return JSONResponse(
            service.extend(principal(request), node_id, body, key),
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.put(PREFIX + "/policy")
    def policy(request: Request, body: dict):
        return response(service.update_policy(principal(request), body))

    @app.post(PREFIX + "/nodes/{node_id}/manual-review")
    def manual_review(
        node_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return response(service.manual_review(principal(request), node_id, body, key))

    return service
