"""ESPN response shapes, reconstructed from the field names the R scripts read.

These are the only ground truth available offline, so every parser is tested
against them. A FakeHttp routes URLs to fixtures so ingest.run can execute
end to end without a network.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from sportspred.util import Http


def team(tid, name, abbr):
    return {'id': str(tid), 'displayName': name, 'name': name.split()[-1],
            'abbreviation': abbr}


def _stat(name, value, abbr=None):
    s = {'name': name, 'value': value, 'displayValue': str(value)}
    if abbr:
        s['abbreviation'] = abbr
    return s


MLB_TEAMS = [
    (1, 'Boston Red Sox', 'BOS', 90, 60, 720, 600),
    (2, 'New York Yankees', 'NYY', 85, 65, 700, 640),
    (3, 'Tampa Bay Rays', 'TB', 70, 80, 600, 660),
    (4, 'Baltimore Orioles', 'BAL', 60, 90, 560, 720),
]


def mlb_standings():
    """Nested groups (league -> division), entries under ``standings.entries``."""
    entries = []
    for tid, name, abbr, w, l, rs, ra in MLB_TEAMS:
        entries.append({
            'team': team(tid, name, abbr),
            'stats': [_stat('wins', w, 'W'), _stat('losses', l, 'L'),
                      _stat('pointsFor', rs, 'RS'), _stat('pointsAgainst', ra, 'RA'),
                      _stat('winPercent', round(w / (w + l), 3), 'PCT')],
        })
    return {'children': [{'name': 'American League',
                          'children': [{'name': 'East',
                                        'standings': {'entries': entries}}]}]}


def mlb_team_statistics(era):
    return {'splits': {'categories': [
        {'name': 'batting', 'stats': [_stat('avg', .261)]},
        {'name': 'pitching', 'stats': [_stat('strikeouts', 1200),
                                       _stat('earnedRunAverage', era, 'ERA')]},
    ]}}


def nfl_standings():
    entries = []
    for tid, name, abbr, w, l, t, pf, pa in [
            (10, 'Kansas City Chiefs', 'KC', 2, 0, 0, 60, 30),
            (11, 'Denver Broncos', 'DEN', 1, 1, 0, 40, 41),
            (12, 'Las Vegas Raiders', 'LV', 0, 2, 0, 20, 55)]:
        entries.append({'team': team(tid, name, abbr), 'stats': [
            _stat('wins', w, 'W'), _stat('losses', l, 'L'), _stat('ties', t, 'T'),
            _stat('pointsFor', pf, 'PF'), _stat('pointsAgainst', pa, 'PA')]})
    return {'children': [{'name': 'AFC', 'standings': {'entries': entries}}]}


def nfl_team_statistics(ypg, give, take):
    return {'splits': {'categories': [
        {'name': 'passing', 'stats': [_stat('netPassingYardsPerGame', 240)]},
        {'name': 'general', 'stats': [_stat('totalYardsPerGame', ypg),
                                      _stat('totalGiveaways', give),
                                      _stat('totalTakeaways', take)]},
    ]}}


def nhl_standings():
    entries = []
    for tid, name, abbr, w, l, otl, gf, ga in [
            (20, 'Toronto Maple Leafs', 'TOR', 30, 15, 5, 170, 140),
            (21, 'Montreal Canadiens', 'MTL', 20, 25, 5, 140, 165)]:
        entries.append({'team': team(tid, name, abbr), 'stats': [
            _stat('wins', w, 'W'), _stat('losses', l, 'L'), _stat('otLosses', otl, 'OTL'),
            _stat('points', w * 2 + otl, 'PTS'),
            _stat('pointsFor', gf, 'GF'), _stat('pointsAgainst', ga, 'GA')]})
    return {'children': [{'name': 'Eastern', 'standings': {'entries': entries}}]}


def nba_standings():
    entries = []
    for tid, name, abbr, w, l, ppg, opp in [
            (30, 'Boston Celtics', 'BOS', 40, 15, 118.0, 108.0),
            (31, 'Miami Heat', 'MIA', 25, 30, 110.0, 112.0)]:
        entries.append({'team': team(tid, name, abbr), 'stats': [
            _stat('wins', w, 'W'), _stat('losses', l, 'L'),
            _stat('avgPointsFor', ppg, 'PPG'), _stat('avgPointsAgainst', opp, 'OPPG')]})
    return {'children': [{'name': 'Eastern', 'standings': {'entries': entries}}]}


def event(eid, d, away, home, away_score=None, home_score=None, final=False,
          season_type=2, probables=None, postponed=False, iso_time='23:10Z'):
    status_name = ('STATUS_POSTPONED' if postponed else
                   'STATUS_FINAL' if final else 'STATUS_SCHEDULED')
    comps = []
    for side, t, score in (('away', away, away_score), ('home', home, home_score)):
        c = {'homeAway': side, 'team': t,
             'score': str(score if score is not None else 0),
             'records': [{'summary': '10-5', 'type': 'total'}]}
        if probables and side in probables:
            c['probables'] = [{'athlete': {'id': probables[side][0],
                                           'displayName': probables[side][1]}}]
        comps.append(c)
    return {
        'id': str(eid), 'date': f'{d}T{iso_time}',
        'season': {'year': d.year, 'type': season_type,
                   'slug': {1: 'preseason', 2: 'regular-season', 3: 'post-season'}[season_type]},
        'competitions': [{'competitors': comps,
                          'status': {'type': {'name': status_name, 'completed': final}}}],
    }


class FakeHttp(Http):
    """Serve fixtures by URL substring; record every URL requested."""

    def __init__(self, routes):
        super().__init__()
        self.routes = routes          # list of (substring, payload-or-callable)
        self.requests = []

    def get_json(self, url, cache=True):
        self.requests.append(url)
        for needle, payload in self.routes:
            if needle in url:
                return payload(url) if callable(payload) else json.loads(json.dumps(payload))
        return None


def mlb_routes(today=None, n_past=20, n_future=3):
    """A complete offline MLB feed: standings, statistics, a schedule window."""
    today = today or date.today()
    teams = {tid: team(tid, name, abbr) for tid, name, abbr, *_ in MLB_TEAMS}
    eras = {1: 3.40, 2: 3.90, 3: 4.30, 4: 4.90}

    def scoreboard(url):
        stamp = url.rsplit('dates=', 1)[-1][:8]
        d = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
        offset = (d - today).days
        if offset < -n_past or offset > n_future:
            return {'events': []}
        pairs = [(1, 2), (3, 4)] if d.day % 2 else [(1, 3), (2, 4)]
        events = []
        for i, (a, h) in enumerate(pairs):
            eid = int(f'{d:%Y%m%d}{i}')
            final = offset < 0
            # The stronger side (lower id) wins most home/away games.
            hs, as_ = (5, 2) if h < a else (3, 6)
            events.append(event(eid, d, teams[a], teams[h],
                                as_ if final else None, hs if final else None, final,
                                probables=None if final else
                                {'away': ('9001', 'Ace Pitcher'), 'home': ('9002', 'Home Starter')}))
        return {'events': events}

    routes = [('baseball/mlb/standings', mlb_standings()),
              ('baseball/mlb/scoreboard', scoreboard)]
    for tid, era in eras.items():
        routes.append((f'/teams/{tid}/statistics', mlb_team_statistics(era)))
    return routes
