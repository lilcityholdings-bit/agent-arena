"""The easy play layer (/play, /mcp, agent docs) and the safety rules around it:
turn clock and forfeits, consent for named challenges, provably fair deals,
ratings that can't be farmed, and request limits."""
import hashlib
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH
os.environ["ARENA_ADMIN_SECRET"] = "test-admin-secret"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.app as arena  # noqa: E402
from api import play  # noqa: E402
from api.app import create_app  # noqa: E402
from engine.cards import deck_from_seeds  # noqa: E402

_counter = [0]


class PlayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        os.remove(_TEST_DB_PATH)

    def setUp(self):
        self.client = self.app.test_client()

    def register(self, prefix="bot", ip=None):
        _counter[0] += 1
        headers = {"X-Forwarded-For": ip} if ip else {}
        r = self.client.post("/bots", json={"name": f"{prefix}-{_counter[0]}"}, headers=headers)
        self.assertEqual(r.status_code, 201, r.get_json())
        return {"X-API-Key": r.get_json()["api_key"], "name": r.get_json()["name"]}

    def hdr(self, bot):
        return {"X-API-Key": bot["X-API-Key"]}

    def play_out(self, bot, state, pick=None):
        pick = pick or (lambda s: random.Random(1).choice(s["legal_moves"]))
        for _ in range(2000):
            if state["status"] == "match_over":
                return state
            if state["status"] == "your_turn":
                r = self.client.post(f"/play/{state['match_id']}/move", json={"move": pick(state), "wait": 0}, headers=self.hdr(bot))
                self.assertEqual(r.status_code, 200, r.get_json())
                state = r.get_json()
            else:
                state = self.client.get(f"/play/{state['match_id']}?wait=0", headers=self.hdr(bot)).get_json()
        self.fail("match never ended")


class TestPlayBasics(PlayTest):
    def test_a_whole_poker_match_against_the_computer_in_one_simple_loop(self):
        bot = self.register()
        state = self.client.post("/play", json={"game": "poker", "opponent": "easy", "length": 10}, headers=self.hdr(bot)).get_json()
        self.assertEqual(state["status"], "your_turn")
        for key in ("legal_moves", "game_state", "how_to_move", "fairness", "seconds_left_to_move"):
            self.assertIn(key, state)
        final = self.play_out(bot, state)
        self.assertEqual(final["played"], 10)
        self.assertIn(final["result"], ("you won", "you lost", "draw"))
        self.assertIsNotNone(final["your_rating"], "10 hands against the computer is a rated match")

    def test_a_whole_duel_against_the_computer(self):
        bot = self.register()
        state = self.client.post("/play", json={"game": "duel", "opponent": "hard", "length": 3}, headers=self.hdr(bot)).get_json()
        self.assertEqual(state["status"], "your_turn")
        self.assertIn("your_hp", state["game_state"])
        final = self.play_out(bot, state)
        self.assertEqual(final["status"], "match_over")

    def test_each_hand_result_is_shown_and_the_deal_is_provably_fair(self):
        bot = self.register()
        state = self.client.post("/play", json={"game": "poker", "opponent": "easy", "length": 30, "client_seed": "myseed123"},
                                 headers=self.hdr(bot)).get_json()
        self.assertEqual(state["fairness"]["client_seed"], "myseed123")
        # Call down every hand until one reaches showdown.
        for _ in range(500):
            legal = state["legal_moves"]
            move = "call" if "call" in legal else "check"
            state = self.client.post(f"/play/{state['match_id']}/move", json={"move": move, "wait": 0}, headers=self.hdr(bot)).get_json()
            lr = state.get("last_result")
            if lr and lr["showdown"]:
                break
        self.assertTrue(lr and lr["showdown"])
        deck = deck_from_seeds(lr["server_seed"], lr["client_seed"])
        dealt = {str(deck[-1]), str(deck[-2])}
        self.assertEqual({lr["cards"]["you"], lr["cards"]["opponent"]}, dealt)
        self.assertEqual(lr["cards"]["board"], str(deck[-3]))
        v = self.client.get(f"/verify/poker?server_seed={lr['server_seed']}&client_seed={lr['client_seed']}").get_json()
        self.assertEqual(v["dealt"]["board_card"], lr["cards"]["board"])

    def test_the_first_hands_commitment_matches_its_revealed_seed(self):
        bot = self.register()
        state = self.client.post("/play", json={"game": "poker", "opponent": "easy", "length": 3}, headers=self.hdr(bot)).get_json()
        commitment = state["fairness"]["deck_commitment"]
        while state["status"] == "your_turn" and not state.get("last_result"):
            state = self.client.post(f"/play/{state['match_id']}/move", json={"move": "fold" if "fold" in state["legal_moves"] else state["legal_moves"][0], "wait": 0},
                                     headers=self.hdr(bot)).get_json()
        self.assertEqual(hashlib.sha256(state["last_result"]["server_seed"].encode()).hexdigest(), commitment)

    def test_moves_are_forgiving_and_errors_explain_themselves(self):
        bot = self.register()
        state = self.client.post("/play", json={"game": "poker", "opponent": "easy"}, headers=self.hdr(bot)).get_json()
        r = self.client.post(f"/play/{state['match_id']}/move", json={"move": "dance", "wait": 0}, headers=self.hdr(bot))
        self.assertEqual(r.status_code, 400)
        self.assertIn("legal_moves", r.get_json())
        move = "CHECK" if "check" in state["legal_moves"] else "Call"
        r = self.client.post(f"/play/{state['match_id']}/move", json={"move": move, "wait": 0}, headers=self.hdr(bot))
        self.assertEqual(r.status_code, 200, "moves are case-insensitive")
        self.assertEqual(self.client.post("/play", json={"game": "chess"}, headers=self.hdr(bot)).status_code, 400)
        self.assertEqual(self.client.post("/play", json={"game": "poker"}).status_code, 401)

    def test_names_are_validated(self):
        for bad in ("ab", "has space", "x" * 40, "cfr_bot", "<script>"):
            self.assertIn(self.client.post("/bots", json={"name": bad}).status_code, (400, 409), bad)
        bot = self.register("dupe")
        self.assertEqual(self.client.post("/bots", json={"name": bot["name"]}).status_code, 409)

    def test_playing_again_resumes_the_match_you_are_in(self):
        bot = self.register()
        a = self.client.post("/play", json={"game": "poker", "opponent": "easy"}, headers=self.hdr(bot)).get_json()
        b = self.client.post("/play", json={"game": "poker", "opponent": "hard"}, headers=self.hdr(bot)).get_json()
        self.assertEqual(a["match_id"], b["match_id"])


class TestMatchmaking(PlayTest):
    def test_two_bots_asking_for_anyone_are_paired(self):
        a, b = self.register(ip="10.0.0.1"), self.register(ip="10.0.0.2")
        wa = self.client.post("/play", json={"game": "duel", "wait": 0}, headers=self.hdr(a)).get_json()
        self.assertEqual(wa["status"], "waiting")
        sb = self.client.post("/play", json={"game": "duel", "wait": 0}, headers=self.hdr(b)).get_json()
        self.assertIn(sb["status"], ("your_turn", "waiting"))
        sa = self.client.get("/play?game=duel&wait=0", headers=self.hdr(a)).get_json()
        self.assertEqual(sa["match_id"], sb["match_id"])
        self.assertEqual(sa["opponent"], b["name"])
        self.assertFalse(sa["opponent_is_computer"])

    def test_a_lonely_bot_gets_the_computer_after_the_wait(self):
        a = self.register()
        self.assertEqual(self.client.post("/play", json={"game": "poker", "wait": 0}, headers=self.hdr(a)).get_json()["status"], "waiting")
        with mock.patch("time.time", return_value=__import__("time").time() + play.QUEUE_FALLBACK_SECONDS + 1):
            s = self.client.get("/play?game=poker&wait=0", headers=self.hdr(a)).get_json()
        self.assertTrue(s["opponent_is_computer"])

    def test_a_named_challenge_only_starts_when_both_bots_agree(self):
        a, b = self.register(ip="10.0.1.1"), self.register(ip="10.0.1.2")
        wa = self.client.post("/play", json={"game": "poker", "opponent": b["name"], "wait": 0}, headers=self.hdr(a)).get_json()
        self.assertEqual(wa["status"], "waiting")
        self.assertEqual(wa["waiting_for"], "the bot you challenged")
        with mock.patch("time.time", return_value=__import__("time").time() + 3600):
            still = self.client.get("/play?game=poker&wait=0", headers=self.hdr(a)).get_json()
        self.assertEqual(still["status"], "waiting", "a named challenge never falls back to the computer")
        invites = self.client.get("/play", headers=self.hdr(b)).get_json()["challenges_waiting_for_you"]
        self.assertEqual(invites[0]["from"], a["name"])
        sb = self.client.post("/play", json={"game": "poker", "opponent": a["name"], "wait": 0}, headers=self.hdr(b)).get_json()
        self.assertEqual(sb["opponent"], a["name"])

    def test_you_cant_play_yourself_or_a_bot_that_doesnt_exist(self):
        a = self.register()
        self.assertEqual(self.client.post("/play", json={"game": "poker", "opponent": a["name"]}, headers=self.hdr(a)).status_code, 400)
        self.assertEqual(self.client.post("/play", json={"game": "poker", "opponent": "nobody-here"}, headers=self.hdr(a)).status_code, 404)

    def test_leaving_the_queue(self):
        a = self.register()
        self.client.post("/play", json={"game": "duel", "wait": 0}, headers=self.hdr(a))
        self.assertTrue(self.client.delete("/play?game=duel", headers=self.hdr(a)).get_json()["left_queue"])
        self.assertEqual(self.client.get("/play?game=duel&wait=0", headers=self.hdr(a)).get_json()["status"], "idle")


class TestClockAndRatings(PlayTest):
    def _pair(self, game, ip_a, ip_b, length=None):
        a, b = self.register(ip=ip_a), self.register(ip=ip_b)
        body = {"game": game, "wait": 0}
        if length:
            body["length"] = length
        self.client.post("/play", json={**body, "opponent": b["name"]}, headers=self.hdr(a))
        self.client.post("/play", json={**body, "opponent": a["name"]}, headers=self.hdr(b))
        sa = self.client.get(f"/play?game={game}&wait=0", headers=self.hdr(a)).get_json()
        return a, b, sa["match_id"]

    def test_a_vanished_opponent_cant_freeze_the_match_and_forfeits(self):
        a, b, match_id = self._pair("poker", "10.1.0.1", "10.1.0.2", length=20)
        real_now = arena._now()
        for i in range(1, 12):
            with mock.patch("api.app._now", return_value=real_now + i * (arena.TURN_SECONDS + 1) * 2):
                sa = self.client.get(f"/play/{match_id}?wait=0", headers=self.hdr(a)).get_json()
                if sa["status"] == "your_turn":
                    self.client.post(f"/play/{match_id}/move", json={"move": sa["legal_moves"][0], "wait": 0}, headers=self.hdr(a))
                if sa["status"] == "match_over":
                    break
        self.assertEqual(sa["status"], "match_over")
        sb = self.client.get(f"/play/{match_id}?wait=0", headers=self.hdr(b)).get_json()
        self.assertIn("forfeit", sb["result"])
        self.assertEqual(sa["result"], "you won (your opponent forfeited)")

    def test_ratings_anchor_to_the_house_and_winners_go_up(self):
        bot = self.register()
        state = self.client.post("/play", json={"game": "poker", "opponent": "easy", "length": 10}, headers=self.hdr(bot)).get_json()
        final = self.play_out(bot, state, pick=lambda s: "raise" if "raise" in s["legal_moves"] else ("call" if "call" in s["legal_moves"] else s["legal_moves"][0]))
        ranking = self.client.get("/rankings?game=poker").get_json()
        self.assertEqual(ranking["house_bots"]["cfr_bot"], 1600)
        self.assertIn(bot["name"], [r["bot"] for r in ranking["rankings"]])
        self.assertNotIn("random_bot", [r["bot"] for r in ranking["rankings"]], "house bots have fixed ratings, not rows")
        expected_direction = {"you won": 1, "you lost": -1, "draw": 0}[final["result"]]
        if expected_direction:
            self.assertEqual((final["your_rating"] > 1000) - (final["your_rating"] < 1000), expected_direction)

    def test_short_matches_and_same_owner_matches_dont_count(self):
        bot = self.register()
        self.play_out(bot, self.client.post("/play", json={"game": "poker", "opponent": "easy", "length": 3}, headers=self.hdr(bot)).get_json())
        self.assertIsNone(self.client.get(f"/bots/{bot['name']}").get_json()["ratings"]["poker"], "3 hands is too short to rate")
        a, b, match_id = self._pair("duel", "10.2.0.1", "10.2.0.1", length=3)  # same owner
        for _ in range(400):
            done = True
            for who in (a, b):
                s = self.client.get(f"/play/{match_id}?wait=0", headers=self.hdr(who)).get_json()
                if s["status"] == "your_turn":
                    self.client.post(f"/play/{match_id}/move", json={"move": "rest", "wait": 0}, headers=self.hdr(who))
                done = done and s["status"] == "match_over"
            if done:
                break
        self.assertIsNone(self.client.get(f"/bots/{a['name']}").get_json()["ratings"]["duel"], "an owner's own bots can't rate each other")

    def test_the_same_pair_can_only_rate_each_other_a_few_times_a_day(self):
        from ledger import db
        with db.connect() as conn:
            results = [db.take_rated_pair_slot(conn, 900001, 900002, arena.MAX_RATED_PAIR_GAMES_PER_DAY) for _ in range(5)]
        self.assertEqual(results, [True] * arena.MAX_RATED_PAIR_GAMES_PER_DAY + [False] * (5 - arena.MAX_RATED_PAIR_GAMES_PER_DAY))

    def test_profile_and_badge(self):
        bot = self.register()
        p = self.client.get(f"/bots/{bot['name']}").get_json()
        self.assertEqual(p["bot"], bot["name"])
        r = self.client.get(f"/bots/{bot['name']}/badge.svg")
        self.assertEqual(r.mimetype, "image/svg+xml")
        self.assertIn(b"Agent Arena", r.data)


class TestSafety(PlayTest):
    def test_too_many_matches_at_once_is_refused(self):
        bot = self.register()
        # One match per game is the natural limit via /play; simulate other live matches directly.
        from ledger import db
        with db.connect() as conn:
            me = db.get_bot_by_name(conn, bot["name"])
            ids = []
            for _ in range(play.MAX_MATCHES_AT_ONCE["free"]):
                mid = db.create_match(conn, me["id"], me["id"], 1, 0, game_type="other")
                db.save_live_match(conn, mid, me["id"], me["id"], {}, game_type="other")
                ids.append(mid)
        r = self.client.post("/play", json={"game": "poker", "opponent": "easy"}, headers=self.hdr(bot))
        self.assertEqual(r.status_code, 429)
        with db.connect() as conn:
            conn.executemany("DELETE FROM live_matches WHERE match_id = ?", [(i,) for i in ids])

    def test_rate_limit_and_request_size(self):
        self.app.testing = False
        try:
            old = play.RATE_LIMIT_PER_MINUTE
            play.RATE_LIMIT_PER_MINUTE = 5
            play._hits.clear()
            codes = [self.client.get("/rules/poker", headers={"X-Forwarded-For": "10.9.9.9"}).status_code for _ in range(7)]
            self.assertEqual(codes[:5], [200] * 5)
            self.assertEqual(codes[-1], 429)
            play.RATE_LIMIT_PER_MINUTE = old
            big = self.client.post("/bots", data="x" * 100_000, headers={"Content-Type": "application/json", "X-Forwarded-For": "10.9.9.8"})
            self.assertEqual(big.status_code, 413)
        finally:
            self.app.testing = True
            play._hits.clear()

    def test_chips_are_refilled_so_nobody_is_locked_out(self):
        from ledger import db
        bot = self.register()
        with db.connect() as conn:
            conn.execute("UPDATE bots SET balance = 0 WHERE name = ?", (bot["name"],))
        s = self.client.post("/play", json={"game": "poker", "opponent": "easy"}, headers=self.hdr(bot)).get_json()
        self.assertEqual(s["status"], "your_turn")

    def test_admin_can_upgrade_a_bot(self):
        bot = self.register()
        r = self.client.post(f"/admin/bots/{bot['name']}/tier", json={"tier": "pro"}, headers={"X-Admin-Secret": "test-admin-secret"})
        self.assertEqual(r.get_json()["tier"], "pro")
        self.assertEqual(self.client.post(f"/admin/bots/{bot['name']}/tier", json={"tier": "pro"}).status_code, 401)


class TestAgentAccess(PlayTest):
    def rpc(self, method, params=None, key=None, mid=1):
        headers = {"X-API-Key": key} if key else {}
        return self.client.post("/mcp", json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, headers=headers).get_json()

    def test_an_ai_assistant_can_play_over_mcp(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertEqual(self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["arena_register", "arena_play", "arena_move", "arena_status", "arena_rules", "arena_rankings"])
        _counter[0] += 1
        reg = self.rpc("tools/call", {"name": "arena_register", "arguments": {"name": f"mcp-bot-{_counter[0]}"}})["result"]
        self.assertFalse(reg["isError"])
        key = reg["structuredContent"]["api_key"]
        state = self.rpc("tools/call", {"name": "arena_play", "arguments": {"game": "poker", "opponent": "easy", "length": 2, "api_key": key}})["result"]["structuredContent"]
        for _ in range(50):
            if state["status"] == "match_over":
                break
            state = self.rpc("tools/call", {"name": "arena_move", "arguments": {"match_id": state["match_id"], "move": state["legal_moves"][0], "api_key": key}})["result"]["structuredContent"]
        self.assertEqual(state["status"], "match_over")
        bad = self.rpc("tools/call", {"name": "arena_play", "arguments": {"game": "poker"}})["result"]
        self.assertTrue(bad["isError"])
        self.assertIn("error", self.rpc("no/such/method"))

    def test_agent_docs_are_served(self):
        txt = self.client.get("/llms.txt").get_data(as_text=True)
        self.assertIn("POST /play", txt)
        self.assertEqual(self.client.get("/skill.md").status_code, 200)
        self.assertIn("Leduc", self.client.get("/rules/poker").get_json()["rules"])
        self.assertIn(b"class Arena", self.client.get("/sdk/arena_client.py").data)


if __name__ == "__main__":
    unittest.main()
