"""End-to-end verification: run a real tournament through the ledger and
check that no chips are created or destroyed, and that the heuristic bot
(which folds weak hands and presses strong ones) actually beats the random
bot over enough hands -- i.e. this is a skill game, not a coin flip."""
import os
import random
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bots.heuristic_bot import HeuristicBot
from bots.random_bot import RandomBot
from ledger import db
from orchestrator import run_match

HANDS = 4000
RAKE_BPS = 500


class TestConservation(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        db.init_db(self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    def test_chip_conservation_and_skill_edge(self):
        rng = random.Random(42)
        with db.connect(self.db_path) as conn:
            random_bot_row = db.create_bot(conn, "random_bot", starting_balance=100000)
            heuristic_bot_row = db.create_bot(conn, "heuristic_bot", starting_balance=100000)

            match_id = run_match(
                conn,
                random_bot_row["id"],
                heuristic_bot_row["id"],
                RandomBot(rng=rng),
                HeuristicBot(rng=rng),
                hands=HANDS,
                rake_bps=RAKE_BPS,
                rng=rng,
            )

        with db.connect(self.db_path) as conn:
            hands_rows = db.match_history(conn, match_id)
            self.assertEqual(len(hands_rows), HANDS)

            total_rake = 0
            for row in hands_rows:
                total_rake += row["rake"]
                # every single hand must itself be zero-sum once rake is accounted for
                self.assertEqual(row["payoff_seat0"] + row["payoff_seat1"], -row["rake"])

            house_bal = db.house_balance(conn)
            self.assertEqual(house_bal, total_rake)

            random_bot = db.get_bot(conn, random_bot_row["id"])
            heuristic_bot = db.get_bot(conn, heuristic_bot_row["id"])

            random_delta = random_bot["balance"] - 100000
            heuristic_delta = heuristic_bot["balance"] - 100000

            # The whole-system invariant: nothing created or destroyed.
            self.assertEqual(random_delta + heuristic_delta + total_rake, 0)

            # Skill edge: the heuristic bot should come out ahead of the
            # random bot over enough hands (it folds bad hands and presses
            # good ones instead of acting uniformly at random).
            self.assertGreater(heuristic_delta, random_delta)


if __name__ == "__main__":
    unittest.main()
