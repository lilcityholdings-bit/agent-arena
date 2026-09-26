"""Leduc Hold'em: the game engine.

Leduc Hold'em is a small heads-up poker variant used as a standard
benchmark game in AI/game-theory research (it's simple enough to solve
exactly, but has real hidden information, bluffing, and multi-street
betting -- unlike a coin flip, skill matters).

Rules (this implementation, fixed-limit):
  - 6-card deck: ranks {J, Q, K}, two suits each.
  - Both players ante 1 chip.
  - Each player is dealt one hole card (hidden from the opponent).
  - Round 1 betting: bet size 2, max 2 raises.
  - One board card is dealt face-up, shared by both players.
  - Round 2 betting: bet size 4, max 2 raises.
  - Showdown: pairing your hole card with the board beats any non-pair;
    otherwise higher hole-card rank wins; equal rank is a split pot.

Player 0 acts first in both betting rounds in this implementation --
the orchestrator is responsible for alternating which bot sits in seat 0
across a match so position is fair over many hands.

Bankroll / all-in handling: each player brings a finite `stack` into the
hand (their real ledger balance). A bet or raise can never ask a player
to put in more than they have left. If a player doesn't have enough left
to make a full raise, "raise" simply isn't offered as a legal action --
they can still call (for whatever they have, i.e. an all-in call) or
fold. Once either player is fully all-in (nothing left behind), no more
raising is legal for either side for the rest of the hand -- this is a
deliberate simplification for the heads-up (2-player) case, where side
pots never apply, rather than modeling a "raise the other side can't
respond to" as meaningful.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .cards import Card, new_shuffled_deck
from .evaluator import compare

ANTE = 1
BET_SIZE = {1: 2, 2: 4}
MAX_RAISES = 2
UNLIMITED_STACK = 10**9  # effectively "no cap", for callers that don't care about bankroll


class IllegalAction(Exception):
    pass


@dataclass
class HandResult:
    payoffs: dict  # {0: net_chip_change, 1: net_chip_change}
    pot_before_rake: int
    rake_taken: int
    went_to_showdown: bool
    winner: int | None  # None if split pot
    hole_cards: dict  # {0: Card, 1: Card} -- revealed for the log/UI
    board_card: Card | None


@dataclass
class LeducHand:
    """One hand of Leduc Hold'em between seat 0 and seat 1."""

    rng: random.Random
    rake_bps: int = 500  # rake in basis points of the pot, e.g. 500 = 5%
    stacks: dict = field(default_factory=lambda: {0: UNLIMITED_STACK, 1: UNLIMITED_STACK})

    round: int = field(default=1, init=False)
    to_act: int = field(default=0, init=False)
    contributed: dict = field(default_factory=lambda: {0: 0, 1: 0}, init=False)
    hole: dict = field(default_factory=dict, init=False)
    board: Card | None = field(default=None, init=False)
    _deck: list = field(default_factory=list, init=False)
    checks_this_round: int = field(default=0, init=False)
    raises_this_round: int = field(default=0, init=False)
    folded: int | None = field(default=None, init=False)
    done: bool = field(default=False, init=False)
    result: HandResult | None = field(default=None, init=False)
    action_history: list = field(default_factory=list, init=False)

    def start(self, deck: list | None = None) -> None:
        """Deals the hand. Pass `deck` (e.g. from cards.deck_from_seeds) to use a
        provably fair order instead of shuffling with self.rng."""
        if min(self.stacks[0], self.stacks[1]) < ANTE:
            raise IllegalAction("a player's stack is too small to even post the ante")
        self._deck = list(deck) if deck is not None else new_shuffled_deck(self.rng)
        self.hole = {0: self._deck.pop(), 1: self._deck.pop()}
        # The board card is drawn now too (not when round 2 starts) so a
        # hand's entire outcome is determined by its RNG draw at start()
        # and nothing later needs the RNG or the deck -- which is what
        # lets a hand be safely serialized/reloaded mid-hand (see
        # to_dict/from_dict) without carrying random.Random state around.
        # state_for() only reveals it once self.round == 2.
        self.board = self._deck.pop()
        self.contributed = {0: ANTE, 1: ANTE}
        self.round = 1
        self.to_act = 0
        self.checks_this_round = 0
        self.raises_this_round = 0
        self.folded = None
        self.done = False
        self.result = None
        self.action_history = []

    def pot(self) -> int:
        return self.contributed[0] + self.contributed[1]

    def to_call(self, player: int) -> int:
        opp = 1 - player
        return max(0, self.contributed[opp] - self.contributed[player])

    def stack_left(self, player: int) -> int:
        """Chips this player still has behind (not yet put into the pot)."""
        return self.stacks[player] - self.contributed[player]

    def is_all_in(self, player: int) -> bool:
        return self.stack_left(player) <= 0

    def legal_actions(self) -> list[str]:
        if self.done:
            return []
        player = self.to_act
        opp = 1 - player
        tc = self.to_call(player)
        actions = []
        if tc == 0:
            actions.append("check")
        else:
            actions.append("fold")
            actions.append("call")

        bet_size = BET_SIZE[self.round]
        can_afford_raise = self.stack_left(player) >= tc + bet_size
        opponent_can_respond = self.stack_left(opp) > 0
        if self.raises_this_round < MAX_RAISES and can_afford_raise and opponent_can_respond:
            actions.append("raise")
        return actions

    def state_for(self, player: int) -> dict:
        """Public + own-private view of the state for `player`."""
        return {
            "round": self.round,
            "to_act": self.to_act,
            "your_hole_card": str(self.hole[player]) if self.hole else None,
            "board_card": str(self.board) if self.board and self.round == 2 else None,
            "your_contributed": self.contributed[player],
            "opponent_contributed": self.contributed[1 - player],
            "your_stack_left": self.stack_left(player),
            "opponent_stack_left": self.stack_left(1 - player),
            "pot": self.pot(),
            "to_call": self.to_call(player),
            "legal_actions": self.legal_actions() if self.to_act == player else [],
            "action_history": list(self.action_history),
            "done": self.done,
        }

    def apply(self, action: str) -> None:
        if self.done:
            raise IllegalAction("hand is already over")
        player = self.to_act
        legal = self.legal_actions()
        if action not in legal:
            raise IllegalAction(f"{action!r} not legal; choices were {legal}")
        self.action_history.append(action)

        if action == "fold":
            self.folded = player
            self._finish(went_to_showdown=False)
            return

        if action == "call":
            # Cap at what's actually behind -- an all-in call for less is
            # still a legal call, it just doesn't fully match the bet.
            tc = self.to_call(player)
            actual = min(tc, self.stack_left(player))
            self.contributed[player] += actual
            shortfall = tc - actual
            if shortfall > 0:
                # Heads-up: nobody else could have contested the uncalled
                # portion of the bet either, so it goes back to the raiser
                # immediately rather than sitting in a pot no one fought for.
                self.contributed[1 - player] -= shortfall
            self._advance_round_or_close()
            return

        if action == "check":
            self.checks_this_round += 1
            if self.checks_this_round >= 2:
                self._advance_round_or_close()
            else:
                self.to_act = 1 - player
            return

        if action == "raise":
            bet_size = BET_SIZE[self.round]
            # legal_actions() already guaranteed this player can afford a
            # full raise, so no capping needed here.
            self.contributed[player] += self.to_call(player) + bet_size
            self.raises_this_round += 1
            self.checks_this_round = 0
            self.to_act = 1 - player
            return

        raise IllegalAction(f"unknown action {action!r}")  # pragma: no cover

    def _advance_round_or_close(self) -> None:
        if self.round == 1:
            # self.board was already drawn in start(); just reveal it.
            self.round = 2
            self.to_act = 0
            self.checks_this_round = 0
            self.raises_this_round = 0
        else:
            self._finish(went_to_showdown=True)

    def _finish(self, went_to_showdown: bool) -> None:
        self.done = True
        pot = self.pot()
        rake = (pot * self.rake_bps) // 10000
        payout_pool = pot - rake

        if not went_to_showdown:
            winner = 1 - self.folded
            payouts = {winner: payout_pool, self.folded: 0}
        else:
            cmp = compare(self.hole[0], self.hole[1], self.board)
            if cmp == 0:
                winner = None
                # split pot as evenly as possible; any odd chip favors seat 0
                half = payout_pool // 2
                payouts = {0: half + (payout_pool % 2), 1: half}
            elif cmp == 1:
                winner = 0
                payouts = {0: payout_pool, 1: 0}
            else:
                winner = 1
                payouts = {0: 0, 1: payout_pool}

        payoffs = {p: payouts[p] - self.contributed[p] for p in (0, 1)}
        self.result = HandResult(
            payoffs=payoffs,
            pot_before_rake=pot,
            rake_taken=rake,
            went_to_showdown=went_to_showdown,
            winner=winner,
            hole_cards=dict(self.hole),
            board_card=self.board,
        )

    def to_dict(self) -> dict:
        """Serialize this hand's full state -- everything needed to
        reconstruct it with from_dict(). Safe to call at any point in a
        hand (mid-betting or finished): no RNG/deck state is needed
        because start() draws every card the hand will ever use up
        front (see the comment in start())."""
        return {
            "rake_bps": self.rake_bps,
            "stacks": dict(self.stacks),
            "round": self.round,
            "to_act": self.to_act,
            "contributed": dict(self.contributed),
            "hole": {p: str(c) for p, c in self.hole.items()},
            "board": str(self.board) if self.board else None,
            "checks_this_round": self.checks_this_round,
            "raises_this_round": self.raises_this_round,
            "folded": self.folded,
            "done": self.done,
            "action_history": list(self.action_history),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LeducHand":
        """Inverse of to_dict(). Pass a fresh rng -- it's never actually
        used again once a hand is reconstructed this way, since nothing
        left in the hand ever draws a new card."""
        # JSON round-trips always turn int dict keys into strings, so every
        # {0: ..., 1: ...} mapping needs its keys cast back on the way in.
        stacks = {int(p): v for p, v in data["stacks"].items()}
        hand = cls(rng=random.Random(), rake_bps=data["rake_bps"], stacks=stacks)
        hand.round = data["round"]
        hand.to_act = data["to_act"]
        hand.contributed = {int(p): v for p, v in data["contributed"].items()}
        hand.hole = {int(p): Card.parse(c) for p, c in data["hole"].items()}
        hand.board = Card.parse(data["board"]) if data["board"] else None
        hand.checks_this_round = data["checks_this_round"]
        hand.raises_this_round = data["raises_this_round"]
        hand.folded = data["folded"]
        hand.done = data["done"]
        hand.action_history = list(data.get("action_history", []))
        if hand.done:
            # Recompute the result rather than serializing it separately --
            # _finish() is deterministic given the state above.
            hand._finish(went_to_showdown=hand.folded is None)
        return hand
