"""Persistent memory: the part of the system that gets better on its own.

Three files per league, all committed back to the repository so that every
scheduled run starts from everything the previous runs learned:

``history/<league>_games.csv``   append-only archive of completed games.
    The ingest scripts only see a rolling window of the schedule. Merging each
    window into an archive means Elo and the form features keep growing a
    longer memory instead of restarting from 1500 every hour.

``history/<league>_ledger.csv``  every prediction, written *before* the game.
    This is the only genuinely leak-free scoreboard the system has. Season
    standings describe how a season turned out, so scoring a standings model
    against past games flatters it. A prediction recorded at 9am and graded at
    11pm cannot flatter anything.

``model_state/<league>.json``    tuned parameters, calibration and a run log.
    New parameters have to beat the incumbent on validation before they are
    adopted, so a bad hour cannot degrade the model.
"""
from __future__ import annotations

import math
import os

from . import config
from .features import detect_preseason
from .glm import PlattCalibrator, score
from .util import (clamp, logistic, logit, now_iso, num, parse_date, read_csv,
                   read_json, write_csv, write_json)

MODEL_VERSION = 3
LEDGER_MATURITY = 150      # graded pre-game predictions before the ledger rules
ARCHIVE_FIELDS = ['game_id', 'game_date', 'away_team', 'home_team',
                  'away_score', 'home_score', 'winner', 'first_seen']
LEDGER_FIELDS = ['game_id', 'game_date', 'away_team', 'home_team', 'predicted_at',
                 'model_version', 'pregame', 'preseason',
                 'p_final', 'p_elo', 'p_glm', 'p_prior', 'starter_edge',
                 'favored_team', 'away_score', 'home_score', 'winner',
                 'graded', 'correct']


def _key(row):
    gid = (row.get('game_id') or '').strip()
    if gid:
        return gid
    return '|'.join([(row.get('game_date') or '')[:10],
                     (row.get('away_team') or '').strip(),
                     (row.get('home_team') or '').strip()])


class LeagueMemory:
    def __init__(self, league_key, history_dir=None, state_dir=None):
        # Resolved on construction rather than bound as import-time defaults,
        # so the locations stay overridable (tests, alternate checkouts).
        history_dir = history_dir or config.HISTORY_DIR
        state_dir = state_dir or config.STATE_DIR
        self.league = league_key
        self.archive_path = os.path.join(history_dir, f'{league_key}_games.csv')
        self.ledger_path = os.path.join(history_dir, f'{league_key}_ledger.csv')
        self.state_path = os.path.join(state_dir, f'{league_key}.json')
        self.archive = {r_k: r for r_k, r in
                        ((_key(r), r) for r in read_csv(self.archive_path))}
        self.ledger = {r_k: r for r_k, r in
                       ((_key(r), r) for r in read_csv(self.ledger_path))}
        self.state = read_json(self.state_path, {})

    # ── archive ─────────────────────────────────────────────────────────────
    def merge_archive(self, rows, league_key=None):
        """Fold this run's completed games into the permanent archive.

        Exhibition games are left out: the archive exists to give the ratings a
        longer memory, and preseason results are not evidence about anybody.
        """
        added = 0
        for r in rows:
            if (r.get('status') or '') != 'Final':
                continue
            if league_key:
                d = parse_date(r.get('game_date'))
                if d and detect_preseason(r, d, league_key):
                    continue
            winner = (r.get('winner') or '').strip()
            hs, as_ = num(r.get('home_score')), num(r.get('away_score'))
            if not winner or hs is None or as_ is None:
                continue
            if hs == 0 and as_ == 0:          # ESPN placeholder for a no-show
                continue
            k = _key(r)
            if k in self.archive:
                continue
            self.archive[k] = {
                'game_id': (r.get('game_id') or '').strip(),
                'game_date': (r.get('game_date') or '')[:10],
                'away_team': (r.get('away_team') or '').strip(),
                'home_team': (r.get('home_team') or '').strip(),
                'away_score': int(as_), 'home_score': int(hs),
                'winner': winner, 'first_seen': now_iso()[:10],
            }
            added += 1
        return added

    def archive_rows(self):
        """Archive entries shaped like enriched-CSV rows, oldest first."""
        out = []
        for r in self.archive.values():
            out.append({
                'game_id': r.get('game_id', ''),
                'game_date': r.get('game_date', ''),
                'away_team': r.get('away_team', ''),
                'home_team': r.get('home_team', ''),
                'away_score': r.get('away_score', ''),
                'home_score': r.get('home_score', ''),
                'winner': r.get('winner', ''),
                'status': 'Final',
            })
        out.sort(key=lambda r: (r['game_date'], r['game_id']))
        return out

    def save_archive(self):
        rows = sorted(self.archive.values(),
                      key=lambda r: (r.get('game_date', ''), r.get('game_id', '')))
        write_csv(self.archive_path, rows, ARCHIVE_FIELDS)

    # ── ledger ──────────────────────────────────────────────────────────────
    def entry_for(self, game):
        """The stored forecast for a game, if we have already published one."""
        return self.ledger.get(self._key_for(game))

    @staticmethod
    def _key_for(game):
        return _key({'game_id': game.get('game_id'),
                     'game_date': str(game.get('date')),
                     'away_team': game.get('away'),
                     'home_team': game.get('home')})

    def record(self, game, parts, favored, pregame=True, preseason=False):
        """Log a forecast.

        Until kickoff the forecast may be refreshed — a late lineup change or
        injury report should count — so a pre-game entry is replaced by a
        newer pre-game one. From first pitch it is frozen: re-running the
        model afterwards must not rewrite it, otherwise a finished game can
        change who it says was favoured, because the standings model behind
        it now knows the result. The last forecast published before the game
        started is the one we are held to.

        ``pregame`` records whether the forecast beat the first pitch. Only
        pre-game rows are used to score the model; back-filled rows, and
        exhibition games, exist so that every game still displays a stable
        number.
        """
        k = self._key_for(game)
        existing = self.ledger.get(k)
        if existing and (existing.get('p_final') or '') != '':
            # Frozen once the game has started; a pre-game entry can only be
            # replaced by another pre-game entry.
            if not pregame or existing.get('graded') == '1':
                return False
        self.ledger[k] = {
            'game_id': game['game_id'],
            'game_date': str(game['date']),
            'away_team': game['away'],
            'home_team': game['home'],
            'predicted_at': now_iso(),
            'model_version': MODEL_VERSION,
            'pregame': '1' if pregame else '0',
            'preseason': '1' if preseason else '0',
            'p_final': round(parts['prob'], 4),
            'p_elo': round(parts['elo_prob'], 4) if parts.get('elo_prob') is not None else '',
            'p_glm': round(parts['glm_prob'], 4) if parts.get('glm_prob') is not None else '',
            'p_prior': round(parts['prior_prob'], 4) if parts.get('prior_prob') is not None else '',
            'starter_edge': round(parts['starter_edge'], 4) if parts.get('starter_edge') is not None else '',
            'favored_team': favored,
            'away_score': '', 'home_score': '', 'winner': '',
            'graded': '0', 'correct': '',
        }
        return True

    def grade(self):
        """Attach results to ledger entries whose games have since finished."""
        graded = 0
        for k, entry in self.ledger.items():
            if entry.get('graded') == '1':
                continue
            res = self.archive.get(k)
            if not res:
                continue
            winner = res.get('winner', '')
            if not winner:
                continue
            p = num(entry.get('p_final'))
            fav = entry.get('favored_team', '')
            entry['away_score'] = res.get('away_score', '')
            entry['home_score'] = res.get('home_score', '')
            entry['winner'] = winner
            entry['graded'] = '1'
            entry['correct'] = '1' if (fav and fav == winner) else '0'
            if p is None:
                entry['correct'] = ''
            graded += 1
        return graded

    def graded_rows(self, pregame_only=False):
        rows = [e for e in self.ledger.values()
                if e.get('graded') == '1' and e.get('winner')
                and e.get('preseason') != '1']
        if pregame_only:
            rows = [e for e in rows if e.get('pregame') == '1']
        return rows

    def save_ledger(self):
        rows = sorted(self.ledger.values(),
                      key=lambda r: (r.get('game_date', ''), r.get('game_id', '')))
        write_csv(self.ledger_path, rows, LEDGER_FIELDS)

    # ── leak-free scoring of each component ─────────────────────────────────
    def component_scores(self):
        rows = self.graded_rows(pregame_only=True)
        if not rows:
            return {}, 0
        out = {}
        ys = [1 if r['winner'] == r['home_team'] else 0 for r in rows]
        for name in ('p_final', 'p_elo', 'p_glm', 'p_prior'):
            pairs = [(num(r.get(name)), y) for r, y in zip(rows, ys)]
            probs = [p for p, _ in pairs if p is not None]
            outs = [y for p, y in pairs if p is not None]
            if len(probs) >= 20:
                out[name[2:]] = score(probs, outs)
        return out, len(rows)

    def tune_trust_from_ledger(self):
        """Choose how far to lean on the learned model rather than the
        standings prior, using only predictions recorded before kickoff."""
        rows = self.graded_rows(pregame_only=True)
        usable = [r for r in rows
                  if num(r.get('p_elo')) is not None and num(r.get('p_prior')) is not None]
        if len(usable) < LEDGER_MATURITY:
            return None
        ys = [1 if r['winner'] == r['home_team'] else 0 for r in usable]
        model_p = []
        for r in usable:
            glm = num(r.get('p_glm'))
            elo = num(r.get('p_elo'))
            model_p.append(glm if glm is not None else elo)
        prior_p = [num(r.get('p_prior')) for r in usable]

        best_w, best_ll = None, None
        for step in range(21):
            w = step / 20.0
            probs = [logistic(w * logit(m) + (1 - w) * logit(p))
                     for m, p in zip(model_p, prior_p)]
            ll = score(probs, ys)['logloss']
            if ll is not None and (best_ll is None or ll < best_ll):
                best_ll, best_w = ll, w
        return {'trust': best_w, 'logloss': best_ll, 'n': len(usable)}

    def ledger_calibrator(self):
        """Recalibrate on graded pre-game predictions once there are enough."""
        rows = self.graded_rows(pregame_only=True)
        probs, ys = [], []
        for r in rows:
            p = num(r.get('p_final'))
            if p is None:
                continue
            probs.append(p)
            ys.append(1 if r['winner'] == r['home_team'] else 0)
        if len(probs) < LEDGER_MATURITY:
            return None
        cal = PlattCalibrator().fit(probs, ys)
        before = score(probs, ys)['logloss']
        after = score([cal.apply(p) for p in probs], ys)['logloss']
        if after is None or before is None or after >= before - 1e-4:
            return None
        return cal

    # ── state ───────────────────────────────────────────────────────────────
    def previous_best(self):
        return self.state.get('best') or {}

    def adopt(self, candidate, metrics, notes=''):
        """Champion/challenger: keep new parameters only when they validate
        better than the incumbent, so a noisy hour cannot make the model worse."""
        prev = self.previous_best()
        prev_ll = (prev.get('metrics') or {}).get('logloss')
        new_ll = (metrics or {}).get('logloss')
        prev_n = (prev.get('metrics') or {}).get('n') or 0
        new_n = (metrics or {}).get('n') or 0

        adopted = True
        reason = 'first fit'
        if prev_ll is not None and new_ll is not None:
            # Require a real improvement, or a materially bigger sample.
            if new_ll <= prev_ll - 1e-4:
                reason = f'log loss {prev_ll:.4f} -> {new_ll:.4f}'
            elif new_n >= prev_n * 1.15:
                reason = f'more evidence ({prev_n} -> {new_n} games)'
            else:
                adopted = False
                reason = f'kept incumbent (challenger {new_ll:.4f} vs {prev_ll:.4f})'
        elif new_ll is None:
            adopted = False
            reason = 'challenger produced no validation score'

        if adopted:
            self.state['best'] = {'params': candidate, 'metrics': metrics,
                                  'adopted_at': now_iso(), 'reason': reason}
        log = self.state.setdefault('runs', [])
        log.append({'at': now_iso(), 'adopted': adopted, 'reason': reason,
                    'metrics': metrics, 'notes': notes,
                    'model_version': MODEL_VERSION})
        self.state['runs'] = log[-60:]          # keep the trail bounded
        return adopted, reason

    def save_state(self, extra=None):
        self.state['league'] = self.league
        self.state['updated'] = now_iso()
        self.state['model_version'] = MODEL_VERSION
        if extra:
            self.state.update(extra)
        write_json(self.state_path, self.state)

    def learning_curve(self):
        """Rolling accuracy of graded pre-game picks — the 'is it improving?'
        chart on the site."""
        rows = sorted(self.graded_rows(pregame_only=True),
                      key=lambda r: r.get('game_date', ''))
        by_day = {}
        for r in rows:
            if r.get('correct') not in ('0', '1'):
                continue
            d = r.get('game_date', '')[:10]
            slot = by_day.setdefault(d, [0, 0])
            slot[1] += 1
            slot[0] += int(r['correct'])
        points, run_c, run_n = [], 0, 0
        for d in sorted(by_day):
            c, n = by_day[d]
            run_c += c
            run_n += n
            points.append({'date': d, 'correct': c, 'n': n,
                           'cum_acc': round(run_c / run_n, 4)})
        return points


# ─────────────────────────────────────────────────────────────────────────────
#  Player-prop ledger: the same discipline, applied to every projection
# ─────────────────────────────────────────────────────────────────────────────
PROP_FIELDS = ['game_id', 'game_date', 'athlete_id', 'player', 'side', 'key', 'label',
               'stat', 'dist', 'line', 'proj', 'season', 'over', 'pick', 'conf',
               'recorded_at', 'actual', 'played', 'graded', 'hit', 'push']
PROP_TUNING_MIN = 30       # graded props of one kind before its parameters move
PROP_TUNING_FULL = 200     # ... and the sample size at which they move fully


class PropsLedger:
    """Every published prop, graded against the final box score.

    Two things come out of it: an honest hit rate per market and confidence
    tier for the site, and per-market corrections — a bias multiplier on the
    projection and a scale on the spread — that ``props.py`` applies once a
    market has enough graded history. Rows are never rewritten.
    """

    def __init__(self, league_key, history_dir=None):
        history_dir = history_dir or config.HISTORY_DIR
        self.league = league_key
        self.path = os.path.join(history_dir, f'{league_key}_props.csv')
        self.rows = {self._key(r): r for r in read_csv(self.path)}

    @staticmethod
    def _key(r):
        return '|'.join([str(r.get('game_id') or ''), str(r.get('athlete_id') or ''),
                         str(r.get('key') or '')])

    def replace_game(self, game_id):
        """Drop a game's rows so a fresh pre-kickoff pricing can replace them.
        Never called once the game has started; graded rows are never touched."""
        for k in [k for k, r in self.rows.items()
                  if r.get('game_id') == game_id and r.get('graded') != '1']:
            del self.rows[k]

    def has_game(self, game_id):
        return any(r.get('game_id') == game_id for r in self.rows.values())

    def record(self, game, side, player, prop):
        if not game.get('game_id') or not player.get('id'):
            return False
        row = {
            'game_id': game['game_id'], 'game_date': str(game['date']),
            'athlete_id': player['id'], 'player': player.get('name', ''), 'side': side,
            'key': prop['key'], 'label': prop['label'], 'stat': prop.get('stat', ''),
            'dist': prop.get('dist', ''), 'line': prop['line'], 'proj': prop['proj'],
            'season': prop['season'], 'over': prop['over'], 'pick': prop['pick'],
            'conf': prop['conf'], 'recorded_at': now_iso(),
            'actual': '', 'played': '', 'graded': '0', 'hit': '', 'push': '',
        }
        k = self._key(row)
        if k in self.rows:
            return False
        self.rows[k] = row
        return True

    def ungraded_games(self, finished_ids):
        """Game ids that have finished and still hold ungraded props."""
        wanted = set()
        for r in self.rows.values():
            if r.get('graded') != '1' and r.get('game_id') in finished_ids:
                wanted.add(r['game_id'])
        return sorted(wanted)

    def grade_game(self, game_id, box):
        """Grade every prop for one game from ``boxscore_player_stats`` output."""
        graded = 0
        for r in self.rows.values():
            if r.get('game_id') != game_id or r.get('graded') == '1':
                continue
            entry = box.get(str(r.get('athlete_id')))
            r['graded'] = '1'
            if not entry or not entry.get('played', True):
                r['played'] = '0'
                r['actual'] = ''
                r['hit'] = ''
                graded += 1
                continue
            stat_key = (r.get('stat') or '')[:-3] if (r.get('stat') or '').endswith('_pg') else r.get('stat')
            actual = entry['stats'].get(stat_key)
            r['played'] = '1'
            if actual is None:
                r['actual'] = ''
                r['hit'] = ''
                graded += 1
                continue
            line = num(r.get('line'))
            r['actual'] = round(actual, 2)
            if line is not None and abs(actual - line) < 1e-9:
                r['push'] = '1'
                r['hit'] = ''
            else:
                went_over = actual > (line if line is not None else 0)
                r['push'] = '0'
                r['hit'] = '1' if (went_over == (r.get('pick') == 'over')) else '0'
            graded += 1
        return graded

    def graded(self):
        return [r for r in self.rows.values()
                if r.get('graded') == '1' and r.get('played') == '1'
                and r.get('actual') not in ('', None) and r.get('push') != '1']

    def scorecard(self):
        """Hit rate by market and by confidence tier — for the Model tab."""
        rows = self.graded()
        by_key, by_conf = {}, {}
        for r in rows:
            hit = r.get('hit')
            if hit not in ('0', '1'):
                continue
            k = by_key.setdefault(r['key'], {'label': r.get('label', r['key']), 'n': 0, 'hit': 0})
            k['n'] += 1
            k['hit'] += int(hit)
            c = by_conf.setdefault(r.get('conf', 'low'), {'n': 0, 'hit': 0})
            c['n'] += 1
            c['hit'] += int(hit)
        for d in list(by_key.values()) + list(by_conf.values()):
            d['pct'] = round(d['hit'] / d['n'], 4) if d['n'] else None
        total = sum(v['n'] for v in by_conf.values())
        hits = sum(v['hit'] for v in by_conf.values())
        return {'total': total, 'hit': hits,
                'pct': round(hits / total, 4) if total else None,
                'by_key': by_key, 'by_conf': by_conf}

    def tune(self):
        """Per-market corrections from the graded history.

        ``bias``   multiplier on the projection (actual / projected, shrunk
                   toward 1 by sample size);
        ``spread`` multiplier on the distribution's spread, from how the
                   residuals actually scattered versus what the distribution
                   assumed. Both are clamped so a strange month cannot swing
                   a market by more than a third.
        """
        buckets = {}
        for r in self.graded():
            proj, actual = num(r.get('proj')), num(r.get('actual'))
            if proj is None or actual is None or proj <= 0:
                continue
            buckets.setdefault(r['key'], []).append((proj, actual, r.get('dist', '')))
        tuned = {}
        for key, pairs in buckets.items():
            n = len(pairs)
            if n < PROP_TUNING_MIN:
                continue
            weight = clamp((n - PROP_TUNING_MIN) / float(PROP_TUNING_FULL - PROP_TUNING_MIN), 0.0, 1.0)
            sum_p = sum(p for p, _, _ in pairs)
            sum_a = sum(a for _, a, _ in pairs)
            raw_bias = (sum_a / sum_p) if sum_p > 0 else 1.0
            bias = 1.0 + weight * (clamp(raw_bias, 0.67, 1.5) - 1.0)
            # Observed scatter vs the model's own assumed scatter.
            dist = pairs[0][2]
            resid = [a - p * raw_bias for p, a, _ in pairs]
            obs_var = sum(x * x for x in resid) / max(n - 1, 1)
            if dist == 'normal':
                assumed = sum((p * raw_bias) for p, _, _ in pairs) / n
                # Spread rules grow roughly with the mean; compare against a
                # mean-scaled reference so the multiplier is unit-free.
                ref_var = max(assumed, 1e-6)
            else:
                ref_var = max(sum_a / n, 1e-6)           # Poisson reference
            raw_spread = math.sqrt(obs_var / ref_var) if ref_var > 0 else 1.0
            spread = 1.0 + weight * (clamp(raw_spread, 0.67, 1.5) - 1.0)
            tuned[key] = {'bias': round(bias, 4), 'spread': round(spread, 4), 'n': n,
                          'raw_bias': round(raw_bias, 4)}
        return tuned

    def save(self):
        rows = sorted(self.rows.values(),
                      key=lambda r: (r.get('game_date', ''), r.get('game_id', ''),
                                     r.get('athlete_id', ''), r.get('key', '')))
        if rows:
            write_csv(self.path, rows, PROP_FIELDS)


class FrozenBoards:
    """The prop board each game went into first pitch with.

    Props are re-priced every run while a game is still to come, so the last
    pre-kickoff board is the one that gets frozen here. From then on the page
    shows this board — the live box score may be drawn over it, but no line,
    projection or probability changes — and once graded, each prop carries
    its outcome. Boards are kept only for games starting within two days and
    for a few days after they finish, so the file stays small.
    """

    KEEP_DAYS_AFTER = 4
    STORE_AHEAD_DAYS = 2

    def __init__(self, league_key, history_dir=None):
        history_dir = history_dir or config.HISTORY_DIR
        self.path = os.path.join(history_dir, f'{league_key}_boards.json')
        self.boards = read_json(self.path, {}) or {}

    def get(self, game_id):
        entry = self.boards.get(str(game_id))
        return entry.get('board') if entry else None

    def is_frozen(self, game_id):
        entry = self.boards.get(str(game_id))
        return bool(entry and entry.get('frozen'))

    def store(self, game, board, frozen):
        """Keep a pre-kickoff board (replacing the previous one), or mark the
        stored board frozen once the game has started."""
        gid = str(game.get('game_id') or '')
        if not gid:
            return
        existing = self.boards.get(gid)
        if existing and existing.get('frozen'):
            return
        self.boards[gid] = {'date': str(game.get('date')), 'board': board,
                            'frozen': bool(frozen), 'updated': now_iso()}

    def freeze(self, game_id):
        entry = self.boards.get(str(game_id))
        if entry:
            entry['frozen'] = True

    def annotate(self, game_id, ledger_rows):
        """Write graded outcomes back onto the frozen board's props."""
        entry = self.boards.get(str(game_id))
        if not entry:
            return
        graded = {(r.get('athlete_id'), r.get('key')): r for r in ledger_rows
                  if r.get('game_id') == str(game_id) and r.get('graded') == '1'}
        for side in ('away', 'home'):
            for player in entry['board'].get(side) or []:
                for prop in player.get('props') or []:
                    row = graded.get((player.get('id'), prop.get('key')))
                    if not row:
                        continue
                    prop['actual'] = num(row.get('actual'))
                    prop['played'] = row.get('played') == '1'
                    prop['hit'] = (None if row.get('hit') not in ('0', '1')
                                   else row.get('hit') == '1')
                    prop['push'] = row.get('push') == '1'

    def prune(self, today):
        keep = {}
        for gid, entry in self.boards.items():
            d = parse_date(entry.get('date'))
            if d is None:
                continue
            if (today - d).days <= self.KEEP_DAYS_AFTER:
                keep[gid] = entry
        self.boards = keep

    def save(self):
        write_json(self.path, self.boards, indent=None)
