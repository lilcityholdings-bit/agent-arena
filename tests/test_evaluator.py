import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cards import Card
from engine.evaluator import compare, score_hand


class TestEvaluator(unittest.TestCase):
    def test_pair_beats_high_card(self):
        board = Card("K", "a")
        pair_holder = Card("K", "b")
        high_card_holder = Card("Q", "a")
        self.assertEqual(compare(pair_holder, high_card_holder, board), 1)
        self.assertEqual(compare(high_card_holder, pair_holder, board), -1)

    def test_higher_rank_wins_no_pair(self):
        board = Card("J", "a")
        self.assertEqual(compare(Card("K", "a"), Card("Q", "b"), board), 1)
        self.assertEqual(compare(Card("Q", "b"), Card("K", "a"), board), -1)

    def test_tie_splits(self):
        board = Card("J", "a")
        self.assertEqual(compare(Card("Q", "a"), Card("Q", "b"), board), 0)

    def test_higher_pair_beats_lower_pair(self):
        # Only reachable if the board pairs one seat while both hold same-rank
        # hole cards is impossible (only 2 of each rank exist and both would
        # need one each plus a third for the board) -- but score_hand alone
        # should still order pairs by rank correctly for completeness.
        self.assertGreater(score_hand(Card("K", "a"), Card("K", "b")), score_hand(Card("Q", "a"), Card("Q", "b")))


if __name__ == "__main__":
    unittest.main()
