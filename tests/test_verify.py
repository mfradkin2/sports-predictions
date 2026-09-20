"""The gate that decides whether a build is fit to publish.

Each test breaks the site the way it has actually broken, or could, and
checks the gate notices. A guard nobody tests is a guard nobody can trust.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from sportspred import config, verify
from sportspred.render import asset_version


def a_payload(**over):
    data = {key: 'x' for key in verify.REQUIRED_KEYS}
    data.update({'league': 'mlb', 'today': '2026-09-20', 'games': [],
                 'prop_groups': config.prop_groups_for('baseball')})
    data.update(over)
    return data


def a_game(**over):
    game = {'id': '1', 'date': '2026-09-20', 'favored': 'New York Yankees'}
    game.update(over)
    return game


def a_prop(**over):
    prop = {'key': 'hits', 'label': 'Hits', 'stat': 'hits_pg',
            'line': 0.5, 'proj': 1.1, 'pick': 'over'}
    prop.update(over)
    return prop


def board(*props):
    return {'away': [], 'home': [{'id': 'p1', 'name': 'A Hitter', 'props': list(props)}]}


class VerifyCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = self.dir.name
        self.data = os.path.join(self.base, 'data')
        os.makedirs(self.data)
        self.addCleanup(self.dir.cleanup)

    def build(self, payload=None, version=None, side_files=True, pages=('index.html', 'mlb.html')):
        v = version or asset_version()
        for name in pages:
            with open(os.path.join(self.base, name), 'w', encoding='utf-8') as fh:
                fh.write(f'<link rel="stylesheet" href="assets/app.css?v={v}">\n'
                         f'<script src="assets/app.js?v={v}" defer></script>\n')
        data = payload if payload is not None else a_payload()
        with open(os.path.join(self.data, 'mlb.js'), 'w', encoding='utf-8') as fh:
            fh.write('window.SP_DATA=window.SP_DATA||{};\n'
                     'window.SP_DATA["mlb"]=' + json.dumps(data) + ';\n')
        if side_files:
            for name, marker, body in (('mlb-history.js', 'SP_HISTORY', '[]'),
                                       ('mlb-records.js', 'SP_RECORDS', '{}')):
                with open(os.path.join(self.data, name), 'w', encoding='utf-8') as fh:
                    fh.write(f'window.{marker}=window.{marker}||{{}};\n'
                             f'window.{marker}["mlb"]={body};\n')

    def problems(self):
        return verify.verify(base=self.base, data_dir=self.data, leagues=['mlb'])

    def assertComplains(self, needle):
        found = self.problems()
        self.assertTrue(any(needle in p for p in found),
                        f'expected a complaint about {needle!r}, got {found}')


class TestAHealthyBuildPasses(VerifyCase):
    def test_nothing_to_complain_about(self):
        self.build(a_payload(games=[a_game(props=board(a_prop()))]))
        self.assertEqual(self.problems(), [])

    # Deliberately no test of the site already on disk. The suite runs before
    # the pipeline, so that would judge the *previous* build: the first change
    # to add a block the old payload cannot have would fail the tests, stop
    # the run, and leave the site unable to rebuild itself out of it. The
    # freshly built output is checked instead, at the end of run_pipeline.py.


class TestStalePages(VerifyCase):
    def test_a_page_built_by_older_code_is_caught(self):
        # Exactly the failure that once left the pages a revision behind:
        # a refresh that started before a change landed pushed its own pages.
        self.build(version='deadbeef')
        self.assertComplains('but the assets in this build hash to')

    def test_a_page_that_loads_no_script_is_caught(self):
        self.build()
        with open(os.path.join(self.base, 'index.html'), 'w', encoding='utf-8') as fh:
            fh.write('<h1>hello</h1>')
        self.assertComplains('does not load app.js')

    def test_no_pages_at_all_is_caught(self):
        self.build(pages=())
        self.assertComplains('no pages were written')


class TestPayloadShape(VerifyCase):
    def test_a_missing_block_is_caught(self):
        data = a_payload()
        del data['model']
        self.build(data)
        self.assertComplains('no "model" block')

    def test_a_payload_that_will_not_parse_is_caught(self):
        self.build()
        with open(os.path.join(self.data, 'mlb.js'), 'w', encoding='utf-8') as fh:
            fh.write('window.SP_DATA["mlb"]={not json};')
        self.assertComplains('could not be read back')

    def test_a_missing_league_file_is_caught(self):
        self.build()
        os.remove(os.path.join(self.data, 'mlb.js'))
        self.assertComplains('mlb.js was not written')

    def test_a_missing_side_file_is_caught(self):
        self.build(side_files=False)
        self.assertComplains('mlb-history.js was not written')

    def test_a_nonsense_date_is_caught(self):
        self.build(a_payload(today='soon'))
        self.assertComplains('"today" is')

    def test_a_market_in_no_category_is_caught(self):
        thin = [{'key': 'bat', 'label': 'Batting', 'markets': ['hits']}]
        self.build(a_payload(prop_groups=thin))
        self.assertComplains('markets in no category chip')

    def test_a_market_in_two_categories_is_caught(self):
        both = config.prop_groups_for('baseball') + [{'key': 'dup', 'label': 'Dup', 'markets': ['hits']}]
        self.build(a_payload(prop_groups=both))
        self.assertComplains('appears in two category chips')


class TestGamesAndProps(VerifyCase):
    def test_a_game_with_no_pick_is_caught(self):
        self.build(a_payload(games=[a_game(favored='')]))
        self.assertComplains('has no pick')

    def test_a_game_with_no_date_is_caught(self):
        self.build(a_payload(games=[a_game(date='')]))
        self.assertComplains('has date')

    def test_a_lean_against_its_own_projection_is_caught(self):
        self.build(a_payload(games=[a_game(props=board(a_prop(proj=1.1, line=0.5, pick='under')))]))
        self.assertComplains('leans under with a projection of 1.1 against 0.5')

    def test_a_projection_no_player_could_post_is_caught(self):
        self.build(a_payload(games=[a_game(props=board(a_prop(proj=16.7, line=0.5)))]))
        self.assertComplains('which no hits ever reaches')

    def test_a_blank_prop_holding_a_line_is_caught(self):
        self.build(a_payload(games=[a_game(props=board(a_prop(pending=True)))]))
        self.assertComplains('waiting on a line but has one')

    def test_a_priced_prop_without_a_line_is_caught(self):
        self.build(a_payload(games=[a_game(props=board(a_prop(line=None)))]))
        self.assertComplains('priced but has no line')

    def test_a_prop_with_no_side_is_caught(self):
        self.build(a_payload(games=[a_game(props=board(a_prop(pick='')))]))
        self.assertComplains('has no side')

    def test_a_blank_prop_without_a_line_is_fine(self):
        self.build(a_payload(games=[a_game(props=board(a_prop(pending=True, line=None, pick='')))]))
        self.assertEqual(self.problems(), [])

    def test_a_graded_prop_that_landed_on_the_line_is_fine(self):
        # A push: the projection sits exactly on the line, either side is
        # an honest lean and neither is wrong.
        self.build(a_payload(games=[a_game(props=board(a_prop(proj=0.5, line=0.5, pick='under')))]))
        self.assertEqual(self.problems(), [])


class TestReport(unittest.TestCase):
    def test_a_clean_build_says_so(self):
        self.assertEqual(verify.report([]), 'Site checks passed.')

    def test_a_long_list_is_trimmed_but_counted(self):
        text = verify.report([f'problem {i}' for i in range(40)], limit=5)
        self.assertIn('40 problem(s)', text)
        self.assertIn('and 35 more', text)
        self.assertEqual(text.count('\n  - '), 5)


if __name__ == '__main__':
    unittest.main()
