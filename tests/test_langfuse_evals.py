"""Langfuse eval datasets and runner (h3-studio#770): fake Langfuse, fake models, no network."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "langfuse_evals", ROOT / "tools" / "langfuse_evals.py"
)
evals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evals)
HAS_SDK = importlib.util.find_spec("langfuse") is not None


class FakeLangfuse:
    """The SDK calls the tool makes, in memory; run_experiment mirrors the SDK's loop."""

    def __init__(self):
        self.datasets, self.items, self.experiments = {}, {}, []
        self.flushed = 0

    def get_dataset(self, name):
        if name not in self.datasets:
            raise LookupError(name)
        items = [
            SimpleNamespace(
                id=i["id"],
                input=i["input"],
                expected_output=i["expected_output"],
                status="ACTIVE",
            )
            for i in self.items.values()
            if i["dataset_name"] == name
        ]
        return SimpleNamespace(name=name, items=items)

    def create_dataset(self, *, name, description):
        self.datasets[name] = description

    def create_dataset_item(self, **item):
        self.items[item["id"]] = item

    def run_experiment(
        self, *, name, run_name, description, data, task, evaluators, max_concurrency
    ):
        results = []
        for item in data:
            output = task(item=item)
            scores = [
                e
                for evaluator in evaluators
                for e in evaluator(output=output, input=item.input)
            ]
            results.append(SimpleNamespace(output=output, evaluations=scores))
        self.experiments.append((name, run_name, results))
        return SimpleNamespace(item_results=results)

    def flush(self):
        self.flushed += 1


class Gemini:
    def __init__(self, *plans):
        self.plans = list(plans)

    def generate(self, model, messages, instruction, **kwargs):
        from studio_platform.google_chat import payload

        payload(model, messages, instruction, kwargs["max_tokens"], True, "low")
        return {"text": json.dumps(self.plans.pop(0))}


def plan(seconds):
    return {
        "reply": "Here is the story.",
        "actions": [
            {
                "tool": "upsert_character",
                "ref": "new:a",
                "name": "Gran",
                "description": "old",
            },
            *(
                {
                    "tool": "upsert_shot",
                    "ref": f"new:s{n}",
                    "text": f"Shot {n}.",
                    "seconds": seconds,
                    "cast": ["new:a"],
                }
                for n in range(3)
            ),
        ],
    }


class DatasetTests(unittest.TestCase):
    def test_sets_cover_both_fixtures_with_stable_ids(self):
        story, inky = evals.story_items(), evals.inky_items()
        self.assertEqual(len(story), 20)
        self.assertEqual(len({i["id"] for i in story}), 20)
        self.assertEqual(story[0]["expectedOutput"], {"seconds": 30})
        self.assertEqual(len(inky), 14 + 10 + 10 + 8 + 8)
        first = inky[0]
        self.assertEqual(first["id"], "inky-text-cases-a1")
        context = first["input"]["context"]
        self.assertEqual(context["characters"], ["林夏"])
        self.assertEqual(context["place"], "雨夜站台")
        self.assertIn("深夜", context["scene_script"])
        # Exactly the bounded context the app sends Jev.
        from studio_platform.inky_models import context as checked

        for item in inky:
            checked(item["input"]["context"])

    def test_upload_is_idempotent(self):
        langfuse = FakeLangfuse()
        counts = evals.upload(langfuse)
        self.assertEqual(
            counts, {"story-chat-conversations": 20, "inky-text-cases": 50}
        )
        evals.upload(langfuse)
        self.assertEqual(len(langfuse.items), 70)
        self.assertEqual(set(langfuse.datasets), set(counts))


class RunnerTests(unittest.TestCase):
    """Runners alone: no SDK needed."""

    def item(self, case, dataset=evals.story_items):
        return next(i for i in dataset() if i["id"].endswith("-" + case))

    def test_story_scores_validity_length_and_latency(self):
        good = evals.run_story(Gemini(plan(10)), self.item("en01"))
        self.assertEqual(good["scores"]["valid_plan"], 1)
        self.assertEqual(good["scores"]["total_seconds"], 30)
        self.assertEqual(good["scores"]["length_ok"], 1)
        self.assertIn("latency_p50_s", good["scores"])
        short = evals.run_story(Gemini(plan(6)), self.item("zh01"))
        self.assertEqual(short["scores"]["length_ok"], 0)  # 18 s for a 30 s ask
        broken = evals.run_story(
            Gemini({"reply": "x", "actions": [{"tool": "nope"}]}), self.item("en02")
        )
        self.assertEqual(broken["scores"]["valid_plan"], 0)
        self.assertTrue(broken["error"].startswith("StoryChatError"))

    def test_inky_scores_against_the_human_flag(self):
        calm = {"0": 1, "1": 0, "2": 0, "3": 0}

        def judge(context):
            vague = len(context["shot_action"]) < 12
            spread = (
                {"0": 0.7, "1": 0.3, "2": 0, "3": 0}
                if vague
                else {"0": 0, "1": 0, "2": 0.3, "3": 0.7}
            )
            return {
                "answers": {
                    "spec": {"probabilities": spread},
                    "cx": {"probabilities": calm},
                    "emo": {"probabilities": {"0": 0, "1": 0, "2": 0, "3": 1}},
                    "cam": {"probabilities": calm},
                    "dur": {"probabilities": calm},
                }
            }

        vague = evals.run_inky(judge, self.item("a1", evals.inky_items))
        clear = evals.run_inky(judge, self.item("a2", evals.inky_items))
        self.assertEqual(
            (vague["scores"]["correct"], clear["scores"]["correct"]), (1, 1)
        )
        self.assertEqual(vague["scores"]["expected_score"], 0.3)


@unittest.skipUnless(HAS_SDK, "langfuse SDK (requirements.lock) not installed here")
class ExperimentTests(unittest.TestCase):
    def test_run_is_one_experiment_with_scores_and_restores_tracing(self):
        from studio_platform import llm_tracing

        langfuse = FakeLangfuse()
        evals.upload(langfuse)
        model = Gemini(plan(10), plan(6))
        runner = (evals.STORY_DATASET, lambda item: evals.run_story(model, item))
        before = llm_tracing._client
        summary = evals.run(
            langfuse, "story-chat", "test-run", 2, runner=runner, cases=["en01", "zh01"]
        )
        self.assertIs(llm_tracing._client, before)
        self.assertEqual(langfuse.flushed, 1)
        self.assertEqual([s["item"][-4:] for s in summary], ["en01", "zh01"])
        self.assertEqual([s["length_ok"] for s in summary], [1, 0])
        name, run_name, results = langfuse.experiments[0]
        self.assertEqual((name, run_name), ("story-chat-conversations", "test-run"))
        scored = {e.name: e.value for e in results[0].evaluations}
        self.assertEqual(scored["valid_plan"], 1.0)
        self.assertEqual(scored["total_seconds"], 30.0)


class CommandTests(unittest.TestCase):
    def test_dry_run_calls_nothing(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = evals.main(
                ["run", "inky", "--dry-run", "--limit", "3"], langfuse=object()
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.getvalue())["would_run"]), 3)

    def test_registry_profile_must_be_langfuse_cloud(self):
        class Config:
            base_url = "https://evil.example"
            env = {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}

        with self.assertRaises(evals.EvalError):
            evals.langfuse_from_registry(lambda service, profile: Config)


if __name__ == "__main__":
    unittest.main()
