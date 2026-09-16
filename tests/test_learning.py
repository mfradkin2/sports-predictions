"""The persistence layer: archive, prediction ledger, champion/challenger."""
import os
import shutil
import tempfile
import unittest

from sportspred import config, model, pipeline, render
from sportspred.learn import LeagueMemory
from sportspred.util import Http, read_csv
from tests.helpers import player_pool, synthetic_rows


class TestArchive(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mem = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_only_completed_games_are_archived(self):
        rows = synthetic_rows(n_days=20, seed=4)
        added = self.mem.merge_archive(rows)
        finals = [r for r in rows if r['status'] == 'Final']
        self.assertEqual(added, len(finals))

    def test_merging_the_same_window_twice_adds_nothing(self):
        rows = synthetic_rows(n_days=20, seed=4)
        self.mem.merge_archive(rows)
        self.assertEqual(self.mem.merge_archive(rows), 0)

    def test_archive_grows_as_the_window_slides(self):
        """The point of the archive: memory outlives the fetch window.

        Two consecutive fetch windows overlap. Everything either one saw has
        to survive, which is what lets Elo keep a season-long memory when the
        ingest only ever looks back a few weeks.
        """
        from datetime import date, timedelta
        early = synthetic_rows(n_days=20, seed=4,
                               start=date.today() - timedelta(days=60))
        later = synthetic_rows(n_days=20, seed=9,
                               start=date.today() - timedelta(days=30))
        self.mem.merge_archive(early)
        first = len(self.mem.archive)
        self.assertGreater(first, 0)
        self.mem.merge_archive(later)
        self.assertGreater(len(self.mem.archive), first)

    def test_placeholder_scores_are_rejected(self):
        self.assertEqual(self.mem.merge_archive([{
            'game_date': '2026-05-01', 'away_team': 'A', 'home_team': 'B',
            'status': 'Final', 'winner': 'B', 'away_score': '0',
            'home_score': '0', 'game_id': '9'}]), 0)

    def test_archive_survives_a_save_and_reload(self):
        self.mem.merge_archive(synthetic_rows(n_days=15, seed=4))
        self.mem.save_archive()
        reopened = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)
        self.assertEqual(len(reopened.archive), len(self.mem.archive))


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mem = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)
        self.game = {'game_id': '77', 'date': '2026-05-01', 'home': 'B',
                     'away': 'A', 'final': False}
        self.parts = {'prob': 0.62, 'elo_prob': 0.6, 'glm_prob': 0.64,
                      'prior_prob': 0.58, 'trust': 0.5}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_records_a_prediction_before_the_game(self):
        self.mem.record(self.game, self.parts, 'B')
        self.assertEqual(len(self.mem.ledger), 1)

    def test_finished_games_are_logged_but_flagged(self):
        """A game first seen after it ended still needs a stable stored pick,
        but must never count toward the model's record."""
        self.mem.record({**self.game, 'final': True}, self.parts, 'B', pregame=False)
        self.assertEqual(len(self.mem.ledger), 1)
        self.assertEqual(list(self.mem.ledger.values())[0]['pregame'], '0')
        self.mem.merge_archive([{'game_id': '77', 'game_date': '2026-05-01',
                                 'away_team': 'A', 'home_team': 'B',
                                 'status': 'Final', 'winner': 'B',
                                 'away_score': '2', 'home_score': '5'}])
        self.mem.grade()
        self.assertEqual(self.mem.graded_rows(pregame_only=True), [])
        self.assertEqual(len(self.mem.graded_rows()), 1)

    def test_the_last_pre_kickoff_forecast_is_the_one_we_are_held_to(self):
        """Refreshing before the game is fine; after it, nothing may move."""
        self.mem.record(self.game, self.parts, 'B', pregame=True)
        self.mem.record(self.game, {**self.parts, 'prob': 0.66}, 'B', pregame=True)
        self.assertEqual(float(list(self.mem.ledger.values())[0]['p_final']), 0.66)
        self.mem.record(self.game, {**self.parts, 'prob': 0.95}, 'B', pregame=False)
        self.assertEqual(float(list(self.mem.ledger.values())[0]['p_final']), 0.66)

    def test_grading_marks_a_correct_pick(self):
        self.mem.record(self.game, self.parts, 'B')
        self.mem.merge_archive([{'game_id': '77', 'game_date': '2026-05-01',
                                 'away_team': 'A', 'home_team': 'B',
                                 'status': 'Final', 'winner': 'B',
                                 'away_score': '2', 'home_score': '5'}])
        self.assertEqual(self.mem.grade(), 1)
        entry = list(self.mem.ledger.values())[0]
        self.assertEqual(entry['correct'], '1')

    def test_ungraded_entries_are_left_alone(self):
        self.mem.record(self.game, self.parts, 'B')
        self.assertEqual(self.mem.grade(), 0)
        self.assertEqual(self.mem.graded_rows(), [])

    def test_component_scores_are_reported_per_source(self):
        self._seed(60)
        scores, n = self.mem.component_scores()
        self.assertEqual(n, 60)
        for key in ('final', 'elo', 'glm', 'prior'):
            self.assertIn(key, scores)
            self.assertEqual(scores[key]['n'], 60)

    def test_trust_needs_enough_graded_forecasts(self):
        self._seed(40)
        self.assertIsNone(self.mem.tune_trust_from_ledger())

    def test_trust_favours_whichever_source_was_right(self):
        """The ledger is the only leak-free scoreboard, so it sets the weight."""
        self._seed(200, elo_good=True)
        tuned = self.mem.tune_trust_from_ledger()
        self.assertIsNotNone(tuned)
        self.assertGreater(tuned['trust'], 0.6)

        other = LeagueMemory('nhl', history_dir=self.dir, state_dir=self.dir)
        self._seed(200, elo_good=False, mem=other)
        self.assertLess(other.tune_trust_from_ledger()['trust'], 0.4)

    def test_learning_curve_is_cumulative(self):
        self._seed(30)
        curve = self.mem.learning_curve()
        self.assertTrue(curve)
        self.assertTrue(all(0 <= p['cum_acc'] <= 1 for p in curve))

    def _seed(self, n, elo_good=True, mem=None):
        """Write n graded ledger rows where one component is the accurate one."""
        mem = mem or self.mem
        import random
        rng = random.Random(6)
        for i in range(n):
            home_won = rng.random() < 0.5
            good, bad = (0.80, 0.20) if home_won else (0.20, 0.80)
            gid = str(1000 + i)
            mem.ledger[gid] = {
                'game_id': gid, 'game_date': f'2026-05-{(i % 28) + 1:02d}',
                'away_team': 'A', 'home_team': 'B', 'predicted_at': '',
                'model_version': 3,
                'pregame': '1',
                'p_final': good if elo_good else bad,
                'p_elo': good if elo_good else bad,
                'p_glm': good if elo_good else bad,
                'p_prior': bad if elo_good else good,
                'favored_team': 'B' if (good if elo_good else bad) > 0.5 else 'A',
                'away_score': '1' if home_won else '4',
                'home_score': '4' if home_won else '1',
                'winner': 'B' if home_won else 'A',
                'graded': '1',
                'correct': '1',
            }


class TestChampionChallenger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mem = LeagueMemory('mlb', history_dir=self.dir, state_dir=self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_first_fit_is_always_adopted(self):
        ok, _ = self.mem.adopt({'l2': 1}, {'logloss': 0.68, 'n': 100})
        self.assertTrue(ok)

    def test_a_worse_challenger_is_rejected(self):
        self.mem.adopt({'l2': 1}, {'logloss': 0.60, 'n': 100})
        ok, reason = self.mem.adopt({'l2': 9}, {'logloss': 0.66, 'n': 100})
        self.assertFalse(ok)
        self.assertIn('incumbent', reason)
        self.assertEqual(self.mem.previous_best()['params']['l2'], 1)

    def test_a_better_challenger_is_adopted(self):
        self.mem.adopt({'l2': 1}, {'logloss': 0.68, 'n': 100})
        ok, _ = self.mem.adopt({'l2': 9}, {'logloss': 0.61, 'n': 100})
        self.assertTrue(ok)
        self.assertEqual(self.mem.previous_best()['params']['l2'], 9)

    def test_a_much_larger_sample_wins_even_if_slightly_worse(self):
        self.mem.adopt({'l2': 1}, {'logloss': 0.60, 'n': 100})
        ok, reason = self.mem.adopt({'l2': 9}, {'logloss': 0.605, 'n': 400})
        self.assertTrue(ok)
        self.assertIn('evidence', reason)

    def test_the_run_log_is_bounded(self):
        for i in range(80):
            self.mem.adopt({'l2': i}, {'logloss': 0.7, 'n': 10})
        self.assertLessEqual(len(self.mem.state['runs']), 60)


class TestPipeline(unittest.TestCase):
    """End to end against a temporary repository layout."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (config.HISTORY_DIR, config.STATE_DIR, config.DATA_DIR, config.BASE)
        config.HISTORY_DIR = os.path.join(self.dir, 'history')
        config.STATE_DIR = os.path.join(self.dir, 'model_state')
        config.DATA_DIR = os.path.join(self.dir, 'data')
        config.BASE = self.dir
        self.csv = os.path.join(self.dir, config.LEAGUES['mlb']['csv_file'])
        from sportspred.util import write_csv
        write_csv(self.csv, synthetic_rows(n_days=120, seed=13))

    def tearDown(self):
        (config.HISTORY_DIR, config.STATE_DIR,
         config.DATA_DIR, config.BASE) = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, **kw):
        return pipeline.run('mlb', fetch_props=False, tune=False, **kw)

    def test_produces_a_complete_payload(self):
        payload, trained, mem = self._run()
        for key in ('league', 'games', 'model', 'accuracy', 'stats', 'history'):
            self.assertIn(key, payload)
        self.assertTrue(payload['games'])

    def test_every_game_carries_what_the_page_renders(self):
        payload, _, _ = self._run()
        for g in payload['games']:
            for key in ('id', 'date', 'away', 'home', 'home_prob', 'away_prob',
                        'favored', 'pick_prob', 'conf', 'components'):
                self.assertIn(key, g)
            self.assertAlmostEqual(g['home_prob'] + g['away_prob'], 1.0, places=2)

    def test_scheduled_games_have_no_score(self):
        payload, _, _ = self._run()
        for g in payload['games']:
            if not g['final']:
                self.assertIsNone(g['home_score'])
                self.assertIsNone(g['away_score'])

    def test_state_and_ledger_are_written(self):
        self._run()
        self.assertTrue(os.path.exists(os.path.join(config.STATE_DIR, 'mlb.json')))
        self.assertTrue(os.path.exists(os.path.join(config.HISTORY_DIR, 'mlb_games.csv')))
        self.assertTrue(os.path.exists(os.path.join(config.HISTORY_DIR, 'mlb_ledger.csv')))

    def test_upcoming_games_are_logged_to_the_ledger(self):
        _, trained, mem = self._run()
        upcoming = [r for r in trained['records'] if not r['game']['final']]
        self.assertTrue(upcoming)
        self.assertTrue(len(mem.ledger) > 0)

    def test_a_second_run_grades_what_has_since_finished(self):
        self._run()
        rows = read_csv(self.csv)
        # Play out the games that were scheduled last time.
        for r in rows:
            if r['status'] != 'Final':
                r['status'] = 'Final'
                r['home_score'], r['away_score'] = '5', '3'
                r['winner'] = r['home_team']
        from sportspred.util import write_csv
        write_csv(self.csv, rows)
        _, _, mem = self._run()
        self.assertTrue(mem.graded_rows())

    def test_rendering_writes_the_site(self):
        payload, _, _ = self._run()
        path, size = render.write_payload(payload)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(size, 100)
        written, version = render.write_site({'mlb': payload})
        self.assertIn('index.html', written)
        with open(os.path.join(self.dir, 'index.html'), encoding='utf-8') as f:
            html = f.read()
        self.assertIn('assets/app.js', html)
        self.assertIn(version, html)
        # The shell must stay small; the data is a separate, cacheable file.
        self.assertLess(len(html), 8000)

    def test_props_survive_an_unreachable_feed(self):
        class Dead(Http):
            def get_json(self, url, cache=True):
                return None
        payload, _, _ = pipeline.run('mlb', fetch_props=True, http=Dead(), tune=False)
        self.assertEqual(payload['props_status'], 'unavailable')
        self.assertTrue(payload['games'])


if __name__ == '__main__':
    unittest.main()
