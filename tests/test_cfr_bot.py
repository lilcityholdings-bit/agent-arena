"""CFRBot is the "hard mode" opponent: a strategy computed offline by
cfr_train.py against the real engine rules (see that file's docstring).
These tests check it loads correctly, doesn't break chip conservation,
and -- the actual point of building it -- has a real, positive edge over
both baseline bots, establishing the skill hierarchy random < heuristic
< cfr that the arena's whole thesis depends on."""
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bots.cfr_bot import CFRBot
from bots.heuristic_bot import HeuristicBot
from bots.random_bot import RandomBot
from ledger import db
from orchestrator import run_match

HANDS = 6000


class TestCFRBot(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        db.init_db(self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    def test_loads_and_plays_a_legal_full_match(self):
        rng = random.Random(1)
        with db.connect(self.db_path) as conn:
            a = db.create_bot(conn, "cfr_a", starting_balance=50000)
            b = db.create_bot(conn, "random_a", starting_balance=50000)
            match_id = run_match(conn, a["id"], b["id"], CFRBot(rng=rng), RandomBot(rng=rng), hands=500, rake_bps=500, rng=rng)
            rows = db.match_history(conn, match_id)
        self.assertEqual(len(rows), 500)

    def test_beats_random_bot(self):
        rng = random.Random(42)
        with db.connect(self.db_path) as conn:
            cfr_bot = db.create_bot(conn, "cfr_b", starting_balance=200000)
            random_bot = db.create_bot(conn, "random_b", starting_balance=200000)
            run_match(conn, cfr_bot["id"], random_bot["id"], CFRBot(rng=rng), RandomBot(rng=rng), hands=HANDS, rake_bps=0, rng=rng)
            cfr_delta = db.get_bot(conn, cfr_bot["id"])["balance"] - 200000
            random_delta = db.get_bot(conn, random_bot["id"])["balance"] - 200000
        self.assertEqual(cfr_delta + random_delta, 0)  # zero rake => zero-sum
        self.assertGreater(cfr_delta, 0)

    def test_beats_heuristic_bot(self):
        rng = random.Random(7)
        with db.connect(self.db_path) as conn:
            cfr_bot = db.create_bot(conn, "cfr_c", starting_balance=200000)
            heuristic_bot = db.create_bot(conn, "heuristic_c", starting_balance=200000)
            run_match(conn, cfr_bot["id"], heuristic_bot["id"], CFRBot(rng=rng), HeuristicBot(rng=rng), hands=HANDS, rake_bps=0, rng=rng)
            cfr_delta = db.get_bot(conn, cfr_bot["id"])["balance"] - 200000
            heuristic_delta = db.get_bot(conn, heuristic_bot["id"])["balance"] - 200000
        self.assertEqual(cfr_delta + heuristic_delta, 0)
        self.assertGreater(cfr_delta, 0)


if __name__ == "__main__":
    unittest.main()
