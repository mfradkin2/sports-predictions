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
        self.assertEqual(props.player_group('football', 'LB'), '')   # defence: no props
        self.assertEqual(props.player_group('basketball', 'PG'), 'skater')

    def test_unknown_football_position_is_skipped(self):
        self.assertEqual(props.player_group('football', 'LS'), '')

    def test_every_league_defines_props_for_its_groups(self):
        for sport, quota in config.GROUP_QUOTA.items():
            for group, _ in quota:
                self.assertTrue(config.props_for(sport, group),
                                f'{sport}/{group} has no props defined')



class TestBookPricedPlayersMakeTheBoard(unittest.TestCase):
    """A player the sportsbooks price belongs on the board, whatever our own
    relevance filter would have said. Before this, a team's fourth receiver
    and change-of-pace back were missing even though every book listed them."""

    def _reserve(self):
        # Two games, one catch for nine yards: below every football minimum.
        return {'id': 'r1', 'name': 'Deep Reserve', 'pos': 'WR',
                'stats': {'gp': 2, 'receptions': 1, 'receivingYards': 9, 'receivingTouchdowns': 0}}

    def _blank(self):
        # Played, but the stat sheet is empty for him.
        return {'id': 'r2', 'name': 'Blank Slate', 'pos': 'TE',
                'stats': {'gp': 2, 'receptions': 0, 'receivingYards': 0, 'receivingTouchdowns': 0}}

    def test_without_a_book_line_the_minimum_still_applies(self):
        for player in (self._reserve(), self._blank()):
            priced, _ = props.project_player(player, 'football', 'wr', 1.0)
            self.assertEqual(priced, [], player['name'])

    def test_a_booked_market_is_priced_however_small_the_projection(self):
        for player in (self._reserve(), self._blank()):
            priced, gp = props.project_player(player, 'football', 'wr', 1.0,
                                              booked={'rec', 'anytd'})
            keys = {p['key'] for p in priced}
            self.assertEqual(keys, {'rec', 'anytd'}, player['name'])
            self.assertEqual(gp, 2)
            for p in priced:
                self.assertGreaterEqual(p['proj'], 0)
                self.assertIn(p['pick'], ('over', 'under'))

    def test_a_player_who_has_not_played_is_still_left_alone(self):
        # No games and no last season to lean on: a book line is not enough
        # to invent a projection from nothing.
        rookie = {'id': 'r3', 'name': 'Not Yet', 'pos': 'WR', 'stats': {'gp': 0}}
        priced, _ = props.project_player(rookie, 'football', 'wr', 1.0, booked={'rec', 'anytd'})
        self.assertEqual(priced, [])

    def test_the_board_carries_every_player_the_books_price(self):
        pool = {'buffalobills': [
            {'id': 'qb', 'name': 'Star Passer', 'pos': 'QB',
             'stats': {'gp': 2, 'passingYards': 540, 'completions': 46, 'passingAttempts': 70,
                       'passingTouchdowns': 4, 'interceptions': 1}},
            self._reserve(), self._blank(),
        ]}
        lines = {}
        for name, key, line in (('deepreserve', 'rec', 1.5), ('deepreserve', 'anytd', 0.5),
                                ('blankslate', 'anytd', 0.5), ('starpasser', 'pass_yds', 250.5)):
            lines[(name, key)] = {'line': line, 'books': 4, 'book': '4 books', 'over': -110, 'under': -110}
        game = {'home': 'Buffalo Bills', 'away': 'Miami Dolphins', 'game_id': 'g1'}
        env = {'league_avg': 22.0, 'teams': {}}
        board = props.build_for_game(game, pool, env, config.LEAGUES['nfl'], 'football', 0.55,
                                         lines=lines, book_mode=True)
        names = [p['name'] for p in board['home']]
        self.assertIn('Deep Reserve', names)
        self.assertIn('Blank Slate', names)
        self.assertEqual(names[0], 'Star Passer')      # the stars still lead the board
        reserve = next(p for p in board['home'] if p['name'] == 'Deep Reserve')
        priced = [p for p in reserve['props'] if not p.get('pending')]
        self.assertEqual({p['key'] for p in priced}, {'rec', 'anytd'})
        for p in priced:
            self.assertEqual(p['line_source'], 'book')
            self.assertLess(abs(p.get('edge_pts', 0)), 40)   # no manufactured edge



class TestMarketCategories(unittest.TestCase):
    """The categories the page groups markets under, sportsbook style."""

    def test_every_market_lands_in_exactly_one_category(self):
        for sport, by_group in config.PROPS.items():
            markets = sorted({s['key'] for specs in by_group.values() for s in specs})
            placed = [m for g in config.prop_groups_for(sport) for m in g['markets']]
            self.assertEqual(sorted(placed), markets, sport)      # all of them
            self.assertEqual(len(placed), len(set(placed)), sport)  # once each

    def test_a_new_market_shows_up_under_more_rather_than_vanishing(self):
        saved = config.PROPS['football']['qb']
        try:
            config.PROPS['football'] = dict(config.PROPS['football'],
                                            qb=saved + [{'key': 'pass_longest', 'label': 'Longest Pass'}])
            groups = config.prop_groups_for('football')
            more = [g for g in groups if g['key'] == 'more']
            self.assertEqual(len(more), 1)
            self.assertIn('pass_longest', more[0]['markets'])
        finally:
            config.PROPS['football'] = dict(config.PROPS['football'], qb=saved)
        self.assertFalse([g for g in config.prop_groups_for('football') if g['key'] == 'more'])

    def test_the_categories_ship_with_the_payload(self):
        for key, cfg in config.LEAGUES.items():
            groups = config.prop_groups_for(cfg['sport'])
            self.assertTrue(groups, key)
            for g in groups:
                self.assertTrue(g['key'] and g['label'] and g['markets'], (key, g))


if __name__ == '__main__':
    unittest.main()


class TestSmallSampleShrinkage(unittest.TestCase):
    def setUp(self):
        self.pool = player_pool(['Team A', 'Team B', 'Team C', 'Team D'])
        self.priors = props.group_priors(self.pool, 'baseball')

    def test_priors_exist_for_the_batter_markets(self):
        self.assertIn('hits_pg', self.priors['batter'])
        self.assertIn('p_so_pg', self.priors['pitcher:starter'])

    def test_a_four_game_hot_streak_is_pulled_toward_the_group(self):
        hot = {'id': 'x', 'name': 'Hot Callup', 'pos': 'LF',
               'stats': {'gp': 4, 'ab': 16, 'hits': 10, 'hr': 3, 'rbi': 8, 'runs': 6,
                         'doubles': 2, 'triples': 0, 'sb': 0}}
        raw, _ = props.project_player(hot, 'baseball', 'batter', 1.0, max_props=None)
        shrunk, _ = props.project_player(hot, 'baseball', 'batter', 1.0, max_props=None,
                                         priors=self.priors)
        raw_hits = next(p for p in raw if p['key'] == 'hits')
        shr_hits = next(p for p in shrunk if p['key'] == 'hits')
        self.assertLess(shr_hits['proj'], raw_hits['proj'] * 0.6)
        # The page still shows the player's true season rate.
        self.assertEqual(shr_hits['season'], raw_hits['season'])
        self.assertEqual(shr_hits['season'], 2.5)

    def test_a_full_season_barely_moves(self):
        regular = self.pool['teama'][0]
        raw, _ = props.project_player(regular, 'baseball', 'batter', 1.0, max_props=None)
        shrunk, _ = props.project_player(regular, 'baseball', 'batter', 1.0, max_props=None,
                                         priors=self.priors)
        a = next(p for p in raw if p['key'] == 'hits')['proj']
        b = next(p for p in shrunk if p['key'] == 'hits')['proj']
        self.assertLess(abs(a - b) / a, 0.15)


class TestTwoWayPlayers(unittest.TestCase):
    """A hitter who also pitched keeps his batting games; the pitching line is
    rated per appearance."""

    def test_pitching_games_do_not_replace_batting_games(self):
        bat = extract_stats('baseball', ['gamesPlayed', 'hits', 'atBats', 'homeRuns'],
                            [139, 139, 502, 45])
        pitch = extract_stats('baseball', ['gamesPlayed', 'inningsPitched', 'strikeouts'],
                              [14, 70.0, 84], category='pitching')
        merged = dict(bat)
        merged.update({k: v for k, v in pitch.items() if k != '__avg__'})
        self.assertEqual(merged['gp'], 139)
        self.assertEqual(merged['p_gp'], 14)
        out = props.per_game(merged)
        self.assertAlmostEqual(out['hits'], 1.0, places=6)
        self.assertAlmostEqual(out['p_so'], 6.0, places=6)
        self.assertAlmostEqual(out['ip'], 5.0, places=6)
        self.assertEqual(out['gp'], 139)

    def test_a_pure_pitcher_still_has_games(self):
        pitch = extract_stats('baseball', ['gamesPlayed', 'inningsPitched', 'strikeouts'],
                              [28, 170.1, 180], category='pitching')
        out = props.per_game(pitch)
        self.assertEqual(out['gp'], 28)
        self.assertAlmostEqual(out['p_so'], 180 / 28, places=6)

    def test_position_player_who_pitched_once_is_priced_as_a_hitter(self):
        stats = {'gp': 120, 'ab': 400, 'hits': 102, 'hr': 5, 'rbi': 40, 'runs': 45,
                 'doubles': 20, 'triples': 1, 'sb': 2, 'p_gp': 2, 'ip': 2.0, 'p_so': 1, 'p_er': 3}
        priced, gp = props.project_player({'stats': stats, 'pos': 'C'}, 'baseball', 'batter', 1.0)
        self.assertEqual(gp, 120)
        hits = next(p for p in priced if p['key'] == 'hits')
        self.assertAlmostEqual(hits['season'], 0.85, places=2)


class TestBinomialHits(unittest.TestCase):
    def test_a_regular_goes_hitless_less_often_than_poisson_says(self):
        from sportspred.util import binom_sf, poisson_sf
        # One hit a game over four at-bats.
        self.assertGreater(binom_sf(0, 4.0, 0.25), poisson_sf(0, 1.0))
        self.assertAlmostEqual(binom_sf(0, 4.0, 0.25), 1 - 0.75 ** 4, places=6)
        # Fractional trials interpolate.
        mid = binom_sf(0, 3.5, 0.25)
        self.assertTrue(binom_sf(0, 3.0, 0.25) < mid < binom_sf(0, 4.0, 0.25))
        self.assertEqual(binom_sf(3, 3.0, 0.5), 0.0)

    def test_hits_are_priced_binomially_from_at_bats(self):
        spec = next(s for s in config.props_for('baseball', 'batter') if s['key'] == 'hits')
        self.assertEqual(spec['dist'], 'binomial')
        player = {'stats': {'gp': 100, 'ab': 400, 'hits': 100, 'hr': 10, 'rbi': 50,
                            'runs': 50, 'doubles': 20, 'triples': 2, 'sb': 5}}
        priced, _ = props.project_player(player, 'baseball', 'batter', 1.0, max_props=None)
        hits = next(p for p in priced if p['key'] == 'hits')
        # 1.0 hits over 4.0 at-bats: P(1+) = 1 - 0.75^4 = 0.684
        self.assertAlmostEqual(hits['over'], 1 - 0.75 ** 4, places=3)


class TestPreviousSeasonPrior(unittest.TestCase):
    def test_one_game_leans_on_last_season(self):
        qb = {'stats': {'gp': 1, 'pass_yds': 334, 'pass_att': 29, 'pass_cmp': 20, 'pass_td': 2, 'pass_int': 0,
                        'rush_yds': 23, 'rush_att': 6},
              'pos': 'QB',
              'prev': {'gp': 17, 'pass_yds': 4080, 'pass_att': 544, 'pass_cmp': 357, 'pass_td': 34, 'pass_int': 8,
                       'rush_yds': 510, 'rush_att': 102}}
        priced, gp = props.project_player(qb, 'football', 'qb', 1.0, max_props=None)
        yds = next(p for p in priced if p['key'] == 'pass_yds')
        # (1 x 334 + 5 x 240) / 6 = 255.7, not last week's 334
        self.assertAlmostEqual(yds['proj'], (334 + 5 * 240) / 6, delta=0.5)
        self.assertEqual(yds['season'], 334)

    def test_a_thin_previous_season_is_ignored(self):
        rb = {'stats': {'gp': 1, 'rush_yds': 156, 'rush_att': 29, 'rec': 5, 'rec_yds': 30, 'rush_td': 2, 'rec_td': 0},
              'pos': 'RB', 'prev': {'gp': 2, 'rush_yds': 40, 'rush_att': 10}}
        priced, _ = props.project_player(rb, 'football', 'rb', 1.0, max_props=None)
        self.assertAlmostEqual(next(p for p in priced if p['key'] == 'rush_yds')['proj'], 156, delta=0.5)

    def test_previous_season_id(self):
        from datetime import date
        from sportspred.pipeline import prev_season
        self.assertEqual(prev_season('nfl', date(2026, 9, 16)), 2025)
        self.assertEqual(prev_season('mlb', date(2026, 9, 16)), 2025)
        self.assertEqual(prev_season('nhl', date(2026, 10, 20)), 2026)   # 2026-27 is "2027"
        self.assertEqual(prev_season('nba', date(2027, 2, 1)), 2026)

    def test_yes_only_markets_get_a_market_probability(self):
        self.assertAlmostEqual(props.implied_over(150, None), 0.4 / 1.06, places=4)
        self.assertIsNone(props.implied_over(None, None))


def _hockey_pool():
    def skater(i, team, gp, goals, assists, sog):
        return {'id': f'{team}s{i}', 'name': f'{team} Skater {i}', 'pos': 'C' if i % 2 else 'D', 'team': team,
                'stats': {'gp': gp, 'goals': goals, 'assists': assists, 'points': goals + assists,
                          'sog': sog, 'blocks': 40, 'toi': 18.0}}
    def goalie(team, gp, saves):
        return {'id': f'{team}g', 'name': f'{team} Goalie', 'pos': 'G', 'team': team,
                'stats': {'gp': gp, 'saves': saves, 'ga': int(gp * 2.6), 'sv_pct': 0.91}}
    pool = {}
    for team in ('Boston Bruins', 'Toronto Maple Leafs'):
        key = team.lower().replace(' ', '')
        pool[key] = [skater(i, key, 78, 20 + 3 * i, 25 + 2 * i, 180 + 10 * i) for i in range(8)] + [goalie(key, 55, 1500)]
    return pool


def _basketball_pool():
    def player(i, team, gp):
        return {'id': f'{team}p{i}', 'name': f'{team} Player {i}', 'pos': 'G' if i < 2 else 'F', 'team': team,
                'stats': {'gp': gp, 'pts': 12 + 3 * i, 'reb': 4 + i, 'ast': 2 + i, 'fg3': 1 + i * 0.3,
                          'stl': 1.0, 'blk': 0.5, 'min': 30, '__avg__': ['pts', 'reb', 'ast', 'fg3', 'stl', 'blk', 'min']}}
    return {t.lower().replace(' ', ''): [player(i, t.lower().replace(' ', ''), 70) for i in range(9)]
            for t in ('Boston Celtics', 'Miami Heat')}


class TestHockeyAndBasketballBoards(unittest.TestCase):
    """The winter leagues run through the same code, but their feeds, groups
    and markets differ; this pins down that a board comes out of each."""

    def _env(self, cfg, home, away):
        return props.team_environment(
            [dict(final=True, home=home, away=away, home_score=4, away_score=2)] * 10, cfg)

    def test_hockey_board_prices_skaters_and_the_goalie_against_book_lines(self):
        from sportspred.odds import _name_key
        cfg, pool = config.LEAGUES['nhl'], _hockey_pool()
        env = self._env(cfg, 'Boston Bruins', 'Toronto Maple Leafs')
        star = pool['bostonbruins'][7]['name']
        lines = {(_name_key(star), 'sog'): {'line': 3.5, 'books': 3, 'book': '3 books', 'over': -120, 'under': 100},
                 (_name_key(star), 'points'): {'line': 0.5, 'books': 2, 'book': '2 books', 'over': -150, 'under': 120},
                 (_name_key('bostonbruins Goalie'), 'saves'): {'line': 27.5, 'books': 1, 'book': 'DraftKings', 'over': -110, 'under': -110}}
        board = props.build_for_game({'home': 'Boston Bruins', 'away': 'Toronto Maple Leafs', 'game_id': '9'},
                                     pool, env, cfg, 'hockey', 0.6, lines=lines, book_mode=True)
        home = {p['name']: p for p in board['home']}
        self.assertIn(star, home)
        booked = {p['key']: p for p in home[star]['props'] if not p.get('pending')}
        self.assertEqual(booked['sog']['line'], 3.5)
        self.assertEqual(booked['sog']['line_source'], 'book')
        self.assertIn('edge_pts', booked['sog'])
        goalie = home['bostonbruins Goalie']
        saves = next(p for p in goalie['props'] if p['key'] == 'saves')
        self.assertEqual(saves['line'], 27.5)
        self.assertIn('pick', saves)
        # Away side has no lines yet: all blank, but present.
        self.assertTrue(board['away'])
        self.assertTrue(all(p.get('pending') for pl in board['away'] for p in pl['props']))

    def test_basketball_board_uses_the_feeds_averages(self):
        from sportspred.odds import _name_key
        cfg, pool = config.LEAGUES['nba'], _basketball_pool()
        env = self._env(cfg, 'Boston Celtics', 'Miami Heat')
        star = pool['bostonceltics'][8]['name']
        lines = {(_name_key(star), 'pts'): {'line': 34.5, 'books': 4, 'book': '4 books', 'over': -110, 'under': -110},
                 (_name_key(star), 'pra'): {'line': 52.5, 'books': 2, 'book': '2 books', 'over': -115, 'under': -105}}
        board = props.build_for_game({'home': 'Boston Celtics', 'away': 'Miami Heat', 'game_id': '8'},
                                     pool, env, cfg, 'basketball', 0.55, lines=lines, book_mode=True)
        home = {p['name']: p for p in board['home']}
        pts = next(p for p in home[star]['props'] if p['key'] == 'pts')
        self.assertEqual(pts['line'], 34.5)
        self.assertEqual(pts['season'], 36.0)          # the feed's own per-game average
        self.assertIn(pts['pick'], ('over', 'under'))
        pra = next(p for p in home[star]['props'] if p['key'] == 'pra')
        self.assertAlmostEqual(pra['season'], 36 + 12 + 10, places=1)

    def test_opening_night_prices_from_last_season(self):
        cfg, pool = config.LEAGUES['nhl'], _hockey_pool()
        env = self._env(cfg, 'Boston Bruins', 'Toronto Maple Leafs')
        # Nobody has played yet, but everyone has last season on record.
        for roster in pool.values():
            for p in roster:
                p['prev'] = dict(p['stats'])
                p['stats'] = {k: 0 for k in p['stats']}
        board = props.build_for_game({'home': 'Boston Bruins', 'away': 'Toronto Maple Leafs', 'game_id': '7'},
                                     pool, env, cfg, 'hockey', 0.5, book_mode=False)
        self.assertTrue(board['home'])
        sog = next(p for pl in board['home'] for p in pl['props'] if p['key'] == 'sog')
        self.assertTrue(sog.get('season_prev'))
        self.assertGreater(sog['proj'], 1.5)
        # And with no previous season either, nothing is invented.
        for roster in pool.values():
            for p in roster:
                p.pop('prev')
        empty = props.build_for_game({'home': 'Boston Bruins', 'away': 'Toronto Maple Leafs', 'game_id': '6'},
                                     pool, env, cfg, 'hockey', 0.5, book_mode=False)
        self.assertEqual(empty['home'], [])


class TestStarterPriors(unittest.TestCase):
    def test_group_prior_reflects_starters_not_the_bench(self):
        pool = {'t': [{'id': str(i), 'pos': 'WR', 'stats': {'gp': 1, 'rec_yds': y, 'rec': max(1, y // 12), 'targets': 3}}
                      for i, y in enumerate([110, 95, 80, 70, 60, 8, 5, 3, 0, 0])]}
        tiers = props.group_priors(pool, 'football')['wr']['rec_yds_pg']
        self.assertGreater(tiers['hi'], 70)    # the upper half, not the 43-yard roster mean
        self.assertLess(tiers['lo'], 10)
        # A fourth receiver is shrunk toward the reserves, a starter toward the starters.
        self.assertEqual(props.group_prior_for({'rec_yds_pg': tiers}, 'rec_yds_pg', 5), tiers['lo'])
        self.assertEqual(props.group_prior_for({'rec_yds_pg': tiers}, 'rec_yds_pg', 90), tiers['hi'])


class TestStartingPitcherRole(unittest.TestCase):
    def test_a_swingman_with_a_starter_line_is_priced_as_a_starter(self):
        from sportspred.odds import _name_key
        cfg = config.LEAGUES['mlb']
        pool = player_pool(['Team A', 'Team B', 'Team C', 'Team D'])   # enough pitchers for a prior
        # A call-up with seven mostly-relief outings.
        swing = {'id': 'sw', 'name': 'Team A Swingman', 'short': 'SW', 'pos': 'P', 'team': 'Team A',
                 'stats': {'gp': 7, 'p_gp': 7, 'starts': 2, 'ip': 14.0, 'p_so': 12, 'p_er': 8, 'p_h': 14}}
        pool['teama'].append(swing)
        env = props.team_environment(
            [dict(final=True, home='Team A', away='Team B', home_score=5, away_score=4)] * 10, cfg)
        game = {'home': 'Team A', 'away': 'Team B', 'game_id': '1'}
        lines = {(_name_key(swing['name']), 'outs'): {'line': 14.5, 'books': 3, 'book': '3 books', 'over': -110, 'under': -110},
                 (_name_key(swing['name']), 'er'): {'line': 2.5, 'books': 3, 'book': '3 books', 'over': -120, 'under': 100}}
        board = props.build_for_game(game, pool, env, cfg, 'baseball', 0.55, lines=lines, book_mode=True)
        me = next(p for p in board['home'] if p['id'] == 'sw')
        outs = next(p for p in me['props'] if p['key'] == 'outs')
        # Two innings an outing as a reliever would project ~6 outs; as a
        # starter he is shrunk toward starters (about 15) instead.
        self.assertGreater(outs['proj'], 11)
        # Priced as the reliever his season says he is, the outs prop does
        # not even clear the minimum for a board.
        as_reliever, _ = props.project_player(swing, 'baseball', 'pitcher', 1.0, max_props=None,
                                              priors=props.group_priors(pool, 'baseball'))
        self.assertNotIn('outs', [p['key'] for p in as_reliever])


class TestTotalBasesAtLeastOne(unittest.TestCase):
    def test_tb_over_half_is_the_chance_of_a_hit(self):
        rates = {'hits_pg': 0.9, 'ab_pg': 3.8}
        pz = props.hitless_chance(rates)
        self.assertAlmostEqual(pz, (1 - 0.9 / 3.8) ** 3.8, places=6)
        spec = next(s for s in config.props_for('baseball', 'batter') if s['key'] == 'tb')
        tail = props.total_bases_tail(dict(rates, doubles=0.2, triples=0.02, hr=0.15))
        spec = dict(spec, line=0.5, p_le=tail)
        line, p_over = props.over_probability(spec, 1.2)
        self.assertAlmostEqual(p_over, 1 - pz, places=4)
        # 1.5 is "two hits or an extra-base hit": lower than 0.5 but above the
        # bare chance of two hits.
        _, p2 = props.over_probability(dict(spec, line=1.5), 1.2)
        self.assertLess(p2, p_over)
        self.assertGreater(p2, 1 - tail[0] - (3.8 * (0.9 / 3.8) * (1 - 0.9 / 3.8) ** 2.8))
        # Other lines still come from the count distribution.
        _, p3 = props.over_probability(dict(spec, line=2.5), 1.2)
        self.assertLess(p3, p2)
