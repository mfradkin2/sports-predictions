"""League, team-stat and player-prop configuration.

One place to describe every sport so the engine, the prop projector and the
renderer all stay in sync.
"""
from __future__ import annotations

import math as _math
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SITE_NAME = 'Sports Predictions'
DATA_DIR = os.path.join(BASE, 'data')
ASSET_DIR = os.path.join(BASE, 'assets')
STATE_DIR = os.path.join(BASE, 'model_state')
HISTORY_DIR = os.path.join(BASE, 'history')

# ─────────────────────────────────────────────────────────────────────────────
#  Team-level league config
# ─────────────────────────────────────────────────────────────────────────────
LEAGUES = {
    'mlb': {
        'name': 'MLB', 'emoji': '⚾', 'accent': '#3b82f6',
        'espn_path': 'baseball/mlb', 'sport': 'baseball',
        'csv_file': 'mlb_predictions.csv', 'season_label': '2026 Season',
        # Elo priors — MLB has the flattest talent curve of the four leagues.
        'elo': {'k': 4.0, 'hfa': 24.0, 'mov': 0.55, 'regress': 0.32, 'scale': 400.0},
        'home_edge': 0.54,
        'avg_total': 8.8,           # league average combined score
        'score_sigma': 4.3,
        'stats': [
            {'key': 'win_pct',     'label': 'Win%',       'lower_better': False, 'fmt': '.3f'},
            {'key': 'pyth_pct',    'label': 'Pyth Win%',  'lower_better': False, 'fmt': '.3f'},
            {'key': 'run_diff_pg', 'label': 'Run Diff/G', 'lower_better': False, 'fmt': '+.2f'},
            {'key': 'rs_pg',       'label': 'Runs/G',     'lower_better': False, 'fmt': '.2f'},
            {'key': 'ra_pg',       'label': 'Runs Allowed/G', 'lower_better': True, 'fmt': '.2f'},
            {'key': 'era',         'label': 'Team ERA',   'lower_better': True,  'fmt': '.2f'},
        ],
    },
    'nhl': {
        'name': 'NHL', 'emoji': '🏒', 'accent': '#0ea5e9',
        'espn_path': 'hockey/nhl', 'sport': 'hockey',
        'csv_file': 'nhl_2026_schedule_enriched.csv', 'season_label': '2025–26 Season',
        'elo': {'k': 6.0, 'hfa': 34.0, 'mov': 0.70, 'regress': 0.30, 'scale': 400.0},
        'home_edge': 0.55,
        'avg_total': 6.1,
        'score_sigma': 1.9,
        'stats': [
            {'key': 'pts_pct',  'label': 'Points%', 'lower_better': False, 'fmt': '.3f'},
            {'key': 'gf_pg',    'label': 'Goals For/G',     'lower_better': False, 'fmt': '.2f'},
            {'key': 'ga_pg',    'label': 'Goals Against/G', 'lower_better': True,  'fmt': '.2f'},
            {'key': 'pp_pct',   'label': 'Power Play%',     'lower_better': False, 'fmt': '.1f', 'suffix': '%'},
            {'key': 'pk_pct',   'label': 'Penalty Kill%',   'lower_better': False, 'fmt': '.1f', 'suffix': '%'},
            {'key': 'save_pct', 'label': 'Save%',           'lower_better': False, 'fmt': '.3f'},
        ],
    },
    'nba': {
        'name': 'NBA', 'emoji': '🏀', 'accent': '#ef4444',
        'espn_path': 'basketball/nba', 'sport': 'basketball',
        'csv_file': 'nba_2026_schedule_enriched.csv', 'season_label': '2025–26 Season',
        'elo': {'k': 18.0, 'hfa': 62.0, 'mov': 1.00, 'regress': 0.25, 'scale': 400.0},
        'home_edge': 0.58,
        'avg_total': 228.0,
        'score_sigma': 12.0,
        'stats': [
            {'key': 'win_pct', 'label': 'Win%',    'lower_better': False, 'fmt': '.3f'},
            {'key': 'net_rtg', 'label': 'Net Rating', 'lower_better': False, 'fmt': '+.1f'},
            {'key': 'ppg',     'label': 'Points/G', 'lower_better': False, 'fmt': '.1f'},
            {'key': 'opp_ppg', 'label': 'Opp Points/G', 'lower_better': True, 'fmt': '.1f'},
            {'key': 'pace',    'label': 'Pace',     'lower_better': False, 'fmt': '.1f'},
        ],
    },
    'nfl': {
        'name': 'NFL', 'emoji': '🏈', 'accent': '#8b5cf6',
        'espn_path': 'football/nfl', 'sport': 'football',
        'csv_file': 'nfl_2026_schedule_enriched.csv', 'season_label': '2026 Season',
        'elo': {'k': 20.0, 'hfa': 48.0, 'mov': 1.00, 'regress': 0.33, 'scale': 400.0},
        'home_edge': 0.56,
        'avg_total': 44.0,
        'score_sigma': 10.0,
        'stats': [
            {'key': 'win_pct',    'label': 'Win%',        'lower_better': False, 'fmt': '.3f'},
            {'key': 'pt_diff_pg', 'label': 'Pt Diff/G',   'lower_better': False, 'fmt': '+.1f'},
            {'key': 'ypg_off',    'label': 'Yards/G Off', 'lower_better': False, 'fmt': '.1f'},
            {'key': 'ypg_def',    'label': 'Yards/G Def', 'lower_better': True,  'fmt': '.1f'},
            {'key': 'to_margin',  'label': 'TO Margin/G', 'lower_better': False, 'fmt': '+.2f'},
        ],
    },
}

LEAGUE_ORDER = ['mlb', 'nfl', 'nba', 'nhl']

# ─────────────────────────────────────────────────────────────────────────────
#  Player statistics — internal key -> ESPN stat aliases
#
#  ESPN spells the same statistic several different ways across endpoints
#  (``avgPoints`` / ``points`` / ``PTS``), so every internal stat carries a list
#  of candidates that are matched after normalising to lowercase alphanumerics.
# ─────────────────────────────────────────────────────────────────────────────
STAT_ALIASES = {
    'basketball': {
        'gp':      ['gamesplayed', 'gp', 'games'],
        'min':     ['avgminutes', 'minutespergame', 'minutes', 'min', 'mpg'],
        'pts':     ['avgpoints', 'pointspergame', 'points', 'pts', 'ppg'],
        'reb':     ['avgrebounds', 'reboundspergame', 'rebounds', 'reb', 'totalrebounds', 'rpg'],
        'ast':     ['avgassists', 'assistspergame', 'assists', 'ast', 'apg'],
        'fg3':     ['avgthreepointfieldgoalsmade', 'threepointfieldgoalsmade',
                    'threepointmade', '3ptm', 'threepointfieldgoalsmadepergame'],
        'stl':     ['avgsteals', 'steals', 'stl'],
        'blk':     ['avgblocks', 'blocks', 'blk'],
        'tov':     ['avgturnovers', 'turnovers', 'to', 'tov'],
        'fga':     ['avgfieldgoalsattempted', 'fieldgoalsattempted', 'fga'],
        'fta':     ['avgfreethrowsattempted', 'freethrowsattempted', 'fta'],
    },
    'baseball': {
        'gp':      ['gamesplayed', 'gp', 'g'],
        'ab':      ['atbats', 'ab'],
        'hits':    ['hits', 'h'],
        'tb':      ['totalbases', 'tb'],
        'hr':      ['homeruns', 'hr'],
        'rbi':     ['rbis', 'rbi', 'runsbattedin'],
        'runs':    ['runs', 'r'],
        'doubles': ['doubles', '2b'],
        'triples': ['triples', '3b'],
        'bb':      ['walks', 'basesonballs', 'bb'],
        'so':      ['strikeouts', 'so', 'k'],
        'sb':      ['stolenbases', 'sb'],
        'avg':     ['avg', 'battingaverage'],
        'obp':     ['onbasepct', 'obp', 'onbasepercentage'],
        'slg':     ['slugavg', 'slg', 'sluggingpercentage'],
        'ops':     ['ops', 'onbaseplusslugging'],
        # pitching
        'ip':      ['inningspitched', 'ip'],
        'p_so':    ['strikeouts', 'so', 'k'],
        'p_er':    ['earnedruns', 'er'],
        'p_h':     ['hits', 'h'],
        'p_bb':    ['walks', 'bb'],
        'era':     ['era', 'earnedrunaverage'],
        'whip':    ['whip'],
        'starts':  ['gamesstarted', 'gs'],
    },
    'football': {
        'gp':        ['gamesplayed', 'gp', 'games'],
        'pass_yds':  ['passingyards', 'passyards', 'yds', 'netpassingyards'],
        'pass_td':   ['passingtouchdowns', 'passtouchdowns', 'td'],
        'pass_att':  ['passingattempts', 'attempts', 'att'],
        'pass_cmp':  ['completions', 'cmp'],
        'pass_int':  ['interceptions', 'int', 'interceptionsthrown'],
        'rush_yds':  ['rushingyards', 'rushyards'],
        'rush_att':  ['rushingattempts', 'rushattempts', 'carries'],
        'rush_td':   ['rushingtouchdowns', 'rushtouchdowns'],
        'rec':       ['receptions', 'rec'],
        'rec_yds':   ['receivingyards', 'recyards'],
        'rec_td':    ['receivingtouchdowns', 'rectouchdowns'],
        'targets':   ['receivingtargets', 'targets', 'tgts'],
        'tackles':   ['totaltackles', 'tackles', 'combinedtackles'],
        'sacks':     ['sacks'],
    },
    'hockey': {
        'gp':      ['gamesplayed', 'gp', 'games', 'gamesplayedtotal', 'gamesskated'],
        'goals':   ['goals', 'g'],
        'assists': ['assists', 'a'],
        'points':  ['points', 'pts'],
        'sog':     ['shotsontarget', 'shotsongoal', 'shotstotal', 'shots', 'sog'],
        'toi':     ['timeonicepergame', 'avgtimeonice', 'timeonice', 'toi'],
        'blocks':  ['blockedshots', 'blocks'],
        'hits':    ['hits'],
        'ppg_pts': ['powerplaypoints'],
        # goalie
        'saves':   ['saves'],
        'sv_pct':  ['savepct', 'savepercentage'],
        'gaa':     ['goalsagainstaverage', 'avggoalsagainst', 'gaa'],
        'ga':      ['goalsagainst'],
        'starts':  ['gamesstarted', 'gs'],
    },
}

# ─────────────────────────────────────────────────────────────────────────────
#  Prop definitions
#
#  ``stat``    internal per-game rate key produced by props.py
#  ``line``    fixed number, or None to derive a half-point line from the
#              projection (the standard sportsbook convention)
#  ``dist``    'poisson' | 'negbin' | 'normal'
#  ``disp``    negbin variance multiplier (var = mean * disp)
#  ``sigma``   callable(mu) -> stdev, for 'normal'
#  ``min_proj``hide the prop when the projection is below this
#  ``rank``    display order; lower is more popular
# ─────────────────────────────────────────────────────────────────────────────
def _sig(a, b):
    """sigma(mu) = a*mu + b — fits yardage and similar continuous volume stats."""
    return lambda mu: max(a * mu + b, 0.6)


def _sqrt_sig(c, floor):
    """sigma(mu) = c*sqrt(mu) — fits basketball box-score spread far better
    than a linear rule, because scoring is a sum of many small independent
    possessions."""
    return lambda mu: max(c * _math.sqrt(max(mu, 1.0)), floor)


PROPS = {
    'baseball': {
        'batter': [
            {'key': 'hits',   'label': 'Hits',        'stat': 'hits_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 1, 'min_proj': 0.35, 'unit': 'H'},
            {'key': 'tb',     'label': 'Total Bases', 'stat': 'tb_pg',   'line': 1.5,
             'dist': 'negbin', 'disp': 1.45, 'rank': 2, 'min_proj': 0.6, 'unit': 'TB'},
            {'key': 'hr',     'label': 'Home Run',    'stat': 'hr_pg',   'line': 0.5,
             'dist': 'poisson', 'rank': 3, 'min_proj': 0.05, 'unit': 'HR'},
            {'key': 'rbi',    'label': 'RBIs',        'stat': 'rbi_pg',  'line': 0.5,
             'dist': 'poisson', 'rank': 4, 'min_proj': 0.2, 'unit': 'RBI'},
            {'key': 'runs',   'label': 'Runs Scored', 'stat': 'runs_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 5, 'min_proj': 0.2, 'unit': 'R'},
            {'key': 'hits2',  'label': 'Hits (2+)',   'stat': 'hits_pg', 'line': 1.5,
             'dist': 'poisson', 'rank': 6, 'min_proj': 0.7, 'unit': 'H'},
            {'key': 'sb',     'label': 'Stolen Base', 'stat': 'sb_pg',   'line': 0.5,
             'dist': 'poisson', 'rank': 7, 'min_proj': 0.08, 'unit': 'SB'},
        ],
        'pitcher': [
            {'key': 'k',      'label': 'Strikeouts',   'stat': 'p_so_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.25, 'rank': 1, 'min_proj': 2.0, 'unit': 'K'},
            {'key': 'outs',   'label': 'Outs Recorded', 'stat': 'outs_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.0, 4.6), 'rank': 2, 'min_proj': 9.0, 'unit': 'Outs'},
            {'key': 'er',     'label': 'Earned Runs',  'stat': 'p_er_pg', 'line': 2.5,
             'dist': 'negbin', 'disp': 1.5, 'rank': 3, 'min_proj': 0.8, 'unit': 'ER'},
            {'key': 'p_hits', 'label': 'Hits Allowed', 'stat': 'p_h_pg',  'line': None,
             'dist': 'negbin', 'disp': 1.3, 'rank': 4, 'min_proj': 2.0, 'unit': 'H'},
        ],
    },
    'basketball': {
        'skater': [
            {'key': 'pts',   'label': 'Points',   'stat': 'pts_pg', 'line': None,
             'dist': 'normal', 'sigma': _sqrt_sig(1.72, 2.5), 'rank': 1, 'min_proj': 6.0, 'unit': 'PTS'},
            {'key': 'reb',   'label': 'Rebounds', 'stat': 'reb_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.35, 'rank': 2, 'min_proj': 2.5, 'unit': 'REB'},
            {'key': 'ast',   'label': 'Assists',  'stat': 'ast_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.45, 'rank': 3, 'min_proj': 1.5, 'unit': 'AST'},
            {'key': 'fg3',   'label': '3-Pointers Made', 'stat': 'fg3_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.15, 'rank': 4, 'min_proj': 0.8, 'unit': '3PM'},
            {'key': 'pra',   'label': 'Pts + Reb + Ast', 'stat': 'pra_pg', 'line': None,
             'dist': 'normal', 'sigma': _sqrt_sig(1.95, 3.5), 'rank': 5, 'min_proj': 12.0, 'unit': 'PRA'},
            {'key': 'pr',    'label': 'Pts + Reb', 'stat': 'pr_pg', 'line': None,
             'dist': 'normal', 'sigma': _sqrt_sig(1.85, 3.0), 'rank': 6, 'min_proj': 9.0, 'unit': 'P+R'},
            {'key': 'pa',    'label': 'Pts + Ast', 'stat': 'pa_pg', 'line': None,
             'dist': 'normal', 'sigma': _sqrt_sig(1.85, 3.0), 'rank': 7, 'min_proj': 9.0, 'unit': 'P+A'},
            {'key': 'stlblk', 'label': 'Steals + Blocks', 'stat': 'stlblk_pg', 'line': None,
             'dist': 'poisson', 'rank': 8, 'min_proj': 0.8, 'unit': 'S+B'},
        ],
    },
    'football': {
        'qb': [
            {'key': 'pass_yds', 'label': 'Passing Yards', 'stat': 'pass_yds_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.22, 24.0), 'rank': 1, 'min_proj': 100.0, 'unit': 'YDS'},
            {'key': 'pass_td',  'label': 'Passing TDs',   'stat': 'pass_td_pg',  'line': 1.5,
             'dist': 'poisson', 'rank': 2, 'min_proj': 0.7, 'unit': 'TD'},
            {'key': 'pass_cmp', 'label': 'Completions',   'stat': 'pass_cmp_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.16, 2.2), 'rank': 3, 'min_proj': 10.0, 'unit': 'CMP'},
            {'key': 'pass_att', 'label': 'Pass Attempts', 'stat': 'pass_att_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.15, 2.6), 'rank': 4, 'min_proj': 15.0, 'unit': 'ATT'},
            {'key': 'pass_int', 'label': 'Interceptions', 'stat': 'pass_int_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 5, 'min_proj': 0.2, 'unit': 'INT'},
            {'key': 'qb_rush',  'label': 'Rushing Yards', 'stat': 'rush_yds_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.55, 9.0), 'rank': 6, 'min_proj': 12.0, 'unit': 'YDS'},
        ],
        'rb': [
            {'key': 'rush_yds', 'label': 'Rushing Yards', 'stat': 'rush_yds_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.50, 12.0), 'rank': 1, 'min_proj': 20.0, 'unit': 'YDS'},
            {'key': 'rush_att', 'label': 'Rush Attempts', 'stat': 'rush_att_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.4, 'rank': 2, 'min_proj': 5.0, 'unit': 'ATT'},
            {'key': 'scrim',    'label': 'Rush + Rec Yards', 'stat': 'scrim_yds_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.48, 14.0), 'rank': 3, 'min_proj': 30.0, 'unit': 'YDS'},
            {'key': 'rec',      'label': 'Receptions', 'stat': 'rec_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.5, 'rank': 4, 'min_proj': 1.2, 'unit': 'REC'},
            {'key': 'anytd',    'label': 'Anytime TD', 'stat': 'td_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 5, 'min_proj': 0.15, 'unit': 'TD'},
        ],
        'wr': [
            {'key': 'rec_yds', 'label': 'Receiving Yards', 'stat': 'rec_yds_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.62, 11.0), 'rank': 1, 'min_proj': 20.0, 'unit': 'YDS'},
            {'key': 'rec',     'label': 'Receptions',      'stat': 'rec_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.5, 'rank': 2, 'min_proj': 1.5, 'unit': 'REC'},
            {'key': 'anytd',   'label': 'Anytime TD',      'stat': 'td_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 3, 'min_proj': 0.12, 'unit': 'TD'},
            {'key': 'scrim',   'label': 'Rush + Rec Yards', 'stat': 'scrim_yds_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.60, 12.0), 'rank': 4, 'min_proj': 25.0, 'unit': 'YDS'},
        ],
        'def': [
            {'key': 'tackles', 'label': 'Tackles + Assists', 'stat': 'tackles_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.4, 'rank': 1, 'min_proj': 3.0, 'unit': 'TKL'},
            {'key': 'sacks',   'label': 'Sacks', 'stat': 'sacks_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 2, 'min_proj': 0.2, 'unit': 'SACK'},
        ],
    },
    'hockey': {
        'skater': [
            {'key': 'sog',     'label': 'Shots on Goal', 'stat': 'sog_pg', 'line': None,
             'dist': 'negbin', 'disp': 1.3, 'rank': 1, 'min_proj': 1.2, 'unit': 'SOG'},
            {'key': 'points',  'label': 'Points',   'stat': 'points_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 2, 'min_proj': 0.25, 'unit': 'PTS'},
            {'key': 'goal',    'label': 'Anytime Goal', 'stat': 'goals_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 3, 'min_proj': 0.07, 'unit': 'G'},
            {'key': 'assists', 'label': 'Assists',  'stat': 'assists_pg', 'line': 0.5,
             'dist': 'poisson', 'rank': 4, 'min_proj': 0.15, 'unit': 'A'},
            {'key': 'points2', 'label': 'Points (2+)', 'stat': 'points_pg', 'line': 1.5,
             'dist': 'poisson', 'rank': 5, 'min_proj': 0.7, 'unit': 'PTS'},
            {'key': 'blocks',  'label': 'Blocked Shots', 'stat': 'blocks_pg', 'line': None,
             'dist': 'poisson', 'rank': 6, 'min_proj': 1.0, 'unit': 'BLK'},
        ],
        'goalie': [
            {'key': 'saves',   'label': 'Saves', 'stat': 'saves_pg', 'line': None,
             'dist': 'normal', 'sigma': _sig(0.0, 6.4), 'rank': 1, 'min_proj': 14.0, 'unit': 'SV'},
            {'key': 'ga',      'label': 'Goals Against', 'stat': 'ga_pg', 'line': 2.5,
             'dist': 'poisson', 'rank': 2, 'min_proj': 1.2, 'unit': 'GA'},
        ],
    },
}

# How many players of each kind make the board for one team. Without a quota a
# single ranking buries the hitters behind the pitchers (and the skaters behind
# the goalie), because their headline numbers are on different scales.
GROUP_QUOTA = {
    'baseball': [('pitcher', 1), ('batter', 6)],
    'hockey': [('goalie', 1), ('skater', 6)],
    'basketball': [('skater', 7)],
    'football': [('qb', 1), ('rb', 2), ('wr', 3), ('def', 1)],
}


def props_for(sport, group):
    return PROPS.get(sport, {}).get(group, [])


def quota_for(sport):
    return GROUP_QUOTA.get(sport, [('skater', 7)])
