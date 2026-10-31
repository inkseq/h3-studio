"""Admin read: how long finished generations took, by GPU type and content (owner, 2026-10-25).

Read-only over the original ledger. Rows carry closed bands and numbers only: no prompts,
media, provider responses or account identities beyond the owner id admins already see.
"""

from __future__ import annotations

import math
import statistics

from fastapi import Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from .operator_capacity import operator_commands, operator_nodes
from .repository import artifacts, attempts, jobs
from .telemetry import _bands

MAX_ROWS = 2000
GROUP_FIELDS = (
    "gpu_type",
    "mode",
    "length",
    "resolution",
    "image_refs_band",
    "audio_ref",
)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def content(request):
    """Frames/fps/height/reference counts from a compiled job request."""
    compiled = request if isinstance(request, dict) else {}
    inner = compiled.get("request") if isinstance(compiled.get("request"), dict) else {}
    output = (
        compiled.get("output_spec")
        if isinstance(compiled.get("output_spec"), dict)
        else {}
    )
    value = {
        "mode": inner.get("mode") if inner.get("mode") in ("fl", "ref") else "unknown"
    }
    for name, source in (
        ("frames", "frame_count"),
        ("fps", "fps"),
        ("height", "height"),
    ):
        item = output.get(source, output.get(name, inner.get(name)))
        if type(item) is int and item > 0:
            value[name] = item
    inputs = inner.get("inputs") if isinstance(inner.get("inputs"), dict) else {}
    for name, plural in (("image_refs", "images"), ("audio_refs", "audios")):
        if isinstance(inputs.get(plural), list):
            value[name] = len(inputs[plural])
    seconds = output.get("actual_duration")
    if type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0:
        # Production output specs carry the native duration instead of an fps.
        value["seconds"] = round(seconds, 2)
        if value.get("frames") and "fps" not in value:
            value["fps"] = max(1, round(value["frames"] / seconds))
    elif value.get("frames") and value.get("fps"):
        value["seconds"] = round(value["frames"] / value["fps"], 2)
    return value


def _intent(worker_id):
    raw = worker_id[len("dstack-") :] if isinstance(worker_id, str) else ""
    if len(raw) != 32 or any(c not in "0123456789abcdef" for c in raw):
        return None
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _summary(values):
    ordered = sorted(values)
    return {
        "median": round(statistics.median(ordered), 1),
        "p90": round(
            ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)], 1
        ),
        "max": round(ordered[-1], 1),
    }


def generation_stats(repo, tenant_id, *, days, now):
    since = now - days * 86400
    with repo.engine.connect() as connection:
        rows = list(
            connection.execute(
                select(
                    jobs.c.id,
                    jobs.c.owner_id,
                    jobs.c.request,
                    jobs.c.created_at,
                    attempts.c.number,
                    attempts.c.worker_id,
                    attempts.c.submission_started_at,
                    attempts.c.updated_at,
                    attempts.c.actual_cost_microusd,
                    # The first output written is the finish; updated_at moves on
                    # later (settlement, reconciliation) and inflated runs to days.
                    select(func.min(artifacts.c.created_at))
                    .where(artifacts.c.attempt_id == attempts.c.id)
                    .scalar_subquery()
                    .label("output_at"),
                )
                .select_from(attempts.join(jobs, attempts.c.job_id == jobs.c.id))
                .where(
                    jobs.c.tenant_id == tenant_id,
                    attempts.c.status == "succeeded",
                    attempts.c.updated_at >= since,
                )
                .order_by(attempts.c.updated_at.desc())
                .limit(MAX_ROWS)
            ).mappings()
        )
        intents = {i for i in (_intent(row["worker_id"]) for row in rows) if i}
        gpus = {}
        if intents:
            for node in connection.execute(
                select(operator_nodes.c.intent_id, operator_nodes.c.payload).where(
                    operator_nodes.c.intent_id.in_(sorted(intents))
                )
            ).mappings():
                payload = node["payload"] if isinstance(node["payload"], dict) else {}
                gpu = (payload.get("selection") or {}).get("gpu_type")
                gpus[node["intent_id"]] = (
                    gpu if isinstance(gpu, str) and gpu else "unknown"
                )
    items = []
    for row in rows:
        started = row["submission_started_at"]
        finished = row["output_at"] if _number(row["output_at"]) else row["updated_at"]
        if not (_number(started) and _number(finished) and finished >= started):
            continue
        detail = content(row["request"])
        item = {
            "job_id": row["id"],
            "owner_id": row["owner_id"],
            "attempt": row["number"],
            "finished_at": finished,
            "gpu_type": gpus.get(_intent(row["worker_id"]), "unknown"),
            "generate_seconds": round(finished - started, 1),
            "wait_seconds": round(started - row["created_at"], 1)
            if _number(row["created_at"]) and started >= row["created_at"]
            else None,
            "cost_microusd": row["actual_cost_microusd"]
            if _number(row["actual_cost_microusd"])
            else None,
            **{
                k: detail[k]
                for k in ("mode", "seconds", "height", "image_refs", "audio_refs")
                if k in detail
            },
            **_bands(detail),
        }
        item["mode"] = detail["mode"]
        items.append(item)
    groups = {}
    for item in items:
        groups.setdefault(tuple(item[name] for name in GROUP_FIELDS), []).append(item)
    summary = []
    for key, members in groups.items():
        costs = [m["cost_microusd"] for m in members if m["cost_microusd"] is not None]
        summary.append(
            {
                **dict(zip(GROUP_FIELDS, key)),
                "count": len(members),
                "generate_seconds": _summary([m["generate_seconds"] for m in members]),
                "median_cost_microusd": int(statistics.median(costs))
                if costs
                else None,
            }
        )
    summary.sort(key=lambda g: (-g["count"], g["gpu_type"], g["mode"]))
    return {
        "days": days,
        "count": len(items),
        "groups": summary,
        "recent": items[:200],
        "overview": overview(repo, tenant_id, items, since=since),
    }


MANUAL_KINDS = ("dstack_start", "dstack_stop", "dstack_hold")


def overview(repo, tenant_id, items, *, since):
    """#819 top line: outcomes, waits, GPU cost per clip, hands-on operations."""
    with repo.engine.connect() as connection:
        outcomes = dict(
            connection.execute(
                select(jobs.c.status, func.count())
                .where(
                    jobs.c.tenant_id == tenant_id,
                    jobs.c.created_at >= since,
                    jobs.c.status.in_(("succeeded", "failed", "cancelled")),
                )
                .group_by(jobs.c.status)
            ).all()
        )
        # Queue-driven starts use the dstack-demand actor; every other machine
        # start/stop/hold was a person (or an agent acting for one).
        manual = connection.execute(
            select(func.count()).where(
                operator_commands.c.created_at >= since,
                operator_commands.c.kind.in_(MANUAL_KINDS),
                ~operator_commands.c.actor.like("dstack-demand:%"),
            )
        ).scalar_one()
    finished = outcomes.get("succeeded", 0) + outcomes.get("failed", 0)
    waits = [i["wait_seconds"] for i in items if _number(i["wait_seconds"])]
    costs = [i["cost_microusd"] for i in items if i["cost_microusd"] is not None]
    return {
        "succeeded": outcomes.get("succeeded", 0),
        "failed": outcomes.get("failed", 0),
        "cancelled": outcomes.get("cancelled", 0),
        "success_rate": round(outcomes.get("succeeded", 0) / finished, 3)
        if finished
        else None,
        "wait_seconds": _summary(waits) if waits else None,
        "median_cost_microusd": int(statistics.median(costs)) if costs else None,
        "total_cost_microusd": sum(costs) if costs else 0,
        "manual_operations": manual,
    }


def register_routes(app):
    from .identity import require_admin

    @app.get("/v1/operator/admin/generation-stats")
    def stats(request: Request, days: int = Query(14, ge=1, le=90)):
        repo, settings = app.state.repository, app.state.settings
        require_admin(
            request.state.principal,
            repo,
            settings,
            machine_code="generation_stats_human_required",
        )
        return JSONResponse(
            generation_stats(repo, settings.tenant_id, days=days, now=repo.clock()),
            headers={"Cache-Control": "no-store"},
        )
