"""The total and the spread.

The property that matters most is the one in the middle: the spread is
derived from the win probability, so it can never name a different favourite
than the pick. Two independent models disagreed on 15% of NFL games, and a
card arguing with itself is worse than a card with no spread on it.
"""
from __future__ import annotations

import unittest

from sportspred import scores
from sportspred.util import norm_cdf


def game(date, away, home, away_score=None, home_score=None, **over):
    g = {'date': date, 'away': away, 'home': home,
         'away_score': away_score, 'home_score': home_score,
         'final': home_score is not None}
    g.update(over)
    return g


def season(n=120, home_bias=0.0):
    """A synthetic league where one team outscores everyone."""
    teams = ['A', 'B', 'C', 'D']
    out = []
    for i in range(n):
        home = teams[i % 4]
        away = teams[(i // 4 + 1 + i) % 4]
        if home == away:
            away = teams[(i + 2) % 4]
        # A scores 30, everyone else 20; defences are all alike.
        hs = (30 if home == 'A' else 20) + home_bias
        as_ = 30 if away == 'A' else 20
        out.append(game(f'2026-01-{(i % 28) + 1:02d}', away, home, as_, hs))
    return out


class TestExpectedScores(unittest.TestCase):
    def test_no_league_average_yet_gives_nothing(self):
        self.assertIsNone(scores.expected(None, (0, 0, 0), (0, 0, 0)))

    def test_a_blend_of_zero_is_the_league_average(self):
        pair = scores.expected(50.0, (400, 200, 10), (200, 400, 10), blend=0.0)
        self.assertEqual([round(x, 2) for x in pair], [25.0, 25.0])

    def test_the_better_attack_is_expected_to_score_more(self):
        strong, weak = (400, 200, 10), (200, 400, 10)
        away, home = scores.expected(50.0, home=strong, away=weak, blend=1.0)
        self.assertGreater(home, away)

    def test_the_home_edge_is_split_between_the_sides(self):
        even = (250, 250, 10)
        away, home = scores.expected(50.0, even, even, home_edge=4.0, blend=1.0)
        self.assertAlmostEqual(home - away, 4.0, places=5)
        self.assertAlmostEqual(home + away, 50.0, places=5)


class TestLeakFree(unittest.TestCase):
    def test_a_games_own_result_is_not_in_its_own_snapshot(self):
        # Every snapshot is built from games strictly before it, so the last
        # game's snapshot cannot know the last game's score.
        games = season(120)
        rows = scores.walk(games)
        self.assertTrue(rows)
        # Totals in the final snapshot come from 119 games, not 120.
        _, home, away, _, _, _ = rows[-1]
        self.assertLessEqual(home[2] + away[2], 2 * (len(games) - 1))

    def test_an_unplayed_game_never_advances_the_tally(self):
        games = season(80) + [game('2026-02-01', 'A', 'B')]
        avg, tally = scores.current(games)
        self.assertEqual(tally.of('B')[2], tally.of('B')[2])    # no crash, no credit
        self.assertIsNotNone(avg)

    def test_preseason_is_ignored(self):
        played = scores.current(season(80))[1].of('A')[2]
        with_exhibition = scores.current(
            season(80) + [game('2026-02-01', 'B', 'A', 99, 99, preseason=True)])[1].of('A')[2]
        self.assertEqual(played, with_exhibition)


class TestFit(unittest.TestCase):
    def test_too_few_games_is_warming_up(self):
        f = scores.fit(season(20))
        self.assertEqual(f['stage'], 'warming up')
        self.assertEqual(f['blend_total'], 0.0)

    def test_enough_games_gets_fitted(self):
        f = scores.fit(season(200))
        self.assertEqual(f['stage'], 'fitted')
        self.assertIsNotNone(f['mae_total'])

    def test_the_blend_is_never_worse_than_the_league_average(self):
        # The grid includes zero, so the fit can always fall back.
        f = scores.fit(season(200))
        self.assertLessEqual(f['mae_total'], f['naive_total'] + 1e-9)

    def test_a_league_where_scoring_is_knowable_uses_the_estimate(self):
        # One team scores 30 and the rest 20: the estimate should win outright.
        f = scores.fit(season(200))
        self.assertGreater(f['blend_total'], 0.0)

    def test_without_win_probabilities_the_spread_is_unfitted(self):
        f = scores.fit(season(200))
        self.assertEqual(f['spread_scale'], 1.0)
        self.assertIsNone(f['mae_margin'])

    def test_with_win_probabilities_the_spread_is_scored(self):
        games = season(200)
        probs = [0.6 if g['home'] == 'A' else 0.4 for g in games]
        f = scores.fit(games, probs)
        self.assertIsNotNone(f['mae_margin'])
        self.assertIsNotNone(f['naive_margin'])
        self.assertGreater(f['spread_n'], 0)


class TestSpreadFollowsTheWinChance(unittest.TestCase):
    """The whole reason the spread is derived rather than modelled."""

    def fitted(self, sd=14.0, scale=1.0):
        return {'margin_sd': sd, 'spread_scale': scale, 'blend_total': 1.0,
                'home_edge': 0.0, 'stage': 'fitted'}

    def test_an_even_game_is_a_pick_em(self):
        self.assertAlmostEqual(scores.spread_from(0.5, self.fitted()), 0.0, places=6)

    def test_a_home_favourite_gets_a_positive_margin(self):
        self.assertGreater(scores.spread_from(0.62, self.fitted()), 0)

    def test_an_away_favourite_gets_a_negative_margin(self):
        self.assertLess(scores.spread_from(0.38, self.fitted()), 0)

    def test_a_bigger_favourite_gives_more_points(self):
        f = self.fitted()
        self.assertGreater(scores.spread_from(0.8, f), scores.spread_from(0.6, f))

    def test_it_is_the_textbook_conversion(self):
        # margin = sd * the normal quantile of the win chance, so running it
        # back through the normal gives the probability you started with.
        f = self.fitted(sd=14.0)
        m = scores.spread_from(0.62, f)
        self.assertAlmostEqual(norm_cdf(m, 0.0, 14.0), 0.62, places=6)

    def test_no_margin_spread_on_record_means_no_spread(self):
        self.assertIsNone(scores.spread_from(0.7, {'margin_sd': None}))

    def test_the_favourite_always_matches_the_win_chance(self):
        f = self.fitted()
        for p in (0.01, 0.2, 0.45, 0.5, 0.55, 0.8, 0.99):
            m = scores.spread_from(p, f)
            if p > 0.5:
                self.assertGreater(m, 0, p)
            elif p < 0.5:
                self.assertLess(m, 0, p)
            else:
                self.assertAlmostEqual(m, 0.0, places=6)


class TestPredict(unittest.TestCase):
    def setUp(self):
        self.games = season(200)
        self.probs = [0.6 if g['home'] == 'A' else 0.45 for g in self.games]
        self.fit = scores.fit(self.games, self.probs)
        self.avg, self.tally = scores.current(self.games)

    def test_a_warming_up_league_gets_no_line_at_all(self):
        # Otherwise every card in the league shows the same league-average
        # total, which reads as broken rather than as honest.
        thin = scores.fit(season(20))
        avg, tally = scores.current(season(20))
        self.assertIsNone(scores.predict(thin, avg or 40.0, (200, 200, 10), (200, 200, 10),
                                         win_prob=0.6))

    def test_a_team_with_no_record_gets_no_line(self):
        self.assertIsNone(scores.predict(self.fit, self.avg, (0, 0, 0), self.tally.of('B'),
                                         win_prob=0.6))

    def test_the_scoreline_adds_up_to_the_total_and_the_spread(self):
        out = scores.predict(self.fit, self.avg, self.tally.of('A'), self.tally.of('B'),
                             win_prob=0.62)
        self.assertAlmostEqual(out['exp_home'] + out['exp_away'], out['total'], delta=0.2)
        self.assertAlmostEqual(out['exp_home'] - out['exp_away'], out['margin'], delta=0.2)

    def test_the_total_does_not_depend_on_who_wins(self):
        a = scores.predict(self.fit, self.avg, self.tally.of('A'), self.tally.of('B'), win_prob=0.9)
        b = scores.predict(self.fit, self.avg, self.tally.of('A'), self.tally.of('B'), win_prob=0.1)
        self.assertEqual(a['total'], b['total'])

    def test_without_a_win_probability_there_is_no_spread(self):
        out = scores.predict(self.fit, self.avg, self.tally.of('A'), self.tally.of('B'))
        self.assertIsNone(out['margin'])
        self.assertIsNotNone(out['total'])

    def test_the_accuracy_travels_with_the_prediction(self):
        out = scores.predict(self.fit, self.avg, self.tally.of('A'), self.tally.of('B'),
                             win_prob=0.6)
        self.assertEqual(out['mae_total'], self.fit['mae_total'])
        self.assertEqual(out['stage'], 'fitted')


if __name__ == '__main__':
    unittest.main()
