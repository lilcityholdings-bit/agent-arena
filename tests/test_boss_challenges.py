"""Phase 1 monetization: pay a real-money entry fee for one shot at a
boss bot, win the whole challenge (net chips positive across every hand
played, not just the last one) and get paid from the prize pool; lose
and the house keeps the fee. These tests lock in the money-safety
properties: fail closed without funding/balance, the challenger's real
risk is capped at exactly the entry fee, and a challenge actually
resolves (and only once) when its match finishes."""
import os
import random
import sys
import tempfile
import unittest

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import create_app, LIVE_MATCHES, LIVE_DUEL_MATCHES, BOSS_CHALLENGE_PRIZE_MULTIPLIER  # noqa: E402

ADMIN_SECRET = "test-admin-secret"


def _pick_leduc_action(legal):
    for choice in ("raise", "call", "check", "fold"):
        if choice in legal:
            return choice
    return legal[0]


class TestBossChallenges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        os.remove(_TEST_DB_PATH)

    def setUp(self):
        self.client = self.app.test_client()
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        LIVE_MATCHES.clear()
        LIVE_DUEL_MATCHES.clear()

    def tearDown(self):
        os.environ.pop("ARENA_ADMIN_SECRET", None)

    def _register(self, name):
        return self.client.post("/bots", json={"name": name}).get_json()

    def _fund(self, bot_id, cents):
        return self.client.post(
            f"/admin/bots/{bot_id}/credit", json={"amount_cents": cents, "note": "test funding"},
            headers={"X-Admin-Secret": ADMIN_SECRET},
        )

    def _fund_prize_pool(self, cents):
        return self.client.post(
            "/admin/prize-pool/fund", json={"amount_cents": cents, "note": "test seed"},
            headers={"X-Admin-Secret": ADMIN_SECRET},
        )

    def _fresh_ledger_db(self):
        """A standalone temp DB, isolated from the shared class-level app
        DB (and from every other test using this helper) -- for tests
        that need to know the prize pool's exact absolute balance rather
        than a delta against whatever cumulative state other tests left."""
        from ledger import db
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(path)
        self.addCleanup(os.remove, path)
        return db, path

    def test_cannot_challenge_without_enough_real_balance(self):
        bot = self._register("challenger_a")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc", "entry_fee_cents": 500}, headers=headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("insufficient", resp.get_json()["error"])

    def test_entry_fee_bounds_enforced(self):
        bot = self._register("challenger_b")
        self._fund(bot["id"], 100000)
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc", "entry_fee_cents": 1}, headers=headers)
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc", "entry_fee_cents": 999999}, headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_game_type_rejected(self):
        bot = self._register("challenger_c")
        self._fund(bot["id"], 100000)
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "chess"}, headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_low_practice_balance_cannot_bust_a_paid_challenge(self):
        """A challenger's practice balance is a completely separate,
        unrelated number from the real entry fee they just paid -- if it
        happens to be near zero (e.g. they lost a bunch of unrelated
        practice matches earlier), the challenge they paid real money for
        must still actually get to play out its full length, not
        instantly end in a bust before a single hand."""
        bot = self._register("challenger_zero_balance")
        self._fund(bot["id"], 100000)
        from ledger import db
        with db.connect(os.environ["ARENA_DB_PATH"]) as conn:
            conn.execute("UPDATE bots SET balance = 0 WHERE id = ?", (bot["id"],))
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc", "entry_fee_cents": 500}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        body = resp.get_json()

        state_resp = self.client.get(f"/matches/{body['match_id']}/state", headers=headers)
        # A busted-immediately match would 404 here (the live match row
        # is deleted the instant it finishes) instead of coming back as a
        # genuinely playable hand -- this is the actual bug signature.
        self.assertEqual(state_resp.status_code, 200, state_resp.get_json())
        state = state_resp.get_json()
        self.assertFalse(state.get("match_done", False))
        self.assertIn("legal_actions", state)
        self.assertIn("your_turn", state)

        # And the challenger's practice balance was actually topped up,
        # not left at the 0 it was forced to before the challenge.
        board = self.client.get("/leaderboard").get_json()
        row = next(b for b in board["bots"] if b["id"] == bot["id"])
        self.assertGreater(row["balance"], 0)

    def test_entry_fee_is_charged_immediately_and_capped(self):
        """The entry fee is the ONLY real money at risk -- the challenge
        match itself plays with practice chips, so a challenger can never
        lose more real value than the fee they agreed to pay."""
        bot = self._register("challenger_d")
        self._fund(bot["id"], 100000)
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc", "entry_fee_cents": 500}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        body = resp.get_json()

        board = self.client.get("/leaderboard").get_json()
        row = next(b for b in board["bots"] if b["id"] == bot["id"])
        self.assertEqual(row["real_balance"], 100000 - 500)  # exactly the fee, no more
        self.assertEqual(body["prize_cents"], 500 * BOSS_CHALLENGE_PRIZE_MULTIPLIER)

        # win or lose the underlying (practice-chip) match, real_balance
        # cannot move any further from this challenge except the funded prize.
        match_id = body["match_id"]
        rng = random.Random(9)
        for _ in range(4000):
            state = self.client.get(f"/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done"):
                break
            if state.get("your_turn"):
                action = _pick_leduc_action(state["legal_actions"])
                resp2 = self.client.post(f"/matches/{match_id}/action", json={"action": action}, headers=headers)
                if resp2.get_json().get("match_done"):
                    break
        board_after = self.client.get("/leaderboard").get_json()
        row_after = next(b for b in board_after["bots"] if b["id"] == bot["id"])
        # real_balance only changes by (at most) the funded prize on a win
        self.assertIn(row_after["real_balance"], (100000 - 500, 100000 - 500 + 500 * BOSS_CHALLENGE_PRIZE_MULTIPLIER))

    def test_challenge_resolves_and_pays_out_a_funded_win(self):
        """Tests the ledger-level resolution function directly (rather
        than trying to force a win against a genuinely hard bot in a
        unit test), on its own isolated DB so the prize pool's absolute
        balance is exactly known rather than a cumulative total shared
        with every other test in this file."""
        db, path = self._fresh_ledger_db()
        with db.connect(path) as conn:
            db.fund_prize_pool(conn, 1_000_000, "test seed")
            bot_id = db.create_bot(conn, "challenger_e", starting_balance=1000)["id"]
            opponent_id = db.create_bot(conn, "throwaway_opponent_e", starting_balance=0)["id"]
            db.credit_real_balance(conn, bot_id, 100000, "test funding")
            dummy_match_id = db.create_match(conn, bot_id, opponent_id, 1, 0)
            db.pay_challenge_entry_fee(conn, bot_id, 500)
            challenge = db.create_boss_challenge(conn, bot_id, "leduc", "cfr_bot", match_id=dummy_match_id, entry_fee_cents=500, prize_cents=2500, win_condition="net_positive_over_challenge")
            resolved = db.resolve_boss_challenge(conn, challenge, won=True)
            self.assertEqual(resolved["status"], "won")
            bot_row = db.get_bot(conn, bot_id)
            self.assertEqual(bot_row["real_balance"], 100000 - 500 + 2500)
            self.assertEqual(db.prize_pool_balance(conn), 1_000_000 - 2500)

            with self.assertRaises(ValueError):
                db.resolve_boss_challenge(conn, challenge, won=True)  # can't resolve twice

    def test_challenge_loss_does_not_touch_prize_pool(self):
        db, path = self._fresh_ledger_db()
        with db.connect(path) as conn:
            db.fund_prize_pool(conn, 1_000_000, "test seed")
            bot_id = db.create_bot(conn, "challenger_f", starting_balance=1000)["id"]
            opponent_id = db.create_bot(conn, "throwaway_opponent_f", starting_balance=0)["id"]
            db.credit_real_balance(conn, bot_id, 100000, "test funding")
            pool_before = db.prize_pool_balance(conn)
            dummy_match_id = db.create_match(conn, bot_id, opponent_id, 1, 0)
            db.pay_challenge_entry_fee(conn, bot_id, 500)
            challenge = db.create_boss_challenge(conn, bot_id, "leduc", "cfr_bot", match_id=dummy_match_id, entry_fee_cents=500, prize_cents=2500, win_condition="net_positive_over_challenge")
            db.resolve_boss_challenge(conn, challenge, won=False)
            bot_row = db.get_bot(conn, bot_id)
            self.assertEqual(bot_row["real_balance"], 100000 - 500)  # unchanged by the loss itself
            self.assertEqual(db.prize_pool_balance(conn), pool_before)

    def test_win_without_enough_funded_pool_pays_only_whats_available(self):
        """Honest degrade: if the pool can't cover the full prize, it
        pays what it actually has rather than pretending to pay more."""
        db, path = self._fresh_ledger_db()
        with db.connect(path) as conn:
            db.fund_prize_pool(conn, 1000, "test seed")
            bot_id = db.create_bot(conn, "challenger_g", starting_balance=1000)["id"]
            opponent_id = db.create_bot(conn, "throwaway_opponent_g", starting_balance=0)["id"]
            db.credit_real_balance(conn, bot_id, 100000, "test funding")
            dummy_match_id = db.create_match(conn, bot_id, opponent_id, 1, 0)
            db.pay_challenge_entry_fee(conn, bot_id, 500)
            challenge = db.create_boss_challenge(conn, bot_id, "leduc", "cfr_bot", match_id=dummy_match_id, entry_fee_cents=500, prize_cents=2500, win_condition="net_positive_over_challenge")
            db.resolve_boss_challenge(conn, challenge, won=True)
            bot_row = db.get_bot(conn, bot_id)
            self.assertEqual(bot_row["real_balance"], 100000 - 500 + 1000)  # only the 1000 available
            self.assertEqual(db.prize_pool_balance(conn), 0)

    def test_end_to_end_challenge_via_api_resolves_when_match_finishes(self):
        """Full path through the real API/ledger wiring (not a direct
        ledger call): create a challenge, play it out, and confirm the
        challenge row transitions out of 'pending' on its own once the
        underlying match completes."""
        self._fund_prize_pool(1_000_000)
        bot = self._register("challenger_h")
        self._fund(bot["id"], 100000)
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "duel", "entry_fee_cents": 100}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        challenge_id = resp.get_json()["challenge_id"]
        match_id = resp.get_json()["match_id"]

        rng = random.Random(11)
        for _ in range(6000):
            state = self.client.get(f"/duel/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done"):
                break
            if state.get("your_turn"):
                move = rng.choice(state["legal_actions"])
                resp2 = self.client.post(f"/duel/matches/{match_id}/action", json={"move": move}, headers=headers)
                if resp2.get_json().get("match_done"):
                    break
        else:
            self.fail("challenge match never finished")

        status = self.client.get(f"/challenges/{challenge_id}", headers=headers).get_json()
        self.assertIn(status["status"], ("won", "lost"))
        self.assertIsNotNone(status["resolved_at"])

    def test_others_cannot_view_your_challenge(self):
        bot_a = self._register("challenger_i")
        bot_b = self._register("challenger_j")
        self._fund(bot_a["id"], 100000)
        headers_a = {"X-API-Key": bot_a["api_key"]}
        headers_b = {"X-API-Key": bot_b["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc", "entry_fee_cents": 100}, headers=headers_a)
        challenge_id = resp.get_json()["challenge_id"]
        resp2 = self.client.get(f"/challenges/{challenge_id}", headers=headers_b)
        self.assertEqual(resp2.status_code, 403)
        resp3 = self.client.get(f"/challenges/{challenge_id}", headers={"X-Admin-Secret": ADMIN_SECRET})
        self.assertEqual(resp3.status_code, 200)


if __name__ == "__main__":
    unittest.main()
