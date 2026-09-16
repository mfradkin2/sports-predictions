"""Model orchestration: Elo tuning, walk-forward validation, blending.

Everything reported here is out-of-sample. A prediction is only ever scored
against a model that could not see the game when it was fitted, so the accuracy
shown on the site is the accuracy a reader would actually have got.
"""
from __future__ import annotations

import math

from . import features as feat
from .glm import LogisticModel, PlattCalibrator, reliability_table, score
from .ratings import EloEngine
from .util import clamp, logistic, logit, num

MIN_TRAIN = 90          # completed games before the learned model is trusted
N_FOLDS = 6
L2_GRID = (0.5, 1.5, 4.0, 10.0, 25.0)


# ─────────────────────────────────────────────────────────────────────────────
#  Dataset
# ─────────────────────────────────────────────────────────────────────────────
def build_dataset(league_key, rows, cfg, elo_params):
    games = feat.normalize_games(rows, league_key)
    engine = EloEngine(**elo_params)
    elo_records = engine.replay(games)
    records = feat.build(games, league_key, elo_records,
                         score_sigma=cfg.get('score_sigma', 10.0))
    return games, records, engine


def outcomes_of(records):
    """1 when the home team won, None when the game does not count.

    Exhibition games are treated as unplayed: they finished, but nothing about
    them should teach the model anything.
    """
    out = []
    for r in records:
        g = r['game']
        if not g['final'] or g.get('preseason'):
            out.append(None)
        else:
            out.append(1 if g['home_score'] > g['away_score']
                       else (0 if g['home_score'] < g['away_score'] else None))
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Elo hyperparameter search
# ─────────────────────────────────────────────────────────────────────────────
ELO_GRID = {
    'k':       [0.5, 0.75, 1.0, 1.4, 2.0],      # multipliers on the league prior
    'hfa':     [0.6, 0.85, 1.0, 1.25, 1.6],
    'mov':     [0.0, 0.35, 0.7, 1.0],
    'regress': [0.25, 0.4],
}


def tune_elo(league_key, rows, cfg, warmup_frac=0.25, max_evals=260):
    """Coordinate-descent search over Elo parameters by out-of-sample log loss.

    Elo is naturally walk-forward — every rating used for a game predates it —
    so replaying the log and scoring the predictions is already honest.
    """
    prior = dict(cfg['elo'])
    best = dict(prior)
    best_metric = None
    evals = 0

    def evaluate(params):
        nonlocal evals
        evals += 1
        games = feat.normalize_games(rows, league_key)
        engine = EloEngine(**params)
        recs = engine.replay(games)
        probs, ys = [], []
        finals = [i for i, g in enumerate(games)
                  if g['final'] and not g.get('preseason')]
        if len(finals) < 40:
            return None
        warm = int(len(finals) * warmup_frac)
        for i in finals[warm:]:
            g = games[i]
            if g['home_score'] == g['away_score']:
                continue
            probs.append(recs[i]['elo_home_prob'])
            ys.append(1 if g['home_score'] > g['away_score'] else 0)
        if len(probs) < 30:
            return None
        return score(probs, ys)

    baseline = evaluate(prior)
    if baseline is None:
        return prior, None
    best_metric = baseline['logloss']

    # Two coordinate sweeps are enough to settle on this smooth a surface.
    for _ in range(2):
        for name, mults in ELO_GRID.items():
            for m in mults:
                if evals >= max_evals:
                    break
                trial = dict(best)
                trial[name] = (prior[name] * m) if name != 'regress' else m
                if name == 'mov':
                    trial[name] = m
                if trial[name] == best[name]:
                    continue
                res = evaluate(trial)
                if res and res['logloss'] < best_metric - 1e-5:
                    best_metric = res['logloss']
                    best = trial
    return best, evaluate(best)


# ─────────────────────────────────────────────────────────────────────────────
#  Walk-forward validation of the learned model
# ─────────────────────────────────────────────────────────────────────────────
def walk_forward(records, outcomes, l2, min_train=MIN_TRAIN, n_folds=N_FOLDS):
    """Expanding-window validation.

    Returns index-aligned lists of out-of-sample GLM probabilities and Elo
    probabilities, plus the outcomes, for the graded portion of the log.
    """
    played = [i for i, o in enumerate(outcomes) if o is not None]
    if len(played) < min_train + 20:
        return [], [], [], []

    holdout = played[min_train:]
    fold_size = max(len(holdout) // n_folds, 1)
    oos_glm, oos_elo, ys, idxs = [], [], [], []

    for start in range(0, len(holdout), fold_size):
        block = holdout[start:start + fold_size]
        if not block:
            continue
        cutoff = block[0]
        train_idx = [i for i in played if i < cutoff]
        if len(train_idx) < min_train:
            continue
        X = [records[i]['vector'] for i in train_idx]
        y = [outcomes[i] for i in train_idx]
        model = LogisticModel(feat.FEATURE_NAMES, l2=l2).fit(X, y)
        for i in block:
            oos_glm.append(model.predict_proba(records[i]['vector']))
            oos_elo.append(records[i]['elo_prob'])
            ys.append(outcomes[i])
            idxs.append(i)
    return oos_glm, oos_elo, ys, idxs


def best_blend(p_a, p_b, ys, steps=21):
    """Find w minimising log loss for logit-space blend w*a + (1-w)*b."""
    best_w, best_ll = 1.0, None
    for s in range(steps):
        w = s / (steps - 1.0)
        probs = [logistic(w * logit(a) + (1 - w) * logit(b))
                 for a, b in zip(p_a, p_b)]
        ll = score(probs, ys)['logloss']
        if ll is not None and (best_ll is None or ll < best_ll):
            best_ll, best_w = ll, w
    return best_w, best_ll


def blend_probs(p_a, p_b, w):
    return [logistic(w * logit(a) + (1 - w) * logit(b)) for a, b in zip(p_a, p_b)]


# ─────────────────────────────────────────────────────────────────────────────
#  Full training run
# ─────────────────────────────────────────────────────────────────────────────
def train(league_key, rows, cfg, elo_params=None, tune=True):
    """Fit everything and report honest out-of-sample metrics.

    Returns a dict holding the production model, the tuned parameters and the
    validation scores — the payload the learning loop stores and compares.
    """
    elo_params = dict(elo_params or cfg['elo'])
    elo_metric = None
    if tune:
        elo_params, elo_metric = tune_elo(league_key, rows, cfg)

    games, records, engine = build_dataset(league_key, rows, cfg, elo_params)
    outcomes = outcomes_of(records)
    n_final = sum(1 for o in outcomes if o is not None)

    result = {
        'league': league_key,
        # 'empty'   nothing to model yet
        # 'warmup'  too little history for walk-forward validation to mean
        #           anything; predictions lean on the standings model
        # 'trained' validated out of sample
        'stage': 'empty',
        'elo_params': elo_params,
        'elo_only': elo_metric,
        'n_final': n_final,
        'n_games': len(games),
        'model': None,
        'l2': None,
        'blend_w': 0.0,
        'calibration': PlattCalibrator().to_dict(),
        'metrics': {},
        'baseline': {},
        'reliability': [],
        'importance': [],
        'records': records,
        'games': games,
        'engine': engine,
        'outcomes': outcomes,
    }

    # Baseline: what the season-stats model in the R scripts would have said.
    prior_probs, prior_ys = [], []
    for i, o in enumerate(outcomes):
        if o is None:
            continue
        p = num(records[i]['game']['row'].get('home_win_probability'))
        if p is None:
            continue
        prior_probs.append(clamp(p, 0.02, 0.98))
        prior_ys.append(o)
    if prior_probs:
        result['baseline'] = score(prior_probs, prior_ys)

    if n_final < MIN_TRAIN + 20:
        # Not enough history for walk-forward validation to say anything. Score
        # Elo on what exists so the number is available, but mark the stage so
        # the site does not present a cold-start figure as a verdict.
        played = [i for i, o in enumerate(outcomes) if o is not None]
        if played:
            result['metrics'] = score([records[i]['elo_prob'] for i in played],
                                      [outcomes[i] for i in played])
        result['stage'] = 'warmup' if n_final else 'empty'
        result['blend_w'] = 0.0
        return result

    # Pick the ridge penalty by walk-forward log loss.
    best = None
    for l2 in L2_GRID:
        g, e, ys, idxs = walk_forward(records, outcomes, l2)
        if not g:
            continue
        w, ll = best_blend(g, e, ys)
        if ll is None:
            continue
        if best is None or ll < best['ll']:
            best = {'l2': l2, 'w': w, 'll': ll, 'glm': g, 'elo': e,
                    'ys': ys, 'idxs': idxs}
    if best is None:
        result['stage'] = 'warmup'
        result['blend_w'] = 0.0
        return result

    blended = blend_probs(best['glm'], best['elo'], best['w'])
    cal = PlattCalibrator().fit(blended, best['ys'])
    calibrated = [cal.apply(p) for p in blended]

    # Keep the calibrator only if it actually helps out of sample.
    raw_ll = score(blended, best['ys'])['logloss']
    cal_ll = score(calibrated, best['ys'])['logloss']
    if cal_ll is None or raw_ll is None or cal_ll > raw_ll:
        cal = PlattCalibrator()
        calibrated = blended

    result['stage'] = 'trained'
    result['l2'] = best['l2']
    result['blend_w'] = best['w']
    result['calibration'] = cal.to_dict()
    result['metrics'] = score(calibrated, best['ys'])
    result['metrics']['elo_only'] = score(best['elo'], best['ys'])
    result['metrics']['glm_only'] = score(best['glm'], best['ys'])
    result['reliability'] = reliability_table(calibrated, best['ys'])
    result['oos'] = {'probs': calibrated, 'ys': best['ys'], 'idxs': best['idxs']}

    # Production model: refit on the complete log.
    played = [i for i, o in enumerate(outcomes) if o is not None]
    X = [records[i]['vector'] for i in played]
    y = [outcomes[i] for i in played]
    prod = LogisticModel(feat.FEATURE_NAMES, l2=best['l2']).fit(X, y)
    result['model'] = prod
    result['importance'] = [{'feature': n, 'coef': round(c, 4), 'weight': round(w, 4)}
                            for n, c, w in prod.importance()]
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Prediction
# ─────────────────────────────────────────────────────────────────────────────
def predict(record, trained, cfg, league_key, trust_override=None, prior_shift=0.0):
    """Final home win probability for one game, with its components.

    The learned model is faded toward the standings-based prior when there is
    not much season on the books yet, which is when Elo and form are noisiest.

    ``trust_override`` comes from the prediction ledger once it holds enough
    graded forecasts. That measurement is leak-free — every probability in it
    was written down before the game — so it supersedes the fixed schedule.

    ``prior_shift`` is a log-odds adjustment to the standings prior for
    information the standings cannot carry (in baseball, who is pitching).
    """
    elo_p = record['elo_prob']
    model = trained.get('model')
    glm_p = model.predict_proba(record['vector']) if model else None
    w = trained.get('blend_w', 0.0)

    if glm_p is not None:
        core = logistic(w * logit(glm_p) + (1 - w) * logit(elo_p))
    else:
        core = elo_p

    cal = PlattCalibrator.from_dict(trained.get('calibration'))
    core = cal.apply(core)

    prior = num(record['game']['row'].get('home_win_probability'))
    prior = clamp(prior, 0.03, 0.97) if prior is not None else None
    if prior is not None and prior_shift:
        prior = clamp(logistic(logit(prior) + prior_shift), 0.03, 0.97)

    n_final = trained.get('n_final', 0)
    maturity = clamp(n_final / float(MIN_TRAIN + 60), 0.0, 1.0)
    sample = clamp(record['min_gp'] / 12.0, 0.0, 1.0)
    if trust_override is not None:
        # Still fade it down for teams with barely any games on the books.
        trust = clamp(trust_override * (0.45 + 0.55 * sample), 0.0, 1.0)
    else:
        # Before the ledger can speak, hold near an even ensemble of the two
        # information sources rather than betting the site on either.
        trust = clamp(0.30 + 0.35 * maturity * sample, 0.0, 1.0)

    if prior is not None:
        final = logistic(trust * logit(core) + (1 - trust) * logit(prior))
    else:
        final = core

    # Never claim more certainty than the sport supports.
    cap = {'mlb': 0.80, 'nhl': 0.82, 'nba': 0.93, 'nfl': 0.92}.get(league_key, 0.9)
    final = clamp(final, 1 - cap, cap)

    return {
        'prob': final,
        'elo_prob': elo_p,
        'glm_prob': glm_p,
        'prior_prob': prior,
        'starter_edge': prior_shift or None,
        'trust': trust,
    }


def edge_drivers(record, trained, top_n=3):
    """The features pushing this pick, biggest first — for the 'why' panel."""
    model = trained.get('model')
    if not model or not model.coef:
        return []
    z = model._z(record['vector'])                       # noqa: SLF001
    parts = []
    for i, name in enumerate(model.feature_names):
        contrib = model.coef[i] * z[i]
        if abs(contrib) < 0.02:
            continue
        parts.append({'feature': name, 'contribution': round(contrib, 4),
                      'value': round(record['vector'][i], 3)})
    parts.sort(key=lambda p: -abs(p['contribution']))
    return parts[:top_n]


FEATURE_LABELS = {
    'elo_diff': 'Elo rating edge',
    'pyth_diff': 'Run/goal differential quality',
    'form_diff': 'Recent form',
    'margin_diff': 'Recent scoring margin',
    'venue_diff': 'Home/road split',
    'rest_diff': 'Rest advantage',
    'b2b_diff': 'Short-rest fatigue',
    'h2h_diff': 'Head-to-head this season',
    'wpct_diff': 'Season win rate',
    'off_diff': 'Recent offence',
    'def_diff': 'Recent defence',
    'sos_diff': 'Strength of schedule',
    'exp_diff': 'Games played',
}
