"""SQLite ledger: bot accounts, matches, hands, and house rake.

Chip conservation invariant (checked by tests/test_conservation.py):
    sum(all bot balance deltas over a match) + rake collected == 0
i.e. every chip that leaves a bot's stack either goes to the other bot
or to the house rake account. Nothing is created or destroyed.
"""
from __future__ import annotations

import json
import os
import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("ARENA_DB_PATH", str(Path(__file__).resolve().parent.parent / "arena.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    api_key TEXT UNIQUE NOT NULL,
    balance INTEGER NOT NULL DEFAULT 1000,
    -- Deliberately separate from `balance` (practice chips, free to mint
    -- on registration). real_balance holds actual settled value, in
    -- integer CENTS. It only ever moves two ways: (1) an admin-attested
    -- credit/debit recording value that genuinely moved outside the app
    -- (see /admin/bots/<id>/credit and /debit in api/app.py -- a manual,
    -- honest mechanism, not a simulation), or (2) playing a currency
    -- "usd" match. /bots/<id>/deposit and /withdraw remain 501 stubs --
    -- those specifically mean automatic, self-service on-chain transfer,
    -- which genuinely isn't built. Real money should never appear to
    -- move by any path that isn't actually one of the two above.
    real_balance INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_a_id INTEGER NOT NULL REFERENCES bots(id),
    bot_b_id INTEGER NOT NULL REFERENCES bots(id),
    -- 'leduc' (the original game) or 'duel'. Determines which engine and
    -- which API endpoints (/matches/* vs /duel/matches/*) own this match;
    -- the row itself, the bots table, the rake account and the real-value
    -- ledger below are all shared across every game -- one house, one
    -- set of accounts, whatever games are actually played.
    game_type TEXT NOT NULL DEFAULT 'leduc',
    -- Generic across games: "hands" for leduc, "fights" for duel.
    hands_requested INTEGER NOT NULL,
    hands_played INTEGER NOT NULL DEFAULT 0,
    rake_bps INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    -- 'practice_chips' (the only currency that existed before this) or
    -- 'usd'. A 'usd' match's stakes are real_balance, in cents, at
    -- unit_value_cents per game-engine chip unit (e.g. 100 means the
    -- engine's 1-chip ante is really a $1 ante). unit_value_cents is
    -- meaningless and unused for practice_chips matches.
    currency TEXT NOT NULL DEFAULT 'practice_chips',
    unit_value_cents INTEGER NOT NULL DEFAULT 1,
    started_at REAL NOT NULL,
    ended_at REAL
);

CREATE TABLE IF NOT EXISTS hands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id),
    game_type TEXT NOT NULL DEFAULT 'leduc',
    hand_number INTEGER NOT NULL,
    seat0_bot_id INTEGER NOT NULL REFERENCES bots(id),
    seat1_bot_id INTEGER NOT NULL REFERENCES bots(id),
    pot INTEGER NOT NULL,
    rake INTEGER NOT NULL,
    -- For leduc: did it reach showdown (vs. someone folding). For duel:
    -- reused to mean "won by knockout" (vs. decided on HP at the round
    -- cap, or a draw) -- same "how it actually ended" idea, game-specific
    -- meaning documented here rather than adding a second near-duplicate
    -- column for it.
    went_to_showdown INTEGER NOT NULL,
    winner_seat INTEGER,
    payoff_seat0 INTEGER NOT NULL,
    payoff_seat1 INTEGER NOT NULL,
    hole_seat0 TEXT,
    hole_seat1 TEXT,
    board TEXT,
    -- Game-specific extras that don't deserve their own columns (e.g.
    -- duel's rounds_played / final_hp), stored as a JSON object. NULL for
    -- games that don't need it.
    detail_json TEXT,
    -- Same meaning as on `matches` -- copied onto every hand so a hand
    -- row is self-describing (pot/rake/payoffs above are always in game
    -- engine units; multiply by unit_value_cents for real cents when
    -- currency = 'usd').
    currency TEXT NOT NULL DEFAULT 'practice_chips',
    unit_value_cents INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS house (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rake_balance INTEGER NOT NULL DEFAULT 0,
    real_rake_balance INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO house (id, rake_balance, real_rake_balance) VALUES (1, 0, 0);

-- Append-only audit trail for every real_balance change: who, how much,
-- why, and an admin's free-text note (e.g. "received via bank transfer
-- 2026-09-10"). match_id is set for a hand's worth of real-money
-- winnings/losses/rake; NULL for an admin credit/debit. Nothing ever
-- updates or deletes a row here -- it's the honest record of where a
-- bot's real_balance number actually came from.
CREATE TABLE IF NOT EXISTS real_value_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id),
    delta_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    admin_note TEXT,
    match_id INTEGER REFERENCES matches(id),
    created_at REAL NOT NULL
);

-- A match that's in progress (has hands left to play) has one row here
-- holding its entire live state as JSON. This is what lets a match
-- survive a server restart -- see ledger/db.py's save/load_live_match
-- and api/app.py, which writes through here after every action instead
-- of trusting only its in-memory cache. The row is deleted once the
-- match finishes (by then everything's durable in `matches`/`hands`).
CREATE TABLE IF NOT EXISTS live_matches (
    match_id INTEGER PRIMARY KEY REFERENCES matches(id),
    game_type TEXT NOT NULL DEFAULT 'leduc',
    creator_bot_id INTEGER NOT NULL REFERENCES bots(id),
    opponent_bot_id INTEGER NOT NULL REFERENCES bots(id),
    state_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Bots waiting to be auto-paired by the matchmaking lobby (see /lobby/*
-- and /duel/lobby/* in api/app.py). A row is deleted as soon as it's
-- matched or cancelled. One bot can only wait in one lobby at a time
-- (across either game) since bot_id is the primary key.
CREATE TABLE IF NOT EXISTS lobby_entries (
    bot_id INTEGER PRIMARY KEY REFERENCES bots(id),
    game_type TEXT NOT NULL DEFAULT 'leduc',
    hands_wanted INTEGER NOT NULL,
    rake_bps INTEGER NOT NULL,
    -- Meaningless for leduc (its ante is fixed at 1) but load-bearing for
    -- duel, where the entry stake is a per-match choice -- included in
    -- matching criteria below so two duel bots are never silently paired
    -- into a stake neither of them actually asked for.
    stake INTEGER NOT NULL DEFAULT 1,
    currency TEXT NOT NULL DEFAULT 'practice_chips',
    unit_value_cents INTEGER NOT NULL DEFAULT 1,
    joined_at REAL NOT NULL,
    fallback_after REAL NOT NULL
);

-- Phase 1 monetization ("beat the boss"): a bot pays a flat real-money
-- entry fee for one shot at beating a boss bot over a fixed number of
-- hands/fights; winning (by the challenge's own win condition, not just
-- "ahead in chips") pays out prize_cents from the prize pool below.
-- Append-only-ish: rows are updated only to move status
-- pending -> won/lost once the underlying match finishes.
CREATE TABLE IF NOT EXISTS boss_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id),
    game_type TEXT NOT NULL,
    boss_name TEXT NOT NULL,
    match_id INTEGER NOT NULL REFERENCES matches(id),
    entry_fee_cents INTEGER NOT NULL,
    prize_cents INTEGER NOT NULL,
    win_condition TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    resolved_at REAL
);

-- The prize pool a challenge win gets paid from, separate from the
-- house's ordinary rake balance so a run of challenge wins can't
-- silently eat rake revenue that was never meant to fund prizes -- an
-- admin funds this deliberately (see /admin/prize-pool/fund).
CREATE TABLE IF NOT EXISTS prize_pool (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance_cents INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO prize_pool (id, balance_cents) VALUES (1, 0);

-- Append-only audit trail for the prize pool itself (funding by an admin,
-- and payouts on a won challenge) -- separate from real_value_transactions
-- because pool funding isn't any bot's money moving, it's the house's.
CREATE TABLE IF NOT EXISTS prize_pool_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delta_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    note TEXT,
    challenge_id INTEGER REFERENCES boss_challenges(id),
    created_at REAL NOT NULL
);

-- Skill ratings (Elo), one per bot per game ("poker" / "duel"). House bots
-- are never stored here: their ratings are fixed in api/app.py.
CREATE TABLE IF NOT EXISTS ratings (
    bot_id INTEGER NOT NULL REFERENCES bots(id),
    game TEXT NOT NULL,
    rating REAL NOT NULL,
    games INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, game)
);

-- How many rated games each pair of (non-house) bots has played today, so two
-- bots can't farm rating off each other.
CREATE TABLE IF NOT EXISTS rated_pairs (
    day TEXT NOT NULL,
    low_id INTEGER NOT NULL,
    high_id INTEGER NOT NULL,
    games INTEGER NOT NULL,
    PRIMARY KEY (day, low_id, high_id)
);

-- The /play queue (api/play.py). target_bot_id NULL means "anyone"; set, it
-- means "only this bot", and the two are matched only when both have named
-- each other, so no bot can be dragged into a match it didn't agree to.
CREATE TABLE IF NOT EXISTS play_queue (
    bot_id INTEGER NOT NULL REFERENCES bots(id),
    game_type TEXT NOT NULL,
    target_bot_id INTEGER,
    joined_at REAL NOT NULL,
    fallback_after REAL,
    PRIMARY KEY (bot_id, game_type)
);

-- The negotiation exam (engine/negotiation.py): one row per exam, one row per
-- negotiation in it. A negotiation's whole state is JSON, written on every
-- turn, so an exam survives a restart the same way a live match does.
CREATE TABLE IF NOT EXISTS negotiation_exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id),
    seed INTEGER NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    grade INTEGER,
    passed INTEGER
);

CREATE TABLE IF NOT EXISTS negotiations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES negotiation_exams(id),
    bot_id INTEGER NOT NULL REFERENCES bots(id),
    position INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@contextmanager
def connect(db_path: Path | str = DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coldef: str) -> None:
    """Adds a column to an existing table if it's not already there --
    lets an arena.db file created before a schema change (e.g. before
    game_type existed) pick up new columns instead of breaking on the
    next run. CREATE TABLE IF NOT EXISTS alone only helps brand-new
    databases, not ones that already exist on disk."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "matches", "game_type", "TEXT NOT NULL DEFAULT 'leduc'")
        _ensure_column(conn, "hands", "game_type", "TEXT NOT NULL DEFAULT 'leduc'")
        _ensure_column(conn, "hands", "detail_json", "TEXT")
        _ensure_column(conn, "live_matches", "game_type", "TEXT NOT NULL DEFAULT 'leduc'")
        _ensure_column(conn, "lobby_entries", "game_type", "TEXT NOT NULL DEFAULT 'leduc'")
        _ensure_column(conn, "lobby_entries", "stake", "INTEGER NOT NULL DEFAULT 1")
        # Who registered the bot (a hash of their network address, never the
        # address itself): bots with the same owner can't rate each other.
        _ensure_column(conn, "bots", "owner_hash", "TEXT")
        # "free" or "pro" -- pro raises the limits in api/play.py.
        _ensure_column(conn, "bots", "tier", "TEXT NOT NULL DEFAULT 'free'")
        _ensure_column(conn, "matches", "forfeited_by_bot_id", "INTEGER")
        # Databases from before keys were hashed: hash any key still stored raw
        # (raw keys are 32 hex characters; hashes are 64).
        for row in conn.execute("SELECT id, api_key FROM bots WHERE length(api_key) != 64").fetchall():
            conn.execute("UPDATE bots SET api_key = ? WHERE id = ?", (hash_key(row["api_key"]), row["id"]))


def hash_key(api_key: str) -> str:
    """API keys are stored only as their SHA-256, so a copy of the database
    doesn't hand anyone every bot's key."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def create_bot(conn: sqlite3.Connection, name: str, starting_balance: int = 1000, owner_hash: str | None = None) -> dict:
    """Returns the raw api_key exactly once; only its hash is stored."""
    api_key = secrets.token_hex(16)
    cur = conn.execute(
        "INSERT INTO bots (name, api_key, balance, real_balance, created_at, owner_hash) VALUES (?, ?, ?, 0, ?, ?)",
        (name, hash_key(api_key), starting_balance, time.time(), owner_hash),
    )
    return {"id": cur.lastrowid, "name": name, "api_key": api_key, "balance": starting_balance}


def get_bot_by_api_key(conn: sqlite3.Connection, api_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bots WHERE api_key = ?", (hash_key(api_key),)).fetchone()


def get_bot(conn: sqlite3.Connection, bot_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()


def get_bot_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bots WHERE name = ?", (name,)).fetchone()


def get_or_create_bot_by_name(conn: sqlite3.Connection, name: str, starting_balance: int) -> dict:
    row = get_bot_by_name(conn, name)
    if row is not None:
        return dict(row)
    return create_bot(conn, name, starting_balance)


def list_bots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bots ORDER BY balance DESC").fetchall()


def create_match(
    conn: sqlite3.Connection,
    bot_a_id: int,
    bot_b_id: int,
    hands_requested: int,
    rake_bps: int,
    currency: str = "practice_chips",
    unit_value_cents: int = 1,
    game_type: str = "leduc",
) -> int:
    cur = conn.execute(
        """INSERT INTO matches (bot_a_id, bot_b_id, hands_requested, rake_bps, currency, unit_value_cents, game_type, started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (bot_a_id, bot_b_id, hands_requested, rake_bps, currency, unit_value_cents, game_type, time.time()),
    )
    return cur.lastrowid


def record_hand(
    conn: sqlite3.Connection,
    match_id: int,
    hand_number: int,
    seat0_bot_id: int,
    seat1_bot_id: int,
    payoffs: dict[int, int],
    pot_before_rake: int,
    rake_taken: int,
    winner: int | None,
    currency: str = "practice_chips",
    unit_value_cents: int = 1,
    game_type: str = "leduc",
    went_to_showdown: bool = False,
    hole_cards: tuple | None = None,
    board_card=None,
    detail: dict | None = None,
) -> None:
    """Records one hand (leduc) or fight (duel) and moves the ledger --
    the only two ways a `payoffs` dict here can reach a bot's balance are
    the practice_chips branch below (mints/burns nothing, just moves
    chips) and the usd branch (moves real_balance and logs an audit row),
    exactly like every other real-value movement in this file."""
    conn.execute(
        """INSERT INTO hands
           (match_id, game_type, hand_number, seat0_bot_id, seat1_bot_id, pot, rake,
            went_to_showdown, winner_seat, payoff_seat0, payoff_seat1,
            hole_seat0, hole_seat1, board, detail_json, currency, unit_value_cents, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            match_id,
            game_type,
            hand_number,
            seat0_bot_id,
            seat1_bot_id,
            pot_before_rake,
            rake_taken,
            int(bool(went_to_showdown)),
            winner,
            payoffs[0],
            payoffs[1],
            str(hole_cards[0]) if hole_cards else None,
            str(hole_cards[1]) if hole_cards else None,
            str(board_card) if board_card else None,
            json.dumps(detail) if detail is not None else None,
            currency,
            unit_value_cents,
            time.time(),
        ),
    )

    if currency == "practice_chips":
        conn.execute("UPDATE bots SET balance = balance + ? WHERE id = ?", (payoffs[0], seat0_bot_id))
        conn.execute("UPDATE bots SET balance = balance + ? WHERE id = ?", (payoffs[1], seat1_bot_id))
        conn.execute("UPDATE house SET rake_balance = rake_balance + ?", (rake_taken,))
    else:
        cents0 = payoffs[0] * unit_value_cents
        cents1 = payoffs[1] * unit_value_cents
        rake_cents = rake_taken * unit_value_cents
        conn.execute("UPDATE bots SET real_balance = real_balance + ? WHERE id = ?", (cents0, seat0_bot_id))
        conn.execute("UPDATE bots SET real_balance = real_balance + ? WHERE id = ?", (cents1, seat1_bot_id))
        conn.execute("UPDATE house SET real_rake_balance = real_rake_balance + ?", (rake_cents,))
        for bot_id, cents in ((seat0_bot_id, cents0), (seat1_bot_id, cents1)):
            if cents != 0:
                _record_real_value_transaction(conn, bot_id, cents, reason="hand_settlement", admin_note=None, match_id=match_id)

    conn.execute("UPDATE matches SET hands_played = hands_played + 1 WHERE id = ?", (match_id,))


def _record_real_value_transaction(
    conn: sqlite3.Connection,
    bot_id: int,
    delta_cents: int,
    reason: str,
    admin_note: str | None,
    match_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO real_value_transactions (bot_id, delta_cents, reason, admin_note, match_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (bot_id, delta_cents, reason, admin_note, match_id, time.time()),
    )


def finish_match(conn: sqlite3.Connection, match_id: int) -> None:
    conn.execute("UPDATE matches SET status = 'completed', ended_at = ? WHERE id = ?", (time.time(), match_id))


def house_balance(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT rake_balance FROM house WHERE id = 1").fetchone()
    return row["rake_balance"]


def match_history(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM hands WHERE match_id = ? ORDER BY hand_number", (match_id,)).fetchall()


def save_live_match(
    conn: sqlite3.Connection, match_id: int, creator_bot_id: int, opponent_bot_id: int, state: dict,
    game_type: str = "leduc",
) -> None:
    conn.execute(
        """INSERT INTO live_matches (match_id, game_type, creator_bot_id, opponent_bot_id, state_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(match_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at""",
        (match_id, game_type, creator_bot_id, opponent_bot_id, json.dumps(state), time.time()),
    )


def load_live_match(conn: sqlite3.Connection, match_id: int, game_type: str | None = None) -> dict | None:
    """`game_type`, when given, guards against the wrong game's endpoint
    trying to load another game's match_id (match ids are shared across
    every game via one `matches` table) -- a mismatch returns None, same
    as "not found", rather than handing back a state blob shaped for a
    different engine."""
    row = conn.execute("SELECT state_json, game_type FROM live_matches WHERE match_id = ?", (match_id,)).fetchone()
    if row is None:
        return None
    if game_type is not None and row["game_type"] != game_type:
        return None
    return json.loads(row["state_json"])


def delete_live_match(conn: sqlite3.Connection, match_id: int) -> None:
    conn.execute("DELETE FROM live_matches WHERE match_id = ?", (match_id,))


def list_live_match_ids(conn: sqlite3.Connection) -> list[int]:
    """Every match still in progress after a restart -- used to warm the
    in-memory cache back up, and to answer 'is this bot already in a
    match' without scanning every match row."""
    return [r["match_id"] for r in conn.execute("SELECT match_id FROM live_matches").fetchall()]


def find_active_match_for_bot(conn: sqlite3.Connection, bot_id: int, game_type: str = "leduc") -> int | None:
    """So a bot that got auto-paired by someone else joining the lobby
    (rather than by calling /matches itself) can discover the match it's
    now in just by polling with its own api_key. Filtered by game_type --
    a bot can have one active match per game simultaneously (a live_matches
    row per match_id, shared table), and a leduc lobby poll shouldn't hand
    back a duel match id or vice versa."""
    row = conn.execute(
        "SELECT match_id FROM live_matches WHERE (creator_bot_id = ? OR opponent_bot_id = ?) AND game_type = ? ORDER BY match_id DESC LIMIT 1",
        (bot_id, bot_id, game_type),
    ).fetchone()
    return row["match_id"] if row else None


def join_lobby(
    conn: sqlite3.Connection,
    bot_id: int,
    hands_wanted: int,
    rake_bps: int,
    fallback_after_seconds: float,
    currency: str = "practice_chips",
    unit_value_cents: int = 1,
    game_type: str = "leduc",
    stake: int = 1,
) -> None:
    now = time.time()
    conn.execute(
        """INSERT INTO lobby_entries (bot_id, game_type, hands_wanted, rake_bps, stake, currency, unit_value_cents, joined_at, fallback_after)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(bot_id) DO UPDATE SET game_type = excluded.game_type, hands_wanted = excluded.hands_wanted,
               rake_bps = excluded.rake_bps, stake = excluded.stake, currency = excluded.currency,
               unit_value_cents = excluded.unit_value_cents, joined_at = excluded.joined_at,
               fallback_after = excluded.fallback_after""",
        (bot_id, game_type, hands_wanted, rake_bps, stake, currency, unit_value_cents, now, now + fallback_after_seconds),
    )


def leave_lobby(conn: sqlite3.Connection, bot_id: int) -> None:
    conn.execute("DELETE FROM lobby_entries WHERE bot_id = ?", (bot_id,))


def get_lobby_entry(conn: sqlite3.Connection, bot_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM lobby_entries WHERE bot_id = ?", (bot_id,)).fetchone()


def find_lobby_opponent(
    conn: sqlite3.Connection, bot_id: int, hands_wanted: int, rake_bps: int, currency: str = "practice_chips",
    unit_value_cents: int = 1, game_type: str = "leduc", stake: int = 1,
) -> sqlite3.Row | None:
    """Another waiting bot who wants the same game/hands/rake/currency/
    unit-value/stake, oldest first. `stake` only matters for duel (leduc
    passes the default 1 on both sides, a no-op match) -- included so two
    duel bots are never paired into an entry stake neither one asked for."""
    return conn.execute(
        """SELECT * FROM lobby_entries
           WHERE bot_id != ? AND game_type = ? AND hands_wanted = ? AND rake_bps = ? AND currency = ? AND unit_value_cents = ? AND stake = ?
           ORDER BY joined_at ASC LIMIT 1""",
        (bot_id, game_type, hands_wanted, rake_bps, currency, unit_value_cents, stake),
    ).fetchone()


def recent_matches(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT m.*, a.name AS bot_a_name, b.name AS bot_b_name
           FROM matches m
           JOIN bots a ON a.id = m.bot_a_id
           JOIN bots b ON b.id = m.bot_b_id
           ORDER BY m.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()


# -- Phase 1 monetization: "beat the boss" challenges --------------------
#
# A bot pays a flat real-money entry fee for one shot at a boss bot; if it
# meets the challenge's win condition it's paid prize_cents from the prize
# pool (funded separately by an admin -- see fund_prize_pool), otherwise
# the entry fee stays with the house as ordinary rake-like revenue. The
# match itself is a completely normal usd match under the hood (same
# engine, same bankroll caps, same conservation guarantees); this layer
# only adds the fixed entry fee and the pass/fail prize payout on top.

def create_boss_challenge(
    conn: sqlite3.Connection, bot_id: int, game_type: str, boss_name: str, match_id: int,
    win_condition: str,
) -> int:
    """A free exam: the fee and prize columns are kept for old databases and always 0."""
    cur = conn.execute(
        """INSERT INTO boss_challenges
           (bot_id, game_type, boss_name, match_id, entry_fee_cents, prize_cents, win_condition, status, created_at)
           VALUES (?, ?, ?, ?, 0, 0, ?, 'pending', ?)""",
        (bot_id, game_type, boss_name, match_id, win_condition, time.time()),
    )
    return cur.lastrowid


def get_boss_challenge(conn: sqlite3.Connection, challenge_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM boss_challenges WHERE id = ?", (challenge_id,)).fetchone()


def resolve_boss_challenge(conn: sqlite3.Connection, challenge_id: int, won: bool) -> dict:
    """Marks an exam passed ("won") or failed ("lost"). Nothing is paid.
    Resolving an already-resolved exam again raises."""
    challenge = get_boss_challenge(conn, challenge_id)
    if challenge is None:
        raise ValueError(f"no such challenge {challenge_id}")
    if challenge["status"] != "pending":
        raise ValueError(f"challenge {challenge_id} already resolved as {challenge['status']}")
    conn.execute(
        "UPDATE boss_challenges SET status = ?, resolved_at = ? WHERE id = ?",
        ("won" if won else "lost", time.time(), challenge_id),
    )
    return dict(get_boss_challenge(conn, challenge_id))


def bot_boss_challenges(conn: sqlite3.Connection, bot_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM boss_challenges WHERE bot_id = ? ORDER BY id DESC LIMIT ?", (bot_id, limit)
    ).fetchall()


def get_boss_challenge_by_match(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM boss_challenges WHERE match_id = ?", (match_id,)).fetchone()


def ensure_minimum_practice_balance(conn: sqlite3.Connection, bot_id: int, minimum: int) -> None:
    """Tops up (never reduces) a bot's practice-chip balance to at least
    `minimum`, for free -- practice chips are already free-to-mint
    elsewhere (every new bot starts with them; baseline bots start with a
    huge stock precisely so they never bust mid-match). Used before a
    boss challenge so a challenger who paid a real entry fee can't have
    their challenge match instantly end in a bust because their unrelated
    practice balance happened to be low -- the challenge is won or lost
    on net chips over the match, never on whether they could afford to
    start it."""
    bot = get_bot(conn, bot_id)
    if bot is not None and bot["balance"] < minimum:
        conn.execute("UPDATE bots SET balance = ? WHERE id = ?", (minimum, bot_id))


# ---- negotiation exams --------------------------------------------------------------------

def create_negotiation_exam(conn: sqlite3.Connection, bot_id: int, seed: int, states: list[dict]) -> int:
    now = time.time()
    exam_id = conn.execute(
        "INSERT INTO negotiation_exams (bot_id, seed, created_at) VALUES (?, ?, ?)", (bot_id, seed, now)
    ).lastrowid
    for position, state in enumerate(states, 1):
        conn.execute(
            "INSERT INTO negotiations (exam_id, bot_id, position, state_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (exam_id, bot_id, position, json.dumps(state), now),
        )
    return exam_id


def get_negotiation_exam(conn: sqlite3.Connection, exam_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM negotiation_exams WHERE id = ?", (exam_id,)).fetchone()


def open_negotiation_exam_for_bot(conn: sqlite3.Connection, bot_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM negotiation_exams WHERE bot_id = ? AND finished_at IS NULL ORDER BY id DESC LIMIT 1", (bot_id,)
    ).fetchone()
    return row["id"] if row else None


def exam_negotiations(conn: sqlite3.Connection, exam_id: int) -> list[tuple[int, dict]]:
    rows = conn.execute(
        "SELECT id, state_json FROM negotiations WHERE exam_id = ? ORDER BY position", (exam_id,)
    ).fetchall()
    return [(r["id"], json.loads(r["state_json"])) for r in rows]


def get_negotiation(conn: sqlite3.Connection, negotiation_id: int) -> tuple[sqlite3.Row, dict] | None:
    row = conn.execute("SELECT * FROM negotiations WHERE id = ?", (negotiation_id,)).fetchone()
    return (row, json.loads(row["state_json"])) if row else None


def save_negotiation(conn: sqlite3.Connection, negotiation_id: int, state: dict) -> None:
    conn.execute(
        "UPDATE negotiations SET state_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(state), time.time(), negotiation_id),
    )


def finish_negotiation_exam(conn: sqlite3.Connection, exam_id: int, grade: int, passed: bool) -> None:
    conn.execute(
        "UPDATE negotiation_exams SET finished_at = ?, grade = ?, passed = ? WHERE id = ? AND finished_at IS NULL",
        (time.time(), grade, int(passed), exam_id),
    )


def negotiation_exam_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(finished_at) AS finished, COALESCE(SUM(passed), 0) AS passed FROM negotiation_exams"
    ).fetchone()
    return {"finished": row["finished"], "passed": row["passed"]}


# ---- ratings ------------------------------------------------------------------------------

START_RATING = 1000.0


def get_rating(conn: sqlite3.Connection, bot_id: int, game: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ratings WHERE bot_id = ? AND game = ?", (bot_id, game)).fetchone()


def apply_elo(conn, game: str, a_id: int, b_id: int, score_a: float,
              fixed_a: float | None = None, fixed_b: float | None = None) -> None:
    """Standard Elo. A side with a fixed rating (a house bot) isn't stored or
    moved. K is larger for a bot's first 10 games so new bots settle fast."""
    def current(bot_id, fixed):
        if fixed is not None:
            return float(fixed), None
        row = get_rating(conn, bot_id, game)
        return (row["rating"], row["games"]) if row else (START_RATING, 0)

    ra, ga = current(a_id, fixed_a)
    rb, gb = current(b_id, fixed_b)
    expected_a = 1 / (1 + 10 ** ((rb - ra) / 400))
    for bot_id, fixed, rating, games, score, expected in (
        (a_id, fixed_a, ra, ga, score_a, expected_a),
        (b_id, fixed_b, rb, gb, 1 - score_a, 1 - expected_a),
    ):
        if fixed is not None:
            continue
        k = 48 if games < 10 else 24
        new = rating + k * (score - expected)
        win, loss, draw = int(score == 1), int(score == 0), int(score == 0.5)
        conn.execute(
            """INSERT INTO ratings (bot_id, game, rating, games, wins, losses, draws, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(bot_id, game) DO UPDATE SET rating = ?, games = games + 1,
                   wins = wins + ?, losses = losses + ?, draws = draws + ?, updated_at = ?""",
            (bot_id, game, new, win, loss, draw, time.time(), new, win, loss, draw, time.time()),
        )


def take_rated_pair_slot(conn, a_id: int, b_id: int, limit: int) -> bool:
    """Counts one rated game between two bots today; False once the pair is
    at `limit` for the day."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    low, high = min(a_id, b_id), max(a_id, b_id)
    row = conn.execute("SELECT games FROM rated_pairs WHERE day = ? AND low_id = ? AND high_id = ?", (day, low, high)).fetchone()
    if row and row["games"] >= limit:
        return False
    conn.execute(
        """INSERT INTO rated_pairs (day, low_id, high_id, games) VALUES (?, ?, ?, 1)
           ON CONFLICT(day, low_id, high_id) DO UPDATE SET games = games + 1""",
        (day, low, high),
    )
    return True


def rating_leaderboard(conn, game: str, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT b.id, b.name, r.rating, r.games, r.wins, r.losses, r.draws
           FROM ratings r JOIN bots b ON b.id = r.bot_id
           WHERE r.game = ? ORDER BY r.rating DESC LIMIT ?""",
        (game, limit),
    ).fetchall()


def rating_rank(conn, game: str, bot_id: int) -> int | None:
    row = get_rating(conn, bot_id, game)
    if row is None:
        return None
    return conn.execute("SELECT COUNT(*) FROM ratings WHERE game = ? AND rating > ?", (game, row["rating"])).fetchone()[0] + 1


def count_bots_by_owner_since(conn, owner_hash: str, since: float) -> int:
    return conn.execute("SELECT COUNT(*) FROM bots WHERE owner_hash = ? AND created_at >= ?", (owner_hash, since)).fetchone()[0]


def active_match_ids_for_bot(conn, bot_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT match_id, game_type FROM live_matches WHERE creator_bot_id = ? OR opponent_bot_id = ? ORDER BY match_id",
        (bot_id, bot_id),
    ).fetchall()


def get_match(conn, match_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()


def set_forfeit(conn, match_id: int, bot_id: int) -> None:
    conn.execute("UPDATE matches SET forfeited_by_bot_id = ? WHERE id = ?", (bot_id, match_id))


def top_up_practice_chips(conn, bot_id: int, floor: int, amount: int) -> None:
    """Practice chips have no value, so a bot that runs low is simply refilled
    rather than locked out of playing."""
    conn.execute("UPDATE bots SET balance = ? WHERE id = ? AND balance < ?", (amount, bot_id, floor))


def set_tier(conn, bot_id: int, tier: str) -> None:
    conn.execute("UPDATE bots SET tier = ? WHERE id = ?", (tier, bot_id))


def bot_recent_matches(conn, bot_id: int, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT m.id, m.game_type, m.status, m.hands_played, m.hands_requested, m.started_at, m.forfeited_by_bot_id,
                  a.name AS bot_a_name, b.name AS bot_b_name, m.bot_a_id, m.bot_b_id
           FROM matches m JOIN bots a ON a.id = m.bot_a_id JOIN bots b ON b.id = m.bot_b_id
           WHERE m.bot_a_id = ? OR m.bot_b_id = ? ORDER BY m.id DESC LIMIT ?""",
        (bot_id, bot_id, limit),
    ).fetchall()


def net_for_bot_in_match(conn, match_id: int, bot_id: int) -> int:
    net = 0
    for row in match_history(conn, match_id):
        if row["seat0_bot_id"] == bot_id:
            net += row["payoff_seat0"]
        elif row["seat1_bot_id"] == bot_id:
            net += row["payoff_seat1"]
    return net


# ---- /play queue --------------------------------------------------------------------------

def queue_join(conn, bot_id: int, game_type: str, target_bot_id: int | None, fallback_seconds: float | None) -> None:
    now = time.time()
    conn.execute(
        """INSERT INTO play_queue (bot_id, game_type, target_bot_id, joined_at, fallback_after) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(bot_id, game_type) DO UPDATE SET target_bot_id = excluded.target_bot_id,
               joined_at = excluded.joined_at, fallback_after = excluded.fallback_after""",
        (bot_id, game_type, target_bot_id, now, None if fallback_seconds is None else now + fallback_seconds),
    )


def queue_leave(conn, bot_id: int, game_type: str | None = None) -> None:
    if game_type is None:
        conn.execute("DELETE FROM play_queue WHERE bot_id = ?", (bot_id,))
    else:
        conn.execute("DELETE FROM play_queue WHERE bot_id = ? AND game_type = ?", (bot_id, game_type))


def queue_entry(conn, bot_id: int, game_type: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM play_queue WHERE bot_id = ? AND game_type = ?", (bot_id, game_type)).fetchone()


def queue_find_partner(conn, bot_id: int, game_type: str, target_bot_id: int | None) -> sqlite3.Row | None:
    """For "anyone": the longest-waiting bot also asking for anyone. For a named
    bot: that bot, only if it is waiting for this one."""
    if target_bot_id is None:
        return conn.execute(
            """SELECT * FROM play_queue WHERE game_type = ? AND bot_id != ? AND target_bot_id IS NULL
               ORDER BY joined_at LIMIT 1""",
            (game_type, bot_id),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM play_queue WHERE game_type = ? AND bot_id = ? AND target_bot_id = ?",
        (game_type, target_bot_id, bot_id),
    ).fetchone()


def queue_invitations(conn, bot_id: int) -> list[sqlite3.Row]:
    """Bots currently waiting to play this one by name."""
    return conn.execute(
        """SELECT q.game_type, q.joined_at, b.name FROM play_queue q JOIN bots b ON b.id = q.bot_id
           WHERE q.target_bot_id = ? ORDER BY q.joined_at""",
        (bot_id,),
    ).fetchall()
