"""Elo, leak-free features, and the end-to-end model on simulated seasons."""
import unittest
from datetime import date, timedelta

from sportspred import config, features as feat, model
from sportspred.ratings import EloEngine, shrink_probability
from tests.helpers import synthetic_rows


class TestElo(unittest.TestCase):
    def setUp(self):
        self.engine = EloEngine(k=20, hfa=50, mov=1.0, regress=0.5)

    def test_equal_teams_get_the_home_edge(self):
        p = self.engine.expected('A', 'B')
        self.assertGreater(p, 0.5)
        self.assertAlmostEqual(self.engine.expected('A', 'B', neutral=True), 0.5, places=9)

    def test_rating_change_is_zero_sum(self):
        self.engine.observe('A', 'B', 5, 1)
        self.assertAlmostEqual(self.engine.rating('A') + self.engine.rating('B'), 3000.0, places=6)

    def test_bigger_margin_moves_ratings_further(self):
        small = EloEngine(k=20, hfa=0, mov=1.0)
        big = EloEngine(k=20, hfa=0, mov=1.0)
        small.observe('A', 'B', 2, 1)
        big.observe('A', 'B', 20, 1)
        self.assertGreater(big.rating('A'), small.rating('A'))

    def test_margin_is_ignored_when_mov_is_zero(self):
        a = EloEngine(k=20, hfa=0, mov=0.0)
        b = EloEngine(k=20, hfa=0, mov=0.0)
        a.observe('A', 'B', 2, 1)
        b.observe('A', 'B', 30, 1)
        self.assertAlmostEqual(a.rating('A'), b.rating('A'), places=9)

    def test_season_break_pulls_ratings_toward_the_mean(self):
        self.engine.ratings['A'] = 1700.0
        self.engine.new_season()
        self.assertAlmostEqual(self.engine.rating('A'), 1600.0, places=6)

    def test_replay_reports_pre_game_ratings(self):
        games = [
            dict(game_id='1', date=date(2026, 1, 1), home='A', away='B',
                 home_score=9, away_score=0, final=True, season=2026),
            dict(game_id='2', date=date(2026, 1, 8), home='A', away='B',
                 home_score=None, away_score=None, final=False, season=2026),
        ]
        recs = self.engine.replay(games)
        # The first game must be priced before anything is known about A.
        self.assertEqual(recs[0]['home_elo'], 1500.0)
        self.assertGreater(recs[1]['home_elo'], 1500.0)

    def test_snapshot_round_trip(self):
        # Snapshots round to two decimals so the committed state file stays
        # readable; that is the only loss allowed.
        self.engine.observe('A', 'B', 4, 2)
        restored = EloEngine().load(self.engine.snapshot())
        self.assertAlmostEqual(restored.rating('A'), self.engine.rating('A'), delta=0.01)

    def test_shrinkage_pulls_early_season_picks_to_a_coin_flip(self):
        self.assertAlmostEqual(shrink_probability(0.8, 20), 0.8, places=6)
        self.assertLess(shrink_probability(0.8, 1), 0.65)


class TestFeatures(unittest.TestCase):
    def setUp(self):
        self.rows = synthetic_rows(n_days=50, seed=7)
        self.games = feat.normalize_games(self.rows, 'mlb')
        self.engine = EloEngine(**config.LEAGUES['mlb']['elo'])
        self.records = feat.build(self.games, 'mlb', self.engine.replay(self.games), 4.3)

    def test_first_game_has_no_history(self):
        first = self.records[0]['features']
        for key in ('pyth_diff', 'form_diff', 'margin_diff', 'h2h_diff'):
            self.assertEqual(first[key], 0.0, key)

    def test_features_never_see_the_future(self):
        """Rewriting every later result must not change an earlier feature row.

        This is the property the previous model violated: it scored historical
        games with end-of-season standings.
        """
        cut = 40
        before = [dict(r['features']) for r in self.records[:cut]]
        tampered = []
        for i, row in enumerate(self.rows):
            row = dict(row)
            if i >= cut:
                row['home_score'], row['away_score'] = '99', '0'
                row['winner'] = row['home_team']
            tampered.append(row)
        games = feat.normalize_games(tampered, 'mlb')
        recs = feat.build(games, 'mlb', EloEngine(**config.LEAGUES['mlb']['elo']).replay(games), 4.3)
        after = [dict(r['features']) for r in recs[:cut]]
        self.assertEqual(before, after)

    def test_scheduled_games_do_not_advance_team_state(self):
        played = [g for g in self.games if g['final']]
        self.assertTrue(played)
        self.assertTrue(all(g['home_score'] is None for g in self.games if not g['final']))

    def test_zero_zero_final_is_treated_as_not_played(self):
        rows = [{'game_date': '2026-05-01', 'away_team': 'A', 'home_team': 'B',
                 'status': 'Final', 'winner': 'B', 'away_score': '0',
                 'home_score': '0', 'game_id': '1'}]
        self.assertFalse(feat.normalize_games(rows, 'mlb')[0]['final'])

    def test_vector_matches_declared_feature_order(self):
        rec = self.records[10]
        self.assertEqual(rec['vector'], [rec['features'][n] for n in feat.FEATURE_NAMES])

    def test_season_boundaries_follow_the_sport(self):
        self.assertEqual(feat.season_of(date(2026, 1, 15), 'nba'), 2025)
        self.assertEqual(feat.season_of(date(2026, 10, 15), 'nba'), 2026)
        self.assertEqual(feat.season_of(date(2026, 1, 15), 'nfl'), 2025)
        self.assertEqual(feat.season_of(date(2026, 6, 15), 'mlb'), 2026)

    def test_back_to_back_penalty_is_sport_specific(self):
        self.assertEqual(feat._short_rest(0, 'nba'), 1.0)
        self.assertEqual(feat._short_rest(2, 'nba'), 0.0)
        self.assertEqual(feat._short_rest(4, 'nfl'), 1.0)
        self.assertEqual(feat._short_rest(7, 'nfl'), 0.0)


class TestTraining(unittest.TestCase):
    """On data where strength genuinely drives results, the model must find it."""

    @classmethod
    def setUpClass(cls):
        cls.rows = synthetic_rows(n_days=150, seed=11, strength_spread=1.2)
        cls.trained = model.train('mlb', cls.rows, config.LEAGUES['mlb'], tune=False)

    def test_beats_a_coin_flip_out_of_sample(self):
        metrics = self.trained['metrics']
        self.assertGreater(metrics['n'], 100)
        self.assertGreater(metrics['acc'], 0.58)
        self.assertLess(metrics['logloss'], 0.68)

    def test_elo_is_the_dominant_feature_when_strength_drives_results(self):
        top = [i['feature'] for i in self.trained['importance'][:3]]
        self.assertIn('elo_diff', top)

    def test_predictions_stay_inside_the_league_cap(self):
        for rec in self.trained['records']:
            p = model.predict(rec, self.trained, config.LEAGUES['mlb'], 'mlb')['prob']
            self.assertGreaterEqual(p, 0.20 - 1e-9)
            self.assertLessEqual(p, 0.80 + 1e-9)

    def test_strong_home_team_is_favoured(self):
        # Team J is the strongest, Team A the weakest, by construction.
        recs = [r for r in self.trained['records']
                if r['game']['home'] == 'Team J' and r['game']['away'] == 'Team A']
        self.assertTrue(recs)
        probs = [model.predict(r, self.trained, config.LEAGUES['mlb'], 'mlb')['prob']
                 for r in recs[-5:]]
        self.assertGreater(sum(probs) / len(probs), 0.6)

    def test_blend_weight_is_a_valid_proportion(self):
        self.assertGreaterEqual(self.trained['blend_w'], 0.0)
        self.assertLessEqual(self.trained['blend_w'], 1.0)

    def test_handles_a_league_with_no_completed_games(self):
        rows = [r for r in synthetic_rows(n_days=5, seed=2)]
        for r in rows:
            r['status'] = 'Scheduled'
            r['winner'] = ''
        trained = model.train('nhl', rows, config.LEAGUES['nhl'], tune=False)
        self.assertEqual(trained['n_final'], 0)
        self.assertIsNone(trained['model'])
        p = model.predict(trained['records'][0], trained, config.LEAGUES['nhl'], 'nhl')
        self.assertGreater(p['prob'], 0.0)
        self.assertLess(p['prob'], 1.0)

    def test_handles_an_entirely_empty_league(self):
        trained = model.train('nba', [], config.LEAGUES['nba'], tune=False)
        self.assertEqual(trained['n_games'], 0)

    def test_tuning_does_not_make_elo_worse(self):
        params, metric = model.tune_elo('mlb', self.rows, config.LEAGUES['mlb'])
        self.assertIsNotNone(metric)
        base = EloEngine(**config.LEAGUES['mlb']['elo'])
        games = feat.normalize_games(self.rows, 'mlb')
        recs = base.replay(games)
        from sportspred.glm import score
        idx = [i for i, g in enumerate(games) if g['final']]
        warm = int(len(idx) * 0.25)
        baseline = score([recs[i]['elo_home_prob'] for i in idx[warm:]],
                         [1 if games[i]['home_score'] > games[i]['away_score'] else 0
                          for i in idx[warm:]])
        self.assertLessEqual(metric['logloss'], baseline['logloss'] + 1e-6)


if __name__ == '__main__':
    unittest.main()
