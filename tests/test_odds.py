"""Sportsbook lines: consensus, matching, caching, and the pricing hand-off."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from sportspred import odds, props
from sportspred.config import props_for
from sportspred.util import Http


def _book(title, market, rows):
    return {'title': title, 'markets': [{'key': market, 'outcomes': [
        o for name, point, over, under in rows for o in (
            {'name': 'Over', 'description': name, 'point': point, 'price': over},
            {'name': 'Under', 'description': name, 'point': point, 'price': under})]}]}


PAYLOAD = {'bookmakers': [
    _book('DraftKings', 'batter_hits', [('Shohei Ohtani', 1.5, -105, -115)]),
    _book('FanDuel', 'batter_hits', [('Shohei Ohtani', 0.5, -250, 190)]),
    _book('BetMGM', 'batter_hits', [('Shohei Ohtani', 1.5, -110, -110)]),
    _book('DraftKings', 'pitcher_strikeouts', [('Tyler Glasnow', 6.5, -120, 100)]),
    {'title': 'Caesars', 'markets': [{'key': 'batter_home_runs', 'outcomes': [
        {'name': 'Yes', 'description': 'Aaron Judge Jr.', 'point': 0.5, 'price': 180},
        {'name': 'No', 'description': 'Aaron Judge Jr.', 'point': 0.5, 'price': -240}]}]},
]}


class FakeHttp(Http):
    """Serves canned responses and records the URLs requested."""

    def __init__(self, responses, remaining=500):
        super().__init__(timeout=1, retries=0, pause=0)
        self.responses = responses
        self.urls = []
        self.remaining = remaining

    def get_json(self, url, cache=True):
        self.urls.append(url)
        self.last_headers = {'x-requests-remaining': str(self.remaining)}
        for prefix, data in self.responses.items():
            if prefix in url:
                return data
        return None


def _game(gid, home, away, hours_ahead=6, final=False):
    start = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return {'game_id': gid, 'home': home, 'away': away, 'final': final,
            'date': start.date(), 'row': {'game_start_utc': start.strftime('%Y-%m-%dT%H:%MZ')}}


class TestConsensus(unittest.TestCase):
    def test_median_line_across_books(self):
        lines = odds.consensus_lines(PAYLOAD, 'mlb')
        hits = lines[('shoheiohtani', 'hits')]
        self.assertEqual(hits['line'], 1.5)         # median of 1.5, 0.5, 1.5
        self.assertEqual(hits['books'], 3)
        self.assertEqual(hits['book'], '3 books')
        self.assertEqual(hits['over'], -110)

    def test_market_serves_every_prop_mapped_to_it(self):
        lines = odds.consensus_lines(PAYLOAD, 'mlb')
        self.assertIn(('shoheiohtani', 'hits2'), lines)
        self.assertEqual(lines[('tylerglasnow', 'k')]['book'], 'DraftKings')

    def test_yes_no_markets_and_suffixes(self):
        lines = odds.consensus_lines(PAYLOAD, 'mlb')
        judge = lines[('aaronjudge', 'hr')]
        self.assertEqual(judge['line'], 0.5)
        self.assertEqual(judge['over'], 180)
        self.assertEqual(odds.line_for(lines, 'Aaron Judge', 'hr'), judge)
        self.assertIsNone(odds.line_for(lines, 'Aaron Judge', 'rbi'))
        self.assertIsNone(odds.line_for(None, 'Aaron Judge', 'hr'))

    def test_unknown_markets_are_ignored(self):
        payload = {'bookmakers': [_book('X', 'batter_walks', [('A B', 0.5, -110, -110)])]}
        self.assertEqual(odds.consensus_lines(payload, 'mlb'), {})


class TestEventMatching(unittest.TestCase):
    def test_matches_on_both_teams_and_date(self):
        games = [_game('1', 'New York Yankees', 'Boston Red Sox'),
                 _game('2', 'Los Angeles Dodgers', 'San Diego Padres')]
        today = datetime.now(timezone.utc)
        events = [
            {'id': 'e1', 'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox',
             'commence_time': today.strftime('%Y-%m-%dT%H:%M:%SZ')},
            {'id': 'e2', 'home_team': 'San Diego Padres', 'away_team': 'Los Angeles Dodgers',
             'commence_time': today.strftime('%Y-%m-%dT%H:%M:%SZ')},   # sides swapped: no match
            {'id': 'e3', 'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox',
             'commence_time': (today + timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')},
        ]
        self.assertEqual(odds.match_events(events, games), {'1': 'e1'})

    def test_nickname_only_names_still_match(self):
        self.assertTrue(odds._same_team('Athletics', 'Oakland Athletics'))
        self.assertTrue(odds._same_team('Los Angeles Dodgers', 'LA Dodgers'))
        self.assertFalse(odds._same_team('New York Mets', 'New York Yankees'))


class TestLoadLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_key = os.environ.get('ODDS_API_KEY')
        os.environ['ODDS_API_KEY'] = 'k123'

    def tearDown(self):
        self.tmp.cleanup()
        if self.old_key is None:
            os.environ.pop('ODDS_API_KEY', None)
        else:
            os.environ['ODDS_API_KEY'] = self.old_key

    def _http(self, remaining=500):
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return FakeHttp({
            '/events?': [{'id': 'ev1', 'home_team': 'New York Yankees',
                          'away_team': 'Boston Red Sox', 'commence_time': now}],
            '/events/ev1/odds': PAYLOAD,
        }, remaining=remaining)

    def test_disabled_without_a_key(self):
        os.environ['ODDS_API_KEY'] = ''
        lines, status = odds.load_lines('mlb', [_game('1', 'New York Yankees', 'Boston Red Sox')],
                                        FakeHttp({}), cache_dir=self.tmp.name)
        self.assertEqual(status, 'disabled')
        self.assertEqual(lines, {})

    def test_fetches_once_and_then_serves_the_cache(self):
        games = [_game('1', 'New York Yankees', 'Boston Red Sox'),
                 _game('2', 'Chicago Cubs', 'Miami Marlins', hours_ahead=90),  # too far out
                 _game('3', 'Seattle Mariners', 'Houston Astros', final=True)]
        http = self._http()
        lines, status = odds.load_lines('mlb', games, http, cache_dir=self.tmp.name)
        self.assertEqual(status, 'live')
        self.assertEqual(set(lines), {'1'})
        self.assertEqual(lines['1'][('shoheiohtani', 'hits')]['line'], 1.5)
        self.assertEqual(len(http.urls), 2)
        self.assertTrue(all('k123' in u for u in http.urls))
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, 'mlb_lines.json')))

        http2 = self._http()
        lines2, status2 = odds.load_lines('mlb', games, http2, cache_dir=self.tmp.name)
        self.assertEqual(status2, 'cached')
        self.assertEqual(http2.urls, [])
        self.assertEqual(lines2['1'][('shoheiohtani', 'hits')]['line'], 1.5)

    def test_stops_when_the_quota_reserve_is_reached(self):
        games = [_game('1', 'New York Yankees', 'Boston Red Sox')]
        http = self._http(remaining=odds.MIN_REMAINING)
        lines, status = odds.load_lines('mlb', games, http, cache_dir=self.tmp.name)
        self.assertEqual(status, 'exhausted')
        self.assertEqual(lines, {})
        self.assertEqual(len(http.urls), 1)          # the events call only

    def test_key_is_redacted_from_the_error_table(self):
        http = FakeHttp({})
        client = odds.OddsClient(http, key='secret')
        http.errors['x'] = 'y'
        client.get('/sports/baseball_mlb/events')
        http.errors[http.urls[-1]] = 'HTTP 401'
        client.get('/sports/baseball_mlb/events')
        self.assertFalse(any('secret' in k for k in http.errors))


class TestPricingAgainstBookLines(unittest.TestCase):
    def _spec(self, key):
        return next(s for s in props_for('baseball', 'batter') if s['key'] == key)

    def test_model_line_when_no_book(self):
        p = props._price(self._spec('hits'), 1.1, 1.3)
        self.assertEqual(p['line_source'], 'model')
        self.assertEqual(p['line'], 0.5)             # the spec's fixed line
        self.assertNotIn('book', p)
        spec = dict(self._spec('hits'), line=None)
        self.assertEqual(props._price(spec, 1.1, 1.3)['line'], 1.5)

    def test_book_line_replaces_the_derived_one(self):
        book = {'line': 1.5, 'books': 3, 'book': '3 books', 'over': -250, 'under': 190}
        p = props._price(self._spec('hits'), 1.1, 1.3, book)
        self.assertEqual(p['line_source'], 'book')
        self.assertEqual(p['line'], 1.5)
        self.assertEqual(p['book'], '3 books')
        self.assertEqual(p['books'], 3)
        self.assertEqual(p['book_over'], -250)
        # P(hits >= 2) for a 1.3-hit projection is well under a half.
        self.assertEqual(p['pick'], 'under')
        self.assertLess(p['over'], 0.45)
        # Against a market line the edge is the gap to that line.
        self.assertAlmostEqual(p['edge'], p['line_gap'], delta=0.006)

    def test_a_book_without_a_line_is_ignored(self):
        p = props._price(self._spec('hits'), 1.1, 1.3, {'line': None})
        self.assertEqual(p['line_source'], 'model')


if __name__ == '__main__':
    unittest.main()


class TestBookModeBoards(unittest.TestCase):
    """With the odds feed connected, a prop is only ever priced against a
    market line; without one it stays blank until the books post it."""

    def setUp(self):
        from sportspred import config
        from tests.helpers import player_pool
        self.cfg = config.LEAGUES['mlb']
        self.pool = player_pool(['Team A', 'Team B'])
        self.env = props.team_environment(
            [dict(final=True, home='Team A', away='Team B', home_score=5, away_score=4)] * 10,
            self.cfg)
        self.game = {'home': 'Team A', 'away': 'Team B', 'game_id': '1', 'date': date(2026, 9, 16)}

    def _build(self, lines, book_mode=True):
        return props.build_for_game(self.game, self.pool, self.env, self.cfg, 'baseball', 0.55,
                                    lines=lines, book_mode=book_mode)

    def test_no_lines_yet_means_blank_props(self):
        board = self._build(None)
        self.assertTrue(board['home'])
        for player in board['home']:
            for p in player['props']:
                self.assertTrue(p['pending'])
                self.assertIsNone(p['line'])
                self.assertNotIn('pick', p)
                self.assertIn('proj', p)          # our projection is still shown
                self.assertLessEqual(p['rank'], props.PENDING_RANK)

    def test_lines_fill_in_and_bring_extra_players(self):
        # A bench bat the quota would not pick, plus the star, both get lines.
        bench = self.pool['teama'][5]['name']
        star = self.pool['teama'][0]['name']
        lines = {(odds._name_key(bench), 'hits'): {'line': 0.5, 'books': 2, 'book': '2 books'},
                 (odds._name_key(star), 'tb'): {'line': 1.5, 'books': 1, 'book': 'DraftKings'},
                 (odds._name_key(star), 'hr'): {'line': 0.5, 'books': 1, 'book': 'DraftKings'}}
        board = self._build(lines)
        names = [p['name'] for p in board['home']]
        self.assertIn(bench, names)
        self.assertIn(star, names)
        # Everyone the books priced leads the board.
        self.assertTrue(all(any(not x.get('pending') for x in p['props']) for p in board['home'][:2]))
        star_props = {p['key']: p for p in next(p for p in board['home'] if p['name'] == star)['props']}
        self.assertEqual(star_props['tb']['line_source'], 'book')
        self.assertEqual(star_props['tb']['line'], 1.5)
        self.assertEqual(star_props['tb']['book'], 'DraftKings')
        self.assertIn('pick', star_props['tb'])
        # Markets without a line stay blank next to the priced ones.
        blanks = [p for p in star_props.values() if p.get('pending')]
        self.assertTrue(blanks)
        # Priced props come first.
        keys = [p.get('pending', False) for p in next(p for p in board['home'] if p['name'] == star)['props']]
        self.assertEqual(keys, sorted(keys))

    def test_model_mode_prices_everything_itself(self):
        board = self._build(None, book_mode=False)
        for player in board['home']:
            for p in player['props']:
                self.assertEqual(p['line_source'], 'model')
                self.assertIsNotNone(p['line'])

    def test_blank_props_are_never_written_to_the_ledger(self):
        from sportspred.learn import PropsLedger
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PropsLedger('mlb', history_dir=tmp)
            board = self._build(None)
            player = board['home'][0]
            self.assertFalse(ledger.record(self.game, 'home', player, player['props'][0]))
            priced = props._price(dict(props_for('baseball', 'batter')[0]), 1.0, 1.1,
                                  {'line': 0.5, 'books': 1, 'book': 'FanDuel'})
            self.assertTrue(ledger.record(self.game, 'home', player, priced))
            row = next(iter(ledger.rows.values()))
            self.assertEqual(row['line_source'], 'book')
            self.assertEqual(row['book'], 'FanDuel')

    def test_board_rebuilt_from_ledger_when_none_was_stored(self):
        from sportspred.learn import PropsLedger
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PropsLedger('mlb', history_dir=tmp)
            board = self._build(None, book_mode=False)
            # The synthetic pool reuses athlete ids across teams, so one side.
            for player in board['home']:
                for p in player['props']:
                    ledger.record(self.game, 'home', player, p)
            rebuilt = ledger.board_for('1')
            self.assertEqual(len(rebuilt['home']), len(board['home']))
            first = rebuilt['home'][0]['props'][0]
            for key in ('line', 'proj', 'over', 'pick', 'pick_prob', 'conf', 'label'):
                self.assertIn(key, first)
            self.assertEqual(ledger.board_for('nope'), None)


def _age(path, gid):
    with open(path) as f:
        cache = json.load(f)
    cache[gid]['fetched'] = (datetime.now(timezone.utc) - timedelta(hours=odds.REFRESH_HOURS + 1)
                             ).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(path, 'w') as f:
        json.dump(cache, f)


class TestRefreshPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get('ODDS_API_KEY')
        os.environ['ODDS_API_KEY'] = 'k'

    def tearDown(self):
        self.tmp.cleanup()
        if self.old is None:
            os.environ.pop('ODDS_API_KEY', None)
        else:
            os.environ['ODDS_API_KEY'] = self.old

    def test_stale_lines_are_refetched_and_new_players_appear(self):
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        events = [{'id': 'ev1', 'home_team': 'New York Yankees',
                   'away_team': 'Boston Red Sox', 'commence_time': now}]
        games = [_game('1', 'New York Yankees', 'Boston Red Sox')]
        first = {'bookmakers': [_book('DraftKings', 'batter_hits', [('Aaron Judge', 1.5, -110, -110)])]}
        lines, status = odds.load_lines('mlb', games, FakeHttp({'/events?': events, '/odds': first}),
                                        cache_dir=self.tmp.name)
        self.assertEqual(set(k[0] for k in lines['1']), {'aaronjudge'})
        # Age the cache past the refresh window.
        path = os.path.join(self.tmp.name, 'mlb_lines.json')
        _age(path, '1')
        second = {'bookmakers': [_book('DraftKings', 'batter_hits',
                                       [('Aaron Judge', 1.5, -110, -110), ('Juan Soto', 0.5, -200, 160)])]}
        http = FakeHttp({'/events?': events, '/odds': second})
        lines, status = odds.load_lines('mlb', games, http, cache_dir=self.tmp.name)
        self.assertEqual(status, 'live')
        self.assertEqual(set(k[0] for k in lines['1']), {'aaronjudge', 'juansoto'})
        # An empty answer later never wipes what we have.
        _age(path, '1')
        lines, _ = odds.load_lines('mlb', games, FakeHttp({'/events?': events, '/odds': {'bookmakers': []}}),
                                   cache_dir=self.tmp.name)
        self.assertEqual(set(k[0] for k in lines['1']), {'aaronjudge', 'juansoto'})


class TestMainLineAndNames(unittest.TestCase):
    def test_alternate_long_shot_lines_do_not_replace_the_main_line(self):
        payload = {'bookmakers': [{'title': 'DraftKings', 'markets': [{'key': 'batter_home_runs', 'outcomes': [
            {'name': 'Over', 'description': 'Randal Grichuk', 'point': 0.5, 'price': 260},
            {'name': 'Under', 'description': 'Randal Grichuk', 'point': 0.5, 'price': -340},
            {'name': 'Over', 'description': 'Randal Grichuk', 'point': 1.5, 'price': 8000},
        ]}]}]}
        lines = odds.consensus_lines(payload, 'mlb')
        self.assertEqual(lines[('randalgrichuk', 'hr')]['line'], 0.5)
        self.assertEqual(lines[('randalgrichuk', 'hr')]['over'], 260)

    def test_most_balanced_two_sided_point_wins(self):
        points = {0.5: {'over': -300, 'under': 230}, 1.5: {'over': 105, 'under': -125}, 2.5: {'over': 400, 'under': -600}}
        self.assertEqual(odds.main_line(points)[0], 1.5)
        self.assertEqual(odds.main_line({1.5: {'over': 5500}})[0], 1.5)   # only option
        self.assertEqual(odds.main_line({})[0], None)

    def test_accented_names_match(self):
        self.assertEqual(odds._name_key('Agustín Ramírez'), odds._name_key('Agustin Ramirez'))
        self.assertEqual(odds._name_key('José Ramírez Jr.'), 'joseramirez')


class TestBudgetPacing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in ('ODDS_API_KEY', 'ODDS_MONTHLY_CREDITS')}
        os.environ['ODDS_API_KEY'] = 'k'

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_daily_allowance_spreads_the_month(self):
        os.environ['ODDS_MONTHLY_CREDITS'] = '3100'
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        b = odds.Budget(self.tmp.name, now)
        self.assertAlmostEqual(b.daily_allowance(), (3100 - odds.MIN_REMAINING) / 30, places=3)
        b.spend(60, remaining=2000)
        b.save()
        again = odds.Budget(self.tmp.name, now)
        self.assertEqual(again.remaining, 2000)          # the API's figure wins
        self.assertEqual(again.state['spent_today'], 60)
        # A new month resets the spend.
        fresh = odds.Budget(self.tmp.name, datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(fresh.state['spent_month'], 0)

    def test_fetching_stops_at_the_daily_allowance_and_prices_soonest_first(self):
        os.environ['ODDS_MONTHLY_CREDITS'] = str(odds.MIN_REMAINING + 31 * 15)   # 15 credits a day
        now = datetime.now(timezone.utc)
        stamp = lambda h: (now + timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M:%SZ')
        events = [{'id': 'e1', 'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox', 'commence_time': stamp(20)},
                  {'id': 'e2', 'home_team': 'Chicago Cubs', 'away_team': 'Miami Marlins', 'commence_time': stamp(5)}]
        games = [_game('1', 'New York Yankees', 'Boston Red Sox', hours_ahead=20),
                 _game('2', 'Chicago Cubs', 'Miami Marlins', hours_ahead=5)]
        # The API's own header says 52 credits remain after the first call,
        # which the pacing trusts over the configured plan.
        http = FakeHttp({'/events?': events,
                         '/events/e1/odds': {'bookmakers': [_book('DK', 'batter_hits', [('A Judge', 0.5, -200, 160)])]},
                         '/events/e2/odds': {'bookmakers': [_book('DK', 'batter_hits', [('P Crow-Armstrong', 0.5, -150, 120)])]}},
                        remaining=odds.MIN_REMAINING + 12)
        lines, status = odds.load_lines('mlb', games, http, cache_dir=self.tmp.name)
        # One game's worth of markets (10 credits) fits the day; the second does not.
        self.assertEqual(status, 'budgeted')
        self.assertEqual(set(lines), {'2'})              # the game starting sooner
        self.assertEqual(sum('/odds' in u for u in http.urls), 1)
        with open(os.path.join(self.tmp.name, 'odds_budget.json')) as f:
            state = json.load(f)
        self.assertEqual(state['spent_today'], len(set(odds.MARKETS['mlb'].values())))


class TestEdgeAgainstTheBook(unittest.TestCase):
    def test_edge_is_our_probability_minus_the_devigged_book_price(self):
        self.assertAlmostEqual(props.implied_over(-110, -110), 0.5, places=6)
        self.assertAlmostEqual(props.implied_over(100, -120), 0.4783, places=3)
        spec = dict(props_for('baseball', 'batter')[0], line=None)
        book = {'line': 0.5, 'books': 2, 'book': '2 books', 'over': -200, 'under': 160}
        p = props._price(spec, 1.0, 1.0, book)
        self.assertAlmostEqual(p['book_p'], props.implied_over(-200, 160), places=4)
        ours = p['over'] if p['pick'] == 'over' else 1 - p['over']
        theirs = p['book_p'] if p['pick'] == 'over' else 1 - p['book_p']
        self.assertAlmostEqual(p['edge_pts'], round((ours - theirs) * 100, 1), places=1)
        # No prices, no book edge.
        q = props._price(spec, 1.0, 1.0, {'line': 0.5, 'books': 1, 'book': 'X'})
        self.assertNotIn('edge_pts', q)


class TestLeagueShare(unittest.TestCase):
    def test_a_league_cannot_take_the_whole_day_from_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get('ODDS_MONTHLY_CREDITS')
            os.environ['ODDS_MONTHLY_CREDITS'] = str(odds.MIN_REMAINING + 31 * 100)   # 100 a day
            try:
                b = odds.Budget(tmp, datetime(2026, 9, 1, tzinfo=timezone.utc))
                b.spend(10, league='mlb')                      # the first-of-day fetch
                # MLB may keep going until it has used what the others have not.
                spent = 10
                while b.can_spend(10, 'mlb'):
                    b.spend(10, league='mlb'); spent += 10
                self.assertEqual(spent, 100)                   # nobody else has spent: all of it
                # But once NHL has spent, MLB's share is capped at the remainder or a quarter.
                b2 = odds.Budget(tmp, datetime(2026, 9, 2, tzinfo=timezone.utc))
                b2.spend(60, league='nhl')
                self.assertTrue(b2.can_spend(10, 'mlb'))       # 40 left for the others
                b2.spend(40, league='mlb')
                self.assertFalse(b2.can_spend(10, 'nhl'))
                self.assertFalse(b2.can_spend(10, 'mlb'))
                # And a quarter is always guaranteed even if others have spent it all.
                b3 = odds.Budget(tmp, datetime(2026, 9, 3, tzinfo=timezone.utc))
                b3.spend(75, league='nfl')
                self.assertTrue(b3.can_spend(20, 'nba'))
            finally:
                if old is None:
                    os.environ.pop('ODDS_MONTHLY_CREDITS', None)
                else:
                    os.environ['ODDS_MONTHLY_CREDITS'] = old


class TestMarketBlend(unittest.TestCase):
    def test_a_three_game_sample_leans_on_the_book(self):
        spec = dict(props_for('baseball', 'pitcher')[0], line=None)
        book = {'line': 2.5, 'books': 2, 'book': '2 books', 'over': -110, 'under': -110}
        rookie = props._price(spec, 6.0, 6.0, book, sample=3, sport='baseball')
        veteran = props._price(spec, 6.0, 6.0, book, sample=150, sport='baseball')
        raw = props._price(spec, 6.0, 6.0, book)
        # Same projection: the rookie's probability sits much closer to the
        # book's 50% than the veteran's does.
        self.assertLess(abs(rookie['over'] - 0.5), abs(veteran['over'] - 0.5))
        self.assertAlmostEqual(veteran['over'], raw['over'], delta=0.05)
        self.assertLess(abs(rookie['edge_pts']), abs(veteran['edge_pts']))
        self.assertEqual(rookie['proj'], veteran['proj'])
