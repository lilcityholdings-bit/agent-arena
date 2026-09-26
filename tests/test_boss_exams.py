"""Boss exams: a free, fixed-length match against the strongest baseline bot
for a game. Passing means finishing net chips positive over the whole exam.
Nothing is paid in or out. These tests cover that exams are free, can't be
busted by a low balance, resolve exactly once, and are private to their bot."""
import os
import random
import sys
import tempfile
import unittest

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import create_app, LIVE_MATCHES, LIVE_DUEL_MATCHES  # noqa: E402
from ledger import db  # noqa: E402

ADMIN_SECRET = "test-admin-secret"


class TestBossExams(unittest.TestCase):
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
        resp = self.client.post("/bots", json={"name": name})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def test_an_exam_is_free(self):
        bot = self._register("examinee_free")
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc", "entry_fee_cents": 500},
                                headers={"X-API-Key": bot["api_key"]})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        body = resp.get_json()
        self.assertNotIn("entry_fee_cents", body)
        self.assertNotIn("prize_cents", body)
        self.assertEqual(body["boss"], "cfr_bot")

    def test_unknown_game_type_rejected(self):
        bot = self._register("examinee_chess")
        resp = self.client.post("/challenges/boss", json={"game_type": "chess"}, headers={"X-API-Key": bot["api_key"]})
        self.assertEqual(resp.status_code, 400)

    def test_a_low_practice_balance_cannot_bust_an_exam(self):
        bot = self._register("examinee_broke")
        with db.connect() as conn:
            conn.execute("UPDATE bots SET balance = 0 WHERE id = ?", (bot["id"],))
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc"}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        state = self.client.get(f"/matches/{resp.get_json()['match_id']}/state", headers=headers)
        self.assertEqual(state.status_code, 200, "the exam must actually be playable")
        self.assertFalse(state.get_json().get("match_done", False))

    def test_an_exam_resolves_once_when_its_match_finishes(self):
        bot = self._register("examinee_duel")
        headers = {"X-API-Key": bot["api_key"]}
        resp = self.client.post("/challenges/boss", json={"game_type": "duel"}, headers=headers)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        challenge_id, match_id = resp.get_json()["challenge_id"], resp.get_json()["match_id"]
        rng = random.Random(11)
        for _ in range(6000):
            state = self.client.get(f"/duel/matches/{match_id}/state", headers=headers).get_json()
            if state.get("match_done"):
                break
            if state.get("your_turn"):
                r = self.client.post(f"/duel/matches/{match_id}/action", json={"move": rng.choice(state["legal_actions"])}, headers=headers)
                if r.get_json().get("match_done"):
                    break
        else:
            self.fail("exam match never finished")
        status = self.client.get(f"/challenges/{challenge_id}", headers=headers).get_json()
        self.assertIn(status["status"], ("won", "lost"))
        self.assertIsNotNone(status["resolved_at"])
        with db.connect() as conn:
            with self.assertRaises(ValueError):
                db.resolve_boss_challenge(conn, challenge_id, won=True)

    def test_others_cannot_view_your_exam(self):
        a = self._register("examinee_a")
        b = self._register("examinee_b")
        resp = self.client.post("/challenges/boss", json={"game_type": "leduc"}, headers={"X-API-Key": a["api_key"]})
        challenge_id = resp.get_json()["challenge_id"]
        self.assertEqual(self.client.get(f"/challenges/{challenge_id}", headers={"X-API-Key": b["api_key"]}).status_code, 403)
        self.assertEqual(self.client.get(f"/challenges/{challenge_id}", headers={"X-Admin-Secret": ADMIN_SECRET}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
