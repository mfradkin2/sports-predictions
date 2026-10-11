"""How many points each side scores — and so the total and the spread.

The win-probability model answers one question: who wins. It says nothing
about by how much, or how high-scoring the game will be, which are the other
two numbers a sportsbook puts next to a moneyline. This module supplies them.

The estimate is deliberately plain. Each team carries a scoring rate and a
rate conceded, both season-to-date and both pulled toward the league average
by a few games of prior so a hot first fortnight does not run away with it.
An expected score is the league average scaled by how much better than
average this attack is and how much worse than average that defence is, with
the measured home-field edge split between the sides:

    expected home = avg * (home attack / avg) * (away defence / avg) + edge/2
    expected away = avg * (away attack / avg) * (home defence / avg) - edge/2

Total is the sum, and that is what gets published.

The spread does not come from here. Subtracting those two expected scores
gives a margin, and it is a decent one, but it is a *second* opinion about
who wins — and on the archive it disagreed with the win-probability model
about the favourite in 15% of NFL games and 30% of baseball ones. A card
whose spread says Seattle and whose moneyline says San Francisco is the site
arguing with itself. So the spread is derived from the win probability
instead, which cannot contradict it: if margins in a league scatter by about
fourteen points, a 62% favourite is favoured by fourteen times the normal
quantile of 0.62, or four and a quarter. That is also what a sportsbook does
to move between a moneyline and a spread, and on the archive it was slightly
the more accurate of the two (NFL 10.07 against 10.22 points of average
error, baseball 3.33 against 3.34). The expected scores shown are then
rebuilt from the published total and spread, so the three numbers always
add up.

Everything that matters about this model is in the last step. Scoring is far
noisier than winning, and in some sports there is almost nothing to know: on
the archive the full estimate beats simply guessing the league average by 7%
on NFL margins, and by about 1% on baseball ones, where run scoring at the
team-game level is close to irreducible. So the published number is a blend
of the estimate and the league average, and each league's blend is fitted
walk-forward on its own archive (:func:`fit`). A league where the estimate
earns its keep gets it in full; a league where it does not falls back toward
the average, which is the honest answer; a league with no record yet gets the
average outright rather than a guess dressed up as a prediction.

The fit reports its own error, and the error of the league average it is
measured against, so the page can say how much to trust the number instead of
printing it bare.
"""
from __future__ import annotations

import collections
import statistics

from .util import clamp, norm_ppf

# Games of league-average prior mixed into each team's rate. Ten is where
# both the NFL and MLB archives bottomed out; the curve is flat enough either
# side that nothing turns on the exact value.
SHRINK = 10.0
# A team needs this many games before its own rates mean anything, and the
# league needs this many team-games before its average does.
MIN_TEAM_GAMES = 5
MIN_LEAGUE_GAMES = 60
# Below this many graded games a fitted blend is noise, so the league average
# stands alone.
MIN_FIT_GAMES = 40
BLEND_STEPS = (0.0, 0.25, 0.5, 0.75, 1.0)
# How much of the league's margin spread to use when turning a win
# probability into a spread. One is the textbook conversion; the grid lets a
# league say its margins are a little tighter or looser than that.
SCALE_STEPS = (0.8, 0.9, 1.0, 1.1, 1.2)


def _rate(scored, conceded, played, avg, which):
    if not played:
        return avg
    raw = (scored if which == 'off' else conceded) / played
    return (raw * played + avg * SHRINK) / (played + SHRINK)


def expected(avg, home, away, home_edge=0.0, blend=1.0):
    """``(expected away, expected home)`` for one game.

    ``home`` and ``away`` are ``(scored, conceded, played)`` totals as they
    stood before the game. ``blend`` is how much of the estimate to keep: 0
    gives the league average to both sides, 1 the estimate in full.
    """
    if not avg or avg <= 0:
        return None
    half = avg / 2.0
    h_off = _rate(*home, avg=half, which='off')
    h_def = _rate(*home, avg=half, which='def')
    a_off = _rate(*away, avg=half, which='off')
    a_def = _rate(*away, avg=half, which='def')
    exp_home = half * (h_off / half) * (a_def / half) + home_edge / 2.0
    exp_away = half * (a_off / half) * (h_def / half) - home_edge / 2.0
    # Blend each side toward the league average, which is what a model with
    # nothing to say should fall back to.
    base_home = half + home_edge / 2.0
    base_away = half - home_edge / 2.0
    b = max(0.0, min(1.0, float(blend)))
    return (b * exp_away + (1 - b) * base_away,
            b * exp_home + (1 - b) * base_home)


class _Tally:
    """Running scoring totals, the league average and the home-field edge.

    One pass, read before each game and advanced after it, so nothing a
    prediction sees can include its own result.
    """

    def __init__(self):
        self.teams = collections.defaultdict(lambda: [0.0, 0.0, 0])  # scored, conceded, played
        self.points = self.games = 0.0
        self.edge_sum = self.edge_n = 0.0

    def avg(self):
        """League average combined score, or None before there is one."""
        if self.games < MIN_LEAGUE_GAMES:
            return None
        return self.points / self.games * 2.0

    def edge(self):
        return (self.edge_sum / self.edge_n) if self.edge_n else 0.0

    def of(self, team):
        return tuple(self.teams[team])

    def ready(self, home, away):
        return (self.avg() is not None
                and self.teams[home][2] >= MIN_TEAM_GAMES
                and self.teams[away][2] >= MIN_TEAM_GAMES)

    def add(self, home, away, home_score, away_score):
        for team, scored, conceded in ((home, home_score, away_score),
                                       (away, away_score, home_score)):
            t = self.teams[team]
            t[0] += scored
            t[1] += conceded
            t[2] += 1
        self.points += home_score + away_score
        self.games += 2
        self.edge_sum += home_score - away_score
        self.edge_n += 1


def _replay(games):
    """Walk the archive once, collecting the pre-game snapshots and leaving
    the tally at the end so upcoming games can be priced off the same pass."""
    tally = _Tally()
    rows = []
    for g in games:
        home, away = g['home'], g['away']
        hs, as_ = g.get('home_score'), g.get('away_score')
        if hs is not None and as_ is not None and tally.ready(home, away):
            rows.append((tally.avg(), tally.of(home), tally.of(away), tally.edge(),
                         hs + as_, hs - as_))
        if hs is None or as_ is None or g.get('preseason'):
            continue
        tally.add(home, away, hs, as_)
    return rows, tally


def walk(games):
    """The pre-game snapshots alone: ``(avg, home, away, edge, total, margin)``
    per game that had enough record behind it to be worth judging."""
    return _replay(games)[0]


def current(games):
    """Where the league stands now, for pricing games not yet played:
    ``(average combined score, tally)``. The tally answers ``of(team)`` with
    that team's ``(scored, conceded, played)`` and ``ready(home, away)``."""
    _, tally = _replay(games)
    return tally.avg(), tally


def _mae(values):
    return sum(values) / len(values) if values else None


WARMING = {'n': 0, 'blend_total': 0.0, 'home_edge': 0.0, 'margin_sd': None,
           'spread_scale': 1.0, 'spread_n': 0, 'stage': 'warming up',
           'mae_total': None, 'mae_margin': None,
           'naive_total': None, 'naive_margin': None}


def fit(games, win_probs=None):
    """Fit the total's blend and the spread's scale, and report how both do.

    ``win_probs`` is the leak-free win probability for each game, aligned with
    ``games`` — Elo's replayed number, which never sees its own result. Where
    it is missing the spread cannot be fitted, so the league's own margin
    spread is used unscaled, which is the textbook conversion.
    """
    played = walk(games)
    if len(played) < MIN_FIT_GAMES:
        return dict(WARMING, n=len(played))

    edge = played[-1][3]
    rows = []
    for avg, home, away, home_edge, total, margin in played:
        pair = expected(avg, home, away, home_edge, blend=1.0)
        if pair is None:
            continue
        rows.append((pair[1] + pair[0], avg, total))

    scored = []
    for b in BLEND_STEPS:
        err = _mae([abs((b * r[0] + (1 - b) * r[1]) - r[2]) for r in rows])
        scored.append((err, b))
    scored.sort(key=lambda x: (x[0], -x[1]))
    mae_total, blend_total = scored[0]

    margins = [row[5] for row in played]
    margin_sd = statistics.pstdev(margins) if len(margins) > 1 else None

    # The spread, scored against the same games, from whatever leak-free win
    # probability was supplied.
    mae_margin = naive_margin = None
    scale = 1.0
    pairs = []
    if win_probs:
        by_game = win_probs if isinstance(win_probs, dict) else None
        if by_game is None:
            win_probs = list(win_probs)
        for i, g in enumerate(games):
            p = (by_game.get(g.get('game_id')) if by_game
                 else (win_probs[i] if i < len(win_probs) else None))
            hs, as_ = g.get('home_score'), g.get('away_score')
            if p is None or hs is None or as_ is None or g.get('preseason'):
                continue
            pairs.append((float(p), hs - as_))
    if pairs and margin_sd:
        best = None
        for sc in SCALE_STEPS:
            err = _mae([abs(norm_ppf(p, 0.0, sc * margin_sd) - m) for p, m in pairs])
            if best is None or err < best[0]:
                best = (err, sc)
        mae_margin, scale = best
        naive_margin = _mae([abs(edge - m) for _, m in pairs])

    return {
        'n': len(rows),
        'blend_total': round(blend_total, 2),
        'home_edge': round(edge, 2),
        'margin_sd': round(margin_sd, 2) if margin_sd else None,
        'spread_scale': scale,
        'spread_n': len(pairs),
        'stage': 'fitted',
        'mae_total': round(mae_total, 2) if mae_total is not None else None,
        'mae_margin': round(mae_margin, 2) if mae_margin is not None else None,
        'naive_total': round(_mae([abs(r[1] - r[2]) for r in rows]), 2),
        'naive_margin': round(naive_margin, 2) if naive_margin is not None else None,
    }


def spread_from(win_prob, fitted):
    """The home side's margin implied by its win probability.

    Positive means the home team is favoured by that many. This is what keeps
    the spread and the moneyline from arguing: both are the same probability,
    written two ways.
    """
    sd = fitted.get('margin_sd') if fitted else None
    if not sd or win_prob is None:
        return None
    scale = float(fitted.get('spread_scale') or 1.0)
    return norm_ppf(clamp(float(win_prob), 0.01, 0.99), 0.0, scale * float(sd))


def predict(fitted, avg, home, away, win_prob=None):
    """The published total and spread for one upcoming game.

    ``home`` and ``away`` are ``(scored, conceded, played)`` season totals.
    ``win_prob`` is the home side's win probability, which the spread is
    derived from. Returns ``None`` when there is not enough on record to say
    anything.
    """
    if not fitted or not avg or avg <= 0:
        return None
    # Until the fit has something to say, the estimate collapses to the
    # league average, which would put an identical total on every card in the
    # league. Better to publish nothing and say the record is still building.
    if fitted.get('stage') != 'fitted':
        return None
    if home[2] < MIN_TEAM_GAMES or away[2] < MIN_TEAM_GAMES:
        return None
    edge = float(fitted.get('home_edge') or 0.0)
    pair = expected(avg, home, away, edge, blend=fitted.get('blend_total', 0.0))
    if pair is None:
        return None
    total = pair[0] + pair[1]
    margin = spread_from(win_prob, fitted)
    # Rebuilt from the two published numbers, so the scoreline on the card
    # always adds up to the total and the spread beside it.
    exp_home = (total + margin) / 2.0 if margin is not None else pair[1]
    exp_away = (total - margin) / 2.0 if margin is not None else pair[0]
    return {
        'total': round(total, 1),
        'margin': None if margin is None else round(margin, 1),
        'exp_away': round(exp_away, 1),
        'exp_home': round(exp_home, 1),
        'mae_total': fitted.get('mae_total'),
        'mae_margin': fitted.get('mae_margin'),
        'naive_total': fitted.get('naive_total'),
        'naive_margin': fitted.get('naive_margin'),
        'stage': fitted.get('stage', 'warming up'),
    }
