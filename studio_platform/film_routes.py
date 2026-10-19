"""One public film API for the browser and scoped Agent clients."""

from fastapi import Header, Request

from .film_runs import FilmRuns


def register_film_routes(app, *, enqueue=None):
    service = FilmRuns(app, enqueue=enqueue)
    app.state.film_runs = service

    @app.post('/v1/projects/{project_id}/films/preflights', status_code=201)
    def preflight(
        project_id: str,
        request: Request,
        body: dict,
        idempotency_key: str = Header(..., alias='Idempotency-Key'),
    ):
        return service.preflight(
            request.state.principal, project_id, body, idempotency_key
        )

    @app.post('/v1/projects/{project_id}/films', status_code=202)
    def submit(
        project_id: str,
        request: Request,
        body: dict,
        idempotency_key: str = Header(..., alias='Idempotency-Key'),
    ):
        return service.submit(
            request.state.principal, project_id, body, idempotency_key
        )

    @app.get('/v1/projects/{project_id}/films/{run_id}')
    def get(project_id: str, run_id: str, request: Request):
        return service.get(request.state.principal, project_id, run_id)

    @app.post('/v1/projects/{project_id}/films/{run_id}/cancel', status_code=202)
    def cancel(project_id: str, run_id: str, request: Request):
        return service.cancel(request.state.principal, project_id, run_id)

    @app.post('/v1/projects/{project_id}/films/{run_id}/resume', status_code=202)
    def resume(project_id: str, run_id: str, request: Request):
        return service.resume(request.state.principal, project_id, run_id)

    return service
