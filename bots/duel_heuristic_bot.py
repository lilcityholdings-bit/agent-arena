from __future__ import annotations

import random
from collections import Counter

from .base import Bot
from engine.duel import STRIKE, GRAPPLE, BLOCK, DODGE, REST, MOVE_COST

# The cheapest legal answer to each move, ranked by what actually stops it
# (see engine/duel.py's resolution table): STRIKE loses to BLOCK just as
# cleanly as to DODGE, but BLOCK costs less, so BLOCK is listed first.
COUNTER_PRIORITY = {
    GRAPPLE: [STRIKE, DODGE],
    BLOCK: [GRAPPLE],
    STRIKE: [BLOCK, DODGE],
    REST: [STRIKE, GRAPPLE],
    DODGE: [BLOCK, REST],
}


class HeuristicDuelBot(Bot):
    """Reads the opponent's recent move frequency and plays the cheapest
    counter to whatever they've favored, with a bluff/randomization
    frequency so it isn't a pure (and therefore trivially exploitable)
    counter-machine, and a low-stamina/low-HP survival override so it
    doesn't bankrupt its own stamina chasing a read it can't afford."""

    name = "heuristic_duel_bot"

    def __init__(self, rng: random.Random | None = None, bluff_freq: float = 0.15, memory: int = 5):
        self.rng = rng or random.Random()
        self.bluff_freq = bluff_freq
        self.memory = memory

    def act(self, state: dict) -> str:
        legal = state["legal_actions"]
        if len(legal) == 1:
            return legal[0]

        if self.rng.random() < self.bluff_freq:
            return self.rng.choice(legal)

        # low HP and can't afford to be caught resting: never volunteer REST
        if state["your_hp"] <= 8 and REST in legal and len(legal) > 1:
            legal = [m for m in legal if m != REST]

        # action_history entries are {"p0": ..., "p1": ...} -- state["seat"]
        # says which slot is us, so the other slot is the opponent's move.
        recent = state["action_history"][-self.memory:]
        seat = state.get("seat")
        if seat is not None and recent:
            opp_key = "p1" if seat == 0 else "p0"
            freq = Counter(h[opp_key] for h in recent)
            predicted = freq.most_common(1)[0][0]
            for candidate in COUNTER_PRIORITY.get(predicted, []):
                if candidate in legal:
                    return candidate

        # no read yet: bias toward attacking over resting, cheapest first
        for preferred in (STRIKE, BLOCK, GRAPPLE, DODGE, REST):
            if preferred in legal:
                return preferred
        return legal[0]
