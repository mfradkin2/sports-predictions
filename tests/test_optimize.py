"""The optimiser: wider search, same evidence bar."""
from __future__ import annotations

import random
import tempfile
import unittest

from sportspred import config, features as feat, model, optimize
from sportspred.learn import PropsLedger
from sportspred.props import _price
from sportspred.config import props_for
from tests.helpers import synthetic_rows


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = synthetic_rows(n_days=120, seed=11)
        cls.cfg = config.LEAGUES['mlb']

    def test_incumbent_defaults_fill_in(self):
        s = optimize.incumbent_settings(None, self.cfg)
        self.assertEqual(s['form'], feat.DEFAULT_FORM)
        self.assertEqual(s['features'], feat.FEATURE_NAMES)
        self.assertEqual(s['elo_params'], self.cfg['elo'])
        s = optimize.incumbent_settings({'form': {'n': 12, 'decay': 0.85}, 'features': ['elo_diff', 'pyth_diff']},
                                        self.cfg)
        self.assertEqual(s['form'], {'n': 12, 'decay': 0.85})
        self.assertEqual(s['features'], ['elo_diff', 'pyth_diff'])

    def test_search_never_returns_something_worse_than_where_it_started(self):
        settings, trained, report = optimize.search('mlb', self.rows, self.cfg, None,
                                                    seed='t', tune_elo=False, max_evals=12)
        self.assertEqual(report['stage'], 'trained')
        self.assertGreaterEqual(report['evals'], 2)
        self.assertLessEqual(report['end'], round(report['start'], 4) + 1e-9)
        for m in report['moves']:
            self.assertLess(m['to'], m['from'])
        # The model handed back is fitted with the settings it names.
        self.assertEqual(trained['form'], settings['form'])
        self.assertEqual(trained['features'], settings['features'])
        self.assertEqual(trained['model'].feature_names, settings['features'])

    def test_every_neighbour_is_a_single_step(self):
        s = optimize.incumbent_settings(None, self.cfg)
        for label, cand in optimize._neighbours(s):
            changed = sum(1 for k in ('form', 'features') if cand[k] != s[k])
            self.assertEqual(changed, 1, label)
            self.assertGreaterEqual(len(cand['features']), 3)

    def test_probe_is_reproducible_and_keeps_the_core_signals(self):
        s = optimize.incumbent_settings(None, self.cfg)
        a = optimize.random_probe(s, 'mlb-2026-09-17-15')
        b = optimize.random_probe(s, 'mlb-2026-09-17-15')
        c = optimize.random_probe(s, 'mlb-2026-09-17-16')
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)       # a different hour looks somewhere else
        for probe in (a, c):
            self.assertIn('elo_diff', probe['features'])
            self.assertIn('pyth_diff', probe['features'])
            self.assertIn(probe['form']['n'], optimize.FORM_WINDOWS)

    def test_short_logs_come_back_untouched(self):
        rows = synthetic_rows(n_days=10, seed=2)
        settings, trained, report = optimize.search('mlb', rows, self.cfg, None, tune_elo=False)
        self.assertNotEqual(report['stage'], 'trained')
        self.assertEqual(settings, optimize.incumbent_settings(None, self.cfg))
        self.assertEqual(report['moves'], [])

    def test_features_can_be_chosen_per_league(self):
        names = ['elo_diff', 'pyth_diff', 'wpct_diff']
        trained = model.train('mlb', self.rows, self.cfg, tune=False,
                              form={'n': 12, 'decay': 0.85}, features=names)
        rec = trained['records'][-1]
        self.assertEqual(rec['feature_names'], names)
        self.assertEqual(len(rec['vector']), 3)
        self.assertEqual(trained['model'].feature_names, names)
        self.assertEqual(optimize.describe(trained)['signals'],
                         ['Team strength edge', 'Season scoring margin', 'Season win rate'])


def _graded_props(ledger, n, seed=5, sharp=1.0):
    """``n`` graded props whose outcomes follow logit(p) * sharp: with
    ``sharp`` above one the recorded probabilities were too timid, with it
    below one they were overconfident."""
    from sportspred.util import logistic, logit
    rng = random.Random(seed)
    game = {'game_id': 'g', 'date': '2026-09-15'}
    for i in range(n):
        p = rng.uniform(0.2, 0.8)
        truth = logistic(sharp * logit(p))
        went_over = rng.random() < truth
        prop = {'key': 'hits', 'label': 'Hits', 'stat': 'hits_pg', 'dist': 'binomial', 'line': 0.5,
                'proj': 1.0, 'season': 1.0, 'over': round(p, 4), 'raw_over': round(p, 4),
                'model_over': round(min(max(p + rng.uniform(-0.15, 0.15), 0.05), 0.95), 4),
                'book_p': round(truth, 4), 'pick': 'over' if p >= 0.5 else 'under', 'conf': 'low'}
        ledger.record({**game, 'game_id': f'g{i}', 'date': f'2026-0{1 + i // 300}-{1 + (i // 10) % 28:02d}'},
                      'home', {'id': f'p{i}', 'gp': rng.choice((1, 2, 5, 10, 30))}, prop)
        row = next(r for r in ledger.rows.values() if r['game_id'] == f'g{i}')
        hit = '1' if went_over == (prop['pick'] == 'over') else '0'
        row.update(graded='1', played='1', actual='1' if went_over else '0', push='0', hit=hit)


class TestPropsCorrections(unittest.TestCase):
    def test_record_keeps_the_optimisers_raw_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PropsLedger('mlb', history_dir=tmp)
            prop = {'key': 'hits', 'label': 'Hits', 'stat': 'hits_pg', 'dist': 'binomial', 'line': 0.5,
                    'proj': 1.0, 'season': 1.0, 'over': 0.61, 'raw_over': 0.6, 'model_over': 0.7,
                    'book_p': 0.5, 'pick': 'over', 'conf': 'med'}
            ledger.record({'game_id': 'g', 'date': '2026-09-15'}, 'home', {'id': 'p', 'gp': 12}, prop)
            row = next(iter(ledger.rows.values()))
            self.assertEqual((row['model_p'], row['book_p'], row['raw_p'], row['gp']), (0.7, 0.5, 0.6, 12))
            ledger.save()
            again = PropsLedger('mlb', history_dir=tmp)
            self.assertEqual(next(iter(again.rows.values()))['gp'], '12')

    def test_calibration_waits_for_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PropsLedger('mlb', history_dir=tmp)
            _graded_props(ledger, 60, sharp=2.0)
            self.assertIsNone(ledger.calibration())

    def test_timid_probabilities_get_sharpened_and_honest_ones_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PropsLedger('mlb', history_dir=tmp)
            _graded_props(ledger, 600, sharp=2.2)
            cal = ledger.calibration()
            self.assertIsNotNone(cal)
            self.assertGreater(cal['a'], 1.3)
            self.assertLess(cal['holdout_after'], cal['holdout_before'])
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PropsLedger('mlb', history_dir=tmp)
            _graded_props(ledger, 600, sharp=1.0)
            self.assertIsNone(ledger.calibration())

    def test_market_speed_is_learned_only_when_the_book_is_clearly_better(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PropsLedger('mlb', history_dir=tmp)
            # The book's number is the truth here, ours is noise around it:
            # the ledger should slow the hand-over to our number right down.
            _graded_props(ledger, 600, sharp=1.0)
            mk = ledger.market_k(3)
            self.assertIsNotNone(mk)
            self.assertGreater(mk['k'], 3)
            self.assertLess(mk['logloss'], mk['logloss_default'])
            corr = ledger.corrections(3)
            self.assertIn('_market', corr)
            self.assertEqual(corr['_market']['k'], mk['k'])

    def test_pricing_applies_the_learned_corrections(self):
        spec = next(s for s in props_for('baseball', 'batter') if s['key'] == 'hits')
        spec = dict(spec, trials=4.0)
        book = {'line': 0.5, 'books': 3, 'book': '3 books', 'over': -150, 'under': 120}
        plain = _price(spec, 1.1, 1.1, book, season=1.1, sample=2, sport='baseball')
        slow = _price(spec, 1.1, 1.1, book, season=1.1, sample=2, sport='baseball',
                      tuning={'_market': {'k': 80}})
        # With a slow hand-over the blended number sits nearer the book's.
        self.assertLess(abs(slow['raw_over'] - plain['book_p']), abs(plain['raw_over'] - plain['book_p']))
        self.assertEqual(slow['model_over'], plain['model_over'])
        sharp = _price(spec, 1.1, 1.1, book, season=1.1, sample=2, sport='baseball',
                       tuning={'_calibration': {'a': 2.0, 'b': 0.0}})
        self.assertEqual(sharp['raw_over'], plain['raw_over'])
        self.assertGreater(abs(sharp['over'] - 0.5), abs(plain['over'] - 0.5))
        # Corrections under underscore keys never masquerade as a market.
        from sportspred.props import apply_tuning
        self.assertEqual(apply_tuning(spec, {'_calibration': {'a': 2.0}})[1], 1.0)


class TestVoidRows(unittest.TestCase):
    """A corrupt feed day is void everywhere a verdict is counted or shown."""
    def _ledger(self):
        ledger = PropsLedger('mlb', history_dir=self.tmp)
        sane = {'key': 'hits', 'label': 'Hits', 'stat': 'hits_pg', 'dist': 'binomial', 'line': 0.5,
                'proj': 1.0, 'season': 1.0, 'over': 0.6, 'raw_over': 0.6, 'pick': 'over', 'conf': 'med'}
        broken = dict(sane, proj=16.7, season=12.7)
        for i in range(10):
            ledger.record({'game_id': 'g1', 'date': '2026-09-15'}, 'home', {'id': f'p{i}', 'gp': 20}, sane if i % 2 else broken)
        for r in ledger.rows.values():
            r.update(graded='1', played='1', actual='0', push='0', hit='0')   # every one missed
        return ledger

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_void_rows_leave_every_count(self):
        ledger = self._ledger()
        self.assertEqual(len(ledger.suspect_keys()), 5)
        sc = ledger.scorecard()
        self.assertEqual(sc['total'], 5)                 # only the sane half counts
        self.assertEqual(ledger.tallies()['g1'], {'n': 5, 'hit': 0, 'push': 0})
        self.assertEqual(len(ledger._outcome_rows()), 5)
        board = ledger.board_for('g1')
        flags = [p.get('void') for pl in board['home'] for p in pl['props']]
        self.assertEqual(sum(1 for f in flags if f), 5)

    def test_frozen_board_shows_void_not_a_verdict(self):
        from sportspred.learn import FrozenBoards
        ledger = self._ledger()
        boards = FrozenBoards('mlb', history_dir=self.tmp)
        board = {'away': [], 'home': [{'id': 'p0', 'name': 'x', 'props': [{'key': 'hits', 'line': 0.5}]},
                                      {'id': 'p1', 'name': 'y', 'props': [{'key': 'hits', 'line': 0.5}]}]}
        boards.store({'game_id': 'g1', 'date': '2026-09-15'}, board, True)
        boards.annotate('g1', ledger.rows.values(), is_void=ledger.is_void)
        got = boards.get('g1')['home']
        self.assertTrue(got[0]['props'][0].get('void'))
        self.assertIsNone(got[0]['props'][0]['hit'])
        self.assertFalse(got[1]['props'][0].get('void'))
        self.assertIs(got[1]['props'][0]['hit'], False)


class TestPayloadEfficiency(unittest.TestCase):
    def test_finished_boards_keep_only_what_results_show(self):
        from sportspred.pipeline import slim_board
        board = {'locked': True, 'away_out': [1], 'home_out': [], 'away': [], 'home': [
            {'id': 'a', 'name': 'A', 'pos': 'RF', 'headshot': 'h', 'status': 'ok', 'extra': 1, 'props': [
                {'key': 'hits', 'label': 'Hits', 'line': 0.5, 'proj': 1.1, 'pick': 'over', 'pick_prob': 0.6,
                 'conf': 'med', 'over': 0.6, 'under': 0.4, 'model_over': 0.62, 'raw_over': 0.6, 'book_p': 0.55,
                 'book_over': -140, 'dist': 'binomial', 'rank': 1, 'hit': True, 'actual': 2},
                {'key': 'tb', 'label': 'TB', 'pending': True, 'line': None}]},
            {'id': 'b', 'name': 'B', 'props': [{'key': 'hr', 'pending': True, 'line': None}]}]}
        slim = slim_board(board)
        self.assertTrue(slim['locked'])
        self.assertNotIn('away_out', slim)
        self.assertEqual([pl['id'] for pl in slim['home']], ['a'])       # nothing graded for B
        p = slim['home'][0]['props']
        self.assertEqual(len(p), 1)                                       # the pending row is gone
        self.assertEqual(p[0]['hit'], True)
        for gone in ('over', 'under', 'model_over', 'raw_over', 'book_p', 'book_over', 'dist', 'rank'):
            self.assertNotIn(gone, p[0])
        self.assertNotIn('extra', slim['home'][0])
        self.assertIsNone(slim_board(None))

    def test_player_pool_is_reused_while_fresh(self):
        from datetime import datetime, timedelta, timezone
        from sportspred.pipeline import pool_is_fresh
        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(pool_is_fresh((now - timedelta(minutes=20)).strftime('%Y-%m-%dT%H:%M:%SZ'), now))
        self.assertFalse(pool_is_fresh((now - timedelta(minutes=70)).strftime('%Y-%m-%dT%H:%M:%SZ'), now))
        self.assertFalse(pool_is_fresh('', now))
        self.assertFalse(pool_is_fresh(None, now))
        self.assertFalse(pool_is_fresh((now + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ'), now))


if __name__ == '__main__':
    unittest.main()
