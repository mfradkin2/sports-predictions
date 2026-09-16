"""Synthetic fixtures shared by the tests."""
from __future__ import annotations

import random
from datetime import date, timedelta

TEAMS = [f'Team {chr(65 + i)}' for i in range(10)]


def synthetic_rows(n_days=60, seed=3, strength_spread=0.9, start=None):
    """A season where team strength really does drive results.

    Teams are given a fixed latent strength, games are simulated from it, and
    the enriched-CSV columns are filled in the same shape the R ingest writes.
    A model that works should recover the ordering.
    """
    rng = random.Random(seed)
    start = start or (date.today() - timedelta(days=n_days))
    strength = {t: (i - 4.5) / 4.5 * strength_spread for i, t in enumerate(TEAMS)}
    # Game ids derive from the start date so two different windows produce
    # genuinely different games, the way consecutive real fetches do.
    rows, gid = [], int(start.strftime('%Y%m%d')) * 100
    for day in range(n_days + 10):
        d = start + timedelta(days=day)
        order = TEAMS[:]
        rng.shuffle(order)
        for i in range(0, len(order) - 1, 2):
            away, home = order[i], order[i + 1]
            future = d > date.today()
            edge = strength[home] - strength[away] + 0.15
            p_home = 1 / (1 + pow(2.718281828, -2.2 * edge))
            hs = max(int(rng.gauss(4.5 + 2.0 * edge, 2.6)), 0)
            as_ = max(int(rng.gauss(4.5 - 2.0 * edge, 2.6)), 0)
            if hs == as_:
                hs += 1 if rng.random() < p_home else 0
                as_ += 1 if hs == as_ else 0
            gid += 1
            rows.append({
                'game_date': str(d),
                'away_team': away, 'home_team': home,
                'status': 'Scheduled' if future else 'Final',
                'winner': '' if future else (home if hs > as_ else away),
                'favored_team': home if p_home >= 0.5 else away,
                'away_win_probability': round(1 - p_home, 4),
                'home_win_probability': round(p_home, 4),
                'game_id': str(gid),
                'away_score': '0' if future else str(as_),
                'home_score': '0' if future else str(hs),
                'away_record': '', 'home_record': '',
                'away_win_pct': 0.5, 'home_win_pct': 0.5,
                'away_pyth_pct': 0.5, 'home_pyth_pct': 0.5,
                'away_run_diff_pg': 0, 'home_run_diff_pg': 0,
                'away_rs_pg': 4.5, 'home_rs_pg': 4.5,
                'away_ra_pg': 4.5, 'home_ra_pg': 4.5,
                'away_era': 4.0, 'home_era': 4.0,
                'game_start_utc': f'{d}T23:10Z',
                'game_time': '11:10 PM ET',
            })
    return rows


def player_pool(teams=None, seed=5):
    """A small MLB-shaped player pool, expressed as season totals."""
    rng = random.Random(seed)
    pool = {}
    for t in (teams or TEAMS):
        key = t.lower().replace(' ', '')
        roster = []
        for i in range(6):
            gp = rng.randint(110, 150)
            hits = int(gp * 3.7 * rng.uniform(.22, .31))
            roster.append({
                'id': f'b{i}', 'name': f'{t} Batter {i}', 'short': f'B{i}',
                'pos': 'LF', 'team': t, 'headshot': '',
                'stats': {'gp': gp, 'ab': int(gp * 3.7), 'hits': hits,
                          'hr': rng.randint(5, 40), 'rbi': rng.randint(30, 110),
                          'runs': int(hits * .45), 'doubles': int(hits * .19),
                          'triples': rng.randint(0, 5), 'sb': rng.randint(0, 25)},
            })
        for i in range(2):
            starts = rng.randint(24, 32)
            ip = starts * rng.uniform(5.0, 6.3)
            roster.append({
                'id': f'p{i}', 'name': f'{t} Pitcher {i}', 'short': f'P{i}',
                'pos': 'P', 'team': t, 'headshot': '',
                'stats': {'gp': starts, 'starts': starts, 'ip': round(ip, 1),
                          'p_so': int(ip * rng.uniform(.8, 1.3)),
                          'p_er': int(ip * .45), 'p_h': int(ip * .9)},
            })
        pool[key] = roster
    return pool
