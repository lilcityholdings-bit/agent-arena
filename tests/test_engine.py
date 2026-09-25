import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.leduc import LeducHand, IllegalAction, MAX_RAISES


class TestEngine(unittest.TestCase):
    def test_start_state(self):
        hand = LeducHand(rng=random.Random(1))
        hand.start()
        self.assertEqual(hand.contributed, {0: 1, 1: 1})
        self.assertEqual(hand.round, 1)
        self.assertEqual(hand.to_act, 0)
        self.assertEqual(hand.legal_actions(), ["check", "raise"])

    def test_fold_ends_hand_immediately_and_pays_correctly(self):
        hand = LeducHand(rng=random.Random(1), rake_bps=0)
        hand.start()
        hand.apply("raise")  # seat 0 bets 2 -> contributed {0:3, 1:1}
        hand.apply("fold")  # seat 1 folds
        self.assertTrue(hand.done)
        r = hand.result
        self.assertFalse(r.went_to_showdown)
        self.assertEqual(r.winner, 0)
        # seat 0 put in 3, gets pot (4) back -> net +1; seat 1 put in 1, gets 0 -> net -1
        self.assertEqual(r.payoffs[0], 1)
        self.assertEqual(r.payoffs[1], -1)
        self.assertEqual(r.payoffs[0] + r.payoffs[1], 0)  # zero rake => zero-sum

    def test_check_check_advances_round(self):
        hand = LeducHand(rng=random.Random(1))
        hand.start()
        hand.apply("check")
        self.assertEqual(hand.round, 1)  # not advanced yet, seat 1 still to act
        hand.apply("check")
        self.assertEqual(hand.round, 2)
        self.assertIsNotNone(hand.board)
        self.assertEqual(hand.to_act, 0)

    def test_cannot_raise_more_than_max(self):
        hand = LeducHand(rng=random.Random(1))
        hand.start()
        count = 0
        while "raise" in hand.legal_actions() and count < 10:
            hand.apply("raise")
            count += 1
        self.assertLessEqual(count, MAX_RAISES)
        self.assertNotIn("raise", hand.legal_actions())

    def test_illegal_action_raises(self):
        hand = LeducHand(rng=random.Random(1))
        hand.start()
        with self.assertRaises(IllegalAction):
            hand.apply("call")  # nothing to call yet, only check/raise legal

    def test_short_stack_cannot_raise_only_call_or_fold(self):
        # seat 1 only has 2 chips total: 1 for the ante, 1 left behind --
        # not enough to call a 2-chip raise plus make a new one.
        hand = LeducHand(rng=random.Random(1), stacks={0: 100, 1: 2})
        hand.start()
        hand.apply("raise")  # seat 0 bets, contributed[0] = 3
        self.assertNotIn("raise", hand.legal_actions())  # seat 1 can't afford to raise back
        self.assertIn("call", hand.legal_actions())
        self.assertIn("fold", hand.legal_actions())

    def test_all_in_call_for_less_returns_uncalled_chips_and_conserves(self):
        hand = LeducHand(rng=random.Random(3), rake_bps=0, stacks={0: 100, 1: 2})
        hand.start()
        hand.apply("raise")  # seat 0 contributed 1 (ante) + 2 (raise) = 3
        hand.apply("call")  # seat 1 only has 1 chip left behind -> all-in call for less
        # seat 1's whole stack (2) is in; seat 0's uncalled extra chip comes back.
        self.assertEqual(hand.contributed[1], 2)
        self.assertEqual(hand.contributed[0], 2)
        self.assertTrue(hand.is_all_in(1))
        self.assertEqual(hand.round, 2)  # a call always closes the betting round

        # Both players are effectively locked out of further betting now
        # (seat 1 has nothing left, so no raise can be responded to) --
        # checking through to showdown is the only legal path.
        while not hand.done:
            self.assertEqual(hand.legal_actions(), ["check"])
            hand.apply("check")

        r = hand.result
        self.assertEqual(sum(r.payoffs.values()), 0)  # zero rake => exactly zero-sum
        # neither payoff should ever imply a player lost more than they had behind
        self.assertGreaterEqual(hand.stacks[0] + r.payoffs[0], 0)
        self.assertGreaterEqual(hand.stacks[1] + r.payoffs[1], 0)

    def test_once_one_player_all_in_no_more_raises(self):
        # seat 0 has only 2 chips behind after the ante -- exactly one bet_size.
        hand = LeducHand(rng=random.Random(5), stacks={0: 3, 1: 100})
        hand.start()
        hand.apply("raise")  # seat 0 puts in its last 2 chips -> fully all-in
        self.assertTrue(hand.is_all_in(0))
        # seat 1 has plenty of chips left, but seat 0 (already all-in) could
        # never respond to a further raise, so raising is not offered.
        self.assertNotIn("raise", hand.legal_actions())
        self.assertEqual(set(hand.legal_actions()), {"fold", "call"})


    def test_full_hand_to_showdown_pot_math(self):
        hand = LeducHand(rng=random.Random(2), rake_bps=1000)  # 10% rake
        hand.start()
        # play check-check to the river, then check-check to showdown
        while not hand.done:
            hand.apply("check")
        r = hand.result
        self.assertTrue(r.went_to_showdown)
        self.assertEqual(r.pot_before_rake, 2)  # two 1-chip antes, no bets
        self.assertEqual(r.rake_taken, 0)  # 10% of 2 floors to 0
        self.assertEqual(sum(r.payoffs.values()), -r.rake_taken)


    def test_serialization_round_trip_mid_hand(self):
        from engine.leduc import LeducHand as LH

        hand = LH(rng=random.Random(7), rake_bps=500, stacks={0: 50, 1: 50})
        hand.start()
        hand.apply("raise")  # leave it mid-hand, seat 1 to act

        restored = LH.from_dict(hand.to_dict())
        self.assertEqual(restored.round, hand.round)
        self.assertEqual(restored.to_act, hand.to_act)
        self.assertEqual(restored.contributed, hand.contributed)
        self.assertEqual(restored.hole, hand.hole)
        self.assertEqual(restored.legal_actions(), hand.legal_actions())

        # play both hands out identically from here and confirm they agree
        while not hand.done:
            action = hand.legal_actions()[0]
            hand.apply(action)
            restored.apply(action)
        self.assertEqual(hand.result.payoffs, restored.result.payoffs)

    def test_serialization_survives_a_real_json_round_trip(self):
        # to_dict()/from_dict() alone can't catch bugs where dict keys
        # silently survive as ints in memory -- json.dumps/loads (what the
        # API actually does to persist a live match) turns int keys into
        # strings, which is exactly the kind of thing that breaks a naive
        # from_dict().
        import json

        from engine.leduc import LeducHand as LH

        hand = LH(rng=random.Random(11), rake_bps=500, stacks={0: 40, 1: 40})
        hand.start()
        hand.apply("raise")
        blob = json.loads(json.dumps(hand.to_dict()))
        restored = LH.from_dict(blob)
        self.assertEqual(restored.contributed, hand.contributed)
        self.assertEqual(restored.stacks, hand.stacks)
        self.assertEqual(restored.hole, hand.hole)
        self.assertEqual(restored.legal_actions(), hand.legal_actions())

    def test_serialization_round_trip_finished_hand(self):
        from engine.leduc import LeducHand as LH

        hand = LH(rng=random.Random(9), rake_bps=500)
        hand.start()
        while not hand.done:
            hand.apply(hand.legal_actions()[0])

        restored = LH.from_dict(hand.to_dict())
        self.assertTrue(restored.done)
        self.assertEqual(restored.result.payoffs, hand.result.payoffs)
        self.assertEqual(restored.result.rake_taken, hand.result.rake_taken)


if __name__ == "__main__":
    unittest.main()
