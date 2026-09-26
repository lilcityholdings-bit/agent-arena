"""Smoke tests for the HTTP API using Flask's test client (no real server
needed). Covers: registering bots, a full match against a baseline bot,
and a real remote-vs-remote match driven by two independent api keys."""
import os
import sys
import tempfile
import unittest

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH
os.environ.pop("ARENA_ADMIN_SECRET", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import create_app  # noqa: E402

ADMIN_SECRET = "test-api-admin-secret-do-not-use-in-prod"


def _pick_action(legal: list[str]) -> str:
    """Prefer raise, so pots -- and rake -- actually grow; this is just a
    fixed test strategy to exercise the wire protocol, not a real bot."""
    for choice in ("raise", "call", "check"):
        if choice in legal:
            return choice
    return legal[0]


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
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

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.get_json()["status"], "ok")

    def test_full_match_against_baseline_bot(self):
        bot = self._register("smoketest_bot_a")
        headers = {"X-API-Key": bot["api_key"]}

        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 20}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        match_id = resp.get_json()["match_id"]

        # Play the match to completion, always checking (a simple fixed
        # strategy is enough to prove the wire protocol round-trips).
        for _ in range(2000):
            resp = self.client.get(f"/matches/{match_id}/state", headers=headers)
            data = resp.get_json()
            if data.get("match_done"):
                break
            if not data["your_turn"]:
                # Shouldn't happen against a baseline opponent -- baseline
                # turns are auto-played server-side -- but guard anyway.
                continue
            action = _pick_action(data["legal_actions"])
            resp = self.client.post(f"/matches/{match_id}/action", json={"action": action}, headers=headers)
            self.assertEqual(resp.status_code, 200, resp.get_json())
            if resp.get_json().get("match_done"):
                break
        else:
            self.fail("match did not finish in a reasonable number of steps")

        summary = self.client.get(f"/matches/{match_id}").get_json()
        self.assertEqual(len(summary["hands"]), 20)

        # No rake: every chip one side lost, the other side won.
        self.assertEqual(sum(h["rake"] for h in summary["hands"]), 0)
        self.assertEqual(sum(h["payoff_seat0"] + h["payoff_seat1"] for h in summary["hands"]), 0)

    def test_remote_vs_remote_match_both_sides_poll(self):
        bot_a = self._register("smoketest_bot_b")
        bot_b = self._register("smoketest_bot_c")
        headers_a = {"X-API-Key": bot_a["api_key"]}
        headers_b = {"X-API-Key": bot_b["api_key"]}

        resp = self.client.post("/matches", json={"opponent": str(bot_b["id"]), "hands": 10}, headers=headers_a)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        match_id = resp.get_json()["match_id"]

        headers_by_seat_owner = {"a": headers_a, "b": headers_b}
        for _ in range(2000):
            done = False
            for owner, hdrs in headers_by_seat_owner.items():
                state = self.client.get(f"/matches/{match_id}/state", headers=hdrs).get_json()
                if state.get("match_done"):
                    done = True
                    break
                if state["your_turn"]:
                    action = _pick_action(state["legal_actions"])
                    resp = self.client.post(f"/matches/{match_id}/action", json={"action": action}, headers=hdrs)
                    self.assertEqual(resp.status_code, 200, resp.get_json())
                    if resp.get_json().get("match_done"):
                        done = True
                    break
            if done:
                break
        else:
            self.fail("remote-vs-remote match did not finish in a reasonable number of steps")

        summary = self.client.get(f"/matches/{match_id}").get_json()
        self.assertEqual(len(summary["hands"]), 10)

    def test_bot_vs_bot_match_is_free(self):
        """Playing another bot is free and uses practice chips, same as the computer."""
        bot_a = self._register("smoketest_bot_wager_a")
        bot_b = self._register("smoketest_bot_wager_b")
        headers_a = {"X-API-Key": bot_a["api_key"]}

        resp = self.client.post("/matches", json={"opponent": str(bot_b["id"]), "hands": 10}, headers=headers_a)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        self.assertEqual(resp.get_json()["currency"], "practice_chips")

    def test_playing_the_computer_is_free_by_default(self):
        """The other half of the same policy: no currency needs to be
        specified (and none should be required) to play a baseline bot --
        it just works with the free practice_chips default."""
        bot = self._register("smoketest_bot_free_computer")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/matches", json={"opponent": "heuristic_bot", "hands": 1}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        self.assertEqual(resp.get_json()["currency"], "practice_chips")

    def test_baseline_bot_by_id_is_still_recognized_as_the_computer(self):
        """A baseline bot is also just a row with a real id in `bots` --
        resolving an opponent by that id (instead of by its reserved
        name) must still count as "the computer" for wager-policy
        purposes, not accidentally look like a real opponent."""
        bot = self._register("smoketest_bot_baseline_by_id")
        headers = {"X-API-Key": bot["api_key"]}
        # Registering a bot against random_bot ensures the baseline bots
        # exist as real rows (created lazily on first use); then look its
        # id up via the leaderboard and reuse that id directly.
        resp = self.client.post("/matches", json={"opponent": "random_bot", "hands": 1}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        board = self.client.get("/leaderboard").get_json()
        random_bot_row = next(b for b in board["bots"] if b["name"] == "random_bot")
        baseline_id = random_bot_row["id"]

        resp = self.client.post("/matches", json={"opponent": str(baseline_id), "hands": 1}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        self.assertEqual(resp.get_json()["opponent"], "random_bot")


if __name__ == "__main__":
    unittest.main()
