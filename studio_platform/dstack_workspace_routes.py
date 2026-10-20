"""Authenticated P4 workspace and presentation-group HTTP contracts."""

from fastapi import Header, Request
from fastapi.responses import JSONResponse

from .dstack_workspace import CapacityLoss, DstackWorkspace

PREFIX = "/v1/operator/dstack"


def register_routes(app, *, service):
    workspace = DstackWorkspace(service)
    app.state.dstack_workspace = workspace

    def principal(request):
        return getattr(request.state, "principal", None)

    def response(value, status=200):
        return JSONResponse(
            value, status_code=status, headers={"Cache-Control": "no-store"}
        )

    @app.exception_handler(CapacityLoss)
    async def capacity_loss(_request, error):
        return response({"code": error.code, **error.impact}, 409)

    @app.get(PREFIX + "/workspace")
    def state(request: Request):
        return response(workspace.state(principal(request)))

    @app.get(PREFIX + "/admin/workspace")
    def admin_state(request: Request):
        return response(workspace.state(principal(request), admin=True))

    @app.get(PREFIX + "/admin/demand-start")
    def demand_start(request: Request):
        return response(workspace.demand_start(principal(request)))

    @app.put(PREFIX + "/admin/demand-start")
    def set_demand_start(
        request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")
    ):
        return response(workspace.set_demand_start(principal(request), body, key))

    @app.post(PREFIX + "/groups", status_code=201)
    def create(
        request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")
    ):
        return response(workspace.create_group(principal(request), body, key), 201)

    @app.post(PREFIX + "/nodes/{node_id}/group")
    def move(
        node_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return response(workspace.move(principal(request), node_id, body, key))

    @app.get(PREFIX + "/nodes/{node_id}/stop-impact")
    def node_impact(node_id: str, request: Request):
        return response(workspace.impact(principal(request), node_id=node_id))

    @app.get(PREFIX + "/nodes/{node_id}/metrics")
    def node_metrics(node_id: str, request: Request):
        from sqlalchemy import select
        from .dstack_metrics import machine_metrics
        from .stage_metrics import configured_stage_reader
        from .repository import instance_intents
        from .dstack_workspace import _workers

        actor = principal(request)
        service.authorize(actor)
        with service.repo.engine.connect() as connection:
            node = service._owned_node(connection, actor, node_id, allow_admin=True)
            reader = configured_stage_reader(
                getattr(service.settings, "stage_query_config_file", None)
            )
            intent = dict(
                connection.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == node["intent_id"]
                    )
                )
                .mappings()
                .one()
            )
            value = machine_metrics(
                connection,
                node,
                intent,
                _workers(connection, intent),
                service.repo.clock(),
                stage_reader=reader,
            )
        return response({"node_id": node_id, **value})

    @app.get(PREFIX + "/groups/{group_id}/stop-impact")
    def group_impact(group_id: str, request: Request):
        return response(workspace.impact(principal(request), group_id=group_id))

    @app.post(PREFIX + "/groups/{group_id}/stop", status_code=202)
    def stop(
        group_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return response(
            workspace.group_command(principal(request), group_id, body, key, "stop"),
            202,
        )

    @app.post(PREFIX + "/groups/{group_id}/hold")
    def hold(
        group_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return response(
            workspace.group_command(principal(request), group_id, body, key, "hold")
        )

    return workspace
