"""Adversarial input. Every one of these can arrive from a live feed."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta

from sportspred import config, features as feat, model, pipeline, props, render
from sportspred.espn import extract_stats, norm_team
from sportspred.glm import LogisticModel, PlattCalibrator, score
from sportspred.learn import LeagueMemory
from sportspred.ratings import EloEngine
from sportspred.util import num, read_csv, short_name, write_csv
from tests.helpers import synthetic_rows

TODAY = date.today()


def row(**kw):
    base = {
        'game_date': str(TODAY - timedelta(days=1)),
        'away_team': 'Team A', 'home_team': 'Team B',
        'status': 'Final', 'winner': 'Team B',
        'away_score': '2', 'home_score': '5',
        'game_id': '1', 'favored_team': 'Team B',
        'home_win_probability': '0.6', 'away_win_probability': '0.4',
    }
    base.update(kw)
    return base


class TestMalformedFeedRows(unittest.TestCase):
    def test_missing_teams_are_dropped(self):
        games = feat.normalize_games(
            [row(home_team=''), row(away_team='', game_id='2'), row(game_id='3')], 'mlb')
        self.assertEqual(len(games), 1)

    def test_unparseable_date_is_dropped(self):
        games = feat.normalize_games(
            [row(game_date='not-a-date'), row(game_date='', game_id='2')], 'mlb')
        self.assertEqual(games, [])

    def test_final_without_scores_is_treated_as_unplayed(self):
        games = feat.normalize_games([row(home_score='', away_score='')], 'mlb')
        self.assertFalse(games[0]['final'])

    def test_final_without_a_winner_is_treated_as_unplayed(self):
        self.assertFalse(feat.normalize_games([row(winner='')], 'mlb')[0]['final'])

    def test_a_team_playing_itself_does_not_crash(self):
        games = feat.normalize_games([row(away_team='Team B')], 'mlb')
        engine = EloEngine(**config.LEAGUES['mlb']['elo'])
        recs = feat.build(games, 'mlb', engine.replay(games), 4.3)
        self.assertEqual(len(recs), 1)

    def test_whitespace_in_team_names_is_trimmed(self):
        games = feat.normalize_games([row(home_team='  Team B  ')], 'mlb')
        self.assertEqual(games[0]['home'], 'Team B')

    def test_non_numeric_scores_are_ignored(self):
        self.assertFalse(feat.normalize_games([row(home_score='PPD')], 'mlb')[0]['final'])

    def test_duplicate_game_ids_do_not_double_count_in_the_archive(self):
        tmp = tempfile.mkdtemp()
        try:
            mem = LeagueMemory('mlb', history_dir=tmp, state_dir=tmp)
            self.assertEqual(mem.merge_archive([row(), row(), row()], 'mlb'), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rows_without_a_game_id_are_keyed_by_matchup(self):
        tmp = tempfile.mkdtemp()
        try:
            mem = LeagueMemory('mlb', history_dir=tmp, state_dir=tmp)
            mem.merge_archive([row(game_id=''), row(game_id='')], 'mlb')
            self.assertEqual(len(mem.archive), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestInProgressGames(unittest.TestCase):
    """A live game arrives as 'Scheduled' with a running score."""

    def setUp(self):
        self.rows = [row(status='Scheduled', winner='', away_score='3', home_score='1')]

    def test_running_score_does_not_make_it_final(self):
        self.assertFalse(feat.normalize_games(self.rows, 'mlb')[0]['final'])

    def test_running_score_never_moves_elo(self):
        games = feat.normalize_games(self.rows, 'mlb')
        engine = EloEngine(**config.LEAGUES['mlb']['elo'])
        engine.replay(games)
        self.assertEqual(engine.ratings, {})

    def test_running_score_is_not_published_as_a_final_score(self):
        games = feat.normalize_games(self.rows, 'mlb')
        self.assertIsNone(games[0]['home_score'])


class TestRatingsEdges(unittest.TestCase):
    def test_empty_log(self):
        self.assertEqual(EloEngine().replay([]), [])

    def test_unknown_team_uses_the_base_rating(self):
        self.assertEqual(EloEngine().rating('Nobody'), 1500.0)

    def test_a_single_team_dominating_does_not_run_away(self):
        engine = EloEngine(k=20, hfa=0, mov=1.0)
        for _ in range(200):
            engine.observe('A', 'B', 50, 0)
        # Elo converges; the update shrinks as the expectation approaches 1.
        self.assertLess(engine.rating('A'), 2600)
        self.assertGreater(engine.rating('A'), 1500)

    def test_probabilities_stay_in_range_at_extremes(self):
        engine = EloEngine()
        engine.ratings['A'] = 5000.0
        engine.ratings['B'] = -1000.0
        p = engine.expected('A', 'B')
        self.assertGreater(p, 0.99)
        self.assertLessEqual(p, 1.0)


class TestModelEdges(unittest.TestCase):
    def test_all_games_won_by_the_home_side(self):
        rows = []
        for i in range(160):
            rows.append(row(game_id=str(i),
                            game_date=str(TODAY - timedelta(days=160 - i)),
                            away_team=f'A{i % 8}', home_team=f'B{i % 8}'))
        trained = model.train('mlb', rows, config.LEAGUES['mlb'], tune=False)
        for rec in trained['records'][:20]:
            p = model.predict(rec, trained, config.LEAGUES['mlb'], 'mlb')['prob']
            self.assertGreater(p, 0.0)
            self.assertLess(p, 1.0)

    def test_identical_teams_produce_a_near_coin_flip(self):
        # Genuinely random 50/50 results. A strictly alternating sequence would
        # be a real pattern, and the model would be right to pick up on it.
        import random
        rng = random.Random(4)
        rows = []
        for i in range(120):
            home_wins = rng.random() < 0.5
            rows.append(row(
                game_id=str(i), game_date=str(TODAY - timedelta(days=120 - i)),
                away_team='A', home_team='B',
                winner='B' if home_wins else 'A',
                home_score='3' if home_wins else '1',
                away_score='1' if home_wins else '3'))
        trained = model.train('mlb', rows, config.LEAGUES['mlb'], tune=False)
        rec = trained['records'][-1]
        p = model.predict(rec, trained, config.LEAGUES['mlb'], 'mlb')['prob']
        self.assertGreater(p, 0.3)
        self.assertLess(p, 0.7)

    def test_a_single_game_does_not_crash_training(self):
        trained = model.train('mlb', [row()], config.LEAGUES['mlb'], tune=False)
        self.assertEqual(trained['stage'], 'warmup')

    def test_tuning_on_a_tiny_log_falls_back_to_the_prior(self):
        params, metric = model.tune_elo('mlb', [row()], config.LEAGUES['mlb'])
        self.assertIsNone(metric)
        self.assertEqual(params, config.LEAGUES['mlb']['elo'])

    def test_scoring_ignores_missing_predictions(self):
        self.assertEqual(score([None, 0.6], [1, 1])['n'], 1)


class TestPropsEdges(unittest.TestCase):
    def test_a_player_with_no_statistics_produces_no_props(self):
        out, gp = props.project_player({'stats': {}}, 'baseball', 'batter', 1.0)
        self.assertEqual(out, [])

    def test_a_player_with_zero_games_produces_no_props(self):
        out, _ = props.project_player(
            {'stats': {'gp': 0, 'hits': 0}}, 'baseball', 'batter', 1.0)
        self.assertEqual(out, [])

    def test_negative_or_absurd_values_are_not_published(self):
        out, _ = props.project_player(
            {'stats': {'gp': 100, 'hits': -5}}, 'baseball', 'batter', 1.0)
        self.assertTrue(all(p['proj'] > 0 for p in out))

    def test_environment_with_no_games_still_returns_a_league_average(self):
        env = props.team_environment([], config.LEAGUES['mlb'])
        self.assertGreater(env['league_avg'], 0)
        exp = props.expected_scores(env, 'A', 'B', 0.5, config.LEAGUES['mlb'])
        self.assertGreater(exp['home'], 0)

    def test_expected_scores_never_go_negative(self):
        env = props.team_environment(
            [dict(final=True, home='A', away='B', home_score=0, away_score=0)] * 5,
            config.LEAGUES['mlb'])
        for p in (0.0, 0.5, 1.0):
            exp = props.expected_scores(env, 'A', 'B', p, config.LEAGUES['mlb'])
            self.assertGreater(exp['home'], 0)
            self.assertGreater(exp['away'], 0)

    def test_probabilities_are_always_between_zero_and_one(self):
        for sport, groups in config.PROPS.items():
            for group, specs in groups.items():
                for spec in specs:
                    for proj in (0.001, 0.5, 5.0, 250.0, 5000.0):
                        _, p = props.over_probability(spec, proj, proj)
                        self.assertGreaterEqual(p, 0.0, f'{sport}/{spec["key"]}')
                        self.assertLessEqual(p, 1.0, f'{sport}/{spec["key"]}')

    def test_unparseable_stat_values_are_skipped(self):
        stats = extract_stats('baseball', ['hits', 'homeRuns'], ['abc', None])
        self.assertNotIn('hits', stats)

    def test_team_lookup_is_case_and_punctuation_insensitive(self):
        self.assertEqual(norm_team('St. Louis Cardinals'), norm_team('st louis cardinals'))


class TestLedgerRobustness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_ledger_missing_columns_is_survivable(self):
        path = os.path.join(self.dir, 'mlb_ledger.csv')
        write_csv(path, [{'game_id': '1', 'game_date': '2026-05-01'}])
        mem = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)
        self.assertEqual(mem.grade(), 0)
        self.assertEqual(mem.component_scores(), ({}, 0))
        self.assertIsNone(mem.tune_trust_from_ledger())

    def test_a_ledger_row_with_a_junk_probability_is_ignored(self):
        mem = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)
        mem.ledger['1'] = {
            'game_id': '1', 'game_date': '2026-05-01', 'away_team': 'A',
            'home_team': 'B', 'pregame': '1', 'p_final': 'oops',
            'favored_team': 'B', 'winner': 'B', 'graded': '1', 'correct': '1'}
        scores, n = mem.component_scores()
        self.assertEqual(n, 1)
        self.assertEqual(scores, {})

    def test_corrupt_state_json_does_not_stop_a_run(self):
        with open(os.path.join(self.dir, 'mlb.json'), 'w', encoding='utf-8') as f:
            f.write('{ not json')
        mem = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)
        self.assertEqual(mem.previous_best(), {})

    def test_an_entry_refreshes_before_kickoff_and_freezes_after(self):
        mem = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)
        game = {'game_id': '5', 'date': '2026-05-01', 'home': 'B', 'away': 'A', 'final': False}
        parts = {'prob': 0.6, 'elo_prob': 0.6, 'glm_prob': None, 'prior_prob': None}
        self.assertTrue(mem.record(game, parts, 'B', pregame=True))
        # A later pre-game forecast replaces it (lineups, injuries).
        self.assertTrue(mem.record(game, {**parts, 'prob': 0.7}, 'B', pregame=True))
        self.assertEqual(float(list(mem.ledger.values())[0]['p_final']), 0.7)
        # Once the game has started nothing can touch it.
        self.assertFalse(mem.record(game, {**parts, 'prob': 0.95}, 'A', pregame=False))
        self.assertEqual(float(list(mem.ledger.values())[0]['p_final']), 0.7)

    def test_a_ledger_round_trips_through_disk(self):
        mem = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)
        game = {'game_id': '5', 'date': '2026-05-01', 'home': 'B', 'away': 'A', 'final': False}
        mem.record(game, {'prob': 0.61, 'elo_prob': 0.6,
                          'glm_prob': 0.62, 'prior_prob': 0.59}, 'B')
        mem.save_ledger()
        again = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)
        self.assertEqual(float(list(again.ledger.values())[0]['p_final']), 0.61)


class TestOutputSafety(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (config.HISTORY_DIR, config.STATE_DIR, config.DATA_DIR, config.BASE)
        config.HISTORY_DIR = os.path.join(self.dir, 'history')
        config.STATE_DIR = os.path.join(self.dir, 'model_state')
        config.DATA_DIR = os.path.join(self.dir, 'data')
        config.BASE = self.dir

    def tearDown(self):
        (config.HISTORY_DIR, config.STATE_DIR,
         config.DATA_DIR, config.BASE) = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_payload_is_valid_json_and_parses_back(self):
        csv_path = os.path.join(self.dir, config.LEAGUES['mlb']['csv_file'])
        write_csv(csv_path, synthetic_rows(n_days=40, seed=44))
        payload, _, _ = pipeline.run('mlb', fetch_props=False, tune=False)
        path, _ = render.write_payload(payload)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        body = text.split('=', 2)[2].rsplit(';', 2)[0]
        self.assertIsInstance(json.loads(body), dict)

    def test_line_separators_in_a_name_cannot_break_parsing(self):
        csv_path = os.path.join(self.dir, config.LEAGUES['mlb']['csv_file'])
        rows = synthetic_rows(n_days=20, seed=46)
        for r in rows:
            r['home_team'] = 'Line\u2028Break United'
            if r['winner']:
                r['winner'] = r['home_team']
        write_csv(csv_path, rows)
        payload, _, _ = pipeline.run('mlb', fetch_props=False, tune=False)
        path, _ = render.write_payload(payload)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn('\u2028', text)
        self.assertIn('\\u2028', text)

    def test_a_team_name_containing_markup_cannot_break_the_payload(self):
        csv_path = os.path.join(self.dir, config.LEAGUES['mlb']['csv_file'])
        rows = synthetic_rows(n_days=20, seed=45)
        for r in rows:
            r['home_team'] = '</script><img src=x onerror=alert(1)>'
            r['winner'] = r['home_team'] if r['winner'] else ''
        write_csv(csv_path, rows)
        payload, _, _ = pipeline.run('mlb', fetch_props=False, tune=False)
        path, _ = render.write_payload(payload)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        # Angle brackets are escaped, so the name cannot terminate a script
        # block even if this payload is ever inlined rather than linked.
        self.assertNotIn('</script>', text)
        self.assertNotIn('<img', text)

    def test_the_site_builds_when_every_league_is_empty(self):
        written, version = render.write_site({})
        self.assertIn('index.html', written)
        self.assertTrue(version)


if __name__ == '__main__':
    unittest.main()
