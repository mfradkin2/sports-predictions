#!/usr/bin/env python3
"""Build the Sports Predictions site — ingest, learn, predict, render.

    python3 run_pipeline.py                 # every league, with player props
    python3 run_pipeline.py mlb nfl         # just these leagues
    python3 run_pipeline.py --no-ingest     # build from the CSVs already on disk
    python3 run_pipeline.py --no-props      # skip the player-stat fetch
    python3 run_pipeline.py --no-tune       # reuse the stored Elo parameters

Each league is independent: one failing does not stop the others, and the site
is rewritten from whatever succeeded. An ingest that cannot reach ESPN keeps
the previous CSV, so a feed outage degrades to stale data rather than no site.
"""
from __future__ import annotations

import sys
import traceback

from sportspred import ingest, pipeline, render
from sportspred.config import LEAGUE_ORDER, LEAGUES
from sportspred.util import Http


def main(argv):
    args = [a for a in argv if not a.startswith('--')]
    flags = {a for a in argv if a.startswith('--')}
    leagues = [a.lower() for a in args if a.lower() in LEAGUES] or list(LEAGUE_ORDER)
    fetch_props = '--no-props' not in flags
    do_ingest = '--no-ingest' not in flags
    tune = '--no-tune' not in flags

    http = Http(budget_s=240) if fetch_props else None
    payloads, failures = {}, []

    for key in leagues:
        print(f'\n=== {LEAGUES[key]["name"]} ===')
        if do_ingest:
            try:
                ingest.run(key, http=Http(timeout=15, retries=2, pause=0.15, budget_s=600))
            except Exception as exc:                 # noqa: BLE001
                print(f'  ingest failed ({type(exc).__name__}: {exc}); '
                      'building from the previous CSV')
                traceback.print_exc(limit=2)
        try:
            payload, trained, memory = pipeline.run(
                key, fetch_props=fetch_props, http=http, tune=tune)
        except Exception as exc:                     # noqa: BLE001
            failures.append(key)
            print(f'  FAILED: {type(exc).__name__}: {exc}')
            traceback.print_exc(limit=3)
            continue

        payloads[key] = payload
        model = payload['model']
        val = model['validation'] or {}
        path, size = render.write_payload(payload)
        print(f'  archive {model["archive"]} games · trained on {model["n_train"]}')
        if model.get('stage') == 'trained' and val.get('acc') is not None:
            print(f'  validated {val["acc"]*100:.1f}% acc · '
                  f'brier {val["brier"]} · log loss {val["logloss"]} (n={val["n"]})')
        elif model.get('n_train'):
            print(f'  warming up — {model["n_train"]} games on record, '
                  f'{model.get("min_train", 110)} needed before validation is meaningful')
        acc = payload['accuracy']
        ver = acc['verified']
        if ver['total']:
            print(f'  verified pre-game record {ver["correct"]}/{ver["total"]} '
                  f'= {ver["pct"]*100:.1f}%')
        else:
            print('  verified pre-game record: none yet '
                  '(builds up as forecasts are published and graded)')
        if acc.get('backfilled'):
            print(f'  {acc["backfilled"]} completed games were first seen after the '
                  'fact and are excluded from the record')
        props = sum(1 for g in payload['games'] if g.get('props'))
        print(f'  {len(payload["games"])} games · props on {props} · feed: {payload["props_status"]}')
        print(f'  wrote {path} ({size/1024:.0f} KB)')

    if not payloads:
        print('\nNo league produced a payload; leaving the existing site in place.')
        return 1

    written, version = render.write_site(payloads)
    print(f'\nSite rebuilt (assets v{version}): {", ".join(written)}')
    if failures:
        print(f'Leagues that failed this run: {", ".join(failures)}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
