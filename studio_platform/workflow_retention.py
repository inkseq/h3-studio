"""Finished generation jobs give up their DBOS history.

Every poll of a running job adds durable rows, and the business backup refuses a
workflow export above its size limit. A finished job's result lives in its own
rows, and its transport history is never replayed. Film, payment and account
workflows are small and left alone.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select

from . import workflow_status
from .repository_schema import jobs

KEEP_SECONDS = 3 * 86400
SWEEP_SECONDS = 600
BATCH = 20
ENDED = ("succeeded", "failed", "cancelled")

_logger = logging.getLogger(__name__)


def expired_ids(connection, now, *, limit=BATCH):
    from .dbos_transport import APPLICATION, WORKFLOW, workflow_id

    cutoff = now - KEEP_SECONDS
    ended = workflow_status.ended_before(
        connection, APPLICATION, WORKFLOW, int(cutoff * 1000)
    )
    if not ended:
        return []
    finished = {
        workflow_id(row.tenant_id, row.owner_id, row.project_id, row.id)
        for row in connection.execute(
            select(
                jobs.c.tenant_id, jobs.c.owner_id, jobs.c.project_id, jobs.c.id
            ).where(jobs.c.status.in_(ENDED), jobs.c.updated_at < cutoff)
        )
    }
    selected = []
    for ident in ended:
        base = ident.split("/", 1)[0]
        if base in finished and not workflow_status.open_ids(
            connection, base, APPLICATION, WORKFLOW
        ):
            selected.append(ident)
            if len(selected) == limit:
                break
    return selected


class Sweeper:
    """One small delete per interval; a failure waits for the next interval."""

    def __init__(self, repo, *, clock=time.time, client_factory=None):
        self.repo = repo
        self.clock = clock
        self.client_factory = client_factory
        self.last = None

    def _client(self):
        if self.client_factory:
            return self.client_factory()
        from dbos import DBOSClient

        from .workflow_schema import dbos_client_config

        return DBOSClient(**dbos_client_config(self.repo.engine))

    def sweep_if_due(self):
        now = self.clock()
        if self.last is not None and now - self.last < SWEEP_SECONDS:
            return 0
        self.last = now
        try:
            with self.repo.engine.connect() as connection:
                ids = expired_ids(connection, now)
            if not ids:
                return 0
            client = self._client()
            try:
                client.delete_workflows(ids)
            finally:
                client.destroy()
            return len(ids)
        except Exception as error:
            _logger.warning(
                "workflow_retention_sweep_failed error_type=%s", type(error).__name__
            )
            return 0
