"""Story chat and Inky evals on Langfuse datasets (h3-studio#770).

    python tools/langfuse_evals.py upload
    python tools/langfuse_evals.py run story-chat --limit 2 --run-name prompt-x
    python tools/langfuse_evals.py run inky --limit 2 --run-name jev-x
    python tools/langfuse_evals.py run story-chat --dry-run

``upload`` puts the fixed eval sets into Langfuse datasets. Item ids are stable, so a
second upload updates the same items instead of adding copies.

``run`` is one Langfuse experiment (the pinned SDK's ``run_experiment``) over a
dataset. Each item goes through the production code path: story chat uses the same
instruction, prompt messages, plan parser and card rules as a real turn, with Gemini;
Inky uses the same Jev judgement as the story page. The app's own tracing records
each model call under its item, and each item gets scores. Every prompt, model or
setting change is then compared on the same items in Langfuse's experiment view.
``--dry-run`` makes no model call and writes nothing.

Credentials come only from the central registry: ``langfuse--inkseq`` for Langfuse and
the app's own Gemini and TypeSafe profiles for the models. OpenRouter is never used.
Runs are capped by ``--limit`` (default 2); a full run is a deliberate choice.
Needs the ``langfuse`` package from requirements.lock (the WSL or app virtualenv).

The eval sets are synthetic (no person's story or picture). Retention of what the app
itself sends to Langfuse is described in docs/AI-FLOWS.zh-CN.md.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"
STORY_SET = FIXTURES / "story-chat" / "eval-conversations.json"
INKY_SET = FIXTURES / "inky" / "eval-cases.json"
STORY_DATASET = "story-chat-conversations"
INKY_DATASET = "inky-text-cases"
# The text cases of the Inky set; upload_card needs pictures and stays in the test suite.
INKY_KINDS = {
    "vague_action": "spec",
    "needs_keyframe": "cx",
    "emotion_not_shown": "emo",
    "multiple_camera_moves": "cam",
    "action_too_long": "dur",
}
TEXT_LIMIT = 2000


class EvalError(RuntimeError):
    """Static messages; never a credential or a response body."""


def clip(value, limit=TEXT_LIMIT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


# ---- eval sets ---------------------------------------------------------------


def story_items(path=STORY_SET):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        {
            "id": f"{STORY_DATASET}-{conv['id']}",
            "input": {
                "lang": conv["lang"],
                "kind": conv["kind"],
                "turns": conv["turns"],
            },
            "expectedOutput": conv.get("expected") or {},
            "metadata": {"case": conv["id"], "source": "h3-studio#756"},
        }
        for conv in value["conversations"]
    ]


def inky_items(path=INKY_SET):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    story = value["story"]
    cards = {card["id"]: card for card in story["cards"]}
    items = []
    for kind in INKY_KINDS:
        for case in value[kind]:
            items.append(
                {
                    "id": f"{INKY_DATASET}-{case['id']}",
                    "input": {
                        "kind": kind,
                        "context": inky_context(story, cards, case),
                    },
                    "expectedOutput": {"flag": bool(case["flag"])},
                    "metadata": {
                        "case": case["id"],
                        "threshold": value["thresholds"].get(kind),
                        "source": "h3-studio#687",
                    },
                }
            )
    return items


def inky_context(story, cards, case):
    """The bounded context the story page sends Jev for one shot."""
    from studio_platform.inky_models import language

    def names(ids):
        return [cards[i]["name"] for i in ids if i in cards]

    return {
        "shot_action": case["text"],
        "scene_script": story["script"].get(case.get("scene"), ""),
        "characters": names(case.get("cast", [])),
        "place": cards.get(case.get("place"), {}).get("name", ""),
        "props": names(case.get("props", [])),
        "lang": language(case["text"]),
        "seconds": case.get("seconds", 5),
    }


DATASETS = {
    STORY_DATASET: (
        story_items,
        "Story chat conversations (#756 A/B set): plan validity, length, latency.",
    ),
    INKY_DATASET: (
        inky_items,
        "Inky shot-text findings (#687 fixtures): Jev flag against the human label.",
    ),
}


# ---- Langfuse -------------------------------------------------------------------


def langfuse_from_registry(loader=None):
    """The pinned Langfuse SDK on the central ``langfuse--inkseq`` profile."""
    from studio_platform.central_registry import load_api
    from studio_platform.langfuse_config import ORIGINS, PROFILE

    config = (loader or load_api)("langfuse", profile=PROFILE)
    if (config.base_url or "").rstrip("/") not in ORIGINS:
        raise EvalError("langfuse_profile_invalid")
    from langfuse import Langfuse

    return Langfuse(
        public_key=config.env["LANGFUSE_PUBLIC_KEY"],
        secret_key=config.env["LANGFUSE_SECRET_KEY"],
        base_url=config.base_url,
        environment="eval",
    )


def as_item(item):
    """A dataset item (SDK object or dict) as the plain dict the runners read."""
    if isinstance(item, dict):
        return copy.deepcopy(item)
    return {
        "id": item.id,
        "input": copy.deepcopy(item.input),
        "expectedOutput": copy.deepcopy(item.expected_output),
    }


def scores_of(*, output, **kwargs):
    """Experiment evaluator: the scores each runner already measured, one per name."""
    from langfuse import Evaluation

    return [
        Evaluation(name=name, value=float(value))
        for name, value in (output or {}).get("scores", {}).items()
    ]


# ---- runners ------------------------------------------------------------------


def run_story(client, item):
    """Every turn of one conversation, carried on one in-memory story like a real chat."""
    from studio_platform import story_chat as sc
    from studio_platform.guided import Guided
    from studio_platform.story_chat_advice import instruction, turn_chat

    guided = Guided.__new__(Guided)  # Only its pure document apply is used.
    project = sc.new_story("eval-" + item["id"], "Untitled", item["input"]["lang"])
    latencies, valid, error = [], True, None
    for index, text in enumerate(item["input"]["turns"]):
        chat = turn_chat(sc.chat_of(project), text)
        started = time.monotonic()
        try:
            result = client.generate(
                sc.MODEL,
                sc.prompt_messages(project, chat, text),
                instruction(sc.INSTRUCTION, True),
                max_tokens=4096,
                json_mode=True,
                thinking=sc.THINKING,
            )
            latencies.append(time.monotonic() - started)
            plan = sc._parse(result["text"])
            actions, changes, proposal, _ = sc.apply_plan(
                project, chat, plan, model_fresh=True
            )
            for action in actions:
                guided.apply(project, action)
            project["journey"]["storyChat"] = sc.append_turn(
                chat, f"eval-{index:02d}-turn", text, plan, changes, proposal
            )
        except Exception as caught:
            valid = False
            error = type(caught).__name__ + ":" + str(getattr(caught, "code", ""))
            break
    shots = sc.shots_of(project, sc.chat_of(project))
    seconds = sum((shot.get("data") or {}).get("seconds", 5) for shot in shots)
    expected = item.get("expectedOutput") or {}
    scores = {"valid_plan": int(valid), "shots": len(shots), "total_seconds": seconds}
    if latencies:
        scores["latency_p50_s"] = round(statistics.median(latencies), 2)
        scores["latency_max_s"] = round(max(latencies), 2)
    if "seconds" in expected:
        target = expected["seconds"]
        scores["length_ok"] = int(abs(seconds - target) <= max(5, 0.2 * target))
    if "shots" in expected:
        scores["shot_count_ok"] = int(len(shots) == expected["shots"])
    reply = (sc.chat_of(project).get("messages") or [{}])[-1].get("text", "")
    return {
        "reply": clip(reply),
        "cards": sc.cards(project, sc.chat_of(project)),
        "error": error,
        "scores": scores,
    }


def run_inky(judge, item):
    """One Jev judgement, scored against the human flag for its finding."""
    from studio_platform.inky_models import expected_score, text_findings

    kind, context = item["input"]["kind"], item["input"]["context"]
    response = judge(context)
    value = expected_score(response["answers"][INKY_KINDS[kind]])
    flagged = kind in text_findings(response)
    return {
        "flagged": flagged,
        "scores": {
            "correct": int(flagged == item["expectedOutput"]["flag"]),
            "expected_score": round(value, 3),
        },
    }


def default_runner(name):
    if name == "story-chat":
        from studio_platform.google_chat import GoogleChatClient

        client = GoogleChatClient()
        return STORY_DATASET, lambda item: run_story(client, item)
    from studio_platform.inky_providers import Jev

    judge = Jev()
    return INKY_DATASET, lambda item: run_inky(judge, item)


# ---- commands -------------------------------------------------------------------


def upload(langfuse):
    counts = {}
    for name, (load, description) in DATASETS.items():
        try:
            langfuse.get_dataset(name)
        except Exception:
            langfuse.create_dataset(name=name, description=description)
        items = load()
        for item in items:
            # The fixed id makes this an update of the same item, never a copy.
            langfuse.create_dataset_item(
                dataset_name=name,
                id=item["id"],
                input=item["input"],
                expected_output=item["expectedOutput"],
                metadata=item["metadata"],
            )
        counts[name] = len(items)
    return counts


def run(langfuse, name, run_name, limit, runner=None, cases=None):
    """One Langfuse experiment on the dataset; returns each item's scores."""
    from studio_platform import llm_tracing

    dataset, task = runner or default_runner(name)
    items = [
        i
        for i in langfuse.get_dataset(dataset).items
        if str(getattr(i, "status", "ACTIVE")).endswith("ACTIVE")
    ]
    items.sort(key=lambda i: i.id)
    if cases:
        items = [i for i in items if i.id.rsplit("-", 1)[-1] in cases]
    items = items[:limit]
    # The app's own model-call tracing records each call under its experiment item.
    previous, llm_tracing._client = llm_tracing._client, langfuse
    try:
        result = langfuse.run_experiment(
            name=dataset,
            run_name=run_name,
            description=f"{name} eval via tools/langfuse_evals.py",
            data=items,
            task=lambda *, item, **_: task(as_item(item)),
            evaluators=[scores_of],
            max_concurrency=1,
        )
    finally:
        llm_tracing._client = previous
        langfuse.flush()
    return [
        {
            "item": item.id,
            **((res.output or {}).get("scores") or {"error": "task_failed"}),
        }
        for item, res in zip(items, result.item_results, strict=True)
    ]


def main(argv=None, langfuse=None):
    parser = argparse.ArgumentParser(
        description="Story chat and Inky evals on Langfuse."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("upload", help="put the eval sets into Langfuse datasets")
    runs = sub.add_parser("run", help="run one dataset as a Langfuse experiment")
    runs.add_argument("target", choices=("story-chat", "inky"))
    runs.add_argument("--run-name", default=None)
    runs.add_argument("--limit", type=int, default=2)
    runs.add_argument("--case", action="append", help="only this case id (repeatable)")
    runs.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "run" and args.dry_run:
        load = story_items if args.target == "story-chat" else inky_items
        chosen = [
            i["id"]
            for i in load()
            if not args.case or i["id"].rsplit("-", 1)[-1] in args.case
        ]
        print(json.dumps({"would_run": chosen[: args.limit]}, ensure_ascii=False))
        return 0
    langfuse = langfuse or langfuse_from_registry()
    if args.command == "upload":
        print(json.dumps(upload(langfuse)))
        return 0
    run_name = (
        args.run_name or f"{args.target}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    )
    summary = run(langfuse, args.target, run_name, args.limit, cases=args.case)
    print(json.dumps({"run": run_name, "items": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
