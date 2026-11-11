"""Capacity timeline, provider snapshots, incident export and new alerts (#1140).

Recording only: nothing here starts, stops or funds a machine. The round trip
runs a simulator scenario, exports one machine from its throwaway ledger with
the real export code, replays the export and compares the outcome.
"""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from sqlalchemy import select

from studio_platform import capacity_reasons
from studio_platform.capacity_health import download_overdue, evaluate
from studio_platform.capacity_incident_export import build_fixture, read_incident
from studio_platform.capacity_snapshots import (
    RecordingClient,
    SnapshotBuffer,
    error_code,
    inventory_presence,
    install,
    safe_text,
    sanitize_run,
)
from studio_platform.capacity_timeline import (
    DEMAND_BURST,
    EVENT,
    MAX_ENTRIES,
    CapacityRecorder,
    demand_key,
)
from studio_platform.dstack_request import DstackError
from studio_platform.error_monitoring import report_capacity_health, sanitized_event
from studio_platform.repository import Repository, outbox

# Synthetic token shapes, assembled at runtime so no literal looks like a key.
API_TOKEN = "tok" + "_" + "Ab1" * 10
HUB_TOKEN = "hub" + "_" + "Z9y" * 12
SECRETS = (
    "203.0.113.7",
    "10.0.0.12",
    "2001:db8::1",
    "ops@example.com",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey",
    API_TOKEN,
    HUB_TOKEN,
    "inkseq-staging-account",
    "root",
    "s3cr3t-password",
    "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
)


def provider_run():
    """A dstack run document with every kind of private field it can carry."""
    return {
        "id": "ddd4eff2-c7d9-49d5-8d60-f5670333fad8",
        "run_name": "sixnine-57e00117d27b4c8e99902b251c951efa",
        "project_name": "inkseq-staging-account",
        "user": "ops@example.com",
        "status": "running",
        "termination_reason": None,
        "termination_reason_message": "token %s rejected" % API_TOKEN,
        "cost": 0.42,
        "run_spec": {
            "ssh_key_pub": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey",
            "configuration": {
                "env": {"HF_TOKEN": HUB_TOKEN},
                "registry_auth": {"password": "s3cr3t-password"},
            },
        },
        "latest_job_submission": {
            "status": "running",
            "exit_status": None,
            "termination_reason": None,
            "job_provisioning_data": {
                "backend": "vastai",
                "instance_id": "4300000",
                "region": "us-west",
                "price": 0.62,
                "hostname": "203.0.113.7",
                "internal_ip": "10.0.0.12",
                "ssh_port": 22,
                "username": "root",
                "ssh_proxy": {"hostname": "2001:db8::1", "username": "root"},
                "backend_data": '{"api_key": "Bearer abcdefghijklmnopqrstuvwxyz0123456789"}',
                "instance_type": {
                    "name": "10000",
                    "resources": {"gpus": [{"name": "RTX5090"}], "spot": False},
                },
            },
        },
    }


class SanitizationTests(unittest.TestCase):
    def test_run_snapshot_keeps_identity_and_drops_every_secret(self):
        value = sanitize_run(provider_run())
        text = json.dumps(value)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        for key in ("env", "ssh", "hostname", "username", "project_name", "user"):
            self.assertNotIn('"%s' % key, text)
        data = value["latest_job_submission"]["job_provisioning_data"]
        self.assertEqual(value["status"], "running")
        self.assertEqual(data["instance_id"], "4300000")
        self.assertEqual(data["region"], "us-west")
        self.assertEqual(data["price"], 0.62)
        self.assertEqual(data["instance_type"]["gpus"], ["RTX5090"])
        # The intent hex in a run name looks like a token and is dropped too.
        self.assertNotIn("run_name", value)

    def test_text_patterns(self):
        # Names (accounts, users) are excluded by the field allowlist; values
        # are checked by pattern for addresses, emails, keys and tokens.
        for secret in SECRETS:
            if secret not in ("inkseq-staging-account", "root", "s3cr3t-password"):
                self.assertIsNone(safe_text(secret), secret)
        for plain in ("dstack_download_too_slow", "us-west", "RTX5090", "4300000"):
            self.assertEqual(safe_text(plain), plain)

    def test_errors_keep_codes_or_types_never_messages(self):
        self.assertEqual(
            error_code(DstackError("dstack_api_unavailable")), "dstack_api_unavailable"
        )
        self.assertEqual(
            error_code(RuntimeError("connect to 203.0.113.7 with token abc failed")),
            "RuntimeError",
        )

    def test_recording_client_is_transparent(self):
        run = provider_run()
        inner = Mock()
        inner.get.return_value = run
        inner.provider_inventory.side_effect = DstackError(
            "dstack_provider_inventory_unavailable"
        )
        inner.project = "sixnine"
        buffer = SnapshotBuffer(lambda: 100.0)
        client = RecordingClient(inner, buffer)
        self.assertIs(client.get(run_name="x"), run)
        with self.assertRaises(DstackError) as raised:
            client.provider_inventory("vastai")
        self.assertIs(raised.exception, inner.provider_inventory.side_effect)
        self.assertEqual(client.project, "sixnine")
        taken = buffer.take(None, run["id"])
        self.assertEqual(len(taken), 1)
        self.assertNotIn("203.0.113.7", json.dumps(taken))
        self.assertEqual(
            inventory_presence(buffer.inventory("vastai"), "1", 100.0),
            {"backend": "vastai", "error": "dstack_provider_inventory_unavailable"},
        )

    def test_install_wraps_once(self):
        service = SimpleNamespace(
            capacity=SimpleNamespace(client=Mock()), provider_inventory=Mock()
        )
        first = install(service, lambda: 1.0)
        client, reader = service.capacity.client, service.provider_inventory
        self.assertIs(install(service, lambda: 1.0), first)
        self.assertIs(service.capacity.client, client)
        self.assertIs(service.provider_inventory, reader)


class RecorderBoundsTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.repo = Repository("sqlite://", clock=lambda: self.now)
        self.repo.create_schema()
        service = SimpleNamespace(
            repo=self.repo,
            settings=SimpleNamespace(tenant_id="tenant"),
            capacity=SimpleNamespace(client=Mock()),
            provider_inventory=None,
        )
        self.recorder = CapacityRecorder(service, Mock())

    def rows(self, key):
        with self.repo.engine.connect() as connection:
            return [
                row
                for row in connection.execute(
                    select(outbox.c.payload)
                    .where(outbox.c.event_type == EVENT, outbox.c.aggregate_id == key)
                    .order_by(outbox.c.created_at)
                ).scalars()
            ]

    def test_demand_decisions_are_written_on_change_with_bursts_summarised(self):
        key = demand_key("tenant")
        for step in range(40):
            self.now += 10
            code = ("capacity_limit", "backoff")[step % 2]
            self.recorder.record_tick(
                {"demand_start": {"state": "blocked", "reason_code": code}}
            )
        rows = self.rows(key)
        changes = [row for row in rows if row["code"] != "demand_summary"]
        summaries = [row for row in rows if row["code"] == "demand_summary"]
        self.assertLessEqual(len(changes), DEMAND_BURST * 2)
        self.assertTrue(summaries)
        self.assertEqual(
            set(summaries[0]["detail"]["counts"]), {"capacity_limit", "backoff"}
        )
        # An unchanged decision writes nothing.
        before = len(self.rows(key))
        for _ in range(5):
            self.now += 1
            self.recorder.record_tick(
                {"demand_start": {"state": "blocked", "reason_code": "backoff"}}
            )
        self.assertLessEqual(len(self.rows(key)) - before, 1)

    def test_entries_per_machine_are_capped(self):
        from studio_platform.capacity_timeline import _Track

        key = "intent-1"

        self.recorder._tracks[key] = _Track()
        for index in range(MAX_ENTRIES + 50):
            self.recorder._entry(key, "boot", "code_%d" % index, self.now, index)
        self.recorder._flush(self.now)
        rows = self.rows(key)
        self.assertEqual(len(rows), MAX_ENTRIES)
        self.assertEqual(rows[-1]["code"], "timeline_truncated")

    def test_refusals_by_worker_are_counted_and_drained(self):
        capacity_reasons.drain_workers()
        capacity_reasons.count("claim", "worker_window_closed", "dstack-abc")
        capacity_reasons.count(
            "defer", "managed_dstack_readiness_unconfirmed", "dstack-abc"
        )
        capacity_reasons.count("claim", "no_worker_for_pool")
        self.assertEqual(
            capacity_reasons.drain_workers(),
            {
                "dstack-abc": {
                    "claim:worker_window_closed": 1,
                    "defer:managed_dstack_readiness_unconfirmed": 1,
                }
            },
        )
        self.assertEqual(capacity_reasons.drain_workers(), {})
        capacity_reasons.drain()

    def test_a_failing_read_never_breaks_the_tick(self):
        self.recorder.repo = Mock(
            clock=lambda: 1.0, engine=Mock(connect=Mock(side_effect=OSError))
        )
        with self.assertLogs("studio_platform.capacity_timeline", "WARNING"):
            self.recorder.record_tick({"demand_start": {"state": "idle"}})


class AlertTests(unittest.TestCase):
    def binding(self, **progress):
        value = {
            "valid": True,
            "started_at": 0,
            "bytes_total": 52 * 1000**3,
            "bytes_complete": 10 * 1000**3,
            "completed_at": None,
            "last_progress_at": 1350,
            "last_observed_at": 1350,
        }
        value.update(progress)
        return {"download_progress": value}

    def test_download_overdue(self):
        # 52 GB at the 50 MB/s floor is 1040 s, plus 300 s slack.
        self.assertIsNone(download_overdue(self.binding(), 1300))
        self.assertEqual(download_overdue(self.binding(), 1400), (0, "download_slow"))
        self.assertIsNone(download_overdue({"download_progress": None}, 1400))
        self.assertEqual(
            download_overdue(self.binding(last_progress_at=900), 1400),
            (0, "download_stalled"),
        )
        self.assertEqual(
            download_overdue(self.binding(last_observed_at=900), 1400),
            (0, "download_unobserved"),
        )
        self.assertIsNone(download_overdue(self.binding(completed_at=1200), 5000))

    def test_overdue_download_is_one_finding_per_episode(self):
        node = {
            "intent_id": "a" * 32,
            "provider": "vast",
            "desired_state": "running",
            "runtime_state": "starting",
            "payload": {"dstack": {"apply_started": True, **self.binding()}},
        }
        findings = evaluate([node], [], [], [], now=1400)
        self.assertEqual(
            [(f["code"], f["diagnosis"], f["since"]) for f in findings],
            [("capacity_download_overdue", "download_slow", 0)],
        )
        later = evaluate([node], [], [], [], now=1600)
        self.assertEqual(findings[0]["episode"], later[0]["episode"])

    def test_sentry_carries_the_exact_reason_and_nothing_else(self):
        client = Mock()
        report_capacity_health(
            client,
            {
                "event_id": "0" * 32,
                "code": "capacity_start_blocked",
                "diagnosis": "start_blocked",
                "detail": "dstack_test_task_limit_reached",
            },
        )
        event = client.capture_event.call_args.args[0]
        self.assertEqual(
            sanitized_event(dict(event))["tags"],
            {
                "code": "capacity_start_blocked",
                "diagnosis": "start_blocked",
                "detail": "dstack_test_task_limit_reached",
            },
        )
        for diagnosis in ("worker_unreachable", "download_stalled"):
            event["tags"] = {"code": "capacity_paid_idle_queue", "diagnosis": diagnosis}
            self.assertIsNotNone(sanitized_event(dict(event)))
        event["tags"] = {
            "code": "capacity_start_blocked",
            "diagnosis": "start_blocked",
            "detail": "user ops@example.com",
        }
        self.assertIsNone(sanitized_event(dict(event)))


class TimelineCliTests(unittest.TestCase):
    def test_prints_the_timeline_and_where_it_got_stuck(self):
        import io
        import os
        from unittest.mock import patch

        from studio_platform.capacity_timeline_cli import main

        with tempfile.TemporaryDirectory() as folder:
            url = "sqlite:///" + str(Path(folder) / "ledger.sqlite3")
            repo = Repository(url)
            repo.create_schema()
            now = 1_800_000_000.0
            intent = "0123abcd-0000-4000-8000-000000000001"
            entries = [
                ("start", "start_requested", 0, {}),
                ("offer", "offer_chosen", 30, {"region": "us-west"}),
                ("allocation", "allocated", 60, {}),
                ("run", "run_running", 90, {}),
                ("download", "download_progress", 120, {"percent": 10.0}),
                ("download", "download_stalled", 900, {"percent": 40.0}),
            ]
            with repo.transaction() as connection:
                for index, (stage, code, offset, detail) in enumerate(entries):
                    connection.execute(
                        outbox.insert().values(
                            id="e%d" % index,
                            event_type=EVENT,
                            aggregate_id=intent,
                            payload={
                                "version": 1,
                                "stage": stage,
                                "code": code,
                                "at": now + offset,
                                "detail": detail,
                            },
                            created_at=now + offset,
                        )
                    )
            repo.close()
            out = io.StringIO()
            environment = {"SIXNINE_DATABASE_URL": url, "SIXNINE_DATABASE_URL_FILE": ""}
            with patch.dict(os.environ, environment):
                self.assertEqual(main([intent[:8]], out=out), 0)
                text = out.getvalue()
                self.assertIn("download_stalled", text)
                self.assertIn("stuck before: model download", text)
                recent = io.StringIO()
                main(["--recent", "5"], out=recent)
                self.assertIn(intent, recent.getvalue())

    def test_a_machine_the_provider_never_had_reads_as_closed(self):
        # #1214: such a machine has nothing to remove; it is not "still waiting".
        from studio_platform.capacity_timeline_cli import TIMELINE, diagnose

        def entry(code, at, detail=None):
            return {"_type": TIMELINE, "code": code, "at": at, "detail": detail or {}}

        entries = [
            entry("start_requested", 0),
            entry(
                "stop_requested",
                300,
                {"reason": "dstack_never_allocated", "claims": 0},
            ),
            entry("no_allocation_confirmed", 300),
            entry("intent_destroyed", 300),
            entry("timeline_closed", 300),
        ]
        lines = diagnose(entries, 14_000)
        self.assertTrue(lines[0].startswith("stopped: dstack_never_allocated"))
        self.assertIn("closed: the provider had no machine; nothing was charged", lines)
        self.assertFalse(any("still waiting" in line for line in lines))
        # A machine that was allocated still waits for its removal.
        del entries[2]
        self.assertTrue(
            any(
                line.startswith("still waiting for: provider removal")
                for line in diagnose(entries, 14_000)
            )
        )


class ExportTests(unittest.TestCase):
    def test_run_read_errors_become_windows(self):
        from studio_platform.capacity_incident_export import _read_errors

        snapshots = [
            {"kind": "run", "at": 100, "data": {"status": "running"}},
            {"kind": "inventory", "at": 130, "data": {"present": True}},
            {"kind": "run", "at": 160, "data": {"error": "dstack_api_unavailable"}},
            {"kind": "run", "at": 400, "data": {"status": "running"}},
            {"kind": "run", "at": 500, "data": {"error": "TimeoutError"}},
        ]
        self.assertEqual(
            _read_errors(snapshots, 100),
            [[60, 300, "dstack_api_unavailable"], [400, 4000, "TimeoutError"]],
        )

    def test_a_timeline_without_a_start_is_refused(self):
        with self.assertRaises(ValueError):
            build_fixture(
                {"intent_id": "x" * 36, "timeline": [], "snapshots": [], "attempts": []}
            )


class IncidentRoundTripTests(unittest.TestCase):
    """Simulate, export one machine, replay the export: same stop and claims."""

    def round_trip(self, scenario):
        from capacity_sim import incident_scenario, run_scenario

        def export(world):
            first = next(iter(world.client.machines.values()))
            with world.repo.engine.connect() as connection:
                incident = read_incident(connection, first.intent_id)
            return build_fixture(incident, name="replay_" + scenario.name)

        original = run_scenario(scenario, inspect=export)
        fixture = original["inspect"]
        text = json.dumps(fixture)
        self.assertNotIn("superdan", text)
        self.assertNotIn(original["outcomes"][0]["intent_id"], text)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "incident_replay.json"
            path.write_text(text, encoding="utf-8")
            replay = run_scenario(incident_scenario(path))
        recorded = fixture["recorded"]["outcome"]
        first = original["outcomes"][0]
        self.assertEqual(
            (recorded["stop_reason"], recorded["claims"]),
            (first["stop_reason"], first["claims"]),
        )
        again = replay["outcomes"][0]
        self.assertEqual(
            (again["stop_reason"], again["claims"]),
            (first["stop_reason"], first["claims"]),
            json.dumps(fixture["machine"]),
        )
        return fixture

    def test_worker_lost_machine_replays_the_same(self):
        from capacity_sim import JobBatch, MachineScript, Scenario

        fixture = self.round_trip(
            Scenario(
                "roundtrip_worker_lost",
                "One machine whose worker goes silent with work queued.",
                hours=3,
                max_machines=1,
                jobs=[JobBatch(0, 6, actual_runtime_s=300)],
                machines=[MachineScript(worker_lost_at=20 * 60)],
            )
        )
        self.assertIn("worker_lost_at", fixture["machine"])

    def test_stalled_download_replays_the_same(self):
        from capacity_sim import JobBatch, MachineScript, Scenario

        fixture = self.round_trip(
            Scenario(
                "roundtrip_download_stall",
                "One machine whose download stops at 40 percent.",
                hours=2,
                max_machines=1,
                jobs=[JobBatch(0, 2)],
                machines=[MachineScript(download_stall_at=0.4)],
            )
        )
        self.assertEqual(fixture["machine"]["download_stall_at"], 0.4)


if __name__ == "__main__":
    unittest.main()
