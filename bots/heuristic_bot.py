from __future__ import annotations

import random

from .base import Bot

RANK_STRENGTH = {"J": 0, "Q": 1, "K": 2}


class HeuristicBot(Bot):
    """A simple, deliberately non-optimal strategy so RandomBot has a bot
    with a real edge to lose to in the verification tournament:

      - Pairs and top pair (K): always raise if possible, else call.
      - Middle card (Q) pre-board, or a made pair: call, occasionally raise.
      - Weak (J, no pair, board doesn't help): fold to a bet, else check,
        with a small bluff-raise frequency so it isn't purely exploitable.
    """

    name = "heuristic_bot"

    def __init__(self, rng: random.Random | None = None, bluff_freq: float = 0.15):
        self.rng = rng or random.Random()
        self.bluff_freq = bluff_freq

    def _hand_strength(self, state: dict) -> str:
        hole = state["your_hole_card"][0]
        board = state["board_card"][0] if state["board_card"] else None
        if board and hole == board:
            return "pair"
        if hole == "K":
            return "strong"
        if hole == "Q":
            return "medium"
        return "weak"

    def act(self, state: dict) -> str:
        legal = state["legal_actions"]
        strength = self._hand_strength(state)

        if strength in ("pair", "strong"):
            if "raise" in legal:
                return "raise"
            if "call" in legal:
                return "call"
            return "check"

        if strength == "medium":
            if "call" in legal:
                return "call"
            if "raise" in legal and self.rng.random() < self.bluff_freq:
                return "raise"
            return "check" if "check" in legal else "fold"

        # weak
        if "check" in legal:
            return "check"
        if self.rng.random() < self.bluff_freq and "raise" in legal:
            return "raise"
        if "fold" in legal:
            return "fold"
        return "call"
