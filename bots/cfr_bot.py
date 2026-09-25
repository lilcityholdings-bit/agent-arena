from __future__ import annotations

import json
import os
import random

from .base import Bot

_STRATEGY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cfr_strategy.json")

# Maps state_for()'s action names to the single-word action names used as
# CFR training's action-history tokens. They're already the same strings
# ("check", "call", "raise", "fold") -- kept as a constant anyway so the
# mapping is explicit and named, not implicit.
_ACTIONS = ("fold", "check", "call", "raise")


class CFRBot(Bot):
    """Plays from a strategy computed offline by cfr_train.py (chance-
    sampled CFR against the real engine rules -- see that file's
    docstring for exactly what was and wasn't modeled). Falls back to a
    uniform-random choice on any information set the training run never
    visited (shouldn't happen for a fully-trained strategy, but a bot
    should never crash instead of just making a legal move)."""

    name = "cfr_bot"

    def __init__(self, rng: random.Random | None = None, strategy_path: str = _STRATEGY_PATH):
        self.rng = rng or random.Random()
        with open(strategy_path) as f:
            self.strategy: dict[str, dict[str, float]] = json.load(f)

    @staticmethod
    def _infoset_key(state: dict) -> str:
        player = state["to_act"]
        hole_rank = state["your_hole_card"][0]
        board_rank = state["board_card"][0] if state["board_card"] else None
        actions = ",".join(state["action_history"])
        return f"{player}|{hole_rank}|{board_rank or '-'}|{actions}"

    def act(self, state: dict) -> str:
        legal = state["legal_actions"]
        key = self._infoset_key(state)
        probs = self.strategy.get(key)
        if not probs:
            return self.rng.choice(legal)

        # Restrict to actions that are actually legal right now (a
        # stack-capped live match can make "raise" illegal in a spot the
        # unlimited-stack training never saw fold away) and renormalize.
        probs = {a: p for a, p in probs.items() if a in legal}
        if not probs:
            return self.rng.choice(legal)
        total = sum(probs.values())
        r = self.rng.random() * total
        upto = 0.0
        for action, p in probs.items():
            upto += p
            if r <= upto:
                return action
        return next(iter(probs))  # floating point edge case
