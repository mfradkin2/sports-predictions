"""ESPN data access for player-level information.

This module is deliberately paranoid. ESPN's public JSON is undocumented and
changes shape between endpoints and sports, so every reader accepts several
layouts, every field is optional, and any failure degrades to "no props for
this game" rather than breaking the build.
"""
from __future__ import annotations

import re

from .config import STAT_ALIASES
from .util import Http, dig, num

SITE_API = 'https://site.api.espn.com/apis/site/v2/sports'
WEB_API = 'https://site.web.api.espn.com/apis'

# How many athletes to pull per sport, ordered by the sort key below. Season
# leaders are who props are actually offered on.
BULK_LIMIT = 400
BULK_PAGES = 3

SORT_KEYS = {
    'basketball': 'offensive.avgPoints:desc',
    'baseball': None,
    'football': None,
    'hockey': None,
}


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def norm_team(name):
    """Collapse the many spellings ESPN uses for one franchise."""
    n = norm(name)
    aliases = {
        'athletics': 'athletics', 'oaklandathletics': 'athletics',
        'lasvegasathletics': 'athletics', 'sacramentoathletics': 'athletics',
        'laangels': 'losangelesangels', 'lakers': 'losangeleslakers',
        'clippers': 'laclippers', 'losangelesclippers': 'laclippers',
    }
    return aliases.get(n, n)


def team_keys(name):
    """Every key a team might be filed under, most specific first.

    The player feed names teams by nickname ("Angels"); the schedule uses the
    display name ("Los Angeles Angels"). Both, plus the nickname on its own,
    are tried so the two sources meet.
    """
    from .util import short_name
    full = norm_team(name)
    nick = norm_team(short_name(name))
    keys = [full]
    if nick and nick != full:
        keys.append(nick)
    return keys


def find_team(mapping, name):
    """Look a team up in a dict keyed by normalised names, tolerating the
    nickname / full-name mismatch in either direction."""
    if not mapping or not name:
        return None
    for k in team_keys(name):
        if k in mapping:
            return mapping[k]
    full = norm_team(name)
    # Last resort: a stored nickname that ends the full name ("angels" in
    # "losangelesangels"), or the reverse.
    for k, v in mapping.items():
        if k and (full.endswith(k) or k.endswith(full)) and len(k) >= 4:
            return v
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Stat extraction
# ─────────────────────────────────────────────────────────────────────────────
def _alias_map(sport):
    """ESPN stat name (normalised) -> our internal key."""
    out = {}
    for internal, candidates in STAT_ALIASES.get(sport, {}).items():
        for cand in candidates:
            out.setdefault(norm(cand), internal)
    return out


AVG_MARKERS = ('avg', 'pergame', 'permatch', 'average')


def is_average_name(name):
    """True when ESPN's own field name says the number is already per game.

    ESPN mixes ``homeRuns`` (a season total) and ``avgPoints`` (a rate) in the
    same payload, so the field name is the most reliable signal we get about
    which one we are holding.
    """
    n = norm(name)
    return any(m in n for m in AVG_MARKERS)


PITCHING_ALIASES = {
    'gp': ['gamesplayed', 'gp', 'g', 'appearances'],
    'starts': ['gamesstarted', 'gs'],
    'ip': ['inningspitched', 'innings', 'ip'],
    'p_so': ['strikeouts', 'so', 'k'],
    'p_er': ['earnedruns', 'er'],
    'p_h': ['hits', 'h', 'hitsallowed'],
    'p_bb': ['walks', 'basesonballs', 'bb'],
    'p_hr': ['homeruns', 'hr', 'homerunsallowed'],
    'era': ['era', 'earnedrunaverage'],
    'whip': ['whip'],
    'wins': ['wins', 'w'],
}


def _pitching_alias_map():
    out = {}
    for internal, candidates in PITCHING_ALIASES.items():
        for cand in candidates:
            out.setdefault(norm(cand), internal)
    return out


def extract_stats(sport, names, values, category=''):
    """Zip parallel name/value arrays into our internal stat keys.

    ``__avg__`` records which keys arrived already expressed per game, so the
    caller does not have to guess from the magnitude alone. For baseball the
    category matters: "strikeouts" in a pitching line is the pitcher's, not
    the batter's, so pitching categories use their own alias table.
    """
    if sport == 'baseball' and 'pitch' in norm(category):
        amap = _pitching_alias_map()
    else:
        amap = _alias_map(sport)
    stats = {}
    avg_keys = []
    for name, value in zip(names or [], values or []):
        key = amap.get(norm(name))
        if key is None:
            continue
        avg = is_average_name(name)
        if key in stats and not (avg and key not in avg_keys):
            continue                          # keep the first, unless this is the average
        v = num(value)
        if v is None and isinstance(value, str) and ':' in value:
            v = _clock_to_minutes(value)
        if v is None:
            continue
        stats[key] = v
        if avg and key not in avg_keys:
            avg_keys.append(key)
    if avg_keys:
        stats['__avg__'] = avg_keys
    return stats


def _clock_to_minutes(value):
    """'18:42' -> 18.7 (used by NHL time-on-ice and NBA minutes)."""
    try:
        parts = [float(p) for p in str(value).split(':')]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] + parts[1] / 60.0
    if len(parts) == 3:
        return parts[0] * 60 + parts[1] + parts[2] / 60.0
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Bulk athlete statistics
# ─────────────────────────────────────────────────────────────────────────────
def fetch_athlete_stats(http, sport, league, season=None, pages=BULK_PAGES):
    """Season statistics for the league's most-used players.

    Returns ``{normalised_team_name: [player, ...]}``. Empty on any failure.
    Baseball needs two passes: the default listing is batting lines, and the
    pitching lines come from ``category=pitching``; a pitcher present in both
    ends up with one merged record.
    """
    by_id = {}
    order = []
    passes = [''] if sport != 'baseball' else ['', 'pitching']
    for category in passes:
        for page in range(1, pages + 1):
            url = (f'{WEB_API}/common/v3/sports/{sport}/{league}/statistics/byathlete'
                   f'?region=us&lang=en&contentorigin=espn&isqualified=false'
                   f'&limit={BULK_LIMIT}&page={page}')
            if category:
                url += f'&category={category}'
            if season:
                url += f'&season={season}'
            sort = SORT_KEYS.get(sport)
            if sort and not category:
                url += f'&sort={sort}'
            data = http.get_json(url)
            if not data:
                break
            batch = _parse_byathlete(data, sport)
            for p in batch:
                key = p.get('id') or p.get('name')
                if key in by_id:
                    have = by_id[key]
                    merged_avg = list(have['stats'].get('__avg__', [])) + \
                        list(p['stats'].get('__avg__', []))
                    have['stats'].update({k: v for k, v in p['stats'].items() if k != '__avg__'})
                    if merged_avg:
                        have['stats']['__avg__'] = merged_avg
                    if not have.get('pos') and p.get('pos'):
                        have['pos'] = p['pos']
                else:
                    by_id[key] = p
                    order.append(key)
            total_pages = num(dig(data, 'pagination', 'pages'), 1) or 1
            if page >= total_pages or not batch:
                break

    pool = {}
    for key in order:
        p = by_id[key]
        if not p.get('team'):
            continue
        pool.setdefault(norm_team(p['team']), []).append(p)
    return pool


def _parse_byathlete(data, sport):
    """Handle both layouts: category names at the top level, or per athlete."""
    top_names = {}
    for cat in data.get('categories') or []:
        cname = cat.get('name') or ''
        names = cat.get('names') or cat.get('labels') or []
        if names:
            top_names[cname] = names
    _dump_names(sport, top_names)

    out = []
    for entry in data.get('athletes') or []:
        ath = entry.get('athlete') or {}
        stats = {}
        for cat in entry.get('categories') or []:
            cname = cat.get('name') or ''
            names = cat.get('names') or cat.get('labels') or top_names.get(cname) or []
            values = cat.get('totals') or cat.get('values') or cat.get('stats') or []
            if isinstance(values, list) and values and isinstance(values[0], dict):
                names = [v.get('name') or v.get('abbreviation') for v in values]
                values = [v.get('value', v.get('displayValue')) for v in values]
            found = extract_stats(sport, names, values, category=cname)
            merged_avg = list(stats.get('__avg__', [])) + list(found.pop('__avg__', []))
            stats.update(found)
            if merged_avg:
                stats['__avg__'] = merged_avg
        if not {k for k in stats if k != '__avg__'}:
            continue
        player = _athlete_meta(ath)
        player['stats'] = stats
        if player['name']:
            out.append(player)
    return out


def _dump_names(sport, top_names):
    """With SP_DEBUG_DUMP set, record each category's column names once, so an
    unrecognised column (a games-played field under an unexpected name) can be
    read back from a cloud run without shipping the whole response."""
    import json
    import os
    d = os.environ.get('SP_DEBUG_DUMP', '').strip()
    if not d or not top_names:
        return
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f'{sport}_byathlete_columns.json')
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(top_names, f, indent=1)
    except OSError:
        pass


def _athlete_meta(ath):
    team = (ath.get('teamName') or dig(ath, 'team', 'displayName')
            or dig(ath, 'team', 'name') or '')
    short_team = (ath.get('teamShortName') or dig(ath, 'team', 'abbreviation') or '')
    if team and short_team and norm(team) == norm(short_team):
        team = dig(ath, 'team', 'displayName') or team
    return {
        'id': str(ath.get('id') or ''),
        'name': ath.get('displayName') or ath.get('fullName') or '',
        'short': ath.get('shortName') or ath.get('displayName') or '',
        'pos': (dig(ath, 'position', 'abbreviation')
                or dig(ath, 'position', 'name') or ''),
        'jersey': str(ath.get('jersey') or ''),
        'team': team,
        'team_abbr': short_team,
        'headshot': dig(ath, 'headshot', 'href') or '',
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Roster fallback (per team)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_team_rosters(http, sport, league, team_ids, cap=40):
    """Fallback source: one roster request per team, with season splits."""
    pool = {}
    for tid in list(team_ids)[:cap]:
        url = f'{SITE_API}/{sport}/{league}/teams/{tid}/roster?enable=stats'
        data = http.get_json(url)
        if not data:
            continue
        team = (dig(data, 'team', 'displayName') or dig(data, 'team', 'name') or '')
        entries = []
        groups = data.get('athletes') or []
        for grp in groups:
            items = grp.get('items') if isinstance(grp, dict) and 'items' in grp else [grp]
            for ath in items or []:
                meta = _athlete_meta(ath)
                if not meta['name']:
                    continue
                meta['team'] = meta['team'] or team
                meta['stats'] = _roster_stats(ath, sport)
                if meta['stats']:
                    entries.append(meta)
        if entries:
            pool.setdefault(norm_team(team), []).extend(entries)
    return pool


def _roster_stats(ath, sport):
    stats = {}
    for block in (ath.get('statistics') or []):
        for split in (block.get('splits') or []):
            items = split.get('stats') or []
            if items and isinstance(items[0], dict):
                names = [i.get('name') or i.get('abbreviation') for i in items]
                values = [i.get('value', i.get('displayValue')) for i in items]
                found = extract_stats(sport, names, values)
                merged_avg = list(stats.get('__avg__', [])) + list(found.pop('__avg__', []))
                stats.update(found)
                if merged_avg:
                    stats['__avg__'] = merged_avg
    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  Per-event extras: probable pitchers and game leaders
# ─────────────────────────────────────────────────────────────────────────────
def fetch_scoreboard(http, sport, league, yyyymmdd):
    url = f'{SITE_API}/{sport}/{league}/scoreboard?dates={yyyymmdd}'
    return http.get_json(url)


def probable_starters(scoreboard):
    """MLB probable pitchers, keyed by game id -> {'home': ..., 'away': ...}."""
    out = {}
    for ev in (scoreboard or {}).get('events') or []:
        comp = dig(ev, 'competitions', 0) or {}
        slot = {}
        for c in comp.get('competitors') or []:
            side = c.get('homeAway')
            probable = c.get('probables') or []
            if not probable:
                continue
            ath = dig(probable, 0, 'athlete') or {}
            if not ath:
                continue
            slot[side] = {
                'id': str(ath.get('id') or ''),
                'name': ath.get('displayName') or ath.get('shortName') or '',
                'headshot': dig(ath, 'headshot', 'href') or '',
                'summary': dig(probable, 0, 'statistics', 0, 'displayValue') or '',
            }
        # Older payloads put both pitchers under competition.probables.
        for probable in comp.get('probables') or []:
            ath = probable.get('athlete') or {}
            tid = str(dig(probable, 'team', 'id') or '')
            for c in comp.get('competitors') or []:
                if str(dig(c, 'team', 'id') or '') == tid and c.get('homeAway') not in slot:
                    slot[c['homeAway']] = {
                        'id': str(ath.get('id') or ''),
                        'name': ath.get('displayName') or '',
                        'headshot': dig(ath, 'headshot', 'href') or '',
                        'summary': '',
                    }
        if slot:
            out[str(ev.get('id') or '')] = slot
    return out


def event_leaders(scoreboard, sport):
    """Per-game season leaders ESPN already ships with the scoreboard.

    A thin but very reliable source: it needs no extra requests and always
    covers the teams actually playing today.
    """
    out = {}
    for ev in (scoreboard or {}).get('events') or []:
        comp = dig(ev, 'competitions', 0) or {}
        per_team = {}
        for c in comp.get('competitors') or []:
            team = dig(c, 'team', 'displayName') or ''
            people = []
            for cat in c.get('leaders') or []:
                for ldr in cat.get('leaders') or []:
                    ath = ldr.get('athlete') or {}
                    if not ath.get('displayName'):
                        continue
                    people.append({
                        **_athlete_meta(ath),
                        'team': team,
                        'leader_cat': cat.get('name') or '',
                        'leader_value': ldr.get('displayValue') or '',
                    })
            if people:
                per_team[c.get('homeAway') or team] = people
        if per_team:
            out[str(ev.get('id') or '')] = per_team
    return out


def fetch_team_index(http, sport, league):
    """team id -> display name, used by the roster fallback."""
    data = http.get_json(f'{SITE_API}/{sport}/{league}/teams?limit=50')
    out = {}
    for grp in dig(data, 'sports', 0, 'leagues', 0, 'teams') or []:
        t = grp.get('team') or {}
        if t.get('id'):
            out[str(t['id'])] = t.get('displayName') or t.get('name') or ''
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Injuries
# ─────────────────────────────────────────────────────────────────────────────
OUT_STATUSES = ('out', 'injured reserve', 'suspended', 'suspension',
                'season', 'doubtful', 'inactive', 'reserve', 'injured list',
                'non-roster', 'paternity', 'bereavement')
LIMITED_STATUSES = ('questionable', 'day-to-day', 'day to day', 'probable',
                    'game time decision', 'gtd', 'limited')
# Baseball's statuses are IL stints: "10-Day-IL", "15-Day IL", "60-Day-IL".
# Hockey and football use "IR" and "IR-R". Matched as whole tokens so a
# name like "Wilson" cannot trip them.
_LIST_TOKENS = re.compile(r'(?:^|[\s\-/])(il|ir|ir-r|pup|nfi|ltir)(?:$|[\s\-/])')


def fetch_injuries(http, sport, league):
    """Current injury report, keyed by normalised team name.

    ESPN nests this two ways depending on the sport, so both are read:
    ``injuries[].injuries[]`` (team -> players) and a flat ``injuries[]``.
    Returns ``{team: {athlete_id: {...}}}``; empty on any failure.
    """
    data = http.get_json(f'{SITE_API}/{sport}/{league}/injuries')
    out = {}
    for block in (data or {}).get('injuries') or []:
        team = (block.get('displayName') or dig(block, 'team', 'displayName') or '')
        entries = block.get('injuries')
        if entries is None:
            entries = [block]
            team = team or dig(block, 'athlete', 'team', 'displayName') or ''
        for e in entries or []:
            ath = e.get('athlete') or {}
            aid = str(ath.get('id') or '')
            if not aid:
                for link in ath.get('links') or []:
                    m = re.search(r'/id/(\d+)', str(link.get('href') or ''))
                    if m:
                        aid = m.group(1)
                        break
            if not aid:
                continue
            status = str(e.get('status') or dig(e, 'type', 'description') or '').strip()
            level = classify_status(status)
            if level == 'active':
                continue
            record = {
                'id': aid,
                'name': ath.get('displayName') or ath.get('shortName') or '',
                'pos': dig(ath, 'position', 'abbreviation') or '',
                'status': status,
                'level': level,
                'detail': (dig(e, 'details', 'type') or dig(e, 'details', 'detail')
                           or e.get('shortComment') or e.get('longComment') or ''),
            }
            key = norm_team(team) if team else norm_team(dig(ath, 'team', 'displayName') or '')
            out.setdefault(key, {})[aid] = record
    return out


def classify_status(status):
    """'out' | 'limited' | 'active' from ESPN's free-text status."""
    s = (status or '').strip().lower()
    if not s:
        return 'active'
    if any(w in s for w in LIMITED_STATUSES):
        return 'limited'
    if any(w in s for w in OUT_STATUSES) or _LIST_TOKENS.search(s):
        return 'out'
    return 'active'


# ─────────────────────────────────────────────────────────────────────────────
#  Game summary / box score
# ─────────────────────────────────────────────────────────────────────────────
def fetch_summary(http, sport, league, event_id):
    return http.get_json(f'{SITE_API}/{sport}/{league}/summary?event={event_id}')


# Box-score column names differ from the season-stat endpoints and are
# grouped by category, which is what disambiguates "YDS" in passing from
# "YDS" in rushing. Keys are normalised category name -> {column: internal}.
BOX_COLUMNS = {
    'baseball': {
        'batting': {'ab': 'ab', 'r': 'runs', 'h': 'hits', 'rbi': 'rbi', 'hr': 'hr',
                    'bb': 'bb', 'k': 'so', 'so': 'so', 'sb': 'sb', '2b': 'doubles',
                    '3b': 'triples', 'tb': 'tb', 'atbats': 'ab', 'runs': 'runs',
                    'hits': 'hits', 'rbis': 'rbi', 'homeruns': 'hr', 'walks': 'bb',
                    'strikeouts': 'so', 'stolenbases': 'sb', 'doubles': 'doubles',
                    'triples': 'triples', 'totalbases': 'tb'},
        'pitching': {'ip': 'ip', 'h': 'p_h', 'r': 'p_r', 'er': 'p_er', 'bb': 'p_bb',
                     'k': 'p_so', 'so': 'p_so', 'hr': 'p_hr', 'pc': 'pitches',
                     'inningspitched': 'ip', 'hits': 'p_h', 'earnedruns': 'p_er',
                     'walks': 'p_bb', 'strikeouts': 'p_so', 'pitches': 'pitches'},
    },
    'basketball': {
        '': {'min': 'min', 'pts': 'pts', 'reb': 'reb', 'ast': 'ast', 'stl': 'stl',
             'blk': 'blk', 'to': 'tov', '3pt': 'fg3', 'fg': 'fgm', 'ft': 'ftm',
             'oreb': 'oreb', 'dreb': 'dreb', 'pf': 'pf', 'minutes': 'min',
             'points': 'pts', 'rebounds': 'reb', 'assists': 'ast', 'steals': 'stl',
             'blocks': 'blk', 'turnovers': 'tov', 'threepointfieldgoalsmade': 'fg3',
             'threepointfieldgoalsmadethreepointfieldgoalsattempted': 'fg3',
             'fieldgoalsmadefieldgoalsattempted': 'fgm'},
    },
    'football': {
        'passing': {'catt': 'pass_cmp', 'yds': 'pass_yds', 'td': 'pass_td', 'int': 'pass_int',
                    'completionsattempts': 'pass_cmp', 'completionspassingattempts': 'pass_cmp',
                    'passingyards': 'pass_yds', 'passingtouchdowns': 'pass_td',
                    'interceptions': 'pass_int', 'sacks': 'sacked'},
        'rushing': {'car': 'rush_att', 'yds': 'rush_yds', 'td': 'rush_td', 'long': 'rush_long',
                    'rushingattempts': 'rush_att', 'rushingyards': 'rush_yds',
                    'rushingtouchdowns': 'rush_td'},
        'receiving': {'rec': 'rec', 'yds': 'rec_yds', 'td': 'rec_td', 'tgts': 'targets',
                      'long': 'rec_long', 'receptions': 'rec', 'receivingyards': 'rec_yds',
                      'receivingtouchdowns': 'rec_td', 'receivingtargets': 'targets'},
        'defensive': {'tot': 'tackles', 'solo': 'solo', 'sacks': 'sacks', 'tfl': 'tfl',
                      'totaltackles': 'tackles', 'sacks': 'sacks'},
        'kicking': {'fg': 'fgm', 'xp': 'xpm', 'pts': 'kick_pts'},
    },
    'hockey': {
        'skaters': {'g': 'goals', 'a': 'assists', 'pts': 'points', 's': 'sog', 'sog': 'sog',
                    'bs': 'blocks', 'hits': 'hits', 'toi': 'toi', 'goals': 'goals',
                    'assists': 'assists', 'points': 'points', 'shotsongoal': 'sog',
                    'shots': 'sog', 'blockedshots': 'blocks', 'timeonice': 'toi'},
        'forwards': None, 'defenses': None, 'defensemen': None,   # alias skaters
        'goalies': {'sa': 'shots_against', 'ga': 'ga', 'sv': 'saves', 'svpct': 'sv_pct',
                    'toi': 'toi', 'shotsagainst': 'shots_against', 'goalsagainst': 'ga',
                    'saves': 'saves', 'savepct': 'sv_pct'},
    },
}


def _box_value(raw):
    """'12' -> 12; '3-7' -> 3 (made-attempted); '18:42' -> minutes; '' -> None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ('--', '-'):
        return None
    if ':' in s:
        return _clock_to_minutes(s)
    if '-' in s and not s.startswith('-'):
        s = s.split('-', 1)[0]
    if '/' in s:
        s = s.split('/', 1)[0]
    return num(s)


def boxscore_player_stats(summary, sport):
    """Every player's in-game line from a summary payload.

    Returns ``{athlete_id: {'name':..., 'team':..., 'stats': {internal: value}}}``.
    Categories map onto the same internal keys the props use, so a prop for
    ``pass_yds`` can be checked against the live ``pass_yds``.
    """
    table = BOX_COLUMNS.get(sport) or {}
    out = {}
    for side in dig(summary, 'boxscore', 'players') or []:
        team = dig(side, 'team', 'displayName') or ''
        for group in side.get('statistics') or []:
            cat = norm(group.get('name') or group.get('type') or '')
            columns = table.get(cat)
            if columns is None:
                # Hockey lists forwards/defense separately; basketball has one
                # unnamed group; fall back to whatever single mapping exists.
                if sport == 'hockey' and cat not in ('goalies', 'goalie'):
                    columns = table.get('skaters')
                elif sport == 'basketball':
                    columns = table.get('')
                else:
                    continue
            labels = [norm(x) for x in (group.get('labels') or group.get('names') or [])]
            names = [norm(x) for x in (group.get('names') or [])]
            for entry in group.get('athletes') or []:
                ath = entry.get('athlete') or {}
                aid = str(ath.get('id') or '')
                if not aid:
                    continue
                values = entry.get('stats') or []
                rec = out.setdefault(aid, {
                    'id': aid, 'name': ath.get('displayName') or ath.get('shortName') or '',
                    'team': team, 'played': True, 'stats': {}})
                if entry.get('didNotPlay') or entry.get('active') is False:
                    rec['played'] = False
                for i, raw in enumerate(values):
                    key = None
                    if i < len(names):
                        key = columns.get(names[i])
                    if key is None and i < len(labels):
                        key = columns.get(labels[i])
                    if key is None:
                        continue
                    v = _box_value(raw)
                    if v is not None and key not in rec['stats']:
                        rec['stats'][key] = v
    # Derived lines the props are priced on.
    for rec in out.values():
        st = rec['stats']
        if sport == 'baseball':
            if 'tb' not in st and 'hits' in st:
                singles = st['hits'] - st.get('doubles', 0) - st.get('triples', 0) - st.get('hr', 0)
                st['tb'] = max(singles, 0) + 2 * st.get('doubles', 0) + 3 * st.get('triples', 0) + 4 * st.get('hr', 0)
            if 'ip' in st:
                whole = int(st['ip'])
                st['outs'] = whole * 3 + round((st['ip'] - whole) * 10)
        elif sport == 'basketball':
            if 'pts' in st:
                st['pra'] = st['pts'] + st.get('reb', 0) + st.get('ast', 0)
                st['pr'] = st['pts'] + st.get('reb', 0)
                st['pa'] = st['pts'] + st.get('ast', 0)
            st['stlblk'] = st.get('stl', 0) + st.get('blk', 0)
        elif sport == 'football':
            st['scrim_yds'] = st.get('rush_yds', 0) + st.get('rec_yds', 0)
            st['td'] = st.get('rush_td', 0) + st.get('rec_td', 0)
        elif sport == 'hockey':
            if 'points' not in st and ('goals' in st or 'assists' in st):
                st['points'] = st.get('goals', 0) + st.get('assists', 0)
    return out


def game_state(summary):
    """('scheduled' | 'live' | 'final', detail string) from a summary payload."""
    status = (dig(summary, 'header', 'competitions', 0, 'status') or
              dig(summary, 'competitions', 0, 'status') or {})
    name = dig(status, 'type', 'name') or ''
    detail = dig(status, 'type', 'shortDetail') or dig(status, 'type', 'detail') or ''
    if name == 'STATUS_FINAL' or dig(status, 'type', 'completed'):
        return 'final', detail
    if name in ('STATUS_IN_PROGRESS', 'STATUS_HALFTIME', 'STATUS_END_PERIOD',
                'STATUS_DELAYED', 'STATUS_RAIN_DELAY'):
        return 'live', detail
    return 'scheduled', detail
