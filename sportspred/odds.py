"""Sportsbook lines for player props, from The Odds API.

The line a prop is priced against should be the market's, not our own: our
projection is the thing being tested, and testing it against a line we also
derived proves nothing. This module fetches the consensus line per player and
market from the books, budget-aware, and caches them so each game costs one
request per market group.

Requires ``ODDS_API_KEY`` (https://the-odds-api.com). Without it the props
board falls back to model-derived lines and says so.
"""
from __future__ import annotations

import calendar
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from . import config
from .espn import norm
from .util import Http, now_iso, num, parse_iso, read_json, short_name, write_json

API = 'https://api.the-odds-api.com/v4'
SPORT_KEYS = {'mlb': 'baseball_mlb', 'nba': 'basketball_nba',
              'nfl': 'americanfootball_nfl', 'nhl': 'icehockey_nhl'}
REGION = 'us'
AHEAD_HOURS = 36            # fetch lines only this close to kickoff
MIN_REMAINING = 40          # keep this much monthly quota in reserve
# How often a game's lines are re-fetched before kickoff, so a player the
# books had not posted yet fills in once they do. Each fetch costs one credit
# per market, so this is a quota dial (ODDS_REFRESH_HOURS overrides it).
REFRESH_HOURS = 4
# Monthly credit allowance to pace against (ODDS_MONTHLY_CREDITS overrides).
# Spend is spread evenly over the days left in the month, so a busy slate
# early on cannot leave the last week with nothing.
MONTHLY_CREDITS = 20000

# Our prop key -> the book's market key. Every market listed here costs one
# unit of quota per game, so the list is the popular markets only.
MARKETS = {
    'mlb': {'hits': 'batter_hits', 'tb': 'batter_total_bases', 'hr': 'batter_home_runs',
            'rbi': 'batter_rbis', 'runs': 'batter_runs_scored', 'sb': 'batter_stolen_bases',
            'hits2': 'batter_hits', 'k': 'pitcher_strikeouts', 'outs': 'pitcher_outs',
            'er': 'pitcher_earned_runs', 'p_hits': 'pitcher_hits_allowed'},
    'nba': {'pts': 'player_points', 'reb': 'player_rebounds', 'ast': 'player_assists',
            'fg3': 'player_threes', 'pra': 'player_points_rebounds_assists',
            'pr': 'player_points_rebounds', 'pa': 'player_points_assists',
            'stlblk': 'player_blocks_steals'},
    'nfl': {'pass_yds': 'player_pass_yds', 'pass_td': 'player_pass_tds',
            'pass_cmp': 'player_pass_completions', 'pass_att': 'player_pass_attempts',
            'pass_int': 'player_pass_interceptions', 'qb_rush': 'player_rush_yds',
            'rush_yds': 'player_rush_yds', 'rush_att': 'player_rush_attempts',
            'scrim': 'player_rush_reception_yds', 'rec': 'player_receptions',
            'rec_yds': 'player_reception_yds', 'anytd': 'player_anytime_td'},
    'nhl': {'sog': 'player_shots_on_goal', 'points': 'player_points', 'points2': 'player_points',
            'goal': 'player_goal_scorer_anytime', 'assists': 'player_assists',
            'blocks': 'player_blocked_shots', 'saves': 'player_total_saves'},
}


def api_key():
    return os.environ.get('ODDS_API_KEY', '').strip()


def _name_key(name):
    """'Jr.'-and-accent tolerant player-name key: Agustín Ramírez and
    Agustin Ramirez are the same man."""
    n = unicodedata.normalize('NFKD', name or '')
    n = ''.join(ch for ch in n if not unicodedata.combining(ch)).lower()
    n = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?', '', n)
    return re.sub(r'[^a-z0-9]', '', n)


class OddsClient:
    """Thin, quota-aware wrapper. ``remaining`` tracks the header the API
    returns so a run never burns the month's allowance."""

    def __init__(self, http=None, key=None):
        self.http = http or Http(timeout=15, retries=1, pause=0.2)
        self.key = key if key is not None else api_key()
        self.remaining = None
        self.used = 0

    def enabled(self):
        return bool(self.key)

    def get(self, path, **params):
        if not self.enabled():
            return None
        if self.remaining is not None and self.remaining <= MIN_REMAINING:
            return None
        params['apiKey'] = self.key
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        url = f'{API}{path}?{query}'
        data = self.http.get_json(url, cache=False)
        self.used += 1
        # Never let the key leak into the run log through the error table.
        if url in self.http.errors:
            self.http.errors[url.replace(self.key, 'REDACTED')] = self.http.errors.pop(url)
        remaining = num((self.http.last_headers or {}).get('x-requests-remaining'))
        if data is not None and remaining is not None:
            self.remaining = remaining
        return data

    def events(self, league_key):
        return self.get(f'/sports/{SPORT_KEYS[league_key]}/events') or []

    def event_props(self, league_key, event_id, market_keys):
        return self.get(f'/sports/{SPORT_KEYS[league_key]}/events/{event_id}/odds',
                        regions=REGION, markets=','.join(sorted(set(market_keys))),
                        oddsFormat='american')


def match_events(events, games):
    """Book event id per game id, matched on the two team names and the date."""
    out = {}
    for ev in events or []:
        home, away = ev.get('home_team') or '', ev.get('away_team') or ''
        start = parse_iso(ev.get('commence_time'))
        for g in games:
            if g['game_id'] in out:
                continue
            if not (_same_team(home, g['home']) and _same_team(away, g['away'])):
                continue
            if start and abs((start.date() - g['date']).days) > 1:
                continue
            out[g['game_id']] = ev.get('id')
            break
    return out


def _same_team(a, b):
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    sa, sb = norm(short_name(a)), norm(short_name(b))
    return bool(sa and sb and (sa == sb or na.endswith(sb) or nb.endswith(sa)))


def consensus_lines(payload, league_key):
    """{(player_key, prop_key): {'line', 'over', 'under', 'book', 'books'}}.

    Where several books post the same market the median line is used and the
    number of books recorded, so the page can say how firm the number is.
    """
    market_to_props = {}
    for prop_key, market in MARKETS.get(league_key, {}).items():
        market_to_props.setdefault(market, []).append(prop_key)
    seen = {}
    for book in (payload or {}).get('bookmakers') or []:
        title = book.get('title') or book.get('key') or ''
        for market in book.get('markets') or []:
            props = market_to_props.get(market.get('key'))
            if not props:
                continue
            by_player = {}
            for oc in market.get('outcomes') or []:
                player = _name_key(oc.get('description') or oc.get('name') or '')
                if not player:
                    continue
                side = (oc.get('name') or '').lower()
                point = num(oc.get('point'), 0.5)
                slot = by_player.setdefault(player, {}).setdefault(point, {})
                if side in ('over', 'yes'):
                    slot['over'] = num(oc.get('price'))
                elif side in ('under', 'no'):
                    slot['under'] = num(oc.get('price'))
            for player, points in by_player.items():
                line, slot = main_line(points)
                if line is None:
                    continue
                slot = dict(slot, line=line)
                for prop_key in props:
                    entry = seen.setdefault((player, prop_key), {'lines': [], 'books': [], 'over': [], 'under': []})
                    entry['lines'].append(slot['line'])
                    entry['books'].append(title)
                    if slot.get('over') is not None:
                        entry['over'].append(slot['over'])
                    if slot.get('under') is not None:
                        entry['under'].append(slot['under'])
    out = {}
    for key, e in seen.items():
        lines = sorted(e['lines'])
        med = lines[len(lines) // 2]
        out[key] = {'line': med, 'books': len(e['books']),
                    'book': e['books'][0] if len(e['books']) == 1 else f'{len(e["books"])} books',
                    'over': _median(e['over']), 'under': _median(e['under'])}
    return out


def main_line(points):
    """Pick a book's main line among the points it posts for one player.

    A market can carry alternates ("1.5 home runs at +5500") next to the main
    number. The main line is the one with both sides priced and the prices
    closest to even; a one-sided point is a long-shot alternate and is used
    only when nothing better exists.
    """
    best, best_key = None, None
    for point, slot in points.items():
        over, under = slot.get('over'), slot.get('under')
        two_sided = over is not None and under is not None
        if two_sided:
            balance = abs(_implied(over) - _implied(under))
        else:
            price = over if over is not None else under
            balance = 10.0 + (abs(price) / 1000.0 if price is not None else 5.0)
        key = (0 if two_sided else 1, balance)
        if best_key is None or key < best_key:
            best, best_key = point, key
    return best, (points.get(best) or {})


def _implied(price):
    """American odds to implied probability (with the vig still in)."""
    if price is None:
        return 0.5
    return 100.0 / (price + 100.0) if price > 0 else -price / (-price + 100.0)


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


class Budget:
    """Credit pacing shared by every league, persisted next to the caches.

    ``remaining`` is what the API's own header last reported; until the first
    call of a month it is assumed to be the plan's allowance. Today's spend is
    kept so several runs in one day share one daily allowance.
    """

    def __init__(self, cache_dir, now=None):
        self.path = os.path.join(cache_dir, 'odds_budget.json')
        self.now = now or datetime.now(timezone.utc)
        self.monthly = num(os.environ.get('ODDS_MONTHLY_CREDITS'), MONTHLY_CREDITS) or MONTHLY_CREDITS
        state = read_json(self.path, {}) or {}
        month, day = self.now.strftime('%Y-%m'), self.now.strftime('%Y-%m-%d')
        if state.get('month') != month:
            state = {'month': month, 'spent_month': 0, 'remaining': None}
        if state.get('day') != day:
            state['day'], state['spent_today'] = day, 0
        self.state = state

    @property
    def remaining(self):
        r = self.state.get('remaining')
        return self.monthly - self.state.get('spent_month', 0) if r is None else r

    def daily_allowance(self):
        days_in_month = calendar.monthrange(self.now.year, self.now.month)[1]
        days_left = days_in_month - self.now.day + 1
        return max(0.0, (self.remaining - MIN_REMAINING) / days_left)

    def can_spend(self, cost, first_of_day_ok=True):
        spent = self.state.get('spent_today', 0)
        if spent == 0 and first_of_day_ok and self.remaining - cost > MIN_REMAINING:
            return True                  # never let a day go entirely blank
        return spent + cost <= self.daily_allowance()

    def spend(self, cost, remaining=None):
        self.state['spent_today'] = self.state.get('spent_today', 0) + cost
        self.state['spent_month'] = self.state.get('spent_month', 0) + cost
        if remaining is not None:
            self.state['remaining'] = remaining

    def save(self):
        self.state['updated'] = now_iso()
        write_json(self.path, self.state, indent=None)


def load_lines(league_key, games, http=None, cache_dir=None):
    """Sportsbook lines for the games starting soon: {game_id: consensus}.

    Cached per game; a game is fetched once, when it is within AHEAD_HOURS of
    kickoff, so the monthly quota goes as far as it can. Returns ``({}, status)``
    with a status of 'disabled', 'cached', 'live' or 'exhausted'.
    """
    cache_dir = cache_dir or config.DATA_DIR
    path = os.path.join(cache_dir, f'{league_key}_lines.json')
    cache = read_json(path, {}) or {}
    client = OddsClient(http)
    if not client.enabled():
        return {gid: v['lines'] for gid, v in cache.items() if v.get('lines')}, 'disabled'

    now = datetime.now(timezone.utc)
    refresh = timedelta(hours=num(os.environ.get('ODDS_REFRESH_HOURS'), REFRESH_HOURS))
    budget = Budget(cache_dir, now)
    fresh, stale = [], []
    for g in games:
        if g['final']:
            continue
        start = parse_iso((g.get('row') or {}).get('game_start_utc'))
        if start is None:
            start = datetime.combine(g['date'], datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23)
        if start < now or start - now > timedelta(hours=AHEAD_HOURS):
            continue
        fetched = parse_iso((cache.get(g['game_id']) or {}).get('fetched'))
        if fetched is None:
            fresh.append((start, g))
        elif now - fetched >= refresh:
            stale.append((fetched, g))
    # A game never priced comes before refreshing one that is; soonest first.
    soon = [g for _, g in sorted(fresh, key=lambda x: x[0])] + \
           [g for _, g in sorted(stale, key=lambda x: x[0])]
    status = 'cached'
    if soon:
        events = client.events(league_key)
        matched = match_events(events, soon)
        markets = list(set(MARKETS.get(league_key, {}).values()))
        cost = len(markets)
        for g in soon:
            ev_id = matched.get(g['game_id'])
            if not ev_id:
                continue
            if not budget.can_spend(cost):
                status = 'budgeted'          # the rest waits for tomorrow's allowance
                break
            payload = client.event_props(league_key, ev_id, markets)
            if payload is None:
                status = 'exhausted' if client.remaining is not None and client.remaining <= MIN_REMAINING else status
                continue
            budget.spend(cost, client.remaining)
            lines = consensus_lines(payload, league_key)
            if not lines and (cache.get(g['game_id']) or {}).get('lines'):
                continue                     # a blank answer never erases lines we have
            cache[g['game_id']] = {'fetched': now_iso(), 'date': str(g['date']),
                                   'event': ev_id, 'lines': {f'{p}|{k}': v for (p, k), v in lines.items()}}
            status = 'live'
        # Keep the cache to recent games.
        cutoff = str((now - timedelta(days=5)).date())
        cache = {gid: v for gid, v in cache.items() if (v.get('date') or '') >= cutoff}
        write_json(path, cache, indent=None)
        budget.save()
    out = {}
    for gid, v in cache.items():
        lines = {}
        for k, val in (v.get('lines') or {}).items():
            player, prop = k.split('|', 1)
            lines[(player, prop)] = val
        out[gid] = lines
    return out, status


def line_for(lines, player_name, prop_key):
    if not lines:
        return None
    return lines.get((_name_key(player_name), prop_key))
