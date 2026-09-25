"""HTTP API so external bots (your own agents, or anyone else's) can
register, join the matchmaking lobby or start a match directly, and play
hand-by-hand against either a baseline bot or another registered bot.

Durability: every mutation to a live (in-progress) match is written
through to the `live_matches` table (ledger/db.py) in the same request
that made it, in addition to an in-process cache (LIVE_MATCHES) kept for
speed. A restart loses nothing but re-reads state from SQLite on first
touch -- see _get_live(). Completed hands and running balances were
already durable before this; now in-progress matches are too.

Auth: pass your api_key either as header `X-API-Key` or in the JSON body.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, render_template, request

from bots.cfr_bot import CFRBot
from bots.heuristic_bot import HeuristicBot
from bots.random_bot import RandomBot
from bots.duel_boss_bot import BossDuelBot
from bots.duel_heuristic_bot import HeuristicDuelBot
from bots.duel_random_bot import RandomDuelBot
from engine.leduc import ANTE, IllegalAction, LeducHand
from engine.duel import DuelFight
from engine.duel import IllegalAction as DuelIllegalAction
from ledger import db

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "templates")
app = Flask(__name__, template_folder=_TEMPLATE_DIR)
_lock = threading.Lock()
_rng = random.Random()

# Baseline bots are real ledger accounts (huge starting bankroll) so every
# hand -- baseline or remote-vs-remote -- is recorded the same way, with
# no special-casing and no foreign-key gymnastics.
# random_bot and heuristic_bot are easy; cfr_bot plays a strategy computed
# offline by bots/cfr_train.py and is the real challenge -- see its
# docstring for exactly what it is and isn't (approximately) solved for.
BASELINE_BOT_FACTORIES = {
    "random_bot": lambda: RandomBot(rng=_rng),
    "heuristic_bot": lambda: HeuristicBot(rng=_rng),
    "cfr_bot": lambda: CFRBot(rng=_rng),
}
# Duel's baseline bots -- same skill hierarchy shape (random < heuristic <
# hard), same reserved-name/auto-registered pattern, just a disjoint name
# set so there's no collision with the leduc baselines in the shared
# `bots` table. See bots/duel_boss_bot.py for what "hard" means here.
DUEL_BASELINE_BOT_FACTORIES = {
    "random_duel_bot": lambda: RandomDuelBot(rng=_rng),
    "heuristic_duel_bot": lambda: HeuristicDuelBot(rng=_rng),
    "boss_duel_bot": lambda: BossDuelBot(rng=_rng),
}
BASELINE_STARTING_BALANCE = 1_000_000
LOBBY_FALLBACK_BOT = "heuristic_bot"
LOBBY_DUEL_FALLBACK_BOT = "heuristic_duel_bot"
LOBBY_DEFAULT_FALLBACK_SECONDS = 20

DEFAULT_STARTING_BALANCE = 1000
DEFAULT_RAKE_BPS = 500
MAX_HANDS_PER_MATCH = 10000
MAX_FIGHTS_PER_MATCH = 10000
DEFAULT_DUEL_STAKE = 10
MAX_DUEL_STAKE = 100_000
# practice_chips: the free-to-mint number every bot starts with (`balance`).
# usd: real_balance, in cents -- only reachable via an admin-attested
# credit (see /admin/bots/<id>/credit) since there's no self-service
# deposit path yet. Anything else is refused outright.
SUPPORTED_CURRENCIES = ("practice_chips", "usd")
SUPPORTED_CURRENCY = "practice_chips"  # kept as the explicit default
DEFAULT_UNIT_VALUE_CENTS = 100  # $1.00 per game-engine chip unit, for usd matches
MAX_UNIT_VALUE_CENTS = 100_000  # $1,000 per unit -- a sanity ceiling, not a business decision

# --------------------------------------------------------------------------
# Phase 1 monetization: "beat the boss" challenges.
#
# A bot pays a flat, non-refundable real-money entry fee for one shot at
# a boss bot over a fixed number of hands/fights, played with ordinary
# practice chips (no extra real money at risk beyond the entry fee -- see
# the docstring on the /challenges/boss route for why). Win (net chips
# positive over the whole challenge, not just "won the last hand") and
# the challenge pays PRIZE_MULTIPLIER times the entry fee from the prize
# pool; lose and the entry fee stays with the house. This is deliberately
# the simplest monetization mechanism that needs no payment processor and
# no liquidity beyond what an admin funds into the prize pool up front --
# see credit_real_balance/fund_prize_pool for how real money gets in.
#
# These specific numbers (fee bounds, multiplier, challenge length) are a
# first pass -- see the "Design review" section of README.md for the
# actual math behind them and what was adjusted after checking it.
# --------------------------------------------------------------------------
BOSS_CHALLENGE_BOSS_NAME = {"leduc": "cfr_bot", "duel": "boss_duel_bot"}
BOSS_CHALLENGE_LENGTH = {"leduc": 150, "duel": 60}
BOSS_CHALLENGE_ENTRY_FEE_DEFAULT_CENTS = 500     # $5.00
BOSS_CHALLENGE_ENTRY_FEE_MIN_CENTS = 100         # $1.00
BOSS_CHALLENGE_ENTRY_FEE_MAX_CENTS = 10_000      # $100.00
# Measured, not guessed: simulating heuristic_bot vs cfr_bot over 150
# hands wins ~31% of the time, and heuristic_duel_bot could not beat
# boss_duel_bot even once in 50 tries at 25-75 fights. A multiplier is
# only safe if it can't go net-negative for the house even against a
# challenger as strong as the boss itself (the worst case a symmetric
# game can produce, ~50% win rate) -- 1 - 0.5*multiplier >= 0 requires
# multiplier <= 2. See README.md's "Design review" section for the full
# investigation, including why this was 5 in an earlier, unchecked draft.
BOSS_CHALLENGE_PRIZE_MULTIPLIER = 2              # win 2x your entry fee -- safe even in the worst case
BOSS_CHALLENGE_RAKE_BPS = 0  # the entry fee is the monetization; no extra rake stacked on top
# Free practice chips topped up before a challenge starts, so a low (or
# zero) practice balance -- completely unrelated to the real entry fee
# just paid -- can never bust the challenge match before it's played a
# single hand/fight. Same order of magnitude as BASELINE_STARTING_BALANCE.
BOSS_CHALLENGE_MIN_PRACTICE_BALANCE = 1_000_000


def _resolve_currency(body: dict) -> tuple[str, int, str | None]:
    """Returns (currency, unit_value_cents, error). On error the other two
    values are meaningless and the caller should 400 with the error."""
    currency = body.get("currency", SUPPORTED_CURRENCY)
    if currency not in SUPPORTED_CURRENCIES:
        return currency, 1, f"currency {currency!r} isn't available -- choose one of {SUPPORTED_CURRENCIES!r}"
    if currency == "practice_chips":
        return currency, 1, None
    unit_value_cents = int(body.get("unit_value_cents", DEFAULT_UNIT_VALUE_CENTS))
    if not (1 <= unit_value_cents <= MAX_UNIT_VALUE_CENTS):
        return currency, 1, f"unit_value_cents must be between 1 and {MAX_UNIT_VALUE_CENTS}"
    return currency, unit_value_cents, None

# match_id -> live match dict (with real Bot/LeducHand objects). In-memory
# cache only -- ledger.db.live_matches is the durable source of truth;
# see _get_live()/_persist_live().
LIVE_MATCHES: dict[int, dict] = {}
# Same idea, for Duel matches -- a separate dict (not a shared one keyed
# by match_id) because match_id numbering is shared across both games via
# the one `matches` table, but the two dicts hold objects shaped
# completely differently (LeducHand vs DuelFight), and every duel
# call-site already knows it's on the duel path, so there's no ambiguity.
LIVE_DUEL_MATCHES: dict[int, dict] = {}


def _ensure_baseline_bots(conn, factories: dict = None) -> dict[str, int]:
    ids = {}
    for name in (factories or BASELINE_BOT_FACTORIES):
        row = db.get_or_create_bot_by_name(conn, name, BASELINE_STARTING_BALANCE)
        ids[name] = row["id"]
    return ids


def _all_reserved_names() -> set:
    return set(BASELINE_BOT_FACTORIES) | set(DUEL_BASELINE_BOT_FACTORIES)


def _get_api_key() -> str | None:
    return request.headers.get("X-API-Key") or (request.get_json(silent=True) or {}).get("api_key")


def _require_bot():
    api_key = _get_api_key()
    if not api_key:
        return None, (jsonify(error="missing api_key (header X-API-Key or json body)"), 401)
    with db.connect() as conn:
        bot = db.get_bot_by_api_key(conn, api_key)
    if bot is None:
        return None, (jsonify(error="invalid api_key"), 401)
    return bot, None


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok")


@app.route("/", methods=["GET"])
def dashboard():
    with db.connect() as conn:
        bots = [dict(r) for r in db.list_bots(conn)]
        matches = [dict(r) for r in db.recent_matches(conn, limit=25)]
        house_rake = db.house_balance(conn)
        real_house_rake_cents = db.real_house_balance(conn)
        prize_pool_cents = db.prize_pool_balance(conn)
    return render_template(
        "index.html", bots=bots, matches=matches, house_rake=house_rake,
        real_house_rake_cents=real_house_rake_cents, prize_pool_cents=prize_pool_cents,
    )


@app.route("/bots", methods=["POST"])
def register_bot():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(error="name is required"), 400
    if name in _all_reserved_names():
        return jsonify(error=f"{name!r} is a reserved baseline-bot name"), 400
    starting_balance = int(body.get("starting_balance", DEFAULT_STARTING_BALANCE))
    with db.connect() as conn:
        try:
            bot = db.create_bot(conn, name, starting_balance)
        except Exception as exc:  # unique constraint, etc.
            return jsonify(error=f"could not register bot: {exc}"), 400
    return jsonify(bot), 201


@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    with db.connect() as conn:
        rows = db.list_bots(conn)
        house = db.house_balance(conn)
        real_house = db.real_house_balance(conn)
    return jsonify(
        house_rake_collected=house,
        house_real_rake_collected_cents=real_house,
        bots=[{"id": r["id"], "name": r["name"], "balance": r["balance"], "real_balance": r["real_balance"]} for r in rows],
    )


# --------------------------------------------------------------------------
# Real value.
#
# `balance` (practice chips) is what practice_chips matches play with.
# `real_balance` (in cents) is real, and moves exactly two ways:
#   1. An admin-attested credit/debit below -- Robert (or whoever holds
#      ARENA_ADMIN_SECRET) records that value actually moved outside the
#      app (a bank transfer, a crypto payment) and attests to it here.
#      This is real bookkeeping, not a simulation: the money already
#      moved somewhere else; this just records it. Refuses outright
#      (503) if ARENA_ADMIN_SECRET isn't set, so it's impossible to
#      credit/debit real value by accident on a deployment nobody
#      configured for it.
#   2. Playing a currency="usd" match -- see /matches and /lobby/join.
#      Bankroll caps and payouts work exactly like practice_chips
#      matches, just denominated in real_balance instead.
# /deposit and /withdraw below remain 501 stubs deliberately -- they mean
# something different (automatic, self-service, on-chain), which still
# isn't built.
# --------------------------------------------------------------------------

ADMIN_SECRET_ENV_VAR = "ARENA_ADMIN_SECRET"


def _require_admin():
    configured = os.environ.get(ADMIN_SECRET_ENV_VAR)
    if not configured:
        return (
            jsonify(error=f"admin actions are disabled until {ADMIN_SECRET_ENV_VAR} is set on the server"),
            503,
        )
    provided = request.headers.get("X-Admin-Secret")
    if not provided or provided != configured:
        return jsonify(error="invalid or missing X-Admin-Secret"), 401
    return None


@app.route("/admin/bots/<int:bot_id>/credit", methods=["POST"])
def admin_credit(bot_id: int):
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        amount_cents = int(body["amount_cents"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="amount_cents (integer, positive) is required"), 400
    admin_note = str(body.get("note", "")).strip()
    if not admin_note:
        return jsonify(error="note is required -- record what this credit corresponds to (e.g. 'bank transfer ref #123')"), 400
    with db.connect() as conn:
        if db.get_bot(conn, bot_id) is None:
            return jsonify(error="no such bot"), 404
        try:
            updated = db.credit_real_balance(conn, bot_id, amount_cents, admin_note)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    return jsonify(bot_id=bot_id, real_balance=updated["real_balance"], credited_cents=amount_cents), 200


@app.route("/admin/bots/<int:bot_id>/debit", methods=["POST"])
def admin_debit(bot_id: int):
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        amount_cents = int(body["amount_cents"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="amount_cents (integer, positive) is required"), 400
    admin_note = str(body.get("note", "")).strip()
    if not admin_note:
        return jsonify(error="note is required -- record what this debit corresponds to (e.g. 'payout sent, ref #123')"), 400
    with db.connect() as conn:
        if db.get_bot(conn, bot_id) is None:
            return jsonify(error="no such bot"), 404
        try:
            updated = db.debit_real_balance(conn, bot_id, amount_cents, admin_note)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    return jsonify(bot_id=bot_id, real_balance=updated["real_balance"], debited_cents=amount_cents), 200


@app.route("/bots/<int:bot_id>/transactions", methods=["GET"])
def bot_transactions(bot_id: int):
    """A bot can audit its own real-value history with its own api_key;
    an admin can audit any bot's with X-Admin-Secret."""
    provided_admin_secret = request.headers.get("X-Admin-Secret")
    configured_admin_secret = os.environ.get(ADMIN_SECRET_ENV_VAR)
    is_admin = bool(configured_admin_secret) and provided_admin_secret == configured_admin_secret
    if not is_admin:
        bot, err = _require_bot()
        if err:
            return err
        if bot["id"] != bot_id:
            return jsonify(error="you can only view your own transaction history (or authenticate as admin)"), 403
    with db.connect() as conn:
        rows = db.real_value_transactions(conn, bot_id)
    return jsonify(bot_id=bot_id, transactions=[dict(r) for r in rows])

@app.route("/bots/<int:bot_id>/deposit", methods=["POST"])
def deposit(bot_id: int):
    bot, err = _require_bot()
    if err:
        return err
    if bot["id"] != bot_id:
        return jsonify(error="you can only deposit into your own account"), 403
    return jsonify(
        error="deposits are not implemented yet",
        detail=(
            "real_balance has no path in from any chain right now. This endpoint is a stable "
            "placeholder for when stablecoin settlement is wired in -- calling it moves no money "
            "and changes no balance."
        ),
    ), 501


@app.route("/bots/<int:bot_id>/withdraw", methods=["POST"])
def withdraw(bot_id: int):
    bot, err = _require_bot()
    if err:
        return err
    if bot["id"] != bot_id:
        return jsonify(error="you can only withdraw from your own account"), 403
    return jsonify(
        error="withdrawals are not implemented yet",
        detail=(
            "real_balance has no path out to any chain right now. This endpoint is a stable "
            "placeholder for when stablecoin settlement is wired in -- calling it moves no money "
            "and changes no balance."
        ),
    ), 501


# --------------------------------------------------------------------------
# Opponent resolution + live-match (de)serialization
# --------------------------------------------------------------------------

def _resolve_opponent(conn, opponent_key: str, factories: dict = None) -> tuple[int, str, bool]:
    """Returns (opponent_bot_id, opponent_name, is_baseline).

    is_baseline has to be right regardless of how the opponent was named,
    because it's what _check_wager_policy uses to decide "the computer is
    free" vs "competitive play needs a real wager" -- looking it up by the
    reserved name is the common path, but a baseline bot is also just a
    row with a real id in the `bots` table, so a client passing that id
    instead of the name must still resolve to is_baseline=True. Otherwise
    id-based lookup would be a loophole around both the free-computer and
    wager-required rules.
    """
    factories = factories or BASELINE_BOT_FACTORIES
    if opponent_key in factories:
        ids = _ensure_baseline_bots(conn, factories)
        return ids[opponent_key], opponent_key, True
    opp_row = db.get_bot(conn, int(opponent_key)) if opponent_key.isdigit() else None
    if opp_row is None:
        raise ValueError(f"unknown opponent {opponent_key!r} (must be a baseline bot name or a bot id)")
    return opp_row["id"], opp_row["name"], opp_row["name"] in factories


def _check_wager_policy(is_baseline: bool, currency: str) -> str | None:
    """The traffic-generation rule: playing the computer (a baseline bot)
    is always free, and competitive play (another real bot) always
    requires an actual wager. practice_chips can't be a real wager -- it's
    the free currency every bot is minted with -- so "requires a wager"
    concretely means "requires currency=usd", the one currency backed by
    real_balance an admin actually funded. Returns an error string to 400
    with, or None if `currency` is allowed for this kind of opponent."""
    if is_baseline and currency != "practice_chips":
        return "the computer is free to play -- baseline bots only play practice_chips matches, never real money"
    if not is_baseline and currency != "usd":
        return "competitive play (bot vs bot) requires a real wager -- pass currency=\"usd\" (baseline bots are the free, practice_chips option)"
    return None


def _new_live_match(
    match_id: int,
    creator_bot_id: int,
    opponent_bot_id: int,
    opponent_name: str,
    is_baseline: bool,
    hands: int,
    rake_bps: int,
    currency: str = SUPPORTED_CURRENCY,
    unit_value_cents: int = 1,
) -> dict:
    return {
        "match_id": match_id,
        "creator_bot_id": creator_bot_id,
        "opponent_bot_id": opponent_bot_id,
        "opponent_name": opponent_name,
        "opponent_is_baseline": is_baseline,
        "opponent_bot_obj": BASELINE_BOT_FACTORIES[opponent_name]() if is_baseline else None,
        "hands_requested": hands,
        "hands_played": 0,
        "rake_bps": rake_bps,
        "currency": currency,
        "unit_value_cents": unit_value_cents,
        "hand": None,
        "hand_number": 0,
        "done": False,
    }


def _serialize_live(live: dict) -> dict:
    return {
        "creator_bot_id": live["creator_bot_id"],
        "opponent_bot_id": live["opponent_bot_id"],
        "opponent_name": live["opponent_name"],
        "opponent_is_baseline": live["opponent_is_baseline"],
        "hands_requested": live["hands_requested"],
        "hands_played": live["hands_played"],
        "rake_bps": live["rake_bps"],
        "currency": live["currency"],
        "unit_value_cents": live["unit_value_cents"],
        "hand_number": live["hand_number"],
        "done": live["done"],
        "hand": live["hand"].to_dict() if live["hand"] is not None else None,
    }


def _deserialize_live(match_id: int, data: dict) -> dict:
    live = _new_live_match(
        match_id,
        data["creator_bot_id"],
        data["opponent_bot_id"],
        data["opponent_name"],
        data["opponent_is_baseline"],
        data["hands_requested"],
        data["rake_bps"],
        data.get("currency", SUPPORTED_CURRENCY),
        data.get("unit_value_cents", 1),
    )
    live["hands_played"] = data["hands_played"]
    live["hand_number"] = data["hand_number"]
    live["done"] = data["done"]
    live["hand"] = LeducHand.from_dict(data["hand"]) if data["hand"] is not None else None
    return live


def _persist_live(live: dict) -> None:
    with db.connect() as conn:
        db.save_live_match(conn, live["match_id"], live["creator_bot_id"], live["opponent_bot_id"], _serialize_live(live), game_type="leduc")


def _maybe_resolve_boss_challenge(conn, match_id: int, challenger_bot_id: int) -> dict | None:
    """If this match_id belongs to a pending boss challenge, decides won
    vs. lost by the challenge's actual win condition -- net payoff to the
    challenger summed across every hand/fight actually played in this
    match, not just whether the last one went their way -- and pays out
    (or doesn't) accordingly. Returns the resolved challenge row, or None
    if this match had no challenge attached."""
    challenge = db.get_boss_challenge_by_match(conn, match_id)
    if challenge is None or challenge["status"] != "pending":
        return None
    net = 0
    for row in db.match_history(conn, match_id):
        if row["seat0_bot_id"] == challenger_bot_id:
            net += row["payoff_seat0"]
        elif row["seat1_bot_id"] == challenger_bot_id:
            net += row["payoff_seat1"]
    return db.resolve_boss_challenge(conn, challenge["id"], won=net > 0)


def _finish_match(live: dict) -> None:
    live["done"] = True
    with db.connect() as conn:
        db.finish_match(conn, live["match_id"])
        db.delete_live_match(conn, live["match_id"])
        _maybe_resolve_boss_challenge(conn, live["match_id"], live["creator_bot_id"])
    LIVE_MATCHES.pop(live["match_id"], None)


def _get_live(match_id: int) -> dict | None:
    live = LIVE_MATCHES.get(match_id)
    if live is not None:
        return live
    with db.connect() as conn:
        data = db.load_live_match(conn, match_id, game_type="leduc")
    if data is None:
        return None
    live = _deserialize_live(match_id, data)
    LIVE_MATCHES[match_id] = live
    return live


# --------------------------------------------------------------------------
# Hand/round progression
# --------------------------------------------------------------------------

def _seats_for_hand(live: dict) -> dict:
    """Which logical side ('creator' / 'opponent') sits in seat 0 vs 1 for
    the CURRENT hand_number. Alternates so position is fair over a match."""
    if live["hand_number"] % 2 == 1:
        return {0: "creator", 1: "opponent"}
    return {0: "opponent", 1: "creator"}


def _my_seat(live: dict, side: str) -> int:
    seats = _seats_for_hand(live)
    return 0 if seats[0] == side else 1


def _start_next_hand(live: dict) -> None:
    """Starts the next hand, enforcing real bankroll: each side's stack is
    read fresh from its current ledger balance, so a bot can never bet
    more than it actually has. If either side can't even cover the ante
    anymore, the match ends right here (busted)."""
    live["hand_number"] += 1
    seats = _seats_for_hand(live)
    balance_field = "balance" if live["currency"] == SUPPORTED_CURRENCY else "real_balance"
    unit_value_cents = live["unit_value_cents"]
    with db.connect() as conn:
        balances = {
            "creator": db.get_bot(conn, live["creator_bot_id"])[balance_field],
            "opponent": db.get_bot(conn, live["opponent_bot_id"])[balance_field],
        }
    # For a real-money match, balances are in cents; convert down to the
    # engine's game-unit stacks (floor -- a partial unit can't be staked).
    if live["currency"] != SUPPORTED_CURRENCY:
        balances = {side: amount // unit_value_cents for side, amount in balances.items()}
    stacks = {0: balances[seats[0]], 1: balances[seats[1]]}

    if min(stacks.values()) < ANTE:
        _finish_match(live)
        return

    hand = LeducHand(rng=_rng, rake_bps=live["rake_bps"], stacks=stacks)
    hand.start()
    live["hand"] = hand
    _autoplay_baseline_turns(live)


def _autoplay_baseline_turns(live: dict) -> None:
    """If it's the baseline opponent's turn, play it immediately -- a
    baseline bot doesn't need a real HTTP round trip to decide."""
    if not live["opponent_is_baseline"]:
        return
    hand = live["hand"]
    while not hand.done:
        seats = _seats_for_hand(live)
        side_to_act = seats[hand.to_act]
        if side_to_act != "opponent":
            break
        state = hand.state_for(hand.to_act)
        action = live["opponent_bot_obj"].act(state)
        hand.apply(action)


def _settle_hand_and_maybe_advance(live: dict) -> None:
    """Call once a hand is done: record it to the ledger, then either
    start the next hand or close out the match."""
    hand = live["hand"]
    seats = _seats_for_hand(live)
    seat0_id = live["creator_bot_id"] if seats[0] == "creator" else live["opponent_bot_id"]
    seat1_id = live["creator_bot_id"] if seats[1] == "creator" else live["opponent_bot_id"]

    result = hand.result
    with db.connect() as conn:
        db.record_hand(
            conn, live["match_id"], live["hand_number"], seat0_id, seat1_id,
            payoffs=result.payoffs, pot_before_rake=result.pot_before_rake,
            rake_taken=result.rake_taken, winner=result.winner,
            went_to_showdown=result.went_to_showdown,
            hole_cards=result.hole_cards, board_card=result.board_card,
            currency=live["currency"], unit_value_cents=live["unit_value_cents"],
        )

    live["hands_played"] += 1
    if live["hands_played"] >= live["hands_requested"]:
        _finish_match(live)
    else:
        _start_next_hand(live)


def _state_payload(live: dict, side: str) -> dict:
    seat = _my_seat(live, side)
    state = live["hand"].state_for(seat)
    state["your_turn"] = live["hand"].to_act == seat
    state["hand_number"] = live["hand_number"]
    state["hands_played"] = live["hands_played"]
    state["hands_requested"] = live["hands_requested"]
    state["match_done"] = False
    return state


# --------------------------------------------------------------------------
# Direct match creation (you already know your opponent's id or name)
# --------------------------------------------------------------------------

@app.route("/matches", methods=["POST"])
def create_match():
    bot, err = _require_bot()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    opponent_key = str(body.get("opponent", "")).strip()
    hands = int(body.get("hands", 1))
    rake_bps = int(body.get("rake_bps", DEFAULT_RAKE_BPS))
    if not opponent_key:
        return jsonify(error="opponent is required (a baseline bot name, or another bot's id)"), 400
    if not (1 <= hands <= MAX_HANDS_PER_MATCH):
        return jsonify(error=f"hands must be between 1 and {MAX_HANDS_PER_MATCH}"), 400
    currency, unit_value_cents, currency_error = _resolve_currency(body)
    if currency_error:
        return jsonify(error=currency_error), 400

    with _lock:
        try:
            with db.connect() as conn:
                opponent_bot_id, opponent_name, is_baseline = _resolve_opponent(conn, opponent_key)
                wager_error = _check_wager_policy(is_baseline, currency)
                if wager_error:
                    return jsonify(error=wager_error), 400
                match_id = db.create_match(conn, bot["id"], opponent_bot_id, hands, rake_bps, currency, unit_value_cents)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        live = _new_live_match(match_id, bot["id"], opponent_bot_id, opponent_name, is_baseline, hands, rake_bps, currency, unit_value_cents)
        LIVE_MATCHES[match_id] = live
        _start_next_hand(live)
        if not live["done"]:
            _persist_live(live)

    if live["done"]:
        return jsonify(match_id=match_id, opponent=opponent_name, match_done=True, note="a side couldn't cover the ante; match ended immediately"), 201
    return jsonify(match_id=match_id, opponent=opponent_name, hands_requested=hands, rake_bps=rake_bps, currency=currency, unit_value_cents=unit_value_cents), 201


@app.route("/matches/<int:match_id>/state", methods=["GET"])
def match_state(match_id: int):
    bot, err = _require_bot()
    if err:
        return err
    with _lock:
        live = _get_live(match_id)
        if live is None:
            return jsonify(error="unknown or already-archived match_id"), 404
        if bot["id"] not in (live["creator_bot_id"], live["opponent_bot_id"]):
            return jsonify(error="you are not a participant in this match"), 403
        if live["done"]:
            return jsonify(match_done=True, hands_played=live["hands_played"], hands_requested=live["hands_requested"])
        side = "creator" if bot["id"] == live["creator_bot_id"] else "opponent"
        return jsonify(_state_payload(live, side))


@app.route("/matches/<int:match_id>/action", methods=["POST"])
def match_action(match_id: int):
    bot, err = _require_bot()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    action = body.get("action")

    with _lock:
        live = _get_live(match_id)
        if live is None:
            return jsonify(error="unknown or already-archived match_id"), 404
        if live["done"]:
            return jsonify(error="match already finished"), 400
        if bot["id"] not in (live["creator_bot_id"], live["opponent_bot_id"]):
            return jsonify(error="you are not a participant in this match"), 403

        side = "creator" if bot["id"] == live["creator_bot_id"] else "opponent"
        hand = live["hand"]
        seat = _my_seat(live, side)
        if hand.to_act != seat:
            return jsonify(error="not your turn"), 409
        try:
            hand.apply(action)
        except IllegalAction as exc:
            return jsonify(error=str(exc), legal_actions=hand.legal_actions()), 400

        _autoplay_baseline_turns(live)

        if hand.done:
            _settle_hand_and_maybe_advance(live)
            if live["done"]:
                return jsonify(hand_resolved=True, match_done=True, hands_played=live["hands_played"])
            _persist_live(live)
            return jsonify(hand_resolved=True, match_done=False, state=_state_payload(live, side))

        _persist_live(live)
        return jsonify(hand_resolved=False, match_done=False, state=_state_payload(live, side))


@app.route("/matches/<int:match_id>", methods=["GET"])
def match_summary(match_id: int):
    with db.connect() as conn:
        rows = db.match_history(conn, match_id)
    if not rows:
        live = _get_live(match_id)
        if live is None:
            return jsonify(error="no such match"), 404
        return jsonify(
            match_id=match_id,
            hands_played=live["hands_played"],
            hands_requested=live["hands_requested"],
            done=live["done"],
        )
    return jsonify(match_id=match_id, hands=[dict(r) for r in rows])


# --------------------------------------------------------------------------
# Matchmaking lobby -- join a queue instead of needing an opponent's id
# --------------------------------------------------------------------------

@app.route("/lobby/join", methods=["POST"])
def lobby_join():
    """practice_chips is the free-to-play-the-computer currency -- and
    since a baseline bot never holds real_balance, a practice_chips entry
    can never be a competitive (bot vs bot) wager anyway, so there's no
    reason to make it wait: it's matched against a baseline bot instantly,
    which is exactly what "free, to generate traffic" wants. usd entries
    are the real thing -- they queue for a genuine opponent and never fall
    back to the computer, because competitive play requires a real wager,
    and a baseline bot can't supply one."""
    bot, err = _require_bot()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    hands = int(body.get("hands", 20))
    rake_bps = int(body.get("rake_bps", DEFAULT_RAKE_BPS))
    fallback_after_seconds = float(body.get("fallback_after_seconds", LOBBY_DEFAULT_FALLBACK_SECONDS))
    if not (1 <= hands <= MAX_HANDS_PER_MATCH):
        return jsonify(error=f"hands must be between 1 and {MAX_HANDS_PER_MATCH}"), 400
    currency, unit_value_cents, currency_error = _resolve_currency(body)
    if currency_error:
        return jsonify(error=currency_error), 400

    with _lock:
        with db.connect() as conn:
            existing_match_id = db.find_active_match_for_bot(conn, bot["id"])
            if existing_match_id:
                return jsonify(matched=True, match_id=existing_match_id)

            if currency == "practice_chips":
                opponent_bot_id, opponent_name, _ = _resolve_opponent(conn, LOBBY_FALLBACK_BOT)
                match_id = db.create_match(conn, bot["id"], opponent_bot_id, hands, rake_bps, currency, unit_value_cents)
            else:
                opponent_row = db.find_lobby_opponent(conn, bot["id"], hands, rake_bps, currency, unit_value_cents)
                if opponent_row is None:
                    db.join_lobby(conn, bot["id"], hands, rake_bps, fallback_after_seconds, currency, unit_value_cents)
                    return jsonify(matched=False, poll="/lobby/status")

                db.leave_lobby(conn, opponent_row["bot_id"])
                opponent_bot_row = db.get_bot(conn, opponent_row["bot_id"])
                match_id = db.create_match(conn, bot["id"], opponent_row["bot_id"], hands, rake_bps, currency, unit_value_cents)

        if currency == "practice_chips":
            live = _new_live_match(match_id, bot["id"], opponent_bot_id, opponent_name, True, hands, rake_bps, currency, unit_value_cents)
            LIVE_MATCHES[match_id] = live
            _start_next_hand(live)
            if not live["done"]:
                _persist_live(live)
            return jsonify(matched=True, match_id=match_id, opponent=opponent_name, note="practice_chips is free-to-play -- matched instantly against the computer; competitive bot-vs-bot play needs currency=\"usd\""), 201

        live = _new_live_match(match_id, bot["id"], opponent_row["bot_id"], opponent_bot_row["name"], False, hands, rake_bps, currency, unit_value_cents)
        LIVE_MATCHES[match_id] = live
        _start_next_hand(live)
        if not live["done"]:
            _persist_live(live)

    return jsonify(matched=True, match_id=match_id, opponent_bot_id=opponent_row["bot_id"], opponent=opponent_bot_row["name"]), 201


@app.route("/lobby/status", methods=["GET"])
def lobby_status():
    bot, err = _require_bot()
    if err:
        return err

    with _lock:
        with db.connect() as conn:
            existing_match_id = db.find_active_match_for_bot(conn, bot["id"])
            if existing_match_id:
                return jsonify(matched=True, match_id=existing_match_id)

            entry = db.get_lobby_entry(conn, bot["id"])
            if entry is None or entry["game_type"] != "leduc":
                return jsonify(matched=False, waiting=False, error="not in the lobby -- call /lobby/join first")

            # Only usd (competitive) entries ever sit in the lobby now --
            # practice_chips ("play the computer") entries are matched
            # instantly at /lobby/join and never reach here. So there's no
            # baseline fallback to offer: a real wager needs a real
            # opponent, however long that takes.
            return jsonify(matched=False, waiting=True, waiting_seconds=round(time.time() - entry["joined_at"], 1), note="usd lobby entries wait for a real opponent -- competitive play requires a real wager, so there's no auto-fallback to the computer")


@app.route("/lobby/leave", methods=["POST"])
def lobby_leave():
    bot, err = _require_bot()
    if err:
        return err
    with db.connect() as conn:
        db.leave_lobby(conn, bot["id"])
    return jsonify(left=True)


# ==========================================================================
# Duel: the martial-arts game. A deliberately separate endpoint family
# (/duel/...) rather than forcing it through /matches/*/action -- Leduc is
# turn-alternating (one action, one response tells you what happened)
# while Duel is simultaneous (both fighters submit before anything
# resolves), so "the same endpoint shape" would be the wrong abstraction.
# What IS actually shared: the bots table, the house rake account, the
# real-value ledger and audit log, and the live-match durability + lobby
# tables (tagged by game_type) -- see ledger/db.py.
# ==========================================================================

def _new_live_duel_match(
    match_id: int,
    creator_bot_id: int,
    opponent_bot_id: int,
    opponent_name: str,
    is_baseline: bool,
    fights: int,
    rake_bps: int,
    stake: int,
    currency: str = SUPPORTED_CURRENCY,
    unit_value_cents: int = 1,
) -> dict:
    return {
        "match_id": match_id,
        "creator_bot_id": creator_bot_id,
        "opponent_bot_id": opponent_bot_id,
        "opponent_name": opponent_name,
        "opponent_is_baseline": is_baseline,
        "opponent_bot_obj": DUEL_BASELINE_BOT_FACTORIES[opponent_name]() if is_baseline else None,
        "fights_requested": fights,
        "fights_played": 0,
        "rake_bps": rake_bps,
        "stake": stake,
        "currency": currency,
        "unit_value_cents": unit_value_cents,
        "fight": None,
        "fight_number": 0,
        "done": False,
    }


def _serialize_live_duel(live: dict) -> dict:
    return {
        "creator_bot_id": live["creator_bot_id"],
        "opponent_bot_id": live["opponent_bot_id"],
        "opponent_name": live["opponent_name"],
        "opponent_is_baseline": live["opponent_is_baseline"],
        "fights_requested": live["fights_requested"],
        "fights_played": live["fights_played"],
        "rake_bps": live["rake_bps"],
        "stake": live["stake"],
        "currency": live["currency"],
        "unit_value_cents": live["unit_value_cents"],
        "fight_number": live["fight_number"],
        "done": live["done"],
        "fight": live["fight"].to_dict() if live["fight"] is not None else None,
    }


def _deserialize_live_duel(match_id: int, data: dict) -> dict:
    live = _new_live_duel_match(
        match_id,
        data["creator_bot_id"],
        data["opponent_bot_id"],
        data["opponent_name"],
        data["opponent_is_baseline"],
        data["fights_requested"],
        data["rake_bps"],
        data["stake"],
        data.get("currency", SUPPORTED_CURRENCY),
        data.get("unit_value_cents", 1),
    )
    live["fights_played"] = data["fights_played"]
    live["fight_number"] = data["fight_number"]
    live["done"] = data["done"]
    live["fight"] = DuelFight.from_dict(data["fight"], rng=_rng) if data["fight"] is not None else None
    return live


def _persist_live_duel(live: dict) -> None:
    with db.connect() as conn:
        db.save_live_match(conn, live["match_id"], live["creator_bot_id"], live["opponent_bot_id"], _serialize_live_duel(live), game_type="duel")


def _finish_duel_match(live: dict) -> None:
    live["done"] = True
    with db.connect() as conn:
        db.finish_match(conn, live["match_id"])
        db.delete_live_match(conn, live["match_id"])
        _maybe_resolve_boss_challenge(conn, live["match_id"], live["creator_bot_id"])
    LIVE_DUEL_MATCHES.pop(live["match_id"], None)


def _get_live_duel(match_id: int) -> dict | None:
    live = LIVE_DUEL_MATCHES.get(match_id)
    if live is not None:
        return live
    with db.connect() as conn:
        data = db.load_live_match(conn, match_id, game_type="duel")
    if data is None:
        return None
    live = _deserialize_live_duel(match_id, data)
    LIVE_DUEL_MATCHES[match_id] = live
    return live


def _seats_for_fight(live: dict) -> dict:
    if live["fight_number"] % 2 == 1:
        return {0: "creator", 1: "opponent"}
    return {0: "opponent", 1: "creator"}


def _my_seat_duel(live: dict, side: str) -> int:
    seats = _seats_for_fight(live)
    return 0 if seats[0] == side else 1


def _start_next_fight(live: dict) -> None:
    live["fight_number"] += 1
    seats = _seats_for_fight(live)
    balance_field = "balance" if live["currency"] == SUPPORTED_CURRENCY else "real_balance"
    unit_value_cents = live["unit_value_cents"]
    with db.connect() as conn:
        balances = {
            "creator": db.get_bot(conn, live["creator_bot_id"])[balance_field],
            "opponent": db.get_bot(conn, live["opponent_bot_id"])[balance_field],
        }
    if live["currency"] != SUPPORTED_CURRENCY:
        balances = {side: amount // unit_value_cents for side, amount in balances.items()}
    stacks = {0: balances[seats[0]], 1: balances[seats[1]]}

    if min(stacks.values()) < live["stake"]:
        _finish_duel_match(live)
        return

    fight = DuelFight(rng=_rng, rake_bps=live["rake_bps"], stake=live["stake"], stacks=stacks)
    fight.start()
    live["fight"] = fight
    _autoplay_baseline_duel_moves(live)


def _autoplay_baseline_duel_moves(live: dict) -> None:
    """Pre-submits the baseline opponent's move for the current round, if
    it hasn't already, so a remote bot's move is the only thing the round
    is ever actually waiting on."""
    if not live["opponent_is_baseline"]:
        return
    fight = live["fight"]
    if fight is None or fight.done:
        return
    seats = _seats_for_fight(live)
    opp_seat = 0 if seats[0] == "opponent" else 1
    if opp_seat not in fight.pending_moves:
        state = fight.state_for(opp_seat)
        move = live["opponent_bot_obj"].act(state)
        fight.submit(opp_seat, move)


def _settle_fight_and_maybe_advance(live: dict) -> None:
    fight = live["fight"]
    seats = _seats_for_fight(live)
    seat0_id = live["creator_bot_id"] if seats[0] == "creator" else live["opponent_bot_id"]
    seat1_id = live["creator_bot_id"] if seats[1] == "creator" else live["opponent_bot_id"]

    result = fight.result
    with db.connect() as conn:
        db.record_hand(
            conn, live["match_id"], live["fight_number"], seat0_id, seat1_id,
            payoffs=result.payoffs, pot_before_rake=result.pot_before_rake,
            rake_taken=result.rake_taken, winner=result.winner,
            game_type="duel", went_to_showdown=result.ko,
            detail={"rounds_played": result.rounds_played, "final_hp": result.final_hp},
            currency=live["currency"], unit_value_cents=live["unit_value_cents"],
        )

    live["fights_played"] += 1
    if live["fights_played"] >= live["fights_requested"]:
        _finish_duel_match(live)
    else:
        _start_next_fight(live)


def _state_payload_duel(live: dict, side: str) -> dict:
    seat = _my_seat_duel(live, side)
    state = live["fight"].state_for(seat)
    state["fight_number"] = live["fight_number"]
    state["fights_played"] = live["fights_played"]
    state["fights_requested"] = live["fights_requested"]
    state["match_done"] = False
    return state


@app.route("/duel/matches", methods=["POST"])
def create_duel_match():
    bot, err = _require_bot()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    opponent_key = str(body.get("opponent", "")).strip()
    fights = int(body.get("fights", 1))
    rake_bps = int(body.get("rake_bps", DEFAULT_RAKE_BPS))
    stake = int(body.get("stake", DEFAULT_DUEL_STAKE))
    if not opponent_key:
        return jsonify(error="opponent is required (a baseline bot name, or another bot's id)"), 400
    if not (1 <= fights <= MAX_FIGHTS_PER_MATCH):
        return jsonify(error=f"fights must be between 1 and {MAX_FIGHTS_PER_MATCH}"), 400
    if not (1 <= stake <= MAX_DUEL_STAKE):
        return jsonify(error=f"stake must be between 1 and {MAX_DUEL_STAKE}"), 400
    currency, unit_value_cents, currency_error = _resolve_currency(body)
    if currency_error:
        return jsonify(error=currency_error), 400

    with _lock:
        try:
            with db.connect() as conn:
                opponent_bot_id, opponent_name, is_baseline = _resolve_opponent(conn, opponent_key, DUEL_BASELINE_BOT_FACTORIES)
                wager_error = _check_wager_policy(is_baseline, currency)
                if wager_error:
                    return jsonify(error=wager_error), 400
                match_id = db.create_match(conn, bot["id"], opponent_bot_id, fights, rake_bps, currency, unit_value_cents, game_type="duel")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        live = _new_live_duel_match(match_id, bot["id"], opponent_bot_id, opponent_name, is_baseline, fights, rake_bps, stake, currency, unit_value_cents)
        LIVE_DUEL_MATCHES[match_id] = live
        _start_next_fight(live)
        if not live["done"]:
            _persist_live_duel(live)

    if live["done"]:
        return jsonify(match_id=match_id, opponent=opponent_name, match_done=True, note="a side couldn't cover the entry stake; match ended immediately"), 201
    return jsonify(match_id=match_id, opponent=opponent_name, fights_requested=fights, rake_bps=rake_bps, stake=stake, currency=currency, unit_value_cents=unit_value_cents), 201


@app.route("/duel/matches/<int:match_id>/state", methods=["GET"])
def duel_match_state(match_id: int):
    bot, err = _require_bot()
    if err:
        return err
    with _lock:
        live = _get_live_duel(match_id)
        if live is None:
            return jsonify(error="unknown or already-archived match_id"), 404
        if bot["id"] not in (live["creator_bot_id"], live["opponent_bot_id"]):
            return jsonify(error="you are not a participant in this match"), 403
        if live["done"]:
            return jsonify(match_done=True, fights_played=live["fights_played"], fights_requested=live["fights_requested"])
        side = "creator" if bot["id"] == live["creator_bot_id"] else "opponent"
        return jsonify(_state_payload_duel(live, side))


@app.route("/duel/matches/<int:match_id>/action", methods=["POST"])
def duel_match_action(match_id: int):
    bot, err = _require_bot()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    move = body.get("move")

    with _lock:
        live = _get_live_duel(match_id)
        if live is None:
            return jsonify(error="unknown or already-archived match_id"), 404
        if live["done"]:
            return jsonify(error="match already finished"), 400
        if bot["id"] not in (live["creator_bot_id"], live["opponent_bot_id"]):
            return jsonify(error="you are not a participant in this match"), 403

        side = "creator" if bot["id"] == live["creator_bot_id"] else "opponent"
        fight = live["fight"]
        seat = _my_seat_duel(live, side)
        if seat in fight.pending_moves:
            return jsonify(error="you've already submitted a move this round -- waiting on your opponent"), 409
        try:
            fight.submit(seat, move)
        except DuelIllegalAction as exc:
            return jsonify(error=str(exc), legal_actions=fight.legal_actions(seat)), 400

        _autoplay_baseline_duel_moves(live)

        if fight.done:
            _settle_fight_and_maybe_advance(live)
            if live["done"]:
                return jsonify(fight_resolved=True, match_done=True, fights_played=live["fights_played"])
            _persist_live_duel(live)
            return jsonify(fight_resolved=True, match_done=False, state=_state_payload_duel(live, side))

        _persist_live_duel(live)
        round_resolved = seat not in fight.pending_moves  # true once both sides have moved
        return jsonify(fight_resolved=False, round_resolved=round_resolved, match_done=False, state=_state_payload_duel(live, side))


@app.route("/duel/matches/<int:match_id>", methods=["GET"])
def duel_match_summary(match_id: int):
    with db.connect() as conn:
        rows = db.match_history(conn, match_id)
    if not rows:
        live = _get_live_duel(match_id)
        if live is None:
            return jsonify(error="no such match"), 404
        return jsonify(
            match_id=match_id,
            fights_played=live["fights_played"],
            fights_requested=live["fights_requested"],
            done=live["done"],
        )
    return jsonify(match_id=match_id, fights=[dict(r) for r in rows])


@app.route("/duel/lobby/join", methods=["POST"])
def duel_lobby_join():
    """Same free-computer / wager-required-for-competitive split as
    /lobby/join (see its docstring): a practice_chips entry is matched
    against a baseline duel bot immediately, a usd entry queues for a real
    opponent and never falls back to the computer."""
    bot, err = _require_bot()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    fights = int(body.get("fights", 20))
    rake_bps = int(body.get("rake_bps", DEFAULT_RAKE_BPS))
    stake = int(body.get("stake", DEFAULT_DUEL_STAKE))
    fallback_after_seconds = float(body.get("fallback_after_seconds", LOBBY_DEFAULT_FALLBACK_SECONDS))
    if not (1 <= fights <= MAX_FIGHTS_PER_MATCH):
        return jsonify(error=f"fights must be between 1 and {MAX_FIGHTS_PER_MATCH}"), 400
    currency, unit_value_cents, currency_error = _resolve_currency(body)
    if currency_error:
        return jsonify(error=currency_error), 400

    with _lock:
        with db.connect() as conn:
            existing_match_id = db.find_active_match_for_bot(conn, bot["id"], game_type="duel")
            if existing_match_id:
                return jsonify(matched=True, match_id=existing_match_id)

            if currency == "practice_chips":
                opponent_bot_id, opponent_name, _ = _resolve_opponent(conn, LOBBY_DUEL_FALLBACK_BOT, DUEL_BASELINE_BOT_FACTORIES)
                match_id = db.create_match(conn, bot["id"], opponent_bot_id, fights, rake_bps, currency, unit_value_cents, game_type="duel")
            else:
                # Matched by stake too -- otherwise whichever bot's /join
                # call happens to complete the pairing would silently
                # decide the stake for BOTH sides, discarding the other
                # bot's own request.
                opponent_row = db.find_lobby_opponent(conn, bot["id"], fights, rake_bps, currency, unit_value_cents, game_type="duel", stake=stake)
                if opponent_row is None:
                    db.join_lobby(conn, bot["id"], fights, rake_bps, fallback_after_seconds, currency, unit_value_cents, game_type="duel", stake=stake)
                    return jsonify(matched=False, poll="/duel/lobby/status")

                db.leave_lobby(conn, opponent_row["bot_id"])
                opponent_bot_row = db.get_bot(conn, opponent_row["bot_id"])
                match_id = db.create_match(conn, bot["id"], opponent_row["bot_id"], fights, rake_bps, currency, unit_value_cents, game_type="duel")

        if currency == "practice_chips":
            live = _new_live_duel_match(match_id, bot["id"], opponent_bot_id, opponent_name, True, fights, rake_bps, stake, currency, unit_value_cents)
            LIVE_DUEL_MATCHES[match_id] = live
            _start_next_fight(live)
            if not live["done"]:
                _persist_live_duel(live)
            return jsonify(matched=True, match_id=match_id, opponent=opponent_name, note="practice_chips is free-to-play -- matched instantly against the computer; competitive bot-vs-bot play needs currency=\"usd\""), 201

        # opponent_row["stake"] == stake by construction (matched on it above).
        live = _new_live_duel_match(match_id, bot["id"], opponent_row["bot_id"], opponent_bot_row["name"], False, fights, rake_bps, stake, currency, unit_value_cents)
        LIVE_DUEL_MATCHES[match_id] = live
        _start_next_fight(live)
        if not live["done"]:
            _persist_live_duel(live)

    return jsonify(matched=True, match_id=match_id, opponent_bot_id=opponent_row["bot_id"], opponent=opponent_bot_row["name"]), 201


@app.route("/duel/lobby/status", methods=["GET"])
def duel_lobby_status():
    bot, err = _require_bot()
    if err:
        return err

    with _lock:
        with db.connect() as conn:
            existing_match_id = db.find_active_match_for_bot(conn, bot["id"], game_type="duel")
            if existing_match_id:
                return jsonify(matched=True, match_id=existing_match_id)

            entry = db.get_lobby_entry(conn, bot["id"])
            if entry is None or entry["game_type"] != "duel":
                return jsonify(matched=False, waiting=False, error="not in the duel lobby -- call /duel/lobby/join first")

            # Only usd (competitive) entries ever sit in the lobby now --
            # practice_chips ("play the computer") entries are matched
            # instantly at /duel/lobby/join and never reach here.
            return jsonify(matched=False, waiting=True, waiting_seconds=round(time.time() - entry["joined_at"], 1), note="usd lobby entries wait for a real opponent -- competitive play requires a real wager, so there's no auto-fallback to the computer")


@app.route("/duel/lobby/leave", methods=["POST"])
def duel_lobby_leave():
    bot, err = _require_bot()
    if err:
        return err
    with db.connect() as conn:
        db.leave_lobby(conn, bot["id"])
    return jsonify(left=True)


# ==========================================================================
# Phase 1 monetization: "beat the boss" challenges. See the constants
# block near the top of this file for the actual numbers and why.
# ==========================================================================

@app.route("/admin/prize-pool/fund", methods=["POST"])
def admin_fund_prize_pool():
    """An admin moving real value they've actually set aside into the
    prize pool -- the same honest-attestation pattern as /admin/bots/*
    /credit: this records money that was genuinely earmarked for prizes,
    it doesn't create any. Challenge wins can only ever pay out of what's
    actually been funded here (see resolve_boss_challenge)."""
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        amount_cents = int(body["amount_cents"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="amount_cents (integer, positive) is required"), 400
    note = str(body.get("note", "")).strip()
    if not note:
        return jsonify(error="note is required -- record what this funding corresponds to"), 400
    with db.connect() as conn:
        try:
            new_balance = db.fund_prize_pool(conn, amount_cents, note)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    return jsonify(prize_pool_balance_cents=new_balance, funded_cents=amount_cents), 200


@app.route("/prize-pool", methods=["GET"])
def prize_pool_status():
    """Public and deliberately so -- a challenger should be able to see
    the pool can actually cover the prize before paying an entry fee."""
    with db.connect() as conn:
        balance = db.prize_pool_balance(conn)
    return jsonify(balance_cents=balance)


@app.route("/challenges/boss", methods=["POST"])
def create_boss_challenge():
    """Pay a flat, non-refundable real-money entry fee for one shot at
    the hard bot for the chosen game (cfr_bot for leduc, boss_duel_bot
    for duel), over a fixed number of hands/fights. "Winning" means net
    chips positive summed across the WHOLE challenge, not just the final
    hand -- a long enough sample that the outcome is actually about
    skill, not a single lucky showdown. The challenge itself is played
    with ordinary practice chips: the entry fee is the only real money
    that changes hands going in, so a challenger's risk is capped at
    exactly what they agreed to pay, never more. Win, and the challenge
    pays out from the prize pool (see /prize-pool) -- lose, and the entry
    fee stays with the house, same as it would if you never win a
    fairground game."""
    bot, err = _require_bot()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    game_type = str(body.get("game_type", "")).strip().lower()
    if game_type not in BOSS_CHALLENGE_BOSS_NAME:
        return jsonify(error=f"game_type must be one of {sorted(BOSS_CHALLENGE_BOSS_NAME)}"), 400
    entry_fee_cents = int(body.get("entry_fee_cents", BOSS_CHALLENGE_ENTRY_FEE_DEFAULT_CENTS))
    if not (BOSS_CHALLENGE_ENTRY_FEE_MIN_CENTS <= entry_fee_cents <= BOSS_CHALLENGE_ENTRY_FEE_MAX_CENTS):
        return jsonify(error=f"entry_fee_cents must be between {BOSS_CHALLENGE_ENTRY_FEE_MIN_CENTS} and {BOSS_CHALLENGE_ENTRY_FEE_MAX_CENTS}"), 400

    boss_name = BOSS_CHALLENGE_BOSS_NAME[game_type]
    length = BOSS_CHALLENGE_LENGTH[game_type]
    prize_cents = entry_fee_cents * BOSS_CHALLENGE_PRIZE_MULTIPLIER

    with _lock:
        try:
            with db.connect() as conn:
                if bot["real_balance"] < entry_fee_cents:
                    return jsonify(error=f"insufficient real_balance ({bot['real_balance']} cents) to pay a {entry_fee_cents}-cent entry fee"), 400
                factories = BASELINE_BOT_FACTORIES if game_type == "leduc" else DUEL_BASELINE_BOT_FACTORIES
                opponent_bot_id, opponent_name, _ = _resolve_opponent(conn, boss_name, factories)
                db.pay_challenge_entry_fee(conn, bot["id"], entry_fee_cents)
                # The challenge is won or lost on net chips over the match,
                # never on whether the challenger's (or the boss's --
                # baseline bots share one account across every match ever
                # played against them, so their balance can in principle
                # run down over time too) unrelated practice balance
                # happened to be enough to start it -- see the docstring
                # on ensure_minimum_practice_balance.
                db.ensure_minimum_practice_balance(conn, bot["id"], BOSS_CHALLENGE_MIN_PRACTICE_BALANCE)
                db.ensure_minimum_practice_balance(conn, opponent_bot_id, BOSS_CHALLENGE_MIN_PRACTICE_BALANCE)
                match_id = db.create_match(conn, bot["id"], opponent_bot_id, length, BOSS_CHALLENGE_RAKE_BPS, "practice_chips", 1, game_type=game_type)
                challenge_id = db.create_boss_challenge(
                    conn, bot["id"], game_type, boss_name, match_id,
                    entry_fee_cents, prize_cents, win_condition="net_positive_over_challenge",
                )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        if game_type == "leduc":
            live = _new_live_match(match_id, bot["id"], opponent_bot_id, opponent_name, True, length, BOSS_CHALLENGE_RAKE_BPS)
            LIVE_MATCHES[match_id] = live
            _start_next_hand(live)
            if not live["done"]:
                _persist_live(live)
            play_at = f"/matches/{match_id}"
        else:
            live = _new_live_duel_match(match_id, bot["id"], opponent_bot_id, opponent_name, True, length, BOSS_CHALLENGE_RAKE_BPS, DEFAULT_DUEL_STAKE)
            LIVE_DUEL_MATCHES[match_id] = live
            _start_next_fight(live)
            if not live["done"]:
                _persist_live_duel(live)
            play_at = f"/duel/matches/{match_id}"

    return jsonify(
        challenge_id=challenge_id,
        match_id=match_id,
        boss=opponent_name,
        length=length,
        entry_fee_cents=entry_fee_cents,
        prize_cents=prize_cents,
        play_at=play_at,
        note="win condition is net chips positive summed over the whole challenge, not just the last hand",
    ), 201


@app.route("/challenges/<int:challenge_id>", methods=["GET"])
def challenge_status(challenge_id: int):
    """A challenge's own api_key owner can check it, and so can an admin
    -- same visibility rule as /bots/<id>/transactions."""
    provided_admin_secret = request.headers.get("X-Admin-Secret")
    configured_admin_secret = os.environ.get(ADMIN_SECRET_ENV_VAR)
    is_admin = bool(configured_admin_secret) and provided_admin_secret == configured_admin_secret
    with db.connect() as conn:
        challenge = db.get_boss_challenge(conn, challenge_id)
        if challenge is None:
            return jsonify(error="no such challenge"), 404
        if not is_admin:
            bot, err = _require_bot()
            if err:
                return err
            if bot["id"] != challenge["bot_id"]:
                return jsonify(error="not your challenge"), 403
    return jsonify(dict(challenge))


def create_app():
    db.init_db()
    with db.connect() as conn:
        _ensure_baseline_bots(conn, BASELINE_BOT_FACTORIES)
        _ensure_baseline_bots(conn, DUEL_BASELINE_BOT_FACTORIES)
    return app


if __name__ == "__main__":
    create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
