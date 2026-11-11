"""Print a machine's capacity timeline and where it got stuck; read-only.

    python -m studio_platform.capacity_timeline_cli <intent_id>     # one machine
    python -m studio_platform.capacity_timeline_cli --recent 10     # latest machines
    python -m studio_platform.capacity_timeline_cli demand          # demand decisions
    python tools/capacity_timeline.py ...                           # same, from a checkout

Runs inside the app or controller container (for example through SSM), with
the usual SIXNINE_DATABASE_URL(_FILE). It opens a read-only transaction and
writes nothing. A unique intent id prefix (8+ characters) is enough.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import sys
import time

from sqlalchemy import func, select

TIMELINE = "capacity.timeline"
SNAPSHOT = "capacity.provider_snapshot"
ALERT = "capacity.health_alert"
# The lifecycle a healthy machine walks through, in order.
MILESTONES = (
    ("start_requested", "demand start"),
    ("offer_chosen", "provider offer"),
    ("allocated", "provider allocation"),
    ("run_running", "run running"),
    ("download_complete", "model download"),
    ("runtime_ready", "model load / runtime ready"),
    ("worker_ready", "worker registration"),
    ("first_claim", "first claim"),
)
CLOSING = (
    ("stop_requested", "stop request"),
    ("removal_confirmed", "provider removal"),
    ("intent_destroyed", "intent destroyed"),
    ("timeline_closed", "billing settled"),
)
PROBLEMS = (
    "download_stalled",
    "download_slow",
    "worker_expired",
    "worker_unknown",
    "claim_refused",
    "collect_failed",
    "collect_output_stored",
    "collect_reclaimed",
    "timeline_truncated",
)


@contextmanager
def read_only_connection(database_url=None):
    from .repository import Repository
    from .settings import database_url_from_environment

    repo = Repository(database_url or database_url_from_environment())
    try:
        with repo.engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
            yield connection
    finally:
        repo.close()


def _rows(connection, aggregate, kinds=(TIMELINE, SNAPSHOT, ALERT)):
    from .repository import outbox

    result = []
    for row in connection.execute(
        select(outbox.c.event_type, outbox.c.payload, outbox.c.created_at)
        .where(outbox.c.aggregate_id == aggregate, outbox.c.event_type.in_(kinds))
        .order_by(outbox.c.created_at, outbox.c.id)
    ).mappings():
        payload = dict(row["payload"] or {})
        at = payload.get("at")
        payload["at"] = at if isinstance(at, (int, float)) else row["created_at"]
        payload["_type"] = row["event_type"]
        result.append(payload)
    result.sort(key=lambda entry: entry["at"])
    return result


def resolve(connection, value):
    """The full intent id for an id or a unique prefix of at least 8 characters."""
    from .repository import outbox

    if len(value) < 8:
        raise SystemExit("give at least 8 characters of the intent id")
    matches = list(
        connection.execute(
            select(outbox.c.aggregate_id)
            .where(
                outbox.c.event_type == TIMELINE,
                outbox.c.aggregate_id.like(value.replace("%", "") + "%"),
            )
            .distinct()
            .limit(2)
        ).scalars()
    )
    if len(matches) != 1:
        raise SystemExit("no unique intent for %r" % value)
    return matches[0]


def _clock(at, t0):
    offset = at - t0
    sign = "-" if offset < 0 else "+"
    offset = abs(int(offset))
    return "%s %s%02d:%02d:%02d" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(at)),
        sign,
        offset // 3600,
        offset % 3600 // 60,
        offset % 60,
    )


def _brief(entry):
    detail = entry.get("detail") or {}
    if entry["_type"] == SNAPSHOT:
        return "%s %s" % (
            entry.get("kind"),
            json.dumps(entry.get("data"), sort_keys=True)[:160],
        )
    if entry["_type"] == ALERT:
        return "ALERT %s diagnosis=%s detail=%s" % (
            entry.get("code"),
            entry.get("diagnosis"),
            entry.get("detail"),
        )
    keep = {
        key: value
        for key, value in detail.items()
        if value not in (None, {}, [], "")
        and key not in ("created_at", "expires_at", "blocked_since", "stalled_since")
    }
    return "%-28s %s" % (
        entry.get("code"),
        json.dumps(keep, sort_keys=True)[:200] if keep else "",
    )


def diagnose(entries, now):
    """Where the machine got stuck and why, from its own timeline."""
    codes = {}
    for entry in entries:
        if entry["_type"] == TIMELINE:
            codes.setdefault(entry.get("code"), entry)
    if not codes:
        return ["no timeline recorded for this machine"]
    lines = []
    stop = codes.get("stop_requested")
    reached = [name for code, name in MILESTONES if code in codes]
    missing = next(
        ((code, name) for code, name in MILESTONES if code not in codes), None
    )
    last = max((e for e in entries if e["_type"] == TIMELINE), key=lambda e: e["at"])
    if stop is not None:
        reason = (stop.get("detail") or {}).get("reason")
        lines.append(
            "stopped: %s, after %s (claims %s)"
            % (
                reason,
                reached[-1] if reached else "nothing",
                (stop.get("detail") or {}).get("claims"),
            )
        )
        # No machine was ever allocated: there is nothing to remove (#1214).
        unallocated = (
            "no_allocation_confirmed" in codes or reason == "dstack_never_allocated"
        )
        pending = next(
            (
                (c, n)
                for c, n in CLOSING
                if c not in codes and not (unallocated and c == "removal_confirmed")
            ),
            None,
        )
        if unallocated and pending is None:
            lines.append("closed: the provider had no machine; nothing was charged")
        elif pending is not None:
            lines.append(
                "still waiting for: %s (last change %s, %d min ago)"
                % (pending[1], last.get("code"), (now - last["at"]) // 60)
            )
        else:
            lines.append("closed: provider removal confirmed and billing settled")
    elif missing is not None:
        lines.append(
            "stuck before: %s (reached: %s); last change %s %d min ago"
            % (
                missing[1],
                ", ".join(reached) or "nothing",
                last.get("code"),
                (now - last["at"]) // 60,
            )
        )
    else:
        lines.append(
            "serving: every boot stage reached; last change %s" % last.get("code")
        )
    ready = codes.get("runtime_ready")
    for entry in entries:
        lost = (
            ready is not None
            and entry.get("stage") == "boot"
            and entry["at"] > ready["at"]
            and entry.get("code") != "serving"
            and (stop is None or entry["at"] < stop["at"])
        )
        if lost or entry.get("code") in PROBLEMS or entry["_type"] == ALERT:
            lines.append("  why: " + _brief(entry))
    return lines[:12]


def show_machine(connection, intent_id, out):
    entries = _rows(connection, intent_id)
    if not entries:
        print("no timeline for %s" % intent_id, file=out)
        return
    t0 = entries[0]["at"]
    print("machine %s" % intent_id, file=out)
    for entry in entries:
        print(
            "  %s  %-8s %s"
            % (
                _clock(entry["at"], t0),
                entry.get("stage") or entry["_type"].split(".")[-1],
                _brief(entry),
            ),
            file=out,
        )
    print("where it got stuck:", file=out)
    for line in diagnose(entries, time.time()):
        print("  " + line, file=out)


def show_demand(connection, tenant, out, limit=60):
    from .capacity_timeline import demand_key

    entries = _rows(connection, demand_key(tenant))[-limit:]
    if not entries:
        print("no demand decisions recorded", file=out)
        return
    t0 = entries[0]["at"]
    for entry in entries:
        print("  %s  %s" % (_clock(entry["at"], t0), _brief(entry)), file=out)


def recent(connection, count):
    from .repository import outbox

    rows = connection.execute(
        select(outbox.c.aggregate_id, func.max(outbox.c.created_at).label("last"))
        .where(
            outbox.c.event_type == TIMELINE, outbox.c.aggregate_id.not_like("demand:%")
        )
        .group_by(outbox.c.aggregate_id)
        .order_by(func.max(outbox.c.created_at).desc())
        .limit(count)
    )
    return [row.aggregate_id for row in rows]


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", nargs="?", help="intent id (or prefix), or 'demand'")
    parser.add_argument("--recent", type=int, metavar="N")
    parser.add_argument("--tenant", help="tenant for 'demand' (default from settings)")
    args = parser.parse_args(argv)
    if not args.target and not args.recent:
        parser.error("give an intent id, 'demand' or --recent N")
    with read_only_connection() as connection:
        if args.target == "demand":
            tenant = args.tenant
            if tenant is None:
                from .settings import Settings

                tenant = Settings.from_environment().tenant_id
            show_demand(connection, tenant, out)
        elif args.target:
            show_machine(connection, resolve(connection, args.target), out)
        else:
            for intent_id in recent(connection, max(1, min(args.recent, 50))):
                entries = _rows(connection, intent_id, (TIMELINE,))
                summary = diagnose([dict(e) for e in entries], time.time())[0]
                print("%s  %s" % (intent_id, summary), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
