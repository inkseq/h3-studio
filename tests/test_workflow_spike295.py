"""Offline checks; no application startup, SDK migration, DSN or cloud access."""

import asyncio
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "tools" / "workflow_spike295.py"
SPEC = importlib.util.spec_from_file_location("workflow_spike295", SOURCE)
spike = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike)


class SpikeTests(unittest.TestCase):
    def args(self, **overrides):
        values = dict(
            engine="dbos",
            command="worker",
            run_id="fixture",
            seconds=1,
            polling=5.0,
            pid=None,
            step_seconds=0,
            dead_pid=2147483647,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_immutable_completion_reuses_original_without_reexecuting_stub(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(spike.time, "sleep") as sleep:
                first = spike.stub(root, "run-a", "shot-0", 10)
                second = spike.stub(root, "run-a", "shot-0", 10)
                self.assertEqual(first, second)
                self.assertEqual(sleep.call_count, 1)
            self.assertEqual(len((root / "started.jsonl").read_text().splitlines()), 1)
            self.assertEqual(len(list((root / "outputs").iterdir())), 1)
            with self.assertRaisesRegex(spike.SpikeError, "identity_conflict"):
                spike.stub(root, "different-run", "shot-0", 0)

    def test_outputs_cover_three_shots_and_pipeline_plus_twenty_bulk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key in (*spike.STAGES, *(f"bulk-{index}" for index in range(20))):
                spike.stub(root, "fixture", key, 0)
            self.assertEqual(len(list((root / "outputs").iterdir())), 26)

    def test_unknown_original_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.json"
            target.write_text('{"unknown":true}')
            with self.assertRaises(spike.SpikeError):
                spike.write_once(target, {"accepted": True})
            self.assertEqual(json.loads(target.read_text()), {"unknown": True})

    def test_execute_count_includes_sync_async_and_batch_without_storing_queries(self):
        calls = []

        class Cursor:
            def execute(self, query, params=None, **kwargs):
                calls.append((query, params))

            def executemany(self, query, params, **kwargs):
                calls.append((query, params))

        class AsyncCursor:
            async def execute(self, query, params=None, **kwargs):
                calls.append((query, params))

            async def executemany(self, query, params, **kwargs):
                calls.append((query, params))

        meter = spike.QueryMeter()
        with patch.dict(
            "sys.modules",
            psycopg=SimpleNamespace(Cursor=Cursor, AsyncCursor=AsyncCursor),
        ):
            sync, asynchronous = meter.cursor_classes()
        sync().execute("not retained", ("not retained",))
        sync().executemany("batch", iter([(1,), (2,)]))
        asyncio.run(asynchronous().execute("LISTEN some_channel"))
        asyncio.run(asynchronous().executemany("batch", iter([(1,), (2,), (3,)])))
        self.assertEqual(meter.value(), 7)
        self.assertEqual(set(vars(meter)), {"lock", "executions"})
        self.assertEqual(len(calls), 4)

    def test_counter_is_thread_safe(self):
        meter = spike.QueryMeter()
        threads = [
            threading.Thread(target=lambda: [meter.count() for _ in range(1000)])
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(meter.value(), 4000)

    def test_database_tx_rate_is_never_relabelled_as_queries(self):
        before = dict(monotonic=0, transactions=100, statement_calls=None)
        after = dict(monotonic=30, transactions=160, statement_calls=None)
        value = spike.database_delta(before, after)
        self.assertIsNone(value["database_sql_calls_per_second"])
        self.assertEqual(value["database_transactions_per_second"], 2)
        before["statement_calls"], after["statement_calls"] = 200, 230
        self.assertEqual(
            spike.database_delta(before, after)["database_sql_calls_per_second"], 1
        )
        after["statement_calls"] = 5
        self.assertIsNone(
            spike.database_delta(before, after)["database_sql_calls_per_second"]
        )

    def test_elapsed_cpu_and_query_count_use_actual_window(self):
        value = spike.summarize(
            dict(monotonic=10, host_cpu_total=100, host_cpu_idle=40),
            dict(monotonic=18, host_cpu_total=200, host_cpu_idle=130),
            count_before=20,
            count_after=24,
        )
        self.assertEqual(value["measured_seconds"], 8)
        self.assertEqual(value["host_cpu_percent"], 10)
        self.assertEqual(value["worker_sql_execute_per_second"], 0.5)
        self.assertIsNone(value["whole_host_sql_qps"])

    def test_production_or_unapproved_run_never_loads_database_config(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"INKSEQ_DEPLOYMENT_TARGET": "production"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            root = Path(directory) / "workflow-spike295" / "fixture"
            result = spike.main(
                [
                    "initialize",
                    "--engine",
                    "dbos",
                    "--run-id",
                    "fixture",
                    "--work-dir",
                    str(root),
                    "--authorized-staging-spike",
                ]
            )
            self.assertEqual(result, 1)
            self.assertIn("staging_spike_scope_not_authorized", output.getvalue())
            self.assertFalse(root.exists())

    def test_recovery_requires_dead_original_worker_receipt_and_same_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                dead_pid=2147483647, run_id="original", engine="procrastinate"
            )
            spike.write_once(
                root / "worker-2147483647.json",
                dict(
                    pid=args.dead_pid,
                    run_id="original",
                    engine=args.engine,
                    version=spike.VERSION,
                ),
            )
            spike.recovered_worker(root, args)
            args.run_id = "different"
            with self.assertRaisesRegex(spike.SpikeError, "receipt_invalid"):
                spike.recovered_worker(root, args)

    @unittest.skipUnless(__import__("os").name == "posix", "Linux executor lock")
    def test_only_one_executor_across_engine_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "one"
            with spike.executor_lock(root):
                with self.assertRaisesRegex(spike.SpikeError, "already_running"):
                    with spike.executor_lock(root.parent / "two"):
                        self.fail("second executor acquired lock")

    def test_url_driver_conversion_preserves_encoded_credentials_and_options(self):
        value = (
            "postgresql+psycopg://local:u%40x@127.0.0.1:6551/fixture?sslmode=disable"
        )
        self.assertEqual(
            spike.driver_url(value),
            value.replace("postgresql+psycopg://", "postgresql://", 1),
        )
        with self.assertRaises(spike.SpikeError):
            spike.driver_url("sqlite:///not-allowed")

    def test_dbos_sync_registration_is_outside_running_async_loop(self):
        def register(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return SimpleNamespace(destroy=Mock()), SimpleNamespace(dispose=Mock())

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(spike.importlib.metadata, "version", return_value="3.3.0"),
            patch.dict(
                "sys.modules", dbos=SimpleNamespace(DBOS=object(), DBOSClient=object())
            ),
            patch.object(spike, "dbos_app", side_effect=register),
            patch.object(
                spike, "sample_window", new=AsyncMock(return_value={"sample": True})
            ),
            patch.object(spike.asyncio, "sleep", new=AsyncMock()),
        ):
            self.assertEqual(
                asyncio.run(spike.execute(self.args(), "unused", Path(directory))),
                {"sample": True},
            )

    def test_unknown_submission_never_constructs_or_calls_another_producer(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(spike.importlib.metadata, "version", return_value="3.3.0"),
        ):
            root = Path(directory)
            original = b'{"original":"unconfirmed"}'
            (root / "submission-request.json").write_bytes(original)
            with self.assertRaises(FileExistsError):
                asyncio.run(spike.execute(self.args(command="submit"), "unused", root))
            self.assertEqual((root / "submission-request.json").read_bytes(), original)

    def test_procrastinate_recovery_retries_same_original_ids_only(self):
        async def run(stalled):
            manager = SimpleNamespace(
                get_stalled_jobs=AsyncMock(return_value=stalled),
                retry_job_by_id_async=AsyncMock(),
            )
            app = SimpleNamespace(job_manager=manager)

            class Open:
                async def __aenter__(self):
                    return app

                async def __aexit__(self, *args):
                    return False

            app.open_async = Open
            enqueue = AsyncMock()
            with (
                patch.object(
                    spike.importlib.metadata, "version", return_value="3.10.0"
                ),
                patch.object(
                    spike,
                    "procrastinate_app",
                    new=AsyncMock(return_value=(app, spike.QUEUE, None, None, enqueue)),
                ),
                patch.object(spike, "recovered_worker") as original_worker,
            ):
                value = await spike.execute(
                    self.args(engine="procrastinate", command="recover"),
                    "unused",
                    Path("unused"),
                )
                original_worker.assert_called_once()
            enqueue.assert_not_called()
            self.assertEqual(value, {"requeued_original_ids": [17]})
            self.assertEqual(manager.retry_job_by_id_async.await_args.args, (17,))

        asyncio.run(
            run(
                [
                    SimpleNamespace(
                        id=17,
                        task_kwargs={"key": "shot-0", "original_run_id": "fixture"},
                    )
                ]
            )
        )

    def test_idle_window_with_any_fixture_transition_is_not_accepted(self):
        def sample(t):
            return dict(
                monotonic=t,
                host_cpu_total=100 + t * 10,
                host_cpu_idle=50 + t * 9,
                processes=[dict(pid=1, rss_bytes=100)],
            )

        before = dict(
            monotonic=0,
            transactions=0,
            statement_calls=None,
            sdk_work={"dbos": [21, 100, 0]},
        )
        after = dict(
            monotonic=1,
            transactions=1,
            statement_calls=None,
            sdk_work={"dbos": [22, 101, 0]},
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                spike, "host_sample", side_effect=[sample(0), sample(1), sample(2)]
            ),
            patch.object(spike.time, "monotonic", return_value=1),
            patch.object(spike, "database_snapshot", side_effect=[before, after]),
        ):
            with self.assertRaisesRegex(
                spike.SpikeError, "idle_window_had_fixture_activity"
            ):
                asyncio.run(
                    spike.sample_window(
                        Path(directory), seconds=1, idle=True, url="unused"
                    )
                )
            self.assertFalse(list(Path(directory).glob("resource-summary*")))


if __name__ == "__main__":
    unittest.main()
