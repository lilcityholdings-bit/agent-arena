"""Duel: a martial-arts combat game for bots.

Bots don't have real bodies, so this isn't physics -- it's an abstracted
turn-based fight that keeps the two things that make Leduc work as a
skill game: hidden information and a real resource constraint that makes
some plays unaffordable, the way betting does in poker.

Each round both fighters *simultaneously and secretly* choose one move:

    STRIKE   a fast attack
    GRAPPLE  a takedown
    BLOCK    a guard
    DODGE    an evasion
    REST     do nothing, recover stamina, undefended if attacked

Moves cost stamina (except REST, which is free and always legal -- the
fallback when you can't afford anything else, like a check). Once both
fighters have submitted a move for the round, it resolves:

    STRIKE  beats GRAPPLE   (hit them before the grab lands)
    GRAPPLE beats BLOCK     (throw through the guard)
    BLOCK   beats STRIKE    (the guard stops the hit)
    DODGE   beats STRIKE and GRAPPLE (evades either attack) but not BLOCK
    Two matching STRIKEs or GRAPPLEs CLASH -- both take damage
    Anything else (BLOCK vs BLOCK/DODGE/REST, DODGE vs BLOCK/DODGE/REST,
    REST vs BLOCK/DODGE/REST) is a no-damage beat: nothing lands
    REST vs an attack (STRIKE or GRAPPLE) is undefended -- full damage,
    worse than being blocked or dodged, because resting offered no
    defense at all

This is the same core shape as rock-paper-scissors (STRIKE > GRAPPLE >
BLOCK > STRIKE) with two deliberately asymmetric options layered on:
DODGE is the safe answer to both attacks but is the most expensive move
in the game, and REST is free but leaves you exposed -- so "what did my
opponent's stamina bar make them likely to do" is the actual skill here,
same role that pot odds and bet sizing play in poker.

Economics: each fight has a fixed entry stake (deducted from each
fighter's bankroll, like Leduc's ante) rather than round-by-round
betting -- there's no natural "raise" in a fight, so the wager is
settled once, up front, and the pot (both stakes, minus rake) goes to
whoever wins the fight. A draw (round cap reached at equal HP) splits
the pot, same as an exact-tie showdown in Leduc.

Hidden information: `state_for(player)` never reveals the opponent's
move for the current round until both fighters have submitted and the
round has resolved -- that's the one and only thing that makes this a
game of imperfect information instead of a solved lookup table.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

STRIKE = "strike"
GRAPPLE = "grapple"
BLOCK = "block"
DODGE = "dodge"
REST = "rest"

ALL_MOVES = (STRIKE, GRAPPLE, BLOCK, DODGE, REST)

MOVE_COST = {STRIKE: 5, GRAPPLE: 7, BLOCK: 3, DODGE: 9, REST: 0}

MAX_HP = 30
MAX_STAMINA = 30
STAMINA_REGEN = 4          # both fighters regen this much stamina after every round
MAX_ROUNDS = 40             # fight is decided by HP if nobody's KO'd by then
UNLIMITED_STACK = 10 ** 9

# Damage amounts, named so the resolution table below reads like the rules text.
STRIKE_VS_GRAPPLE_DAMAGE = 6      # clean hit before the grab lands
GRAPPLE_VS_BLOCK_DAMAGE = 5       # thrown through the guard
GRAPPLE_VS_BLOCK_STAMINA = 4      # extra stamina lost being thrown
CLASH_STRIKE_DAMAGE = 3           # both STRIKE: a trade
CLASH_GRAPPLE_DAMAGE = 3          # both GRAPPLE: a scramble
CLASH_GRAPPLE_STAMINA = 2         # extra stamina lost in the scramble
UNDEFENDED_DAMAGE = 8             # REST eaten by STRIKE or GRAPPLE: no defense at all


class IllegalAction(Exception):
    pass


@dataclass
class FightResult:
    payoffs: dict[int, int]        # net change to each player's stack (can be negative)
    pot_before_rake: int
    rake_taken: int
    winner: int | None             # 0, 1, or None for a draw
    rounds_played: int
    ko: bool                       # True if won by knockout, False if decided on points/draw
    final_hp: dict[int, int]


@dataclass
class DuelFight:
    rng: random.Random
    rake_bps: int = 500
    stacks: dict[int, int] = field(default_factory=lambda: {0: UNLIMITED_STACK, 1: UNLIMITED_STACK})
    stake: int = 10

    round_num: int = field(init=False, default=0)
    hp: dict[int, int] = field(init=False, default_factory=dict)
    stamina: dict[int, int] = field(init=False, default_factory=dict)
    pending_moves: dict[int, str] = field(init=False, default_factory=dict)
    action_history: list[tuple[str, str]] = field(init=False, default_factory=list)  # revealed (p0_move, p1_move) per round
    done: bool = field(init=False, default=False)
    result: FightResult | None = field(init=False, default=None)

    def start(self) -> None:
        if self.stacks[0] < self.stake or self.stacks[1] < self.stake:
            raise IllegalAction("a fighter's stack can't cover the entry stake")
        self.round_num = 0
        self.hp = {0: MAX_HP, 1: MAX_HP}
        self.stamina = {0: MAX_STAMINA, 1: MAX_STAMINA}
        self.pending_moves = {}
        self.action_history = []
        self.done = False
        self.result = None

    def pot(self) -> int:
        return self.stake * 2

    def legal_actions(self, player: int) -> list[str]:
        if self.done:
            return []
        if player in self.pending_moves:
            return []  # already submitted this round, waiting on opponent
        s = self.stamina[player]
        return [m for m in ALL_MOVES if MOVE_COST[m] <= s]

    def other(self, player: int) -> int:
        return 1 - player

    def submit(self, player: int, move: str) -> None:
        if self.done:
            raise IllegalAction("fight is already over")
        if player in self.pending_moves:
            raise IllegalAction("already submitted a move this round")
        if move not in ALL_MOVES:
            raise IllegalAction(f"unknown move: {move!r}")
        if MOVE_COST[move] > self.stamina[player]:
            raise IllegalAction(f"not enough stamina for {move}")
        self.pending_moves[player] = move
        if len(self.pending_moves) == 2:
            self._resolve_round()

    def _resolve_round(self) -> None:
        m0, m1 = self.pending_moves[0], self.pending_moves[1]
        self.stamina[0] -= MOVE_COST[m0]
        self.stamina[1] -= MOVE_COST[m1]
        self._apply_outcome(m0, m1)
        self.action_history.append((m0, m1))
        self.pending_moves = {}
        self.round_num += 1

        for p in (0, 1):
            self.stamina[p] = min(MAX_STAMINA, self.stamina[p] + STAMINA_REGEN)

        if self.hp[0] <= 0 or self.hp[1] <= 0:
            self._finish(ko=True)
        elif self.round_num >= MAX_ROUNDS:
            self._finish(ko=False)

    def _apply_outcome(self, m0: str, m1: str) -> None:
        beats = {
            (STRIKE, GRAPPLE), (GRAPPLE, BLOCK), (BLOCK, STRIKE),
            (DODGE, STRIKE), (DODGE, GRAPPLE),
            (STRIKE, REST), (GRAPPLE, REST),  # resting through an attack is undefended
        }
        if (m0, m1) in beats:
            self._land(winner=0, loser=1, winner_move=m0, loser_move=m1)
        elif (m1, m0) in beats:
            self._land(winner=1, loser=0, winner_move=m1, loser_move=m0)
        elif m0 == STRIKE and m1 == STRIKE:
            self.hp[0] = max(0, self.hp[0] - CLASH_STRIKE_DAMAGE)
            self.hp[1] = max(0, self.hp[1] - CLASH_STRIKE_DAMAGE)
        elif m0 == GRAPPLE and m1 == GRAPPLE:
            self.hp[0] = max(0, self.hp[0] - CLASH_GRAPPLE_DAMAGE)
            self.hp[1] = max(0, self.hp[1] - CLASH_GRAPPLE_DAMAGE)
            self.stamina[0] = max(0, self.stamina[0] - CLASH_GRAPPLE_STAMINA)
            self.stamina[1] = max(0, self.stamina[1] - CLASH_GRAPPLE_STAMINA)
        # everything else (BLOCK/DODGE/REST vs BLOCK/DODGE/REST, and the
        # REST-vs-nonattack cases) is a no-op: nothing landed.

    def _land(self, winner: int, loser: int, winner_move: str, loser_move: str) -> None:
        if loser_move == REST and winner_move in (STRIKE, GRAPPLE):
            self.hp[loser] = max(0, self.hp[loser] - UNDEFENDED_DAMAGE)
            return
        if winner_move == STRIKE and loser_move == GRAPPLE:
            self.hp[loser] = max(0, self.hp[loser] - STRIKE_VS_GRAPPLE_DAMAGE)
        elif winner_move == GRAPPLE and loser_move == BLOCK:
            self.hp[loser] = max(0, self.hp[loser] - GRAPPLE_VS_BLOCK_DAMAGE)
            self.stamina[loser] = max(0, self.stamina[loser] - GRAPPLE_VS_BLOCK_STAMINA)
        elif winner_move == BLOCK and loser_move == STRIKE:
            pass  # blocked clean: no damage, striker already paid the stamina cost
        elif winner_move == DODGE and loser_move in (STRIKE, GRAPPLE):
            pass  # evaded clean: no damage, attacker already paid the stamina cost

    def _finish(self, ko: bool) -> None:
        self.done = True
        pot = self.pot()
        rake = (pot * self.rake_bps) // 10000
        payout_pool = pot - rake

        if self.hp[0] <= 0 and self.hp[1] <= 0:
            winner = None  # simultaneous KO: a draw
        elif self.hp[0] <= 0:
            winner = 1
        elif self.hp[1] <= 0:
            winner = 0
        elif self.hp[0] == self.hp[1]:
            winner = None
        else:
            winner = 0 if self.hp[0] > self.hp[1] else 1

        if winner is None:
            half = payout_pool // 2
            gross = {0: half, 1: payout_pool - half}
        else:
            gross = {winner: payout_pool, self.other(winner): 0}

        payoffs = {p: gross[p] - self.stake for p in (0, 1)}
        self.result = FightResult(
            payoffs=payoffs,
            pot_before_rake=pot,
            rake_taken=rake,
            winner=winner,
            rounds_played=self.round_num,
            ko=ko and winner is not None,
            final_hp=dict(self.hp),
        )

    def state_for(self, player: int) -> dict:
        opp = self.other(player)
        return {
            "seat": player,
            "round": self.round_num,
            "your_hp": self.hp.get(player, MAX_HP),
            "opponent_hp": self.hp.get(opp, MAX_HP),
            "your_stamina": self.stamina.get(player, MAX_STAMINA),
            "opponent_stamina": self.stamina.get(opp, MAX_STAMINA),
            "your_stack_left": self.stacks[player] - self.stake,
            "opponent_stack_left": self.stacks[opp] - self.stake,
            "you_have_submitted": player in self.pending_moves,
            "opponent_has_submitted": opp in self.pending_moves,
            "your_turn": (not self.done) and player not in self.pending_moves,
            "legal_actions": self.legal_actions(player),
            "action_history": [
                {"p0": h[0], "p1": h[1]} for h in self.action_history
            ],
            "done": self.done,
            "result": None if self.result is None else {
                "winner": self.result.winner,
                "rounds_played": self.result.rounds_played,
                "ko": self.result.ko,
                "final_hp": self.result.final_hp,
                "your_payoff": self.result.payoffs[player],
            },
        }

    def to_dict(self) -> dict:
        return {
            "rake_bps": self.rake_bps,
            "stacks": dict(self.stacks),
            "stake": self.stake,
            "round_num": self.round_num,
            "hp": dict(self.hp),
            "stamina": dict(self.stamina),
            "pending_moves": dict(self.pending_moves),
            "action_history": [list(h) for h in self.action_history],
            "done": self.done,
        }

    @classmethod
    def from_dict(cls, data: dict, rng: random.Random) -> "DuelFight":
        fight = cls(rng=rng, rake_bps=data["rake_bps"],
                    stacks={int(p): v for p, v in data["stacks"].items()},
                    stake=data["stake"])
        fight.round_num = data["round_num"]
        fight.hp = {int(p): v for p, v in data["hp"].items()}
        fight.stamina = {int(p): v for p, v in data["stamina"].items()}
        fight.pending_moves = {int(p): v for p, v in data["pending_moves"].items()}
        fight.action_history = [tuple(h) for h in data["action_history"]]
        fight.done = data["done"]
        if fight.done:
            fight._finish(ko=any(hp <= 0 for hp in fight.hp.values()))
        return fight
