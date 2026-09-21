"""The monitor itself.

A health check nobody tests is worse than none: it reports "healthy" while
the thing it watches burns. Each test breaks one of the promises in
docs/RUNBOOK.md and checks the monitor notices — and, just as important,
that the ordinary shapes of the data do not set it off.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import health_check                                     # noqa: E402
from sportspred import config                           # noqa: E402
from sportspred.render import asset_version             # noqa: E402

LEDGER_COLS = ('game_id,game_date,predicted_at,favored_team,graded,correct,preseason,replay')
PROP_COLS = 'game_id,athlete_id,key,player,line,proj,pick,played,graded,hit,push,stat'


def stamp(minutes_ago):
    return (health_check.now() - dt.timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


class HealthCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = self.dir.name
        self.data = os.path.join(self.base, 'data')
        self.history = os.path.join(self.base, 'history')
        os.makedirs(self.data)
        os.makedirs(self.history)
        for name, value in (('BASE', self.base), ('DATA_DIR', self.data),
                            ('HISTORY_DIR', self.history)):
            patch = unittest.mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # The checkout comparison shells out to git; there is no repository
        # here, so it is stubbed out except where a test is about it.
        for name in ('check_checkout', 'check_publishing'):
            patch = unittest.mock.patch.object(health_check, name, lambda report: None)
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, path, text):
        with io.open(os.path.join(self.base, path), 'w', encoding='utf-8') as fh:
            fh.write(text)

    def build(self, games=(), built_minutes_ago=5, **over):
        v = asset_version()
        self.write('index.html', f'<link rel="stylesheet" href="assets/app.css?v={v}">\n'
                                 f'<script src="assets/app.js?v={v}" defer></script>\n')
        from sportspred import verify
        payload = {key: 'x' for key in verify.REQUIRED_KEYS}
        payload.update({'league': 'mlb', 'today': '2026-09-20', 'games': list(games),
                        'props_status': 'live', 'lines_status': {'mode': 'book'},
                        'generated': stamp(built_minutes_ago),
                        'prop_groups': config.prop_groups_for('baseball')})
        payload.update(over)
        self.write('data/mlb.js', 'window.SP_DATA={};\nwindow.SP_DATA["mlb"]='
                   + json.dumps(payload) + ';\n')
        for name, marker, body in (('mlb-history.js', 'SP_HISTORY', '[]'),
                                   ('mlb-records.js', 'SP_RECORDS', '{}')):
            self.write(f'data/{name}', f'window.{marker}={{}};\nwindow.{marker}["mlb"]={body};\n')

    def ledger(self, *lines):
        self.write('history/mlb_ledger.csv', LEDGER_COLS + '\n' + ''.join(l + '\n' for l in lines))

    def props(self, *lines):
        self.write('history/mlb_props.csv', PROP_COLS + '\n' + ''.join(l + '\n' for l in lines))

    def run_check(self):
        return health_check.run(['mlb'])

    def assertFlags(self, level, needle):
        report = self.run_check()
        found = getattr(report, level)
        self.assertTrue(any(needle in f for f in found),
                        f'expected a {level[:-1]} about {needle!r}; got '
                        f'problems={report.problems} watches={report.watches}')

    def assertQuiet(self):
        report = self.run_check()
        self.assertEqual(report.problems, [])


class TestAHealthySiteIsQuiet(HealthCase):
    def test_nothing_to_report(self):
        self.build(games=[{'id': '1', 'date': '2026-09-20', 'favored': 'Mets'}])
        self.assertQuiet()


class TestFreshness(HealthCase):
    def test_a_board_hours_old_is_a_problem(self):
        self.build(built_minutes_ago=200)
        self.assertFlags('problems', 'hours old')

    def test_a_board_an_hour_and_a_half_old_is_only_a_watch(self):
        self.build(built_minutes_ago=90)
        self.assertFlags('watches', 'minutes old')
        self.assertQuiet()

    def test_a_board_that_does_not_say_when_it_was_built(self):
        self.build(generated='')
        self.assertFlags('problems', 'does not say when it was built')


class TestTheLockPromise(HealthCase):
    """The one promise that cannot be repaired after the fact."""
    def game(self, **over):
        g = {'id': '1', 'date': '2026-09-20', 'favored': 'Mets',
             'start': stamp(120).replace('Z', ''), 'locked': True}
        g.update(over)
        return g

    def test_a_pick_first_recorded_after_kickoff_is_caught(self):
        self.build(games=[self.game()])
        self.ledger(f'1,2026-09-20,{stamp(30)},Mets,0,,0,')
        self.assertFlags('problems', 'after the game started')

    def test_a_pick_recorded_before_kickoff_is_fine(self):
        self.build(games=[self.game()])
        self.ledger(f'1,2026-09-20,{stamp(300)},Mets,0,,0,')
        self.assertQuiet()

    def test_a_replay_is_not_a_late_pick(self):
        # A replay deliberately predicts games that already happened.
        self.build(games=[self.game()])
        self.ledger(f'1,2026-09-20,{stamp(30)},Mets,0,,0,1')
        self.assertQuiet()

    def test_a_finished_game_that_was_never_locked_is_caught(self):
        self.build(games=[self.game(locked=False, final=True)])
        self.assertFlags('problems', 'never locked')


class TestResultsReachable(HealthCase):
    def test_graded_games_that_reach_nothing_are_caught(self):
        self.build()
        self.ledger('1,2026-09-20,,Mets,1,1,0,')
        self.assertFlags('problems', 'none can be reached in Results')

    def test_graded_games_in_the_archive_are_reachable(self):
        self.build()
        self.ledger('1,2026-09-20,,Mets,1,1,0,')
        self.write('data/mlb-history.js', 'window.SP_HISTORY={};\n'
                                          'window.SP_HISTORY["mlb"]=[{"id":"1"}];\n')
        self.assertQuiet()


class TestLedgerIntegrity(HealthCase):
    def test_a_game_recorded_twice_is_caught(self):
        self.build()
        self.ledger('1,2026-09-20,,Mets,1,1,0,', '1,2026-09-20,,Mets,1,0,0,')
        self.write('data/mlb-history.js', 'window.SP_HISTORY={};\nwindow.SP_HISTORY["mlb"]=[{"id":"1"}];\n')
        self.assertFlags('problems', 'appear twice in the ledger')

    def test_a_verdict_without_a_result_is_caught(self):
        self.build()
        self.ledger('1,2026-09-20,,Mets,1,maybe,0,')
        self.write('data/mlb-history.js', 'window.SP_HISTORY={};\nwindow.SP_HISTORY["mlb"]=[{"id":"1"}];\n')
        self.assertFlags('problems', 'its verdict is')


class TestGradeability(HealthCase):
    """A market published but never scored: quiet damage, because the picks
    look right on the page and simply never reach the record.

    Judged on the most recent handful, in ledger order, so the alarm lights
    within about one slate of a market breaking and goes out as soon as a fix
    actually grades something — rather than staying lit for a week over
    damage already done and no longer fixable.
    """
    def market(self, key, n, scored_from=None):
        """``scored_from`` is the index at and after which picks got a
        verdict, so the scored ones are the most recent."""
        cut = n if scored_from is None else scored_from
        return [f'g{i},9{i},{key},A Player,0.5,1.0,over,1,1,'
                f'{"1" if i >= cut else ""},0,hits_pg' for i in range(n)]

    def test_a_market_that_is_never_scored_is_caught(self):
        self.build()
        self.props(*self.market('rbi', 20))
        self.assertFlags('problems', 'published but cannot be graded')

    def test_a_known_ungradeable_market_is_only_a_watch(self):
        self.build()
        self.props(*self.market('sb', 20))
        self.assertFlags('watches', 'were scored')
        self.assertQuiet()

    def test_one_recent_success_clears_it(self):
        # The shape right after a fix lands: a long tail of picks that were
        # never scored and never can be, and one that just was.
        self.build()
        self.props(*self.market('rbi', 20, scored_from=19))
        self.assertQuiet()

    def test_old_successes_do_not_excuse_a_market_that_broke(self):
        self.build()
        self.props(*self.market('rbi', 30, scored_from=0)[:10],
                   *self.market('rbi', 30)[10:])
        self.assertFlags('problems', 'published but cannot be graded')

    def test_an_occasional_gap_is_not_a_fault(self):
        self.build()
        rows = self.market('rbi', 20, scored_from=0)
        rows[5] = rows[5].replace(',1,0,hits_pg', ',,0,hits_pg')
        self.props(*rows)
        self.assertQuiet()

    def test_too_few_picks_to_judge_says_nothing(self):
        self.build()
        self.props(*self.market('rbi', 4))
        self.assertQuiet()

    def test_a_player_who_did_not_play_owes_no_verdict(self):
        self.build()
        self.props(*[f'g{i},9{i},rbi,A Player,0.5,1.0,over,0,1,,0,hits_pg' for i in range(20)])
        self.assertQuiet()


class TestFrozenBoards(HealthCase):
    def test_a_board_for_a_game_under_way_that_is_not_frozen(self):
        self.build(games=[{'id': '1', 'date': '2026-09-20', 'favored': 'Mets',
                           'start': stamp(120).replace('Z', '')}])
        self.write('history/mlb_boards.json', json.dumps({'1': {'frozen': 0}}))
        self.assertFlags('problems', 'not frozen')

    def test_a_frozen_board_is_fine(self):
        self.build(games=[{'id': '1', 'date': '2026-09-20', 'favored': 'Mets',
                           'start': stamp(120).replace('Z', '')}])
        self.write('history/mlb_boards.json', json.dumps({'1': {'frozen': 1}}))
        self.assertQuiet()

    def test_a_board_for_a_game_not_yet_started_is_fine(self):
        self.build(games=[{'id': '1', 'date': '2026-09-20', 'favored': 'Mets',
                           'start': stamp(-120).replace('Z', '')}])
        self.write('history/mlb_boards.json', json.dumps({'1': {'frozen': 0}}))
        self.assertQuiet()


class TestStandingConditions(HealthCase):
    """Section 4 of the runbook: real, understood, and not incidents."""
    def test_a_feed_on_its_fallback_is_a_watch_not_a_problem(self):
        self.build(props_status='off', lines_status={'mode': 'model'})
        report = self.run_check()
        self.assertEqual(report.problems, [])
        self.assertEqual(len(report.watches), 2)


class TestTheVerdict(HealthCase):
    def test_a_healthy_site_exits_zero(self):
        self.build()
        with unittest.mock.patch.object(health_check, 'run', return_value=health_check.Report()):
            self.assertEqual(health_check.main(['--quiet']), 0)

    def test_a_broken_site_exits_non_zero(self):
        report = health_check.Report()
        report.problem('mlb', 'something is wrong')
        with unittest.mock.patch.object(health_check, 'run', return_value=report):
            self.assertEqual(health_check.main(['--quiet']), 1)

    def test_a_check_that_cannot_run_says_so(self):
        with unittest.mock.patch.object(health_check, 'run', side_effect=RuntimeError('boom')):
            self.assertEqual(health_check.main(['--quiet']), 2)


if __name__ == '__main__':
    unittest.main()
