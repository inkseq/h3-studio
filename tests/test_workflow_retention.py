"""Finished generation jobs give up DBOS history; everything else keeps it."""

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from studio_platform.dbos_transport import APPLICATION, WORKFLOW, workflow_id
from studio_platform.repository_schema import jobs
from studio_platform.workflow_retention import (
    BATCH,
    KEEP_SECONDS,
    SWEEP_SECONDS,
    Sweeper,
    expired_ids,
)

NOW = 2_000_000_000.0
OLD = NOW - KEEP_SECONDS - 60
NEW = NOW - KEEP_SECONDS + 60


class Client:
    def __init__(self, fail=False):
        self.deleted, self.destroyed, self.fail = [], 0, fail

    def delete_workflows(self, ids):
        if self.fail:
            raise OSError("down")
        self.deleted.extend(ids)

    def destroy(self):
        self.destroyed += 1


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ATTACH ':memory:' AS inkseq_workflows")
            connection.exec_driver_sql(
                "CREATE TABLE platform_jobs (id TEXT, tenant_id TEXT, owner_id TEXT, "
                "project_id TEXT, status TEXT, updated_at REAL)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE inkseq_workflows.workflow_status (workflow_uuid TEXT, "
                "status TEXT, application_name TEXT, name TEXT, created_at REAL, "
                "updated_at REAL)"
            )
        self.assertEqual(jobs.name, "platform_jobs")

    def job(self, name, status="succeeded", updated=OLD):
        with self.engine.begin() as connection:
            connection.execute(
                text("INSERT INTO platform_jobs VALUES (:i,'t','o','p',:s,:u)"),
                {"i": name, "s": status, "u": updated},
            )
        return workflow_id("t", "o", "p", name)

    def run_row(self, ident, status="SUCCESS", updated=OLD, name=WORKFLOW):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO inkseq_workflows.workflow_status VALUES "
                    "(:i,:s,:a,:n,:u,:u)"
                ),
                {
                    "i": ident,
                    "s": status,
                    "a": APPLICATION,
                    "n": name,
                    "u": updated * 1000,
                },
            )
        return ident

    def expired(self, **kwargs):
        with self.engine.connect() as connection:
            return expired_ids(connection, NOW, **kwargs)

    def test_only_old_finished_runs_of_finished_jobs_expire(self):
        done = self.job("done")
        wanted = {self.run_row(done), self.run_row(done + "/epoch", "ERROR")}
        self.run_row(self.job("recent", updated=NEW))
        self.run_row(self.job("recent-run"), updated=NEW)
        self.run_row(self.job("running", "running"))
        self.run_row("inkseq-job-unknown")
        self.run_row(self.job("film"), name="inkseq-film-v1")
        self.assertEqual(set(self.expired()), wanted)

    def test_an_open_epoch_keeps_every_epoch_of_that_job(self):
        reopened = self.job("reopened")
        self.run_row(reopened)
        self.run_row(reopened + "/epoch", "PENDING")
        self.assertEqual(self.expired(), [])

    def test_batch_is_bounded_and_oldest_first(self):
        idents = [
            self.run_row(self.job(f"job{i:02d}"), updated=OLD - i)
            for i in range(BATCH + 3)
        ]
        self.assertEqual(self.expired(), list(reversed(idents))[:BATCH])

    def sweeper(self, client, clock):
        return Sweeper(
            SimpleNamespace(engine=self.engine),
            clock=lambda: clock[0],
            client_factory=lambda: client,
        )

    def test_sweep_deletes_through_the_client_on_its_interval(self):
        ident = self.run_row(self.job("done"))
        client, clock = Client(), [NOW]
        sweeper = self.sweeper(client, clock)
        self.assertEqual(sweeper.sweep_if_due(), 1)
        self.assertEqual((client.deleted, client.destroyed), ([ident], 1))
        clock[0] += SWEEP_SECONDS - 1
        self.assertEqual(sweeper.sweep_if_due(), 0)
        self.assertEqual(client.deleted, [ident])

    def test_a_failed_delete_never_reaches_the_controller_loop(self):
        self.run_row(self.job("done"))
        client = Client(fail=True)
        self.assertEqual(self.sweeper(client, [NOW]).sweep_if_due(), 0)
        self.assertEqual(client.destroyed, 1)


if __name__ == "__main__":
    unittest.main()
