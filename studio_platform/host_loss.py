"""Paid GPUs a lost CPU host may stop off-host (#816).

The CPU host runs the site, database, controller and dstack server together.
If it dies, a Vast rental keeps billing with nobody to stop it. While healthy,
the controller writes the paid Vast instances an off-host stopper may stop;
a host timer publishes the file and the stopper acts only on the heartbeat
alarm plus its own outage check. supervan's machines are never listed.

Writing is best effort and never affects capacity or business state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from sqlalchemy import select

from .operator_capacity import operator_nodes
from .repository import instance_intents

FILE_NAME = "host-loss-stoppable.json"
EXCLUDED_OWNERS = frozenset({"supervan"})
TERMINAL = ("destroyed", "not_created")


def stoppable(connection):
    """Live Vast rentals with a known instance id, excluding protected owners."""
    rows = connection.execute(
        select(
            instance_intents.c.id,
            instance_intents.c.provider_instance_id,
            operator_nodes.c.payload,
        )
        .select_from(
            instance_intents.outerjoin(
                operator_nodes, operator_nodes.c.intent_id == instance_intents.c.id
            )
        )
        .where(
            instance_intents.c.provider == "vast",
            instance_intents.c.state.not_in(TERMINAL),
            instance_intents.c.provider_instance_id.is_not(None),
        )
    ).all()
    result = []
    for intent_id, instance_id, payload in rows:
        owner = payload.get("owner_id") if isinstance(payload, dict) else None
        # Unknown ownership is not proof that a machine is ours to stop.
        if owner is None or owner in EXCLUDED_OWNERS:
            continue
        if isinstance(instance_id, str) and instance_id.isdigit():
            result.append({"instance_id": instance_id, "intent_id": intent_id})
    return sorted(result, key=lambda item: item["instance_id"])


class Manifest:
    """Rewrite the stop list at most once per interval from the loop heartbeat."""

    def __init__(self, repo, directory, *, clock=time.time, interval=60):
        self.repo, self.path = repo, Path(directory) / FILE_NAME
        self.clock, self.interval, self.last = clock, interval, None

    def beat(self):
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            return
        self.last = now
        try:
            with self.repo.engine.connect() as connection:
                items = stoppable(connection)
            raw = json.dumps(
                {"version": 1, "updated_at": now, "instances": items},
                sort_keys=True,
            ).encode()
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".host-loss-")
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            os.chmod(name, 0o640)
            os.replace(name, self.path)
        except Exception:
            pass  # A stale list ages out; the stopper refuses old files.
