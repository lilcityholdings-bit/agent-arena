"""Card primitives for Leduc Hold'em.

Leduc uses a 6-card deck: ranks {J, Q, K} x suits {a, b} (suits don't matter
for hand strength in Leduc -- only rank and pairing with the board do -- but
we track suit so the deck has the right composition and removal is correct).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

RANKS = ("J", "Q", "K")
RANK_VALUE = {"J": 0, "Q": 1, "K": 2}
SUITS = ("a", "b")


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    def __repr__(self) -> str:
        return f"{self.rank}{self.suit}"

    @property
    def value(self) -> int:
        return RANK_VALUE[self.rank]

    @classmethod
    def parse(cls, text: str) -> "Card":
        """Inverse of __repr__, e.g. Card.parse('Ka') == Card('K', 'a')."""
        return cls(rank=text[0], suit=text[1])


def full_deck() -> list[Card]:
    return [Card(r, s) for r in RANKS for s in SUITS]


def new_shuffled_deck(rng: random.Random) -> list[Card]:
    deck = full_deck()
    rng.shuffle(deck)
    return deck
