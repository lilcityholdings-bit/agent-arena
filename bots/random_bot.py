from __future__ import annotations

import random

from .base import Bot


class RandomBot(Bot):
    """Picks a uniformly random legal action. Baseline opponent / sanity check."""

    name = "random_bot"

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    def act(self, state: dict) -> str:
        return self.rng.choice(state["legal_actions"])
