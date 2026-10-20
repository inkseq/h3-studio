"""Demo adapter: canned storyboards from the user's own photos (#382).

Never calls GPU, credits, production, an LLM or the Inkseq API. Revisions use
simple phrase rules so the whole chat flow can be tried before the backend.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import uuid

from . import render
from .adapter import Card, Estimate, Film, Storyboard

THEMES = [
    (("咖啡", "coffee", "cafe", "café"), "咖啡店",
     ["门头开场", "手冲特写", "招牌饮品", "店内氛围", "客人笑脸", "细节质感"]),
    (("餐", "饭", "菜", "面", "restaurant", "food"), "餐厅",
     ["门头开场", "招牌菜特写", "后厨烟火", "用餐氛围", "客人笑脸", "细节质感"]),
    (("发", "美发", "理发", "salon", "hair"), "美发店",
     ["门头开场", "造型过程", "前后对比", "店内环境", "顾客满意", "细节质感"]),
    (("花", "flower", "florist"), "花店",
     ["门头开场", "花束特写", "包花过程", "店内一角", "送花瞬间", "细节质感"]),
    (("健身", "gym", "fitness"), "健身房",
     ["门头开场", "器械特写", "训练瞬间", "教练指导", "会员笑脸", "细节质感"]),
]  # fmt: skip
GENERIC = ("小店", ["门头开场", "主打产品", "制作过程", "店内氛围", "客人笑脸", "细节质感"])
MOTIONS = ["缓慢推近", "左右平移", "轻微拉远", "跟随移动"]
MAX_CARDS = 8
CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
             "八": 8, "九": 9, "十": 10}  # fmt: skip


def number(text):
    return int(text) if text.isdigit() else CN_DIGITS.get(text)


NUM = r"(\d+|[一二两三四五六七八九十])"


class DemoAdapter:
    def __init__(self, workdir):
        self.workdir = Path(workdir)

    def _folder(self, board):
        folder = self.workdir / board.id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def draft(self, chat_id, photos, brief):
        text = brief.lower()
        theme, roles = next(
            ((name, roles) for keys, name, roles in THEMES if any(k in text for k in keys)),
            GENERIC,
        )
        cards = []
        for index, _photo in enumerate(photos[: MAX_CARDS - 1]):
            role = roles[index % len(roles)]
            cards.append(
                Card(
                    title=role,
                    description=f"照片{index + 1}，{MOTIONS[index % len(MOTIONS)]}",
                    photo=index + 1,
                    seconds=3.0,
                )
            )
        cards.append(
            Card(
                title="收尾",
                description=(brief.strip()[:40] or "欢迎光临") + "，店名和地址",
                photo=1,
                seconds=3.0,
            )
        )
        board = Storyboard(
            id=uuid.uuid4().hex[:12],
            title=f"{theme}宣传片（演示）",
            brief=brief,
            photos=list(photos),
            cards=cards,
            aspect="16:9" if "横屏" in brief else "9:16",
        )
        return board

    def revise(self, board, instruction):
        board = Storyboard.from_dict(board.to_dict())
        text = instruction.strip()
        changes = []
        # Card numbers always refer to the board the user is looking at, so
        # replacements apply first and deletions last, highest number first.
        for match in re.finditer(rf"第{NUM}张[^张，,。；;]{{0,6}}?(?:换成|用)照片{NUM}", text):
            n, p = number(match.group(1)), number(match.group(2))
            if n and p and 1 <= n <= len(board.cards) and 1 <= p <= len(board.photos):
                board.cards[n - 1].photo = p
                board.cards[n - 1].description = f"照片{p}，{MOTIONS[n % len(MOTIONS)]}"
                changes.append(f"第{n}张换成照片{p}")
        doomed = {
            n
            for match in re.finditer(rf"(?:删掉|删除|去掉)第?{NUM}张", text)
            if (n := number(match.group(1))) and 1 <= n <= len(board.cards)
        }
        for n in sorted(doomed, reverse=True):
            if len(board.cards) > 1:
                board.cards.pop(n - 1)
                changes.append(f"删掉了第{n}张")
        if "横屏" in text and board.aspect != "16:9":
            board.aspect = "16:9"
            changes.append("改成横屏")
        if "竖屏" in text and board.aspect != "9:16":
            board.aspect = "9:16"
            changes.append("改成竖屏")
        for words, factor, label in (
            (("短一点", "快一点", "shorter"), 0.67, "整体缩短"),
            (("长一点", "慢一点", "longer"), 1.3, "整体放慢"),
        ):
            if any(w in text for w in words):
                for card in board.cards:
                    card.seconds = float(max(2, min(6, round(card.seconds * factor))))
                changes.append(label)
        if not changes:
            match = re.search(rf"第?{NUM}张(.+)", text)
            n = number(match.group(1)) if match else None
            if n and 1 <= n <= len(board.cards):
                board.cards[n - 1].note = match.group(2).strip(" ，,。")
                board.cards[n - 1].description = board.cards[n - 1].note[:40]
                changes.append(f"第{n}张：{board.cards[n - 1].note[:20]}")
            else:
                board.style = text[:60]
                changes.append(f"整体风格：{board.style[:20]}")
        board.revision += 1
        return board, "；".join(changes)

    def grid(self, board):
        return render.grid(
            board, str(self._folder(board) / f"grid-r{board.revision}.jpg")
        )

    def estimate(self, board):
        return Estimate(
            credits=int(round(board.duration * 10)),
            seconds=board.duration,
            note="演示模式：不会扣积分，也不会用显卡",
        )

    def generate(self, chat_id, board, progress):
        folder = self._folder(board)
        clips = []
        for number_, card, _start, _end in board.timeline():
            progress(number_ - 1, len(board.cards))
            clips.append(render.clip(board, number_, card, folder))
        progress(len(board.cards), len(board.cards))
        path = render.concat(clips, str(folder / f"film-r{board.revision}.mp4"))
        return Film(path=path, storyboard_id=board.id, revision=board.revision, clips=clips)

    def regenerate_card(self, chat_id, board, film, number_, note, progress):
        board = Storyboard.from_dict(board.to_dict())
        card = board.cards[number_ - 1]
        card.version += 1
        if note:
            card.note = note[:60]
            card.description = f"按反馈重做：{card.note[:30]}"
        board.revision += 1
        folder = self._folder(board)
        progress(0, 1)
        clips = list(film.clips)
        clips[number_ - 1] = render.clip(board, number_, card, folder)
        progress(1, 1)
        path = render.concat(clips, str(folder / f"film-r{board.revision}.mp4"))
        return board, replace(film, path=path, revision=board.revision, clips=clips)

    def frame(self, film, second):
        out = Path(film.path).with_name(f"frame-{second:.1f}s.jpg")
        return render.frame(film.path, second, str(out))
