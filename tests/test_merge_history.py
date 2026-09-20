"""Folding an overlapping refresh's ledger rows into this run's.

Two refreshes can be in flight at once. The one that pushes second must not
drop what the first recorded: a pick is written to the ledger once, at
publication, so a lost row is a pick that gets republished later — after
kickoff, if the game has started. Each test is one way the two runs can
disagree.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import merge_history                                    # noqa: E402

LEDGER = 'game_id,game_date,favored_team,graded,correct\n'


class MergeCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.theirs = os.path.join(self.dir.name, 'theirs')
        self.ours = os.path.join(self.dir.name, 'ours')
        os.makedirs(self.theirs)
        os.makedirs(self.ours)

    def write(self, where, name, text):
        with io.open(os.path.join(where, name), 'w', encoding='utf-8') as fh:
            fh.write(text)

    def read(self, name):
        with io.open(os.path.join(self.ours, name), encoding='utf-8') as fh:
            return fh.read()

    def merge(self):
        return merge_history.merge(self.theirs, self.ours)


class TestLedgers(MergeCase):
    def test_a_row_only_the_other_run_has_is_kept(self):
        self.write(self.theirs, 'mlb_ledger.csv', LEDGER + '1,2026-09-20,Yankees,0,\n')
        self.write(self.ours, 'mlb_ledger.csv', LEDGER + '2,2026-09-20,Mets,0,\n')
        self.merge()
        self.assertIn('1,2026-09-20,Yankees', self.read('mlb_ledger.csv'))
        self.assertIn('2,2026-09-20,Mets', self.read('mlb_ledger.csv'))

    def test_a_row_both_runs_have_keeps_ours(self):
        # Ours is the later build: it has graded the game, theirs has not.
        self.write(self.theirs, 'mlb_ledger.csv', LEDGER + '1,2026-09-20,Yankees,0,\n')
        self.write(self.ours, 'mlb_ledger.csv', LEDGER + '1,2026-09-20,Yankees,1,1\n')
        self.merge()
        rows = [r for r in self.read('mlb_ledger.csv').splitlines()[1:] if r]
        self.assertEqual(rows, ['1,2026-09-20,Yankees,1,1'])

    def test_identical_files_are_left_alone(self):
        for where in (self.theirs, self.ours):
            self.write(where, 'mlb_ledger.csv', LEDGER + '1,2026-09-20,Yankees,0,\n')
        self.assertEqual(self.merge(), [])

    def test_a_prop_is_identified_by_player_and_market(self):
        head = 'game_id,athlete_id,key,line,pick\n'
        self.write(self.theirs, 'mlb_props.csv', head + '1,99,hits,0.5,over\n1,99,rbi,0.5,under\n')
        self.write(self.ours, 'mlb_props.csv', head + '1,99,hits,0.5,over\n')
        self.merge()
        text = self.read('mlb_props.csv')
        self.assertEqual(text.count('1,99,hits'), 1)
        self.assertIn('1,99,rbi', text)

    def test_a_column_one_side_lacks_does_not_lose_the_row(self):
        # A build either side of a schema change.
        self.write(self.theirs, 'mlb_ledger.csv', 'game_id,game_date\n1,2026-09-20\n')
        self.write(self.ours, 'mlb_ledger.csv', LEDGER + '2,2026-09-20,Mets,0,\n')
        self.merge()
        self.assertIn('1,2026-09-20,,,', self.read('mlb_ledger.csv'))

    def test_a_file_only_the_other_run_has_is_taken_whole(self):
        self.write(self.theirs, 'nhl_ledger.csv', LEDGER + '7,2026-09-20,Bruins,0,\n')
        self.merge()
        self.assertIn('7,2026-09-20,Bruins', self.read('nhl_ledger.csv'))

    def test_an_unmergeable_file_keeps_ours_rather_than_failing(self):
        self.write(self.theirs, 'mlb_boards.json', 'not json at all')
        self.write(self.ours, 'mlb_boards.json', '{"1":{"frozen":1}}')
        notes = self.merge()
        self.assertTrue(any('kept ours' in n for n in notes), notes)
        self.assertEqual(json.loads(self.read('mlb_boards.json')), {'1': {'frozen': 1}})

    def test_an_unknown_file_merges_on_the_whole_row(self):
        self.write(self.theirs, 'mlb_notes.csv', 'a,b\n1,2\n3,4\n')
        self.write(self.ours, 'mlb_notes.csv', 'a,b\n1,2\n')
        self.merge()
        self.assertEqual(self.read('mlb_notes.csv').count('1,2'), 1)
        self.assertIn('3,4', self.read('mlb_notes.csv'))


class TestBoards(MergeCase):
    def board(self, **over):
        b = {'board': {}, 'date': '2026-09-20', 'frozen': 0, 'updated': 'x'}
        b.update(over)
        return b

    def test_a_board_only_the_other_run_has_is_kept(self):
        self.write(self.theirs, 'mlb_boards.json', json.dumps({'1': self.board()}))
        self.write(self.ours, 'mlb_boards.json', json.dumps({'2': self.board()}))
        self.merge()
        self.assertEqual(sorted(json.loads(self.read('mlb_boards.json'))), ['1', '2'])

    def test_a_board_the_other_run_froze_wins(self):
        # Freezing is what locks a board at kickoff; it must never thaw.
        self.write(self.theirs, 'mlb_boards.json', json.dumps({'1': self.board(frozen=1, updated='locked')}))
        self.write(self.ours, 'mlb_boards.json', json.dumps({'1': self.board(frozen=0, updated='open')}))
        self.merge()
        self.assertEqual(json.loads(self.read('mlb_boards.json'))['1']['updated'], 'locked')

    def test_our_open_board_is_kept_when_neither_is_frozen(self):
        self.write(self.theirs, 'mlb_boards.json', json.dumps({'1': self.board(updated='older')}))
        self.write(self.ours, 'mlb_boards.json', json.dumps({'1': self.board(updated='newer')}))
        self.merge()
        self.assertEqual(json.loads(self.read('mlb_boards.json'))['1']['updated'], 'newer')

    def test_our_frozen_board_is_not_thawed_by_theirs(self):
        self.write(self.theirs, 'mlb_boards.json', json.dumps({'1': self.board(frozen=0, updated='open')}))
        self.write(self.ours, 'mlb_boards.json', json.dumps({'1': self.board(frozen=1, updated='locked')}))
        self.merge()
        self.assertEqual(json.loads(self.read('mlb_boards.json'))['1']['updated'], 'locked')


class TestNothingToMerge(MergeCase):
    def test_no_other_run_at_all_is_fine(self):
        self.assertEqual(merge_history.merge(os.path.join(self.dir.name, 'nope'), self.ours), [])


if __name__ == '__main__':
    unittest.main()
