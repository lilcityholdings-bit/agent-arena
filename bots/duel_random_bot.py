from __future__ import annotations

import random

from .base import Bot


class RandomDuelBot(Bot):
    """Uniform random among whatever's currently affordable. The floor
    every other Duel bot needs to actually beat."""

    name = "random_duel_bot"

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    def act(self, state: dict) -> str:
        return self.rng.choice(state["legal_actions"])
