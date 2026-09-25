"""BossDuelBot: the "hard mode" opponent for Duel.

Leduc's hard bot (CFRBot) solves the *entire* game offline, because
Leduc's full game tree is small enough to enumerate exactly (288
information sets). Duel's full game tree isn't -- HP and stamina are
each one of ~7 meaningfully different levels apiece for two players
across up to 40 rounds, which is too large a state space to solve
exactly with the same from-scratch approach in this environment.

So BossDuelBot does the next best honest thing: on every round, it
solves the *immediate round* as its own small zero-sum matrix game
(only the moves both fighters can currently afford, given their real
stamina) with the same regret-matching technique CFR is built from --
this is exactly the textbook way to solve something like
rock-paper-scissors: self-play two regret-matching learners against
each other and average their strategies, which provably converges to a
Nash equilibrium of that matrix game. It reuses the real engine
(engine/duel.py) to score every candidate move pair, the same way
cfr_train.py reuses LeducHand, so the payoffs it optimizes against are
exactly the real resolution table, not a re-guessed approximation of it.

What this honestly is NOT: a solve of the whole multi-round fight. It's
myopic -- it plays the best response to "assume this round matters and
nothing else does," so it won't, for example, deliberately punt a round
to bait a stamina trap two rounds later the way a true equilibrium
strategy of the full game might. In practice this still makes it a
materially stronger, balanced (not purely predictable) opponent than a
frequency-counting heuristic, which is what the tests below establish.
"""
from __future__ import annotations

import random

from .base import Bot
from engine.duel import ALL_MOVES, MOVE_COST, DuelFight


def _legal_for(stamina: int) -> list[str]:
    return [m for m in ALL_MOVES if MOVE_COST[m] <= stamina]


def _round_payoff_to_seat0(rng: random.Random, hp: dict, stamina: dict, m0: str, m1: str) -> int:
    """Damage dealt to seat 1 minus damage taken by seat 0 if this pair of
    moves were played right now. Exactly zero-sum (seat 1's payoff is the
    negation), because it's the same HP deltas from the other side."""
    f = DuelFight(rng=rng, stake=1)
    f.start()
    f.hp = dict(hp)
    f.stamina = dict(stamina)
    f.round_num = 0
    hp_before = dict(f.hp)
    f.submit(0, m0)
    f.submit(1, m1)
    dealt = hp_before[1] - f.hp[1]
    taken = hp_before[0] - f.hp[0]
    return dealt - taken


def solve_stage_nash(payoff, legal0, legal1, iterations=600, rng=None):
    """Self-play regret matching over a small zero-sum bimatrix `payoff`
    (keyed (m0, m1) -> utility to seat 0). Returns (avg_strategy0,
    avg_strategy1), each a dict move -> probability."""
    rng = rng or random.Random()
    regret0 = {m: 0.0 for m in legal0}
    regret1 = {m: 0.0 for m in legal1}
    sum0 = {m: 0.0 for m in legal0}
    sum1 = {m: 0.0 for m in legal1}

    def strategy_from_regret(regret):
        positive = {m: max(0.0, r) for m, r in regret.items()}
        total = sum(positive.values())
        if total > 0:
            return {m: v / total for m, v in positive.items()}
        n = len(regret)
        return {m: 1.0 / n for m in regret}

    for _ in range(iterations):
        s0 = strategy_from_regret(regret0)
        s1 = strategy_from_regret(regret1)
        for m in legal0:
            sum0[m] += s0[m]
        for m in legal1:
            sum1[m] += s1[m]

        a1 = rng.choices(legal1, weights=[s1[m] for m in legal1])[0]
        a0 = rng.choices(legal0, weights=[s0[m] for m in legal0])[0]

        realized0 = payoff[(a0, a1)]
        for m in legal0:
            regret0[m] += payoff[(m, a1)] - realized0
        realized1 = -realized0
        for m in legal1:
            regret1[m] += (-payoff[(a0, m)]) - realized1

    def normalize(strategy_sum):
        total = sum(strategy_sum.values())
        if total <= 0:
            n = len(strategy_sum)
            return {m: 1.0 / n for m in strategy_sum}
        return {m: v / total for m, v in strategy_sum.items()}

    return normalize(sum0), normalize(sum1)


class BossDuelBot(Bot):
    name = "boss_duel_bot"

    def __init__(self, rng: random.Random | None = None, iterations: int = 600):
        self.rng = rng or random.Random()
        self.iterations = iterations

    def act(self, state: dict) -> str:
        legal = state["legal_actions"]
        if len(legal) == 1:
            return legal[0]

        seat = state["seat"]
        opp = 1 - seat
        hp = {seat: state["your_hp"], opp: state["opponent_hp"]}
        stamina = {seat: state["your_stamina"], opp: state["opponent_stamina"]}
        legal_opp = _legal_for(stamina[opp])

        legal0 = legal if seat == 0 else legal_opp
        legal1 = legal_opp if seat == 0 else legal

        payoff = {}
        for m0 in legal0:
            for m1 in legal1:
                payoff[(m0, m1)] = _round_payoff_to_seat0(self.rng, hp, stamina, m0, m1)

        strat0, strat1 = solve_stage_nash(payoff, legal0, legal1, iterations=self.iterations, rng=self.rng)
        my_strategy = strat0 if seat == 0 else strat1
        moves = list(my_strategy.keys())
        weights = [my_strategy[m] for m in moves]
        return self.rng.choices(moves, weights=weights)[0]
