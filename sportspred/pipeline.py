"""Run one league end to end: ingest -> learn -> predict -> props -> payload."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from . import espn, model, odds, props as props_mod
from . import config
from .config import LEAGUES
from .glm import score
from .learn import MODEL_VERSION, FrozenBoards, LeagueMemory, PropsLedger
from .model import FEATURE_LABELS
from .util import (Http, clamp, format_eastern, now_iso, num, parse_iso, read_csv,
                   read_json, short_name, today_utc, write_json)

RECENT_DAYS = 21        # how far back the Results view can reach
LIVE_RECENT_DAYS = 4    # finished games kept in the first-paint payload
UPCOMING_DAYS = 14
GRADE_CAP = 40          # box scores fetched per run to grade finished props
# Price props only this close to kickoff: further out the lineups are guesses
# and the payload balloons. Football plays once a week with lineups known days
# ahead, so it gets the whole week.
PROPS_AHEAD_DAYS = {'nfl': 6}
PROPS_AHEAD_DEFAULT = 2
STARTER_COEF = 0.12     # log-odds per run of ERA between probable starters
STARTER_MIN_STARTS = 5
STARTER_CAP = 0.35


def started(game, now=None):
    """Has this game's scheduled start already passed?"""
    now = now or datetime.now(timezone.utc)
    start = parse_iso((game.get('row') or {}).get('game_start_utc'))
    if start is not None:
        return start <= now
    # No timestamp: fall back to the calendar date, which is only wrong for
    # games still to be played later today.
    return game['date'] < now.date()


def is_preseason(game):
    return bool(game.get('preseason'))


def merge_sources(csv_rows, archive_rows):
    """Current window (authoritative, carries team stats) + everything older."""
    seen = set()
    merged = []
    for r in csv_rows:
        key = (r.get('game_id') or '').strip() or '|'.join([
            (r.get('game_date') or '')[:10], r.get('away_team', ''), r.get('home_team', '')])
        seen.add(key)
        merged.append(r)
    for r in archive_rows:
        key = (r.get('game_id') or '').strip() or '|'.join([
            (r.get('game_date') or '')[:10], r.get('away_team', ''), r.get('home_team', '')])
        if key not in seen:
            merged.append(r)
    merged.sort(key=lambda r: ((r.get('game_date') or '')[:10], r.get('game_id') or ''))
    return merged


def run(league_key, fetch_props=True, http=None, tune=True):
    cfg = LEAGUES[league_key]
    csv_path = os.path.join(os.path.dirname(config.DATA_DIR), cfg['csv_file'])
    csv_rows = read_csv(csv_path)

    memory = LeagueMemory(league_key)
    added = memory.merge_archive(csv_rows, league_key)
    rows = merge_sources(csv_rows, memory.archive_rows())

    # ── learn ───────────────────────────────────────────────────────────────
    prev = memory.previous_best()
    prev_elo = (prev.get('params') or {}).get('elo_params')
    trained = model.train(league_key, rows, cfg,
                          elo_params=prev_elo if not tune else None, tune=tune)

    candidate = {'elo_params': trained['elo_params'], 'l2': trained['l2'],
                 'blend_w': trained['blend_w'], 'calibration': trained['calibration']}
    adopted, reason = memory.adopt(candidate, trained.get('metrics') or {},
                                   notes=f'{trained["n_final"]} completed games')
    if not adopted and prev.get('params'):
        # Roll back to the incumbent, then rebuild with its parameters.
        keep = prev['params']
        trained = model.train(league_key, rows, cfg,
                              elo_params=keep.get('elo_params'), tune=False)
        trained['l2'] = keep.get('l2', trained['l2'])
        trained['blend_w'] = keep.get('blend_w', trained['blend_w'])
        trained['calibration'] = keep.get('calibration', trained['calibration'])

    # ── leak-free re-weighting from the prediction ledger ───────────────────
    memory.grade()
    ledger_trust = memory.tune_trust_from_ledger()
    trust_override = ledger_trust['trust'] if ledger_trust else None
    ledger_cal = memory.ledger_calibrator()
    if ledger_cal:
        trained['calibration'] = ledger_cal.to_dict()

    # ── live player and team signals ────────────────────────────────────────
    http = http or (Http(budget_s=180) if fetch_props else None)
    pool, pool_status = ({}, 'off')
    injuries = {}
    if fetch_props and http is not None:
        pool, pool_status = load_pool(league_key, cfg, http)
        injuries = load_injuries(league_key, cfg, http)
    starter_edges = starter_edge_by_game(trained['records'], pool) if league_key == 'mlb' else {}

    # ── predict every game, then freeze the answer ──────────────────────────
    #
    # The model is refit every hour, and the standings model behind it is
    # recomputed from *current* standings. Left alone, that means a finished
    # game's probability keeps moving, and the team it names as the favourite
    # can flip once the result is in the standings — the model reading back its
    # own answer. So the first forecast published for a game is written to the
    # ledger and is what the site shows from then on.
    records = trained['records']
    now = datetime.now(timezone.utc)
    for rec in records:
        game = rec['game']
        live = model.predict(rec, trained, cfg, league_key, trust_override,
                             prior_shift=starter_edges.get(game['game_id'], 0.0))
        live = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in live.items()}
        rec['live_prediction'] = live

        stored = memory.entry_for(game) if game['game_id'] else None
        has_started = game['final'] or started(game, now)
        frozen = bool(stored and num(stored.get('p_final')) is not None
                      and (has_started or stored.get('pregame') != '1'))
        if frozen:
            prob = clamp(num(stored['p_final']), 0.0, 1.0)
            favored = stored.get('favored_team') or (
                game['home'] if prob >= 0.5 else game['away'])
            rec['prediction'] = {
                'prob': prob,
                'elo_prob': num(stored.get('p_elo'), live['elo_prob']),
                'glm_prob': num(stored.get('p_glm'), live.get('glm_prob')),
                'prior_prob': num(stored.get('p_prior'), live.get('prior_prob')),
                'starter_edge': num(stored.get('starter_edge'), live.get('starter_edge')),
                'trust': live.get('trust', 0.0),
            }
            rec['favored'] = favored
            rec['locked'] = True
            rec['pregame'] = stored.get('pregame') == '1'
            rec['locked_at'] = stored.get('predicted_at', '')
        else:
            favored = game['home'] if live['prob'] >= 0.5 else game['away']
            rec['prediction'] = live
            rec['favored'] = favored
            # Anything we are seeing for the first time after it kicked off is
            # marked as such, so it never counts toward the model's record.
            pregame = not has_started
            rec['locked'] = False
            rec['pregame'] = pregame
            rec['locked_at'] = ''
            if game['game_id']:
                memory.record(game, live, favored, pregame=pregame,
                              preseason=is_preseason(game))

    memory.grade()
    components, n_graded = memory.component_scores()

    # ── player props: grade what has finished, then price what is coming ────
    props_ledger = PropsLedger(league_key)
    boards = FrozenBoards(league_key)
    prop_board, prop_status = {}, pool_status
    lines_status = {'status': 'off', 'mode': 'model', 'games': 0}
    graded_props = 0
    if fetch_props and http is not None:
        graded_props = grade_props(league_key, cfg, props_ledger, records, http)
        props_tuning = props_ledger.tune()
        if pool:
            prop_board = price_props(league_key, cfg, records, pool,
                                     injuries, props_tuning, http, boards,
                                     lines_status=lines_status)
            record_props(props_ledger, records, prop_board, boards)
    else:
        props_tuning = props_ledger.tune()
    # Started games keep the board they went in with, whether or not the
    # feed was reachable this run; finished ones carry their outcomes.
    prop_board = frozen_boards_for(records, boards, props_ledger, prop_board)
    boards.prune(today_utc())
    props_record = props_ledger.scorecard()

    payload = build_payload(league_key, cfg, trained, memory, components,
                            n_graded, ledger_trust, prop_board, prop_status,
                            injuries=injuries, props_record=props_record,
                            props_tuning=props_tuning, lines_status=lines_status,
                            props_tallies=props_ledger.tallies())

    memory.save_archive()
    memory.save_ledger()
    props_ledger.save()
    boards.save()
    memory.save_state({
        'elo': trained['engine'].snapshot(),
        'last_metrics': trained.get('metrics'),
        'ledger': {'graded': n_graded, 'components': components,
                   'trust': ledger_trust},
        'props': {'graded_this_run': graded_props, 'record': props_record,
                  'tuning': props_tuning, 'lines': lines_status},
        'archive_size': len(memory.archive),
        'new_games_this_run': added,
    })
    return payload, trained, memory


# ─────────────────────────────────────────────────────────────────────────────
#  Live player signals: pool, injuries, starters, props
# ─────────────────────────────────────────────────────────────────────────────
def load_pool(league_key, cfg, http):
    """Fetch the player pool, falling back to the previous run's cached copy
    whenever the feed is unavailable so a blip upstream does not blank props."""
    sport, league = cfg['espn_path'].split('/')
    cache_path = os.path.join(config.DATA_DIR, f'{league_key}_players.json')
    status = 'live'
    pool = {}
    try:
        pool = espn.fetch_athlete_stats(http, sport, league)
    except Exception:                        # noqa: BLE001
        pool = {}
    if not pool:
        try:
            index = espn.fetch_team_index(http, sport, league)
            pool = espn.fetch_team_rosters(http, sport, league, index.keys())
            status = 'roster'
        except Exception:                    # noqa: BLE001
            pool = {}
    if not pool:
        cached = read_json(cache_path, {})
        pool = cached.get('pool') or {}
        status = 'cached' if pool else 'unavailable'
    else:
        fill_games_played(league_key, pool)
        write_json(cache_path, {'updated': now_iso(), 'pool': pool}, indent=None)
    if league_key == 'mlb' and pool:
        _TEAM_ERA['mlb'] = team_era_from_pool(pool)
    return pool, status


def fill_games_played(league_key, pool):
    """Hockey's player listing carries no games-played column, so a skater's
    rate cannot be formed from it alone. Use the team's games played from the
    latest standings snapshot as the denominator, flagged as an estimate; a
    regular's rate is right, a part-timer's is understated (and so is priced
    conservatively rather than inflated)."""
    if league_key != 'nhl':
        return
    snap_path = os.path.join(config.HISTORY_DIR, f'{league_key}_team_stats.csv')
    latest = {}
    for r in read_csv(snap_path):
        if (r.get('date') or '') >= (latest.get(r.get('team', ''), {}).get('date') or ''):
            latest[r.get('team', '')] = r
    team_gp = {}
    for team, r in latest.items():
        gp = num(r.get('gp'))
        if gp:
            for k in espn.team_keys(team):
                team_gp[k] = gp
    for key, roster in pool.items():
        gp = team_gp.get(key)
        if not gp:
            continue
        for p in roster:
            st = p.get('stats') or {}
            if not num(st.get('gp')):
                st['gp'] = gp
                st['gp_est'] = 1
                p['stats'] = st


def load_injuries(league_key, cfg, http):
    sport, league = cfg['espn_path'].split('/')
    cache_path = os.path.join(config.DATA_DIR, f'{league_key}_injuries.json')
    try:
        report = espn.fetch_injuries(http, sport, league)
    except Exception:                        # noqa: BLE001
        report = {}
    if report:
        write_json(cache_path, {'updated': now_iso(), 'report': report}, indent=None)
        return report
    cached = read_json(cache_path, {})
    # A stale injury list is worse than none: only reuse a same-day cache.
    if (cached.get('updated') or '')[:10] == str(today_utc()):
        return cached.get('report') or {}
    return {}


def team_era_from_pool(pool):
    """Team ERA = 9 * earned runs / innings, summed over the team's pitchers.
    The team-statistics endpoint returns no ERA from the runner, but the
    pitcher listing carries every arm's line, which is the same number."""
    out = {}
    for key, roster in (pool or {}).items():
        er = ip = 0.0
        for p in roster:
            st = p.get('stats') or {}
            innings = num(st.get('ip'))
            if innings is None or innings <= 0:
                continue
            whole = int(innings)
            ip += whole + (innings - whole) * 10 / 3.0        # 6.1 IP = 6⅓
            er += num(st.get('p_er'), 0.0) or 0.0
        if ip >= 30:
            out[key] = round(9.0 * er / ip, 2)
    return out


def pool_index(pool):
    """athlete id -> player record, across every team in the pool."""
    out = {}
    for roster in (pool or {}).values():
        for p in roster:
            if p.get('id'):
                out[str(p['id'])] = p
    return out


def starter_edge_by_game(records, pool):
    """Log-odds shift for the home side from the two probable starters' ERA.

    The single biggest per-game factor in baseball, and one the standings
    model cannot see. Applied to the standings prior with a documented
    coefficient and recorded in the ledger, so once enough graded games exist
    the coefficient can be checked rather than assumed.
    """
    if not pool:
        return {}
    by_id = pool_index(pool)
    out = {}
    for rec in records:
        row = rec['game']['row']
        away_id = str(row.get('away_probable_id') or '')
        home_id = str(row.get('home_probable_id') or '')
        if not away_id or not home_id:
            continue
        a, h = by_id.get(away_id), by_id.get(home_id)
        if not a or not h:
            continue
        a_rates = props_mod.per_game(a.get('stats') or {})
        h_rates = props_mod.per_game(h.get('stats') or {})
        a_era, h_era = num(a_rates.get('era')), num(h_rates.get('era'))
        a_gs = num(a_rates.get('starts')) or num(a_rates.get('gp')) or 0
        h_gs = num(h_rates.get('starts')) or num(h_rates.get('gp')) or 0
        if a_era is None or h_era is None or a_gs < STARTER_MIN_STARTS or h_gs < STARTER_MIN_STARTS:
            continue
        edge = clamp(STARTER_COEF * (a_era - h_era), -STARTER_CAP, STARTER_CAP)
        out[rec['game']['game_id']] = edge
    return out


def price_props(league_key, cfg, records, pool, injuries, tuning, http, boards=None,
                lines_status=None):
    """Price props for games that have not started yet.

    A game that has already started is never re-priced: its board was frozen
    at first pitch (see ``frozen_boards_for``). Lines come from the sportsbooks
    where ``ODDS_API_KEY`` is set (``odds.load_lines``); ``lines_status``, if a
    dict is passed, receives the outcome of that lookup.
    """
    sport, league = cfg['espn_path'].split('/')
    today = today_utc()
    now = datetime.now(timezone.utc)
    horizon = today + timedelta(days=UPCOMING_DAYS)
    near = today + timedelta(days=PROPS_AHEAD_DAYS.get(league_key, PROPS_AHEAD_DEFAULT))
    targets = [r for r in records
               if not r['game']['final']
               and not started(r['game'], now)
               and today <= r['game']['date'] <= min(horizon, near)
               and r['game']['game_id']]
    if not targets:
        return {}

    starters = {}
    if sport == 'baseball':
        for offset in range(0, 3):
            stamp = (today + timedelta(days=offset)).strftime('%Y%m%d')
            board = espn.fetch_scoreboard(http, sport, league, stamp)
            if board:
                starters.update(espn.probable_starters(board))

    try:
        book_lines, status = odds.load_lines(league_key, [r['game'] for r in targets], http)
    except Exception as exc:                 # noqa: BLE001 - lines are optional
        book_lines, status = {}, f'error: {type(exc).__name__}'
    book_mode = status not in ('disabled',) and not status.startswith('error')
    if lines_status is not None:
        lines_status['status'] = status
        lines_status['mode'] = 'book' if book_mode else 'model'
        lines_status['games'] = sum(1 for r in targets if book_lines.get(r['game']['game_id']))

    env = props_mod.team_environment([r['game'] for r in records], cfg)
    out = {}
    for rec in targets:
        game = rec['game']
        try:
            board = props_mod.build_for_game(
                game, pool, env, cfg, sport, rec['prediction']['prob'],
                starters=starters.get(game['game_id']), injuries=injuries, tuning=tuning,
                lines=book_lines.get(game['game_id']), book_mode=book_mode)
        except Exception:                    # noqa: BLE001
            continue
        out[game['game_id']] = board
        # Keep the boards of games starting soon so the one in force at first
        # pitch survives even if the next run cannot reach the feed.
        if boards is not None and (game['date'] - today).days <= FrozenBoards.STORE_AHEAD_DAYS:
            boards.store(game, board, frozen=False)
    return out


def frozen_boards_for(records, boards, ledger, live_boards):
    """Boards for games that have started: the stored pre-kickoff board, frozen
    now if it was not already, with graded outcomes written on."""
    now = datetime.now(timezone.utc)
    today = today_utc()
    out = dict(live_boards)
    for rec in records:
        game = rec['game']
        gid = game['game_id']
        if not gid or gid in out:
            continue
        if not (game['final'] or started(game, now)):
            continue
        if (today - game['date']).days > LIVE_RECENT_DAYS:
            continue
        board = boards.get(gid)
        if not board:
            # No stored board (started before boards existed, or the file was
            # lost): the ledger holds what was published, so rebuild from it.
            board = ledger.board_for(gid)
            if not board:
                continue
            boards.store(game, board, frozen=True)
        boards.freeze(gid)
        boards.annotate(gid, ledger.rows.values())
        board = dict(boards.get(gid))
        board['locked'] = True
        out[gid] = board
    return out


def record_props(ledger, records, prop_board, boards=None):
    """Write every prop for a game that has not started to the props ledger.

    Rows for a game are replaced each run until first pitch, so the ledger
    holds the board the game actually went in with; started games are never
    touched.
    """
    now = datetime.now(timezone.utc)
    n = 0
    for rec in records:
        game = rec['game']
        board = prop_board.get(game['game_id'])
        if not board or game['final'] or started(game, now):
            continue
        ledger.replace_game(game['game_id'])
        for side in ('away', 'home'):
            for player in board.get(side) or []:
                for prop in player.get('props') or []:
                    n += int(ledger.record(game, side, player, prop))
    return n


def grade_props(league_key, cfg, ledger, records, http):
    """Grade finished games' props from their box scores, a bounded batch per run."""
    sport, league = cfg['espn_path'].split('/')
    finished = {r['game']['game_id'] for r in records if r['game']['final'] and r['game']['game_id']}
    pending = ledger.ungraded_games(finished)[:GRADE_CAP]
    graded = 0
    for gid in pending:
        summary = espn.fetch_summary(http, sport, league, gid)
        if not summary:
            continue
        state, _ = espn.game_state(summary)
        if state != 'final':
            continue
        box = espn.boxscore_player_stats(summary, sport)
        if not box:
            continue
        graded += ledger.grade_game(gid, box)
    return graded


def injuries_for_game(game, injuries, pool, limit=6):
    """Notable unavailable players on each side, for the matchup panel."""
    if not injuries:
        return None
    by_id = pool_index(pool)
    out = {}
    for side, team in (('away', game['away']), ('home', game['home'])):
        rows = []
        for aid, rep in (espn.find_team(injuries, team) or {}).items():
            player = by_id.get(aid) or {}
            rows.append({'name': rep.get('name') or player.get('name', ''),
                         'pos': rep.get('pos') or player.get('pos', ''),
                         'status': rep.get('status', ''), 'level': rep.get('level', ''),
                         'detail': (rep.get('detail') or '')[:80]})
        rows.sort(key=lambda r: (0 if r['level'] == 'out' else 1, r['name']))
        out[side] = rows[:limit]
    return out if (out.get('away') or out.get('home')) else None


# ─────────────────────────────────────────────────────────────────────────────
#  Payload
# ─────────────────────────────────────────────────────────────────────────────
def build_payload(league_key, cfg, trained, memory, components, n_graded,
                  ledger_trust, prop_board, prop_status, injuries=None,
                  props_record=None, props_tuning=None, lines_status=None,
                  props_tallies=None):
    today = today_utc()
    recent_cut = today - timedelta(days=RECENT_DAYS)
    horizon = today + timedelta(days=UPCOMING_DAYS)

    # The live board keeps a short tail of finished games; everything older
    # goes to a history file the Results view loads on demand, so the first
    # paint does not pay for a month of box scores.
    live_cut = today - timedelta(days=LIVE_RECENT_DAYS)
    tallies = props_tallies or {}
    games, history = [], []
    for rec in trained['records']:
        g = rec['game']
        if g['date'] > horizon:
            continue
        if g['date'] >= live_cut:
            games.append(game_json(rec, cfg, league_key,
                                   prop_board.get(g['game_id']), trained,
                                   injuries=injuries_for_game(g, injuries, _pool_cache(league_key))
                                   if not g['final'] else None,
                                   props_tally=tallies.get(g['game_id'])))
        elif g['final'] and not g.get('preseason') and g['date'] >= recent_cut:
            history.append(game_json(rec, cfg, league_key, None, trained,
                                     props_tally=tallies.get(g['game_id'])))
    games.sort(key=lambda g: (g['date'], g['time'] or '', g['home']))
    history.sort(key=lambda g: g['date'], reverse=True)

    metrics = trained.get('metrics') or {}
    return {
        'league': league_key,
        'name': cfg['name'],
        'emoji': cfg['emoji'],
        'accent': cfg['accent'],
        'season': cfg['season_label'],
        'espn_path': cfg['espn_path'],
        'generated': now_iso(),
        'today': str(today),
        'model': {
            'version': MODEL_VERSION,
            'stage': trained.get('stage', 'empty'),
            'min_train': model.MIN_TRAIN + 20,
            'elo': trained['elo_params'],
            'blend_w': trained.get('blend_w'),
            'l2': trained.get('l2'),
            'trust': (ledger_trust or {}).get('trust'),
            'trust_source': 'prediction ledger' if ledger_trust else 'maturity schedule',
            'n_train': trained.get('n_final', 0),
            'archive': len(memory.archive),
            'validation': metrics,
            'reliability': trained.get('reliability') or [],
            'importance': [
                {**i, 'label': FEATURE_LABELS.get(i['feature'], i['feature'])}
                for i in (trained.get('importance') or [])
            ],
            'ledger': {'graded': n_graded, 'components': components},
            'runs': (memory.state.get('runs') or [])[-12:],
            'curve': memory.learning_curve()[-90:],
        },
        'stats': cfg['stats'],
        'props_status': prop_status,
        'lines_status': lines_status or {'status': 'off', 'mode': 'model', 'games': 0},
        'props_record': props_record or {},
        'props_tuning': props_tuning or {},
        'preseason_excluded': sum(
            1 for r in trained['records']
            if r['game'].get('preseason') and r['game']['final']),
        'feature_labels': FEATURE_LABELS,
        'games': games,
        'history': history,
        'accuracy': accuracy_block(trained),
    }


_POOL_CACHE = {}
_TEAM_ERA = {}


def _pool_cache(league_key):
    if league_key not in _POOL_CACHE:
        cached = read_json(os.path.join(config.DATA_DIR, f'{league_key}_players.json'), {})
        _POOL_CACHE[league_key] = cached.get('pool') or {}
    return _POOL_CACHE[league_key]


def game_json(rec, cfg, league_key, props, trained, injuries=None, props_tally=None):
    g = rec['game']
    row = g['row']
    pred = rec.get('prediction') or {}
    prob = pred.get('prob', 0.5)
    ctx = rec['context']

    # A scheduled game carries 0-0 in the feed; that is not a score.
    away_score = g['away_score'] if g['final'] else None
    home_score = g['home_score'] if g['final'] else None
    correct = None
    if g['final'] and g['winner']:
        correct = (rec.get('favored') == g['winner'])

    # Values only, in the order of payload['stats']; the labels and formats
    # live once at the league level rather than on all few-hundred games.
    team_stats = []
    eras = _TEAM_ERA.get(league_key) or {}
    for s in cfg['stats']:
        av = num(row.get(f'away_{s["key"]}'))
        hv = num(row.get(f'home_{s["key"]}'))
        if s['key'] == 'era' and eras:
            av = av if av is not None else espn.find_team(eras, g['away'])
            hv = hv if hv is not None else espn.find_team(eras, g['home'])
        team_stats.append([av, hv])
    if all(a is None and h is None for a, h in team_stats):
        team_stats = []

    # Feature key and signed contribution only — the human-readable labels
    # ship once per payload rather than on every game.
    drivers = [[d['feature'], round(d['contribution'], 3)]
               for d in model.edge_drivers(rec, trained)]

    out = {
        'id': g['game_id'],
        'date': str(g['date']),
        'preseason': bool(g.get('preseason')),
        'locked': bool(rec.get('locked')),
        'pregame': bool(rec.get('pregame')),
        'counted': bool(rec.get('pregame')) and not g.get('preseason'),
        'time': format_eastern(row.get('game_start_utc') or row.get('game_time'),
                               str(g['date'])),
        'away': g['away'],
        'home': g['home'],
        'away_s': short_name(g['away']),
        'home_s': short_name(g['home']),
        'away_rec': (row.get('away_record') or '').strip(),
        'home_rec': (row.get('home_record') or '').strip(),
        'final': g['final'],
        'winner': g['winner'],
        'away_score': int(away_score) if away_score is not None else None,
        'home_score': int(home_score) if home_score is not None else None,
        'home_prob': round(prob, 4),
        'away_prob': round(1 - prob, 4),
        'favored': rec.get('favored', ''),
        'pick_prob': round(max(prob, 1 - prob), 4),
        'conf': conf_tier(prob),
        'correct': correct,
        'components': {
            'elo': round(pred.get('elo_prob', 0.5), 4),
            'model': round(pred['glm_prob'], 4) if pred.get('glm_prob') is not None else None,
            'standings': round(pred['prior_prob'], 4) if pred.get('prior_prob') is not None else None,
            'trust': round(pred.get('trust', 0.0), 2),
        },
        'context': ctx,
        'stats': team_stats,
        'drivers': drivers,
    }
    if props:
        out['props'] = props
        out['props_locked'] = bool(props.get('locked'))
    if props_tally:
        out['props_tally'] = props_tally
    if injuries:
        out['injuries'] = injuries
    if pred.get('starter_edge'):
        out['starter_edge'] = round(pred['starter_edge'], 3)
    return out


def conf_tier(prob):
    p = max(prob, 1 - prob)
    if p >= 0.66:
        return 'high'
    if p >= 0.57:
        return 'med'
    return 'low'


def accuracy_block(trained):
    """Two separate records, because they mean very different things.

    ``verified`` counts only games whose forecast was published before kickoff.
    It is the real track record, and it starts empty on a fresh install.

    ``backtest`` is the walk-forward validation score: the model applied to
    historical games using only what was known at the time. It is available
    immediately but it is a simulation, not a record.

    What is *not* reported is the model's hit rate on games it first saw after
    they had finished. Those picks are made with standings that already contain
    the result, so they score near-perfectly and mean nothing.
    """
    tiers = {'high': [0, 0], 'med': [0, 0], 'low': [0, 0]}
    rows, verified_correct, verified_total = [], 0, 0
    backfilled = 0

    for rec in trained['records']:
        g = rec['game']
        if not g['final'] or not g['winner'] or 'prediction' not in rec:
            continue
        if g.get('preseason'):
            continue
        ok = rec['favored'] == g['winner']
        if not rec.get('pregame'):
            backfilled += 1
            continue
        tier = conf_tier(rec['prediction']['prob'])
        tiers[tier][1] += 1
        tiers[tier][0] += int(ok)
        verified_total += 1
        verified_correct += int(ok)
        rows.append({'away': g['away'], 'home': g['home'], 'ok': ok})

    teams = {}
    for r in rows:
        for t in (r['away'], r['home']):
            slot = teams.setdefault(t, {'n': 0, 'ok': 0})
            slot['n'] += 1
            slot['ok'] += int(r['ok'])
    team_rows = sorted(
        ({'team': t, 'n': v['n'], 'ok': v['ok'],
          'acc': round(v['ok'] / v['n'], 4) if v['n'] else 0}
         for t, v in teams.items()),
        key=lambda t: (-t['n'], -t['acc']))

    backtest = None
    oos = trained.get('oos')
    if trained.get('stage') == 'trained' and oos:
        bt_tiers = {'high': [0, 0], 'med': [0, 0], 'low': [0, 0]}
        bt_ok = 0
        for prob, y in zip(oos['probs'], oos['ys']):
            hit = int((prob >= 0.5) == (y == 1))
            tier = conf_tier(prob)
            bt_tiers[tier][1] += 1
            bt_tiers[tier][0] += hit
            bt_ok += hit
        n = len(oos['ys'])
        backtest = {
            'total': n,
            'correct': bt_ok,
            'pct': round(bt_ok / n, 4) if n else None,
            'buckets': {k: {'n': v[1], 'ok': v[0],
                            'pct': round(v[0] / v[1], 4) if v[1] else None}
                        for k, v in bt_tiers.items()},
        }

    return {
        'verified': {
            'total': verified_total,
            'correct': verified_correct,
            'pct': round(verified_correct / verified_total, 4) if verified_total else None,
            'buckets': {k: {'n': v[1], 'ok': v[0],
                            'pct': round(v[0] / v[1], 4) if v[1] else None}
                        for k, v in tiers.items()},
        },
        'backtest': backtest,
        'backfilled': backfilled,
        'teams': team_rows,
    }
