"""Same thesis test as test_cfr_bot.py, for the Duel game: skill has to
actually translate into an edge, or the whole "skill game, not a coin
flip" premise is wrong for this game too. Runs fights directly against
the engine (no ledger) to keep this fast; ledger-level conservation for
Duel is covered in test_ledger_multigame.py once matches are wired up
end to end."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.duel import DuelFight
from bots import RandomDuelBot, HeuristicDuelBot, BossDuelBot


def run_fight(bot0, bot1, rng, rake_bps=0, stake=10):
    f = DuelFight(rng=rng, rake_bps=rake_bps, stake=stake)
    f.start()
    while not f.done:
        for p, bot in ((0, bot0), (1, bot1)):
            if p not in f.pending_moves:
                f.submit(p, bot.act(f.state_for(p)))
    return f.result


def tournament(factory_a, factory_b, n, seed, rake_bps=0):
    rng = random.Random(seed)
    delta_a = delta_b = total_rake = 0
    for i in range(n):
        bot_a, bot_b = factory_a(rng), factory_b(rng)
        if i % 2 == 0:  # alternate seats so neither side has a positional edge
            result = run_fight(bot_a, bot_b, rng, rake_bps=rake_bps)
            pa, pb = result.payoffs[0], result.payoffs[1]
        else:
            result = run_fight(bot_b, bot_a, rng, rake_bps=rake_bps)
            pb, pa = result.payoffs[0], result.payoffs[1]
        delta_a += pa
        delta_b += pb
        total_rake += result.rake_taken
    return delta_a, delta_b, total_rake


class TestDuelSkillHierarchy(unittest.TestCase):
    def test_heuristic_beats_random(self):
        delta_h, delta_r, rake = tournament(
            lambda r: HeuristicDuelBot(rng=r), lambda r: RandomDuelBot(rng=r), n=500, seed=1)
        self.assertEqual(delta_h + delta_r + rake, 0)
        self.assertGreater(delta_h, delta_r)

    def test_boss_beats_random(self):
        delta_boss, delta_r, rake = tournament(
            lambda r: BossDuelBot(rng=r, iterations=150), lambda r: RandomDuelBot(rng=r), n=80, seed=2)
        self.assertEqual(delta_boss + delta_r + rake, 0)
        self.assertGreater(delta_boss, delta_r)

    def test_boss_beats_heuristic(self):
        delta_boss, delta_h, rake = tournament(
            lambda r: BossDuelBot(rng=r, iterations=150), lambda r: HeuristicDuelBot(rng=r), n=80, seed=3)
        self.assertEqual(delta_boss + delta_h + rake, 0)
        self.assertGreater(delta_boss, delta_h)

    def test_rake_is_collected_and_still_conserves(self):
        delta_boss, delta_r, rake = tournament(
            lambda r: BossDuelBot(rng=r, iterations=150), lambda r: RandomDuelBot(rng=r), n=40, seed=4, rake_bps=500)
        self.assertGreater(rake, 0)
        self.assertEqual(delta_boss + delta_r + rake, 0)


if __name__ == "__main__":
    unittest.main()
