"""Elo rating engine.

Ratings are replayed forward through the game log, so the rating attached to a
game is always the *pre-game* rating. That removes the look-ahead leakage in
the previous approach, which scored historical games with end-of-season team
statistics.
"""
from __future__ import annotations

import math

from .util import clamp

BASE_RATING = 1500.0


class EloEngine:
    """538-style Elo with a margin-of-victory multiplier and season regression.

    Parameters
    ----------
    k        base update size per game
    hfa      home-field advantage, in rating points
    mov      0 -> ignore margin entirely, 1 -> full margin-of-victory scaling
    regress  fraction of a rating pulled back to the mean at a season boundary
    scale    Elo logistic scale (400 = a 400-point edge is ~10:1 odds)
    """

    def __init__(self, k=20.0, hfa=50.0, mov=1.0, regress=0.33, scale=400.0,
                 base=BASE_RATING):
        self.k = float(k)
        self.hfa = float(hfa)
        self.mov = clamp(float(mov), 0.0, 1.0)
        self.regress = clamp(float(regress), 0.0, 1.0)
        self.scale = float(scale)
        self.base = float(base)
        self.ratings: dict[str, float] = {}
        self.games_seen: dict[str, int] = {}
        self._season = None

    # ── core maths ───────────────────────────────────────────────────────────
    def rating(self, team):
        return self.ratings.get(team, self.base)

    def expected(self, home, away, neutral=False):
        """Pre-game probability that ``home`` wins."""
        edge = self.rating(home) - self.rating(away) + (0.0 if neutral else self.hfa)
        return 1.0 / (1.0 + 10 ** (-edge / self.scale))

    def _mov_multiplier(self, margin, winner_edge):
        if self.mov <= 0:
            return 1.0
        margin = max(abs(margin), 1)
        full = math.log(margin + 1.0) * (2.2 / (winner_edge * 0.001 + 2.2))
        return 1.0 + self.mov * (full - 1.0)

    def new_season(self):
        """Pull every rating part-way back to the mean between seasons."""
        for team in list(self.ratings):
            self.ratings[team] = self.base + (1 - self.regress) * (self.ratings[team] - self.base)
            self.games_seen[team] = int(self.games_seen.get(team, 0) * 0.25)

    def observe(self, home, away, home_score, away_score, neutral=False):
        """Apply one completed game and return the pre-game home win prob."""
        exp_home = self.expected(home, away, neutral)
        if home_score == away_score:
            result = 0.5
        else:
            result = 1.0 if home_score > away_score else 0.0

        margin = home_score - away_score
        winner_edge = (self.rating(home) - self.rating(away) + (0 if neutral else self.hfa))
        if result == 0.0:
            winner_edge = -winner_edge
        mult = self._mov_multiplier(margin, max(winner_edge, -300.0))

        delta = self.k * mult * (result - exp_home)
        self.ratings[home] = self.rating(home) + delta
        self.ratings[away] = self.rating(away) - delta
        self.games_seen[home] = self.games_seen.get(home, 0) + 1
        self.games_seen[away] = self.games_seen.get(away, 0) + 1
        return exp_home

    # ── replay ───────────────────────────────────────────────────────────────
    def replay(self, games):
        """Walk the game log forward.

        ``games`` must be sorted by date and each entry is a dict with keys
        home, away, home_score, away_score, final, season, neutral.

        Returns a list of per-game records carrying the *pre-game* state, which
        is what downstream models are allowed to see.
        """
        out = []
        for g in games:
            season = g.get('season')
            if self._season is not None and season != self._season:
                self.new_season()
            self._season = season

            home, away = g['home'], g['away']
            neutral = bool(g.get('neutral'))
            pre = {
                'game_id': g.get('game_id'),
                'date': g.get('date'),
                'home': home,
                'away': away,
                'home_elo': self.rating(home),
                'away_elo': self.rating(away),
                'home_elo_games': self.games_seen.get(home, 0),
                'away_elo_games': self.games_seen.get(away, 0),
                'elo_home_prob': self.expected(home, away, neutral),
                'final': bool(g.get('final')),
            }
            out.append(pre)
            counts = (g.get('final')
                      and not g.get('preseason')
                      and g.get('home_score') is not None
                      and g.get('away_score') is not None)
            if counts:
                self.observe(home, away, g['home_score'], g['away_score'], neutral)
        return out

    def snapshot(self):
        return {
            'ratings': {t: round(r, 2) for t, r in sorted(self.ratings.items())},
            'games_seen': dict(self.games_seen),
            'params': {'k': self.k, 'hfa': self.hfa, 'mov': self.mov,
                       'regress': self.regress, 'scale': self.scale},
        }

    def load(self, snap):
        if not snap:
            return self
        self.ratings = {t: float(v) for t, v in (snap.get('ratings') or {}).items()}
        self.games_seen = {t: int(v) for t, v in (snap.get('games_seen') or {}).items()}
        return self


def shrink_probability(prob, games_seen, full_confidence=20, floor=0.30):
    """Pull a prediction toward a coin flip when a team has barely played.

    Early in a season an Elo edge is mostly noise; this keeps opening-week
    predictions honest instead of over-confident.
    """
    weight = clamp(games_seen / float(full_confidence), floor, 1.0)
    return 0.5 + weight * (prob - 0.5)
