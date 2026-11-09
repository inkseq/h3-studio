"""Turn one machine's recorded capacity timeline into a simulator scenario.

    python -m studio_platform.capacity_incident_export <intent_id> [--since T] [--until T]
    python tools/export_capacity_incident.py <intent_id> > tests/fixtures/capacity_sim/incident_x.json

Reads the database read-only (the capacity timeline, provider snapshots and the
machine's own attempts) and prints a fixture for ``tests/capacity_sim.py``: a
MachineScript with the recorded timings (allocation, download, model load,
worker loss, provider removal, stop) and provider responses (identity fields,
read errors), the jobs that machine ran and the work still queued when it
stopped. See docs/CAPACITY-SIMULATOR.md, "From incident to regression test".

The fixture is meant for the public repository: it carries no owner, project,
account, email, IP, credential, budget amount or spend. Identifiers are
replaced by the scenario name; prices are the provider's hourly offer price.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

from sqlalchemy import select

TIMELINE = "capacity.timeline"
SNAPSHOT = "capacity.provider_snapshot"
REGISTRATION_LEASE_SECONDS = 120
SYNTHETIC_TASK_LIMIT_MICROUSD = 50_000_000
MAX_JOBS = 60


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def read_incident(connection, intent_id, *, since=None, until=None):
    """Timeline entries, snapshots and this machine's attempts, oldest first."""
    from .repository import attempts, jobs, outbox

    def window(at):
        return (since is None or at >= since) and (until is None or at <= until)

    events = connection.execute(
        select(outbox.c.event_type, outbox.c.payload, outbox.c.created_at)
        .where(
            outbox.c.event_type.in_((TIMELINE, SNAPSHOT)),
            outbox.c.aggregate_id == intent_id,
        )
        .order_by(outbox.c.created_at, outbox.c.id)
    ).mappings()
    timeline, snapshots = [], []
    for row in events:
        payload = dict(row["payload"] or {})
        at = payload.get("at") if _finite(payload.get("at")) else row["created_at"]
        if not window(at):
            continue
        payload["at"] = at
        (timeline if row["event_type"] == TIMELINE else snapshots).append(payload)
    timeline.sort(key=lambda entry: entry["at"])
    snapshots.sort(key=lambda entry: entry["at"])
    worker = "dstack-" + intent_id.replace("-", "")
    claimed = []
    for row in connection.execute(
        select(
            attempts.c.job_id,
            attempts.c.created_at,
            attempts.c.updated_at,
            attempts.c.submission_started_at,
            attempts.c.status,
            jobs.c.created_at.label("job_created_at"),
            jobs.c.updated_at.label("job_updated_at"),
            jobs.c.status.label("job_status"),
            jobs.c.expected_runtime_s,
            jobs.c.request,
        )
        .join(jobs, jobs.c.id == attempts.c.job_id)
        .where(attempts.c.worker_id == worker)
        .order_by(attempts.c.created_at)
    ).mappings():
        if window(row["created_at"]):
            claimed.append(dict(row))
    return {
        "intent_id": intent_id,
        "timeline": timeline,
        "snapshots": snapshots,
        "attempts": claimed,
    }


def _first(timeline, *, stage=None, code=None, after=None, where=None):
    for entry in timeline:
        if stage is not None and entry.get("stage") != stage:
            continue
        if code is not None and entry.get("code") not in (
            code if isinstance(code, tuple) else (code,)
        ):
            continue
        if after is not None and entry["at"] < after:
            continue
        if where is not None and not where(entry):
            continue
        return entry
    return None


def _seconds(value):
    return None if value is None else max(0, int(round(value)))


def build_fixture(incident, *, name=None):
    """A simulator fixture dict for one recorded machine."""
    timeline, snapshots = incident["timeline"], incident["snapshots"]
    start = _first(timeline, code="start_requested")
    if start is None:
        raise ValueError("capacity_incident_start_not_recorded")
    detail = start.get("detail", {})
    t0 = detail.get("created_at") if _finite(detail.get("created_at")) else start["at"]
    machine = {}
    notes = []

    stop = _first(timeline, code="stop_requested")
    stop_at = stop["at"] if stop else None
    create = next(
        (
            s
            for s in snapshots
            if s.get("kind") == "create" and s.get("data", {}).get("error")
        ),
        None,
    )
    allocated = _first(timeline, code="allocated")
    if create is not None:
        notes.append("create failed with " + create["data"]["error"])
        if allocated is not None:
            machine["create"] = "reply_lost"
    if allocated is None:
        if _first(timeline, code="no_allocation_confirmed") or any(
            "no_capacity" in str(s.get("data", {})) for s in snapshots
        ):
            machine["no_capacity"] = True
        else:
            machine["allocate_after"] = None
    else:
        machine["allocate_after"] = _seconds(allocated["at"] - t0)
        running = _first(timeline, code="run_running", after=allocated["at"])
        running_at = running["at"] if running else allocated["at"] + 30
        machine["running_after"] = _seconds(running_at - allocated["at"])
        downloads = [e for e in timeline if e.get("stage") == "download"]
        complete = next(
            (e for e in downloads if e["code"] == "download_complete"), None
        )
        # The model load starting is the most precise end of the download.
        loading = _first(timeline, code="phase_model_load", after=running_at)
        if complete is not None and loading is not None:
            complete = dict(complete, at=min(complete["at"], loading["at"]))
        if downloads:
            machine["download_bytes"] = (
                downloads[-1]["detail"].get("bytes_total") or 52 * 1000**3
            )
        if complete is not None:
            machine["download_seconds"] = max(1, _seconds(complete["at"] - running_at))
        elif downloads:
            last = max(
                (e for e in downloads if e["code"] != "download_complete"),
                key=lambda e: e["detail"].get("percent") or 0,
            )
            fraction = (last["detail"].get("percent") or 0) / 100
            stuck = _first(timeline, code=("download_stalled", "download_slow"))
            rate = last["detail"].get("rate_bytes_per_s")
            total = machine["download_bytes"]
            quiet = (stuck or {}).get("detail", {}).get("last_progress_age_s") or 0
            if stuck is not None and (rate is None or quiet >= 120):
                # Bytes stopped: the last progress is the entry time minus its age.
                age = stuck["detail"].get("last_progress_age_s") or 0
                elapsed = max(60, stuck["at"] - age - running_at)
                machine["download_stall_at"] = round(max(fraction, 0.01), 3)
                machine["download_seconds"] = _seconds(elapsed / max(fraction, 0.01))
            elif rate:
                machine["download_seconds"] = _seconds(total / rate)
        ready = _first(timeline, code="runtime_ready")
        if ready is not None and complete is not None:
            machine["load_seconds"] = max(1, _seconds(ready["at"] - complete["at"]))
        elif complete is not None and _first(
            timeline,
            stage="boot",
            after=complete["at"],
            where=lambda e: "failed" in str(e.get("code")),
        ):
            machine["load_fails"] = True
        if ready is not None:
            machine.update(_worker_loss(timeline, ready["at"], stop_at, t0))
        ended = _first(
            timeline,
            code=("run_failed", "run_terminated", "run_aborted", "run_done"),
            after=allocated["at"],
        )
        if ended is not None and (stop_at is None or ended["at"] < stop_at):
            machine["terminal_at"] = _seconds(ended["at"] - t0)
        elif ended is not None and stop_at is not None:
            machine["stop_seconds"] = max(1, _seconds(ended["at"] - stop_at))
    offer = _first(timeline, code="offer_chosen")
    if offer and _finite(offer["detail"].get("quoted_hourly_microusd")):
        machine["price"] = round(offer["detail"]["quoted_hourly_microusd"] / 1e6, 4)
    machine["read_errors"] = _read_errors(snapshots, t0)
    run = _run_overlay(snapshots)
    attempts = incident["attempts"]
    jobs = _jobs(attempts, t0)
    queued = stop.get("detail", {}).get("queued") if stop else None
    if isinstance(queued, int) and queued > 0:
        runtimes = [job["actual_runtime_s"] for job in jobs] or [240]
        expected = [job["expected_runtime_s"] for job in jobs] or [1800]
        jobs.append(
            {
                "at": 0,
                "count": min(queued, MAX_JOBS),
                "expected_runtime_s": max(expected),
                "actual_runtime_s": sorted(runtimes)[len(runtimes) // 2],
            }
        )
    notes.append(
        "stopped for %s after %d claims" % (stop["detail"].get("reason"), len(attempts))
        if stop
        else "not stopped when exported"
    )
    end = max([entry["at"] for entry in timeline] or [t0])
    scenario = {
        "name": name or "incident_" + incident["intent_id"][:8],
        "description": "Recorded machine replay: " + ", ".join(notes),
        "hours": max(1.0, math.ceil((end - t0) / 1800 + 1) / 2),
        "max_machines": 1,
        "jobs": jobs or [{"at": 0, "count": 1}],
    }
    if detail.get("max_runtime_seconds"):
        scenario["test_guard"] = {
            "task_limit_microusd": SYNTHETIC_TASK_LIMIT_MICROUSD,
            "max_runtime_seconds": detail["max_runtime_seconds"],
        }
    if _finite(detail.get("observation_fresh_seconds")):
        scenario["observation_fresh_seconds"] = int(detail["observation_fresh_seconds"])
    outcome = {
        "stop_reason": stop["detail"].get("reason") if stop else None,
        "claims": len(attempts),
        "submitted": sum(_finite(a["submission_started_at"]) for a in attempts),
    }
    return {
        "description": scenario["description"],
        "machine": machine,
        "run": run,
        "scenario": scenario,
        "recorded": {
            "outcome": outcome,
            "timeline": [
                {
                    "t": round(entry["at"] - t0, 1),
                    "stage": entry.get("stage"),
                    "code": entry.get("code"),
                }
                for entry in timeline
            ],
            "provider": [
                {
                    "t": round(s["at"] - t0, 1),
                    "kind": s.get("kind"),
                    "data": _public(s.get("data")),
                }
                for s in snapshots
            ],
        },
    }


def _worker_loss(timeline, ready_at, stop_at, t0):
    """worker_lost_at when a serving host stopped answering and never recovered.

    The first sign is the runtime read leaving "serving" (its tunnel or worker
    is gone), else the registration expiring one lease after the last renewal.
    """
    before_stop = [e for e in timeline if stop_at is None or e["at"] < stop_at]
    boots = [e for e in before_stop if e.get("stage") == "boot" and e["at"] > ready_at]
    lost = None
    for entry in boots:
        serving = entry.get("code") == "serving"
        lost = None if serving else lost or entry
    expired = _first(
        before_stop, code=("worker_expired", "worker_unknown"), after=ready_at
    )
    if lost is not None:
        return {"worker_lost_at": _seconds(lost["at"] - t0)}
    if expired is not None:
        at = expired["at"] - REGISTRATION_LEASE_SECONDS
        return {"worker_lost_at": _seconds(max(ready_at, at) - t0)}
    return {}


def _jobs(attempts, t0):
    """The jobs this machine claimed, grouped by mode and expected runtime.

    Each group runs at its median measured runtime (submission to the
    attempt's own end); jobs queued together are claimed in an arbitrary fair
    order, so per-job runtimes would make the replay depend on that order.
    """
    groups, seen = {}, set()
    for attempt in attempts:
        if attempt["job_id"] in seen or len(seen) >= MAX_JOBS:
            continue
        seen.add(attempt["job_id"])
        request = attempt["request"] if isinstance(attempt["request"], dict) else {}
        inner = (
            request.get("request") if isinstance(request.get("request"), dict) else {}
        )
        mode = inner.get("mode") if inner.get("mode") in ("fl", "ref") else "fl"
        expected = int(attempt["expected_runtime_s"] or 1800)
        group = groups.setdefault(
            (mode, expected), {"at": None, "count": 0, "runs": []}
        )
        group["count"] += 1
        arrived = _seconds(max(0, attempt["job_created_at"] - t0))
        group["at"] = arrived if group["at"] is None else min(group["at"], arrived)
        started, ended = attempt["submission_started_at"], attempt["updated_at"]
        if attempt["status"] != "attempt_lost" and _finite(started) and ended > started:
            group["runs"].append(ended - started)
    jobs = []
    for (mode, expected), group in groups.items():
        runs = sorted(group["runs"]) or [min(expected, 240)]
        jobs.append(
            {
                "at": group["at"],
                "count": group["count"],
                "mode": mode,
                "expected_runtime_s": expected,
                "actual_runtime_s": max(
                    30, min(expected, _seconds(runs[len(runs) // 2]))
                ),
            }
        )
    return jobs


def _read_errors(snapshots, t0):
    """[[from, until, code]] windows in which the run read raised an error."""
    windows, current = [], None
    for snapshot in snapshots:
        if snapshot.get("kind") != "run":
            continue
        error = snapshot.get("data", {}).get("error")
        at = _seconds(snapshot["at"] - t0)
        if error and current is None:
            current = [at, None, error]
        elif current is not None and error != current[2]:
            current[1] = at
            windows.append(current)
            current = [at, None, error] if error else None
    if current is not None:
        current[1] = current[0] + 3600
        windows.append(current)
    return windows


def _run_overlay(snapshots):
    """Recorded provisioning fields laid over the simulated run reads."""
    for snapshot in reversed(snapshots):
        data = snapshot.get("data", {})
        provisioning = data.get("latest_job_submission", {}).get(
            "job_provisioning_data"
        )
        if snapshot.get("kind") == "run" and isinstance(provisioning, dict):
            fields = {
                k: provisioning[k]
                for k in ("region", "price")
                if provisioning.get(k) is not None
            }
            if fields:
                return {"latest_job_submission": {"job_provisioning_data": fields}}
    return None


def _public(data):
    """Drop provider identifiers from the readable copy of a snapshot."""
    if not isinstance(data, dict):
        return data
    hidden = {
        "id",
        "run_name",
        "instance_id",
        "submitted_at",
        "last_processed_at",
        "finished_at",
        "cost",
    }
    return {k: _public(v) for k, v in data.items() if k not in hidden}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("intent_id")
    parser.add_argument(
        "--since", type=float, help="unix seconds; earlier entries are ignored"
    )
    parser.add_argument(
        "--until", type=float, help="unix seconds; later entries are ignored"
    )
    parser.add_argument("--name", help="scenario name (default incident_<intent>)")
    args = parser.parse_args(argv)
    from .capacity_timeline_cli import read_only_connection

    with read_only_connection() as connection:
        incident = read_incident(
            connection, args.intent_id, since=args.since, until=args.until
        )
    print(json.dumps(build_fixture(incident, name=args.name), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
