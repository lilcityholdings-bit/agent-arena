"""Agent Arena is a proving ground, not a casino. These tests lock in that
there is no way to put real money in, move it, or take it out, and they
cover each money bug that existed before:

- a caller could set the house rake, and a negative one minted chips;
- a caller could pick their own starting balance;
- API keys were stored as plain text.
"""
import os
import sys
import tempfile
import unittest

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH
os.environ["ARENA_ADMIN_SECRET"] = "test-admin-secret"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import create_app, DEFAULT_STARTING_BALANCE  # noqa: E402
from ledger import db  # noqa: E402

ADMIN = {"X-Admin-Secret": "test-admin-secret"}


def _pick(legal):
    for choice in ("raise", "call", "check"):
        if choice in legal:
            return choice
    return legal[0]


class TestNoRealMoney(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        os.remove(_TEST_DB_PATH)

    def setUp(self):
        self.client = self.app.test_client()

    def _register(self, name, **extra):
        resp = self.client.post("/bots", json={"name": name, **extra})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def _play_out(self, match_id, headers):
        for _ in range(5000):
            state = self.client.get(f"/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done"):
                return
            if state.get("your_turn"):
                r = self.client.post(f"/matches/{match_id}/action", json={"action": _pick(state["legal_actions"])}, headers=headers)
                if r.get_json().get("match_done"):
                    return
        self.fail("match did not finish")

    def test_a_negative_rake_from_the_caller_cannot_mint_chips(self):
        bot = self._register("minter")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 20, "rake_bps": -10000}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        self.assertEqual(resp.get_json()["rake_bps"], 0, "the server decides the rake, not the caller")
        match_id = resp.get_json()["match_id"]
        self._play_out(match_id, headers)
        hands = self.client.get(f"/matches/{match_id}").get_json()["hands"]
        self.assertEqual(sum(h["rake"] for h in hands), 0)
        self.assertEqual(sum(h["payoff_seat0"] + h["payoff_seat1"] for h in hands), 0, "chips only move between the two players")

    def test_a_caller_cannot_choose_its_starting_balance(self):
        bot = self._register("whale", starting_balance=999_999_999)
        self.assertEqual(bot["balance"], DEFAULT_STARTING_BALANCE)

    def test_api_keys_are_stored_hashed_and_still_work(self):
        bot = self._register("hashed")
        with db.connect() as conn:
            stored = conn.execute("SELECT api_key FROM bots WHERE id = ?", (bot["id"],)).fetchone()["api_key"]
        self.assertNotEqual(stored, bot["api_key"])
        self.assertEqual(stored, db.hash_key(bot["api_key"]))
        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 1}, headers={"X-API-Key": bot["api_key"]})
        self.assertEqual(resp.status_code, 201)
        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 1}, headers={"X-API-Key": stored})
        self.assertEqual(resp.status_code, 401, "the stored hash is not itself a usable key")

    def test_old_plain_text_keys_are_hashed_on_upgrade(self):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO bots (name, api_key, balance, real_balance, created_at) VALUES ('legacy', 'abcdef0123456789abcdef0123456789', 1000, 0, 0)"
            )
        db.init_db()
        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 1},
                                headers={"X-API-Key": "abcdef0123456789abcdef0123456789"})
        self.assertEqual(resp.status_code, 201, "a legacy key keeps working after it's hashed")

    def test_real_money_routes_are_gone(self):
        bot = self._register("nomoney")
        auth = {"X-API-Key": bot["api_key"]}
        for method, path, headers in [
            ("post", f"/admin/bots/{bot['id']}/credit", ADMIN),
            ("post", f"/admin/bots/{bot['id']}/debit", ADMIN),
            ("post", f"/bots/{bot['id']}/deposit", auth),
            ("post", f"/bots/{bot['id']}/withdraw", auth),
            ("get", f"/bots/{bot['id']}/transactions", auth),
            ("post", "/admin/prize-pool/fund", ADMIN),
            ("get", "/prize-pool", {}),
        ]:
            resp = getattr(self.client, method)(path, json={"amount_cents": 100, "note": "x"}, headers=headers)
            self.assertEqual(resp.status_code, 404, f"{method.upper()} {path} should not exist")

    def test_usd_is_refused_everywhere(self):
        a = self._register("usd_a")
        b = self._register("usd_b")
        headers = {"X-API-Key": a["api_key"]}
        for path, body in [
            ("/matches", {"opponent": "random_bot", "hands": 1, "currency": "usd"}),
            ("/matches", {"opponent": str(b["id"]), "hands": 1, "currency": "usd"}),
            ("/lobby/join", {"hands": 1, "currency": "usd"}),
            ("/duel/matches", {"opponent": "random_duel_bot", "fights": 1, "currency": "usd"}),
            ("/duel/lobby/join", {"fights": 1, "currency": "usd"}),
        ]:
            resp = self.client.post(path, json=body, headers=headers)
            self.assertEqual(resp.status_code, 400, f"{path} accepted usd")
        self.assertNotIn("real_balance", self.client.get("/leaderboard").get_json()["bots"][0])


if __name__ == "__main__":
    unittest.main()
