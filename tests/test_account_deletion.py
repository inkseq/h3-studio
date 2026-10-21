"""Disposable offline deletion safety; never production or a real provider."""

import io
import asyncio
import importlib.util
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import uuid

from PIL import Image
from sqlalchemy import insert, select, update, Table, MetaData, Column, String
from workos._errors import NotFoundError

from studio_platform.account_deletion import (
    AccountDeletion,
    DeletionError,
    original_cleanup,
    enabled,
)
from studio_platform.api import create_app
from studio_platform.auth import Principal, digest, sessions
from studio_platform.content_lifecycle import history_cleanup_activation_id
from studio_platform.credit_schema import (
    metadata as credit_metadata,
    accounts as credit_accounts,
)
from studio_platform.identity_schema import (
    metadata as identity_metadata,
    users,
    admin_audit,
)
from studio_platform.payment_schema import metadata as payment_metadata
from studio_platform.quick_chat import objects
from studio_platform.repository import Conflict, Scope, documents
from studio_platform.session_authority import read, write
from studio_platform.settings import Settings
from test_platform_repository import LedgerCase


class DisposableProvider:
    def __init__(self, subject):
        self.subject = subject
        self.deleted = False
        self.calls = []
        self.fail = False
        self.lost = False

    def get_user(self, ident):
        self.calls.append(("get", ident))
        if self.fail:
            raise RuntimeError("PRIVATE provider data")
        if self.deleted:
            raise NotFoundError("fixture absent")
        return SimpleNamespace(id=self.subject)

    def delete_user(self, ident):
        self.calls.append(("delete", ident))
        self.deleted = True
        if self.lost:
            raise RuntimeError("PRIVATE lost response")


class AccountDeletionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(
            Settings(
                Path(self.temp.name) / "delete",
                database_url=self.url,
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
            ),
            repository=self.repo,
        )
        self.auth = self.app.state.auth
        self.auth.clock = lambda: self.now
        self.user_id = str(uuid.uuid4())
        self.owner = "u_" + uuid.UUID(self.user_id).hex
        self.subject = "user_" + "D" * 24
        self.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id,
            public_origin="https://staging.inkseq.com",
            deployment_target="staging",
            public_origin_aliases=(),
            account_deletion_test_owners=(self.owner,),
            operator_admin_owners=(),
            operator_capacity_owners=(),
        )
        with self.repo.transaction() as conn:
            identity_metadata.create_all(conn)
            credit_metadata.create_all(conn)
            payment_metadata.create_all(conn)
            version = Table(
                "alembic_version",
                MetaData(),
                Column("version_num", String(32), primary_key=True),
            )
            version.create(conn)
            conn.execute(insert(version).values(version_num="20261020_0005"))
            conn.execute(
                insert(users).values(
                    id=self.user_id,
                    tenant=self.scope.tenant_id,
                    owner=self.owner,
                    role="user",
                    status="active",
                    display_name="Private fixture",
                    email="private@example.test",
                    email_verified=1,
                    workos_user_id=self.subject,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            conn.execute(
                insert(credit_accounts).values(
                    user_id=self.user_id,
                    tenant=self.scope.tenant_id,
                    balance=0,
                    held=0,
                    version=0,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            conn.execute(
                insert(objects).values(
                    id=history_cleanup_activation_id(self.scope.tenant_id),
                    tenant=self.scope.tenant_id,
                    owner="__system__",
                    session_id="__history_system",
                    kind="history_cleanup_activation",
                    parent_id=None,
                    business_key="v1",
                    version=1,
                    payload={
                        "version": 1,
                        "enabled": True,
                        "activated_release": "a" * 40,
                    },
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        self.token = self.auth.issue_workos_session(
            self.owner, self.subject, captured_epoch=self.auth.capture_epoch()
        )
        self.principal = Principal(
            self.owner,
            "browser:" + self.owner,
            auth_mode="workos",
            password_version=0.0,
            session_hash=digest(self.token),
            user_id=self.user_id,
            role="user",
            identity_source="canonical",
        )
        self.provider = DisposableProvider(self.subject)
        self.workos = SimpleNamespace(
            config=lambda: {"client_id": "client_" + "D" * 24, "api_key": "offline"},
            sdk_factory=lambda _cfg: SimpleNamespace(
                user_management=self.provider, close=lambda: None
            ),
        )
        self.service = AccountDeletion(
            self.repo, self.auth, self.settings, self.workos, self.app.state.quick_chat
        )
        self.enqueues = []
        self.repo.generation_transport = SimpleNamespace(
            enqueue_account_operation=lambda *args, **kwargs: self.enqueues.append(
                args[1:]
            ),
            job_event=lambda *args, **kwargs: None,
            close=lambda: None,
        )

    def preview(self):
        return self.service.preview(self.principal, {})

    def confirm(self, preview=None):
        preview = preview or self.preview()
        return self.service.confirm(
            self.principal,
            {"preview_id": preview["preview_id"], "confirmed": True},
            "delete-once",
        )

    def row(self, ident):
        with self.repo.engine.connect() as conn:
            return self.service._record(conn, ident)

    def finish(self, ident):
        for _ in range(12):
            result = self.service.turn(self.scope.tenant_id, ident)
            if result["done"]:
                return result
        self.fail(
            "original_fixture_deletion_did_not_complete: "
            + str(self.row(ident)["payload"]["error_code"])
        )

    def test_production_unconditionally_refuses_before_database_or_provider(self):
        self.settings.deployment_target = "production"
        with patch.object(self.repo, "transaction", side_effect=AssertionError("noDB")):
            for callback in (
                lambda: self.preview(),
                lambda: self.confirm({"preview_id": str(uuid.uuid4())}),
            ):
                with self.assertRaisesRegex(
                    DeletionError, "account_deletion_not_enabled"
                ):
                    callback()
        self.assertEqual(self.provider.calls, [])

    def test_empty_disposable_preview_confirm_original_provider_and_tombstone(self):
        before = self.repo.get_budget("owner-budget")
        preview = self.preview()
        self.assertTrue(preview["can_delete"], preview["blockers"])
        self.assertFalse(preview["production_enabled"])
        self.assertNotIn(self.owner, str(preview))
        self.assertNotIn("private@example.test", str(preview))
        result = self.confirm(preview)
        ident = result["operation_id"]
        self.assertEqual(result["state"], "pending")
        self.assertEqual(self.enqueues, [(self.scope.tenant_id, ident)])
        self.assertTrue(self.finish(ident)["done"])
        self.assertEqual(
            [c for c in self.provider.calls if c[0] == "delete"],
            [("delete", self.subject)],
        )
        with self.repo.engine.connect() as conn:
            user = (
                conn.execute(select(users).where(users.c.id == self.user_id))
                .mappings()
                .one()
            )
            self.assertEqual(user["id"], self.user_id)
            self.assertEqual(user["owner"], "deleted_" + uuid.UUID(self.user_id).hex)
            self.assertIsNone(user["email"])
            self.assertIsNone(user["workos_user_id"])
            self.assertEqual(user["status"], "suspended")
            account = (
                conn.execute(
                    select(credit_accounts).where(
                        credit_accounts.c.user_id == self.user_id
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(
                (account["balance"], account["held"], account["version"]), (0, 0, 0)
            )
        self.assertEqual(self.repo.get_budget("owner-budget"), before)

    def test_preview_expiry_changed_source_and_foreign_session_are_closed(self):
        first = self.preview()
        self.now += 601
        with self.assertRaisesRegex(DeletionError, "account_deletion_preview_changed"):
            self.confirm(first)
        current = self.preview()
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.owner, "__projects"),
            "project",
            "new",
            {"id": "new", "title": "Changed"},
        )
        with self.assertRaisesRegex(DeletionError, "account_deletion_preview_changed"):
            self.confirm(current)
        current = self.preview()
        other = Principal(
            self.owner, "other", user_id=self.user_id, session_hash="f" * 64
        )
        with self.assertRaises(DeletionError):
            self.service.confirm(
                other, {"preview_id": current["preview_id"], "confirmed": True}, "other"
            )
        self.assertEqual(self.provider.calls, [])

    def test_active_job_and_credit_hold_are_counted_without_cancellation_or_release(
        self,
    ):
        self.repo.configure_budget(
            "disposable",
            tenant_id=self.scope.tenant_id,
            owner_id=self.owner,
            limit_microusd=1_000_000,
        )
        scope = Scope(self.scope.tenant_id, self.owner, "story")
        plan = self.plan(scope=scope)
        job = self.repo.create_job(
            scope, plan["id"], "queued", budget_account_ids=["disposable"]
        )
        with self.repo.transaction() as conn:
            conn.execute(
                update(credit_accounts)
                .where(credit_accounts.c.user_id == self.user_id)
                .values(balance=20, held=5)
            )
        preview = self.preview()
        codes = {v["code"] for v in preview["blockers"]}
        self.assertTrue(
            {
                "jobs_active_or_uncertain",
                "unsettled_reservations",
                "credit_holds_unsettled",
            }
            <= codes
        )
        with self.assertRaisesRegex(DeletionError, "account_deletion_blocked"):
            self.confirm(preview)
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")
        self.assertEqual(
            self.repo.get_budget("disposable")["reserved_microusd"],
            job["estimated_cost_microusd"],
        )

    def test_authenticated_inflight_write_is_fenced_but_unrelated_owner_survives(self):
        scope = Scope(self.scope.tenant_id, self.owner, "__projects")
        self.repo.put_document(scope, "project", "old", {"id": "old"})
        operation = self.confirm()
        ident = operation["operation_id"]
        with self.assertRaisesRegex(Conflict, "account_deletion_pending"):
            self.repo.put_document(scope, "project", "late", {"id": "late"})
        self.repo.put_document(
            self.other, "note", "unrelated", {"text": "still usable"}
        )
        with original_cleanup(self.scope.tenant_id, self.owner, str(uuid.uuid4())):
            with self.assertRaisesRegex(Conflict, "account_deletion_pending"):
                self.repo.put_document(scope, "project", "wrong-operation", {})
        self.assertEqual(self.row(ident)["payload"]["phase"], "media")

    def test_exact_media_bytes_deleted_and_unchanged_accounting_rows_preserved(self):
        image = io.BytesIO()
        Image.new("RGB", (256, 256), "navy").save(image, "PNG")
        image.seek(0)
        asset = self.app.state.assets.upload(self.owner, "story", image, "private.png")
        preview = self.preview()
        self.assertTrue(preview["can_delete"], preview["blockers"])
        ident = self.confirm(preview)["operation_id"]
        record = self.row(ident)
        keys = [s["object_key"] for s in record["payload"]["plan"]["objects"]]
        self.assertTrue(keys)
        self.finish(ident)
        from studio_platform.storage import ObjectNotFound

        for key in keys:
            with self.assertRaises(ObjectNotFound):
                self.app.state.storage.stat(key)

    def test_unknown_provider_delete_response_reconciles_without_second_delete(self):
        self.provider.lost = True
        ident = self.confirm()["operation_id"]
        self.finish(ident)
        self.assertEqual(len([v for v in self.provider.calls if v[0] == "delete"]), 1)

    def test_unknown_provider_delete_ack_with_user_still_present_never_reposts(self):
        ident = self.confirm()["operation_id"]
        self.service.turn(self.scope.tenant_id, ident)
        with patch.object(
            self.provider,
            "delete_user",
            side_effect=RuntimeError("PRIVATE unknown response"),
        ) as delete_user:
            self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
            self.service = AccountDeletion(
                self.repo,
                self.auth,
                self.settings,
                self.workos,
                self.app.state.quick_chat,
            )
            for _ in range(2):
                self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
            self.assertEqual(delete_user.call_count, 1)
        record = self.row(ident)
        self.assertEqual(record["payload"]["provider"]["state"], "requested")
        self.assertEqual(
            record["payload"]["error_code"],
            "account_deletion_provider_delete_unconfirmed",
        )
        self.assertEqual(record["id"], ident)

    def test_default_disabled_aliased_origin_and_machine_refuse(self):
        self.assertTrue(self.service.capability(self.principal))
        for changes in (
            {"account_deletion_test_owners": ()},
            {"public_origin": "https://inkseq.com"},
            {"public_origin_aliases": ("https://inkseq.com",)},
        ):
            previous = {k: getattr(self.settings, k) for k in changes}
            for key, value in changes.items():
                setattr(self.settings, key, value)
            self.assertFalse(self.service.capability(self.principal))
            with self.assertRaisesRegex(DeletionError, "account_deletion_not_enabled"):
                self.preview()
            for key, value in previous.items():
                setattr(self.settings, key, value)
        machine = Principal(self.owner, "machine", machine=True)
        with self.assertRaisesRegex(DeletionError, "browser_required"):
            self.service.preview(machine, {})

    def test_protected_admin_qa_and_operator_never_preview(self):
        with self.repo.transaction() as conn:
            conn.execute(
                update(users).where(users.c.id == self.user_id).values(role="admin")
            )
        with self.assertRaisesRegex(DeletionError, "account_deletion_protected"):
            self.preview()
        with self.repo.transaction() as conn:
            conn.execute(
                update(users).where(users.c.id == self.user_id).values(role="user")
            )
        self.settings.operator_capacity_owners = (self.owner,)
        with self.assertRaisesRegex(DeletionError, "account_deletion_protected"):
            self.preview()
        self.settings.operator_capacity_owners = ()
        for name in ("qa-admin", "qa-user", "superdan", "supervan"):
            self.assertTrue(self.service._protected({"owner": name, "role": "user"}))
        self.assertEqual(self.provider.calls, [])

    def test_acceptance_and_completion_audit_are_once_with_original_uuid(self):
        ident = self.confirm()["operation_id"]
        self.finish(ident)
        self.assertTrue(self.service.turn(self.scope.tenant_id, ident)["done"])
        with self.repo.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(admin_audit).where(
                        admin_audit.c.target_user_id == self.user_id
                    )
                ).mappings()
            )
        self.assertEqual(
            {r["action"] for r in rows},
            {"account_deletion_requested", "account_deletion_completed"},
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(
            all(
                r["actor_user_id"] == self.user_id
                and r["after"]["operation_id"] == ident
                for r in rows
            )
        )
        self.assertNotIn("private@example.test", str(rows))

    def test_enqueue_failure_rolls_back_suspension_fence_revocation_and_audit(self):
        self.repo.generation_transport.enqueue_account_operation = (
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        )
        with self.assertRaises(RuntimeError):
            self.confirm()
        with self.repo.engine.connect() as conn:
            self.assertIsNone(
                read(conn, self.scope.tenant_id, "deletion_owner", self.owner)
            )
            self.assertEqual(
                conn.execute(
                    select(users.c.status).where(users.c.id == self.user_id)
                ).scalar_one(),
                "active",
            )
            self.assertEqual(list(conn.execute(select(admin_audit))), [])
            self.assertIsNotNone(
                conn.execute(
                    select(sessions.c.token_hash).where(
                        sessions.c.token_hash == digest(self.token)
                    )
                ).first()
            )
        self.assertEqual(self.provider.calls, [])

    def test_pending_film_without_child_jobs_is_a_blocker(self):
        with self.repo.transaction() as conn:
            conn.execute(
                insert(objects).values(
                    id="film-fixture",
                    tenant=self.scope.tenant_id,
                    owner=self.owner,
                    session_id="film-fixture",
                    kind="film_run",
                    parent_id=None,
                    business_key="film-fixture",
                    version=1,
                    payload={"phase": "accepted"},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        result = self.preview()
        self.assertIn("film_run_unresolved", {b["code"] for b in result["blockers"]})
        self.assertFalse(result["can_delete"])

    def test_late_credit_hold_stops_finalization_without_changing_money(self):
        ident = self.confirm()["operation_id"]
        for _ in range(6):
            if self.row(ident)["payload"]["phase"] == "finalize":
                break
            self.service.turn(self.scope.tenant_id, ident)
        with self.repo.transaction() as conn:
            conn.execute(
                update(credit_accounts)
                .where(credit_accounts.c.user_id == self.user_id)
                .values(balance=30, held=7)
            )
        self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
        self.assertEqual(self.row(ident)["payload"]["phase"], "finalize")
        with self.repo.engine.connect() as conn:
            balance = conn.execute(
                select(credit_accounts.c.balance, credit_accounts.c.held).where(
                    credit_accounts.c.user_id == self.user_id
                )
            ).one()
            self.assertEqual(tuple(balance), (30, 7))
            self.assertEqual(
                conn.execute(
                    select(users.c.owner).where(users.c.id == self.user_id)
                ).scalar_one(),
                self.owner,
            )

    def test_provider_outage_and_client_change_never_delete_or_claim_complete(self):
        ident = self.confirm()["operation_id"]
        self.service.turn(self.scope.tenant_id, ident)
        self.provider.fail = True
        self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
        self.provider.fail = False
        self.workos.config = lambda: {"client_id": "client_" + "X" * 24}
        self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
        self.assertEqual([c for c in self.provider.calls if c[0] == "delete"], [])
        self.assertEqual(self.row(ident)["payload"]["phase"], "provider")

    def test_foreign_operation_pointer_and_internal_context_refuse(self):
        ident = self.confirm()["operation_id"]
        with self.repo.transaction() as conn:
            write(
                conn,
                self.scope.tenant_id,
                "deletion",
                ident,
                {"owner": "foreign", "user_id": self.user_id},
                self.now,
            )
        with self.assertRaisesRegex(DeletionError, "account_deletion_binding_changed"):
            self.service.turn(self.scope.tenant_id, ident)
        self.assertEqual(self.provider.calls, [])

    def test_same_key_atomic_replay_never_enqueues_again_and_changed_body_conflicts(
        self,
    ):
        from studio_platform.auth import sessions

        preview = self.preview()
        row = None
        with self.repo.engine.connect() as conn:
            row = dict(
                conn.execute(
                    select(sessions).where(sessions.c.token_hash == digest(self.token))
                )
                .mappings()
                .one()
            )
        result = self.confirm(preview)
        # Reconstitute only the pre-check context to exercise service replay;
        # public HTTP cannot authenticate the suspended/deleted cookie again.
        with self.repo.transaction() as conn:
            conn.execute(
                update(users).where(users.c.id == self.user_id).values(status="active")
            )
            conn.execute(insert(sessions).values(**row))
        self.assertEqual(self.confirm(preview), result)
        with self.assertRaisesRegex(
            DeletionError, "account_deletion_idempotency_conflict"
        ):
            self.service.confirm(
                self.principal,
                {"preview_id": str(uuid.uuid4()), "confirmed": True},
                "delete-once",
            )
        self.assertEqual(len(self.enqueues), 1)

    def test_phase_recovery_reuses_same_plan_and_account_promotion_prevents_provider_delete(
        self,
    ):
        ident = self.confirm()["operation_id"]
        self.service.turn(self.scope.tenant_id, ident)
        # A trusted operator mutation outside the guarded HTTP API must still
        # stop this worker before another irreversible provider operation.
        with self.repo.transaction() as conn:
            conn.execute(
                update(users).where(users.c.id == self.user_id).values(role="admin")
            )
        self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
        self.assertEqual(self.provider.calls, [])
        with self.repo.transaction() as conn:
            conn.execute(
                update(users).where(users.c.id == self.user_id).values(role="user")
            )
        restored = AccountDeletion(
            self.repo, self.auth, self.settings, self.workos, self.app.state.quick_chat
        )
        self.service = restored
        self.finish(ident)
        self.assertEqual(len(self.enqueues), 1)

    def test_unknown_delete_response_rechecks_original_media_key_without_duplicate_delete(
        self,
    ):
        image = io.BytesIO()
        Image.new("RGB", (256, 256), "navy").save(image, "PNG")
        image.seek(0)
        self.app.state.assets.upload(self.owner, "story", image, "private.png")
        ident = self.confirm()["operation_id"]
        original = self.app.state.storage.delete
        deleted = []

        def lost(key):
            deleted.append(key)
            original(key)
            raise RuntimeError("PRIVATE lost response")

        with patch.object(self.app.state.storage, "delete", side_effect=lost):
            self.finish(ident)
        self.assertEqual(len(deleted), len(set(deleted)))
        self.assertTrue(
            all(
                s["state"] == "deleted"
                for s in self.row(ident)["payload"]["plan"]["objects"]
            )
        )

    def test_changed_storage_bytes_are_retained_with_specific_original_diagnostic(self):
        from dataclasses import replace

        image = io.BytesIO()
        Image.new("RGB", (256, 256), "navy").save(image, "PNG")
        image.seek(0)
        self.app.state.assets.upload(self.owner, "story", image, "private.png")
        ident = self.confirm()["operation_id"]
        original = self.app.state.storage.stat
        with (
            patch.object(
                self.app.state.storage,
                "stat",
                side_effect=lambda key: replace(original(key), size_bytes=1),
            ),
            patch.object(
                self.app.state.storage,
                "delete",
                side_effect=AssertionError("must_not_delete"),
            ),
        ):
            self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
        record = self.row(ident)
        self.assertEqual(record["payload"]["phase"], "media")
        self.assertEqual(
            record["payload"]["plan"]["objects"][0]["error_code"],
            "storage_identity_changed",
        )
        self.assertEqual(self.provider.calls, [])

    def test_shared_reference_keeps_original_media_and_foreign_document(self):
        image = io.BytesIO()
        Image.new("RGB", (256, 256), "navy").save(image, "PNG")
        image.seek(0)
        asset = self.app.state.assets.upload(self.owner, "story", image, "private.png")
        original_key = self.app.state.assets.get(self.owner, asset["id"])["original"][
            "key"
        ]
        # Conservative historical/imported reference: ordinary new authoring
        # correctly refuses a foreign-owned reference before it can exist.
        with self.repo.transaction() as conn:
            conn.execute(
                insert(documents).values(
                    tenant_id=self.other.tenant_id,
                    owner_id=self.other.owner_id,
                    project_id=self.other.project_id,
                    kind="note",
                    document_id="shared",
                    payload={"asset_id": asset["id"]},
                    version=1,
                    updated_at=self.now,
                )
            )
        preview = self.preview()
        self.assertTrue(preview["can_delete"], preview["blockers"])
        self.assertEqual(preview["retained"]["shared_or_unknown_media"]["assets"], 1)
        ident = self.confirm(preview)["operation_id"]
        self.assertNotIn(asset["id"], self.row(ident)["payload"]["plan"]["asset_ids"])
        self.finish(ident)
        self.assertEqual(
            self.repo.get_document(self.other, "note", "shared")["payload"],
            {"asset_id": asset["id"]},
        )
        self.app.state.storage.stat(original_key)

    def test_live_machine_is_blocked_without_operator_stop_or_budget_release(self):
        from studio_platform.operator_capacity import operator_commands, operator_nodes
        from studio_platform.repository import instance_intents

        ident = str(uuid.uuid4())
        command = str(uuid.uuid4())
        with self.repo.transaction() as conn:
            conn.execute(
                insert(instance_intents).values(
                    id=ident,
                    intent_key=ident,
                    pool="fixture",
                    request_hash="f" * 64,
                    state="running",
                    physical_gpus=1,
                    slots=1,
                    reserved_cost_microusd=0,
                    provider_instance_id="fixture",
                    provider="fixture",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            conn.execute(
                insert(operator_commands).values(
                    id=command,
                    actor=self.owner,
                    idempotency_key=command,
                    request_hash="f" * 64,
                    kind="start",
                    state="succeeded",
                    payload={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            conn.execute(
                insert(operator_nodes).values(
                    intent_id=ident,
                    command_id=command,
                    ordinal=0,
                    binding_id="fixture",
                    binding_hash="f" * 64,
                    payload={"tenant_id": self.scope.tenant_id, "owner_id": self.owner},
                    desired_state="running",
                    runtime_state="ready",
                    updated_at=self.now,
                )
            )
        self.assertIn(
            "machine_obligations_unresolved",
            {b["code"] for b in self.preview()["blockers"]},
        )
        with self.assertRaisesRegex(DeletionError, "account_deletion_blocked"):
            self.confirm()
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    select(operator_nodes.c.desired_state).where(
                        operator_nodes.c.intent_id == ident
                    )
                ).scalar_one(),
                "running",
            )

    def test_keys_service_clients_and_telegram_grant_are_revoked_at_acceptance(self):
        from studio_platform.auth import clients, personal_keys
        from studio_platform.agent_connect import connections

        key = self.auth.create_key(
            self.owner,
            authenticated_session=self.principal,
            name="Offline fixture",
            scopes=["projects:read"],
            all_projects=True,
        )
        service_token = secrets.token_urlsafe(32)
        self.auth.register_client(
            "fixture-service", service_token, self.owner, ["story"], ["projects:read"]
        )
        connect = self.app.state.agent_connect
        connect.clock = lambda: self.now
        grant = connect.issue(
            self.principal,
            name="Telegram fixture",
            authorization_profile_id="creator-full",
            authorization_profile_version=1,
            idempotency_key="fixture_grant",
            origin="http://127.0.0.1:8850",
            scope={"kind": "all"},
            permission="organise",
            client="telegram",
        )
        self.assertIsNotNone(self.auth.bearer(key["api_key"]))
        self.confirm()
        self.assertIsNone(self.auth.bearer(key["api_key"]))
        self.assertIsNone(self.auth.bearer(service_token))
        with self.repo.engine.connect() as conn:
            self.assertIsNotNone(
                conn.execute(
                    select(personal_keys.c.revoked_at).where(
                        personal_keys.c.id == key["id"]
                    )
                ).scalar_one()
            )
            self.assertEqual(
                conn.execute(
                    select(clients.c.disabled).where(clients.c.id == "fixture-service")
                ).scalar_one(),
                1,
            )
            self.assertIsNotNone(
                conn.execute(
                    select(connections.c.revoked_at).where(
                        connections.c.id == grant["connection"]["id"]
                    )
                ).scalar_one()
            )

    def test_late_signed_payment_and_refund_still_use_original_tombstoned_uuid(self):
        # Separate frozen Stripe fixture isolates obligation semantics from any
        # provider; this is not a LIVE event or a claim of legal anonymization.
        from test_payments import PaymentTests
        from studio_platform.credit_schema import entries
        from studio_platform.payment_schema import payments

        case = PaymentTests()
        case.setUp()
        try:
            payment = case.purchase()
            original_user = payment["payment"]["user_id"]
            tombstone = "deleted_" + uuid.UUID(original_user).hex
            with case.repo.transaction() as conn:
                conn.execute(
                    update(users)
                    .where(users.c.id == original_user)
                    .values(
                        owner=tombstone,
                        status="suspended",
                        email=None,
                        workos_user_id=None,
                    )
                )
            paid = case.event(payment, event_id="evt_late_original")
            self.assertEqual(case.process(paid)["state"], "processed")
            self.assertEqual(case.process(paid)["state"], "processed")
            refund = case.event(
                payment,
                event_id="evt_late_refund",
                kind="charge.refunded",
                amount_refunded=500,
            )
            self.assertEqual(case.process(refund)["state"], "processed")
            self.assertEqual(case.process(refund)["state"], "processed")
            records = case.rows(entries)
            self.assertEqual(
                [(r["kind"], r["amount"]) for r in records],
                [("purchase", 500), ("refund", -500)],
            )
            self.assertTrue(all(r["user_id"] == original_user for r in records))
            self.assertEqual(case.rows(payments)[0]["user_id"], original_user)
        finally:
            case.tearDown()
            case.doCleanups()

    def test_fresh_same_email_provider_identity_never_relinks_financial_uuid(self):
        ident = self.confirm()["operation_id"]
        self.finish(ident)
        fresh = self.app.state.workos_auth._upsert(
            {
                "subject": "user_" + "N" * 24,
                "email": "private@example.test",
                "display_name": "New fixture",
            },
            {"purpose": "login"},
            None,
        )
        self.assertNotEqual(fresh["id"], self.user_id)
        self.assertNotEqual(fresh["owner"], self.owner)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(credit_accounts.c.user_id)).scalars().all(),
                [self.user_id],
            )

    def test_concurrent_transport_epochs_only_delete_original_provider_user_once(self):
        ident = self.confirm()["operation_id"]
        self.service.turn(self.scope.tenant_id, ident)
        entered, release = threading.Event(), threading.Event()
        original = self.provider.get_user

        def held(subject):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("offline timeout")
            return original(subject)

        with (
            patch.object(self.provider, "get_user", side_effect=held),
            ThreadPoolExecutor(2) as pool,
        ):
            first = pool.submit(self.service.turn, self.scope.tenant_id, ident)
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(self.service.turn, self.scope.tenant_id, ident)
            self.assertFalse(second.result(timeout=5)["done"])
            release.set()
            self.assertFalse(first.result(timeout=5)["done"])
        self.finish(ident)
        self.assertEqual(
            [call for call in self.provider.calls if call[0] == "delete"],
            [("delete", self.subject)],
        )

    def test_admin_patch_cannot_reactivate_or_promote_original_fenced_user(self):
        from studio_platform.identity_admin import IdentityAdmin

        admin = str(uuid.uuid4())
        with self.repo.transaction() as conn:
            conn.execute(
                insert(users).values(
                    id=admin,
                    tenant=self.scope.tenant_id,
                    owner="superdan",
                    role="admin",
                    status="active",
                    display_name="Offline admin",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        self.confirm()
        service = IdentityAdmin(self.repo, self.settings)
        principal = Principal(
            "superdan",
            "browser:superdan",
            user_id=admin,
            role="admin",
            identity_source="canonical",
        )
        for change in ({"role": "admin"}, {"status": "active"}):
            with self.assertRaisesRegex(Conflict, "account_deletion_pending"):
                service.patch(
                    principal, self.user_id, {**change, "reason": "Offline fence check"}
                )
        with self.repo.engine.connect() as conn:
            current = conn.execute(
                select(users.c.role, users.c.status).where(users.c.id == self.user_id)
            ).one()
            self.assertEqual(tuple(current), ("user", "suspended"))

    def test_http_same_origin_human_cookie_only_and_pending_no_store(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import COOKIE

        actual = self.app.state.account_deletion
        actual.settings = self.settings
        actual.workos = self.workos
        self.auth.session_checker = None  # Explicit offline provider boundary.
        with TestClient(self.app, base_url="https://testserver") as client:
            self.assertEqual(
                client.post("/v1/account/deletion/preview", json={}).status_code, 401
            )
            client.cookies.set(COOKIE, self.token)
            self.assertEqual(
                client.post(
                    "/v1/account/deletion/preview",
                    json={},
                    headers={"Origin": "https://evil.test"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/v1/account/deletion/preview", json={"owner": "foreign"}
                ).status_code,
                422,
            )
            preview = client.post("/v1/account/deletion/preview", json={})
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.headers["cache-control"], "no-store")
            result = client.post(
                "/v1/account/deletion",
                json={"preview_id": preview.json()["preview_id"], "confirmed": True},
                headers={"Idempotency-Key": "http-original"},
            )
            self.assertEqual(result.status_code, 202)
            self.assertEqual(result.json()["state"], "pending")
            self.assertIn("Max-Age=0", result.headers["set-cookie"])
            self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(self.provider.calls, [])

    def test_versioned_deletion_route_preserves_original_session_workflow_identity(
        self,
    ):
        from studio_platform.dbos_transport import GenerationTransport, QUEUE
        from studio_platform.account_sessions import WORKFLOW as SESSION_WORKFLOW
        from studio_platform.account_deletion import WORKFLOW as DELETION_WORKFLOW

        calls = []
        transport = object.__new__(GenerationTransport)
        transport.client = SimpleNamespace(
            enqueue_in_transaction=lambda *args: calls.append(args)
        )
        ident = str(uuid.uuid4())
        connection = Mock()
        with patch("studio_platform.workflow_schema.assert_execution_allowed"):
            transport.enqueue_account_operation(connection, self.scope.tenant_id, ident)
            transport.enqueue_account_operation(
                connection, self.scope.tenant_id, ident, kind="deletion"
            )
            with self.assertRaisesRegex(ValueError, "account_operation_kind_invalid"):
                transport.enqueue_account_operation(
                    connection, self.scope.tenant_id, ident, kind="foreign"
                )
        self.assertEqual(
            [r[1]["workflow_name"] for r in calls],
            [SESSION_WORKFLOW, DELETION_WORKFLOW],
        )
        self.assertEqual({r[1]["queue_name"] for r in calls}, {QUEUE})
        self.assertTrue(calls[0][1]["workflow_id"].startswith("inkseq-account-"))
        self.assertTrue(calls[1][1]["workflow_id"].startswith("inkseq-account-delete-"))
        self.assertNotEqual(calls[0][1]["workflow_id"], calls[1][1]["workflow_id"])
        connection.execute.assert_not_called()

    def test_unknown_deletion_never_falls_back_to_session_or_claims_completed(self):
        from studio_platform.account_workflows import register_account_workflows

        functions, steps = {}, {}

        class Runtime:
            @staticmethod
            def step(**kwargs):
                def decorate(function):
                    steps[kwargs["name"]] = function
                    return function

                return decorate

            @staticmethod
            def workflow(**kwargs):
                def decorate(function):
                    functions[kwargs["name"]] = function
                    return function

                return decorate

            @staticmethod
            async def recv_async(*args, **kwargs):
                raise InterruptedError("offline durable wait")

        sessions = SimpleNamespace(
            turn=Mock(return_value={"done": True, "state": "completed"})
        )
        deletion = SimpleNamespace(turn=Mock(return_value=None))
        register_account_workflows(
            Runtime, self.repo, self.settings, sessions=sessions, deletion=deletion
        )
        self.assertEqual(
            set(steps),
            {"inkseq.account.lifecycle-turn.v1", "inkseq.account.deletion-turn.v1"},
        )
        self.assertEqual(
            set(functions),
            {"inkseq.account.lifecycle.v1", "inkseq.account.deletion.v1"},
        )
        self.assertEqual(
            asyncio.run(
                functions["inkseq.account.lifecycle.v1"](
                    self.scope.tenant_id, "session"
                )
            )["state"],
            "completed",
        )
        with self.assertRaises(InterruptedError):
            asyncio.run(
                functions["inkseq.account.deletion.v1"](self.scope.tenant_id, "unknown")
            )
        self.assertEqual(sessions.turn.call_count, 1)

    @unittest.skipUnless(
        importlib.util.find_spec("dbos"),
        "Pinned DBOS SDK is exercised by the workflow CI runtime",
    )
    def test_pinned_sdk_unknown_workflow_dispatch_does_not_write_terminal_outcome(self):
        from dbos import _core
        from dbos._error import DBOSWorkflowFunctionNotFoundError

        runtime = SimpleNamespace(
            _registry=SimpleNamespace(workflow_info_map={}),
            _sys_db=Mock(),
            _serializer=Mock(),
        )
        status = {
            "workflow_uuid": "original-deletion",
            "name": "inkseq.account.deletion.v1",
            "inputs": "offline",
            "serialization": None,
        }
        with (
            patch.object(
                _core, "deserialize_args", return_value={"args": (), "kwargs": {}}
            ),
            self.assertRaises(DBOSWorkflowFunctionNotFoundError),
        ):
            _core.execute_dequeued_workflow(runtime, status, "original-owner-xid")
        runtime._sys_db.update_workflow_outcome.assert_not_called()


if __name__ == "__main__":
    unittest.main()
