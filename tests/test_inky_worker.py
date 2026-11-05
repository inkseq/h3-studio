"""One-shot advice orchestration with fake providers; no paid requests."""

from unittest.mock import Mock, patch

from studio_platform.inky_worker import (
    InkyRewriteFailed,
    InkyRewriteUnavailable,
    InkyWorker,
)
from studio_platform.inky_budget import InkyBudget
from test_inky_store import InkyStoreTests


class InkyWorkerTests(InkyStoreTests):
    def worker(self, *, examples=None):
        p0 = {"probabilities": {"0": 1, "1": 0, "2": 0, "3": 0}}
        p3 = {"probabilities": {"0": 0, "1": 0, "2": 0, "3": 1}}
        judge = Mock(
            return_value={
                "answers": {"spec": p0, "cx": p3, "emo": p3, "cam": p0, "dur": p0}
            }
        )
        if examples is None:
            examples = Mock(
                return_value={
                    "text": '{"example":"Rain wets the ticket."}',
                    "actual_micros": 600,
                }
            )
            examples.max_cost_micros = 1000
        return InkyWorker(self.store, judge=judge, examples=examples)

    def refresh(self, worker):
        worker.refresh(self.principal, self.scope.project_id)

    def test_refresh_read_and_replay_never_duplicate_providers(self):
        worker = self.worker()
        self.refresh(worker)
        self.assertEqual(worker.judge.call_count, 1)
        self.assertEqual(worker.examples.call_count, 2)
        self.assertTrue(all("example" in item for item in self.read()["items"]))
        self.refresh(worker)
        self.read()
        self.assertEqual(worker.judge.call_count, 1)
        self.assertEqual(worker.examples.call_count, 2)

    def rewrite_worker(self, answer='{"text":"Rain runs down her raised hand."}'):
        worker = self.worker()
        worker.examples.rewrite = Mock(
            return_value={"text": answer, "actual_micros": 300}
        )
        return worker

    def rewrite_target(self, worker):
        self.refresh(worker)
        entry = next(
            item
            for item in self.read()["items"]
            if item["kind"] in {"vague_action", "emotion_not_shown"}
        )
        return entry, self.store.rewrite_target(
            self.principal, self.scope.project_id, entry["id"]
        )

    def test_rewrite_returns_a_draft_and_saves_nothing(self):
        worker = self.rewrite_worker()
        _, (source, kind) = self.rewrite_target(worker)
        before = self.read()
        text = worker.rewrite(
            self.principal, self.scope.project_id, source, kind, "request-0001"
        )
        self.assertEqual(text, "Rain runs down her raised hand.")
        self.assertEqual(worker.examples.rewrite.call_count, 1)
        self.assertEqual(before, self.read())

    def test_rewrite_same_request_never_calls_twice(self):
        worker = self.rewrite_worker()
        _, (source, kind) = self.rewrite_target(worker)
        args = (self.principal, self.scope.project_id, source, kind, "request-0002")
        worker.rewrite(*args)
        with self.assertRaises(InkyRewriteUnavailable):
            worker.rewrite(*args)
        self.assertEqual(worker.examples.rewrite.call_count, 1)

    def test_rewrite_over_the_allowance_calls_nothing(self):
        worker = self.rewrite_worker()
        _, (source, kind) = self.rewrite_target(worker)
        with patch("studio_platform.inky_budget.ACCOUNT_DAILY", 0):
            with self.assertRaises(InkyRewriteUnavailable):
                worker.rewrite(
                    self.principal, self.scope.project_id, source, kind, "request-0003"
                )
        self.assertEqual(worker.examples.rewrite.call_count, 0)

    def test_rewrite_refuses_an_unusable_answer(self):
        for answer in (
            "not json",
            '{"text":""}',
            '{"text":"ok","extra":1}',
            '{"text":"See https://example.test now"}',
        ):
            with self.subTest(answer=answer):
                worker = self.rewrite_worker(answer)
                _, (source, kind) = self.rewrite_target(worker)
                with self.assertRaises(InkyRewriteFailed):
                    worker.rewrite(
                        self.principal,
                        self.scope.project_id,
                        source,
                        kind,
                        "request-" + str(abs(hash(answer)))[:10],
                    )

    def test_rewrite_route_answers_and_refuses_plainly(self):
        worker = self.rewrite_worker()
        entry, _ = self.rewrite_target(worker)
        path = (
            f"/v1/inky/suggestions/{entry['id']}/rewrite"
            f"?project_id={self.scope.project_id}"
        )
        body = {"request_id": "route-request-1"}
        with self.client() as client:
            # No evaluator on this deployment: nothing to call.
            client.app.state.inky_worker = None
            self.assertEqual(client.post(path, json=body).status_code, 503)
            client.app.state.inky_worker = worker
            done = client.post(path, json=body)
            self.assertEqual(done.status_code, 200, done.text)
            self.assertEqual(
                done.json(),
                {
                    "text": "Rain runs down her raised hand.",
                    "shot_id": entry["shot_id"],
                },
            )
            self.assertEqual(done.headers["cache-control"], "no-store")
            # The same request again spends nothing more.
            self.assertEqual(client.post(path, json=body).status_code, 429)
            self.assertEqual(
                client.post(path, json={"request_id": "no"}).status_code, 422
            )
            stale = "f" * 64
            self.assertEqual(
                client.post(
                    f"/v1/inky/suggestions/{stale}/rewrite"
                    f"?project_id={self.scope.project_id}",
                    json=body,
                ).status_code,
                409,
            )
        self.assertEqual(worker.examples.rewrite.call_count, 1)

    def test_budget_exhaustion_keeps_templates_and_logs_without_context(self):
        InkyBudget(self.repo).reserve(
            tenant="other",
            owner="other",
            project="other",
            source_id="hold",
            max_cost_micros=20_000_000,
        )
        worker = self.worker()
        with self.assertLogs("studio_platform.inky_worker", level="INFO") as logs:
            self.refresh(worker)
        self.assertEqual(worker.examples.call_count, 0)
        self.assertEqual(len(self.read()["items"]), 2)
        self.assertTrue(all("example" not in item for item in self.read()["items"]))
        self.assertTrue(any("inky_example_budget" in log for log in logs.output))
        self.assertNotIn("Rain", str(logs.output))

    def test_unknown_provider_failure_retains_budget_and_never_replays(self):
        provider = Mock(side_effect=RuntimeError("SECRET provider body"))
        provider.max_cost_micros = 10_000_000
        worker = self.worker(examples=provider)
        with self.assertLogs("studio_platform.inky_worker", level="INFO") as logs:
            self.refresh(worker)
        self.refresh(worker)
        self.assertEqual(provider.call_count, 2)
        self.assertNotIn("SECRET", str(logs.output))
        denied = InkyBudget(self.repo).reserve(
            tenant="new", owner="new", project="new", source_id="new", max_cost_micros=1
        )
        self.assertEqual(denied["reason"], "inky_example_budget")

    def test_source_changed_during_judgement_gets_no_examples_or_old_advice(self):
        worker = self.worker()
        response = worker.judge.return_value

        def changed(source):
            self.project["entities"][-1]["data"]["prompt"] = "Changed."
            self.save()
            return response

        worker.judge.side_effect = changed
        self.refresh(worker)
        self.assertEqual(worker.examples.call_count, 0)
        self.assertEqual(self.read(), {"items": []})

    def test_mute_blocks_judgement_and_examples(self):
        self.store.settings(
            self.principal,
            self.scope.project_id,
            {"expected_revision": 0, "muted": True},
        )
        worker = self.worker()
        self.refresh(worker)
        self.assertEqual(worker.judge.call_count, 0)
        self.assertEqual(worker.examples.call_count, 0)


def load_tests(loader, tests, pattern):
    import unittest

    # Reuse the disposable fixture without repeating the store module's cases.
    return unittest.TestSuite(
        InkyWorkerTests(name)
        for name in InkyWorkerTests.__dict__
        if name.startswith("test_")
    )
