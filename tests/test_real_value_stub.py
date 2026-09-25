"""Real value has two honest paths in and one honest way to play it, and
nothing else: an admin-attested credit/debit (recording value that moved
outside the app), and a currency="usd" match (which stakes real_balance
instead of practice chips, with the same bankroll caps and rake as any
other match). /deposit and /withdraw remain 501 stubs -- they mean
"automatic, self-service, on-chain", which still isn't built. These
tests lock in "refuses honestly by default, works correctly when
actually authorized" as the contract."""
import os
import sys
import tempfile
import unittest

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH
os.environ.pop("ARENA_ADMIN_SECRET", None)  # start with admin actions disabled

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import create_app  # noqa: E402

ADMIN_SECRET = "test-admin-secret-do-not-use-in-prod"


class TestRealValueStub(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        os.remove(_TEST_DB_PATH)

    def setUp(self):
        self.client = self.app.test_client()
        os.environ.pop("ARENA_ADMIN_SECRET", None)

    def tearDown(self):
        os.environ.pop("ARENA_ADMIN_SECRET", None)

    def _register(self, name):
        resp = self.client.post("/bots", json={"name": name})
        return resp.get_json()

    def test_new_bot_has_zero_real_balance(self):
        bot = self._register("real_value_bot_a")
        self.assertEqual(bot["real_balance"], 0)
        board = self.client.get("/leaderboard").get_json()
        row = next(b for b in board["bots"] if b["id"] == bot["id"])
        self.assertEqual(row["real_balance"], 0)

    def test_deposit_and_withdraw_are_honest_501_stubs(self):
        bot = self._register("real_value_bot_b")
        headers = {"X-API-Key": bot["api_key"]}

        dep = self.client.post(f"/bots/{bot['id']}/deposit", json={"amount": 100}, headers=headers)
        self.assertEqual(dep.status_code, 501)
        self.assertIn("not implemented", dep.get_json()["error"])

        wd = self.client.post(f"/bots/{bot['id']}/withdraw", json={"amount": 100}, headers=headers)
        self.assertEqual(wd.status_code, 501)
        self.assertIn("not implemented", wd.get_json()["error"])

        board = self.client.get("/leaderboard").get_json()
        row = next(b for b in board["bots"] if b["id"] == bot["id"])
        self.assertEqual(row["real_balance"], 0)
        self.assertEqual(row["balance"], 1000)

    def test_cannot_deposit_into_someone_elses_account(self):
        bot_a = self._register("real_value_bot_c")
        bot_b = self._register("real_value_bot_d")
        headers_a = {"X-API-Key": bot_a["api_key"]}
        resp = self.client.post(f"/bots/{bot_b['id']}/deposit", json={"amount": 100}, headers=headers_a)
        self.assertEqual(resp.status_code, 403)

    def test_match_rejects_unsupported_currency(self):
        bot = self._register("real_value_bot_e")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post(
            "/matches",
            json={"opponent": "random_bot", "hands": 5, "currency": "usdc"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("practice_chips", resp.get_json()["error"])

    def test_match_with_default_currency_still_works(self):
        bot = self._register("real_value_bot_f")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 5}, headers=headers)
        self.assertEqual(resp.status_code, 201)

    # -- admin credit/debit: fail-closed by default, correct when authorized --

    def test_admin_actions_refused_when_secret_not_configured(self):
        bot = self._register("real_value_bot_g")
        resp = self.client.post(
            f"/admin/bots/{bot['id']}/credit",
            json={"amount_cents": 500, "note": "test"},
            headers={"X-Admin-Secret": "anything"},
        )
        self.assertEqual(resp.status_code, 503)

    def test_admin_credit_requires_correct_secret(self):
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        bot = self._register("real_value_bot_h")
        resp = self.client.post(
            f"/admin/bots/{bot['id']}/credit",
            json={"amount_cents": 500, "note": "test"},
            headers={"X-Admin-Secret": "wrong-secret"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_admin_credit_and_debit_with_correct_secret(self):
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        bot = self._register("real_value_bot_i")
        headers = {"X-Admin-Secret": ADMIN_SECRET}

        resp = self.client.post(f"/admin/bots/{bot['id']}/credit", json={"amount_cents": 5000, "note": "bank transfer #1"}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["real_balance"], 5000)

        resp = self.client.post(f"/admin/bots/{bot['id']}/debit", json={"amount_cents": 2000, "note": "payout #1"}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["real_balance"], 3000)

        # a credit/debit without a note is refused -- there must always be
        # a stated reason behind a real-value ledger entry
        resp = self.client.post(f"/admin/bots/{bot['id']}/credit", json={"amount_cents": 100}, headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_admin_debit_refuses_to_go_negative(self):
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        bot = self._register("real_value_bot_j")
        headers = {"X-Admin-Secret": ADMIN_SECRET}
        resp = self.client.post(f"/admin/bots/{bot['id']}/debit", json={"amount_cents": 100, "note": "test"}, headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_bot_can_view_own_transactions_but_not_someone_elses(self):
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        bot_a = self._register("real_value_bot_k")
        bot_b = self._register("real_value_bot_l")
        self.client.post(
            f"/admin/bots/{bot_a['id']}/credit",
            json={"amount_cents": 1000, "note": "test"},
            headers={"X-Admin-Secret": ADMIN_SECRET},
        )

        own = self.client.get(f"/bots/{bot_a['id']}/transactions", headers={"X-API-Key": bot_a["api_key"]})
        self.assertEqual(own.status_code, 200)
        self.assertEqual(len(own.get_json()["transactions"]), 1)

        others = self.client.get(f"/bots/{bot_a['id']}/transactions", headers={"X-API-Key": bot_b["api_key"]})
        self.assertEqual(others.status_code, 403)

        as_admin = self.client.get(f"/bots/{bot_a['id']}/transactions", headers={"X-Admin-Secret": ADMIN_SECRET})
        self.assertEqual(as_admin.status_code, 200)

    # -- an actual usd match, played end to end --

    def test_usd_match_cannot_use_a_baseline_opponent(self):
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        bot = self._register("real_value_bot_m")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 5, "currency": "usd"}, headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_usd_match_end_to_end_conserves_real_value(self):
        os.environ["ARENA_ADMIN_SECRET"] = ADMIN_SECRET
        admin_headers = {"X-Admin-Secret": ADMIN_SECRET}
        bot_a = self._register("real_value_bot_n")
        bot_b = self._register("real_value_bot_o")
        for bot in (bot_a, bot_b):
            resp = self.client.post(f"/admin/bots/{bot['id']}/credit", json={"amount_cents": 10000, "note": "test funding"}, headers=admin_headers)
            self.assertEqual(resp.status_code, 200)

        headers_a = {"X-API-Key": bot_a["api_key"]}
        headers_b = {"X-API-Key": bot_b["api_key"]}
        resp = self.client.post(
            "/matches",
            json={"opponent": str(bot_b["id"]), "hands": 20, "rake_bps": 500, "currency": "usd", "unit_value_cents": 25},
            headers=headers_a,
        )
        self.assertEqual(resp.status_code, 201, resp.get_json())
        match_id = resp.get_json()["match_id"]
        self.assertEqual(resp.get_json()["currency"], "usd")

        headers_by_owner = {"a": headers_a, "b": headers_b}
        for _ in range(4000):
            done = False
            for hdrs in headers_by_owner.values():
                state = self.client.get(f"/matches/{match_id}/state", headers=hdrs).get_json()
                if state.get("match_done"):
                    done = True
                    break
                if state.get("your_turn"):
                    legal = state["legal_actions"]
                    action = "raise" if "raise" in legal else ("call" if "call" in legal else "check")
                    resp = self.client.post(f"/matches/{match_id}/action", json={"action": action}, headers=hdrs)
                    if resp.get_json().get("match_done"):
                        done = True
                    break
            if done:
                break
        else:
            self.fail("usd match did not finish")

        board = self.client.get("/leaderboard").get_json()
        row_a = next(b for b in board["bots"] if b["id"] == bot_a["id"])
        row_b = next(b for b in board["bots"] if b["id"] == bot_b["id"])
        delta_a = row_a["real_balance"] - 10000
        delta_b = row_b["real_balance"] - 10000
        rake_collected = board["house_real_rake_collected_cents"]

        # real value is conserved exactly like practice chips: every cent
        # either moved between the two bots or went to the house as rake.
        self.assertEqual(delta_a + delta_b + rake_collected, 0)
        # practice-chip balances must be completely untouched by a usd match
        self.assertEqual(row_a["balance"], 1000)
        self.assertEqual(row_b["balance"], 1000)

        # each bot's own transaction log accounts for its real_balance delta
        own_log = self.client.get(f"/bots/{bot_a['id']}/transactions", headers=headers_a).get_json()
        logged_total = sum(t["delta_cents"] for t in own_log["transactions"] if t["reason"] == "hand_settlement")
        self.assertEqual(logged_total, delta_a)


if __name__ == "__main__":
    unittest.main()
