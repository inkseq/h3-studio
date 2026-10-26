"""Local feedback safety tests with an in-memory Sentry sink, never network."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from studio_platform.auth import Principal
from studio_platform.error_monitoring import DSN
from studio_platform.product_feedback import (
    ProductFeedback,
    FeedbackBody,
    forward_feedback,
    redact_feedback,
)
from studio_platform.repository import Conflict, NotFound, Scope
from studio_platform.session_authority import write as authority_write
from test_platform_repository import LedgerCase


class Sink:
    def __init__(self):
        self.items = []

    def capture_envelope(self, envelope):
        self.items.append(envelope)


class FeedbackTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.sink = Sink()
        self.client = SimpleNamespace(
            dsn=DSN, options={"environment": "staging"}, transport=self.sink
        )
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                repository=self.repo,
                settings=SimpleNamespace(tenant_id=self.scope.tenant_id),
                error_monitoring_client=self.client,
            )
        )
        self.service = ProductFeedback(self.app)
        self.principal = Principal(self.scope.owner_id, "browser")
        self.body = {
            "category": "missing_capability",
            "severity": "low",
            "summary": "The tool cannot import a timeline",
            "suggestion": "Add timeline import",
            "source": "agent",
            "intent": "Import existing edit",
        }
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__projects"),
            "project",
            self.scope.project_id,
            {
                "id": self.scope.project_id,
                "entities": [{"id": "shot", "type": "shot", "data": {}}],
            },
        )

    def post(self, key="report-one", body=None, principal=None):
        return self.service.post(principal or self.principal, body or self.body, key)

    def agent(self, actor="client-one", **changes):
        return replace(
            Principal(
                self.scope.owner_id,
                actor,
                machine=True,
                project_ids=(self.scope.project_id,),
                scopes=("projects:read",),
            ),
            **changes,
        )

    def configure(self, enabled, revision=0, key="setting-one", principal=None):
        return self.service.configure(
            principal or self.principal,
            {"expected_revision": revision, "enabled": enabled},
            key,
        )

    def record(self, ident):
        return self.repo.get_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__feedback"),
            "product_feedback",
            ident,
        )["payload"]

    def test_one_forward_original_receipt_and_closed_modern_feedback_envelope(self):
        body = {**self.body, "project_id": self.scope.project_id, "shot_id": "shot"}
        receipt = self.post(body=body)
        self.assertEqual(receipt["forwarding"], "enqueued")
        self.assertEqual(self.post(body=body), receipt)
        self.assertEqual(len(self.sink.items), 1)
        self.assertEqual(
            self.service.get(self.principal, receipt["report_id"]), receipt
        )
        self.assertEqual(self.service.list(self.principal)["reports"], [receipt])
        with self.assertRaisesRegex(Conflict, "idempotency_conflict"):
            self.post(body={**body, "severity": "high"})
        envelope = self.sink.items[0]
        self.assertEqual(len(envelope.items), 1)
        self.assertEqual(envelope.items[0].type, "feedback")
        outbound = envelope.items[0].payload.json
        self.assertEqual(
            set(outbound),
            {"event_id", "timestamp", "platform", "environment", "contexts", "tags"},
        )
        self.assertEqual(
            outbound["contexts"], {"feedback": {"message": self.body["summary"]}}
        )
        self.assertEqual(envelope.headers["event_id"], outbound["event_id"])
        self.assertEqual(outbound["tags"]["actor_kind"], "human")
        text = json.dumps(outbound)
        for forbidden in (
            self.principal.owner,
            self.scope.project_id,
            self.body["intent"],
            self.body["suggestion"],
            '"user"',
            '"request"',
            '"trace"',
        ):
            self.assertNotIn(forbidden, text)
        altered = copy.deepcopy(self.record(receipt["report_id"]))
        altered.update(request={"cookie": "SECRET"}, user={"email": "SECRET"})
        altered["references"]["private"] = "SECRET"
        altered["references"]["owner_ref"] = "SECRET"
        forward_feedback(self.client, altered)
        self.assertNotIn(
            "SECRET",
            json.dumps(self.sink.items[-1].items[0].payload.json),
        )

    def test_opt_out_cas_replay_and_machine_cannot_override(self):
        first = self.post()
        self.assertEqual(
            self.service.settings(self.principal),
            {"revision": 0, "enabled": True, "include_context_allowed": False},
        )
        off = self.configure(False)
        self.assertEqual(self.configure(False), off)
        with self.assertRaises(Conflict):
            self.configure(True)
        with self.assertRaises(Conflict):
            self.configure(True, key="stale")
        with self.assertRaises(HTTPException) as disabled:
            self.post(key="another")
        self.assertEqual(disabled.exception.detail, "feedback_disabled")
        self.assertEqual(self.post(), first)
        self.assertEqual(len(self.sink.items), 1)
        for action in (
            lambda: self.configure(True, revision=1, principal=self.agent()),
            lambda: self.service.settings(self.agent()),
            lambda: self.post(
                principal=replace(self.principal, support_viewer="admin")
            ),
        ):
            with self.assertRaises(HTTPException):
                action()
        self.configure(True, revision=1, key="enable")
        self.post(key="now-enabled")
        self.assertEqual(len(self.sink.items), 2)

    def test_account_connection_scope_and_reference_validation(self):
        actor = self.agent()
        first = self.post(
            principal=actor,
            body={**self.body, "project_id": self.scope.project_id, "shot_id": "shot"},
        )
        for principal in (
            Principal(self.other.owner_id, "browser", role="admin"),
            self.agent("client-two"),
            replace(actor, project_ids=("different",)),
        ):
            with self.assertRaises(NotFound):
                self.service.get(principal, first["report_id"])
            self.assertEqual(self.service.list(principal)["reports"], [])
        self.assertEqual(len(self.service.list(actor)["reports"]), 1)
        self.assertEqual(len(self.service.list(self.principal)["reports"]), 1)
        with self.assertRaises(HTTPException):
            self.post(principal=replace(actor, scopes=()))
        for body in (
            {**self.body, "project_id": "foreign-project"},
            {**self.body, "project_id": self.scope.project_id, "shot_id": "missing"},
        ):
            with self.assertRaises((NotFound, ValueError)):
                self.post(key="bad-reference", body=body)
        self.assertEqual(len(self.sink.items), 1)
        with self.assertRaises(NotFound):
            ProductFeedback(
                SimpleNamespace(
                    state=SimpleNamespace(
                        repository=self.repo,
                        settings=SimpleNamespace(tenant_id="other-tenant"),
                    )
                )
            ).get(self.principal, first["report_id"])

    def test_sensitive_fields_and_context_are_rejected_before_record_or_send(self):
        for value in (
            "https://customer.example/video",
            "user@example.test",
            "Bearer supersecrettoken",
            "api_key=supersecrettoken",
            "\nsecret",
            " " * 4,
        ):
            with self.assertRaises(ValidationError):
                self.post(body={**self.body, "summary": value})
        for extra in (
            {"prompt": "secret"},
            {"artifact_id": "x"},
            {"shot_id": "x"},
            {"category": "bug"},
        ):
            with self.assertRaises(ValidationError):
                FeedbackBody.model_validate({**self.body, **extra})
        with self.assertRaises(HTTPException) as denied:
            self.post(body={**self.body, "include_context": True})
        self.assertEqual(denied.exception.detail, "feedback_context_disabled")
        self.assertEqual(self.service.list(self.principal)["reports"], [])
        self.assertEqual(self.sink.items, [])

    def test_rate_limits_atomic_under_concurrency_and_replay_costs_no_quota(self):
        def submit(i):
            try:
                return self.post(key=f"key-{i}", principal=self.agent())
            except HTTPException as error:
                self.assertEqual(error.status_code, 429)
                return None

        with (
            patch("studio_platform.product_feedback.ACTOR_HOURLY", 1),
            patch("studio_platform.product_feedback.OWNER_HOURLY", 2),
        ):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(submit, range(4)))
            self.assertEqual(sum(value is not None for value in results), 1)
            winner = next(i for i, value in enumerate(results) if value)
            self.assertEqual(
                self.post(key=f"key-{winner}", principal=self.agent()), results[winner]
            )
            self.post(key="other-actor", principal=self.agent("client-two"))
            with self.assertRaises(HTTPException) as limited:
                self.post(key="third", principal=self.agent("client-three"))
            self.assertEqual(limited.exception.status_code, 429)
            self.now += 3601
            self.post(key="later", principal=self.agent())
        self.assertEqual(len(self.sink.items), 3)

    def test_unknown_pending_and_disabled_never_resubmit_on_replay(self):
        with patch(
            "studio_platform.product_feedback.forward_feedback",
            side_effect=RuntimeError("transport unknown"),
        ) as forward:
            unknown = self.post()
            self.assertEqual(unknown["forwarding"], "unknown")
            self.assertEqual(self.post(), unknown)
            self.assertEqual(forward.call_count, 1)
        # Simulate interruption after pending was committed but before a send result.
        with patch(
            "studio_platform.product_feedback.forward_feedback",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.post(key="interrupted")
        pending = self.post(key="interrupted")
        self.assertEqual(pending["forwarding"], "pending")
        self.assertEqual(self.sink.items, [])
        self.app.state.error_monitoring_client = None
        self.assertEqual(self.post(key="disabled")["forwarding"], "disabled")

    def test_same_key_concurrency_does_not_forward_twice(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.post(), range(4)))
        self.assertEqual(len({r["event_id"] for r in results}), 1)
        self.assertEqual(len(self.sink.items), 1)
        self.assertEqual(self.post()["forwarding"], "enqueued")

    def test_deletion_gate_and_explicit_purge_redact_only_matching_local_reports(self):
        report = self.post(body={**self.body, "project_id": self.scope.project_id})
        own = self.post(key="unrelated")
        foreign = Principal(self.other.owner_id, "browser")
        other = self.post(principal=foreign, key="foreign")
        with self.repo.transaction() as conn:
            redact_feedback(
                conn,
                self.scope.tenant_id,
                self.scope.owner_id,
                project_ids=(self.scope.project_id,),
                now=self.now,
            )
        self.assertEqual(self.record(report["report_id"]), {"content_deleted": True})
        with self.assertRaises(NotFound):
            self.service.get(self.principal, report["report_id"])
        self.assertEqual(self.service.get(self.principal, own["report_id"]), own)
        self.assertEqual(self.service.get(foreign, other["report_id"]), other)
        with self.repo.transaction() as conn:
            authority_write(
                conn,
                self.scope.tenant_id,
                "deletion_owner",
                self.scope.owner_id,
                {"operation_id": "deletion"},
                self.now,
            )
        for action in (
            lambda: self.post(key="after-deletion"),
            lambda: self.configure(False),
        ):
            with self.assertRaisesRegex(Conflict, "account_deletion_pending"):
                action()
        self.assertEqual(len(self.sink.items), 3)

    def test_disabled_or_foreign_transport_never_falls_back(self):
        receipt = self.post()
        record = self.record(receipt["report_id"])
        for client in (
            None,
            SimpleNamespace(dsn="", transport=self.sink),
            SimpleNamespace(dsn="https://foreign.example", transport=self.sink),
        ):
            self.assertEqual(forward_feedback(client, record), "disabled")
        self.assertEqual(len(self.sink.items), 1)
