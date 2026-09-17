"""The optimiser: every run, try to beat the settings the site is using.

The learning loop already refits the game model each hour and keeps new
settings only when they validate better (``LeagueMemory.adopt``). This
module widens what "new settings" can mean. Starting from the incumbent it
walks the neighbourhood of

* the form window and its decay (how many recent games matter, and how fast
  older ones fade),
* the feature set (each core signal can be dropped, each extra one added),
* the rating engine's speed and home edge (``model.tune_elo``),

scoring every candidate by walk-forward log loss — the loss a reader would
have seen, since each historical game is predicted only from games before
it. One seeded random probe per run keeps the search from settling into a
corner: over a season of hourly runs that is a few thousand free looks at
settings coordinate descent would never reach.

Nothing here touches a published pick. The winner becomes the challenger
that ``adopt`` judges against the incumbent, with the same evidence bar.
"""
from __future__ import annotations

import random

from . import features as feat
from . import model

FORM_WINDOWS = (8, 12, 16, 20, 25, 30, 40)
FORM_DECAYS = (0.80, 0.85, 0.90, 0.94, 0.97, 1.0)
MIN_IMPROVEMENT = 1e-4      # in log loss; below this is noise
MAX_EVALS = 36              # each is a full walk-forward fit, well under a second


def incumbent_settings(prev_params, cfg):
    """The settings the site is using now, filled in with defaults."""
    prev = prev_params or {}
    return {
        'elo_params': dict(prev.get('elo_params') or cfg['elo']),
        'form': dict(feat.DEFAULT_FORM, **(prev.get('form') or {})),
        'features': list(prev.get('features') or feat.FEATURE_NAMES),
    }


def _neighbours(settings):
    """Every one-step move from ``settings``: nudge the window, nudge the
    decay, drop one core feature, add one extra feature."""
    out = []
    form = settings['form']
    n_i = _nearest_index(FORM_WINDOWS, form['n'])
    for j in (n_i - 1, n_i + 1):
        if 0 <= j < len(FORM_WINDOWS):
            out.append(('form window', dict(settings, form=dict(form, n=FORM_WINDOWS[j]))))
    d_i = _nearest_index(FORM_DECAYS, form['decay'])
    for j in (d_i - 1, d_i + 1):
        if 0 <= j < len(FORM_DECAYS):
            out.append(('form decay', dict(settings, form=dict(form, decay=FORM_DECAYS[j]))))
    names = settings['features']
    for name in feat.FEATURE_NAMES + feat.EXTRA_FEATURES:
        if name in names:
            if len(names) > 3:
                out.append((f'without {name}', dict(settings, features=[x for x in names if x != name])))
        else:
            out.append((f'with {name}', dict(settings, features=names + [name])))
    return out


def _nearest_index(grid, value):
    return min(range(len(grid)), key=lambda i: abs(grid[i] - value))


def random_probe(settings, seed):
    """One random setting, reproducible for the hour it runs in."""
    rng = random.Random(seed)
    form = {'n': rng.choice(FORM_WINDOWS), 'decay': rng.choice(FORM_DECAYS)}
    pool = feat.FEATURE_NAMES + feat.EXTRA_FEATURES
    features = [n for n in pool if rng.random() < 0.6]
    for must in ('elo_diff', 'pyth_diff'):        # the two signals every version keeps
        if must not in features:
            features.append(must)
    features = [n for n in pool if n in features]   # canonical order
    return {'elo_params': dict(settings['elo_params']), 'form': form, 'features': features}


def search(league_key, rows, cfg, prev_params, seed='', tune_elo=True, max_evals=MAX_EVALS):
    """Find the best-validating settings reachable from the incumbent.

    Returns ``(settings, trained, report)``: the winning settings, the fitted
    model for them, and a plain record of what was tried for the site and
    the state file. When the log is too short to validate, the incumbent
    comes back untouched with ``report['stage']`` saying why.
    """
    settings = incumbent_settings(prev_params, cfg)
    if tune_elo:
        elo, _ = model.tune_elo(league_key, rows, cfg)
        settings['elo_params'] = dict(elo)

    def fit(s):
        return model.train(league_key, rows, cfg, elo_params=s['elo_params'], tune=False,
                           form=s['form'], features=s['features'])

    best = fit(settings)
    best_ll = (best.get('metrics') or {}).get('logloss')
    report = {'stage': best.get('stage'), 'evals': 1, 'start': best_ll, 'moves': [],
              'probe': None, 'seed': seed}
    if best.get('stage') != 'trained' or best_ll is None:
        return settings, best, report

    evals = 1
    improved = True
    while improved and evals < max_evals:
        improved = False
        for label, cand in _neighbours(settings):
            if evals >= max_evals:
                break
            trial = fit(cand)
            evals += 1
            ll = (trial.get('metrics') or {}).get('logloss')
            if ll is not None and ll < best_ll - MIN_IMPROVEMENT:
                report['moves'].append({'move': label, 'from': round(best_ll, 4), 'to': round(ll, 4)})
                settings, best, best_ll = cand, trial, ll
                improved = True
                break                       # restart the neighbourhood from the new point

    probe = random_probe(settings, seed)
    trial = fit(probe)
    evals += 1
    ll = (trial.get('metrics') or {}).get('logloss')
    report['probe'] = {'form': probe['form'], 'features': probe['features'],
                       'logloss': round(ll, 4) if ll is not None else None,
                       'won': bool(ll is not None and ll < best_ll - MIN_IMPROVEMENT)}
    if report['probe']['won']:
        report['moves'].append({'move': 'random probe', 'from': round(best_ll, 4), 'to': round(ll, 4)})
        settings, best, best_ll = probe, trial, ll

    report['evals'] = evals
    report['end'] = round(best_ll, 4)
    return settings, best, report


def describe(settings):
    """The settings in words, for the page."""
    form = settings.get('form') or feat.DEFAULT_FORM
    names = settings.get('features') or feat.FEATURE_NAMES
    labels = model.FEATURE_LABELS
    return {
        'form_window': int(form['n']),
        'form_decay': float(form['decay']),
        'signals': [labels.get(n, n) for n in names],
        'elo': {k: round(float(v), 2) for k, v in (settings.get('elo_params') or {}).items()},
    }
