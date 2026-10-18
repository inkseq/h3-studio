"""Stopped-writer recovery: real local storage, tiny PNG and isolated DBs.

Uses the same explicitly local PostgreSQL opt-in as the platform ledger tests.
No inference/provider requests, production DBs or existing processes are used.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest import mock

from sqlalchemy import event, insert

from studio_platform import media
from studio_platform.asset_recovery import list_receipts, main, recover_asset
from studio_platform.assets import (
    AssetConflict,
    AssetNotFound,
    AssetService,
    asset_table,
)
from studio_platform.settings import Settings
from studio_platform.storage import (
    IntegrityError,
    LocalObjectStore,
    ObjectNotFound,
    S3ObjectStore,
    StorageWriteUncertain,
)
from test_platform_assets import FaultStore, png
from test_platform_repository import LedgerCase
from test_platform_storage import config
from test_platform_storage_multipart import FakeMultipartS3, ProcessStopped


class R2AssetScratchTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        selected = config()
        self.fake = FakeMultipartS3(selected)
        self.store = S3ObjectStore(selected, client=self.fake)
        self.service = AssetService(
            self.repo.engine,
            self.store,
            self.root,
            tenant=self.scope.tenant_id,
            max_bytes=2 * 1024 * 1024,
        )

    def upload(self):
        return self.service.upload(
            self.scope.owner_id,
            self.scope.project_id,
            io.BytesIO(png()),
            "reference.png",
        )

    def pending(self):
        with mock.patch.object(self.service, "_cleanup_after_release"):
            asset = self.upload()
        return self.service.journal.get(self.scope.owner_id, asset["id"])

    def test_ready_r2_upload_removes_only_its_verified_scratch_and_keeps_objects(self):
        # Existing local media is an independent retained rollback copy.
        retained = self.root / "existing-local-media"
        retained.write_bytes(b"must retain")
        asset = self.upload()
        receipt = self.service.journal.get(self.scope.owner_id, asset["id"])
        remote_bytes = sum(value["size_bytes"] for value in receipt["objects"].values())
        self.assertEqual((asset["status"], receipt["busy"]), ("ready", False))
        self.assertFalse(self.service._stage(receipt).exists())
        self.assertEqual(receipt["scratch_cleanup"]["phase"], "complete")
        self.assertEqual(
            self.service.usage(self.scope.owner_id)["accounted_bytes"], remote_bytes
        )
        self.assertEqual(len(self.fake.objects), 4)
        self.assertEqual(retained.read_bytes(), b"must retain")
        self.assertEqual(
            self.service.resume(self.scope.owner_id, asset["id"])["status"], "ready"
        )
        self.assertEqual(
            self.service.usage(self.scope.owner_id)["accounted_bytes"], remote_bytes
        )

    def test_crash_after_unlink_before_quota_commit_recovers_once_without_reupload(
        self,
    ):
        receipt = self.pending()
        before = self.service.usage(self.scope.owner_id)["accounted_bytes"]
        save = self.service.journal.save

        def interrupted(value, **kwargs):
            if value.get("scratch_cleanup", {}).get("phase") == "complete":
                raise ProcessStopped()
            return save(value, **kwargs)

        with mock.patch.object(self.service.journal, "save", side_effect=interrupted):
            with self.assertRaises(ProcessStopped):
                self.service._cleanup_ready(receipt)
        self.assertEqual(
            self.service.usage(self.scope.owner_id)["accounted_bytes"], before
        )
        self.assertEqual(list(self.service._stage(receipt).iterdir()), [])
        with mock.patch.object(
            self.store, "put", side_effect=AssertionError("Never reupload")
        ):
            self.service.resume(self.scope.owner_id, receipt["id"])
            self.service.resume(self.scope.owner_id, receipt["id"])
        remote_bytes = sum(value["size_bytes"] for value in receipt["objects"].values())
        self.assertEqual(
            self.service.usage(self.scope.owner_id)["accounted_bytes"], remote_bytes
        )

    def test_busy_legacy_corrupt_remote_and_unexpected_files_do_not_delete(self):
        for reason in ("busy", "legacy", "remote", "changed", "extra"):
            with self.subTest(reason=reason):
                receipt = self.pending()
                source = self.service._file(receipt, "source.png")
                before = self.service.usage(self.scope.owner_id)["accounted_bytes"]
                if reason == "busy":
                    receipt["busy"] = True
                elif reason == "legacy":
                    receipt.pop("scratch_policy")
                elif reason == "remote":
                    self.fake.objects[receipt["objects"]["original"]["key"]]["body"] = (
                        b"bad"
                    )
                elif reason == "changed":
                    source.write_bytes(png("red"))
                else:
                    (source.parent / "unrelated.bin").write_bytes(b"preserve")
                if reason in ("remote", "changed", "extra"):
                    with self.assertRaises(IntegrityError):
                        self.service._cleanup_ready(receipt)
                else:
                    self.service._cleanup_ready(receipt)
                self.assertEqual(
                    source.read_bytes(), png("red") if reason == "changed" else png()
                )
                self.assertEqual(
                    self.service.usage(self.scope.owner_id)["accounted_bytes"], before
                )

    def test_unknown_put_keeps_staging_and_original_fixed_key(self):
        original = self.store.put

        def lost(key, *args, **kwargs):
            original(key, *args, **kwargs)
            raise StorageWriteUncertain(key)

        with mock.patch.object(self.store, "put", side_effect=lost):
            with self.assertRaises(StorageWriteUncertain):
                self.upload()
        asset = self.service.list(self.scope.owner_id, self.scope.project_id)[0]
        receipt = self.service.journal.get(self.scope.owner_id, asset["id"])
        key = receipt["objects"]["original"]["key"]
        self.assertEqual(receipt["asset"]["status"], "storage_unknown")
        self.assertTrue(self.service._file(receipt, "source.png").exists())
        self.assertNotIn("scratch_cleanup", receipt)
        self.service.resume(self.scope.owner_id, receipt["id"])
        latest = self.service.journal.get(self.scope.owner_id, receipt["id"])
        self.assertEqual(latest["objects"]["original"]["key"], key)
        self.assertEqual(latest["scratch_cleanup"]["phase"], "complete")

    def test_ready_legacy_asset_without_upload_receipt_keeps_prior_fast_path(self):
        import uuid

        ident = uuid.uuid4().hex
        asset = dict(
            id=ident,
            asset_id=ident,
            status="ready",
            project_id=self.scope.project_id,
            created_at=1,
            file_name="old.png",
        )
        with self.repo.engine.begin() as conn:
            conn.execute(
                insert(asset_table).values(
                    id=ident,
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    project_id=self.scope.project_id,
                    status="ready",
                    created=1,
                    record=json.dumps(asset),
                )
            )
        directory = self.service.staging_dir / ident
        directory.mkdir()
        path = directory / "source.png"
        path.write_bytes(png())
        self.assertEqual(
            self.service.resume(self.scope.owner_id, ident)["status"], "ready"
        )
        self.assertEqual(path.read_bytes(), png())


class AssetRecoveryTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.store = FaultStore(self.root / "objects")
        self.service = self.make_service()
        self.settings = Settings(
            self.root,
            database_url=self.url,
            tenant_id="test-tenant",
            max_upload_bytes=2 * 1024 * 1024,
        )

    def make_service(self, store=None):
        return AssetService(
            self.repo.engine,
            store or self.store,
            self.root,
            tenant="test-tenant",
            max_bytes=2 * 1024 * 1024,
        )

    def upload(
        self, client="recovery-one", owner="superdan", project="project-1", source=None
    ):
        return self.service.upload(
            owner,
            project,
            source or io.BytesIO(png()),
            "reference.png",
            client_asset_id=client,
        )

    def only(self):
        return self.service.list("superdan", "project-1")[0]

    def crash_during_prepare(self):
        with mock.patch.object(media, "normalize", side_effect=ProcessStopped()):
            with self.assertRaises(ProcessStopped):
                self.upload()
        return self.only()

    def crash_before_release(self):
        with mock.patch.object(self.service, "_release", side_effect=ProcessStopped()):
            with self.assertRaises(ProcessStopped):
                self.upload()
        return self.only()

    def recover(self, asset, service=None, **changes):
        service = service or self.make_service()
        receipt = service.journal.get("superdan", asset["id"])
        args = dict(
            owner="superdan",
            project_id="project-1",
            asset_id=asset["id"],
            receipt_version=receipt["version"],
            assert_writers_stopped=True,
        )
        args.update(changes)
        return recover_asset(service, **args)

    def cli(self, *args):
        output = io.StringIO()
        with (
            mock.patch(
                "studio_platform.asset_recovery.Settings.from_environment",
                return_value=self.settings,
            ),
            redirect_stdout(output),
        ):
            code = main(["--tenant", "test-tenant", "--owner", "superdan", *args])
        return code, json.loads(output.getvalue())

    def test_received_busy_restart_stays_blocked_until_explicit_fenced_same_receipt_recovery(
        self,
    ):
        asset = self.crash_during_prepare()
        before = self.service.journal.get("superdan", asset["id"])
        self.assertTrue(before["accepted_input"] and before["busy"])
        self.assertEqual(self.service._file(before, "source.png").read_bytes(), png())
        restarted = self.make_service()
        with self.assertRaises(AssetConflict):
            restarted.resume("superdan", asset["id"])
        with self.assertRaises(ValueError):
            self.recover(asset, restarted, assert_writers_stopped=False)
        self.assertEqual(
            restarted.journal.get("superdan", asset["id"])["version"], before["version"]
        )
        result = self.recover(asset, restarted)
        self.assertEqual(
            (result["asset_id"], result["status"], result["busy"]),
            (asset["id"], "ready", False),
        )
        self.assertEqual(restarted.usage("superdan")["active_uploads"], 0)
        self.assertEqual(len(self.store.calls), 4)
        self.assertEqual(restarted._file(before, "source.png").read_bytes(), png())

    def test_ready_busy_quota_window_recovers_by_verified_release_without_decoding_or_put(
        self,
    ):
        asset = self.crash_before_release()
        self.assertEqual(asset["status"], "ready")
        self.assertEqual(self.service.usage("superdan")["active_uploads"], 1)
        calls = list(self.store.calls)
        original = self.service.get("superdan", asset["id"])["original"]
        with mock.patch.object(
            media,
            "normalize",
            side_effect=AssertionError("ready must never regenerate"),
        ):
            recovered = self.recover(asset)
            # Idempotent re-confirmation of current evidence does not double release.
            again = self.recover(asset)
        self.assertEqual(recovered["status"], again["status"])
        self.assertEqual(self.store.calls, calls)
        self.assertEqual(self.service.usage("superdan")["active_uploads"], 0)
        self.assertEqual(
            self.service.get("superdan", asset["id"])["original"], original
        )

    def test_ready_busy_corrupt_object_does_not_release_quota(self):
        asset = self.crash_before_release()
        receipt = self.service.journal.get("superdan", asset["id"])
        blob = self.store._directory(receipt["objects"]["original"]["key"]) / "blob"
        blob.write_bytes(b"bad")
        with self.assertRaises(IntegrityError):
            self.recover(asset)
        latest = self.service.journal.get("superdan", asset["id"])
        self.assertEqual(latest["version"], receipt["version"])
        self.assertTrue(latest["busy"])
        self.assertEqual(self.service.usage("superdan")["active_uploads"], 1)

    def test_identity_version_and_storage_conflicts_leave_original_receipt_untouched(
        self,
    ):
        asset = self.crash_during_prepare()
        before = self.service.journal.get("superdan", asset["id"])
        for changes, error in (
            ({"owner": "supervan"}, AssetNotFound),
            ({"project_id": "foreign-project"}, AssetNotFound),
            ({"receipt_version": before["version"] + 1}, AssetConflict),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                self.recover(asset, **changes)
        foreign = self.make_service(LocalObjectStore(self.root / "different-objects"))
        with self.assertRaises(AssetConflict):
            self.recover(asset, foreign)
        self.assertEqual(self.service.journal.get("superdan", asset["id"]), before)
        self.assertEqual(self.store.calls, [])

    def test_unknown_original_recovers_only_recorded_key_without_reencoding(self):
        self.store.failure = "crash"
        with self.assertRaises(ProcessStopped):
            self.upload()
        asset = self.only()
        key = self.service.journal.get("superdan", asset["id"])["objects"]["original"][
            "key"
        ]
        with mock.patch.object(
            media,
            "normalize",
            side_effect=AssertionError("prepared original cannot regenerate"),
        ):
            result = self.recover(asset)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.store.calls[0], key)
        self.assertEqual(len(self.store.calls), 4)
        self.assertEqual(
            self.service.get("superdan", asset["id"])["original"]["key"], key
        )

    def test_missing_unknown_original_retains_reservation_and_never_blind_put(self):
        self.store.failure = "before"
        with self.assertRaises(StorageWriteUncertain):
            self.upload()
        asset = self.only()
        before = self.service.journal.get("superdan", asset["id"])
        with self.assertRaises(ObjectNotFound):
            self.recover(asset)
        after = self.service.journal.get("superdan", asset["id"])
        self.assertEqual(
            after["objects"]["original"]["key"], before["objects"]["original"]["key"]
        )
        self.assertEqual(after["reserved"], before["reserved"])
        self.assertEqual(
            self.service.usage("superdan")["accounted_bytes"], before["reserved"]
        )
        self.assertEqual(len(self.store.calls), 1)

    def test_active_receive_prepare_and_ready_release_cannot_be_stolen_even_with_operator_assertion(
        self,
    ):
        for phase in ("receiving", "prepare", "ready-release"):
            entered, finish = threading.Event(), threading.Event()

            def block(callback):
                def wrapped(*args, **kwargs):
                    entered.set()
                    if not finish.wait(5):
                        raise RuntimeError("bounded test operation did not unblock")
                    return callback(*args, **kwargs)

                return wrapped

            source = io.BytesIO(png())
            if phase == "receiving":
                source.read = block(source.read)
                patch = mock.patch.object(media, "normalize", wraps=media.normalize)
            elif phase == "prepare":
                patch = mock.patch.object(
                    media, "normalize", side_effect=block(media.normalize)
                )
            else:
                patch = mock.patch.object(
                    self.service, "_release", side_effect=block(self.service._release)
                )
            with (
                self.subTest(phase=phase),
                patch,
                ThreadPoolExecutor(max_workers=1) as executor,
            ):
                future = executor.submit(
                    self.upload, "live-" + phase, "superdan", "project-1", source
                )
                try:
                    self.assertTrue(entered.wait(3))
                    asset = next(
                        v
                        for v in self.service.list("superdan", "project-1")
                        if v["client_asset_id"] == "live-" + phase
                    )
                    before = self.service.journal.get("superdan", asset["id"])
                    with self.assertRaisesRegex(AssetConflict, "仍在运行"):
                        self.recover(asset)
                    self.assertEqual(
                        self.service.journal.get("superdan", asset["id"]), before
                    )
                    self.assertEqual(
                        self.service.usage("superdan")["active_uploads"], 1
                    )
                finally:
                    finish.set()
                self.assertEqual(future.result(timeout=5)["status"], "ready")
            self.assertEqual(self.service.usage("superdan")["active_uploads"], 0)

    def test_other_process_holding_receipt_lock_is_never_overridden(self):
        asset = self.crash_during_prepare()
        script = (
            "from pathlib import Path; import sys; "
            "from studio_platform.asset_operation import asset_operation_lock; "
            "ctx=asset_operation_lock(Path(sys.argv[1]),sys.argv[2]); "
            "assert ctx.__enter__(); print('locked',flush=True); sys.stdin.readline(); ctx.__exit__(None,None,None)"
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.service.operation_dir),
                asset["id"],
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(AssetConflict, "仍在运行"):
                self.recover(asset)
        finally:
            child.communicate("release\n", timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(self.recover(asset)["status"], "ready")

    def test_two_recovery_operators_share_fence_and_only_one_claims_same_version(self):
        asset = self.crash_during_prepare()
        version = self.service.journal.get("superdan", asset["id"])["version"]
        service = self.make_service()
        barrier = threading.Barrier(2)

        def recover():
            barrier.wait(timeout=3)
            try:
                return self.recover(asset, service, receipt_version=version)["state"]
            except AssetConflict:
                return "refused"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: recover(), range(2)))
        self.assertEqual(sorted(results), ["recovered", "refused"])
        self.assertEqual(len(self.store.calls), 4)
        self.assertEqual(service.usage("superdan")["active_uploads"], 0)

    def test_default_cli_is_read_only_bounded_scoped_and_excludes_private_content(self):
        self.upload()
        self.upload("second", owner="supervan")
        before = self.service.journal.get("superdan", self.only()["id"])
        statements = []

        def capture(connection, cursor, statement, parameters, context, many):
            statements.append(statement.strip().upper())

        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            result = list_receipts(
                self.repo.engine, tenant="test-tenant", owner="superdan", limit=1
            )
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertFalse(
            any(
                s.startswith(
                    ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "BEGIN IMMEDIATE")
                )
                for s in statements
            )
        )
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["owner"], "superdan")
        text = json.dumps(result)
        self.assertNotIn("owners/", text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("file_name", text)
        self.assertEqual(
            self.service.journal.get("superdan", self.only()["id"]), before
        )
        with mock.patch(
            "studio_platform.asset_recovery.AssetService",
            side_effect=AssertionError("list cannot construct service"),
        ):
            code, result = self.cli()
        self.assertEqual((code, result["state"]), (0, "read_only"))

    def test_cli_missing_assertion_refuses_before_configuration_and_recovery_requires_current_version(
        self,
    ):
        with mock.patch(
            "studio_platform.asset_recovery.Settings.from_environment",
            side_effect=AssertionError("must not load config"),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--tenant",
                            "test-tenant",
                            "--owner",
                            "superdan",
                            "--mode",
                            "recover",
                        ]
                    ),
                    1,
                )
        asset = self.crash_before_release()
        version = self.service.journal.get("superdan", asset["id"])["version"]
        args = [
            "--mode",
            "recover",
            "--project-id",
            "project-1",
            "--asset-id",
            asset["id"],
            "--receipt-version",
            str(version),
            "--assert-writers-stopped",
        ]
        code, result = self.cli(*args)
        self.assertEqual((code, result["status"], result["busy"]), (0, "ready", False))
        self.assertEqual(
            self.cli(*args)[0], 1
        )  # stale inspect version must be re-listed

    def test_cli_rejects_nonlocal_and_redacts_configuration_failures(self):
        remote = Settings(
            self.root,
            database_url=self.url,
            tenant_id="test-tenant",
            storage_provider="r2",
            storage_bucket="fake",
            storage_endpoint="https://fake.invalid",
            storage_region="auto",
        )
        output = io.StringIO()
        with (
            mock.patch(
                "studio_platform.asset_recovery.Settings.from_environment",
                return_value=remote,
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(["--tenant", "test-tenant", "--owner", "superdan"]), 1
            )
        with (
            mock.patch(
                "studio_platform.asset_recovery.Settings.from_environment",
                side_effect=RuntimeError("FAKE-SECRET-CANARY"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(["--tenant", "test-tenant", "--owner", "superdan"]), 1
            )
        self.assertNotIn("FAKE-SECRET-CANARY", output.getvalue())

    def test_incomplete_reconciliation_is_not_reported_as_success(self):
        asset = self.crash_during_prepare()
        with mock.patch.object(
            self.service, "reconcile", return_value={"status": "failed"}
        ):
            result = self.recover(asset, self.service)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["busy"])
        version = self.service.journal.get("superdan", asset["id"])["version"]
        with mock.patch(
            "studio_platform.asset_recovery.recover_asset", return_value=result
        ):
            code, shown = self.cli(
                "--mode",
                "recover",
                "--project-id",
                "project-1",
                "--asset-id",
                asset["id"],
                "--receipt-version",
                str(version),
                "--assert-writers-stopped",
            )
        self.assertEqual((code, shown["state"]), (2, "incomplete"))

    def partial_receive(self):
        body = png()

        class PartialReader:
            first = True

            def read(self, size=-1):
                if self.first:
                    self.first = False
                    return body[: len(body) // 2]
                raise ProcessStopped()

        with self.assertRaises(ProcessStopped):
            self.upload(source=PartialReader())
        return self.only(), body[: len(body) // 2]

    def test_stopped_partial_receive_can_release_active_but_retains_exact_bytes_and_never_ready(
        self,
    ):
        asset, partial = self.partial_receive()
        receipt = self.service.journal.get("superdan", asset["id"])
        self.assertEqual(receipt["storage_binding"], self.service.storage_binding)
        self.assertEqual(receipt["suffix"], ".png")
        self.assertFalse(receipt.get("accepted_input"))
        held = self.service._file(receipt, "receiving")
        self.assertEqual(held.read_bytes(), partial)
        version = receipt["version"]
        args = [
            "--mode",
            "settle-incomplete",
            "--project-id",
            "project-1",
            "--asset-id",
            asset["id"],
            "--receipt-version",
            str(version),
            "--assert-writers-stopped",
        ]
        with mock.patch.object(
            media,
            "normalize",
            side_effect=AssertionError("partial is never a model input"),
        ):
            code, result = self.cli(*args)
            self.assertEqual(
                (code, result["state"], result["status"], result["media_ready"]),
                (0, "incomplete_settled", "failed", False),
            )
            self.assertEqual(
                self.recover(asset, settle_incomplete=True)["state"],
                "incomplete_settled",
            )
        self.assertEqual(held.read_bytes(), partial)
        self.assertEqual(self.service.usage("superdan")["active_uploads"], 0)
        self.assertEqual(
            self.service.usage("superdan")["accounted_bytes"], len(partial)
        )
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.upload("new-complete-input")["status"], "ready")

    def test_old_unbound_partial_receipt_and_submitted_objects_cannot_be_settled_as_incomplete(
        self,
    ):
        asset, partial = self.partial_receive()
        receipt = self.service.journal.get("superdan", asset["id"])
        receipt.pop("storage_binding")
        self.service.journal.save(receipt)
        before = self.service.journal.get("superdan", asset["id"])
        with self.assertRaisesRegex(AssetConflict, "没有匹配"):
            self.recover(asset, settle_incomplete=True)
        self.assertEqual(self.service.journal.get("superdan", asset["id"]), before)
        self.assertEqual(self.service.usage("superdan")["active_uploads"], 1)
        with self.assertRaises(AssetConflict):
            self.service.settle_incomplete(
                "superdan", asset["id"], expected_version=before["version"]
            )

    def test_complete_unknown_receipt_is_not_misclassified_as_partial_or_budget_released(
        self,
    ):
        self.store.failure = "before"
        with self.assertRaises(StorageWriteUncertain):
            self.upload()
        asset = self.only()
        before = self.service.journal.get("superdan", asset["id"])
        with self.assertRaises(AssetConflict):
            self.recover(asset, settle_incomplete=True)
        self.assertEqual(self.service.journal.get("superdan", asset["id"]), before)
        self.assertEqual(
            self.service.usage("superdan")["accounted_bytes"], before["reserved"]
        )

    def test_cli_never_initializes_an_unrelated_existing_empty_database(self):
        from sqlalchemy import create_engine, inspect

        empty = self.root / "unrelated.sqlite3"
        empty.touch()
        settings = Settings(
            self.root,
            database_url="sqlite:///" + empty.as_posix(),
            tenant_id="test-tenant",
        )
        output = io.StringIO()
        with (
            mock.patch(
                "studio_platform.asset_recovery.Settings.from_environment",
                return_value=settings,
            ),
            mock.patch(
                "studio_platform.asset_recovery.AssetService",
                side_effect=AssertionError("must refuse before constructing service"),
            ),
            redirect_stdout(output),
        ):
            code = main(
                [
                    "--tenant",
                    "test-tenant",
                    "--owner",
                    "superdan",
                    "--mode",
                    "recover",
                    "--project-id",
                    "project-1",
                    "--asset-id",
                    "a" * 32,
                    "--receipt-version",
                    "0",
                    "--assert-writers-stopped",
                ]
            )
        self.assertEqual(code, 1)
        engine = create_engine("sqlite:///" + empty.as_posix())
        try:
            self.assertEqual(inspect(engine).get_table_names(), [])
        finally:
            engine.dispose()
