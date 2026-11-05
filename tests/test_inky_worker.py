"""One-shot advice orchestration with fake providers; no paid requests."""

import json
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
        with patch("studio_platform.inky_budget.REWRITE_DAILY", 0):
            with self.assertRaises(InkyRewriteUnavailable):
                worker.rewrite(
                    self.principal, self.scope.project_id, source, kind, "request-0003"
                )
        self.assertEqual(worker.examples.rewrite.call_count, 0)

    def test_automatic_examples_do_not_use_up_a_person_s_rewrites(self):
        worker = self.rewrite_worker()
        _, (source, kind) = self.rewrite_target(worker)
        with patch("studio_platform.inky_budget.ACCOUNT_DAILY", 0):
            text = worker.rewrite(
                self.principal, self.scope.project_id, source, kind, "request-0004"
            )
        self.assertEqual(text, "Rain runs down her raised hand.")
        # And rewrites leave the automatic allowance alone.
        budget = InkyBudget(self.repo)
        with patch("studio_platform.inky_budget.REWRITE_DAILY", 1):
            refused = budget.reserve(
                tenant=self.store.tenant,
                owner=self.principal.owner,
                project=self.scope.project_id,
                source_id="another-rewrite",
                max_cost_micros=1000,
                purpose="rewrite",
            )
            allowed = budget.reserve(
                tenant=self.store.tenant,
                owner=self.principal.owner,
                project=self.scope.project_id,
                source_id="an-example",
                max_cost_micros=1000,
            )
        self.assertFalse(refused["allowed"])
        self.assertTrue(allowed["allowed"])

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

    def test_a_shot_with_nobody_in_it_gets_no_advice_about_feelings(self):
        worker = self.worker()
        entry, (source, _) = self.rewrite_target(self.rewrite_worker())
        empty = {**source, "context": {**source["context"], "characters": []}}
        with patch(
            "studio_platform.inky_worker.text_findings",
            return_value=["emotion_not_shown", "vague_action"],
        ):
            findings, _ = worker.evaluate(
                self.principal, self.scope.project_id, empty, 0
            )
        self.assertEqual([f["kind"] for f in findings], ["vague_action"])

    def test_saved_advice_about_feelings_stays_hidden_on_a_shot_with_nobody(self):
        worker = self.rewrite_worker()
        self.refresh(worker)
        before = self.read()["items"]
        with patch(
            "studio_platform.inky_store.finding_applies",
            side_effect=lambda kind, value: kind != before[0]["kind"],
        ):
            after = self.read()["items"]
        self.assertNotIn(before[0]["kind"], [item["kind"] for item in after])

    def two_shots(self, first="Lin holds a letter in her right hand."):
        shot = self.project["entities"][3]
        shot["data"]["prompt"] = first
        self.project["entities"].append(
            {
                **shot,
                "id": "later",
                "order": 1,
                "data": {**shot["data"], "prompt": "Lin holds it in her left hand."},
            }
        )
        self.save()

    def continuity_worker(self, score=0.9, note=None):
        worker = self.worker()
        worker.judge.continuity = Mock(
            return_value={"answers": {"conflict": {"noul": score}}}
        )
        worker.examples.continuity = Mock(
            return_value={
                "text": note
                or json.dumps(
                    {
                        "verdict": "mismatch",
                        "with_shot": 1,
                        "note": "The letter was in her right hand.",
                    }
                ),
                "actual_micros": 500,
            }
        )
        return worker

    def continuity_items(self):
        kinds = ("continuity_mismatch", "continuity_question")
        return [i for i in self.read()["items"] if i["kind"] in kinds]

    def test_a_flagged_shot_gets_a_note_naming_the_earlier_shot_and_comes_first(self):
        self.two_shots()
        worker = self.continuity_worker()
        self.refresh(worker)
        entry = self.read()["items"][0]
        self.assertEqual(entry["kind"], "continuity_mismatch")
        self.assertEqual(entry["shot_id"], "later")
        self.assertEqual(entry["other_shot_id"], "shot")
        self.assertEqual(entry["note"], "The letter was in her right hand.")
        # Checked once: nothing changed, so nothing is asked again.
        self.refresh(worker)
        self.assertEqual(worker.judge.continuity.call_count, 1)
        self.assertEqual(self.store.pending(self.principal, self.scope.project_id), 0)

    def test_a_shot_below_the_gate_costs_no_note(self):
        self.two_shots()
        worker = self.continuity_worker(score=0.2)
        self.refresh(worker)
        worker.examples.continuity.assert_not_called()
        self.assertEqual(self.continuity_items(), [])

    def test_the_writer_can_clear_a_flag(self):
        self.two_shots()
        worker = self.continuity_worker(
            note=json.dumps({"verdict": "none", "with_shot": None, "note": ""})
        )
        self.refresh(worker)
        self.assertEqual(self.continuity_items(), [])

    def test_a_refused_check_shows_nothing_and_says_so_in_the_log(self):
        self.two_shots()
        worker = self.continuity_worker()
        worker.judge.continuity.side_effect = RuntimeError("provider said no")
        with self.assertLogs("studio_platform.inky_worker", "WARNING") as logs:
            self.refresh(worker)
        self.assertEqual(self.continuity_items(), [])
        self.assertNotIn("provider said no", "".join(logs.output))

    def test_a_note_goes_when_either_shot_changes_and_the_new_story_is_checked(self):
        self.two_shots()
        worker = self.continuity_worker()
        self.refresh(worker)
        self.assertEqual(len(self.continuity_items()), 1)
        self.project["entities"][3]["data"]["prompt"] = "Lin holds it in her left hand."
        self.save()
        self.assertEqual(self.continuity_items(), [])
        worker.judge.continuity.return_value = {"answers": {"conflict": {"noul": 0.1}}}
        self.refresh(worker)
        self.assertEqual(worker.judge.continuity.call_count, 2)
        self.assertEqual(self.continuity_items(), [])

    def test_ignoring_a_note_keeps_it_away(self):
        self.two_shots()
        worker = self.continuity_worker()
        self.refresh(worker)
        entry = self.continuity_items()[0]
        self.store.ignore(self.principal, self.scope.project_id, entry["id"])
        self.assertEqual(self.continuity_items(), [])

    def test_muted_inky_checks_nothing(self):
        self.two_shots()
        worker = self.continuity_worker()
        with self.repo.transaction() as conn:
            self.store.put(
                conn,
                self.principal,
                self.scope.project_id,
                "inky_settings",
                "settings",
                {"muted": True},
            )
        self.refresh(worker)
        worker.judge.continuity.assert_not_called()

    def test_one_refresh_stays_within_its_bounds(self):
        shot = self.project["entities"][3]
        for index in range(12):
            self.project["entities"].append(
                {
                    **shot,
                    "id": f"more{index}",
                    "order": index + 1,
                    "data": {**shot["data"], "prompt": f"Lin takes step {index}."},
                }
            )
        self.save()
        worker = self.continuity_worker()
        self.refresh(worker)
        self.assertEqual(worker.examples.continuity.call_count, 2)
        worker = self.continuity_worker(score=0.1)
        self.refresh(worker)
        self.assertEqual(worker.judge.continuity.call_count, 8)

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
