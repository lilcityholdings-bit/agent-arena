"""Duel over HTTP: registers bots, plays a full direct match against a
baseline, and a full lobby-matched fight between two real (in-test)
bots -- the same two paths test_api.py and test_durability_and_lobby.py
cover for Leduc, adapted for Duel's simultaneous-submit action shape.
Also checks the two games stay out of each other's way when they share
the bots table, the lobby, and live-match durability."""
import os
import random
import sys
import tempfile
import unittest

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH
os.environ.pop("ARENA_ADMIN_SECRET", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import create_app, LIVE_DUEL_MATCHES, LIVE_MATCHES  # noqa: E402

ADMIN_SECRET = "test-duel-admin-secret-do-not-use-in-prod"


class TestDuelAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        os.remove(_TEST_DB_PATH)

    def setUp(self):
        self.client = self.app.test_client()
        LIVE_DUEL_MATCHES.clear()
        LIVE_MATCHES.clear()

    def _register(self, name):
        return self.client.post("/bots", json={"name": name}).get_json()

    def _fund(self, bot, amount_cents=10000):
        """No-op: bot-vs-bot play is free now (practice chips only)."""

    def test_cannot_register_a_duel_baseline_name(self):
        resp = self.client.post("/bots", json={"name": "boss_duel_bot"})
        self.assertEqual(resp.status_code, 400)

    def test_full_match_against_random_duel_bot(self):
        bot = self._register("duel_api_bot_a")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/duel/matches", json={"opponent": "random_duel_bot", "fights": 3, "rake_bps": 500}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        match_id = resp.get_json()["match_id"]

        rng = random.Random(1)
        for _ in range(2000):
            state = self.client.get(f"/duel/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done"):
                break
            move = rng.choice(state["legal_actions"])
            resp = self.client.post(f"/duel/matches/{match_id}/action", json={"move": move}, headers=headers)
            body = resp.get_json()
            self.assertEqual(resp.status_code, 200, body)
            if body.get("match_done"):
                break
        else:
            self.fail("duel match never finished")

        summary = self.client.get(f"/duel/matches/{match_id}").get_json()
        self.assertEqual(len(summary["fights"]), 3)
        for f in summary["fights"]:
            self.assertEqual(f["payoff_seat0"] + f["payoff_seat1"] + f["rake"], 0)

    def test_illegal_move_is_rejected_with_legal_actions_listed(self):
        bot = self._register("duel_api_bot_b")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/duel/matches", json={"opponent": "random_duel_bot", "fights": 1}, headers=headers)
        match_id = resp.get_json()["match_id"]
        resp = self.client.post(f"/duel/matches/{match_id}/action", json={"move": "flying_kick"}, headers=headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("legal_actions", resp.get_json())

    def test_two_real_bots_matched_via_duel_lobby(self):
        # Competitive (bot vs bot) lobby pairing requires a real usd
        # wager -- a practice_chips join is matched against the computer
        # instantly instead (see test_lobby_join_is_free_and_instant_
        # against_the_computer below), so this needs currency=usd and
        # both sides funded.
        bot_a = self._register("duel_api_bot_c")
        bot_b = self._register("duel_api_bot_d")
        self._fund(bot_a)
        self._fund(bot_b)
        headers_a = {"X-API-Key": bot_a["api_key"]}
        headers_b = {"X-API-Key": bot_b["api_key"]}

        resp = self.client.post("/duel/lobby/join", json={"fights": 2, "rake_bps": 0}, headers=headers_a)
        self.assertEqual(resp.get_json()["matched"], False)

        resp = self.client.post("/duel/lobby/join", json={"fights": 2, "rake_bps": 0}, headers=headers_b)
        self.assertEqual(resp.get_json()["matched"], True)
        match_id = resp.get_json()["match_id"]

        status = self.client.get("/duel/lobby/status", headers=headers_a).get_json()
        self.assertEqual(status["matched"], True)
        self.assertEqual(status["match_id"], match_id)

        rng = random.Random(2)
        headers_by_seat = {"a": headers_a, "b": headers_b}
        for _ in range(3000):
            done = False
            for hdrs in headers_by_seat.values():
                state = self.client.get(f"/duel/matches/{match_id}/state", headers=hdrs).get_json()
                if state.get("match_done"):
                    done = True
                    break
                if state["your_turn"]:
                    move = rng.choice(state["legal_actions"])
                    resp = self.client.post(f"/duel/matches/{match_id}/action", json={"move": move}, headers=hdrs)
                    if resp.get_json().get("match_done"):
                        done = True
                    break
            if done:
                break
        else:
            self.fail("lobby-matched duel never finished")

        summary = self.client.get(f"/duel/matches/{match_id}").get_json()
        self.assertEqual(len(summary["fights"]), 2)

    def test_leduc_and_duel_lobbies_dont_cross_match(self):
        """A bot waiting in the duel lobby must not be handed a leduc
        match (and vice versa) just because both lobbies share a table.
        Uses currency=usd for both joins -- a practice_chips join never
        waits in the lobby at all (it's matched against the computer
        instantly), so it wouldn't exercise this cross-game check."""
        bot_a = self._register("duel_api_bot_e")
        bot_b = self._register("duel_api_bot_f")
        self._fund(bot_a)
        self._fund(bot_b)
        headers_a = {"X-API-Key": bot_a["api_key"]}
        headers_b = {"X-API-Key": bot_b["api_key"]}

        self.client.post("/duel/lobby/join", json={"fights": 5, "rake_bps": 100}, headers=headers_a)
        resp = self.client.post("/lobby/join", json={"hands": 5, "rake_bps": 100}, headers=headers_b)
        # bot_b joined the LEDUC lobby with matching hands/rake but bot_a
        # is waiting in the DUEL lobby -- they must not be paired.
        self.assertEqual(resp.get_json()["matched"], False)

    def test_duel_lobby_join_is_free_and_instant_against_the_computer(self):
        """Asking for the computer skips the queue."""
        bot = self._register("duel_api_bot_free_computer")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/duel/lobby/join", json={"fights": 2, "vs_computer": True}, headers=headers)
        data = resp.get_json()
        self.assertTrue(data["matched"])
        self.assertIn(data["opponent"], ("random_duel_bot", "heuristic_duel_bot", "boss_duel_bot"))

    def test_duel_against_another_bot_is_free(self):
        """Bot-vs-bot duels are free and played for practice chips."""
        bot_a = self._register("duel_api_bot_wager_a")
        bot_b = self._register("duel_api_bot_wager_b")
        headers_a = {"X-API-Key": bot_a["api_key"]}
        resp = self.client.post("/duel/matches", json={"opponent": str(bot_b["id"]), "fights": 2}, headers=headers_a)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        self.assertEqual(resp.get_json()["currency"], "practice_chips")

    def test_duel_match_survives_a_simulated_restart(self):
        """Same proof as test_durability_and_lobby.py's leduc version:
        clearing the in-memory cache (the only thing a real restart would
        lose) must not lose the match -- it reloads from SQLite."""
        bot = self._register("duel_api_bot_h")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/duel/matches", json={"opponent": "random_duel_bot", "fights": 5}, headers=headers)
        match_id = resp.get_json()["match_id"]

        rng = random.Random(3)
        for _ in range(3):
            state = self.client.get(f"/duel/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done"):
                break
            move = rng.choice(state["legal_actions"])
            self.client.post(f"/duel/matches/{match_id}/action", json={"move": move}, headers=headers)

        self.assertIn(match_id, LIVE_DUEL_MATCHES)
        LIVE_DUEL_MATCHES.clear()  # simulate a restart

        for _ in range(3000):
            state = self.client.get(f"/duel/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done"):
                break
            move = rng.choice(state["legal_actions"])
            resp = self.client.post(f"/duel/matches/{match_id}/action", json={"move": move}, headers=headers)
            if resp.get_json().get("match_done"):
                break
        else:
            self.fail("duel match never finished after simulated restart")

        summary = self.client.get(f"/duel/matches/{match_id}").get_json()
        self.assertEqual(len(summary["fights"]), 5)

    def test_lobby_does_not_pair_mismatched_stakes(self):
        """A bot that wants a 50-chip entry stake must never be silently
        paired into a 10-chip fight (or vice versa) just because another
        bot happened to complete the pairing -- the matching bug this
        guards against: whichever side's /join call found the opponent
        used to decide the stake for BOTH sides, discarding the other's."""
        # Stake matching only matters for real bot-vs-bot pairing, which
        # now needs currency=usd (see test_two_real_bots_matched_via_
        # duel_lobby) -- a practice_chips join skips pairing altogether.
        bot_a = self._register("duel_api_bot_stake_a")
        bot_b = self._register("duel_api_bot_stake_b")
        bot_c = self._register("duel_api_bot_stake_c")
        for bot in (bot_a, bot_b, bot_c):
            self._fund(bot)
        headers_a = {"X-API-Key": bot_a["api_key"]}
        headers_b = {"X-API-Key": bot_b["api_key"]}
        headers_c = {"X-API-Key": bot_c["api_key"]}

        self.client.post("/duel/lobby/join", json={"fights": 3, "rake_bps": 0, "stake": 50}, headers=headers_a)
        resp = self.client.post("/duel/lobby/join", json={"fights": 3, "rake_bps": 0, "stake": 10}, headers=headers_b)
        self.assertEqual(resp.get_json()["matched"], False)  # different stake -- must not pair

        resp2 = self.client.post("/duel/lobby/join", json={"fights": 3, "rake_bps": 0, "stake": 50}, headers=headers_c)
        self.assertEqual(resp2.get_json()["matched"], True)  # same stake as bot_a -- must pair

    def test_leaderboard_reflects_both_games(self):
        bot = self._register("duel_api_bot_g")
        headers = {"X-API-Key": bot["api_key"]}
        self.client.post("/duel/matches", json={"opponent": "random_duel_bot", "fights": 1}, headers=headers)
        board = self.client.get("/leaderboard").get_json()
        row = next(b for b in board["bots"] if b["id"] == bot["id"])
        self.assertIn("balance", row)  # shared account, not a duel-specific balance field


if __name__ == "__main__":
    unittest.main()
