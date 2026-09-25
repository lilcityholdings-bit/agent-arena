"""Duel engine tests: the resolution table behaves exactly as documented,
stamina genuinely gates what's legal, and win-payoffs conserve value the
same way Leduc's chips do (every unit either moves to the other fighter
or the house rake -- nothing created or destroyed)."""
import json
import random
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.duel import (
    DuelFight, IllegalAction, STRIKE, GRAPPLE, BLOCK, DODGE, REST,
    MAX_HP, MAX_STAMINA, UNLIMITED_STACK,
)


def fresh(rake_bps=0, stake=10, stacks=None):
    f = DuelFight(rng=random.Random(0), rake_bps=rake_bps, stake=stake,
                  stacks=stacks or {0: UNLIMITED_STACK, 1: UNLIMITED_STACK})
    f.start()
    return f


class TestLegalActions(unittest.TestCase):
    def test_all_moves_legal_at_full_stamina(self):
        f = fresh()
        self.assertEqual(set(f.legal_actions(0)), {STRIKE, GRAPPLE, BLOCK, DODGE, REST})

    def test_rest_always_legal_even_at_zero_stamina(self):
        f = fresh()
        f.stamina[0] = 0
        self.assertEqual(f.legal_actions(0), [REST])

    def test_expensive_moves_illegal_when_unaffordable(self):
        f = fresh()
        f.stamina[0] = 4  # can't afford STRIKE(5), GRAPPLE(7), or DODGE(9)
        self.assertEqual(set(f.legal_actions(0)), {BLOCK, REST})

    def test_submitting_unaffordable_move_raises(self):
        f = fresh()
        f.stamina[0] = 2
        with self.assertRaises(IllegalAction):
            f.submit(0, STRIKE)

    def test_double_submit_raises(self):
        f = fresh()
        f.submit(0, REST)
        with self.assertRaises(IllegalAction):
            f.submit(0, BLOCK)

    def test_stack_below_stake_cannot_start(self):
        f = DuelFight(rng=random.Random(0), stake=10, stacks={0: 5, 1: 100})
        with self.assertRaises(IllegalAction):
            f.start()


class TestResolutionTable(unittest.TestCase):
    def test_strike_beats_grapple(self):
        f = fresh()
        hp1_before = f.hp[1]
        f.submit(0, STRIKE)
        f.submit(1, GRAPPLE)
        self.assertEqual(f.hp[0], MAX_HP)
        self.assertEqual(hp1_before - f.hp[1], 6)

    def test_grapple_beats_block_and_drains_extra_stamina(self):
        f = fresh()
        f.submit(0, GRAPPLE)
        f.submit(1, BLOCK)
        self.assertEqual(f.hp[0], MAX_HP)
        self.assertEqual(MAX_HP - f.hp[1], 5)
        # loser (1) spent 3 on BLOCK, lost 4 more thrown, then regenerated 4
        self.assertEqual(f.stamina[1], MAX_STAMINA - 3 - 4 + 4)

    def test_block_beats_strike_no_damage(self):
        f = fresh()
        f.submit(0, BLOCK)
        f.submit(1, STRIKE)
        self.assertEqual(f.hp[0], MAX_HP)
        self.assertEqual(f.hp[1], MAX_HP)

    def test_dodge_beats_strike_and_grapple(self):
        f = fresh()
        f.submit(0, DODGE)
        f.submit(1, STRIKE)
        self.assertEqual(f.hp[0], MAX_HP)
        self.assertEqual(f.hp[1], MAX_HP)

        f2 = fresh()
        f2.submit(0, DODGE)
        f2.submit(1, GRAPPLE)
        self.assertEqual(f2.hp[0], MAX_HP)
        self.assertEqual(f2.hp[1], MAX_HP)

    def test_matching_strikes_clash_both_take_damage(self):
        f = fresh()
        f.submit(0, STRIKE)
        f.submit(1, STRIKE)
        self.assertEqual(MAX_HP - f.hp[0], 3)
        self.assertEqual(MAX_HP - f.hp[1], 3)

    def test_matching_grapples_clash_damage_and_stamina(self):
        f = fresh()
        f.submit(0, GRAPPLE)
        f.submit(1, GRAPPLE)
        self.assertEqual(MAX_HP - f.hp[0], 3)
        self.assertEqual(MAX_HP - f.hp[1], 3)
        expected = MAX_STAMINA - 7 - 2 + 4
        self.assertEqual(f.stamina[0], expected)
        self.assertEqual(f.stamina[1], expected)

    def test_resting_through_an_attack_is_undefended_and_worse_than_blocking(self):
        f = fresh()
        f.submit(0, REST)
        f.submit(1, STRIKE)
        self.assertEqual(MAX_HP - f.hp[0], 8)  # worse than the 0 damage BLOCK/DODGE would take

    def test_resting_through_a_non_attack_is_a_no_op(self):
        f = fresh()
        f.submit(0, REST)
        f.submit(1, BLOCK)
        self.assertEqual(f.hp[0], MAX_HP)
        self.assertEqual(f.hp[1], MAX_HP)

    def test_both_resting_regenerates_stamina_only(self):
        f = fresh()
        f.stamina[0] = 10
        f.stamina[1] = 10
        f.submit(0, REST)
        f.submit(1, REST)
        self.assertEqual(f.hp[0], MAX_HP)
        self.assertEqual(f.hp[1], MAX_HP)
        self.assertEqual(f.stamina[0], 14)
        self.assertEqual(f.stamina[1], 14)


class TestFightOutcomeAndConservation(unittest.TestCase):
    def test_knockout_ends_the_fight_and_declares_winner(self):
        f = fresh(rake_bps=0)
        f.hp[1] = 6  # one clean strike-vs-grapple from KO
        f.submit(0, STRIKE)
        f.submit(1, GRAPPLE)
        self.assertTrue(f.done)
        self.assertEqual(f.result.winner, 0)
        self.assertTrue(f.result.ko)
        self.assertEqual(f.result.payoffs[0], f.stake)   # won the loser's stake
        self.assertEqual(f.result.payoffs[1], -f.stake)

    def test_payoffs_conserve_value_with_rake(self):
        f = fresh(rake_bps=1000, stake=20)  # 10% rake
        f.hp[1] = 6
        f.submit(0, STRIKE)
        f.submit(1, GRAPPLE)
        self.assertTrue(f.done)
        total = f.result.payoffs[0] + f.result.payoffs[1] + f.result.rake_taken
        self.assertEqual(total, 0)
        self.assertEqual(f.result.rake_taken, (f.pot() * 1000) // 10000)

    def test_round_cap_decides_by_hp_when_nobody_is_ko_d(self):
        f = fresh(rake_bps=0)
        f.hp[0] = 20
        f.hp[1] = 25
        f.round_num = 39  # one round left before the cap
        f.submit(0, REST)
        f.submit(1, REST)
        self.assertTrue(f.done)
        self.assertEqual(f.result.winner, 1)
        self.assertFalse(f.result.ko)

    def test_equal_hp_at_cap_is_a_draw_split_pot(self):
        f = fresh(rake_bps=0, stake=20)
        f.hp[0] = 15
        f.hp[1] = 15
        f.round_num = 39
        f.submit(0, REST)
        f.submit(1, REST)
        self.assertTrue(f.done)
        self.assertIsNone(f.result.winner)
        self.assertEqual(f.result.payoffs[0], 0)
        self.assertEqual(f.result.payoffs[1], 0)

    def test_simultaneous_ko_is_a_draw(self):
        f = fresh(rake_bps=0)
        f.hp[0] = 3
        f.hp[1] = 3
        f.submit(0, STRIKE)
        f.submit(1, STRIKE)  # clash damage (3) drops both to exactly 0
        self.assertTrue(f.done)
        self.assertIsNone(f.result.winner)

    def test_full_random_fight_always_conserves_value(self):
        rng = random.Random(42)
        for trial in range(300):
            rake_bps = rng.choice([0, 300, 500, 1000])
            stake = rng.choice([1, 10, 50])
            f = DuelFight(rng=rng, rake_bps=rake_bps, stake=stake)
            f.start()
            while not f.done:
                for p in (0, 1):
                    if p not in f.pending_moves:
                        legal = f.legal_actions(p)
                        f.submit(p, rng.choice(legal))
            total = f.result.payoffs[0] + f.result.payoffs[1] + f.result.rake_taken
            self.assertEqual(total, 0, f"trial {trial} leaked value")
            self.assertLessEqual(f.result.rounds_played, 40)


class TestSerialization(unittest.TestCase):
    def test_survives_a_real_json_round_trip_mid_fight(self):
        f = fresh(stake=15)
        f.submit(0, STRIKE)
        f.submit(1, BLOCK)
        f.submit(0, DODGE)  # only 0 has submitted this round

        data = json.loads(json.dumps(f.to_dict()))
        restored = DuelFight.from_dict(data, rng=random.Random(1))

        self.assertEqual(restored.hp, f.hp)
        self.assertEqual(restored.stamina, f.stamina)
        self.assertEqual(restored.action_history, f.action_history)
        self.assertEqual(restored.pending_moves, f.pending_moves)
        self.assertFalse(restored.done)

    def test_survives_round_trip_after_fight_is_done(self):
        f = fresh(rake_bps=500)
        f.hp[1] = 6
        f.submit(0, STRIKE)
        f.submit(1, GRAPPLE)
        self.assertTrue(f.done)

        data = json.loads(json.dumps(f.to_dict()))
        restored = DuelFight.from_dict(data, rng=random.Random(1))
        self.assertTrue(restored.done)
        self.assertEqual(restored.result.winner, f.result.winner)
        self.assertEqual(restored.result.payoffs, f.result.payoffs)


if __name__ == "__main__":
    unittest.main()
