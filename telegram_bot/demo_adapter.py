"""Demo adapter: canned results from the user's own photos (#382).

Never calls GPU, credits, production, an LLM or the Inkseq API. Material
names, the outline, shot prompts and fix rewrites come from simple phrase
rules so the whole flow can be tried before the real backend exists.
"""

from __future__ import annotations

from pathlib import Path
import re
import uuid

from . import render
from .adapter import MODES, Card, Estimate, Film, FixProposal, Storyboard

# (keywords, shop, material names, outline beats)
THEMES = [
    (("咖啡", "coffee", "cafe", "café"), "咖啡店",
     ["门头", "手冲", "招牌饮品", "店内", "客人", "细节"],
     ["清晨，店门打开，阳光照进来", "咖啡师专注地手冲一杯咖啡", "招牌饮品上桌，热气升起",
      "客人在窗边坐下，笑着聊天", "店名和地址，欢迎来坐坐"]),
    (("餐", "饭", "菜", "面", "restaurant", "food"), "餐厅",
     ["门头", "招牌菜", "后厨", "用餐区", "客人", "细节"],
     ["门口招牌亮起，客人走进来", "后厨火光，招牌菜出锅", "热菜上桌，特写冒热气",
      "一家人围坐，吃得开心", "店名和地址，欢迎来尝尝"]),
    (("发", "美发", "理发", "salon", "hair"), "美发店",
     ["门头", "造型过程", "成品造型", "店内", "顾客", "细节"],
     ["门口转灯亮起", "发型师专注地剪发造型", "转身亮相，前后对比",
      "顾客照镜子，满意地笑", "店名和预约方式"]),
    (("花", "flower", "florist"), "花店",
     ["门头", "花束", "包花过程", "店内一角", "送花", "细节"],
     ["清晨，花店门口摆满鲜花", "店主挑花、修枝、包花", "花束特写，花瓣上的水珠",
      "顾客接过花束，惊喜地笑", "店名和地址，节日可预订"]),
    (("健身", "gym", "fitness"), "健身房",
     ["门头", "器械", "训练", "教练", "会员", "细节"],
     ["清晨，健身房灯光亮起", "器械特写，金属反光", "会员挥汗训练",
      "教练一对一指导", "店名和体验课信息"]),
]  # fmt: skip
GENERIC = ("小店", ["门头", "主打产品", "制作过程", "店内", "客人", "细节"],
           ["店门打开，开始新的一天", "主打产品特写", "用心制作的过程",
            "客人满意地笑", "店名和地址，欢迎光临"])  # fmt: skip
# (keywords, opening beat, ending beat) for the occasion of the film
OCCASIONS = [
    (("开业", "开张"), "开业大吉，彩带和花篮摆在门口", "新店开业，欢迎第一次来坐坐"),
    (("新品", "上新", "新款"), "新品第一次亮相，特写慢慢推近", "新品现已上市，到店就能尝到"),
    (("节日", "促销", "母亲节", "圣诞", "春节", "中秋"), "节日氛围布置好了，暖暖的灯光",
     "节日限时优惠，到店报暗号有惊喜"),
]  # fmt: skip
MOTIONS = ["缓慢推近", "左右平移", "轻微拉远", "跟随移动"]
MAX_CARDS = 8
CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
             "八": 8, "九": 9, "十": 10}  # fmt: skip
NUM = r"(\d+|[一二两三四五六七八九十])"
# (keywords, plain summary, prompt addition, look change)
FIX_RULES = [
    (("暗", "黑", "dark"), "调亮画面，换成暖色光", "提高曝光，暖色窗边自然光",
     {"brightness": 1.35, "warmth": 0.4}),
    (("亮", "过曝", "刺眼"), "压暗一点，减少过曝", "降低曝光，柔和光线", {"brightness": 0.8}),
    (("冷", "发蓝", "不温馨"), "换成更温馨的暖色调", "暖色调，温馨氛围", {"warmth": 0.6}),
    (("黑白", "复古"), "改成黑白复古风格", "黑白胶片质感", {"color": 0.0}),
    (("脸", "人", "手", "变形", "怪"), "人物重新生成，避免变形", "人物面部和手部自然清晰，避免变形", {}),
    (("糊", "模糊", "不清楚"), "画面更清晰", "画面清晰锐利，对焦准确", {}),
    (("快", "晃"), "镜头运动放慢、更稳", "镜头缓慢稳定移动", {}),
]  # fmt: skip


def number(text):
    return int(text) if text.isdigit() else CN_DIGITS.get(text)


def theme_for(text):
    text = text.lower()
    return next(
        ((shop, names, beats) for keys, shop, names, beats in THEMES
         if any(k in text for k in keys)),
        GENERIC,
    )  # fmt: skip


class DemoAdapter:
    def __init__(self, workdir):
        self.workdir = Path(workdir)

    def _folder(self, board):
        folder = self.workdir / board.id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _copy(self, board):
        return Storyboard.from_dict(board.to_dict())

    def _prompt(self, board, card):
        parts = [
            f"{board.materials[card.photo - 1]}（素材{card.photo}{MODES[card.mode]}）",
            card.description,
            MOTIONS[(card.photo - 1) % len(MOTIONS)],
            board.style or "温馨自然光",
            f"{board.aspect} 竖屏" if board.aspect == "9:16" else f"{board.aspect} 横屏",
        ]
        if card.note:
            parts.append(card.note)
        return "，".join(p for p in parts if p)

    # ---- materials and story ------------------------------------------------

    def start(self, chat_id, photos, brief):
        shop, names, beats = theme_for(brief)
        materials = [
            names[i] if i < len(names) else f"照片{i + 1}" for i in range(len(photos))
        ]
        return Storyboard(
            id=uuid.uuid4().hex[:12],
            title=f"{shop}宣传片（演示）",
            brief=brief,
            photos=list(photos),
            materials=materials,
            outline=self._occasion(list(beats), brief),
            cards=[],
            aspect="16:9" if "横屏" in brief else "9:16",
        )

    @staticmethod
    def _occasion(beats, brief):
        """The occasion (quick pick or words) shapes the opening and the ending."""
        for keys, opening, ending in OCCASIONS:
            if any(k in brief for k in keys):
                return [opening, *beats[1:-1], ending]
        return beats

    def rename_material(self, board, n, name):
        board = self._copy(board)
        board.materials[n - 1] = name.strip()[:12]
        return board

    def revise_outline(self, board, note):
        board = self._copy(board)
        board.style = note.strip()[:40]
        board.outline = [f"{beat}（{board.style}）" if i == 0 else beat
                         for i, beat in enumerate(board.outline)]  # fmt: skip
        board.revision += 1
        return board

    # ---- shot cards -----------------------------------------------------------

    def plan_shots(self, board):
        board = self._copy(board)
        cards, count = [], min(len(board.outline), MAX_CARDS)
        for index in range(count):
            photo = 1 if index == count - 1 else (index % len(board.photos)) + 1
            beat = board.outline[index]
            card = Card(
                title=beat.split("，")[0][:8],
                description=beat,
                photo=photo,
                seconds=3.0,
                mode="first_frame" if index != count - 1 else "reference",
            )
            card.prompt = self._prompt(board, card)
            cards.append(card)
        board.cards = cards
        board.revision += 1
        return board

    def _refresh(self, board, card):
        card.prompt = self._prompt(board, card)

    def swap_material(self, board, n, material):
        board = self._copy(board)
        card = board.cards[n - 1]
        card.photo = material
        self._refresh(board, card)
        board.revision += 1
        return board

    def edit_card(self, board, n, text):
        board = self._copy(board)
        card = board.cards[n - 1]
        card.description = text.strip()[:60]
        self._refresh(board, card)
        board.revision += 1
        return board

    def delete_card(self, board, n):
        board = self._copy(board)
        if len(board.cards) > 1:
            board.cards.pop(n - 1)
        board.revision += 1
        return board

    def revise(self, board, instruction):
        board = self._copy(board)
        text = instruction.strip()
        changes = []
        # Card numbers refer to the board as shown, so replacements apply
        # first and deletions last, highest number first.
        pattern = rf"第{NUM}[张段][^张段，,。；;]{{0,6}}?(?:换成|用)(?:素材|照片){NUM}"
        for match in re.finditer(pattern, text):
            n, p = number(match.group(1)), number(match.group(2))
            if n and p and 1 <= n <= len(board.cards) and 1 <= p <= len(board.photos):
                board.cards[n - 1].photo = p
                changes.append(f"第{n}段用{board.material(p)}")
        doomed = {
            n
            for match in re.finditer(rf"(?:删掉|删除|去掉)第?{NUM}[张段]", text)
            if (n := number(match.group(1))) and 1 <= n <= len(board.cards)
        }
        for n in sorted(doomed, reverse=True):
            if len(board.cards) > 1:
                board.cards.pop(n - 1)
                changes.append(f"删掉了第{n}段")
        for word, aspect, label in (("横屏", "16:9", "改成横屏"), ("竖屏", "9:16", "改成竖屏")):
            if word in text and board.aspect != aspect:
                board.aspect = aspect
                changes.append(label)
        for words, factor, label in (
            (("短一点", "快一点", "shorter"), 0.67, "整体缩短"),
            (("长一点", "慢一点", "longer"), 1.3, "整体放慢"),
        ):
            if any(w in text for w in words):
                for card in board.cards:
                    card.seconds = float(max(2, min(6, round(card.seconds * factor))))
                changes.append(label)
        if not changes:
            match = re.search(rf"第?{NUM}[张段](.+)", text)
            n = number(match.group(1)) if match else None
            if n and 1 <= n <= len(board.cards):
                board.cards[n - 1].description = match.group(2).strip(" ，,。")[:60]
                changes.append(f"第{n}段：{board.cards[n - 1].description[:20]}")
            else:
                board.style = text[:40]
                changes.append(f"整体风格：{board.style[:20]}")
        for card in board.cards:
            self._refresh(board, card)
        board.revision += 1
        return board, "；".join(changes)

    # ---- generation and review ----------------------------------------------

    def estimate(self, board):
        checks = []
        if any(k in board.brief for k in ("小孩", "儿童", "孩子")):
            checks.append("提到了小孩：请确认已获得监护人同意")
        return Estimate(
            credits=int(round(board.duration * 10)),
            seconds=board.duration,
            note="演示模式：不会扣积分，也不会用显卡",
            checks=checks,
        )

    def _render_take(self, board, number_, card, folder):
        take = card.take()
        if take is None:
            take = {"v": card.version, "prompt": card.prompt, "photo": card.photo,
                    "look": dict(card.look), "clip": None}  # fmt: skip
            card.takes.append(take)
        if not take["clip"] or not Path(take["clip"]).exists():
            take["clip"] = render.clip(board, number_, card, folder)
        return take["clip"]

    def _cut(self, board):
        folder = self._folder(board)
        clips = [self._render_take(board, n, card, folder)
                 for n, card, _s, _e in board.timeline()]  # fmt: skip
        path = render.concat(clips, str(folder / f"film-r{board.revision}.mp4"))
        return Film(path=path, storyboard_id=board.id, revision=board.revision)

    def generate(self, chat_id, board, progress):
        board = self._copy(board)
        folder = self._folder(board)
        total = len(board.cards)
        for n, card, _s, _e in board.timeline():
            progress(n - 1, total)
            self._render_take(board, n, card, folder)
        progress(total, total)
        board.revision += 1
        return board, self._cut(board)

    def propose_fix(self, board, n, feedback):
        card = board.cards[n - 1]
        summaries, additions = [], []
        for keys, summary, addition, _look in FIX_RULES:
            if any(k in feedback for k in keys):
                summaries.append(summary)
                additions.append(addition)
        if not summaries:
            summaries, additions = [f"按你的意见调整：{feedback[:20]}"], [feedback[:40]]
        base = card.prompt.split("。修改：")[0]
        return FixProposal(
            number=n,
            feedback=feedback,
            summary="；".join(summaries),
            prompt_before=card.prompt,
            prompt_after=f"{base}。修改：{'，'.join(additions)}",
            credits=int(round(card.seconds * 10)),
        )

    def apply_fix(self, board, n, prompt, progress):
        board = self._copy(board)
        card = board.cards[n - 1]
        progress(0, 1)
        if prompt is not None:
            card.prompt = prompt
            card.note = prompt.split("。修改：", 1)[-1] if "。修改：" in prompt else ""
            look = dict(card.look)
            for keys, _summary, addition, change in FIX_RULES:
                if addition in prompt:
                    look.update(change)
            card.look = look
        card.version = max(t["v"] for t in card.takes) + 1
        board.revision += 1
        film = self._cut(board)
        progress(1, 1)
        return board, film

    def recut(self, board):
        board = self._copy(board)
        board.revision += 1
        return board, self._cut(board)

    def adopt(self, board, n, version):
        board = self._copy(board)
        card = board.cards[n - 1]
        take = card.take(version)
        if take is None:
            raise ValueError("unknown take")
        card.version, card.prompt = version, take["prompt"]
        card.photo, card.look = take["photo"], dict(take["look"])
        board.revision += 1
        return board, self._cut(board)

    # ---- pictures -------------------------------------------------------------

    def materials_sheet(self, board):
        tiles = [(path, f"照片{i}", board.materials[i - 1], "", None)
                 for i, path in enumerate(board.photos, 1)]  # fmt: skip
        return render.sheet(
            tiles,
            "我认出了这些照片",
            f"{len(board.photos)} 张 · 名字不对直接说「3 是吧台」",
            str(self._folder(board) / f"materials-r{board.revision}.jpg"),
        )

    def grid(self, board):
        tiles = [
            (board.photos[card.photo - 1], f"第{n}段",
             f"{render.clock(s)}–{render.clock(e)}  {card.title}",
             f"{board.material(card.photo)}{MODES[card.mode]}", card.look)
            for n, card, s, e in board.timeline()
        ]  # fmt: skip
        return render.sheet(
            tiles,
            board.title,
            f"{len(board.cards)} 个镜头 · {render.clock(board.duration)} · {board.aspect} · {board.tier}",
            str(self._folder(board) / f"grid-r{board.revision}.jpg"),
            tall=board.aspect == "9:16",
        )

    def card_preview(self, board, n):
        card = board.cards[n - 1]
        out = self._folder(board) / f"preview-{n}-r{board.revision}.jpg"
        render.styled(board.photos[card.photo - 1], (540, 960), card.look).save(out)
        return str(out)

    def web_link(self, board, number=None):
        return None

    def add_photo(self, board, path):
        return board

    def frame(self, film, second):
        out = Path(film.path).with_name(f"frame-r{film.revision}-{second:.1f}s.jpg")
        return render.frame(film.path, second, str(out))
