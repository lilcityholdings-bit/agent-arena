"""Tests for the two things that turned a toy into something that could
survive real use: matches surviving a server restart, and the
matchmaking lobby (bots pairing up without needing to know each other's
id ahead of time)."""
import importlib
import os
import sys
import tempfile
import unittest

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH
os.environ.pop("ARENA_ADMIN_SECRET", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.app as app_module  # noqa: E402

ADMIN_SECRET = "test-lobby-admin-secret-do-not-use-in-prod"


def _pick_action(legal: list[str]) -> str:
    for choice in ("raise", "call", "check"):
        if choice in legal:
            return choice
    return legal[0]


class TestDurability(unittest.TestCase):
    """Simulates a server restart mid-match by wiping the in-memory
    LIVE_MATCHES cache (which is all a real restart would lose) and
    checking the match can still be played to completion by reloading
    from the ledger's live_matches table."""

    @classmethod
    def setUpClass(cls):
        cls.app = app_module.create_app()
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        os.remove(_TEST_DB_PATH)

    def setUp(self):
        self.client = self.app.test_client()

    def _register(self, name):
        resp = self.client.post("/bots", json={"name": name})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def test_match_survives_a_simulated_restart(self):
        bot = self._register("durability_bot")
        headers = {"X-API-Key": bot["api_key"]}

        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 10}, headers=headers)
        match_id = resp.get_json()["match_id"]

        # Play a couple of steps normally first.
        for _ in range(3):
            state = self.client.get(f"/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done") or not state.get("your_turn"):
                continue
            self.client.post(f"/matches/{match_id}/action", json={"action": _pick_action(state["legal_actions"])}, headers=headers)

        # "Restart the server": wipe the in-memory cache. Only what's in
        # SQLite (ledger/db.py's live_matches table) can survive this.
        app_module.LIVE_MATCHES.clear()

        # The match must still be reachable and playable from here.
        for _ in range(2000):
            state = self.client.get(f"/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done"):
                break
            if not state.get("your_turn"):
                continue
            resp = self.client.post(f"/matches/{match_id}/action", json={"action": _pick_action(state["legal_actions"])}, headers=headers)
            self.assertEqual(resp.status_code, 200, resp.get_json())
            if resp.get_json().get("match_done"):
                break
        else:
            self.fail("match did not finish after simulated restart")

        summary = self.client.get(f"/matches/{match_id}").get_json()
        self.assertEqual(len(summary["hands"]), 10)


class TestLobby(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.create_app()
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        os.remove(_TEST_DB_PATH)

    def setUp(self):
        self.client = self.app.test_client()

    def _register(self, name):
        resp = self.client.post("/bots", json={"name": name})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def test_two_bots_auto_pair_in_the_lobby(self):
        """Competitive (bot vs bot) lobby pairing requires a real wager --
        currency=usd, with both sides actually funded -- same policy as
        the direct /matches route (see _check_wager_policy in
        api/app.py). Without that, a practice_chips join can never be
        paired with another real bot; it's matched against the computer
        instead (see test_lobby_join_is_free_and_instant_against_the_
        computer below)."""
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        admin_headers = {"X-Admin-Secret": ADMIN_SECRET}
        bot_a = self._register("lobby_bot_a")
        bot_b = self._register("lobby_bot_b")
        for bot in (bot_a, bot_b):
            resp = self.client.post(f"/admin/bots/{bot['id']}/credit", json={"amount_cents": 10000, "note": "test funding"}, headers=admin_headers)
            self.assertEqual(resp.status_code, 200)
        headers_a = {"X-API-Key": bot_a["api_key"]}
        headers_b = {"X-API-Key": bot_b["api_key"]}

        r1 = self.client.post("/lobby/join", json={"hands": 5, "rake_bps": 500, "currency": "usd"}, headers=headers_a)
        self.assertFalse(r1.get_json()["matched"])  # nobody else waiting yet

        r2 = self.client.post("/lobby/join", json={"hands": 5, "rake_bps": 500, "currency": "usd"}, headers=headers_b)
        data2 = r2.get_json()
        self.assertTrue(data2["matched"])  # bot_b's join should find bot_a waiting
        match_id = data2["match_id"]

        # bot_a discovers the match by polling status, without ever being
        # told bot_b's id ahead of time.
        status = self.client.get("/lobby/status", headers=headers_a).get_json()
        self.assertTrue(status["matched"])
        self.assertEqual(status["match_id"], match_id)

    def test_lobby_join_is_free_and_instant_against_the_computer(self):
        """practice_chips is the free-to-play-the-computer currency, so a
        /lobby/join with no currency specified (the default) is matched
        against a baseline bot immediately -- no waiting, since a
        practice_chips entry could never be a competitive (real-wager)
        pairing anyway."""
        bot = self._register("lonely_bot")
        headers = {"X-API-Key": bot["api_key"]}
        r = self.client.post("/lobby/join", json={"hands": 3, "rake_bps": 500}, headers=headers)
        data = r.get_json()
        self.assertTrue(data["matched"])
        self.assertIn(data["opponent"], ("random_bot", "heuristic_bot"))

    def test_usd_lobby_entry_waits_and_never_falls_back_to_the_computer(self):
        """The other half of the policy: a usd (competitive) lobby entry
        must never be quietly handed a free baseline opponent, however
        long it waits -- that would let bot-vs-bot play dodge the real
        wager requirement."""
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        admin_headers = {"X-Admin-Secret": ADMIN_SECRET}
        bot = self._register("lonely_wager_bot")
        resp = self.client.post(f"/admin/bots/{bot['id']}/credit", json={"amount_cents": 10000, "note": "test funding"}, headers=admin_headers)
        self.assertEqual(resp.status_code, 200)
        headers = {"X-API-Key": bot["api_key"]}

        r = self.client.post(
            "/lobby/join",
            json={"hands": 3, "rake_bps": 500, "currency": "usd", "fallback_after_seconds": 0},
            headers=headers,
        )
        self.assertFalse(r.get_json()["matched"])

        status = self.client.get("/lobby/status", headers=headers).get_json()
        self.assertFalse(status["matched"])
        self.assertTrue(status["waiting"])


if __name__ == "__main__":
    unittest.main()
