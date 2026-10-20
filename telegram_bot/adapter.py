"""Storyboard model and the one interface the bot talks to (#382).

The shapes mirror the studio's storyboard project: materials (library assets),
one chapter of shot cards, takes per card with one adopted, and a rough cut of
the adopted takes. The bot never reaches GPU, credits or the Inkseq API
directly; a demo adapter returns local results and a real adapter will call
the reviewed backend later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

Progress = Callable[[int, int], None]

# How a card uses its material, in the owner-facing words for the studio's
# FL (first frame) and REF (reference) shot modes.
MODES = {"first_frame": "当开头画面", "reference": "参考它的样子"}


@dataclass
class Card:
    title: str
    description: str
    photo: int  # 1-based material number
    seconds: float
    prompt: str = ""
    mode: str = "first_frame"
    version: int = 1  # adopted take
    takes: list = field(default_factory=list)  # [{"v", "prompt", "photo", "look", "clip"}]
    look: dict = field(default_factory=dict)  # demo-only visual adjustments
    note: str = ""
    ref: str = ""  # server shot entity ID once saved to a real story

    def take(self, version=None):
        version = self.version if version is None else version
        return next((t for t in self.takes if t["v"] == version), None)


@dataclass
class Storyboard:
    id: str
    title: str
    brief: str
    photos: list[str]
    materials: list[str]  # names, parallel to photos
    outline: list[str]
    cards: list[Card]
    aspect: str = "9:16"
    tier: str = "预览"
    style: str = ""
    revision: int = 1
    remote: dict = field(default_factory=dict)  # server story/asset IDs, if any

    def timeline(self):
        """[(number, card, start, end)] with 1-based card numbers."""
        rows, start = [], 0.0
        for number, card in enumerate(self.cards, 1):
            rows.append((number, card, start, start + card.seconds))
            start += card.seconds
        return rows

    @property
    def duration(self):
        return sum(card.seconds for card in self.cards)

    def locate(self, second):
        for number, _card, start, end in self.timeline():
            if start <= second < end:
                return number
        return None

    def material(self, number):
        return f"照片{number}「{self.materials[number - 1]}」"

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["cards"] = [Card(**card) for card in data["cards"]]
        return cls(**data)


@dataclass
class Film:
    path: str
    storyboard_id: str
    revision: int

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: data[k] for k in ("path", "storyboard_id", "revision")})


@dataclass
class Estimate:
    credits: int
    seconds: float
    note: str
    checks: list[str]  # content-check findings; empty means passed


@dataclass
class FixProposal:
    number: int
    feedback: str
    summary: str  # one plain line for the owner
    prompt_before: str
    prompt_after: str
    credits: int

    def to_dict(self):
        return asdict(self)


class StudioAdapter(Protocol):
    # materials and story
    def start(self, chat_id: int, photos: list[str], brief: str) -> Storyboard: ...
    def rename_material(self, board: Storyboard, number: int, name: str) -> Storyboard: ...
    def revise_outline(self, board: Storyboard, note: str) -> Storyboard: ...
    # shot cards
    def plan_shots(self, board: Storyboard) -> Storyboard: ...
    def revise(self, board: Storyboard, instruction: str) -> tuple[Storyboard, str]: ...
    def swap_material(self, board: Storyboard, number: int, material: int) -> Storyboard: ...
    def edit_card(self, board: Storyboard, number: int, text: str) -> Storyboard: ...
    def delete_card(self, board: Storyboard, number: int) -> Storyboard: ...
    # generation and review
    def estimate(self, board: Storyboard) -> Estimate: ...
    def generate(self, chat_id: int, board: Storyboard, progress: Progress) -> tuple[Storyboard, Film]: ...
    def propose_fix(self, board: Storyboard, number: int, feedback: str) -> FixProposal: ...
    def apply_fix(
        self, board: Storyboard, number: int, prompt: str | None, progress: Progress
    ) -> tuple[Storyboard, Film]:
        """New take for one card (prompt=None rerolls the same prompt), adopt it, re-cut."""
        ...
    def adopt(self, board: Storyboard, number: int, version: int) -> tuple[Storyboard, Film]: ...
    def recut(self, board: Storyboard) -> tuple[Storyboard, Film]:
        """Re-assemble the adopted takes (after removing a part); no generation."""
        ...
    # pictures
    def materials_sheet(self, board: Storyboard) -> str: ...
    def grid(self, board: Storyboard) -> str: ...
    def card_preview(self, board: Storyboard, number: int) -> str: ...
    def frame(self, film: Film, second: float) -> str: ...
    # real-story links and late photos (no-ops for the demo)
    def web_link(self, board: Storyboard, number: int | None = None) -> str | None: ...
    def add_photo(self, board: Storyboard, path: str) -> Storyboard: ...
