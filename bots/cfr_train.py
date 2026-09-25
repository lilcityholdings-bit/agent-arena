"""Computes a near-Nash-equilibrium strategy for our exact Leduc Hold'em
rules via chance-sampled vanilla CFR (counterfactual regret minimization),
and saves it to cfr_strategy.json for CFRBot to play from.

Why this is worth having: RandomBot and HeuristicBot are both easy to
exploit (that's the point of test_conservation.py -- HeuristicBot beating
RandomBot proves skill matters at all). CFRBot is meant to be the actual
hard opponent: Leduc Hold'em is small enough (6 cards, 2 betting rounds,
capped raises) that exact-form CFR converges to a strategy that's very
close to unexploitable in well under a minute, rather than hand-picked
heuristics.

This deliberately reuses engine.leduc.LeducHand -- the exact same rules
class live matches run on -- to walk the game tree, instead of writing a
second, parallel implementation of the betting rules that could quietly
drift out of sync with the real game. Concretely: build_hand() constructs
a hand at an arbitrary point in its action history by replaying that
history through LeducHand.apply(), so every legal-action check, raise
cap, etc. comes from the one tested rules implementation.

Simplifications, stated plainly:
  - Solved with unlimited stacks and zero rake (rake_bps=0). Real matches
    cap bets by actual bankroll and take a rake; this strategy ignores
    both. For the bet sizes here that's a minor approximation, not a
    correctness issue, but it means CFRBot's play is only "equilibrium
    for the raked, stack-capped game" approximately, not exactly.
  - Suits are irrelevant to Leduc hand strength (only rank and pairing
    with the board matter -- see engine/evaluator.py), so information
    sets are keyed on rank only. This is a lossless simplification, not
    an approximation: swapping the two cards of the same rank never
    changes anyone's optimal decision.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cards import Card
from engine.leduc import ANTE, UNLIMITED_STACK, LeducHand

DECK = ["J", "J", "Q", "Q", "K", "K"]

regret_sum: dict[str, dict[str, float]] = {}
strategy_sum: dict[str, dict[str, float]] = {}


def build_hand(hole_ranks: dict, board_rank: str, actions: list[str]) -> LeducHand:
    hand = LeducHand(rng=random.Random(0), rake_bps=0, stacks={0: UNLIMITED_STACK, 1: UNLIMITED_STACK})
    hand.hole = {0: Card(hole_ranks[0], "a"), 1: Card(hole_ranks[1], "b")}
    hand.board = Card(board_rank, "a")
    hand.contributed = {0: ANTE, 1: ANTE}
    hand.round = 1
    hand.to_act = 0
    hand.checks_this_round = 0
    hand.raises_this_round = 0
    hand.folded = None
    hand.done = False
    hand.result = None
    for a in actions:
        hand.apply(a)
    return hand


def infoset_key(player: int, hole_rank: str, visible_board_rank: str | None, actions: list[str]) -> str:
    return f"{player}|{hole_rank}|{visible_board_rank or '-'}|{','.join(actions)}"


def get_strategy(key: str, legal: list[str]) -> dict[str, float]:
    regrets = regret_sum.setdefault(key, {})
    for a in legal:
        regrets.setdefault(a, 0.0)
    positive = {a: max(regrets[a], 0.0) for a in legal}
    total = sum(positive.values())
    if total > 0:
        return {a: positive[a] / total for a in legal}
    return {a: 1.0 / len(legal) for a in legal}


def cfr(hole_ranks: dict, board_rank: str, actions: list[str], reach0: float, reach1: float) -> dict:
    hand = build_hand(hole_ranks, board_rank, actions)
    if hand.done:
        return hand.result.payoffs

    player = hand.to_act
    hole_rank = hole_ranks[player]
    visible_board = board_rank if hand.round == 2 else None
    key = infoset_key(player, hole_rank, visible_board, actions)
    legal = hand.legal_actions()
    strat = get_strategy(key, legal)

    util = {0: 0.0, 1: 0.0}
    action_utils = {}
    for a in legal:
        p0 = reach0 * (strat[a] if player == 0 else 1.0)
        p1 = reach1 * (strat[a] if player == 1 else 1.0)
        payoffs = cfr(hole_ranks, board_rank, actions + [a], p0, p1)
        action_utils[a] = payoffs
        for p in (0, 1):
            util[p] += strat[a] * payoffs[p]

    opp_reach = reach1 if player == 0 else reach0
    own_reach = reach0 if player == 0 else reach1
    regrets = regret_sum[key]
    strat_sum = strategy_sum.setdefault(key, {a: 0.0 for a in legal})
    for a in legal:
        strat_sum.setdefault(a, 0.0)
        regrets[a] += opp_reach * (action_utils[a][player] - util[player])
        strat_sum[a] += own_reach * strat[a]

    return util


def train(iterations: int, seed: int = 0, log_every: int = 0) -> dict:
    rng = random.Random(seed)
    start = time.time()
    for i in range(iterations):
        deck = DECK[:]
        rng.shuffle(deck)
        hole_ranks = {0: deck[0], 1: deck[1]}
        board_rank = deck[2]
        cfr(hole_ranks, board_rank, [], 1.0, 1.0)
        if log_every and (i + 1) % log_every == 0:
            print(f"  {i + 1}/{iterations} iterations ({time.time() - start:.1f}s elapsed)")

    average_strategy = {}
    for key, sums in strategy_sum.items():
        total = sum(sums.values())
        if total > 0:
            average_strategy[key] = {a: v / total for a, v in sums.items()}
        else:
            n = len(sums)
            average_strategy[key] = {a: 1.0 / n for a in sums}
    return average_strategy


if __name__ == "__main__":
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    print(f"training CFR for {iterations} iterations...")
    t0 = time.time()
    strategy = train(iterations, log_every=max(1, iterations // 10))
    print(f"done in {time.time() - t0:.1f}s -- {len(strategy)} information sets")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cfr_strategy.json")
    with open(out_path, "w") as f:
        json.dump(strategy, f, indent=1, sort_keys=True)
    print(f"wrote strategy to {out_path}")
