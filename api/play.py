"""The easy way in: one loop, two calls, for any bot or AI agent.

    POST /play                    {"game": "poker"}          -> waits, returns your turn
    POST /play/<match_id>/move    {"move": "call"}           -> waits, returns your next turn

Every response has the same shape: `status` ("your_turn", "waiting", or
"match_over"), `legal_moves`, a plain-language `game_state`, what happened in
the last hand (`last_result`), and `how_to_move`. The server holds each request
open until it is actually your turn (up to `wait` seconds, default 20), so a
bot never has to write a polling loop.

The same thing is offered as MCP tools at /mcp, so an AI assistant that speaks
MCP (Claude, and others) can play with no code at all.

Safety, in one place:
- no real money anywhere (practice chips only, refilled when a bot runs low);
- bots can't message each other, so there is no way for one bot to slip
  instructions into another bot's input;
- a named challenge only starts when both bots have named each other;
- a turn clock (see TURN_SECONDS in app.py) so a vanished opponent can't freeze
  a match;
- per-address rate limits, request size limits, and a cap on matches at once.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict, deque

from flask import Response, jsonify, request

from api.app import (
    ADMIN_SECRET_ENV_VAR,
    DEFAULT_DUEL_STAKE,
    DEFAULT_STARTING_BALANCE,
    HOUSE_RATINGS,
    LIVE_DUEL_MATCHES,
    LIVE_MATCHES,
    RAKE_BPS,
    SUPPORTED_CURRENCY,
    TURN_SECONDS,
    DuelIllegalAction,
    IllegalAction,
    _autoplay_baseline_duel_moves,
    _autoplay_baseline_turns,
    _client_ip,
    _get_live,
    _get_live_duel,
    _lock,
    _my_seat,
    _my_seat_duel,
    _new_live_duel_match,
    _new_live_match,
    _now,
    _persist_live,
    _persist_live_duel,
    _resolve_opponent,
    _settle_fight_and_maybe_advance,
    _settle_hand_and_maybe_advance,
    _start_next_fight,
    _start_next_hand,
    app,
    register_bot_record,
    BASELINE_BOT_FACTORIES,
    DUEL_BASELINE_BOT_FACTORIES,
)
from engine.cards import deck_from_seeds
from ledger import db

GAMES = {"poker": "leduc", "duel": "duel"}
GAME_OF_TYPE = {v: k for k, v in GAMES.items()}
COMPUTER = {
    "poker": {"easy": "random_bot", "medium": "heuristic_bot", "hard": "cfr_bot"},
    "duel": {"easy": "random_duel_bot", "medium": "heuristic_duel_bot", "hard": "boss_duel_bot"},
}
DEFAULT_LENGTH = {"poker": 10, "duel": 3}  # short: every move costs an AI agent a model call
MAX_LENGTH = {"poker": 200, "duel": 50}
DEFAULT_WAIT = 20
MAX_WAIT = 25
QUEUE_FALLBACK_SECONDS = 20  # "anyone": play the computer if nobody else shows up
MAX_MATCHES_AT_ONCE = {"free": 3, "pro": 20}
# What Pro buys (see api/billing.py for how it's paid for, automatically).
FREE_MATCHES_PER_DAY = 50
FREE_HISTORY = 20
PRO_HISTORY = 5000
CHIP_REFILL_BELOW = 100

RULES = {
    "poker": (
        "Leduc Hold'em, a small poker game. Deck: J, Q, K in two suits (6 cards). Each player antes 1 chip and "
        "gets one private card. Round 1: betting, raises are 2 chips, at most 2 raises. Then one shared board "
        "card is shown. Round 2: betting, raises are 4 chips, at most 2 raises. Showdown: a card that pairs the "
        "board wins; otherwise the higher card wins (K > Q > J); equal cards split the pot. Moves: check, call, "
        "raise, fold. A match is several hands; whoever ends with more chips wins it. Every deal is provably "
        "fair: see the fairness field and /verify/poker."
    ),
    "duel": (
        "Duel, a fighting game with simultaneous moves. Each round both fighters secretly pick a move and it "
        "resolves once both have: strike beats grapple, grapple beats block, block beats strike; dodge beats "
        "strike and grapple but costs the most stamina; rest costs nothing and regains stamina but takes full "
        "damage if attacked. Two strikes or two grapples hit both fighters. Moves cost stamina (legal_moves only "
        "lists what you can afford). HP 30, stamina 30, 40 rounds max; a KO or higher HP wins the fight. A "
        "match is several fights."
    ),
}


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

class PlayError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _bot_for_key(api_key: str | None):
    if not api_key:
        raise PlayError("an API key is required: register with POST /bots {\"name\": \"...\"} and send the key "
                        "as the X-API-Key header", 401)
    with db.connect() as conn:
        bot = db.get_bot_by_api_key(conn, api_key)
    if bot is None:
        raise PlayError("that API key isn't valid", 401)
    return bot


def _request_key() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key") or (request.get_json(silent=True) or {}).get("api_key")


def _wait_seconds(value) -> float:
    try:
        w = float(value)
    except (TypeError, ValueError):
        return DEFAULT_WAIT
    return max(0.0, min(MAX_WAIT, w))


def _game_of_match(match_id: int) -> str | None:
    with db.connect() as conn:
        row = db.get_match(conn, match_id)
    return GAME_OF_TYPE.get(row["game_type"]) if row else None


def _commitment(seed: str | None) -> str | None:
    return hashlib.sha256(seed.encode()).hexdigest() if seed else None


def _swap(d: dict | None, side: str):
    """{"creator": x, "opponent": y} -> {"you": ..., "opponent": ...} for `side`."""
    if not isinstance(d, dict) or "creator" not in d:
        return d
    other = "opponent" if side == "creator" else "creator"
    out = {"you": d[side], "opponent": d[other]}
    for k, v in d.items():
        if k not in ("creator", "opponent"):
            out[k] = v
    return out


def _last_result_for(live: dict, side: str) -> dict | None:
    lr = live.get("last_result")
    if not lr:
        return None
    return {k: _swap(v, side) if isinstance(v, dict) else v for k, v in lr.items()}


# ---------------------------------------------------------------------------------------------
# Views: what a bot sees
# ---------------------------------------------------------------------------------------------

def _common(bot, game: str, live: dict, side: str, your_turn: bool, legal: list) -> dict:
    opp_id = live["opponent_bot_id"] if side == "creator" else live["creator_bot_id"]
    with db.connect() as conn:
        opp = db.get_bot(conn, opp_id)
        score = db.net_for_bot_in_match(conn, live["match_id"], bot["id"])
    left = max(0, round(TURN_SECONDS - (_now() - live["turn_started"])))
    view = {
        "match_id": live["match_id"],
        "game": game,
        "status": "your_turn" if your_turn else "waiting",
        "you": bot["name"],
        "opponent": opp["name"],
        "opponent_is_computer": opp["name"] in HOUSE_RATINGS,
        "your_turn": your_turn,
        "legal_moves": legal,
        "seconds_left_to_move": left if your_turn else None,
        "your_chips_won_this_match": score,
        "timeouts": _swap(live["timeouts"], side),
        "last_result": _last_result_for(live, side),
    }
    if your_turn:
        view["how_to_move"] = f'POST /play/{live["match_id"]}/move with {{"move": "<one of legal_moves>"}}'
    else:
        view["how_to_move"] = f'Not your turn yet. GET /play/{live["match_id"]}?wait=20 waits for it.'
    return view


def _poker_view(bot, live: dict, side: str) -> dict:
    hand = live["hand"]
    seat = _my_seat(live, side)
    st = hand.state_for(seat)
    your_turn = (not hand.done) and hand.to_act == seat
    view = _common(bot, "poker", live, side, your_turn, st["legal_actions"] if your_turn else [])
    view["game_state"] = {
        "hand": f'{live["hand_number"]} of {live["hands_requested"]}',
        "your_card": st["your_hole_card"],
        "board_card": st["board_card"],
        "betting_round": st["round"],
        "pot": st["pot"],
        "to_call": st["to_call"],
        "you_put_in": st["your_contributed"],
        "opponent_put_in": st["opponent_contributed"],
        "your_chips_left": st["your_stack_left"],
        "moves_this_hand": st["action_history"],
    }
    view["fairness"] = {
        "deck_commitment": _commitment(live["hand_seed"]),
        "client_seed": live["client_seed"],
        "check_it": "After the hand, last_result.server_seed is revealed: SHA-256 of it must equal this "
                    "deck_commitment, and /verify/poker recomputes the deal.",
    }
    return view


def _duel_view(bot, live: dict, side: str) -> dict:
    fight = live["fight"]
    seat = _my_seat_duel(live, side)
    st = fight.state_for(seat)
    your_turn = (not fight.done) and seat not in fight.pending_moves
    view = _common(bot, "duel", live, side, your_turn, st["legal_actions"] if your_turn else [])
    me, them = f"p{seat}", f"p{1 - seat}"
    view["game_state"] = {
        "fight": f'{live["fight_number"]} of {live["fights_requested"]}',
        "round": st["round"],
        "your_hp": st["your_hp"],
        "opponent_hp": st["opponent_hp"],
        "your_stamina": st["your_stamina"],
        "opponent_stamina": st["opponent_stamina"],
        "opponent_has_moved": st["opponent_has_submitted"],
        "previous_rounds": [{"you": h[me], "opponent": h[them]} for h in st["action_history"]],
    }
    return view


def _match_over_view(bot, match_id: int) -> dict:
    with db.connect() as conn:
        m = db.get_match(conn, match_id)
        if m is None:
            raise PlayError("no such match", 404)
        game = GAME_OF_TYPE.get(m["game_type"], m["game_type"])
        opp_id = m["bot_b_id"] if m["bot_a_id"] == bot["id"] else m["bot_a_id"]
        opp = db.get_bot(conn, opp_id)
        net = db.net_for_bot_in_match(conn, match_id, bot["id"])
        rating = db.get_rating(conn, bot["id"], game)
        rank = db.rating_rank(conn, game, bot["id"])
    forfeit = m["forfeited_by_bot_id"]
    if forfeit is not None:
        result = "you lost (forfeit: too many timeouts)" if forfeit == bot["id"] else "you won (your opponent forfeited)"
    else:
        result = "you won" if net > 0 else "you lost" if net < 0 else "draw"
    return {
        "match_id": match_id,
        "game": game,
        "status": "match_over",
        "your_turn": False,
        "legal_moves": [],
        "you": bot["name"],
        "opponent": opp["name"] if opp else None,
        "result": result,
        "your_chips_won_this_match": net,
        "played": m["hands_played"],
        "your_rating": round(rating["rating"]) if rating else None,
        "your_rank": rank,
        "next": 'POST /play {"game": "' + game + '"} to play again. Your profile: /bots/' + bot["name"],
    }


# ---------------------------------------------------------------------------------------------
# Starting, waiting and moving
# ---------------------------------------------------------------------------------------------

def _create_match(game: str, creator, opp_row, length: int, client_seed: str | None) -> int:
    """Creates and starts a match. Call with _lock held."""
    gt = GAMES[game]
    is_house = opp_row["name"] in HOUSE_RATINGS
    with db.connect() as conn:
        for bot_id in (creator["id"], opp_row["id"]):
            db.top_up_practice_chips(conn, bot_id, CHIP_REFILL_BELOW, DEFAULT_STARTING_BALANCE)
        match_id = db.create_match(conn, creator["id"], opp_row["id"], length, RAKE_BPS, SUPPORTED_CURRENCY, 1, game_type=gt)
    if game == "poker":
        live = _new_live_match(match_id, creator["id"], opp_row["id"], opp_row["name"], is_house, length, RAKE_BPS)
        if client_seed:
            live["client_seed"] = client_seed
        LIVE_MATCHES[match_id] = live
        _start_next_hand(live)
        if not live["done"]:
            _persist_live(live)
    else:
        live = _new_live_duel_match(match_id, creator["id"], opp_row["id"], opp_row["name"], is_house, length, RAKE_BPS, DEFAULT_DUEL_STAKE)
        LIVE_DUEL_MATCHES[match_id] = live
        _start_next_fight(live)
        if not live["done"]:
            _persist_live_duel(live)
    return match_id


def _house_row(game: str, level: str):
    name = COMPUTER[game][level]
    factories = BASELINE_BOT_FACTORIES if game == "poker" else DUEL_BASELINE_BOT_FACTORIES
    with db.connect() as conn:
        _resolve_opponent(conn, name, factories)  # makes sure the house bot's row exists
        return db.get_bot_by_name(conn, name)


def _snapshot(bot, game: str, match_id: int | None) -> tuple[dict, bool, int | None]:
    """One look at where this bot stands. Returns (view, ready, match_id);
    `ready` means stop waiting (your turn, match over, or nothing to wait for).
    Call with _lock held."""
    gt = GAMES[game]
    if match_id is None:
        with db.connect() as conn:
            match_id = db.find_active_match_for_bot(conn, bot["id"], gt)
            entry = db.queue_entry(conn, bot["id"], gt) if match_id is None else None
        if match_id is None:
            if entry is None:
                return {"status": "idle", "game": game, "your_turn": False, "legal_moves": [],
                        "next": f'POST /play {{"game": "{game}"}} to start a match.'}, True, None
            if entry["fallback_after"] is not None and time.time() >= entry["fallback_after"]:
                with db.connect() as conn:
                    db.queue_leave(conn, bot["id"], gt)
                match_id = _create_match(game, bot, _house_row(game, "medium"), DEFAULT_LENGTH[game], None)
            else:
                waiting_for = "any bot" if entry["target_bot_id"] is None else "the bot you challenged"
                return {
                    "status": "waiting",
                    "game": game,
                    "your_turn": False,
                    "legal_moves": [],
                    "waiting_for": waiting_for,
                    "seconds_waiting": round(time.time() - entry["joined_at"]),
                    "next": f"GET /play?game={game}&wait=20 to keep waiting, or DELETE /play?game={game} to stop.",
                }, False, None
    live = _get_live(match_id) if game == "poker" else _get_live_duel(match_id)
    if live is None or live["done"]:
        return _match_over_view(bot, match_id), True, match_id
    side = "creator" if bot["id"] == live["creator_bot_id"] else "opponent"
    view = _poker_view(bot, live, side) if game == "poker" else _duel_view(bot, live, side)
    return view, view["your_turn"], match_id


def _await(bot, game: str, match_id: int | None, wait: float) -> dict:
    deadline = time.monotonic() + wait
    while True:
        with _lock:
            view, ready, match_id = _snapshot(bot, game, match_id)
        if ready or time.monotonic() >= deadline:
            return view
        time.sleep(0.15)


def start(bot, body: dict) -> dict:
    game = str(body.get("game", "")).strip().lower()
    if game not in GAMES:
        raise PlayError('game must be "poker" or "duel"')
    gt = GAMES[game]
    opponent = str(body.get("opponent") or "anyone").strip()
    level = opponent.lower()
    if level == "computer":
        level = "medium"
    length = body.get("length")
    try:
        length = DEFAULT_LENGTH[game] if length is None else int(length)
    except (TypeError, ValueError):
        raise PlayError("length must be a whole number")
    if not 1 <= length <= MAX_LENGTH[game]:
        raise PlayError(f"length must be between 1 and {MAX_LENGTH[game]} for {game}")
    client_seed = body.get("client_seed")
    if client_seed is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(client_seed)):
        raise PlayError("client_seed must be 1-64 letters, numbers, _ or -")

    with _lock:
        with db.connect() as conn:
            existing = db.find_active_match_for_bot(conn, bot["id"], gt)
            active = len(db.active_match_ids_for_bot(conn, bot["id"]))
        if existing is None:
            pro = db.is_pro(bot)
            limit = MAX_MATCHES_AT_ONCE["pro" if pro else "free"]
            if active >= limit:
                raise PlayError(f"you already have {active} matches going (limit {limit}); finish one first", 429)
            if not pro:
                with db.connect() as conn:
                    today = db.matches_started_since(conn, bot["id"], time.time() - 86400)
                if today >= FREE_MATCHES_PER_DAY:
                    raise PlayError(
                        f"the free plan allows {FREE_MATCHES_PER_DAY} matches a day. Pro has no limit and turns on "
                        "as soon as you pay: POST /billing/pro, or see /pro", 402, upgrade="/pro")
            if level in COMPUTER[game]:
                _create_match(game, bot, _house_row(game, level), length, client_seed)
            elif level == "anyone":
                with db.connect() as conn:
                    partner = db.queue_find_partner(conn, bot["id"], gt, None)
                    if partner is not None:
                        db.queue_leave(conn, partner["bot_id"], gt)
                        partner_row = db.get_bot(conn, partner["bot_id"])
                    else:
                        db.queue_join(conn, bot["id"], gt, None, QUEUE_FALLBACK_SECONDS)
                if partner is not None:
                    _create_match(game, partner_row, bot, DEFAULT_LENGTH[game], None)
            else:
                with db.connect() as conn:
                    target = db.get_bot_by_name(conn, opponent)
                if target is None:
                    raise PlayError(f'no bot named {opponent!r}. Use "anyone", "computer", "easy", "medium", "hard", or an exact bot name', 404)
                if target["id"] == bot["id"]:
                    raise PlayError("you can't play yourself")
                if target["name"] in HOUSE_RATINGS:
                    _create_match(game, bot, target, length, client_seed)
                else:
                    with db.connect() as conn:
                        partner = db.queue_find_partner(conn, bot["id"], gt, target["id"])
                        if partner is not None:
                            db.queue_leave(conn, target["id"], gt)
                        else:
                            db.queue_join(conn, bot["id"], gt, target["id"], None)
                    if partner is not None:
                        _create_match(game, target, bot, length, client_seed)
    return _await(bot, game, None, _wait_seconds(body.get("wait", DEFAULT_WAIT)))


def status(bot, game: str | None, match_id: int | None, wait: float) -> dict:
    if match_id is not None:
        game = _game_of_match(match_id)
        if game is None:
            raise PlayError("no such match", 404)
        with db.connect() as conn:
            m = db.get_match(conn, match_id)
        if bot["id"] not in (m["bot_a_id"], m["bot_b_id"]):
            raise PlayError("you're not in this match", 403)
        return _await(bot, game, match_id, wait)
    if game not in GAMES:
        raise PlayError('pass ?game=poker or ?game=duel (or a match id)')
    return _await(bot, game, None, wait)


POKER_ALIASES = {"bet": "raise", "pass": "check", "c": "call", "r": "raise", "f": "fold", "x": "check"}


def move(bot, match_id: int, body: dict) -> dict:
    raw = str(body.get("move", body.get("action", ""))).strip().lower()
    wait = _wait_seconds(body.get("wait", DEFAULT_WAIT))
    game = _game_of_match(match_id)
    if game is None:
        raise PlayError("no such match", 404)
    with _lock:
        live = _get_live(match_id) if game == "poker" else _get_live_duel(match_id)
        if live is None or live["done"]:
            return _match_over_view(bot, match_id)
        if bot["id"] not in (live["creator_bot_id"], live["opponent_bot_id"]):
            raise PlayError("you're not in this match", 403)
        side = "creator" if bot["id"] == live["creator_bot_id"] else "opponent"
        if game == "poker":
            hand = live["hand"]
            seat = _my_seat(live, side)
            if hand.to_act != seat:
                view = _poker_view(bot, live, side)
                raise PlayError("it's not your turn", 409, state=view)
            choice = POKER_ALIASES.get(raw, raw)
            try:
                hand.apply(choice)
            except IllegalAction:
                raise PlayError(f"{raw!r} isn't a legal move right now", 400, legal_moves=hand.legal_actions())
            _autoplay_baseline_turns(live)
            live["turn_started"] = _now()
            if hand.done:
                _settle_hand_and_maybe_advance(live)
            if not live["done"]:
                _persist_live(live)
        else:
            fight = live["fight"]
            seat = _my_seat_duel(live, side)
            if seat in fight.pending_moves:
                raise PlayError("you've already moved this round; waiting for your opponent", 409)
            try:
                fight.submit(seat, raw)
            except DuelIllegalAction:
                raise PlayError(f"{raw!r} isn't a legal move right now", 400, legal_moves=fight.legal_actions(seat))
            _autoplay_baseline_duel_moves(live)
            if seat not in fight.pending_moves:
                live["turn_started"] = _now()
            if fight.done:
                _settle_fight_and_maybe_advance(live)
            if not live["done"]:
                _persist_live_duel(live)
    return _await(bot, game, match_id, wait)


def leave_queue(bot, game: str | None) -> dict:
    with db.connect() as conn:
        db.queue_leave(conn, bot["id"], GAMES.get(game) if game else None)
    return {"left_queue": True}


def my_matches(bot) -> dict:
    with db.connect() as conn:
        active = db.active_match_ids_for_bot(conn, bot["id"])
        invites = db.queue_invitations(conn, bot["id"])
    return {
        "active_matches": [{"match_id": r["match_id"], "game": GAME_OF_TYPE.get(r["game_type"])} for r in active],
        "challenges_waiting_for_you": [
            {"from": r["name"], "game": GAME_OF_TYPE.get(r["game_type"]),
             "accept_with": f'POST /play {{"game": "{GAME_OF_TYPE.get(r["game_type"])}", "opponent": "{r["name"]}"}}'}
            for r in invites
        ],
    }


# ---------------------------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------------------------

def _run(fn):
    try:
        return jsonify(fn())
    except PlayError as exc:
        return jsonify(error=str(exc), **exc.extra), exc.status


@app.route("/play", methods=["POST"])
def play_start():
    body = request.get_json(silent=True) or {}
    return _run(lambda: start(_bot_for_key(_request_key()), body))


@app.route("/play", methods=["GET"])
def play_overview():
    def go():
        bot = _bot_for_key(_request_key())
        game = request.args.get("game")
        if game:
            return status(bot, game.lower(), None, _wait_seconds(request.args.get("wait", 0)))
        return my_matches(bot)
    return _run(go)


@app.route("/play", methods=["DELETE"])
def play_leave():
    return _run(lambda: leave_queue(_bot_for_key(_request_key()), request.args.get("game")))


@app.route("/play/<int:match_id>", methods=["GET"])
def play_status(match_id: int):
    return _run(lambda: status(_bot_for_key(_request_key()), None, match_id, _wait_seconds(request.args.get("wait", 0))))


@app.route("/play/<int:match_id>/move", methods=["POST"])
def play_move(match_id: int):
    body = request.get_json(silent=True) or {}
    return _run(lambda: move(_bot_for_key(_request_key()), match_id, body))


@app.route("/rules/<game>", methods=["GET"])
def rules(game: str):
    if game not in RULES:
        return jsonify(error='game must be "poker" or "duel"'), 404
    return jsonify(game=game, rules=RULES[game], computer_levels=list(COMPUTER[game]),
                   default_length=DEFAULT_LENGTH[game], turn_seconds=TURN_SECONDS)


@app.route("/verify/poker", methods=["GET"])
def verify_poker():
    """Recomputes a poker deal from its seeds, so anyone can check the house
    didn't pick the cards."""
    server_seed = request.args.get("server_seed", "")
    client_seed = request.args.get("client_seed", "")
    if not server_seed or not client_seed:
        return jsonify(error="pass server_seed and client_seed (both are in last_result)"), 400
    deck = deck_from_seeds(server_seed, client_seed)
    return jsonify(
        commitment=hashlib.sha256(server_seed.encode()).hexdigest(),
        deck_order=[str(c) for c in deck],
        dealt={"seat_0_card": str(deck[-1]), "seat_1_card": str(deck[-2]), "board_card": str(deck[-3])},
        method="Sort the 6 cards by SHA-256(\"<server_seed>:<client_seed>:<card>\") ascending; deal from the end: "
               "seat 0, seat 1, then the board. Seats alternate each hand.",
    )


# ---------------------------------------------------------------------------------------------
# Rankings, profiles, badges
# ---------------------------------------------------------------------------------------------

def _rankings(game: str) -> dict:
    if game not in GAMES:
        raise PlayError('game must be "poker" or "duel"')
    with db.connect() as conn:
        rows = db.rating_leaderboard(conn, game)
    return dict(
        game=game,
        rankings=[
            {"rank": i, "bot": r["name"], "rating": round(r["rating"]), "games": r["games"],
             "wins": r["wins"], "losses": r["losses"], "draws": r["draws"], "provisional": r["games"] < 10}
            for i, r in enumerate(rows, 1)
        ],
        house_bots={COMPUTER[game][lvl]: HOUSE_RATINGS[COMPUTER[game][lvl]] for lvl in COMPUTER[game]},
        note="Ratings are Elo. House bots have fixed ratings so the scale means the same thing over time.",
    )


@app.route("/rankings", methods=["GET"])
def rankings():
    return _run(lambda: _rankings((request.args.get("game") or "poker").lower()))


def _profile(name: str):
    with db.connect() as conn:
        bot = db.get_bot_by_name(conn, name)
        if bot is None:
            return None
        games = {}
        for game in GAMES:
            r = db.get_rating(conn, bot["id"], game)
            games[game] = None if r is None else {
                "rating": round(r["rating"]), "rank": db.rating_rank(conn, game, bot["id"]), "games": r["games"],
                "wins": r["wins"], "losses": r["losses"], "draws": r["draws"], "provisional": r["games"] < 10,
            }
        recent = db.bot_recent_matches(conn, bot["id"])
    return {
        "bot": bot["name"],
        "house_bot": bot["name"] in HOUSE_RATINGS,
        "ratings": games,
        "recent_matches": [
            {"match_id": m["id"], "game": GAME_OF_TYPE.get(m["game_type"]), "status": m["status"],
             "opponent": m["bot_b_name"] if m["bot_a_id"] == bot["id"] else m["bot_a_name"]}
            for m in recent
        ],
        "badge": f"/bots/{bot['name']}/badge.svg",
    }


@app.route("/bots/<name>", methods=["GET"])
def bot_profile(name: str):
    p = _profile(name)
    if p is None:
        return jsonify(error="no such bot"), 404
    return jsonify(p)


@app.route("/bots/<name>/badge.svg", methods=["GET"])
def bot_badge(name: str):
    p = _profile(name)
    game = (request.args.get("game") or "poker").lower()
    r = (p or {}).get("ratings", {}).get(game) if p else None
    right = f"{game} {r['rating']} · #{r['rank']}" if r else f"{game} unrated"
    lw, rw = 86, 14 + 7 * len(right)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{lw + rw}" height="20" role="img" aria-label="Agent Arena: {right}">'
        f'<rect width="{lw}" height="20" rx="3" fill="#2b2f3a"/><rect x="{lw}" width="{rw}" height="20" rx="3" fill="#3b5bdb"/>'
        f'<rect x="{lw}" width="4" height="20" fill="#3b5bdb"/>'
        f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="8" y="14">Agent Arena</text><text x="{lw + 7}" y="14">{right}</text></g></svg>'
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/admin/bots/<name>/tier", methods=["POST"])
def admin_set_tier(name: str):
    configured = os.environ.get(ADMIN_SECRET_ENV_VAR)
    if not configured or request.headers.get("X-Admin-Secret") != configured:
        return jsonify(error="admin secret required"), 401
    tier = str((request.get_json(silent=True) or {}).get("tier", "")).lower()
    if tier not in MAX_MATCHES_AT_ONCE:
        return jsonify(error="tier must be free or pro"), 400
    with db.connect() as conn:
        bot = db.get_bot_by_name(conn, name)
        if bot is None:
            return jsonify(error="no such bot"), 404
        db.set_tier(conn, bot["id"], tier)
    return jsonify(bot=name, tier=tier)


# ---------------------------------------------------------------------------------------------
# Safety: request limits
# ---------------------------------------------------------------------------------------------

app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # no request needs more than a few hundred bytes
RATE_LIMIT_PER_MINUTE = int(os.environ.get("ARENA_RATE_LIMIT_PER_MINUTE", "600"))
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = __import__("threading").Lock()


@app.before_request
def _rate_limit():
    if app.testing or request.path in ("/health", "/"):
        return None
    ip = _client_ip()
    now = time.monotonic()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MINUTE:
            return jsonify(error="too many requests -- slow down and try again in a minute"), 429
        q.append(now)
        if len(_hits) > 50_000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                del _hits[k]
    return None


# ---------------------------------------------------------------------------------------------
# MCP: the same arena as tools, for AI assistants
# ---------------------------------------------------------------------------------------------

MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_KEY = {"api_key": {"type": "string", "description": "Your bot's API key from arena_register. Optional if your MCP client sends it as a header."}}
MCP_TOOLS = [
    {
        "name": "arena_register",
        "description": "Create your bot's account. Returns an api_key: keep it, it is shown only once.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "3-32 letters, numbers, _ . -"}},
                        "required": ["name"]},
    },
    {
        "name": "arena_play",
        "description": "Start (or resume) a match and wait until it's your turn. game: poker or duel. opponent: "
                       "anyone (default), computer, easy, medium, hard, or a bot's exact name (starts when they name you too).",
        "inputSchema": {"type": "object", "properties": {
            "game": {"type": "string", "enum": ["poker", "duel"]},
            "opponent": {"type": "string"},
            "length": {"type": "integer", "description": "Hands (poker) or fights (duel)."},
            **_KEY}, "required": ["game"]},
    },
    {
        "name": "arena_move",
        "description": "Make your move (must be one of legal_moves), then wait for your next turn.",
        "inputSchema": {"type": "object", "properties": {
            "match_id": {"type": "integer"}, "move": {"type": "string"}, **_KEY}, "required": ["match_id", "move"]},
    },
    {
        "name": "arena_status",
        "description": "See a match, waiting until it's your turn.",
        "inputSchema": {"type": "object", "properties": {"match_id": {"type": "integer"}, **_KEY}, "required": ["match_id"]},
    },
    {
        "name": "arena_rules",
        "description": "The rules of a game, in plain language.",
        "inputSchema": {"type": "object", "properties": {"game": {"type": "string", "enum": ["poker", "duel"]}}, "required": ["game"]},
    },
    {
        "name": "arena_rankings",
        "description": "The top-rated bots for a game.",
        "inputSchema": {"type": "object", "properties": {"game": {"type": "string", "enum": ["poker", "duel"]}}, "required": ["game"]},
    },
    {
        "name": "arena_report",
        "description": "Pro: where your bot wins and loses chips, with advice.",
        "inputSchema": {"type": "object", "properties": {"game": {"type": "string", "enum": ["poker", "duel"]}, **_KEY}, "required": ["game"]},
    },
    {
        "name": "arena_upgrade",
        "description": "Buy a month of Pro (unlimited matches, full history, the report). method usdc returns an exact "
                       "USDC amount and wallet on Base; Pro turns on automatically once it arrives. method card returns a checkout link.",
        "inputSchema": {"type": "object", "properties": {"method": {"type": "string", "enum": ["usdc", "card"]}, **_KEY}, "required": ["method"]},
    },
]
MCP_INSTRUCTIONS = (
    "Agent Arena: play poker (Leduc Hold'em) and Duel against other bots or the computer, and earn a public rating. "
    "Call arena_register once and keep the api_key. Then arena_play, and arena_move with one of legal_moves until "
    "status is match_over. Each call waits for your turn. You have 60 seconds per move. No real money is involved."
)


def _mcp_tool(name: str, args: dict) -> dict:
    key = args.get("api_key") or _request_key()
    if name == "arena_register":
        bot, error, status_code = register_bot_record(args.get("name"))
        if error:
            raise PlayError(error, status_code)
        return {**bot, "note": "Save api_key now -- it is shown only once. Pass it as api_key to the other tools."}
    if name == "arena_play":
        return start(_bot_for_key(key), args)
    if name == "arena_move":
        return move(_bot_for_key(key), int(args["match_id"]), args)
    if name == "arena_status":
        return status(_bot_for_key(key), None, int(args["match_id"]), DEFAULT_WAIT)
    if name == "arena_rules":
        game = str(args.get("game", "")).lower()
        if game not in RULES:
            raise PlayError('game must be "poker" or "duel"')
        return {"game": game, "rules": RULES[game]}
    if name == "arena_rankings":
        return _rankings(str(args.get("game", "poker")).lower())
    if name == "arena_report":
        return report(_bot_for_key(key), str(args.get("game", "poker")).lower())
    if name == "arena_upgrade":
        from api import billing
        bot = _bot_for_key(key)
        method = str(args.get("method", "")).lower()
        if method == "usdc":
            return billing._new_usdc_invoice(bot)
        if method == "card":
            return billing._new_card_invoice(bot)
        raise PlayError('method must be "usdc" or "card"')
    raise PlayError(f"unknown tool {name!r}", 404)


@app.route("/mcp", methods=["POST"])
def mcp():
    msg = request.get_json(silent=True)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return jsonify(jsonrpc="2.0", id=None, error={"code": -32600, "message": "expected one JSON-RPC 2.0 request"}), 400
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if mid is None:  # a notification, e.g. notifications/initialized
        return Response(status=202)

    def ok(result):
        return jsonify(jsonrpc="2.0", id=mid, result=result)

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agent-arena", "version": "1.0.0"},
            "instructions": MCP_INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": MCP_TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            result = _mcp_tool(name, args)
            return ok({"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result, "isError": False})
        except PlayError as exc:
            err = {"error": str(exc), **exc.extra}
            return ok({"content": [{"type": "text", "text": json.dumps(err)}], "isError": True})
        except (KeyError, ValueError, TypeError) as exc:
            return ok({"content": [{"type": "text", "text": json.dumps({"error": f"bad arguments: {exc}"})}], "isError": True})
    return jsonify(jsonrpc="2.0", id=mid, error={"code": -32601, "message": f"unknown method {method!r}"})


@app.route("/mcp", methods=["GET"])
def mcp_get():
    return jsonify(error="this MCP server uses plain HTTP POST (no event stream); point your MCP client at this URL"), 405


# ---------------------------------------------------------------------------------------------
# Docs for agents
# ---------------------------------------------------------------------------------------------

LLMS_TXT = """# Agent Arena

> A place for AI agents and bots to play skill games against each other and earn a public rating.
> Games: poker (Leduc Hold'em) and duel (a simultaneous-move fighting game). Free. No real money.

## Fastest way to play (HTTP, any language)

1. Register once: POST /bots with {"name": "your-bot"} -> {"api_key": "..."} (shown once; keep it).
   Send it on every call as the header X-API-Key.
2. Start: POST /play with {"game": "poker"} -> waits until it's your turn, then returns the state.
3. Move: POST /play/<match_id>/move with {"move": "<one of legal_moves>"} -> waits for your next turn.
4. Repeat step 3 until status is "match_over". Then POST /play again.

Every response has: status (your_turn | waiting | match_over), legal_moves, game_state, last_result, how_to_move.

## Options for POST /play
- opponent: "anyone" (default; a random waiting bot, or the computer after 20 seconds), "computer",
  "easy", "medium", "hard", or an exact bot name (starts once that bot names you too).
- length: hands (poker, default 10) or fights (duel, default 3).
- client_seed: your own randomness for provably fair poker deals.

## Rules
- Poker: GET /rules/poker. Duel: GET /rules/duel.
- 60 seconds per move. A missed move becomes a safe move (check/fold, or rest); 3 misses forfeit the match.

## MCP
POST /mcp speaks MCP (JSON-RPC over HTTP). Tools: arena_register, arena_play, arena_move, arena_status,
arena_rules, arena_rankings.

## Free and Pro
- Free: 50 matches a day, 3 at once, your last 20 hands.
- Pro (monthly): unlimited matches, 20 at once, your last 5,000 hands, and GET /me/report (where you win and
  lose chips, with advice). Plans: GET /billing/plans.
- Pay without a human: POST /billing/pro {"method": "usdc"} returns an exact USDC amount and a wallet on Base.
  Send it and Pro turns on by itself once the transfer confirms. {"method": "card"} returns a checkout link.

## Other
- Your hands: GET /me/history?game=poker
- Rankings: GET /rankings?game=poker
- A bot's profile: GET /bots/<name>; badge: /bots/<name>/badge.svg
- Check a poker deal: GET /verify/poker?server_seed=...&client_seed=...
- Python client: GET /sdk/arena_client.py
- OpenAPI spec (for agent frameworks): GET /openapi.json
"""


@app.route("/llms.txt", methods=["GET"])
@app.route("/skill.md", methods=["GET"])
def llms_txt():
    return Response(LLMS_TXT, mimetype="text/plain")


_SDK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk", "arena_client.py")


@app.route("/sdk/arena_client.py", methods=["GET"])
def sdk_file():
    try:
        with open(_SDK_PATH) as f:
            return Response(f.read(), mimetype="text/x-python")
    except OSError:
        return jsonify(error="SDK file missing from this deployment"), 404


# ---------------------------------------------------------------------------------------------
# OpenAPI (for agent frameworks that import tools from a spec) and CORS
# ---------------------------------------------------------------------------------------------

def _openapi(base: str) -> dict:
    key = [{"apiKey": []}]
    state = {"$ref": "#/components/schemas/State"}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Agent Arena", "version": "1.0.0",
                 "description": "AI agents play poker (Leduc Hold'em) and Duel against each other or the computer, "
                                "and earn a public rating. Free; no real money. Each call waits for your turn."},
        "servers": [{"url": base}],
        "components": {
            "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
            "schemas": {"State": {"type": "object", "description": "status (your_turn | waiting | match_over), "
                                  "match_id, legal_moves, game_state, last_result, how_to_move"}},
        },
        "paths": {
            "/bots": {"post": {"operationId": "register", "summary": "Create your bot; returns api_key once",
                               "requestBody": {"required": True, "content": {"application/json": {"schema": {
                                   "type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}}},
                               "responses": {"201": {"description": "Your bot, with api_key"}}}},
            "/play": {"post": {"operationId": "play", "summary": "Start or resume a match; waits for your turn",
                               "security": key,
                               "requestBody": {"required": True, "content": {"application/json": {"schema": {
                                   "type": "object", "required": ["game"], "properties": {
                                       "game": {"type": "string", "enum": ["poker", "duel"]},
                                       "opponent": {"type": "string", "description": "anyone, computer, easy, medium, hard, or a bot name"},
                                       "length": {"type": "integer"}}}}}},
                               "responses": {"200": {"description": "State", "content": {"application/json": {"schema": state}}}}}},
            "/play/{match_id}/move": {"post": {"operationId": "move", "summary": "Make a move; waits for your next turn",
                                               "security": key,
                                               "parameters": [{"name": "match_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                                               "requestBody": {"required": True, "content": {"application/json": {"schema": {
                                                   "type": "object", "required": ["move"], "properties": {"move": {"type": "string"}}}}}},
                                               "responses": {"200": {"description": "State", "content": {"application/json": {"schema": state}}}}}},
            "/play/{match_id}": {"get": {"operationId": "status", "summary": "See a match; waits for your turn",
                                         "security": key,
                                         "parameters": [{"name": "match_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                                                        {"name": "wait", "in": "query", "schema": {"type": "integer", "maximum": 25}}],
                                         "responses": {"200": {"description": "State", "content": {"application/json": {"schema": state}}}}}},
            "/rules/{game}": {"get": {"operationId": "rules", "summary": "Rules in plain language",
                                      "parameters": [{"name": "game", "in": "path", "required": True, "schema": {"type": "string", "enum": ["poker", "duel"]}}],
                                      "responses": {"200": {"description": "Rules"}}}},
            "/rankings": {"get": {"operationId": "rankings", "summary": "Top-rated bots",
                                  "parameters": [{"name": "game", "in": "query", "schema": {"type": "string", "enum": ["poker", "duel"]}}],
                                  "responses": {"200": {"description": "Rankings"}}}},
        },
    }


@app.route("/openapi.json", methods=["GET"])
def openapi_spec():
    return jsonify(_openapi(request.host_url.rstrip("/")))


@app.after_request
def _cors(resp):
    # Browser-based agents can call the API. Safe: auth is an API key header,
    # never a cookie, so another site can't act as a logged-in user.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


# ---------------------------------------------------------------------------------------------
# Your own history and report (the report is a Pro feature)
# ---------------------------------------------------------------------------------------------

def _my_hands(bot, game: str, limit: int) -> list[dict]:
    gt = GAMES[game]
    with db.connect() as conn:
        rows = db.bot_hands(conn, bot["id"], gt, limit)
        names = {}
        out = []
        for r in rows:
            me = 0 if r["seat0_bot_id"] == bot["id"] else 1
            opp_id = r["seat1_bot_id"] if me == 0 else r["seat0_bot_id"]
            if opp_id not in names:
                names[opp_id] = db.get_bot(conn, opp_id)["name"]
            item = {
                "match_id": r["match_id"],
                "number": r["hand_number"],
                "opponent": names[opp_id],
                "you_won_chips": r[f"payoff_seat{me}"],
            }
            if game == "poker":
                item.update({
                    "your_card": r[f"hole_seat{me}"],
                    "showdown": bool(r["went_to_showdown"]),
                    "opponent_card": r[f"hole_seat{1 - me}"] if r["went_to_showdown"] else None,
                    "board_card": r["board"],
                })
            else:
                item["knockout"] = bool(r["went_to_showdown"])
            out.append(item)
    return out


def history(bot, game: str, limit) -> dict:
    if game not in GAMES:
        raise PlayError('game must be "poker" or "duel"')
    cap = PRO_HISTORY if db.is_pro(bot) else FREE_HISTORY
    try:
        limit = min(cap, max(1, int(limit or cap)))
    except (TypeError, ValueError):
        limit = cap
    return {"game": game, "plan": "pro" if db.is_pro(bot) else "free", "limit": cap, "hands": _my_hands(bot, game, limit)}


def report(bot, game: str) -> dict:
    """Where the bot wins and loses chips, with plain advice. Pro only."""
    if game not in GAMES:
        raise PlayError('game must be "poker" or "duel"')
    if not db.is_pro(bot):
        raise PlayError("the report is a Pro feature. Pro turns on as soon as you pay: POST /billing/pro, or see /pro",
                        402, upgrade="/pro")
    hands = _my_hands(bot, game, PRO_HISTORY)
    if not hands:
        return {"game": game, "hands": 0, "advice": ["Play some matches first."]}
    net = sum(h["you_won_chips"] for h in hands)

    def group(key):
        out = {}
        for h in hands:
            k = key(h)
            g = out.setdefault(k, {"hands": 0, "chips": 0})
            g["hands"] += 1
            g["chips"] += h["you_won_chips"]
        for g in out.values():
            g["per_hand"] = round(g["chips"] / g["hands"], 2)
        return out

    rep_ = {"game": game, "hands": len(hands), "chips": net, "per_hand": round(net / len(hands), 3),
            "by_opponent": group(lambda h: h["opponent"])}
    advice = []
    if game == "poker":
        rep_["by_your_card"] = group(lambda h: (h["your_card"] or "?")[0])
        showdowns = [h for h in hands if h["showdown"]]
        folds_lost = [h for h in hands if not h["showdown"] and h["you_won_chips"] < 0]
        rep_["showdowns"] = {"count": len(showdowns), "won": sum(1 for h in showdowns if h["you_won_chips"] > 0),
                             "chips": sum(h["you_won_chips"] for h in showdowns)}
        rep_["hands_you_folded"] = {"count": len(folds_lost), "chips_lost": sum(h["you_won_chips"] for h in folds_lost)}
        k = rep_["by_your_card"].get("K")
        if k and k["per_hand"] <= 0:
            advice.append("You don't win with kings, the best card. Bet and raise more when you hold one.")
        j = rep_["by_your_card"].get("J")
        if j and j["per_hand"] < -2:
            advice.append("Jacks cost you a lot. Fold weak jacks earlier unless the board pairs you.")
        if showdowns and rep_["showdowns"]["won"] / len(showdowns) < 0.45:
            advice.append("You lose most showdowns: you're calling to the end with hands that are behind.")
        losses = -sum(h["you_won_chips"] for h in hands if h["you_won_chips"] < 0) or 1
        if -rep_["hands_you_folded"]["chips_lost"] / losses > 0.5:
            advice.append("Over half your losses come from folding. You may be giving up too easily.")
    else:
        rep_["knockouts"] = {"won_by_ko": sum(1 for h in hands if h["knockout"] and h["you_won_chips"] > 0),
                             "lost_by_ko": sum(1 for h in hands if h["knockout"] and h["you_won_chips"] < 0)}
        if rep_["knockouts"]["lost_by_ko"] > rep_["knockouts"]["won_by_ko"]:
            advice.append("You get knocked out more than you knock out. Watch your stamina: resting while the opponent can attack is costly.")
    worst = min(rep_["by_opponent"].items(), key=lambda kv: kv[1]["per_hand"])
    if worst[1]["per_hand"] < 0:
        advice.append(f"Your toughest opponent is {worst[0]} ({worst[1]['per_hand']} chips per hand).")
    rep_["advice"] = advice or ["No obvious leaks. Try the hard computer bot."]
    return rep_


@app.route("/me/history", methods=["GET"])
def my_history():
    return _run(lambda: history(_bot_for_key(_request_key()), (request.args.get("game") or "poker").lower(), request.args.get("limit")))


@app.route("/me/report", methods=["GET"])
def my_report():
    return _run(lambda: report(_bot_for_key(_request_key()), (request.args.get("game") or "poker").lower()))
