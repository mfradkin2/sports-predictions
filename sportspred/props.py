"""Player prop projections.

For every player we build a per-game rate from their season line, adjust it for
the specific matchup, and then put a distribution around it so a line can be
turned into a probability:

  rate  ->  opponent strength  ->  projected game environment  ->  home/away
        ->  distribution (Poisson / negative binomial / normal)  ->  P(over)

Counting stats are Poisson unless they are visibly over-dispersed (strikeouts,
receptions, rebounds), in which case a negative binomial with a fitted
dispersion is used. Volume stats (yards, points, saves) are normal with a
variance that grows with the mean.
"""
from __future__ import annotations

import math
from collections import defaultdict

from .config import props_for, quota_for
from .espn import find_team, norm_team
from .util import clamp, negbin_sf, norm_sf, num, poisson_sf

# Ceiling on any per-game rate we will believe out of the raw feed; anything
# above it means we were handed a season total rather than an average.
PER_GAME_MAX = {
    'pts': 45, 'reb': 25, 'ast': 18, 'fg3': 9, 'stl': 6, 'blk': 7, 'tov': 9,
    'min': 48, 'fga': 35, 'fta': 30,
    'hits': 5, 'hr': 3, 'rbi': 7, 'runs': 5, 'doubles': 3, 'triples': 2,
    'bb': 5, 'so': 5, 'sb': 4, 'ab': 7,
    'ip': 9.5, 'p_so': 18, 'p_er': 10, 'p_h': 14, 'p_bb': 8,
    'pass_yds': 520, 'pass_td': 7, 'pass_att': 62, 'pass_cmp': 45, 'pass_int': 5,
    'rush_yds': 260, 'rush_att': 38, 'rush_td': 4,
    'rec': 16, 'rec_yds': 260, 'rec_td': 4, 'targets': 20,
    'tackles': 18, 'sacks': 4,
    'goals': 2.5, 'assists': 3.5, 'points': 5, 'sog': 12, 'blocks': 8,
    'saves': 55, 'ga': 9, 'toi': 30,
}

RATE_STATS = {'avg', 'obp', 'slg', 'ops', 'era', 'whip', 'sv_pct', 'gaa', 'gp',
              'starts', 'toi', 'min'}

POSITION_GROUP = {
    'baseball': lambda pos: 'pitcher' if pos.upper() in ('P', 'SP', 'RP') else 'batter',
    'hockey': lambda pos: 'goalie' if pos.upper() == 'G' else 'skater',
    'basketball': lambda pos: 'skater',
    'football': lambda pos: (
        'qb' if pos.upper() == 'QB' else
        'rb' if pos.upper() in ('RB', 'FB', 'HB') else
        'wr' if pos.upper() in ('WR', 'TE') else
        'def' if pos.upper() in ('LB', 'DE', 'DT', 'CB', 'S', 'SS', 'FS',
                                 'MLB', 'OLB', 'ILB', 'NT', 'DB', 'EDGE') else ''
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
#  Per-game rates
# ─────────────────────────────────────────────────────────────────────────────
def per_game(stats):
    """Normalise a season line to per-game rates.

    ESPN mixes season totals and per-game averages inside one payload, so the
    decision is made per stat but informed by the whole block:

    * a field ESPN itself named ``avgPoints`` or ``pointsPerGame`` is already
      a rate and is left alone;
    * a value above any plausible per-game figure is obviously a total;
    * anything else is read as a total too *if* the block clearly holds totals,
      which is what stops a genuine 2-triples season from being published as
      two triples a game.
    """
    gp = num(stats.get('gp')) or 0
    already_avg = set(stats.get('__avg__') or [])
    values = {k: num(v) for k, v in stats.items()
              if k not in ('__avg__', 'gp_est') and num(v) is not None}
    avg_counting = {k for k in already_avg if k not in RATE_STATS}

    # Does this block hold season totals?
    #  * A feed that labels nothing as an average (the NFL, NHL and MLB
    #    listings) is totals throughout, however small the numbers — two games
    #    into a season nothing exceeds a per-game ceiling, so the magnitude
    #    test alone would publish totals as rates.
    #  * A mixed feed (the NBA listing marks most columns avg*) is judged by
    #    magnitude for the unlabelled columns.
    if gp >= 1 and not avg_counting:
        block_is_totals = True
    else:
        block_is_totals = gp > 1 and any(
            PER_GAME_MAX.get(k) is not None and v > PER_GAME_MAX[k]
            for k, v in values.items() if k not in already_avg and k not in RATE_STATS)

    out = {}
    for key, v in values.items():
        if key in RATE_STATS or key in already_avg:
            out[key] = v
            continue
        cap = PER_GAME_MAX.get(key)
        if cap is not None and v > cap:
            if gp >= 1:
                out[key] = v / gp
            continue                      # a total with no games to divide by
        out[key] = v / gp if (block_is_totals and gp >= 1) else v
    out['gp'] = gp
    return out


def derive(sport, rates):
    """Add the composite rates the prop list asks for."""
    r = dict(rates)
    g = lambda k, d=0.0: float(r.get(k, d) or 0.0)  # noqa: E731

    if sport == 'baseball':
        singles = max(g('hits') - g('doubles') - g('triples') - g('hr'), 0.0)
        r['hits_pg'] = g('hits')
        r['hr_pg'] = g('hr')
        r['rbi_pg'] = g('rbi')
        r['runs_pg'] = g('runs')
        r['sb_pg'] = g('sb')
        has_split = any(k in r for k in ('doubles', 'triples', 'hr'))
        if 'tb' in r and g('tb') > 0:
            r['tb_pg'] = g('tb')              # the feed's own total-bases column
        elif has_split:
            r['tb_pg'] = singles + 2 * g('doubles') + 3 * g('triples') + 4 * g('hr')
        else:
            # Without the extra-base breakdown, scale hits by the league-typical
            # bases per hit rather than counting every hit as a single.
            r['tb_pg'] = g('hits') * 1.6
        ip = g('ip')
        r['p_so_pg'] = g('p_so')
        r['p_er_pg'] = g('p_er')
        r['p_h_pg'] = g('p_h')
        r['outs_pg'] = ip * 3.0 if ip else 0.0
    elif sport == 'basketball':
        r['pts_pg'] = g('pts')
        r['reb_pg'] = g('reb')
        r['ast_pg'] = g('ast')
        r['fg3_pg'] = g('fg3')
        r['stlblk_pg'] = g('stl') + g('blk')
        r['pra_pg'] = g('pts') + g('reb') + g('ast')
        r['pr_pg'] = g('pts') + g('reb')
        r['pa_pg'] = g('pts') + g('ast')
    elif sport == 'football':
        r['pass_yds_pg'] = g('pass_yds')
        r['pass_td_pg'] = g('pass_td')
        r['pass_att_pg'] = g('pass_att')
        r['pass_cmp_pg'] = g('pass_cmp')
        r['pass_int_pg'] = g('pass_int')
        r['rush_yds_pg'] = g('rush_yds')
        r['rush_att_pg'] = g('rush_att')
        r['rec_pg'] = g('rec')
        r['rec_yds_pg'] = g('rec_yds')
        r['scrim_yds_pg'] = g('rush_yds') + g('rec_yds')
        r['td_pg'] = g('rush_td') + g('rec_td')
        r['tackles_pg'] = g('tackles')
        r['sacks_pg'] = g('sacks')
    elif sport == 'hockey':
        r['goals_pg'] = g('goals')
        r['assists_pg'] = g('assists')
        r['points_pg'] = g('points') or (g('goals') + g('assists'))
        r['sog_pg'] = g('sog')
        r['blocks_pg'] = g('blocks')
        r['saves_pg'] = g('saves')
        r['ga_pg'] = g('ga')
    return r


# ─────────────────────────────────────────────────────────────────────────────
#  Matchup environment
# ─────────────────────────────────────────────────────────────────────────────
def team_environment(games, cfg):
    """Season-to-date scoring and scoring-allowed rates for every team.

    Small samples are shrunk toward the league average so a team that has
    played twice does not produce a 40% matchup adjustment.
    """
    scored, allowed, played = defaultdict(float), defaultdict(float), defaultdict(int)
    for g in games:
        if not g['final']:
            continue
        scored[g['home']] += g['home_score']
        allowed[g['home']] += g['away_score']
        scored[g['away']] += g['away_score']
        allowed[g['away']] += g['home_score']
        played[g['home']] += 1
        played[g['away']] += 1

    lg_team_avg = cfg.get('avg_total', 10.0) / 2.0
    total_games = sum(played.values())
    if total_games:
        lg_team_avg = sum(scored.values()) / total_games

    prior_games = 8.0
    env = {}
    for team in set(list(scored) + list(allowed)):
        n = played[team]
        off = (scored[team] + prior_games * lg_team_avg) / (n + prior_games)
        de = (allowed[team] + prior_games * lg_team_avg) / (n + prior_games)
        env[norm_team(team)] = {
            'off': off, 'def': de, 'gp': n,
            'off_idx': off / lg_team_avg if lg_team_avg else 1.0,
            'def_idx': de / lg_team_avg if lg_team_avg else 1.0,
        }
    return {'teams': env, 'league_avg': lg_team_avg}


def expected_scores(env, home, away, home_win_prob, cfg):
    """Projected score for each side — the game environment props hang off.

    A rate-on-rate product (the log5 idea applied to scoring) with a nudge from
    the win probability, so a heavy favourite is not projected to lose.
    """
    lg = env['league_avg'] or (cfg.get('avg_total', 10.0) / 2.0)
    h = env['teams'].get(norm_team(home), {})
    a = env['teams'].get(norm_team(away), {})
    h_off = h.get('off_idx', 1.0)
    h_def = h.get('def_idx', 1.0)
    a_off = a.get('off_idx', 1.0)
    a_def = a.get('def_idx', 1.0)

    home_hfa = 1.0 + (cfg.get('home_edge', 0.54) - 0.5) * 0.18
    exp_home = lg * h_off * a_def * home_hfa
    exp_away = lg * a_off * h_def / home_hfa

    # Reconcile with the win probability: a 70% favourite should out-score.
    tilt = (home_win_prob - 0.5) * 0.30
    exp_home *= (1 + tilt)
    exp_away *= (1 - tilt)
    return {'home': max(exp_home, 0.1), 'away': max(exp_away, 0.1), 'league_avg': lg}


def _boost(value, strength, lo=0.75, hi=1.30):
    """Fold a ratio into an adjustment, damped by ``strength``."""
    return clamp(1.0 + strength * (value - 1.0), lo, hi)


def matchup_factor(prop_key, sport, side, exp, env, home, away, is_home):
    """Multiplier applied to a player's baseline rate for this matchup."""
    lg = exp['league_avg'] or 1.0
    own_expected = exp['home'] if is_home else exp['away']
    opp_expected = exp['away'] if is_home else exp['home']
    opp = env['teams'].get(norm_team(away if is_home else home), {})

    offence_like = True
    if sport == 'baseball' and side == 'pitcher':
        offence_like = False
    if sport == 'hockey' and side == 'goalie':
        offence_like = False
    if sport == 'football' and side == 'def':
        offence_like = False

    if offence_like:
        # Scale with how much this team is projected to score.
        factor = _boost(own_expected / lg, 0.65)
        # Volume-only props (attempts, minutes, shots) move less than scoring.
        if prop_key in ('pass_att', 'pass_cmp', 'rush_att', 'rec', 'sog',
                        'tackles', 'reb', 'ast', 'blocks'):
            factor = _boost(own_expected / lg, 0.35)
    else:
        # Pitchers, goalies and defences benefit from a weak opponent.
        factor = _boost(lg / max(opp_expected, 0.1), 0.55)
        if prop_key in ('k', 'outs'):
            # Strikeouts track the opponent's contact quality, not their runs.
            factor = _boost(opp.get('off_idx', 1.0) ** -0.35, 0.5, 0.85, 1.18)
        if prop_key == 'saves':
            # A goalie faces more shots behind a worse team, not a better one.
            factor = _boost(opp.get('off_idx', 1.0), 0.45, 0.82, 1.22)

    # Playing at home is worth a little offence in every sport.
    if offence_like:
        factor *= 1.012 if is_home else 0.988
    return clamp(factor, 0.70, 1.35)


# ─────────────────────────────────────────────────────────────────────────────
#  Probability for one prop
# ─────────────────────────────────────────────────────────────────────────────
def standard_line(baseline):
    """The half-point line a book would hang on a player's usual output.

    Anchored to the season baseline rather than to our adjusted projection —
    otherwise the line moves with the projection and every prop prices at a
    coin flip. Pricing the matchup-adjusted projection against the baseline
    line is what turns the model into an actual lean.
    """
    line = round(baseline * 2.0) / 2.0
    if abs(line - round(line)) < 1e-9:       # avoid a whole-number push
        line = line + 0.5 if baseline >= line else line - 0.5
    return max(line, 0.5)


def _spread(spec, projection):
    """Standard deviation implied by this prop's distribution."""
    dist = spec.get('dist', 'poisson')
    if dist == 'normal':
        return spec['sigma'](projection)
    if dist == 'negbin':
        return math.sqrt(max(projection * float(spec.get('disp', 1.3)), 1e-6))
    return math.sqrt(max(projection, 1e-6))


def over_probability(spec, projection, baseline=None):
    """P(stat > line) for this prop, plus the line it was priced against."""
    dist = spec.get('dist', 'poisson')
    line = spec.get('line')
    if line is None:
        line = standard_line(baseline if baseline is not None else projection)
    line = float(line)
    if dist == 'normal':
        p_over = norm_sf(line, projection, spec['sigma'](projection))
    elif dist == 'negbin':
        var = projection * float(spec.get('disp', 1.3))
        p_over = negbin_sf(math.floor(line), projection, var)
    else:
        p_over = poisson_sf(math.floor(line), projection)
    return line, clamp(p_over, 0.01, 0.99)


def likely_range(spec, projection):
    """A rough 25th-75th percentile band, for showing the spread of outcomes."""
    sd = _spread(spec, projection)
    lo = max(projection - 0.674 * sd, 0.0)
    hi = projection + 0.674 * sd
    step = 1 if projection >= 10 else 0.1
    rnd = (lambda v: round(v)) if step == 1 else (lambda v: round(v, 1))
    return [rnd(lo), rnd(hi)]


def confidence(prob):
    edge = abs(prob - 0.5)
    if edge >= 0.18:
        return 'high'
    if edge >= 0.09:
        return 'med'
    return 'low'


def project_player(player, sport, group, factor, max_props=5, tuning=None):
    """Every prop we can price for one player, best signal first."""
    rates = derive(sport, per_game(player.get('stats') or {}))
    gp = rates.get('gp') or 0
    out = []
    for raw_spec in props_for(sport, group):
        spec, bias = apply_tuning(raw_spec, tuning)
        base = num(rates.get(spec['stat']))
        if base is None or base <= 0:
            continue
        projection = base * factor * bias
        if projection < spec.get('min_proj', 0.0):
            continue
        out.append(_price(spec, base, projection))
    # Most popular first, but let a genuinely strong read jump the queue.
    out.sort(key=lambda p: (p['rank'] - (3 if p['conf'] == 'high' else 0)))
    return out[:max_props], gp


LIMITED_FACTOR = 0.92      # a questionable / day-to-day player's projection


def apply_tuning(spec, tuning):
    """A copy of ``spec`` with the ledger's corrections folded in."""
    t = (tuning or {}).get(spec['key'])
    if not t:
        return spec, 1.0
    out = dict(spec)
    spread = float(t.get('spread', 1.0))
    if spec.get('dist') == 'normal' and 'sigma' in spec:
        base_sigma = spec['sigma']
        out['sigma'] = lambda mu, _b=base_sigma, _s=spread: _b(mu) * _s
    elif spec.get('dist') == 'negbin':
        out['disp'] = max(float(spec.get('disp', 1.3)) * spread * spread, 1.0)
    return out, float(t.get('bias', 1.0))


def _price(spec, baseline, projection):
    """Assemble the published record for one prop."""
    line, p_over = over_probability(spec, projection, baseline)
    sd = _spread(spec, projection)
    # Edge is how far this matchup moves the player off their own season
    # baseline, in standard deviations — not the gap to the line. On a fixed
    # line (home run 0.5, anytime touchdown) the gap to the line is a property
    # of the player, so ranking a slate by it just lists the weakest hitters
    # under a home-run line. Baseline movement is the part the model actually
    # has an opinion about.
    edge = (projection - baseline) / sd if sd > 0 else 0.0
    # Gap to the line, kept for display: it explains the probability.
    line_gap = (projection - line) / sd if sd > 0 else 0.0
    return {
        'key': spec['key'],
        'label': spec['label'],
        'unit': spec.get('unit', ''),
        'line': round(line, 1),
        'proj': round(projection, 2),
        'season': round(baseline, 2),
        'delta': round(projection - baseline, 2),
        'range': likely_range(spec, projection),
        'over': round(p_over, 4),
        'under': round(1 - p_over, 4),
        'pick': 'over' if p_over >= 0.5 else 'under',
        'pick_prob': round(max(p_over, 1 - p_over), 4),
        'conf': confidence(p_over),
        'edge': round(edge, 3),
        'line_gap': round(line_gap, 2),
        'rank': spec.get('rank', 99),
        'stat': spec.get('stat', ''),
        'dist': spec.get('dist', ''),
    }


def player_group(sport, pos):
    fn = POSITION_GROUP.get(sport)
    return fn(pos or '') if fn else ''


# ─────────────────────────────────────────────────────────────────────────────
#  Game-level assembly
# ─────────────────────────────────────────────────────────────────────────────
def _sort_key(sport, group, rates):
    """How prominent a player is — drives who appears at the top of the list."""
    if sport == 'basketball':
        return rates.get('pts_pg', 0) + 0.7 * rates.get('reb_pg', 0) + rates.get('ast_pg', 0)
    if sport == 'baseball':
        if group == 'pitcher':
            return rates.get('p_so_pg', 0) * 2 + rates.get('outs_pg', 0) / 3.0
        return rates.get('tb_pg', 0) + rates.get('hits_pg', 0)
    if sport == 'football':
        return (rates.get('pass_yds_pg', 0) / 10.0 + rates.get('scrim_yds_pg', 0) / 6.0
                + rates.get('rec_pg', 0) + rates.get('tackles_pg', 0) / 2.0)
    if sport == 'hockey':
        if group == 'goalie':
            return rates.get('saves_pg', 0)
        return rates.get('points_pg', 0) * 3 + rates.get('sog_pg', 0)
    return 0.0


def build_for_game(game_row, pool, env, cfg, sport, home_win_prob,
                   max_players=7, starters=None, injuries=None, tuning=None):
    """Prop board for one game: {'home': [...], 'away': [...]}.

    ``injuries`` is ``{team: {athlete_id: report}}`` from the injury feed. A
    player listed as out is left off the board; one listed as questionable is
    kept, marked, and projected a little lower.
    """
    home = game_row['home']
    away = game_row['away']
    exp = expected_scores(env, home, away, home_win_prob, cfg)
    out = {}

    for side, team, is_home in (('away', away, False), ('home', home, True)):
        roster = find_team(pool, team) or []
        team_injuries = find_team(injuries or {}, team) or {}
        by_group = {}
        sidelined = []
        for player in roster:
            group = player_group(sport, player.get('pos', ''))
            if not group:
                continue
            report = team_injuries.get(str(player.get('id') or ''))
            if report and report.get('level') == 'out':
                sidelined.append({'name': player.get('name', ''), 'pos': player.get('pos', ''),
                                  'status': report.get('status', 'Out'),
                                  'detail': report.get('detail', '')})
                continue
            rates = derive(sport, per_game(player.get('stats') or {}))
            if (rates.get('gp') or 0) < 1:
                continue
            factor = matchup_factor('', sport, group, exp, env, home, away, is_home)
            if report and report.get('level') == 'limited':
                factor *= LIMITED_FACTOR
            props, gp = project_player(player, sport, group, factor, tuning=tuning)
            if not props:
                continue
            # Re-price each prop with its own matchup factor.
            priced = []
            for p in props:
                spec = next((s for s in props_for(sport, group) if s['key'] == p['key']), None)
                if spec is None:
                    priced.append(p)
                    continue
                spec, bias = apply_tuning(spec, tuning)
                f = matchup_factor(p['key'], sport, group, exp, env, home, away, is_home)
                if report and report.get('level') == 'limited':
                    f *= LIMITED_FACTOR
                priced.append(_price(spec, p['season'], p['season'] * f * bias))
            entry = {
                'id': player.get('id', ''),
                'name': player.get('name', ''),
                'short': player.get('short', '') or player.get('name', ''),
                'pos': player.get('pos', ''),
                'group': group,
                'gp': int(gp),
                'headshot': player.get('headshot', ''),
                'props': priced,
                '_rank': _sort_key(sport, group, rates),
            }
            if report and report.get('level') == 'limited':
                entry['status'] = report.get('status', 'Questionable')
                entry['status_detail'] = report.get('detail', '')
            by_group.setdefault(group, []).append(entry)

        # Fill a fixed number of slots per position group, then top up from
        # whoever is left so a thin roster still produces a full board.
        entries, used = [], set()
        for group_name, slots in quota_for(sport):
            group_players = sorted(by_group.get(group_name, []), key=lambda e: -e['_rank'])
            for e in group_players[:slots]:
                entries.append(e)
                used.add(id(e))
        if len(entries) < max_players:
            rest = [e for lst in by_group.values() for e in lst if id(e) not in used]
            rest.sort(key=lambda e: -e['_rank'])
            entries.extend(rest[:max_players - len(entries)])

        entries = entries[:max_players]
        # Lead with the most prominent players, grouped by position.
        order = {g: i for i, (g, _) in enumerate(quota_for(sport))}
        entries.sort(key=lambda e: (order.get(e['group'], 99), -e['_rank']))

        # Baseball: the announced starter leads the board even if a reliever
        # has thrown more innings. This runs after the sort above, which would
        # otherwise put them straight back in rate order.
        if sport == 'baseball' and starters:
            want = (starters.get(side) or {}).get('name', '')
            if want:
                pool_all = [e for lst in by_group.values() for e in lst]
                match = next((e for e in pool_all if e['name'] == want), None)
                if match is not None:
                    match['role'] = 'Probable starter'
                    entries = [match] + [e for e in entries if e is not match]
                    entries = entries[:max_players]

        for e in entries:
            e.pop('_rank', None)
        out[side] = entries
        # Listed-out players from this team's pool, most prominent first, so
        # the matchup panel can say who is missing.
        out[f'{side}_out'] = sidelined[:6]

    out['expected'] = {'home': round(exp['home'], 2), 'away': round(exp['away'], 2)}
    return out
