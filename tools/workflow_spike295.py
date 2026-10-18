"""Held, staging-only stub benchmark; never imports the production controller.

No GPU, business jobs, budgets, provider clients or credentials are created.
The SDK schemas and fixture files are experimental state, not a second business
queue. Runtime dependencies are installed separately from the application image.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "inkseq-film-spike-v1"
QUEUE = "inkseq-spike295"
STAGES = ("shot-0", "shot-1", "shot-2", "chapter", "captions", "export")
SCHEMAS = {
    "dbos": "inkseq_spike295_dbos",
    "procrastinate": "inkseq_spike295_procrastinate",
}
PINS = {"dbos": "3.3.0", "procrastinate": "3.10.0"}


class SpikeError(Exception):
    pass


class QueryMeter:
    """Count public cursor.execute/executemany calls, never SQL/parameters.

    Transaction protocol frames are not relabelled as SQL queries. Initialization
    DDL is outside the empty-queue window. The adapter owns every SDK connection,
    including the raw LISTEN connection. This is worker-client SQL rate, not a
    claim about all other applications on the PostgreSQL host.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.executions = 0

    def count(self, executions=1):
        with self.lock:
            self.executions += executions

    def value(self):
        with self.lock:
            return self.executions

    def cursor_classes(self):
        import psycopg

        meter = self

        class SyncCursor(psycopg.Cursor):
            def execute(self, query, params=None, **kwargs):
                meter.count()
                return super().execute(query, params, **kwargs)

            def executemany(self, query, params_seq, **kwargs):
                params_seq = tuple(params_seq)
                meter.count(len(params_seq))
                return super().executemany(query, params_seq, **kwargs)

        class AsyncCursor(psycopg.AsyncCursor):
            async def execute(self, query, params=None, **kwargs):
                meter.count()
                return await super().execute(query, params, **kwargs)

            async def executemany(self, query, params_seq, **kwargs):
                params_seq = tuple(params_seq)
                meter.count(len(params_seq))
                return await super().executemany(query, params_seq, **kwargs)

        return SyncCursor, AsyncCursor


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value or ""):
        raise SpikeError("invalid_fixture_identity")
    return value


def write_once(path, value):
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    try:
        with path.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 1048576
            or path.read_bytes() != raw
        ):
            raise SpikeError("fixture_identity_conflict") from None
    return value


def stub(root, run_id, key, seconds):
    """Tiny immutable fixture output with deterministic identity; no media IO."""
    identifier(run_id)
    identifier(key)
    directory = root / "outputs"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or directory.resolve() != directory:
        raise SpikeError("fixture_output_path_invalid")
    result = dict(
        run_id=run_id,
        key=key,
        fixture_sha256=hashlib.sha256((run_id + ":" + key).encode()).hexdigest(),
    )
    target = directory / (key + ".json")
    if target.exists():
        return write_once(target, result)
    # A crash here leaves only a start observation; replay may execute the
    # in-flight stub again, but the immutable completion is never duplicated.
    with (root / "started.jsonl").open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(dict(key=key, started_at=time.time(), pid=os.getpid())) + "\n"
        )
        output.flush()
    time.sleep(seconds)
    return write_once(target, result)


def host_sample(pids):
    """Linux host CPU counters plus explicitly selected process RSS only."""
    fields = [
        int(value)
        for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    ]
    # guest/guest_nice are already included in user/nice.
    total, idle = sum(fields[:8]), fields[3] + fields[4]
    processes = []
    for pid in pids:
        status = Path("/proc") / str(pid) / "status"
        values = dict(
            line.split(":", 1)
            for line in status.read_text().splitlines()
            if ":" in line
        )
        processes.append(
            dict(pid=pid, rss_bytes=int(values["VmRSS"].split()[0]) * 1024)
        )
    return dict(
        monotonic=time.monotonic(),
        host_cpu_total=total,
        host_cpu_idle=idle,
        processes=processes,
    )


def summarize(first, last, *, count_before=None, count_after=None):
    elapsed = last["monotonic"] - first["monotonic"]
    total = last["host_cpu_total"] - first["host_cpu_total"]
    idle = last["host_cpu_idle"] - first["host_cpu_idle"]
    return dict(
        measured_seconds=elapsed,
        host_cpu_percent=100 * (total - idle) / total if total > 0 else None,
        worker_sql_execute_count=None
        if count_before is None
        else count_after - count_before,
        worker_sql_execute_per_second=None
        if count_before is None
        else (count_after - count_before) / elapsed,
        whole_host_sql_qps=None,
        whole_host_sql_qps_state="unknown_unless_external_pg_stat_statements_proof",
        rss_scope="explicit_pids",
        first=first,
        last=last,
    )


def database_snapshot(url):
    """Metadata-only observations; never install pg_stat_statements or reset it."""
    import psycopg
    from psycopg import sql

    result = dict(statement_calls=None, statement_state="not_installed_or_not_readable")
    with psycopg.connect(
        driver_url(url), application_name="inkseq-spike295-observer"
    ) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        result["transactions"] = connection.execute(
            "SELECT xact_commit + xact_rollback FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()[0]
        result["connections"] = connection.execute(
            "SELECT count(*), count(*) FILTER (WHERE application_name LIKE 'inkseq-spike295-%' "
            "AND application_name <> 'inkseq-spike295-observer') FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchone()
        result["sdk_tables"] = dict(
            connection.execute(
                "SELECT n.nspname, count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname IN ('inkseq_spike295_dbos','inkseq_spike295_procrastinate') "
                "AND c.relkind IN ('r','p') GROUP BY n.nspname"
            ).fetchall()
        )
        result["sdk_work"] = {}
        if SCHEMAS["dbos"] in result["sdk_tables"]:
            result["sdk_work"]["dbos"] = connection.execute(
                "SELECT count(*),coalesce(max(updated_at),0), "
                "count(*) FILTER (WHERE status IN ('ENQUEUED','PENDING','DELAYED')) "
                "FROM inkseq_spike295_dbos.workflow_status"
            ).fetchone()
        if SCHEMAS["procrastinate"] in result["sdk_tables"]:
            result["sdk_work"]["procrastinate"] = connection.execute(
                "SELECT count(*),(SELECT coalesce(max(id),0) FROM inkseq_spike295_procrastinate.procrastinate_events), "
                "count(*) FILTER (WHERE status IN ('todo','doing')) "
                "FROM inkseq_spike295_procrastinate.procrastinate_jobs"
            ).fetchone()
        extension = connection.execute(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace "
            "WHERE e.extname='pg_stat_statements'"
        ).fetchone()
        if extension:
            try:
                # A savepoint preserves the read-only observation if the module
                # exists but was not preloaded or the view cannot be read.
                with connection.transaction():
                    row = connection.execute(
                        sql.SQL(
                            "SELECT coalesce(sum(calls),0) FROM {}.pg_stat_statements "
                            "WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())"
                        ).format(sql.Identifier(extension[0]))
                    ).fetchone()
                    result.update(
                        statement_calls=int(row[0]), statement_state="readable"
                    )
            except psycopg.Error:
                pass
    result["monotonic"] = time.monotonic()
    return result


def database_delta(before, after):
    elapsed = after["monotonic"] - before["monotonic"]
    calls = [before["statement_calls"], after["statement_calls"]]
    valid = (
        elapsed > 0
        and all(value is not None for value in calls)
        and calls[1] >= calls[0]
    )
    transactions = after["transactions"] - before["transactions"]
    return dict(
        observation_seconds=elapsed,
        database_sql_calls_per_second=(calls[1] - calls[0]) / elapsed
        if valid
        else None,
        database_sql_calls_state="pg_stat_statements_includes_observer"
        if valid
        else "unknown_not_installed_unreadable_or_reset",
        database_transactions_per_second=transactions / elapsed
        if elapsed > 0 and transactions >= 0
        else None,
        transaction_scope="database_includes_observer_not_sql_qps",
        before=before,
        after=after,
    )


async def sample_window(root, *, seconds, meter=None, pids=None, idle=False, url=None):
    pids = pids or [os.getpid()]
    database_before = await asyncio.to_thread(database_snapshot, url) if url else None
    before = meter.value() if meter else None
    first = host_sample(pids)
    deadline = first["monotonic"] + seconds
    # This is a resource sampler, never a job-status/reconciliation polling loop.
    rss = []
    with (root / ("resource-samples-" + str(os.getpid()) + ".jsonl")).open(
        "x", encoding="utf-8"
    ) as output:
        while True:
            sample = host_sample(pids)
            rss.append(sum(process["rss_bytes"] for process in sample["processes"]))
            output.write(
                json.dumps(
                    dict(**sample, sql_execute_count=meter.value() if meter else None)
                )
                + "\n"
            )
            output.flush()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(5, remaining))
    result = summarize(
        first,
        host_sample(pids),
        count_before=before,
        count_after=meter.value() if meter else None,
    )
    result.update(
        rss_mean_bytes=sum(rss) / len(rss), rss_max_bytes=max(rss), samples=len(rss)
    )
    result["window_kind"] = "empty_queue" if idle else "functional_run_not_idle"
    if url:
        result["database"] = database_delta(
            database_before, await asyncio.to_thread(database_snapshot, url)
        )
        if idle and (
            database_before["sdk_work"] != result["database"]["after"]["sdk_work"]
            or any(row[2] for row in database_before["sdk_work"].values())
        ):
            raise SpikeError("idle_window_had_fixture_activity")
    return write_once(root / ("resource-summary-" + str(os.getpid()) + ".json"), result)


def driver_url(url):
    if not url.startswith("postgresql+psycopg://"):
        raise SpikeError("existing_explicit_postgresql_config_required")
    return "postgresql://" + url.removeprefix("postgresql+psycopg://")


def initialization_request(root, engine, url):
    import psycopg

    # Unknown initialization is retained, not retried against a partial schema.
    with (root / "initialization-request.json").open("x", encoding="utf-8") as output:
        json.dump(
            dict(engine=engine, schema=SCHEMAS[engine], version=PINS[engine]),
            output,
            sort_keys=True,
        )
    with psycopg.connect(driver_url(url)) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        if connection.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname=%s", (SCHEMAS[engine],)
        ).fetchone():
            raise SpikeError("experimental_schema_already_exists_inspect_original")


def dbos_engine(url, meter):
    import psycopg
    from sqlalchemy import create_engine

    sync_cursor, _ = meter.cursor_classes()
    conninfo = driver_url(url)
    return create_engine(
        url,
        hide_parameters=True,
        pool_size=5,
        max_overflow=0,
        creator=lambda: psycopg.connect(
            conninfo,
            cursor_factory=sync_cursor,
            application_name="inkseq-spike295-dbos",
            options="-c idle_in_transaction_session_timeout=60000",
        ),
    )


def dbos_app(url, run_id, meter, *, polling):
    from dbos import DBOS

    engine = dbos_engine(url, meter)
    DBOS(
        config=dict(
            name="inkseq-film-spike",
            application_version=VERSION,
            executor_id="inkseq-film-spike-staging",
            dbos_system_schema=SCHEMAS["dbos"],
            system_database_url=url,
            system_database_engine=engine,
            enable_otlp=False,
            log_level="CRITICAL",
            console_log_level="CRITICAL",
            run_migrations=False,
            max_executor_threads=4,
        )
    )

    @DBOS.step(name="spike295_stub_v1", retries_allowed=False)
    def step(root_path, original_run_id, key, seconds):
        return stub(Path(root_path), original_run_id, key, seconds)

    @DBOS.workflow(name="spike295_film_v1")
    def film(root_path, original_run_id, seconds):
        return [step(root_path, original_run_id, key, seconds) for key in STAGES]

    @DBOS.workflow(name="spike295_bulk_v1")
    def one(root_path, original_run_id, seconds, key):
        return step(root_path, original_run_id, key, seconds)

    DBOS.launch()
    DBOS.register_queue(
        QUEUE,
        global_concurrency=2,
        worker_concurrency=2,
        polling_interval_sec=polling,
    )
    return DBOS, engine


def dbos_idle_check(url):
    import psycopg

    with psycopg.connect(driver_url(url)) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        if connection.execute(
            "SELECT count(*) FROM inkseq_spike295_dbos.workflow_status "
            "WHERE status IN ('ENQUEUED','PENDING','DELAYED')"
        ).fetchone()[0]:
            raise SpikeError("idle_queue_not_empty")


def recovered_worker(root, args):
    path = root / ("worker-" + str(args.dead_pid) + ".json")
    if args.dead_pid is None or Path("/proc", str(args.dead_pid)).exists():
        raise SpikeError("original_worker_not_proved_exited")
    if path.is_symlink() or path.stat().st_size > 4096:
        raise SpikeError("original_worker_receipt_invalid")
    receipt = json.loads(path.read_bytes())
    if receipt != dict(
        pid=args.dead_pid, run_id=args.run_id, version=VERSION, engine=args.engine
    ):
        raise SpikeError("original_worker_receipt_invalid")


@contextmanager
def executor_lock(root):
    """One experimental executor across both SDKs/runs on this host."""
    import fcntl

    path = root.parent / "executor.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise SpikeError("executor_lock_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SpikeError("experimental_executor_already_running") from None
        yield
    finally:
        os.close(fd)


async def procrastinate_app(url, root, run_id, meter, *, step_seconds):
    import procrastinate

    _, async_cursor = meter.cursor_classes()
    conninfo = driver_url(url)
    connector = procrastinate.PsycopgConnector(
        conninfo=conninfo,
        min_size=1,
        max_size=5,
        kwargs=dict(
            cursor_factory=async_cursor,
            application_name="inkseq-spike295-procrastinate",
            options="-csearch_path=" + SCHEMAS["procrastinate"] + ",pg_catalog",
        ),
    )
    app = procrastinate.App(connector=connector)
    queue = QUEUE

    async def enqueue_once(task, key, **kwargs):
        existing = [
            job
            for job in await app.job_manager.list_jobs_async(queue=queue)
            if job.task_kwargs.get("key") == key
            and job.task_name == task.name
            and job.task_kwargs.get("original_run_id") == run_id
        ]
        if len(existing) > 1:
            raise SpikeError("fixture_duplicate_task_requires_review")
        if existing:
            return existing[0].id
        return await task.configure(
            queue=queue, queueing_lock=run_id + ":" + key
        ).defer_async(key=key, original_run_id=run_id, **kwargs)

    @app.task(name="spike295_film_stage_v1", queue=queue, retry=False)
    async def film_stage(key, index, original_run_id):
        if original_run_id != run_id:
            raise SpikeError("original_fixture_worker_mismatch")
        await asyncio.to_thread(stub, root, run_id, key, step_seconds)
        if index + 1 < len(STAGES):
            await enqueue_once(film_stage, STAGES[index + 1], index=index + 1)

    @app.task(name="spike295_bulk_v1", queue=queue, retry=False)
    async def one(key, original_run_id):
        if original_run_id != run_id:
            raise SpikeError("original_fixture_worker_mismatch")
        await asyncio.to_thread(stub, root, run_id, key, step_seconds)

    return app, queue, film_stage, one, enqueue_once


async def execute(args, url, root):
    meter = QueryMeter()
    if args.engine == "baseline":
        return await sample_window(
            root,
            seconds=args.seconds,
            pids=args.pid,
            idle=args.command == "idle",
            url=url,
        )
    if importlib.metadata.version(args.engine) != PINS[args.engine]:
        raise SpikeError("sdk_pin_mismatch")
    request = dict(
        version=VERSION,
        engine=args.engine,
        run_id=args.run_id,
        stages=STAGES,
        bulk=["bulk-" + str(index) for index in range(20)],
    )
    if args.command == "submit":
        # An existing receipt is inspection-only. Never replay a producer after
        # an uncertain response; the worker resumes its original accepted jobs.
        with (root / "submission-request.json").open("x", encoding="utf-8") as output:
            json.dump(request, output, sort_keys=True)
    if args.engine == "dbos":
        from dbos import DBOS, DBOSClient

        if args.command == "initialize":
            await asyncio.to_thread(initialization_request, root, args.engine, url)
            DBOS.migrate(url, schema=SCHEMAS["dbos"])
            return dict(initialized=True, engine="dbos")
        if args.command in {"worker", "idle"}:
            if args.command == "idle":
                await asyncio.to_thread(dbos_idle_check, url)
            # Pinned DBOS sync registration rejects a running asyncio loop.
            DBOS, engine = await asyncio.to_thread(
                dbos_app, url, args.run_id, meter, polling=args.polling or 1.0
            )
            try:
                await asyncio.sleep(1)
                return await sample_window(
                    root,
                    seconds=args.seconds,
                    meter=meter,
                    idle=args.command == "idle",
                    url=url,
                    pids=[os.getpid(), *(args.pid or [])],
                )
            finally:
                await asyncio.to_thread(DBOS.destroy)
                engine.dispose()
        engine = dbos_engine(url, meter)
        client = DBOSClient(
            system_database_url=url,
            system_database_engine=engine,
            dbos_system_schema=SCHEMAS["dbos"],
            application_name="inkseq-film-spike",
            retry_connection_errors=False,
        )
        try:
            if args.command == "submit":
                # Client enqueues only: a producer is never a second executor.
                client.register_queue(
                    QUEUE,
                    global_concurrency=2,
                    worker_concurrency=2,
                    polling_interval_sec=args.polling or 1.0,
                )
                ids = []
                for key in ("film", *request["bulk"]):
                    handle = client.enqueue(
                        dict(
                            workflow_name="spike295_film_v1"
                            if key == "film"
                            else "spike295_bulk_v1",
                            queue_name=QUEUE,
                            workflow_id="film:" + args.run_id + ":" + key,
                            app_version=VERSION,
                            application_name="inkseq-film-spike",
                        ),
                        str(root),
                        args.run_id,
                        args.step_seconds,
                        *(() if key == "film" else (key,)),
                    )
                    ids.append(handle.get_workflow_id())
                return write_once(
                    root / "submitted.json", dict(**request, workflow_ids=ids)
                )
            if args.command == "status":
                ids = [
                    "film:" + args.run_id + ":" + key
                    for key in ("film", *request["bulk"])
                ]
                statuses = []
                for key in ids:
                    status = client.retrieve_workflow(key).get_status()
                    statuses.append(
                        dict(
                            id=key,
                            status=status.status,
                            claim_latency_seconds=(
                                status.dequeued_at - status.created_at
                            )
                            / 1000
                            if status.dequeued_at is not None
                            and status.created_at is not None
                            else None,
                            recovery_attempts=status.recovery_attempts,
                        )
                    )
                return dict(statuses=statuses)
        finally:
            client.destroy()
            engine.dispose()
    app, queue, film_stage, one, enqueue_once = await procrastinate_app(
        url, root, args.run_id, meter, step_seconds=args.step_seconds
    )
    async with app.open_async():
        if args.command == "initialize":
            from psycopg import sql

            # Explicit reviewed initialization only; no worker startup DDL.
            await asyncio.to_thread(initialization_request, root, args.engine, url)
            await app.connector.execute_query_async(
                query=sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(SCHEMAS["procrastinate"])
                )
            )
            await app.schema_manager.apply_schema_async()
            return dict(initialized=True, engine="procrastinate")
        if args.command == "submit":
            ids = [await enqueue_once(film_stage, STAGES[0], index=0)]
            ids += [await enqueue_once(one, key) for key in request["bulk"]]
            return write_once(root / "submitted.json", dict(**request, job_ids=ids))
        if args.command == "status":
            jobs = [
                job
                for job in await app.job_manager.list_jobs_async(queue=queue)
                if job.task_kwargs.get("original_run_id") == args.run_id
            ]
            latencies = await app.connector.execute_query_all_async(
                query="""
                SELECT j.id, extract(epoch FROM (min(e.at) FILTER (WHERE e.type='started') -
                    min(e.at) FILTER (WHERE e.type='deferred'))) AS claim_seconds
                FROM procrastinate_jobs j JOIN procrastinate_events e ON e.job_id=j.id
                WHERE j.queue_name=%(queue)s AND j.args->>'original_run_id'=%(run_id)s
                GROUP BY j.id
            """,
                queue=queue,
                run_id=args.run_id,
            )
            by_id = {
                row["id"]: float(row["claim_seconds"])
                if row["claim_seconds"] is not None
                else None
                for row in latencies
            }
            return dict(
                statuses=[
                    dict(
                        id=job.id,
                        key=job.task_kwargs.get("key"),
                        status=job.status,
                        claim_latency_seconds=by_id.get(job.id),
                    )
                    for job in jobs
                ]
            )
        if args.command == "recover":
            recovered_worker(root, args)
            stalled = list(await app.job_manager.get_stalled_jobs(queue=queue))
            if len(stalled) > 21:
                raise SpikeError("unexpected_stalled_population")
            for job in stalled:
                if (
                    job.task_kwargs.get("key") not in (*STAGES, *request["bulk"])
                    or job.task_kwargs.get("original_run_id") != args.run_id
                ):
                    raise SpikeError("unexpected_stalled_identity")
            for job in stalled:
                await app.job_manager.retry_job_by_id_async(
                    job.id, retry_at=datetime.now(timezone.utc)
                )
            return dict(requeued_original_ids=[job.id for job in stalled])
        if args.command == "idle":
            if any(
                job.status in {"todo", "doing"}
                for job in await app.job_manager.list_jobs_async(queue=queue)
            ):
                raise SpikeError("idle_queue_not_empty")
        worker = asyncio.create_task(
            app.run_worker_async(
                queues=[queue],
                concurrency=2,
                name="inkseq-film-spike-staging",
                fetch_job_polling_interval=args.polling or 5.0,
                abort_job_polling_interval=args.polling or 5.0,
                install_signal_handlers=False,
                delete_jobs="never",
            )
        )
        try:
            await asyncio.sleep(1)
            if worker.done():
                await worker
                raise SpikeError("worker_startup_failed")
            result = await sample_window(
                root,
                seconds=args.seconds,
                meter=meter,
                idle=args.command == "idle",
                url=url,
                pids=[os.getpid(), *(args.pid or [])],
            )
            if worker.done():
                await worker
                raise SpikeError("worker_ended_during_resource_window")
            return result
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Held staging-only no-GPU workflow resource spike"
    )
    parser.add_argument(
        "command",
        choices=("initialize", "submit", "worker", "idle", "status", "recover"),
    )
    parser.add_argument(
        "--engine", required=True, choices=("baseline", "dbos", "procrastinate")
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--polling", type=float, choices=(1.0, 5.0, 10.0))
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--step-seconds", type=float, default=0.1)
    parser.add_argument("--dead-pid", type=int)
    parser.add_argument("--pid", type=int, action="append")
    parser.add_argument("--authorized-staging-spike", action="store_true")
    args = parser.parse_args(argv)
    try:
        identifier(args.run_id)
        if (
            not args.authorized_staging_spike
            or os.environ.get("INKSEQ_DEPLOYMENT_TARGET") != "staging"
            or not args.work_dir.is_absolute()
            or args.work_dir.is_symlink()
            or args.work_dir.parent.name != "workflow-spike295"
            or args.work_dir.name != args.run_id
            or ".." in args.work_dir.parts
            or not 1 <= args.seconds <= 3600
            or not 0 <= args.step_seconds <= 30
            or any(pid <= 0 for pid in (args.pid or []))
            or args.engine == "baseline"
            and args.command != "idle"
            or args.engine == "dbos"
            and args.command == "recover"
        ):
            raise SpikeError("staging_spike_scope_not_authorized")
        args.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.work_dir.resolve() != args.work_dir:
            raise SpikeError("fixture_path_must_be_canonical")
        if os.name == "posix" and stat.S_IMODE(args.work_dir.stat().st_mode) & 0o077:
            raise SpikeError("fixture_path_must_be_private")
        logging.disable(logging.CRITICAL)
        from studio_platform.settings import database_url_from_environment

        url = database_url_from_environment()
        if args.engine != "baseline" and not url.startswith("postgresql+psycopg://"):
            raise SpikeError("existing_explicit_postgresql_config_required")
        if args.command in {"initialize", "worker", "idle", "recover"}:
            with executor_lock(args.work_dir):
                if args.command in {"worker", "idle"}:
                    write_once(
                        args.work_dir / ("worker-" + str(os.getpid()) + ".json"),
                        dict(
                            pid=os.getpid(),
                            run_id=args.run_id,
                            version=VERSION,
                            engine=args.engine,
                        ),
                    )
                result = asyncio.run(execute(args, url, args.work_dir))
        else:
            result = asyncio.run(execute(args, url, args.work_dir))
        print(json.dumps(result, sort_keys=True))
        return 0
    except SpikeError as error:
        print(json.dumps(dict(code=str(error))))
        return 1
    except Exception:
        print(json.dumps(dict(code="workflow_spike_failed_inspect_original_no_retry")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
