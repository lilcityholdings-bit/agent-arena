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


def deck_from_seeds(server_seed: str, client_seed: str) -> list[Card]:
    """A provably fair deck order that anyone can recompute in any language.

    Each card's position is set by SHA-256("<server_seed>:<client_seed>:<card>"),
    sorted ascending. The server commits to SHA-256(server_seed) before the hand
    and reveals the seed after it, and the bot can supply its own client_seed,
    so neither side can steer the cards. Cards are dealt from the END of this
    list: seat 0's hole card, then seat 1's, then the board card.
    """
    import hashlib

    def key(card: Card) -> str:
        return hashlib.sha256(f"{server_seed}:{client_seed}:{card}".encode()).hexdigest()

    return sorted(full_deck(), key=key)
