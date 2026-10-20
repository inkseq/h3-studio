"""Storyboard model and the one interface the bot talks to (#382).

The bot never reaches GPU, credits or the Inkseq API directly. A demo adapter
returns local results; a real adapter will call the reviewed backend later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

Progress = Callable[[int, int], None]


@dataclass
class Card:
    title: str
    description: str
    photo: int  # 1-based index into Storyboard.photos
    seconds: float
    version: int = 1
    note: str = ""


@dataclass
class Storyboard:
    id: str
    title: str
    brief: str
    photos: list[str]
    cards: list[Card]
    aspect: str = "9:16"
    style: str = ""
    revision: int = 1

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
        """Card number playing at `second`, or None outside the film."""
        for number, _card, start, end in self.timeline():
            if start <= second < end:
                return number
        return None

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
    clips: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class Estimate:
    credits: int
    seconds: float
    note: str


class StudioAdapter(Protocol):
    def draft(self, chat_id: int, photos: list[str], brief: str) -> Storyboard: ...

    def revise(self, board: Storyboard, instruction: str) -> tuple[Storyboard, str]:
        """Return the new board and a one-line summary of what changed."""
        ...

    def grid(self, board: Storyboard) -> str:
        """Path of a storyboard grid image."""
        ...

    def estimate(self, board: Storyboard) -> Estimate: ...

    def generate(self, chat_id: int, board: Storyboard, progress: Progress) -> Film: ...

    def regenerate_card(
        self,
        chat_id: int,
        board: Storyboard,
        film: Film,
        number: int,
        note: str,
        progress: Progress,
    ) -> tuple[Storyboard, Film]: ...

    def frame(self, film: Film, second: float) -> str:
        """Path of a still frame from the film at `second`."""
        ...
