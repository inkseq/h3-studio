"""Weekly feedback digest: python -m telegram_bot.digest --home <dir> [--days 7]

Reads the private feedback log, computes product health numbers, and asks
Gemini (one call) to group what people said into themes. Writes an
anonymised Markdown report next to the log. It never posts anywhere: the
owner or a reviewer decides what becomes an issue in the public repository.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time

from .feedback import FeedbackLog

SAID = {"idea", "rating_note", "model_request", "model_complaint", "model_praise"}
THEMES_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "≤15字主题"},
                    "kind": {"type": "string", "enum": ["功能需求", "不好用", "出错", "表扬"]},
                    "count": {"type": "integer"},
                    "people": {"type": "integer"},
                    "example": {"type": "string", "description": "转述一句代表性的话，去掉店名人名电话地址"},
                    "next_step": {"type": "string", "description": "≤30字建议下一步"},
                    "ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "kind", "count", "people", "example", "next_step", "ids"],
            },
        }
    },
    "required": ["themes"],
}


def health(rows):
    kinds = Counter(r["kind"] for r in rows)
    ratings = [int(r["text"]) for r in rows if r["kind"] == "rating" and r["text"].isdigit()]
    delivered = kinds["film_delivered"]
    fixes = kinds["fix_applied"]
    return {
        "people": len({r["person"] for r in rows}),
        "films_delivered": delivered,
        "satisfied": kinds["satisfied"],
        "fixes_per_film": round(fixes / delivered, 2) if delivered else None,
        "average_rating_of_3": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "ratings": len(ratings),
        "not_understood": kinds["not_understood"],
        "wrong_part_guessed": kinds["wrong_part"],
        "fix_reverted": kinds["reverted"],
        "stale_buttons": kinds["stale_button"],
        "errors": kinds["error"],
        "unsupported": dict(Counter(r["text"] for r in rows if r["kind"] == "unsupported")),
        "abandoned_before_film": sum(1 for r in rows if r["kind"] == "new_film"
                                     and r.get("previous_phase") != "delivered"),  # fmt: skip
    }


def themes(llm, rows):
    said = [{"id": r["id"], "kind": r["kind"], "person": r["person"],
             "text": r.get("summary") or r["text"]} for r in rows if r["kind"] in SAID]  # fmt: skip
    if not said:
        return []
    prompt = ("下面是小店老板用宣传片 bot 时说的话（JSON）。把它们归成主题，"
              "每个主题给出类别、条数、涉及几个人（按 person 去重）、一句转述的代表性原话（去掉店名、人名、电话、地址）、"
              "建议下一步和对应的 id。相近的需求合并。\n" + json.dumps(said, ensure_ascii=False))  # fmt: skip
    return llm.json("digest", [{"text": prompt}], THEMES_SCHEMA)["themes"]


def report(rows, theme_rows, days):
    h = health(rows)
    lines = [f"# 反馈周报（最近 {days} 天，{time.strftime('%Y-%m-%d')}）", "",
             "## 产品健康", ""]  # fmt: skip
    labels = {
        "people": "用户数", "films_delivered": "出片数", "satisfied": "点了满意",
        "fixes_per_film": "平均每条片子返修次数", "average_rating_of_3": "平均评分（满分3）",
        "ratings": "评分条数", "not_understood": "没听懂", "wrong_part_guessed": "猜错段落",
        "fix_reverted": "改完又换回旧版", "stale_buttons": "点了过期按钮", "errors": "出错",
        "unsupported": "发了暂不支持的内容", "abandoned_before_film": "没出片就重新开始",
    }  # fmt: skip
    lines += [f"- {labels[k]}：{v}" for k, v in h.items()]
    lines += ["", "## 用户在说什么", ""]
    if not theme_rows:
        lines.append("（这段时间没有文字反馈）")
    for t in sorted(theme_rows, key=lambda t: (-t["people"], -t["count"])):
        lines += [f"### {t['title']}（{t['kind']}，{t['count']} 条 / {t['people']} 人）",
                  f"> {t['example']}", f"- 建议：{t['next_step']}",
                  f"- 反馈 id：{', '.join(t['ids'][:12])}", ""]  # fmt: skip
    lines += ["", "原始反馈只在本机私有日志里；公开仓库只放这份不含个人信息的摘要。"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    from .__main__ import make_adapter  # reuse the configured Gemini profile

    parser = argparse.ArgumentParser(prog="telegram_bot.digest")
    parser.add_argument("--home", default=".local/telegram-bot")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--ship", nargs="+", metavar="ID", help="mark feedback ids as shipped")
    parser.add_argument("--note", default="", help="one line for the people who asked")
    args = parser.parse_args(argv)
    home = Path(args.home)
    if args.ship:
        log = FeedbackLog(home / "feedback.jsonl")
        for row_id in args.ship:
            log.mark(row_id, status="shipped", shipped_note=args.note[:200])
        print(f"marked {len(args.ship)} shipped; the bot tells opted-in people within a minute")
        return
    rows = FeedbackLog(home / "feedback.jsonl").rows(since=time.time() - args.days * 86400)
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    adapter = make_adapter({"adapter": "llm", "llm": config["llm"]}, home / "digest-work")
    out = home / "digests" / f"feedback-{time.strftime('%Y%m%d')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report(rows, themes(adapter.llm, rows), args.days), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
