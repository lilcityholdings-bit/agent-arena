"""Runs a full match (many hands) between two in-process Bot objects,
recording every hand to the ledger. This is the synchronous path used for
local baseline-bot tournaments and for tests. Remote bots driven over HTTP
use engine.LeducHand directly, one action per request -- see api/app.py.
"""
from __future__ import annotations

import random
import sqlite3

from bots.base import Bot
from engine.leduc import ANTE, LeducHand
from engine.duel import DuelFight
from ledger import db


def run_match(
    conn: sqlite3.Connection,
    bot_a_id: int,
    bot_b_id: int,
    bot_a: Bot,
    bot_b: Bot,
    hands: int,
    rake_bps: int = 500,
    rng: random.Random | None = None,
    enforce_bankroll: bool = True,
) -> int:
    """Plays `hands` hands, alternating who sits in seat 0 each hand so
    position is fair, and records everything to the ledger. Returns the
    match_id.

    When `enforce_bankroll` is True (the default), each hand's stacks are
    read fresh from each bot's current ledger balance, so a bot can never
    bet more than it actually has -- and the match stops early if either
    bot can no longer cover the ante ("busted")."""
    rng = rng or random.Random()
    match_id = db.create_match(conn, bot_a_id, bot_b_id, hands, rake_bps)

    hands_played = 0
    for hand_number in range(1, hands + 1):
        # Alternate seating so neither bot always acts first.
        if hand_number % 2 == 1:
            seat0_bot, seat1_bot = bot_a, bot_b
            seat0_id, seat1_id = bot_a_id, bot_b_id
        else:
            seat0_bot, seat1_bot = bot_b, bot_a
            seat0_id, seat1_id = bot_b_id, bot_a_id

        if enforce_bankroll:
            balances = {
                seat0_id: db.get_bot(conn, seat0_id)["balance"],
                seat1_id: db.get_bot(conn, seat1_id)["balance"],
            }
            if balances[seat0_id] < ANTE or balances[seat1_id] < ANTE:
                break  # one side is busted; stop the match early
            stacks = {0: balances[seat0_id], 1: balances[seat1_id]}
        else:
            stacks = None

        hand = LeducHand(rng=rng, rake_bps=rake_bps, **({"stacks": stacks} if stacks else {}))
        hand.start()
        seats = {0: seat0_bot, 1: seat1_bot}
        while not hand.done:
            player = hand.to_act
            state = hand.state_for(player)
            action = seats[player].act(state)
            hand.apply(action)

        result = hand.result
        db.record_hand(
            conn, match_id, hand_number, seat0_id, seat1_id,
            payoffs=result.payoffs, pot_before_rake=result.pot_before_rake,
            rake_taken=result.rake_taken, winner=result.winner,
            went_to_showdown=result.went_to_showdown,
            hole_cards=result.hole_cards, board_card=result.board_card,
        )
        hands_played += 1

    db.finish_match(conn, match_id)
    return match_id


def run_duel_match(
    conn: sqlite3.Connection,
    bot_a_id: int,
    bot_b_id: int,
    bot_a: Bot,
    bot_b: Bot,
    fights: int,
    rake_bps: int = 500,
    stake: int = 10,
    rng: random.Random | None = None,
    enforce_bankroll: bool = True,
) -> int:
    """The Duel equivalent of run_match: plays `fights` fights, alternating
    seating for fairness, recording each to the same ledger under
    game_type='duel'. Returns the match_id."""
    rng = rng or random.Random()
    match_id = db.create_match(conn, bot_a_id, bot_b_id, fights, rake_bps, game_type="duel")

    for fight_number in range(1, fights + 1):
        if fight_number % 2 == 1:
            seat0_bot, seat1_bot = bot_a, bot_b
            seat0_id, seat1_id = bot_a_id, bot_b_id
        else:
            seat0_bot, seat1_bot = bot_b, bot_a
            seat0_id, seat1_id = bot_b_id, bot_a_id

        if enforce_bankroll:
            balances = {
                seat0_id: db.get_bot(conn, seat0_id)["balance"],
                seat1_id: db.get_bot(conn, seat1_id)["balance"],
            }
            if balances[seat0_id] < stake or balances[seat1_id] < stake:
                break  # one side can't cover the entry stake; stop early
            stacks = {0: balances[seat0_id], 1: balances[seat1_id]}
        else:
            stacks = {0: 10 ** 9, 1: 10 ** 9}

        fight = DuelFight(rng=rng, rake_bps=rake_bps, stake=stake, stacks=stacks)
        fight.start()
        seats = {0: seat0_bot, 1: seat1_bot}
        while not fight.done:
            for p in (0, 1):
                if p not in fight.pending_moves:
                    fight.submit(p, seats[p].act(fight.state_for(p)))

        result = fight.result
        db.record_hand(
            conn, match_id, fight_number, seat0_id, seat1_id,
            payoffs=result.payoffs, pot_before_rake=result.pot_before_rake,
            rake_taken=result.rake_taken, winner=result.winner,
            game_type="duel", went_to_showdown=result.ko,
            detail={"rounds_played": result.rounds_played, "final_hp": result.final_hp},
        )

    db.finish_match(conn, match_id)
    return match_id
