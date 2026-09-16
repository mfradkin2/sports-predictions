"""Two rules the site must never break.

1. A published prediction is frozen. Re-running the model after a game has
   finished must not change who it says was favoured.
2. Exhibition games never reach the results, the accuracy figures, or the
   model's training data.
"""
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta

from sportspred import config, features as feat, model, pipeline
from sportspred.ratings import EloEngine
from sportspred.util import read_csv, write_csv
from tests.helpers import synthetic_rows


class PipelineHarness(unittest.TestCase):
    """Runs the real pipeline against a throwaway repository layout."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (config.HISTORY_DIR, config.STATE_DIR, config.DATA_DIR, config.BASE)
        config.HISTORY_DIR = os.path.join(self.dir, 'history')
        config.STATE_DIR = os.path.join(self.dir, 'model_state')
        config.DATA_DIR = os.path.join(self.dir, 'data')
        config.BASE = self.dir
        self.csv = os.path.join(self.dir, config.LEAGUES['mlb']['csv_file'])

    def tearDown(self):
        (config.HISTORY_DIR, config.STATE_DIR,
         config.DATA_DIR, config.BASE) = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_pipeline(self, league='mlb'):
        return pipeline.run(league, fetch_props=False, tune=False)

    def play_out(self, home_wins=True):
        """Finish every scheduled game, as the next day's feed would."""
        rows = read_csv(self.csv)
        for r in rows:
            if r['status'] == 'Final':
                continue
            r['status'] = 'Final'
            r['home_score'], r['away_score'] = ('6', '2') if home_wins else ('2', '6')
            r['winner'] = r['home_team'] if home_wins else r['away_team']
            # The standings model is recomputed from post-game standings; this
            # is exactly what used to drag the displayed probability around.
            r['home_win_probability'] = '0.95' if home_wins else '0.05'
            r['away_win_probability'] = '0.05' if home_wins else '0.95'
            r['favored_team'] = r['home_team'] if home_wins else r['away_team']
        write_csv(self.csv, rows)


class TestPredictionsAreFrozen(PipelineHarness):
    def setUp(self):
        super().setUp()
        write_csv(self.csv, synthetic_rows(n_days=130, seed=21))

    def test_probability_does_not_move_after_the_game(self):
        payload, _, _ = self.run_pipeline()
        before = {g['id']: (g['home_prob'], g['favored'])
                  for g in payload['games'] if not g['final']}
        self.assertTrue(before)

        self.play_out(home_wins=True)
        payload2, _, _ = self.run_pipeline()
        after = {g['id']: (g['home_prob'], g['favored'])
                 for g in payload2['games'] + payload2['history']}

        checked = 0
        for gid, (prob, fav) in before.items():
            if gid not in after:
                continue
            checked += 1
            self.assertEqual(after[gid], (prob, fav),
                             f'game {gid} changed after it was played')
        self.assertGreater(checked, 10)

    def test_the_favourite_never_flips_to_the_winner(self):
        """The reported bug: a team favoured before kickoff became the other
        team once the result reached the standings."""
        payload, _, _ = self.run_pipeline()
        picks = {g['id']: g['favored'] for g in payload['games'] if not g['final']}
        # Make the away side win every game, the opposite of the usual lean.
        self.play_out(home_wins=False)
        payload2, _, _ = self.run_pipeline()
        for g in payload2['games'] + payload2['history']:
            if g['id'] in picks:
                self.assertEqual(g['favored'], picks[g['id']])

    def test_repeated_runs_are_stable(self):
        first, _, _ = self.run_pipeline()
        second, _, _ = self.run_pipeline()
        a = {g['id']: g['home_prob'] for g in first['games']}
        b = {g['id']: g['home_prob'] for g in second['games']}
        self.assertEqual(a, b)

    def test_started_games_are_locked_and_future_games_are_not(self):
        self.run_pipeline()
        payload, _, _ = self.run_pipeline()
        finals = [g for g in payload['games'] if g['final']]
        future = [g for g in payload['games'] if g['date'] > payload['today']]
        self.assertTrue(finals and future)
        self.assertTrue(all(g['locked'] for g in finals))
        self.assertTrue(all(not g['locked'] for g in future))

    def test_a_future_game_may_still_refresh(self):
        """Lineups and injuries land right up to first pitch; only kickoff freezes."""
        payload, _, mem = self.run_pipeline()
        future = next(g for g in payload['games'] if g['date'] > payload['today'])
        entry = mem.entry_for({'game_id': future['id'], 'date': future['date'],
                               'away': future['away'], 'home': future['home']})
        self.assertEqual(entry['pregame'], '1')
        # Swing the standings prior for that game and re-run: pre-kickoff it moves.
        rows = read_csv(self.csv)
        for r in rows:
            if r['game_id'] == future['id']:
                r['home_win_probability'], r['away_win_probability'] = '0.97', '0.03'
        write_csv(self.csv, rows)
        payload2, _, _ = self.run_pipeline()
        again = next(g for g in payload2['games'] if g['id'] == future['id'])
        self.assertNotEqual(again['home_prob'], future['home_prob'])

    def test_verified_record_counts_only_pre_game_forecasts(self):
        self.run_pipeline()
        self.play_out()
        payload, _, _ = self.run_pipeline()
        acc = payload['accuracy']
        self.assertGreater(acc['verified']['total'], 0)
        # Games first seen after the fact stay out of the headline entirely.
        self.assertGreater(acc['backfilled'], 0)

    def test_backfilled_games_do_not_count_as_forecasts(self):
        """Games only ever seen after they finished are not a track record.

        Their picks are made with standings that already contain the result, so
        counting them would report something close to 100%.
        """
        self.play_out()
        payload, _, mem = self.run_pipeline()
        self.assertEqual(payload['accuracy']['verified']['total'], 0)
        self.assertGreater(payload['accuracy']['backfilled'], 0)
        self.assertEqual(mem.graded_rows(pregame_only=True), [])

    def test_backtest_is_reported_and_is_not_perfect(self):
        payload, _, _ = self.run_pipeline()
        bt = payload['accuracy']['backtest']
        self.assertIsNotNone(bt)
        self.assertGreater(bt['total'], 100)
        # Walk-forward scoring cannot see the result, so it must not be perfect.
        self.assertLess(bt['pct'], 0.95)


class TestPreseasonIsExcluded(PipelineHarness):
    def _rows_with_preseason(self):
        """A short exhibition block followed by games that count."""
        rows = synthetic_rows(n_days=130, seed=31)
        cutoff = str(date.today() - timedelta(days=100))
        for r in rows:
            pre = r['game_date'] <= cutoff
            r['season_type'] = '1' if pre else '2'
            r['season_slug'] = 'preseason' if pre else 'regular-season'
            if pre:
                # Exhibition blowouts that would badly distort the ratings.
                r['home_score'], r['away_score'] = '20', '0'
                r['winner'] = r['home_team']
        return rows, cutoff

    def setUp(self):
        super().setUp()
        self.rows, self.cutoff = self._rows_with_preseason()
        write_csv(self.csv, self.rows)

    def test_preseason_games_are_flagged(self):
        games = feat.normalize_games(self.rows, 'mlb')
        pre = [g for g in games if g['preseason']]
        self.assertTrue(pre)
        self.assertTrue(all(str(g['date']) <= self.cutoff for g in pre))

    def test_preseason_never_reaches_the_results_list(self):
        payload, _, _ = self.run_pipeline()
        for g in payload['games'] + payload['history']:
            if g['final']:
                self.assertFalse(g['preseason'])

    def test_preseason_is_absent_from_accuracy(self):
        payload, trained, _ = self.run_pipeline()
        graded = sum(1 for r in trained['records']
                     if r['game']['final'] and not r['game']['preseason'])
        acc = payload['accuracy']
        self.assertEqual(acc['verified']['total'] + acc['backfilled'], graded)
        self.assertGreater(payload['preseason_excluded'], 0)

    def test_preseason_never_moves_elo(self):
        games = feat.normalize_games(self.rows, 'mlb')
        engine = EloEngine(**config.LEAGUES['mlb']['elo'])
        engine.replay([g for g in games if g['preseason']])
        self.assertEqual(engine.ratings, {})

    def test_preseason_is_not_a_training_target(self):
        games = feat.normalize_games(self.rows, 'mlb')
        engine = EloEngine(**config.LEAGUES['mlb']['elo'])
        recs = feat.build(games, 'mlb', engine.replay(games), 4.3)
        outcomes = model.outcomes_of(recs)
        for rec, o in zip(recs, outcomes):
            if rec['game']['preseason']:
                self.assertIsNone(o)

    def test_preseason_does_not_advance_form_or_records(self):
        games = feat.normalize_games(self.rows, 'mlb')
        engine = EloEngine(**config.LEAGUES['mlb']['elo'])
        recs = feat.build(games, 'mlb', engine.replay(games), 4.3)
        first_real = next(r for r in recs if not r['game']['preseason'])
        # Nothing should have accumulated from the exhibition block.
        self.assertEqual(first_real['context']['home_last10'], '')
        self.assertEqual(first_real['features']['pyth_diff'], 0.0)

    def test_preseason_stays_out_of_the_archive(self):
        _, _, mem = self.run_pipeline()
        for entry in mem.archive.values():
            self.assertGreater(entry['game_date'], self.cutoff)

    def test_upcoming_preseason_is_still_shown(self):
        """They are excluded from the record, not hidden from the schedule."""
        rows = synthetic_rows(n_days=20, seed=32)
        for r in rows:
            r['season_type'] = '1'
            r['season_slug'] = 'preseason'
        write_csv(self.csv, rows)
        payload, _, _ = self.run_pipeline()
        upcoming = [g for g in payload['games'] if not g['final']]
        self.assertTrue(upcoming)
        self.assertTrue(all(g['preseason'] for g in upcoming))
        self.assertEqual(payload['accuracy']['verified']['total'], 0)
        self.assertEqual(payload['accuracy']['backfilled'], 0)

    def test_a_real_nhl_september_slate_reads_as_preseason(self):
        self.assertTrue(feat.preseason_by_date(date(2026, 9, 19), 'nhl'))
        self.assertFalse(feat.preseason_by_date(date(2026, 10, 14), 'nhl'))

    def test_nfl_week_one_is_not_preseason(self):
        # 2026: Labor Day is 7 September, so Week 1 opens on the 10th.
        self.assertTrue(feat.preseason_by_date(date(2026, 9, 9), 'nfl'))
        self.assertFalse(feat.preseason_by_date(date(2026, 9, 10), 'nfl'))


if __name__ == '__main__':
    unittest.main()


class TestPropsFreeze(unittest.TestCase):
    """Prop boards refresh until first pitch, then never change."""

    def setUp(self):
        from sportspred.learn import FrozenBoards
        self.dir = tempfile.mkdtemp()
        self.boards = FrozenBoards('mlb', history_dir=self.dir)
        self.game = {'game_id': 'g9', 'date': date.today()}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_pre_kickoff_board_is_replaced_by_a_newer_one(self):
        self.boards.store(self.game, {'home': [{'id': 'p', 'props': [{'key': 'hits', 'line': 0.5}]}]}, frozen=False)
        self.boards.store(self.game, {'home': [{'id': 'p', 'props': [{'key': 'hits', 'line': 1.5}]}]}, frozen=False)
        self.assertEqual(self.boards.get('g9')['home'][0]['props'][0]['line'], 1.5)

    def test_frozen_board_cannot_be_replaced(self):
        self.boards.store(self.game, {'home': [{'id': 'p', 'props': [{'key': 'hits', 'line': 0.5, 'proj': 1.1}]}]}, frozen=False)
        self.boards.freeze('g9')
        self.boards.store(self.game, {'home': [{'id': 'p', 'props': [{'key': 'hits', 'line': 2.5, 'proj': 3.0}]}]}, frozen=False)
        prop = self.boards.get('g9')['home'][0]['props'][0]
        self.assertEqual((prop['line'], prop['proj']), (0.5, 1.1))

    def test_outcomes_are_written_onto_the_frozen_board(self):
        self.boards.store(self.game, {'home': [{'id': 'p', 'props': [{'key': 'hits', 'line': 0.5}]}]}, frozen=True)
        rows = [{'game_id': 'g9', 'athlete_id': 'p', 'key': 'hits', 'graded': '1',
                 'actual': '2', 'played': '1', 'hit': '1', 'push': '0'}]
        self.boards.annotate('g9', rows)
        prop = self.boards.get('g9')['home'][0]['props'][0]
        self.assertEqual(prop['actual'], 2)
        self.assertTrue(prop['hit'])

    def test_old_boards_are_pruned(self):
        self.boards.store({'game_id': 'old', 'date': date.today() - timedelta(days=30)}, {}, frozen=True)
        self.boards.store(self.game, {}, frozen=False)
        self.boards.prune(date.today())
        self.assertNotIn('old', self.boards.boards)
        self.assertIn('g9', self.boards.boards)

    def test_round_trips_through_disk(self):
        from sportspred.learn import FrozenBoards
        self.boards.store(self.game, {'home': []}, frozen=True)
        self.boards.save()
        again = FrozenBoards('mlb', history_dir=self.dir)
        self.assertTrue(again.is_frozen('g9'))
