"""Leak-free feature construction.

Every feature attached to a game is computed from games that finished *before*
it. The previous model trained on end-of-season team statistics, which quietly
told it how the season turned out; walking the log forward removes that.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, timedelta

from .util import clamp, num, parse_date

# Deliberately compact. Season-to-date win%, raw scoring rate and strength of
# schedule are all computed below for display, but they are left out of the
# model: they are near-collinear with Pythagorean and margin, and on a
# single-season sample the extra columns cost more in variance than they add
# in signal (measured by walk-forward log loss).
FEATURE_NAMES = [
    'elo_diff',        # Elo edge, per 100 points
    'pyth_diff',       # season-to-date Pythagorean win% edge
    'form_diff',       # recency-weighted win rate over the recent window
    'margin_diff',     # recency-weighted scoring-margin edge, in league sigmas
    'venue_diff',      # home team at home vs away team on the road
    'rest_diff',       # days of rest edge, capped
    'b2b_diff',        # short-rest penalty (back-to-back / one-day turnaround)
    'h2h_diff',        # season head-to-head edge between these two teams
]

# Computed and published for transparency, but not fed to the model.
EXTRA_FEATURES = ['wpct_diff', 'off_diff', 'def_diff', 'sos_diff', 'exp_diff']

RECENT_N = 20
FORM_DECAY = 0.90          # weight of each additional game back


# ESPN season types: 1 preseason, 2 regular season, 3 postseason.
ESPN_PRESEASON = 1


def _first_weekday(year, month, weekday):
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def preseason_by_date(d, league_key):
    """Fallback when the feed did not tell us the season type.

    Only needed for rows captured before the ingest started recording it; new
    rows carry ESPN's own classification.
    """
    if league_key == 'nfl':
        # Week 1 kicks off the Thursday after the first Monday in September.
        season_year = d.year if d.month >= 3 else d.year - 1
        opener = _first_weekday(season_year, 9, 0) + timedelta(days=3)
        return d < opener
    if league_key == 'nhl':
        # Camp games run through September; the season opens in early October.
        return d.month == 9 or (d.month == 10 and d.day < 5)
    if league_key == 'nba':
        return d.month == 9 or (d.month == 10 and d.day < 18)
    if league_key == 'mlb':
        # Spring training: February through the third week of March.
        return d.month <= 2 or (d.month == 3 and d.day < 20)
    return False


def detect_preseason(row, d, league_key):
    slug = (row.get('season_slug') or '').strip().lower()
    if slug:
        return slug.startswith('pre')
    season_type = num(row.get('season_type'))
    if season_type is not None:
        return int(season_type) == ESPN_PRESEASON
    return preseason_by_date(d, league_key)


def normalize_games(rows, league_key):
    """Turn enriched-CSV rows into a clean, date-sorted game log."""
    games = []
    for r in rows:
        d = parse_date(r.get('game_date'))
        home = (r.get('home_team') or '').strip()
        away = (r.get('away_team') or '').strip()
        if not d or not home or not away:
            continue
        status = (r.get('status') or '').strip()
        hs, as_ = num(r.get('home_score')), num(r.get('away_score'))
        final = status == 'Final' and (r.get('winner') or '').strip() != ''
        if final and (hs is None or as_ is None):
            final = False
        # A 0-0 "Final" is ESPN's placeholder for a game that never happened.
        if final and hs == 0 and as_ == 0:
            final = False
        games.append({
            'game_id': (r.get('game_id') or '').strip(),
            'date': d,
            'season': season_of(d, league_key),
            'preseason': detect_preseason(r, d, league_key),
            'home': home,
            'away': away,
            'home_score': hs if final else None,
            'away_score': as_ if final else None,
            'final': final,
            'winner': (r.get('winner') or '').strip(),
            'row': r,
        })
    games.sort(key=lambda g: (g['date'], g['game_id']))
    return games


def season_of(d, league_key):
    """Season label. Winter leagues roll over in the summer, not in January."""
    if league_key in ('nhl', 'nba'):
        return d.year if d.month >= 8 else d.year - 1
    if league_key == 'nfl':
        return d.year if d.month >= 3 else d.year - 1
    return d.year


class TeamState:
    __slots__ = ('rs', 'ra', 'w', 'l', 't', 'gp', 'recent', 'margins',
                 'scored', 'allowed', 'opp_elo', 'last_date',
                 'home_w', 'home_gp', 'away_w', 'away_gp')

    def __init__(self):
        self.rs = 0.0
        self.ra = 0.0
        self.w = 0
        self.l = 0
        self.t = 0
        self.gp = 0
        self.recent = deque(maxlen=RECENT_N)   # 1 win / 0.5 tie / 0 loss
        self.margins = deque(maxlen=RECENT_N)
        self.scored = deque(maxlen=RECENT_N)
        self.allowed = deque(maxlen=RECENT_N)
        self.opp_elo = deque(maxlen=40)
        self.last_date = None
        self.home_w = 0.0
        self.home_gp = 0
        self.away_w = 0.0
        self.away_gp = 0

    def venue_rate(self, at_home, league_default=0.5):
        gp = self.home_gp if at_home else self.away_gp
        wins = self.home_w if at_home else self.away_w
        if gp < 5:                       # too few to mean anything; shrink hard
            prior_w = 5 - gp
            return (wins + prior_w * league_default) / 5.0
        return wins / gp

    def sos(self):
        return sum(self.opp_elo) / len(self.opp_elo) if self.opp_elo else 1500.0

    def win_pct(self):
        return (self.w + 0.5 * self.t) / self.gp if self.gp else 0.5

    def pyth(self, exponent):
        if self.rs <= 0 and self.ra <= 0:
            return 0.5
        a = self.rs ** exponent
        b = self.ra ** exponent
        return a / (a + b) if (a + b) > 0 else 0.5

    def form(self):
        if not self.recent:
            return 0.5
        vals = list(self.recent)[::-1]        # newest first
        wsum = tot = 0.0
        for i, v in enumerate(vals):
            w = FORM_DECAY ** i
            wsum += w * v
            tot += w
        return wsum / tot if tot else 0.5

    def margin_form(self):
        return _decayed(self.margins, 0.0)

    def scoring_form(self, default):
        return _decayed(self.scored, default)

    def allowed_form(self, default):
        return _decayed(self.allowed, default)


def _decayed(values, default):
    if not values:
        return default
    wsum = tot = 0.0
    for i, v in enumerate(list(values)[::-1]):
        w = FORM_DECAY ** i
        wsum += w * v
        tot += w
    return wsum / tot if tot else default


PYTH_EXPONENT = {'mlb': 1.83, 'nba': 14.0, 'nfl': 2.37, 'nhl': 2.05}


def build(games, league_key, elo_records, score_sigma=10.0):
    """Attach a leak-free feature vector to every game in the log.

    ``elo_records`` comes from ``EloEngine.replay`` and is index-aligned with
    ``games``.
    """
    exponent = PYTH_EXPONENT.get(league_key, 2.0)
    team_avg = score_sigma * 2.0     # rough per-team scoring level, only a prior
    state = defaultdict(TeamState)
    h2h = defaultdict(lambda: [0, 0])       # (a,b) sorted key -> [a_wins, b_wins]
    season = None
    out = []

    for idx, g in enumerate(games):
        if season is not None and g['season'] != season:
            state = defaultdict(TeamState)
            h2h = defaultdict(lambda: [0, 0])
        season = g['season']

        home, away = g['home'], g['away']
        hs_t, as_t = state[home], state[away]
        elo = elo_records[idx] if idx < len(elo_records) else {}

        rest_h = _rest(hs_t.last_date, g['date'])
        rest_a = _rest(as_t.last_date, g['date'])
        key = tuple(sorted((home, away)))
        rec = h2h[key]
        h2h_home = rec[0] if key[0] == home else rec[1]
        h2h_away = rec[1] if key[0] == home else rec[0]
        h2h_total = h2h_home + h2h_away

        half_total = max(score_sigma, 0.5)
        feats = {
            'elo_diff': (elo.get('home_elo', 1500.0) - elo.get('away_elo', 1500.0)) / 100.0,
            'pyth_diff': hs_t.pyth(exponent) - as_t.pyth(exponent),
            'wpct_diff': hs_t.win_pct() - as_t.win_pct(),
            'form_diff': hs_t.form() - as_t.form(),
            'margin_diff': (hs_t.margin_form() - as_t.margin_form()) / half_total,
            'off_diff': (hs_t.scoring_form(team_avg) - as_t.scoring_form(team_avg)) / half_total,
            'def_diff': (as_t.allowed_form(team_avg) - hs_t.allowed_form(team_avg)) / half_total,
            'venue_diff': hs_t.venue_rate(True) - as_t.venue_rate(False),
            'sos_diff': (hs_t.sos() - as_t.sos()) / 100.0,
            'rest_diff': clamp((rest_h - rest_a) / 3.0, -1.5, 1.5),
            'b2b_diff': _short_rest(rest_a, league_key) - _short_rest(rest_h, league_key),
            'h2h_diff': ((h2h_home - h2h_away) / h2h_total) if h2h_total >= 2 else 0.0,
            'exp_diff': clamp((hs_t.gp - as_t.gp) / 10.0, -1.0, 1.0),
        }

        out.append({
            'index': idx,
            'game': g,
            'features': feats,
            'vector': [feats[n] for n in FEATURE_NAMES],
            'elo_prob': elo.get('elo_home_prob', 0.5),
            'min_gp': min(hs_t.gp, as_t.gp),
            'context': {
                'home_rest': rest_h, 'away_rest': rest_a,
                'home_last10': _last10(hs_t), 'away_last10': _last10(as_t),
                'home_elo': round(elo.get('home_elo', 1500.0), 1),
                'away_elo': round(elo.get('away_elo', 1500.0), 1),
                'home_margin10': round(hs_t.margin_form(), 2),
                'away_margin10': round(as_t.margin_form(), 2),
                'home_scored10': round(hs_t.scoring_form(team_avg), 2),
                'away_scored10': round(as_t.scoring_form(team_avg), 2),
                'home_allowed10': round(hs_t.allowed_form(team_avg), 2),
                'away_allowed10': round(as_t.allowed_form(team_avg), 2),
                'home_venue': round(hs_t.venue_rate(True), 3),
                'away_venue': round(as_t.venue_rate(False), 3),
                'h2h': f'{h2h_home}-{h2h_away}' if h2h_total else '',
            },
        })

        # Advance state only for completed games that count. Exhibition games
        # are played by rosters that will not take the field in the regular
        # season, so letting them move ratings or form is worse than ignoring
        # them.
        if g['final'] and not g.get('preseason'):
            hs, as_ = g['home_score'], g['away_score']
            hs_t.opp_elo.append(elo.get('away_elo', 1500.0))
            as_t.opp_elo.append(elo.get('home_elo', 1500.0))
            _apply(hs_t, hs, as_, at_home=True)
            _apply(as_t, as_, hs, at_home=False)
            hs_t.last_date = g['date']
            as_t.last_date = g['date']
            if hs > as_:
                rec[0 if key[0] == home else 1] += 1
            elif as_ > hs:
                rec[0 if key[0] == away else 1] += 1

    return out


def _apply(st, scored, allowed, at_home):
    st.rs += scored
    st.ra += allowed
    st.gp += 1
    margin = scored - allowed
    st.margins.append(margin)
    st.scored.append(scored)
    st.allowed.append(allowed)
    if margin > 0:
        st.w += 1
        st.recent.append(1.0)
    elif margin < 0:
        st.l += 1
        st.recent.append(0.0)
    else:
        st.t += 1
        st.recent.append(0.5)
    credit = 1.0 if margin > 0 else (0.5 if margin == 0 else 0.0)
    if at_home:
        st.home_gp += 1
        st.home_w += credit
    else:
        st.away_gp += 1
        st.away_w += credit


def _rest(last_date, game_date):
    if last_date is None:
        return 3
    return clamp((game_date - last_date).days, 0, 10)


def _short_rest(rest_days, league_key):
    """How punishing this turnaround is, on a 0–1 scale."""
    if league_key in ('nba', 'nhl'):
        return 1.0 if rest_days <= 0 else (0.35 if rest_days == 1 else 0.0)
    if league_key == 'nfl':
        return 1.0 if rest_days <= 4 else (0.3 if rest_days <= 5 else 0.0)
    return 0.5 if rest_days <= 0 else 0.0     # MLB: only true doubleheaders


def _last10(st):
    if not st.recent:
        return ''
    vals = list(st.recent)
    w = sum(1 for v in vals if v == 1.0)
    t = sum(1 for v in vals if v == 0.5)
    l = len(vals) - w - t
    return f'{w}-{l}-{t}' if t else f'{w}-{l}'
