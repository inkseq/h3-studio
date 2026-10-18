"""Offline behavioral tests for explicitly incomplete business/media backups."""

import copy
from contextlib import closing
from dataclasses import replace
import hashlib
import io
import json
import sqlite3
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

from sqlalchemy import insert
from studio_platform import backup, backup_remote as remote
from studio_platform.artifact_writer import write_receipts, _hash
from studio_platform.auth import Auth
from studio_platform.queue import TaskQueue
from studio_platform.repository import Repository, artifacts, budget_accounts
from studio_platform.storage import ObjectNotFound
import test_platform_backup as fixtures
import test_platform_backup_remote as remote_fixtures
import test_platform_backup_reconcile as reconcile_fixtures
from studio_platform.backup_reconcile import reconcile_copy


class IncompleteCoverageTests(unittest.TestCase):
    # Reuse fixture construction, not the original TestCase's test methods.
    setUp = fixtures.BackupTests.setUp
    target = remote_fixtures.RemoteBackupTests.target

    def source(self, *, failure="missing", allow=True, name="coverage"):
        local = self.store
        config = SimpleNamespace(
            provider="r2",
            endpoint_url="https://example.invalid",
            region="auto",
            bucket="backup-test",
            service="r2",
            profile="fixture",
        )
        queue = TaskQueue(self.repo)
        lease = queue.claim("coverage-worker", "test-pool").lease
        queue.begin_submission(lease)
        queue.record_submitted(lease, "synthetic-upstream-unknown")
        self.attempt_id = lease.attempt_id
        receipt_id = _hash(
            [self.scope.tenant_id, "superdan", self.job["id"], lease.attempt_id]
        )
        roles = {}
        self.media = {}
        for kind, mime in (("video", "video/mp4"), ("audio", "audio/wav")):
            data = ("synthetic-" + kind).encode()
            info = local.write_new(
                "superdan", str(uuid.uuid4()), io.BytesIO(data), content_type=mime
            )
            self.media[info.key] = data
            if kind == "audio":
                self.missing_key = info.key
            spec = dict(
                object_key=info.key,
                size_bytes=info.size_bytes,
                sha256=info.sha256,
                content_type=mime,
                kind=kind,
                validated=True,
            )
            roles[kind] = dict(
                key=info.key,
                status="verified",
                size_bytes=info.size_bytes,
                sha256=info.sha256,
                content_type=mime,
            )
            with self.repo.engine.begin() as conn:
                conn.execute(
                    insert(artifacts).values(
                        id=str(uuid.uuid4()),
                        job_id=self.job["id"],
                        attempt_id=lease.attempt_id,
                        created_at=1,
                        metadata=spec,
                    )
                )
        record = dict(
            id=receipt_id,
            owner="superdan",
            collection_route="gpu-r2-v1",
            binding=_hash(vars(config)),
            roles=roles,
        )
        write_receipts.metadata.create_all(self.repo.engine)
        with self.repo.engine.begin() as conn:
            conn.execute(
                insert(write_receipts).values(
                    id=receipt_id,
                    tenant=self.scope.tenant_id,
                    owner="superdan",
                    job_id=self.job["id"],
                    attempt_id=lease.attempt_id,
                    version=1,
                    record=json.dumps(record),
                )
            )
            conn.execute(
                insert(budget_accounts).values(
                    id="coverage-unknown-reservation",
                    tenant_id=self.scope.tenant_id,
                    owner_id="superdan",
                    limit_microusd=1000,
                    reserved_microusd=123,
                    spent_microusd=17,
                )
            )
        self.original_receipt = json.dumps(record)
        self.repo.configure_pool("test-pool", max_instances=2, max_physical_gpus=2)
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "test-pool",
            "coverage-unknown-instance",
            physical_gpus=1,
            reserved_cost_microusd=23,
            hard_deadline=9999999999,
            budget_account_ids=["coverage-unknown-reservation"],
            dry_run=False,
        )
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(intent["id"], "creation_unknown")
        missing_key = self.missing_key

        class PartialBody(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise TimeoutError("synthetic interrupted GET")
                return super().read(2)

        class R2:
            def stat(self, key):
                if key == missing_key:
                    if failure == "missing":
                        raise ObjectNotFound("synthetic HEAD404")
                    if failure == "forbidden":
                        raise PermissionError("synthetic HEAD403")
                    if failure == "timeout":
                        raise TimeoutError("synthetic HEAD timeout")
                return replace(local.stat(key), provider="r2", sha256=None)

            def open(self, key):
                if key == missing_key:
                    if failure == "get_missing":
                        raise ObjectNotFound("synthetic GET404 after HEAD")
                    if failure == "wrong_body":
                        return io.BytesIO(b"x" * len(self_outer.media[key]))
                    if failure == "partial_get":
                        return PartialBody(self_outer.media[key])
                return local.open(key)

        self_outer = self
        R2.config = config

        def export(engine, destination, **kwargs):
            with closing(backup._snapshot(self.database)) as snapshot:
                return backup._export_business_db(snapshot, destination)

        destination = self.root / name
        with mock.patch.object(
            backup, "_export_postgres_business_db", side_effect=export
        ):
            backup.backup_postgres_engine(
                object(),
                self.store.root,
                destination,
                stores={"local": local, "r2": R2()},
                allow_missing_media=allow,
            )
        return destination

    def test_strict_v1_missing_source_still_refuses_publication(self):
        with self.assertRaises(backup.BackupError):
            self.source(allow=False)
        self.assertFalse((self.root / "coverage" / "manifest.json").exists())

    def test_optin_with_all_bytes_present_is_honestly_complete(self):
        source = self.source(failure="none")
        manifest = backup.verify_local(source)
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["missing_objects"], [])
        self.assertEqual(manifest["coverage"]["status"], "complete")
        self.assertEqual(manifest["coverage"]["missing_bytes"], 0)
        backup.restore_local(source, self.root / "complete-restored")

    def test_local_notfound_is_not_eligible_for_r2_degradation(self):
        original_stat = self.store.stat
        key = self.assets.get("superdan", self.asset["id"])["original"]["key"]

        def stat(candidate):
            if candidate == key:
                raise ObjectNotFound("synthetic local missing")
            return original_stat(candidate)

        with mock.patch.object(self.store, "stat", side_effect=stat):
            with self.assertRaises(backup.BackupError):
                self.source(failure="none")
        self.assertFalse((self.root / "coverage" / "manifest.json").exists())

    def test_mixed_available_bytes_and_missing_receipt_restore_only_with_optin(self):
        source = self.source()
        original = (source / "database.sqlite3").read_bytes()
        with self.assertRaises(backup.BackupError):
            backup.verify_local(source)
        manifest = backup.verify_local(source, allow_incomplete=True)
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["coverage"]["status"], "incomplete")
        missing = manifest["missing_objects"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["key"], self.missing_key)
        self.assertEqual(missing[0]["reason"], "source_object_not_found")
        self.assertEqual(missing[0]["observation"], "head")
        self.assertGreater(missing[0]["observed_at"], 0)
        self.assertNotIn("file", missing[0])
        self.assertEqual(
            {x.get("provider", "local") for x in manifest["objects"]}, {"local", "r2"}
        )
        for item in manifest["objects"]:
            data = (source / "media" / item["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
            self.assertEqual(len(data), item["size_bytes"])
        coverage = manifest["coverage"]
        self.assertEqual(coverage["referenced_objects"], len(manifest["objects"]) + 1)
        self.assertEqual(
            coverage["referenced_bytes"],
            coverage["available_bytes"] + len(self.media[self.missing_key]),
        )
        with self.assertRaises(backup.BackupError):
            backup.restore_local(source, self.root / "refused")
        self.assertFalse((self.root / "refused").exists())
        restored = self.root / "restored"
        backup.restore_local(source, restored, allow_incomplete=True)
        hold = json.loads((restored / "RECOVERY-HOLD.json").read_bytes())
        self.assertEqual(hold["coverage"], coverage)
        self.assertEqual(hold["missing_objects"], missing)
        self.assertFalse(hold["execution_enabled"])
        with closing(sqlite3.connect(restored / "platform.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT reserved_microusd,spent_microusd FROM platform_budget_accounts WHERE id='coverage-unknown-reservation'"
                ).fetchone(),
                (146, 17),
            )
            self.assertEqual(
                db.execute(
                    "SELECT record FROM platform_artifact_write_receipts"
                ).fetchone()[0],
                self.original_receipt,
            )
            self.assertEqual(
                db.execute(
                    "SELECT status FROM platform_jobs WHERE id=?", (self.job["id"],)
                ).fetchone()[0],
                "recovery_hold",
            )
            specs = [
                json.loads(x[0])
                for x in db.execute("SELECT metadata FROM platform_artifacts")
            ]
            absent = next(x for x in specs if x["object_key"] == self.missing_key)
            self.assertNotIn("provider", absent)
            with closing(sqlite3.connect(source / "database.sqlite3")) as original_db:
                for table in ("platform_attempts", "platform_instance_intents"):
                    self.assertEqual(
                        db.execute(
                            'SELECT * FROM ' + table + ' ORDER BY id'
                        ).fetchall(),
                        original_db.execute(
                            'SELECT * FROM ' + table + ' ORDER BY id'
                        ).fetchall(),
                    )
        repo = Repository("sqlite:///" + (restored / "platform.sqlite3").as_posix())
        self.addCleanup(repo.close)
        self.assertFalse(Auth(repo.engine).ready())
        self.assertIsNone(TaskQueue(repo).claim("must-not-execute", "test-pool"))
        self.assertEqual((source / "database.sqlite3").read_bytes(), original)
        self.assertNotIn(self.canary.encode(), original)

    def test_partition_tampering_never_verifies(self):
        source = self.source()
        path = source / "manifest.json"
        original = json.loads(path.read_bytes())
        changes = {
            "omitted": lambda m: m["missing_objects"].clear(),
            "duplicate": lambda m: m["missing_objects"].append(
                copy.deepcopy(m["missing_objects"][0])
            ),
            "overlap": lambda m: m["objects"].append(
                dict(m["missing_objects"][0], file="0" * 64 + ".bin")
            ),
            "owner": lambda m: m["missing_objects"][0].update(owner="supervan"),
            "key": lambda m: m["missing_objects"][0].update(key=m["objects"][0]["key"]),
            "sha": lambda m: m["missing_objects"][0].update(sha256="0" * 64),
            "binding": lambda m: m["missing_objects"][0].update(
                storage_binding="0" * 64
            ),
            "reason": lambda m: m["missing_objects"][0].update(reason="timeout"),
            "observation": lambda m: m["missing_objects"][0].update(observation="get"),
            "timestamp": lambda m: m["missing_objects"][0].update(
                observed_at=float("nan")
            ),
            "false_complete": lambda m: m["coverage"].update(status="complete"),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                value = copy.deepcopy(original)
                change(value)
                path.write_text(json.dumps(value))
                with self.assertRaises(backup.BackupError):
                    backup.verify_local(source, allow_incomplete=True)

    def test_unknown_or_corrupt_reads_are_never_missing_coverage(self):
        for failure in (
            "forbidden",
            "timeout",
            "get_missing",
            "wrong_body",
            "partial_get",
        ):
            case = type(self)("runTest")
            case.setUp()
            try:
                with (
                    self.subTest(failure=failure),
                    self.assertRaises(backup.BackupError),
                ):
                    case.source(failure=failure)
                self.assertFalse((case.root / "coverage" / "manifest.json").exists())
            finally:
                case.doCleanups()

    def test_remote_roundtrip_reports_incomplete_coverage_and_never_retries_unknown_put(
        self,
    ):
        source = self.source()
        target = self.target()
        client = remote_fixtures.FakeS3(target)
        with self.assertRaises(backup.BackupError):
            remote.copy_backup(
                source, target, self.root / "default-refused", client=client
            )
        self.assertEqual(client.writes, [])
        receipt = remote.copy_backup(
            source, target, self.root / "receipt", client=client, allow_incomplete=True
        )
        self.assertEqual(receipt["coverage"]["status"], "incomplete")
        complete = json.loads(client.objects[target.prefix + "/complete.json"]["data"])
        self.assertEqual(complete["coverage"], receipt["coverage"])
        result = remote.restore_copy(
            target,
            receipt,
            self.root / "download",
            self.root / "remote-restored",
            client=client,
            allow_incomplete=True,
        )
        self.assertEqual(result["coverage"], receipt["coverage"])
        self.assertFalse(result["execution_enabled"])
        self.assertTrue(all("VersionId" in args for args in client.reads))
        unknown = remote_fixtures.FakeS3(target)
        unknown.fail_before = "database.sqlite3"
        with self.assertRaises((remote.RemoteBackupError, backup.BackupError)):
            remote.copy_backup(
                source,
                target,
                self.root / "unknown",
                client=unknown,
                allow_incomplete=True,
            )
        writes = list(unknown.writes)
        with self.assertRaises((remote.RemoteBackupError, backup.BackupError)):
            remote.copy_backup(
                source,
                target,
                self.root / "unknown",
                client=unknown,
                allow_incomplete=True,
            )
        self.assertEqual(unknown.writes, writes)
        self.assertFalse(any(k.endswith("complete.json") for k in unknown.objects))

    def test_reconcile_complete_archive_retains_incomplete_media_coverage(self):
        source = self.source()
        target = self.target()
        client = reconcile_fixtures.ReadOnlyS3(target)
        directory = self.root / "reconcile-receipts"
        receipt = remote.copy_backup(
            source, target, directory, client=client, allow_incomplete=True
        )
        intent = json.loads((directory / "intent.json").read_bytes())
        original_files = {p.name: p.read_bytes() for p in directory.iterdir()}
        original_objects = copy.deepcopy(client.objects)
        original_writes = list(client.writes)
        client.readonly = True
        client.reads.clear()
        result = reconcile_copy(target, intent, receipt=receipt, client=client)
        # "complete" proves all archive bytes, not availability of missing source media.
        self.assertEqual(result["classification"], "complete")
        self.assertEqual(result["coverage"], receipt["coverage"])
        self.assertEqual(result["coverage"]["status"], "incomplete")
        self.assertEqual(result["coverage"]["missing_objects"], 1)
        self.assertEqual(result["transfer_receipt"]["coverage"], receipt["coverage"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(result["cloud_write_operations"], 0)
        pinned = {r["Key"]: r for r in client.reads if "VersionId" in r}
        for item in result["verified_files"]:
            self.assertEqual(
                pinned[target.prefix + '/' + item["name"]]["VersionId"],
                item["version_id"],
            )
        completion_reads = [
            r for r in client.reads if r["Key"].endswith('/complete.json')
        ]
        self.assertEqual(
            [r["VersionId"] for r in completion_reads],
            [receipt["completion_version_id"]],
        )
        for which in ("intent", "receipt"):
            with self.subTest(tamper=which):
                changed_intent, changed_receipt = (
                    copy.deepcopy(intent),
                    copy.deepcopy(receipt),
                )
                changed = changed_intent if which == "intent" else changed_receipt
                # Internally consistent coverage, but unequal to its original peer.
                changed["coverage"]["missing_objects"] += 1
                changed["coverage"]["referenced_objects"] += 1
                client.reads.clear()
                with self.assertRaises(remote.RemoteBackupError):
                    reconcile_copy(
                        target, changed_intent, receipt=changed_receipt, client=client
                    )
                self.assertEqual(client.reads, [])
        coherent_bytes = copy.deepcopy(intent)
        coherent_bytes["coverage"]["available_bytes"] += 1
        coherent_bytes["coverage"]["referenced_bytes"] += 1
        client.reads.clear()
        with self.assertRaises(remote.RemoteBackupError):
            reconcile_copy(target, coherent_bytes, client=client)
        self.assertEqual(client.reads, [])
        self.assertEqual(client.writes, original_writes)
        self.assertEqual(client.objects, original_objects)
        self.assertEqual(
            {p.name: p.read_bytes() for p in directory.iterdir()}, original_files
        )


if __name__ == "__main__":
    unittest.main()
