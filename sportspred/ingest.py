"""ESPN ingestion — the Python replacement for the four R scripts.

Pulls standings, per-team statistics and the schedule window for a league and
writes the same enriched CSV the R scripts produced, so the archive, ledger and
model layers read exactly what they always have. Each league is a small table of
what to read and how to weight it; the mechanics are shared.

Two behaviours differ from the R version on purpose:

* a run that finds no games, or cannot reach ESPN at all, leaves the previous
  CSV in place rather than overwriting it with an empty file;
* kickoff times are written both as a raw UTC timestamp and correctly
  localised to Eastern (the R output formatted UTC and labelled it ET).

Everything else — field names, fallback chains, defaults, the strength
formulas and the season-statistics refit — is carried over line for line.
"""
from __future__ import annotations

import math
import os
from datetime import timedelta

from . import config
from .glm import LogisticModel
from .util import (Http, dig, format_eastern, logistic, now_iso, num, parse_date,
                   read_csv, today_utc, write_csv)

SITE_API = 'https://site.api.espn.com/apis/site/v2/sports'
STANDINGS_API = 'https://site.api.espn.com/apis/v2/sports'
# The same document is served from more than one host; try each before
# concluding the standings are unavailable.
STANDINGS_URLS = (
    'https://site.api.espn.com/apis/v2/sports/{path}/standings',
    'https://site.web.api.espn.com/apis/v2/sports/{path}/standings?region=us&lang=en',
    'https://site.api.espn.com/apis/v2/sports/{path}/standings?level=3',
)

MIN_REFIT_GAMES = 30      # the R scripts refit their standings model past this


_LOG = [print]


def log_line(msg):
    _LOG[0](msg)


# ─────────────────────────────────────────────────────────────────────────────
#  Small helpers that mirror the R idioms
# ─────────────────────────────────────────────────────────────────────────────
def _first(sv, *keys, default=None):
    """R's ``sv[["a"]] %||% sv[["b"]] %||% default`` — first present value."""
    for k in keys:
        v = sv.get(k)
        if v is not None and v != '':
            n = num(v)
            if n is not None:
                return n
    return default


def _clip01(x):
    return min(max(x, 0.0), 1.0)


def _stat_map(entry):
    """Standings stats keyed by both ``name`` and ``abbreviation``."""
    sv = {}
    for s in entry.get('stats') or []:
        name = s.get('name') or ''
        value = s.get('value')
        if value is None:
            value = s.get('displayValue')
        sv[name] = value
        if s.get('abbreviation'):
            sv[s['abbreviation']] = value
    return sv


# ─────────────────────────────────────────────────────────────────────────────
#  Per-league standings parsers and strength formulas
# ─────────────────────────────────────────────────────────────────────────────
def _mlb_team(sv):
    w = _first(sv, 'wins', 'W', default=0.0)
    l = _first(sv, 'losses', 'L', default=0.0)
    gp = w + l
    win_pct = w / gp if gp > 0 else 0.5
    rs = _first(sv, 'Runs Scored', 'RS', 'runsScored', 'pointsFor', default=0.0)
    ra = _first(sv, 'Runs Against', 'RA', 'runsAllowed', 'pointsAgainst', default=0.0)
    rs_pg = rs / gp if gp > 0 else 4.5
    ra_pg = ra / gp if gp > 0 else 4.5
    run_diff_pg = rs_pg - ra_pg
    pyth = (rs ** 1.83 / (rs ** 1.83 + ra ** 1.83)) if (rs + ra) > 0 else 0.5
    strength = (0.40 * pyth + 0.20 * win_pct
                + 0.40 * _clip01((run_diff_pg + 3) / 6))
    return {
        'record': f'{w:.0f}-{l:.0f}', 'gp': gp, 'strength': strength,
        'win_pct': round(win_pct, 3), 'pyth_pct': round(pyth, 3),
        'run_diff_pg': round(run_diff_pg, 2), 'rs_pg': round(rs_pg, 2),
        'ra_pg': round(ra_pg, 2), 'era': None,
    }


def _mlb_apply_era(info, era):
    """Once ERA is known the MLB strength blends it in (R step 2)."""
    info['era'] = round(era, 2)
    info['strength'] = (0.35 * info['pyth_pct'] + 0.20 * info['win_pct']
                        + 0.25 * _clip01((info['run_diff_pg'] + 3) / 6)
                        + 0.20 * _clip01((6.0 - era) / 4.0))


def _nhl_team(sv):
    w = _first(sv, 'wins', 'W', default=0.0)
    l = _first(sv, 'losses', 'L', default=0.0)
    otl = _first(sv, 'otl', 'OTL', 'Overtime Losses', 'otLosses', default=0.0)
    gp = w + l + otl
    pts = _first(sv, 'points', 'PTS', default=w * 2 + otl)
    pts_pct = pts / max(gp * 2, 1)
    gf = _first(sv, 'Goals For', 'GF', 'pointsFor', default=0.0)
    ga = _first(sv, 'Goals Against', 'GA', 'pointsAgainst', default=0.0)
    gf_pg = gf / gp if gp > 0 else 3.0
    ga_pg = ga / gp if gp > 0 else 3.0
    pp_pct = _first(sv, 'Power Play %', 'PP%', 'powerPlayPct', default=20.0)
    pk_pct = _first(sv, 'Penalty Kill %', 'PK%', 'penaltyKillPct', default=80.0)
    save_pct = _first(sv, 'Save %', 'SV%', 'savePct', default=0.910)
    strength = (0.35 * pts_pct + 0.20 * min(gf_pg / 4.5, 1) + 0.20 * (1 - min(ga_pg / 4.5, 1))
                + 0.125 * min(pp_pct / 30, 1) + 0.125 * min(pk_pct / 100, 1))
    record = f'{w:.0f}-{l:.0f}-{otl:.0f}' if otl > 0 else f'{w:.0f}-{l:.0f}'
    return {
        'record': record, 'gp': gp, 'strength': strength,
        'pts_pct': round(pts_pct * 100, 1), 'gf_pg': round(gf_pg, 2),
        'ga_pg': round(ga_pg, 2), 'pp_pct': round(pp_pct, 1),
        'pk_pct': round(pk_pct, 1), 'save_pct': round(save_pct, 3),
    }


def _nba_team(sv):
    w = _first(sv, 'wins', 'W', default=0.0)
    l = _first(sv, 'losses', 'L', default=0.0)
    gp = w + l
    win_pct = w / gp if gp > 0 else 0.5
    ppg = _first(sv, 'Points For/Game', 'PPG', 'avgPointsFor', default=110.0)
    opp_ppg = _first(sv, 'Points Against/Game', 'OPPG', 'avgPointsAgainst', default=110.0)
    # Some seasons the feed only carries totals; derive the averages.
    if ppg == 110.0 and gp > 0:
        pf = _first(sv, 'pointsFor', 'PF', default=None)
        if pf:
            ppg = pf / gp
    if opp_ppg == 110.0 and gp > 0:
        pa = _first(sv, 'pointsAgainst', 'PA', default=None)
        if pa:
            opp_ppg = pa / gp
    net_rtg = ppg - opp_ppg
    pace = _first(sv, 'pace', 'Pace', default=100.0)
    strength = (0.40 * _clip01((net_rtg + 15) / 30) + 0.25 * win_pct
                + 0.175 * min(ppg / 130, 1) + 0.175 * (1 - min(opp_ppg / 130, 1)))
    return {
        'record': f'{w:.0f}-{l:.0f}', 'gp': gp, 'strength': strength,
        'win_pct': round(win_pct, 3), 'ppg': round(ppg, 1),
        'opp_ppg': round(opp_ppg, 1), 'net_rtg': round(net_rtg, 1),
        'pace': round(pace, 1),
    }


def _nfl_team(sv):
    w = _first(sv, 'wins', 'W', default=0.0)
    l = _first(sv, 'losses', 'L', default=0.0)
    t = _first(sv, 'ties', 'T', default=0.0)
    gp = w + l + t
    win_pct = (w + 0.5 * t) / gp if gp > 0 else 0.5
    pf = _first(sv, 'Points For', 'PF', 'pointsFor', default=0.0)
    pa = _first(sv, 'Points Against', 'PA', 'pointsAgainst', default=0.0)
    pt_diff_pg = (pf - pa) / gp if gp > 0 else 0.0
    ypg_off = _first(sv, 'Total Yards/Game', 'Offensive Yards/Game', 'avgYardsPerGame',
                     default=340.0)
    ypg_def = _first(sv, 'Opponent Yards/Game', 'Defensive Yards/Game', 'avgOppYardsPerGame',
                     default=340.0)
    to_for = _first(sv, 'Takeaways', 'TO', default=0.0)
    to_against = _first(sv, 'Giveaways', 'TFL', default=0.0)
    to_margin = (to_for - to_against) / gp if gp > 0 else 0.0
    record = f'{w:.0f}-{l:.0f}-{t:.0f}' if t > 0 else f'{w:.0f}-{l:.0f}'
    info = {
        'record': record, 'gp': gp,
        'win_pct': round(win_pct, 3), 'pt_diff_pg': round(pt_diff_pg, 1),
        'ypg_off': round(ypg_off, 1), 'ypg_def': round(ypg_def, 1),
        'to_margin': round(to_margin, 2),
    }
    info['strength'] = _nfl_strength(info)
    return info


def _nfl_strength(info):
    return (0.35 * _clip01((info['pt_diff_pg'] + 20) / 40) + 0.20 * info['win_pct']
            + 0.15 * min(info['ypg_off'] / 450, 1)
            + 0.15 * (1 - min(info['ypg_def'] / 450, 1))
            + 0.15 * _clip01((info['to_margin'] + 2) / 4))


LEAGUE_INGEST = {
    'mlb': {
        'parse': _mlb_team, 'home_adv': 0.10, 'scale': 5.0,
        'lookback': 60, 'forward': 14,
        'stat_cols': ['win_pct', 'pyth_pct', 'run_diff_pg', 'rs_pg', 'ra_pg', 'era'],
        'refit': ('pyth_pct', 'win_pct', 'run_diff_pg', 'rs_pg'),
        'refit_lower_better': ('era',),
        'probables': True,
    },
    'nhl': {
        'parse': _nhl_team, 'home_adv': 0.30, 'scale': 5.0,
        'lookback': 60, 'forward': 14,
        'stat_cols': ['pts_pct', 'gf_pg', 'ga_pg', 'pp_pct', 'pk_pct', 'save_pct'],
        'refit': ('pts_pct', 'gf_pg', 'pp_pct', 'pk_pct'),
        'refit_lower_better': ('ga_pg',),
        'probables': False,
    },
    'nba': {
        'parse': _nba_team, 'home_adv': 0.28, 'scale': 5.5,
        'lookback': 60, 'forward': 14,
        'stat_cols': ['win_pct', 'ppg', 'opp_ppg', 'net_rtg', 'pace'],
        'refit': ('win_pct', 'net_rtg', 'ppg'),
        'refit_lower_better': (),
        'probables': False,
        'rest_term': 0.06,
    },
    'nfl': {
        'parse': _nfl_team, 'home_adv': 0.40, 'scale': 4.5,
        'lookback': 180, 'forward': 30,
        'stat_cols': ['win_pct', 'pt_diff_pg', 'ypg_off', 'ypg_def', 'to_margin'],
        'refit': ('win_pct', 'pt_diff_pg', 'ypg_off', 'to_margin'),
        'refit_lower_better': ('ypg_def',),
        'probables': False,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
#  Standings
# ─────────────────────────────────────────────────────────────────────────────
def fetch_standings(http, league_key):
    """Team stats keyed three ways (id, lowercase name, lowercase abbreviation),
    exactly as the R scripts indexed them."""
    cfg = config.LEAGUES[league_key]
    parse = LEAGUE_INGEST[league_key]['parse']
    data, tried = None, []
    for template in STANDINGS_URLS:
        url = template.format(path=cfg['espn_path'])
        data = http.get_json(url)
        if data and (data.get('children') or data.get('standings') or data.get('entries')):
            break
        tried.append(f'{url} -> {http.why(url) or ("empty/unexpected shape: " + str(list((data or {}).keys())[:6]))}')
        data = None
    if not data:
        for line in tried:
            log_line(f'    standings attempt: {line}')
        return {}

    teams = {}

    def parse_entries(entries):
        for entry in entries or []:
            team = entry.get('team') or {}
            if not team:
                continue
            tid = str(team.get('id') or '')
            name = str(team.get('displayName') or team.get('name') or '')
            abbr = str(team.get('abbreviation') or '')
            info = parse(_stat_map(entry))
            info.update({'id': tid, 'name': name, 'abbr': abbr})
            if tid:
                teams[tid] = info
            if name:
                teams[name.lower()] = info
            if abbr:
                teams[abbr.lower()] = info

    def walk(node):
        if not isinstance(node, dict):
            return
        if dig(node, 'standings', 'entries'):
            parse_entries(node['standings']['entries'])
        if node.get('entries'):
            parse_entries(node['entries'])
        for child in node.get('children') or []:
            walk(child)

    walk(data)
    return teams


def unique_teams(teams):
    seen, out = set(), []
    for info in teams.values():
        if info.get('id') and info['id'] not in seen:
            seen.add(info['id'])
            out.append(info)
    return out


def lookup(teams, name, tid=None):
    """R's ``lookup()``: id, then full name, then any single word of it."""
    if tid and str(tid) in teams:
        return teams[str(tid)]
    if name:
        k = str(name).lower()
        if k in teams:
            return teams[k]
        for w in k.split(' '):
            if w in teams:
                return teams[w]
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Per-team statistics (ERA for MLB, yardage and turnovers for NFL)
# ─────────────────────────────────────────────────────────────────────────────
def _pick_stat(data, wanted, categories=None):
    """Search ``splits.categories[].stats[]`` for the first of ``wanted``."""
    for cat in dig(data, 'splits', 'categories') or []:
        cname = (cat.get('name') or '').lower()
        if categories and cname not in categories:
            continue
        for st in cat.get('stats') or []:
            nm = (st.get('name') or '').lower()
            ab = (st.get('abbreviation') or '').lower()
            for w in wanted:
                if nm == w or ab == w:
                    v = num(st.get('value'))
                    if v is None:
                        v = num(st.get('displayValue'))
                    if v is not None:
                        return v
    return None


def enrich_team_statistics(http, league_key, teams):
    """Fill in what the standings payload does not carry."""
    cfg = config.LEAGUES[league_key]
    hits = 0
    for info in unique_teams(teams):
        data = http.get_json(f'{SITE_API}/{cfg["espn_path"]}/teams/{info["id"]}/statistics')
        if not data:
            continue
        got = False
        if league_key == 'mlb':
            era = _pick_stat(data, ('earnedrunaverage', 'era'), ('pitching', 'pitchingstats'))
            if era is not None:
                _mlb_apply_era(info, era)
                got = True
        elif league_key == 'nfl':
            yds = _pick_stat(data, ('totalyardspergame', 'yardspergame',
                                    'netyardspergame', 'totalyards'))
            give = _pick_stat(data, ('totalgiveaways', 'giveaways', 'turnovers'))
            take = _pick_stat(data, ('totaltakeaways', 'takeaways'))
            gp = info.get('gp') or 0
            if yds is not None:
                if yds > 1000 and gp > 0:          # a season total, not an average
                    yds = yds / gp
                info['ypg_off'] = round(yds, 1)
                got = True
            opp_yds = _pick_stat(data, ('opponentyardspergame', 'yardsallowedpergame',
                                        'totalyardsallowed'))
            if opp_yds is not None:
                if opp_yds > 1000 and gp > 0:
                    opp_yds = opp_yds / gp
                info['ypg_def'] = round(opp_yds, 1)
                got = True
            if give is not None and take is not None and gp > 0:
                info['to_margin'] = round((take - give) / gp, 2)
                got = True
            if got:
                info['strength'] = _nfl_strength(info)
        elif league_key == 'nba':
            pace = _pick_stat(data, ('pace',))
            if pace is not None:
                info['pace'] = round(pace, 1)
                got = True
        elif league_key == 'nhl':
            sv = _pick_stat(data, ('savepct', 'savepercentage', 'sv%'))
            if sv is not None:
                if sv > 1.0:
                    sv = sv / 100.0
                info['save_pct'] = round(sv, 3)
                got = True
        hits += int(got)
    return hits


# ─────────────────────────────────────────────────────────────────────────────
#  Schedule window
# ─────────────────────────────────────────────────────────────────────────────
def _side(comp, which):
    for c in comp.get('competitors') or []:
        if c.get('homeAway') == which:
            return c
    return {}


def _probable(c):
    p = dig(c, 'probables', 0) or {}
    ath = p.get('athlete') or {}
    return {'id': str(ath.get('id') or ''), 'name': ath.get('displayName') or ''}


def parse_event(ev, league_key, teams, last_game_date, query_date=None):
    """One scoreboard event -> one CSV row, in the R column order.

    ``game_date`` is the calendar date the scoreboard was queried for, not the
    UTC date of first pitch: a 10pm Pacific start is past midnight UTC, and
    filing it under tomorrow would move it off today's board and distort rest
    days. ``game_start_utc`` keeps the exact instant.
    """
    spec = LEAGUE_INGEST[league_key]
    comp = dig(ev, 'competitions', 0) or {}
    stype = dig(comp, 'status', 'type', 'name') or ''
    if stype in ('STATUS_POSTPONED', 'STATUS_CANCELED'):
        return None
    is_final = stype == 'STATUS_FINAL' or bool(dig(comp, 'status', 'type', 'completed'))

    away_c, home_c = _side(comp, 'away'), _side(comp, 'home')
    away_name = str(dig(away_c, 'team', 'displayName') or '')
    home_name = str(dig(home_c, 'team', 'displayName') or '')
    away_id = str(dig(away_c, 'team', 'id') or '')
    home_id = str(dig(home_c, 'team', 'id') or '')
    away_score = str(away_c.get('score') if away_c.get('score') is not None else '')
    home_score = str(home_c.get('score') if home_c.get('score') is not None else '')
    away_rec = str(dig(away_c, 'records', 0, 'summary') or '')
    home_rec = str(dig(home_c, 'records', 0, 'summary') or '')

    date_str = query_date or str(ev.get('date') or '')[:10]
    game_date = parse_date(date_str)
    if not game_date or not away_name or not home_name:
        return None

    at = lookup(teams, away_name, away_id)
    ht = lookup(teams, home_name, home_id)
    away_str = at['strength'] if at else 0.5
    home_str = ht['strength'] if ht else 0.5

    rest_adv = 0
    if spec.get('rest_term'):
        away_last = last_game_date.get(away_name) or (game_date - timedelta(days=3))
        home_last = last_game_date.get(home_name) or (game_date - timedelta(days=3))
        rest_adv = min((game_date - home_last).days, 5) - min((game_date - away_last).days, 5)

    x = spec['scale'] * (home_str - away_str) + spec['home_adv']
    if spec.get('rest_term'):
        x += spec['rest_term'] * rest_adv
    home_p = logistic(x)

    winner = ''
    if is_final and away_score and home_score:
        aw, hw = num(away_score), num(home_score)
        if aw is not None and hw is not None:
            winner = away_name if aw > hw else home_name
    if is_final:
        last_game_date[away_name] = game_date
        last_game_date[home_name] = game_date

    row = {
        'game_date': date_str,
        'away_team': away_name, 'home_team': home_name,
        'status': 'Final' if is_final else 'Scheduled',
        'winner': winner,
        'favored_team': home_name if home_p >= 0.5 else away_name,
        'away_win_probability': round(1 - home_p, 4),
        'home_win_probability': round(home_p, 4),
        'game_id': str(ev.get('id') or ''),
        'away_score': away_score, 'home_score': home_score,
        'away_record': away_rec or (at['record'] if at else ''),
        'home_record': home_rec or (ht['record'] if ht else ''),
    }
    for col in spec['stat_cols']:
        row[f'away_{col}'] = at.get(col) if at else None
        row[f'home_{col}'] = ht.get(col) if ht else None
    if spec.get('probables'):
        ap, hp = _probable(away_c), _probable(home_c)
        row['away_probable_starter'] = ap['name']
        row['home_probable_starter'] = hp['name']
        row['away_probable_id'] = ap['id']
        row['home_probable_id'] = hp['id']
    if spec.get('rest_term'):
        row['rest_advantage'] = rest_adv
    row['season_type'] = num(dig(ev, 'season', 'type'))
    row['season_slug'] = str(dig(ev, 'season', 'slug') or '')
    row['game_start_utc'] = str(ev.get('date') or '')
    row['game_time'] = format_eastern(ev.get('date'), date_str)
    return row


def fetch_window(http, league_key, teams, lookback=None, forward=None):
    """Every game from ``lookback`` days ago through ``forward`` days ahead."""
    cfg = config.LEAGUES[league_key]
    spec = LEAGUE_INGEST[league_key]
    lookback = _env_int('SP_LOOKBACK_DAYS', lookback if lookback is not None else spec['lookback'])
    forward = _env_int('SP_FORWARD_DAYS', forward if forward is not None else spec['forward'])
    today = today_utc()
    rows, last_game_date, days_hit = [], {}, 0
    for offset in range(-lookback, forward + 1):
        d = today + timedelta(days=offset)
        data = http.get_json(f'{SITE_API}/{cfg["espn_path"]}/scoreboard?dates={d:%Y%m%d}')
        if not data:
            continue
        days_hit += 1
        for ev in data.get('events') or []:
            row = parse_event(ev, league_key, teams, last_game_date, f'{d:%Y-%m-%d}')
            if row:
                rows.append(row)
    # De-duplicate on (date, away, home) as the R scripts did.
    seen, out = set(), []
    for r in rows:
        k = (r['game_date'], r['away_team'], r['home_team'])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out, days_hit


def _env_int(name, default):
    raw = os.environ.get(name, '')
    try:
        return int(raw) if raw.strip() else int(default)
    except ValueError:
        return int(default)


# ─────────────────────────────────────────────────────────────────────────────
#  Season-statistics refit (R step "Fit Logistic Regression")
# ─────────────────────────────────────────────────────────────────────────────
def refit_prior(rows, league_key):
    """Refit the standings model on this window's completed games.

    Mirrors the R glm: home_won ~ (home - away) season-stat differences, where
    "lower is better" statistics are entered as (away - home). A near-zero
    ridge penalty keeps it effectively unpenalised, as glm() was.
    """
    spec = LEAGUE_INGEST[league_key]
    cols = list(spec['refit'])
    finals_rows = [r for r in rows if r.get('status') == 'Final' and r.get('winner')]
    # A term nobody has (the team-statistics feed returned nothing) is left
    # out rather than blocking the whole refit — the R has_era test.
    lower = [c for c in (spec.get('refit_lower_better') or ())
             if any(num(r.get(f'away_{c}')) is not None for r in finals_rows)
             and any(num(r.get(f'home_{c}')) is not None for r in finals_rows)]
    cols_rest = bool(spec.get('rest_term'))

    def vector(r):
        v = []
        for c in cols:
            h, a = num(r.get(f'home_{c}')), num(r.get(f'away_{c}'))
            if h is None or a is None:
                return None
            v.append(h - a)
        for c in lower:
            h, a = num(r.get(f'home_{c}')), num(r.get(f'away_{c}'))
            if h is None or a is None:
                return None
            v.append(a - h)
        if cols_rest:
            v.append(num(r.get('rest_advantage'), 0.0))
        return v

    X, y = [], []
    for r in rows:
        if r.get('status') != 'Final' or not r.get('winner'):
            continue
        v = vector(r)
        if v is None:
            continue
        X.append(v)
        y.append(1 if r['winner'] == r['home_team'] else 0)
    if len(X) < MIN_REFIT_GAMES:
        return 0
    if len(set(y)) < 2:
        return 0
    model = LogisticModel(cols + lower + (['rest'] if cols_rest else []), l2=0.05).fit(X, y)
    updated = 0
    for r in rows:
        v = vector(r)
        if v is None:
            continue
        p = model.predict_proba(v)
        r['home_win_probability'] = round(p, 4)
        r['away_win_probability'] = round(1 - p, 4)
        r['favored_team'] = r['home_team'] if p >= 0.5 else r['away_team']
        updated += 1
    return updated


# ─────────────────────────────────────────────────────────────────────────────
#  Team-stat snapshots — the seed of a leak-free season-statistics model
# ─────────────────────────────────────────────────────────────────────────────
def snapshot_team_stats(league_key, teams):
    """Append today's standings line for every team to a dated history.

    The enriched CSV only ever holds the *current* standings, which is why the
    old model could not be trained on them without hindsight. A daily snapshot
    means that, in time, each historical game can be paired with the standings
    as they stood that morning.
    """
    path = os.path.join(config.HISTORY_DIR, f'{league_key}_team_stats.csv')
    existing = read_csv(path)
    today = str(today_utc())
    keyed = {(r.get('date'), r.get('team')): r for r in existing}
    cols = LEAGUE_INGEST[league_key]['stat_cols']
    for info in unique_teams(teams):
        row = {'date': today, 'team': info['name'], 'record': info.get('record', ''),
               'gp': info.get('gp', 0), 'strength': round(info.get('strength', 0.5), 4)}
        for c in cols:
            row[c] = info.get(c)
        keyed[(today, info['name'])] = row
    rows = sorted(keyed.values(), key=lambda r: (r.get('date', ''), r.get('team', '')))
    fields = ['date', 'team', 'record', 'gp', 'strength'] + cols
    if rows:
        write_csv(path, rows, fields)
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
def run(league_key, http=None, write=True, log=print):
    """Refresh one league's enriched CSV from ESPN.

    Returns the rows written, or ``None`` when nothing usable came back — in
    which case the previous CSV is left untouched so the site keeps building.
    """
    cfg = config.LEAGUES[league_key]
    http = http or Http(timeout=15, retries=2, pause=0.15)
    csv_path = os.path.join(config.BASE, cfg['csv_file'])
    _LOG[0] = log

    log(f'  [{cfg["name"]}] standings…')
    teams = fetch_standings(http, league_key)
    n_teams = len(unique_teams(teams))
    log(f'  [{cfg["name"]}] {n_teams} teams')
    if n_teams == 0:
        log(f'  [{cfg["name"]}] standings unavailable; keeping the previous CSV')
        return None

    hits = enrich_team_statistics(http, league_key, teams)
    log(f'  [{cfg["name"]}] team statistics for {hits}/{n_teams} teams')

    rows, days = fetch_window(http, league_key, teams)
    log(f'  [{cfg["name"]}] {len(rows)} games over {days} days with data')
    if not rows:
        log(f'  [{cfg["name"]}] no games in window; keeping the previous CSV')
        return None

    refit = refit_prior(rows, league_key)
    finals = sum(1 for r in rows if r['status'] == 'Final')
    log(f'  [{cfg["name"]}] {finals} completed · standings model '
        + (f'refit on {finals}, applied to {refit}' if refit else 'formula only'))

    if write:
        fields = list(rows[0].keys())
        write_csv(csv_path, rows, fields)
        snapshot_team_stats(league_key, teams)
        log(f'  [{cfg["name"]}] wrote {csv_path}')
    return rows
