"""Player prop projections: rate normalisation, pricing, and board assembly."""
import unittest

from sportspred import config, props
from sportspred.espn import extract_stats, norm_team, is_average_name
from tests.helpers import player_pool


class TestRateNormalisation(unittest.TestCase):
    def test_season_totals_are_divided_by_games(self):
        out = props.per_game({'gp': 140, 'hits': 163, 'hr': 34, 'ab': 525})
        self.assertAlmostEqual(out['hits'], 163 / 140, places=6)
        self.assertAlmostEqual(out['hr'], 34 / 140, places=6)

    def test_a_small_total_is_not_mistaken_for_a_rate(self):
        """Two triples in a season is not two triples a game.

        The value sits under any plausible per-game ceiling, so it can only be
        classified by looking at the rest of the block.
        """
        out = props.per_game({'gp': 140, 'hits': 163, 'triples': 2})
        self.assertAlmostEqual(out['triples'], 2 / 140, places=6)

    def test_espn_named_averages_are_left_alone(self):
        stats = extract_stats(
            'basketball',
            ['gamesPlayed', 'avgPoints', 'avgRebounds', 'fieldGoalsAttempted'],
            [60, 24.6, 7.1, 1180])
        out = props.per_game(stats)
        self.assertAlmostEqual(out['pts'], 24.6, places=6)
        self.assertAlmostEqual(out['reb'], 7.1, places=6)
        # The one genuine total in the same block still gets converted.
        self.assertAlmostEqual(out['fga'], 1180 / 60, places=6)

    def test_average_name_detection(self):
        self.assertTrue(is_average_name('avgPoints'))
        self.assertTrue(is_average_name('pointsPerGame'))
        self.assertFalse(is_average_name('points'))
        self.assertFalse(is_average_name('homeRuns'))

    def test_totals_with_no_games_played_are_dropped(self):
        self.assertNotIn('hits', props.per_game({'gp': 0, 'hits': 163}))

    def test_clock_values_become_minutes(self):
        out = extract_stats('hockey', ['timeOnIcePerGame'], ['18:42'])
        self.assertAlmostEqual(out['toi'], 18.7, places=1)


class TestDerivedStats(unittest.TestCase):
    def test_total_bases_uses_extra_base_hits(self):
        rates = props.derive('baseball', {'hits': 1.0, 'doubles': 0.2,
                                          'triples': 0.02, 'hr': 0.2, 'gp': 140})
        # singles .58 + 2(.2) + 3(.02) + 4(.2) = 1.84
        self.assertAlmostEqual(rates['tb_pg'], 1.84, places=6)

    def test_total_bases_falls_back_when_the_split_is_missing(self):
        rates = props.derive('baseball', {'hits': 1.0, 'gp': 140})
        self.assertGreater(rates['tb_pg'], 1.0)

    def test_basketball_combos(self):
        r = props.derive('basketball', {'pts': 20.0, 'reb': 5.0, 'ast': 4.0, 'gp': 60})
        self.assertEqual(r['pra_pg'], 29.0)
        self.assertEqual(r['pr_pg'], 25.0)
        self.assertEqual(r['pa_pg'], 24.0)

    def test_outs_recorded_from_innings(self):
        self.assertAlmostEqual(props.derive('baseball', {'ip': 5.5, 'gp': 30})['outs_pg'],
                               16.5, places=6)


class TestPricing(unittest.TestCase):
    def test_line_is_a_half_point_off_the_baseline(self):
        for baseline in (6.3, 7.0, 0.42, 24.9, 252.0):
            line = props.standard_line(baseline)
            self.assertAlmostEqual(line % 1, 0.5, places=9)

    def test_line_is_anchored_to_the_baseline_not_the_projection(self):
        """A line that moves with the projection would price everything at 50%."""
        spec = config.PROPS['basketball']['skater'][0]
        flat = props._price(spec, 24.1, 24.1)
        boosted = props._price(spec, 24.1, 26.5)
        self.assertEqual(flat['line'], boosted['line'])
        self.assertGreater(boosted['over'], flat['over'] + 0.05)

    def test_probability_rises_with_the_projection(self):
        spec = config.PROPS['football']['qb'][0]
        probs = [props._price(spec, 250.0, 250.0 * f)['over']
                 for f in (0.85, 1.0, 1.15)]
        self.assertLess(probs[0], probs[1])
        self.assertLess(probs[1], probs[2])

    def test_anytime_scorer_matches_poisson(self):
        spec = config.PROPS['hockey']['skater'][2]     # anytime goal, line 0.5
        priced = props._price(spec, 0.42, 0.42)
        self.assertAlmostEqual(priced['over'], 1 - pow(2.718281828459045, -0.42), places=3)

    def test_edge_measures_movement_off_the_baseline(self):
        spec = config.PROPS['baseball']['batter'][2]   # home run, fixed 0.5 line
        # An unchanged projection is not an edge, however lopsided the line is.
        self.assertAlmostEqual(props._price(spec, 0.2, 0.2)['edge'], 0.0, places=9)
        self.assertGreater(props._price(spec, 0.2, 0.3)['edge'], 0.0)

    def test_confidence_tiers(self):
        self.assertEqual(props.confidence(0.5), 'low')
        self.assertEqual(props.confidence(0.62), 'med')
        self.assertEqual(props.confidence(0.75), 'high')

    def test_over_and_under_sum_to_one(self):
        spec = config.PROPS['hockey']['skater'][0]
        p = props._price(spec, 3.3, 3.6)
        self.assertAlmostEqual(p['over'] + p['under'], 1.0, places=4)

    def test_range_brackets_the_projection(self):
        spec = config.PROPS['basketball']['skater'][0]
        p = props._price(spec, 24.0, 24.0)
        self.assertLessEqual(p['range'][0], p['proj'])
        self.assertGreaterEqual(p['range'][1], p['proj'])


class TestMatchupEnvironment(unittest.TestCase):
    def setUp(self):
        self.games = [
            dict(final=True, home='Strong', away='Weak', home_score=8, away_score=2)
            for _ in range(20)
        ] + [
            dict(final=True, home='Weak', away='Strong', home_score=2, away_score=8)
            for _ in range(20)
        ]
        self.cfg = config.LEAGUES['mlb']
        self.env = props.team_environment(self.games, self.cfg)

    def test_scoring_indices_separate_the_teams(self):
        self.assertGreater(self.env['teams']['strong']['off_idx'], 1.0)
        self.assertLess(self.env['teams']['weak']['off_idx'], 1.0)

    def test_small_samples_are_shrunk_toward_the_league(self):
        env = props.team_environment(
            [dict(final=True, home='New', away='Other', home_score=20, away_score=0)],
            self.cfg)
        self.assertLess(env['teams']['new']['off_idx'], 2.0)

    def test_expected_scores_favour_the_stronger_side(self):
        exp = props.expected_scores(self.env, 'Strong', 'Weak', 0.75, self.cfg)
        self.assertGreater(exp['home'], exp['away'])

    def test_hitters_get_a_boost_in_a_high_scoring_spot(self):
        exp = props.expected_scores(self.env, 'Strong', 'Weak', 0.75, self.cfg)
        good = props.matchup_factor('hits', 'baseball', 'batter', exp, self.env,
                                    'Strong', 'Weak', True)
        bad = props.matchup_factor('hits', 'baseball', 'batter', exp, self.env,
                                   'Strong', 'Weak', False)
        self.assertGreater(good, bad)

    def test_factors_stay_within_sane_bounds(self):
        for is_home in (True, False):
            for key in ('hits', 'k', 'saves', 'pass_yds'):
                f = props.matchup_factor(key, 'baseball', 'pitcher', 
                                         props.expected_scores(self.env, 'Strong', 'Weak', 0.9, self.cfg),
                                         self.env, 'Strong', 'Weak', is_home)
                self.assertGreaterEqual(f, 0.70)
                self.assertLessEqual(f, 1.35)


class TestBoardAssembly(unittest.TestCase):
    def setUp(self):
        self.cfg = config.LEAGUES['mlb']
        self.pool = player_pool(['Team A', 'Team B'])
        self.env = props.team_environment(
            [dict(final=True, home='Team A', away='Team B', home_score=5, away_score=4)] * 10,
            self.cfg)
        self.game = {'home': 'Team A', 'away': 'Team B', 'game_id': '1'}

    def test_board_mixes_pitchers_and_hitters(self):
        board = props.build_for_game(self.game, self.pool, self.env, self.cfg,
                                     'baseball', 0.55)
        groups = {p['group'] for p in board['home']}
        self.assertIn('pitcher', groups)
        self.assertIn('batter', groups)

    def test_quota_caps_each_group(self):
        board = props.build_for_game(self.game, self.pool, self.env, self.cfg,
                                     'baseball', 0.55)
        pitchers = [p for p in board['home'] if p['group'] == 'pitcher']
        self.assertLessEqual(len(pitchers), 2)

    def test_probable_starter_is_promoted(self):
        name = [p['name'] for p in self.pool['teama'] if p['pos'] == 'P'][-1]
        board = props.build_for_game(
            self.game, self.pool, self.env, self.cfg, 'baseball', 0.55,
            starters={'home': {'name': name}})
        self.assertEqual(board['home'][0]['name'], name)
        self.assertEqual(board['home'][0]['role'], 'Probable starter')

    def test_unknown_team_yields_an_empty_side(self):
        board = props.build_for_game({'home': 'Nowhere', 'away': 'Team B', 'game_id': '2'},
                                     self.pool, self.env, self.cfg, 'baseball', 0.5)
        self.assertEqual(board['home'], [])

    def test_every_prop_carries_the_fields_the_page_reads(self):
        board = props.build_for_game(self.game, self.pool, self.env, self.cfg,
                                     'baseball', 0.55)
        for player in board['home']:
            for p in player['props']:
                for key in ('label', 'line', 'proj', 'season', 'over', 'under',
                            'pick', 'pick_prob', 'conf', 'edge', 'range', 'rank'):
                    self.assertIn(key, p)

    def test_team_name_aliases_resolve(self):
        self.assertEqual(norm_team('Oakland Athletics'), norm_team('Athletics'))


class TestPositionGroups(unittest.TestCase):
    def test_each_sport_maps_positions(self):
        self.assertEqual(props.player_group('baseball', 'SP'), 'pitcher')
        self.assertEqual(props.player_group('baseball', '1B'), 'batter')
        self.assertEqual(props.player_group('hockey', 'G'), 'goalie')
        self.assertEqual(props.player_group('hockey', 'C'), 'skater')
        self.assertEqual(props.player_group('football', 'QB'), 'qb')
        self.assertEqual(props.player_group('football', 'TE'), 'wr')
        self.assertEqual(props.player_group('football', 'LB'), 'def')
        self.assertEqual(props.player_group('basketball', 'PG'), 'skater')

    def test_unknown_football_position_is_skipped(self):
        self.assertEqual(props.player_group('football', 'LS'), '')

    def test_every_league_defines_props_for_its_groups(self):
        for sport, quota in config.GROUP_QUOTA.items():
            for group, _ in quota:
                self.assertTrue(config.props_for(sport, group),
                                f'{sport}/{group} has no props defined')


if __name__ == '__main__':
    unittest.main()
