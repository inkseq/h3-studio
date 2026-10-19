"""Content-policy HTTP routes; the application keeps its normal auth and CSRF."""

from fastapi import Header, Request
from fastapi.responses import JSONResponse

from .content_policy import ContentPolicyAPI, ContentPolicyError


def register_routes(app, settings, repo, principal_resolver):
    service = ContentPolicyAPI(repo, settings)
    app.state.content_policy = service

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.exception_handler(ContentPolicyError)
    async def content_error(_request, error):
        return JSONResponse(
            {"code": error.code, "detail": error.detail},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/legal/policy")
    def policy():
        return response(service.public())

    @app.get("/v1/admin/content-policy")
    def admin_policy(request: Request):
        return response(service.admin(principal_resolver(request)))

    @app.post("/v1/admin/content-policy")
    def update_policy(
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return response(service.update(principal_resolver(request), body, key=key))

    @app.post("/v1/account/terms")
    def accept_terms(request: Request, body: dict):
        return response(service.accept_terms(principal_resolver(request), body))

    return service
