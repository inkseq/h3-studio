"""Controller progress + independent observer. Never calls a provider or mutates jobs."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from types import SimpleNamespace

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from .operator_capacity import operator_heartbeats
from .repository import outbox

INTERVAL = 60
STALE_AFTER = 180


def key(tenant):
    return "dstack-loop-" + hashlib.sha256(tenant.encode()).hexdigest()[:16]


class Progress:
    """Only the supervising loop may advance this row, never a background timer."""

    def __init__(self, repo, tenant):
        self.repo, self.key = repo, key(tenant)
        self.identity = "dstack-loop-" + str(uuid.uuid4())
        self.last = None

    def beat(self, active, *, starting=False):
        now = self.repo.clock()
        if not starting and self.last is not None and 0 <= now - self.last < INTERVAL:
            return
        values = dict(
            controller_id=self.identity,
            observed_at=now,
            state="starting" if starting else "running" if active else "idle",
        )
        with self.repo.transaction() as conn:
            row = conn.execute(
                select(operator_heartbeats.c.id).where(
                    operator_heartbeats.c.id == self.key
                )
            ).first()
            if row:
                conn.execute(
                    update(operator_heartbeats)
                    .where(operator_heartbeats.c.id == self.key)
                    .values(**values)
                )
            else:
                conn.execute(insert(operator_heartbeats).values(id=self.key, **values))
        self.last = now


def inspect(repo, tenant, *, stale_after=STALE_AFTER):
    """One bounded ledger observation + deterministic alert; no paid-resource action."""
    if type(stale_after) is not int or not 120 <= stale_after <= 1800:
        raise ValueError("controller_liveness_threshold_invalid")
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                select(operator_heartbeats).where(
                    operator_heartbeats.c.id == key(tenant)
                )
            )
            .mappings()
            .first()
        )
    now = repo.clock()
    stamp = row["observed_at"] if row else None
    valid = type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= stamp <= now
    if (
        row
        and valid
        and now - stamp <= stale_after
        and row["state"] in {"running", "idle"}
    ):
        return {"state": "healthy", "new_alert": False}
    # A new process gets bounded initialization time, not indefinite fake progress.
    if row and valid and now - stamp <= stale_after and row["state"] == "starting":
        return {"state": "starting", "new_alert": False}
    fingerprint = (
        (row["controller_id"], str(stamp), row["state"]) if row else ("missing",)
    )
    identity = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "inkseq-controller-liveness:" + key(tenant) + ":" + repr(fingerprint),
        )
    )
    value = {
        "version": 1,
        "event_id": identity,
        "tenant_id": tenant,
        "code": "capacity_controller_stale",
        "diagnosis": "controller_stale",
        "resource_id": key(tenant),
    }
    created = False
    try:
        with repo.transaction() as conn:
            if not conn.execute(
                select(outbox.c.id).where(outbox.c.id == identity)
            ).first():
                conn.execute(
                    insert(outbox).values(
                        id=identity,
                        event_type="capacity.health_alert",
                        aggregate_id=key(tenant),
                        payload=value,
                        created_at=now,
                    )
                )
                created = True
    except IntegrityError:
        # Concurrent checks use one incident identity; don't report a false success
        # unless the competing insertion is visible.
        with repo.engine.connect() as conn:
            if not conn.execute(
                select(outbox.c.id).where(outbox.c.id == identity)
            ).first():
                raise
    return {"state": "stale", "new_alert": created}


def main():
    """Docker health process: independent of the possibly blocked controller loop."""
    from sqlalchemy.engine import make_url
    from .settings import Settings
    from .repository import Repository
    from .capacity_health import pending_reports
    from .error_monitoring import create_client, report_capacity_health
    from .telemetry import configured_telemetry

    repo = client = telemetry = None
    try:
        settings = Settings.from_environment()
        url = make_url(settings.database_url)
        if url.drivername != "postgresql+psycopg":
            raise ValueError("controller_liveness_postgres_required")
        # This observer has its own short-lived connection and bounded SQL. Never
        # alter the controller/worker connections or reset the business ledger.
        url = url.update_query_dict(
            {
                "connect_timeout": "4",
                "options": "-c statement_timeout=4000 -c lock_timeout=1000",
            }
        )
        repo = Repository(url.render_as_string(hide_password=False))
        result = inspect(repo, settings.tenant_id)
        telemetry = configured_telemetry()
        client = create_client(settings)
        pending_reports(
            SimpleNamespace(repo=repo, settings=settings),
            telemetry,
            (lambda value: report_capacity_health(client, value))
            if client.dsn
            else None,
        )
        print(json.dumps(result), flush=True)
        return 0 if result["state"] == "healthy" else 1
    except Exception:
        # DB loss is unhealthy even when a durable alert cannot be written.
        print('{"state":"unavailable"}', flush=True)
        return 1
    finally:
        if client is not None:
            client.close(timeout=1)
        if telemetry is not None:
            telemetry.close()
        if repo is not None:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
