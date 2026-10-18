"""Bounded local CLI: no provider, schema creation, real generation or daemon."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

from sqlalchemy import event

from studio_platform import capacity_cli
import test_platform_capacity as capacity_tests


class CapacityCliTests(capacity_tests.CapacityTests):
    def environment(self):
        return {"SIXNINE_DATABASE_URL": self.url, "SIXNINE_DATABASE_URL_FILE": "",
            "SIXNINE_GENERATION_ENABLED": "1", "SIXNINE_EXECUTION_BACKEND": "comfy-worker",
            "SIXNINE_EXECUTION_POLICY_FILE": str(self.path), "SIXNINE_PUBLIC_ORIGIN": "",
            "SIXNINE_DATA": str(Path(self.temp.name)/"cli-data"), "SIXNINE_AUTH_MODE": "local-test"}

    def test_disabled_exits_before_settings_database_policy_or_cloud_import(self):
        from studio_platform.settings import Settings
        with patch.object(Settings, "from_environment", side_effect=AssertionError("disabled read settings")), \
                patch("studio_platform.repository.Repository", side_effect=AssertionError("disabled opens DB")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(capacity_cli.main([]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "disabled")

    def test_dryrun_only_reads_existing_schema_and_does_not_mutate_waiters(self):
        self.approve()
        job = self.waiting()
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            result = self.controller.preview("cold-approval")
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(result["waiting_count"], 1)
        self.assertTrue(result["approval_current"])
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in statements), statements)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "waiting_capacity")
        self.assertEqual(self.repo.list_instance_intents(), [])
        self.assertEqual(self.provider.creates, [])
        self.assertNotIn("payload", result)

    def test_advance_consumes_ready_fleet_registration_without_scaler_or_provider(self):
        self.approve()
        job = self.waiting()
        intent = self.start()
        self.worker(intent)
        with patch.object(self.scaler, "tick", side_effect=AssertionError("advance invoked scaler")), \
                patch.object(self.provider, "reconcile", side_effect=AssertionError("advance provider")):
            result = self.controller.advance_once("cold-approval")
        self.assertEqual(result["activated"], 1)
        self.assertFalse(result["provider_calls_enabled"])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")

    def test_cli_data_dir_preserves_explicit_database_file_and_never_runs_ddl(self):
        self.approve()
        self.waiting()
        # Synthetic test DSN only, never a real credential file.
        filename = Path(self.temp.name)/"fake-runtime-dsn.txt"
        filename.write_text(self.url, encoding="utf-8")
        filename.chmod(0o600)
        env = self.environment()
        env.update(SIXNINE_DATABASE_URL="", SIXNINE_DATABASE_URL_FILE=str(filename))
        output = io.StringIO()
        from studio_platform.repository import Repository
        original_clock = self.repo.clock
        # CLI has its own repo; pin its clock to the synthetic approval time.
        original_init = Repository.__init__
        def init(repo, url, **kwargs):
            return original_init(repo, url, clock=original_clock)
        with patch.dict(os.environ, env), patch.object(Repository, "__init__", init), \
                patch.object(Repository, "create_schema", side_effect=AssertionError("CLI did DDL")), \
                patch("studio_platform.scaler.ScaleCoordinator", side_effect=AssertionError("CLI built scaler")), \
                redirect_stdout(output):
            result = capacity_cli.main(["--mode", "dry-run", "--approval-id", "cold-approval",
                "--data-dir", str(Path(self.temp.name)/"different-data")])
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["waiting_count"], 1)
        self.assertNotIn(self.url, output.getvalue())
        self.assertFalse((Path(self.temp.name)/"different-data"/"platform.sqlite3").exists())

    def test_missing_sqlite_db_is_not_silently_created_and_errors_do_not_print_dsn(self):
        missing = Path(self.temp.name)/"missing.sqlite3"
        env = self.environment()
        env["SIXNINE_DATABASE_URL"] = "sqlite:///"+missing.as_posix()
        output = io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(output):
            result = capacity_cli.main(["--mode", "dry-run", "--approval-id", "cold-approval"])
        self.assertEqual(result, 1)
        self.assertFalse(missing.exists())
        self.assertNotIn(str(missing), output.getvalue())

    def test_loop_stops_on_term_event_between_bounded_turns(self):
        self.approve()
        self.waiting()
        stopped, output = threading.Event(), []
        def emit(value):
            output.append(json.loads(value))
            stopped.set()  # equivalent to SIGTERM callback; no real long daemon
        self.assertEqual(capacity_cli.run(self.controller, ["cold-approval"], mode="dry-run", loop=True,
            interval_s=15, stop_event=stopped, emit=emit), 0)
        self.assertEqual(len(output), 1)
        self.assertEqual(self.provider.creates, [])


for _name in dir(capacity_tests.CapacityTests):
    if _name.startswith("test_") and _name not in CapacityCliTests.__dict__:
        setattr(CapacityCliTests, _name, None)

if __name__ == "__main__":
    import unittest
    unittest.main()
