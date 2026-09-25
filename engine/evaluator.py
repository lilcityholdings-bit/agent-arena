"""Hand evaluation for Leduc Hold'em.

Each player has exactly one hole card; there is exactly one board card
shared by both players (dealt after round 1 betting). Hand strength:

  1. Pair (hole card rank == board rank) beats any non-pair.
  2. Among pairs, higher board/hole rank wins (only relevant if both
     players somehow pair -- possible since two suits of each rank exist).
  3. Among non-pairs, higher hole-card rank wins.
  4. Equal rank with no pair for either (or equal pair) is a tie -> split.

Returns an integer score per player; higher wins. Ties compare equal.
"""
from __future__ import annotations

from .cards import Card

PAIR_BONUS = 100  # ensures any pair outranks any non-pair (ranks are 0..2)


def score_hand(hole: Card, board: Card) -> int:
    if hole.rank == board.rank:
        return PAIR_BONUS + hole.value
    return hole.value


def compare(hole_a: Card, hole_b: Card, board: Card) -> int:
    """Return 1 if a wins, -1 if b wins, 0 if it's a tie/split."""
    score_a = score_hand(hole_a, board)
    score_b = score_hand(hole_b, board)
    if score_a > score_b:
        return 1
    if score_b > score_a:
        return -1
    return 0
