"""Reporting match results to agenttrust, the public trust score for bots.

Every rated match says something true about a bot: it showed up and played to
the end, or it went silent and forfeited. The arena reports that to agenttrust
as a trusted partner, so an arena record follows a bot everywhere agenttrust is
checked -- and a bot's arena profile links its trust score.

How agenttrust knows these reports really come from this arena, with nothing
copied between the two services: the arena makes its own secret on first boot
and publishes only its SHA-256 at /.well-known/agenttrust-source.json. Whoever
controls this domain controls the source. agenttrust is told the domain once
(its TRUSTED_SOURCES setting) and checks the hash itself.

Reports wait in an outbox table and a background thread sends them, so a slow
or unreachable agenttrust never holds up a match. Nothing is sent until
agenttrust lists the arena as trusted, so no report is ever wasted.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

from flask import jsonify

from api.app import app
from ledger import db

SOURCE_NAME = "arena"
DEFAULT_AGENTTRUST_URL = "https://agenttrust-production-381e.up.railway.app"


def agenttrust_url() -> str | None:
    """Where agenttrust lives; AGENTTRUST_URL=off turns reporting off."""
    url = os.environ.get("AGENTTRUST_URL", DEFAULT_AGENTTRUST_URL).strip().rstrip("/")
    return None if url.lower() in ("", "off", "0") else url


def subject_for(bot_name: str) -> str:
    return f"{SOURCE_NAME}.{bot_name}"


def trust_links(bot_name: str) -> dict | None:
    base = agenttrust_url()
    if not base:
        return None
    sid = subject_for(bot_name)
    return {"id": sid, "profile": f"{base}/trust/{sid}", "api": f"{base}/v1/trust/{sid}",
            "badge": f"{base}/v1/trust/{sid}/badge.svg"}


def _secret(conn) -> str:
    s = db.get_setting(conn, "agenttrust_secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_setting(conn, "agenttrust_secret", s)
    return s


@app.route("/.well-known/agenttrust-source.json", methods=["GET"])
def agenttrust_source():
    with db.connect() as conn:
        digest = hashlib.sha256(_secret(conn).encode()).hexdigest()
    return jsonify(source=SOURCE_NAME, secret_sha256=digest)


def queue_match_result(conn, bot_name: str, forfeited: bool) -> None:
    """Called for each non-house bot in a rated match."""
    if agenttrust_url():
        db.queue_trust_event(conn, bot_name, "ghosted" if forfeited else "cleared_cleanly")


def _http(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read(256 * 1024) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(64 * 1024) or b"{}")
        except ValueError:
            return e.code, {}


_trusted_checked_at = 0.0
_trusted = False


def _arena_is_trusted(base: str) -> bool:
    """Whether agenttrust counts this arena's reports yet (checked every 10 minutes)."""
    global _trusted_checked_at, _trusted
    if time.time() - _trusted_checked_at > 600:
        _trusted_checked_at = time.time()
        try:
            status, health = _http("GET", f"{base}/health")
            _trusted = status == 200 and SOURCE_NAME in (health.get("trusted_sources") or [])
        except (OSError, ValueError):
            _trusted = False
    return _trusted


def flush() -> int:
    """Sends waiting reports. Returns how many went out."""
    base = agenttrust_url()
    if not base or not _arena_is_trusted(base):
        return 0
    with db.connect() as conn:
        secret = _secret(conn)
        rows = db.pending_trust_events(conn)
    sent = 0
    for row in rows:
        try:
            status, reply = _http("POST", f"{base}/v1/attestations", {
                "source": SOURCE_NAME, "secret": secret, "subject": subject_for(row["bot_name"]),
                "event": row["event"], "domain": "wagering",
            })
        except (OSError, ValueError):
            return sent  # agenttrust unreachable; try again next round
        if status == 429 or status >= 500:
            return sent
        if status != 200:
            print(f"agenttrust refused report {row['id']}: {status} {reply.get('error')}", flush=True)
        with db.connect() as conn:
            db.mark_trust_event_sent(conn, row["id"])
        sent += 1
    return sent


_started = False


def start_reporter(interval: float = 30) -> None:
    global _started
    if _started or not agenttrust_url() or os.environ.get("ARENA_BACKGROUND", "1") == "0":
        return
    _started = True

    def loop():
        while True:
            time.sleep(interval)
            try:
                flush()
            except Exception as e:  # never let the reporter die
                print(f"agenttrust reporter: {e}", flush=True)

    threading.Thread(target=loop, name="agenttrust-reporter", daemon=True).start()
