"""The Python port of the R ingest, exercised against ESPN-shaped fixtures."""
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta

from sportspred import config, ingest
from sportspred.util import read_csv
from tests import espn_fixtures as fx


class TestStandings(unittest.TestCase):
    def test_walks_nested_groups(self):
        http = fx.FakeHttp([('baseball/mlb/standings', fx.mlb_standings())])
        teams = ingest.fetch_standings(http, 'mlb')
        self.assertEqual(len(ingest.unique_teams(teams)), 4)

    def test_indexes_by_id_name_and_abbreviation(self):
        http = fx.FakeHttp([('baseball/mlb/standings', fx.mlb_standings())])
        teams = ingest.fetch_standings(http, 'mlb')
        self.assertIs(teams['1'], teams['boston red sox'])
        self.assertIs(teams['1'], teams['bos'])

    def test_mlb_formula_matches_the_r_script(self):
        # 90-60, 720 for, 600 against.
        info = ingest._mlb_team({'wins': 90, 'losses': 60, 'RS': 720, 'RA': 600})
        self.assertEqual(info['record'], '90-60')
        self.assertAlmostEqual(info['win_pct'], 0.6, places=3)
        self.assertAlmostEqual(info['rs_pg'], 4.8, places=2)
        self.assertAlmostEqual(info['ra_pg'], 4.0, places=2)
        self.assertAlmostEqual(info['run_diff_pg'], 0.8, places=2)
        pyth = 720 ** 1.83 / (720 ** 1.83 + 600 ** 1.83)
        self.assertAlmostEqual(info['pyth_pct'], round(pyth, 3), places=3)
        self.assertAlmostEqual(info['strength'],
                               0.40 * pyth + 0.20 * 0.6 + 0.40 * ((0.8 + 3) / 6), places=6)

    def test_mlb_era_reweights_strength(self):
        info = ingest._mlb_team({'wins': 90, 'losses': 60, 'RS': 720, 'RA': 600})
        ingest._mlb_apply_era(info, 3.4)
        self.assertEqual(info['era'], 3.4)
        self.assertAlmostEqual(info['strength'],
                               0.35 * info['pyth_pct'] + 0.20 * info['win_pct']
                               + 0.25 * ((info['run_diff_pg'] + 3) / 6)
                               + 0.20 * ((6.0 - 3.4) / 4.0), places=6)

    def test_nhl_formula(self):
        info = ingest._nhl_team({'wins': 30, 'losses': 15, 'OTL': 5, 'PTS': 65,
                                 'GF': 170, 'GA': 140})
        self.assertEqual(info['record'], '30-15-5')
        self.assertAlmostEqual(info['pts_pct'], 65.0, places=1)       # 65 / 100 * 100
        self.assertAlmostEqual(info['gf_pg'], 3.4, places=2)
        self.assertAlmostEqual(info['pp_pct'], 20.0)                 # default
        self.assertAlmostEqual(info['save_pct'], 0.910)              # default

    def test_nhl_points_default_to_two_per_win_plus_otl(self):
        info = ingest._nhl_team({'wins': 10, 'losses': 5, 'OTL': 2})
        self.assertAlmostEqual(info['pts_pct'], round(22 / 34 * 100, 1), places=1)

    def test_nba_formula(self):
        info = ingest._nba_team({'wins': 40, 'losses': 15, 'PPG': 118.0, 'OPPG': 108.0})
        self.assertAlmostEqual(info['net_rtg'], 10.0)
        self.assertAlmostEqual(info['pace'], 100.0)
        self.assertAlmostEqual(info['strength'],
                               0.40 * ((10 + 15) / 30) + 0.25 * (40 / 55)
                               + 0.175 * (118 / 130) + 0.175 * (1 - 108 / 130), places=6)

    def test_nfl_formula_and_tie_record(self):
        info = ingest._nfl_team({'wins': 8, 'losses': 6, 'T': 1, 'PF': 350, 'PA': 300})
        self.assertEqual(info['record'], '8-6-1')
        self.assertAlmostEqual(info['win_pct'], 8.5 / 15, places=3)
        self.assertAlmostEqual(info['pt_diff_pg'], round(50 / 15, 1), places=1)
        self.assertEqual(info['ypg_off'], 340.0)                     # default until enriched

    def test_missing_stats_fall_back_to_the_r_defaults(self):
        info = ingest._mlb_team({})
        self.assertEqual(info['record'], '0-0')
        self.assertEqual(info['win_pct'], 0.5)
        self.assertEqual(info['rs_pg'], 4.5)
        self.assertEqual(info['pyth_pct'], 0.5)

    def test_lookup_falls_back_from_id_to_name_to_word(self):
        http = fx.FakeHttp([('baseball/mlb/standings', fx.mlb_standings())])
        teams = ingest.fetch_standings(http, 'mlb')
        self.assertEqual(ingest.lookup(teams, 'anything', '2')['name'], 'New York Yankees')
        self.assertEqual(ingest.lookup(teams, 'Boston Red Sox')['abbr'], 'BOS')
        self.assertEqual(ingest.lookup(teams, 'xx BOS yy')['abbr'], 'BOS')
        self.assertIsNone(ingest.lookup(teams, 'Nobody', '999'))

    def test_standings_outage_yields_no_teams(self):
        self.assertEqual(ingest.fetch_standings(fx.FakeHttp([]), 'mlb'), {})


class TestTeamStatistics(unittest.TestCase):
    def test_mlb_era_is_read_from_the_pitching_category(self):
        http = fx.FakeHttp([('baseball/mlb/standings', fx.mlb_standings()),
                            ('/teams/1/statistics', fx.mlb_team_statistics(3.40))])
        teams = ingest.fetch_standings(http, 'mlb')
        hits = ingest.enrich_team_statistics(http, 'mlb', teams)
        self.assertEqual(hits, 1)
        self.assertEqual(teams['1']['era'], 3.4)
        self.assertIsNone(teams['2']['era'])

    def test_nfl_yards_and_turnovers_replace_the_defaults(self):
        http = fx.FakeHttp([('football/nfl/standings', fx.nfl_standings()),
                            ('/teams/10/statistics', fx.nfl_team_statistics(380.5, 3, 7))])
        teams = ingest.fetch_standings(http, 'nfl')
        ingest.enrich_team_statistics(http, 'nfl', teams)
        kc = teams['10']
        self.assertEqual(kc['ypg_off'], 380.5)
        self.assertEqual(kc['to_margin'], 2.0)          # (7 - 3) / 2 games
        self.assertEqual(teams['11']['ypg_off'], 340.0)  # untouched

    def test_nfl_season_total_yards_are_normalised(self):
        http = fx.FakeHttp([('football/nfl/standings', fx.nfl_standings()),
                            ('/teams/10/statistics', fx.nfl_team_statistics(6800, 3, 7))])
        teams = ingest.fetch_standings(http, 'nfl')
        ingest.enrich_team_statistics(http, 'nfl', teams)
        self.assertEqual(teams['10']['ypg_off'], 3400.0)   # 6800 / 2 games


class TestScoreboardWindow(unittest.TestCase):
    def setUp(self):
        self.today = date.today()
        self.http = fx.FakeHttp(fx.mlb_routes(self.today))
        self.teams = ingest.fetch_standings(self.http, 'mlb')

    def test_window_covers_lookback_and_forward(self):
        os.environ.pop('SP_LOOKBACK_DAYS', None)
        rows, days = ingest.fetch_window(self.http, 'mlb', self.teams, lookback=20, forward=3)
        dates = sorted({r['game_date'] for r in rows})
        self.assertEqual(dates[0], str(self.today - timedelta(days=20)))
        self.assertEqual(dates[-1], str(self.today + timedelta(days=3)))
        self.assertEqual(days, 24)

    def test_env_override_widens_the_window(self):
        os.environ['SP_LOOKBACK_DAYS'] = '5'
        try:
            rows, _ = ingest.fetch_window(self.http, 'mlb', self.teams, lookback=20, forward=0)
            self.assertEqual(min(r['game_date'] for r in rows),
                             str(self.today - timedelta(days=5)))
        finally:
            os.environ.pop('SP_LOOKBACK_DAYS', None)

    def test_rows_carry_the_r_column_set(self):
        rows, _ = ingest.fetch_window(self.http, 'mlb', self.teams, lookback=2, forward=1)
        expected = ['game_date', 'away_team', 'home_team', 'status', 'winner', 'favored_team',
                    'away_win_probability', 'home_win_probability', 'game_id',
                    'away_score', 'home_score', 'away_record', 'home_record',
                    'away_win_pct', 'home_win_pct', 'away_pyth_pct', 'home_pyth_pct',
                    'away_run_diff_pg', 'home_run_diff_pg', 'away_rs_pg', 'home_rs_pg',
                    'away_ra_pg', 'home_ra_pg', 'away_era', 'home_era',
                    'away_probable_starter', 'home_probable_starter']
        for col in expected:
            self.assertIn(col, rows[0], col)
        for col in ('season_type', 'season_slug', 'game_start_utc', 'game_time'):
            self.assertIn(col, rows[0], col)

    def test_finals_carry_a_winner_and_scheduled_games_do_not(self):
        rows, _ = ingest.fetch_window(self.http, 'mlb', self.teams, lookback=2, forward=1)
        finals = [r for r in rows if r['status'] == 'Final']
        future = [r for r in rows if r['status'] == 'Scheduled']
        self.assertTrue(finals and future)
        self.assertTrue(all(r['winner'] for r in finals))
        self.assertTrue(all(r['winner'] == '' for r in future))

    def test_probable_starters_are_captured(self):
        rows, _ = ingest.fetch_window(self.http, 'mlb', self.teams, lookback=0, forward=1)
        future = [r for r in rows if r['status'] == 'Scheduled'][0]
        self.assertEqual(future['away_probable_starter'], 'Ace Pitcher')
        self.assertEqual(future['home_probable_id'], '9002')

    def test_kickoff_time_is_localised_correctly(self):
        rows, _ = ingest.fetch_window(self.http, 'mlb', self.teams, lookback=0, forward=0)
        # 23:10Z in September is 7:10 PM Eastern.
        self.assertEqual(rows[0]['game_time'], '7:10 PM ET')
        self.assertTrue(rows[0]['game_start_utc'].endswith('T23:10Z'))

    def test_postponed_games_are_skipped(self):
        t = {1: fx.team(1, 'A', 'A'), 2: fx.team(2, 'B', 'B')}
        ev = fx.event(1, self.today, t[1], t[2], postponed=True)
        self.assertIsNone(ingest.parse_event(ev, 'mlb', {}, {}))

    def test_unknown_teams_get_a_neutral_prior(self):
        t = {1: fx.team(1, 'Nowhere FC', 'NOW'), 2: fx.team(2, 'Elsewhere', 'ELS')}
        ev = fx.event(1, self.today, t[1], t[2])
        row = ingest.parse_event(ev, 'mlb', {}, {})
        self.assertAlmostEqual(row['home_win_probability'],
                               1 / (1 + 2.718281828 ** -0.10), places=3)

    def test_duplicates_collapse_on_date_and_matchup(self):
        t = {1: fx.team(1, 'A', 'A'), 2: fx.team(2, 'B', 'B')}
        ev = fx.event(1, self.today, t[1], t[2])
        http = fx.FakeHttp([('scoreboard', {'events': [ev, ev]})])
        rows, _ = ingest.fetch_window(http, 'mlb', {}, lookback=0, forward=0)
        self.assertEqual(len(rows), 1)

    def test_nba_rest_advantage_tracks_last_game(self):
        t = {30: fx.team(30, 'Boston Celtics', 'BOS'), 31: fx.team(31, 'Miami Heat', 'MIA')}
        last = {}
        d0 = self.today - timedelta(days=3)
        e1 = fx.event(1, d0, t[30], t[31], 100, 110, final=True)
        ingest.parse_event(e1, 'nba', {}, last)
        e2 = fx.event(2, d0 + timedelta(days=1), t[31], t[30])
        row = ingest.parse_event(e2, 'nba', {}, last)
        # Both sides played yesterday: no rest edge.
        self.assertEqual(row['rest_advantage'], 0)
        e3 = fx.event(3, d0 + timedelta(days=1), t[31], fx.team(32, 'New York Knicks', 'NYK'))
        row = ingest.parse_event(e3, 'nba', {}, last)
        # Home side (Knicks) gets the R default of 3 days; away played 1 day ago.
        self.assertEqual(row['rest_advantage'], 3 - 1)


class TestParityFixes(unittest.TestCase):
    def test_game_date_is_the_queried_day_not_the_utc_day(self):
        t = {1: fx.team(1, 'A', 'A'), 2: fx.team(2, 'B', 'B')}
        d = date(2026, 7, 17)
        ev = fx.event(1, d, t[1], t[2], iso_time='02:10Z')       # 10pm Pacific
        ev['date'] = '2026-07-18T02:10Z'
        row = ingest.parse_event(ev, 'mlb', {}, {}, query_date='2026-07-17')
        self.assertEqual(row['game_date'], '2026-07-17')
        self.assertEqual(row['game_start_utc'], '2026-07-18T02:10Z')

    def test_refit_runs_without_era_when_no_team_has_one(self):
        rows = []
        for i in range(60):
            strong = i % 2 == 0
            rows.append({'status': 'Final', 'winner': 'H' if strong else 'A',
                         'home_team': 'H', 'away_team': 'A',
                         'home_pyth_pct': 0.6 if strong else 0.4, 'away_pyth_pct': 0.5,
                         'home_win_pct': 0.6 if strong else 0.4, 'away_win_pct': 0.5,
                         'home_run_diff_pg': 1.0 if strong else -1.0, 'away_run_diff_pg': 0.0,
                         'home_rs_pg': 5.0, 'away_rs_pg': 4.5,
                         'home_era': None, 'away_era': None,
                         'home_win_probability': 0.5, 'away_win_probability': 0.5,
                         'favored_team': ''})
        self.assertEqual(ingest.refit_prior(rows, 'mlb'), 60)


class TestRefit(unittest.TestCase):
    def test_refit_needs_thirty_games(self):
        rows = []
        self.assertEqual(ingest.refit_prior(rows, 'mlb'), 0)

    def test_refit_moves_the_prior_toward_the_better_team(self):
        rows = []
        for i in range(60):
            strong_home = i % 2 == 0
            rows.append({
                'status': 'Final', 'winner': 'H' if strong_home else 'A',
                'home_team': 'H', 'away_team': 'A',
                'home_pyth_pct': 0.6 if strong_home else 0.4, 'away_pyth_pct': 0.5,
                'home_win_pct': 0.6 if strong_home else 0.4, 'away_win_pct': 0.5,
                'home_run_diff_pg': 1.0 if strong_home else -1.0, 'away_run_diff_pg': 0.0,
                'home_rs_pg': 5.0, 'away_rs_pg': 4.5,
                'home_era': 3.5 if strong_home else 4.8, 'away_era': 4.0,
                'home_win_probability': 0.5, 'away_win_probability': 0.5,
                'favored_team': '',
            })
        updated = ingest.refit_prior(rows, 'mlb')
        self.assertEqual(updated, 60)
        strong = [r for r in rows if r['home_pyth_pct'] == 0.6]
        weak = [r for r in rows if r['home_pyth_pct'] == 0.4]
        self.assertGreater(strong[0]['home_win_probability'], 0.6)
        self.assertLess(weak[0]['home_win_probability'], 0.4)
        self.assertEqual(strong[0]['favored_team'], 'H')


class TestRunEndToEnd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (config.BASE, config.HISTORY_DIR)
        config.BASE = self.dir
        config.HISTORY_DIR = os.path.join(self.dir, 'history')

    def tearDown(self):
        config.BASE, config.HISTORY_DIR = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writes_a_csv_the_pipeline_can_read(self):
        os.environ['SP_LOOKBACK_DAYS'] = '20'
        os.environ['SP_FORWARD_DAYS'] = '3'
        try:
            rows = ingest.run('mlb', http=fx.FakeHttp(fx.mlb_routes()), log=lambda *_: None)
        finally:
            os.environ.pop('SP_LOOKBACK_DAYS', None)
            os.environ.pop('SP_FORWARD_DAYS', None)
        self.assertTrue(rows)
        path = os.path.join(self.dir, config.LEAGUES['mlb']['csv_file'])
        back = read_csv(path)
        self.assertEqual(len(back), len(rows))
        self.assertIn('home_era', back[0])
        eras = {r['home_era'] for r in back} | {r['away_era'] for r in back}
        self.assertIn('3.4', eras)

    def test_snapshots_team_stats_for_the_day(self):
        os.environ['SP_LOOKBACK_DAYS'] = '5'
        try:
            ingest.run('mlb', http=fx.FakeHttp(fx.mlb_routes()), log=lambda *_: None)
        finally:
            os.environ.pop('SP_LOOKBACK_DAYS', None)
        snap = read_csv(os.path.join(config.HISTORY_DIR, 'mlb_team_stats.csv'))
        self.assertEqual(len(snap), 4)
        self.assertEqual({r['date'] for r in snap}, {str(date.today())})
        # Running again the same day replaces rather than duplicates.
        ingest.run('mlb', http=fx.FakeHttp(fx.mlb_routes()), log=lambda *_: None)
        self.assertEqual(len(read_csv(os.path.join(config.HISTORY_DIR, 'mlb_team_stats.csv'))), 4)

    def test_an_outage_leaves_the_previous_csv_alone(self):
        path = os.path.join(self.dir, config.LEAGUES['mlb']['csv_file'])
        with open(path, 'w', encoding='utf-8') as f:
            f.write('game_date,away_team,home_team\n2026-01-01,A,B\n')
        result = ingest.run('mlb', http=fx.FakeHttp([]), log=lambda *_: None)
        self.assertIsNone(result)
        self.assertEqual(read_csv(path)[0]['away_team'], 'A')

    def test_a_window_with_no_games_leaves_the_previous_csv_alone(self):
        path = os.path.join(self.dir, config.LEAGUES['mlb']['csv_file'])
        with open(path, 'w', encoding='utf-8') as f:
            f.write('game_date,away_team,home_team\n2026-01-01,A,B\n')
        http = fx.FakeHttp([('baseball/mlb/standings', fx.mlb_standings()),
                            ('scoreboard', {'events': []})])
        self.assertIsNone(ingest.run('mlb', http=http, log=lambda *_: None))
        self.assertEqual(read_csv(path)[0]['away_team'], 'A')


if __name__ == '__main__':
    unittest.main()
