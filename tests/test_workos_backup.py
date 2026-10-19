"""Synthetic hosted identity metadata through the original backup transport."""

import copy
import json
import shutil
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform import backup, backup_remote, workos_backup
import test_platform_backup as fixtures
import test_platform_backup_remote as remote_fixtures
from test_business_backup_schedule import host


CREDENTIALS = {"client_id": "client_" + "a" * 24, "api_key": "sk_" + "b" * 40}
CANARY = "private-upstream-response-token-must-not-be-backed-up"


def client_factory(**_credentials):
    user = SimpleNamespace(
        id="user_test01",
        email="test@example.invalid",
        email_verified=True,
        first_name="",
        last_name=None,
        external_id=None,
        created_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
        access_token=CANARY,
        password_reset_token=CANARY,
    )
    identity = SimpleNamespace(
        provider="GithubOAuth", idp_id="test-provider-subject", token=CANARY
    )
    return SimpleNamespace(
        user_management=SimpleNamespace(
            list_users=lambda **_: SimpleNamespace(
                auto_paging_iter=lambda: iter([user])
            ),
            get_user_identities=lambda *_a, **_k: [identity],
        ),
        close=lambda: None,
    )


class WorkOSBackupTests(unittest.TestCase):
    setUp = fixtures.BackupTests.setUp
    backup = fixtures.BackupTests.backup
    target = remote_fixtures.RemoteBackupTests.target

    def packet(self):
        return workos_backup.snapshot(
            CREDENTIALS, client_factory=client_factory, clock=lambda: 1000
        )

    def test_sdk_pages_export_only_whitelisted_profile_and_oauth_fields(self):
        packet = self.packet()
        self.assertEqual(packet["status"], "complete")
        self.assertEqual(
            packet["users"][0]["identities"],
            [
                {"provider": "GithubOAuth", "idp_id": "test-provider-subject"},
            ],
        )
        encoded = json.dumps(packet)
        self.assertNotIn(CREDENTIALS["api_key"], encoded)
        self.assertNotIn(CANARY, encoded)
        self.assertNotIn("password_reset_token", encoded)
        bad = copy.deepcopy(packet)
        bad["users"][0]["access_token"] = CANARY
        with self.assertRaisesRegex(ValueError, "workos_backup_invalid"):
            workos_backup.validate(bad)

    def test_provider_failure_discards_partial_response_and_does_not_reflect_it(self):
        def broken(**credentials):
            client = client_factory(**credentials)

            def fail(*_a, **_k):
                raise RuntimeError(CANARY + CREDENTIALS["api_key"])

            client.user_management.get_user_identities = fail
            return client

        packet = workos_backup.snapshot(
            CREDENTIALS, client_factory=broken, clock=lambda: 1000
        )
        self.assertEqual((packet["status"], packet["users"]), ("unavailable", []))
        self.assertNotIn(CANARY, json.dumps(packet))
        self.assertNotIn(CREDENTIALS["api_key"], json.dumps(packet))
        ticks = iter((0, 61))
        packet = workos_backup.snapshot(
            CREDENTIALS,
            client_factory=client_factory,
            clock=lambda: 1000,
            monotonic=lambda: next(ticks),
        )
        self.assertEqual((packet["status"], packet["users"]), ("unavailable", []))

    def test_export_stops_at_incremental_size_limit_before_reading_later_users(self):
        client = client_factory()
        original = next(client.user_management.list_users().auto_paging_iter())
        visited = []

        def users():
            for index in range(10):
                visited.append(index)
                yield SimpleNamespace(**dict(vars(original), id=f"user_test{index}"))

        client.user_management.list_users = lambda **_: SimpleNamespace(
            auto_paging_iter=users
        )
        with patch.object(workos_backup, "MAX_BYTES", 800):
            packet = workos_backup.snapshot(
                CREDENTIALS, client_factory=lambda **_: client, clock=lambda: 1000
            )
        self.assertEqual((packet["status"], packet["users"]), ("unavailable", []))
        self.assertLess(len(visited), 10)

    def test_metadata_survives_publication_remote_readback_and_isolated_restore(self):
        original = self.backup()
        counts = backup.verify_local(original)["tables"]

        def export(_engine, destination, **_options):
            shutil.copyfile(original / "database.sqlite3", destination)
            return counts

        source = self.root / "hosted-backup"
        with patch.object(backup, "_export_postgres_business_db", side_effect=export):
            result = backup.backup_postgres_engine(
                None,
                None,
                source,
                stores={"local": self.store},
                hosted_users=self.packet(),
            )
        self.assertEqual(result["hosted_users_state"], "complete")
        target = self.target()
        client = remote_fixtures.FakeS3(target)
        receipt = backup_remote.copy_backup(
            source, target, self.root / "receipts", client=client
        )
        destination = self.root / "restored"
        result = backup_remote.restore_copy(
            target, receipt, self.root / "readback", destination, client=client
        )
        self.assertFalse(result["execution_enabled"])
        manifest = backup.verify_local(source)
        self.assertEqual(
            workos_backup.read(destination, manifest["hosted_users"]),
            self.packet(),
        )
        self.assertNotIn(
            CANARY.encode(), b"".join(item["data"] for item in client.objects.values())
        )
        (source / workos_backup.FILE).write_text("{}")
        with self.assertRaises(backup.BackupError):
            backup.verify_local(source)

    def test_daily_reports_hosted_failure_after_recording_business_completion(self):
        operation = self.root / "operation"
        (operation / "snapshot").mkdir(parents=True)
        manifest = operation / "snapshot" / "manifest.json"
        manifest.write_text("{}")
        with patch.object(
            host, "read", return_value={"hosted_users": {"status": "unavailable"}}
        ):
            with patch.object(
                host, "retention_after_success", return_value={"state": "retained"}
            ) as retain:
                with self.assertRaisesRegex(ValueError, "business_confirmed"):
                    host.finish_daily(
                        operation, {"state": "copied_not_restored"}, {}, "image"
                    )
                retain.assert_called_once_with({}, "image")
        for status in ({}, {"hosted_users": {"status": "complete"}}):
            with patch.object(host, "read", return_value=status):
                host.require_hosted_coverage(operation)


if __name__ == "__main__":
    unittest.main()
