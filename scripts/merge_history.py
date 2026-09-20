#!/usr/bin/env python3
"""Fold another refresh's ledger rows into this one's, losing neither.

Two refreshes can overlap: one checks the repository out, builds for five
minutes, and finds that the other pushed in the meantime. The generated
pages and payloads do not matter — this run's are the fresher build and
simply replace them. The ledgers do. They are the audit trail: a pick is
written once, when it is first published, and never rewritten, so a row the
other run recorded is a row this run has no way to reproduce. Dropping it
would mean republishing that pick later, possibly after the game had
started, which is the one thing the ledger exists to prevent.

So this merges by identity: every row this run has is kept as it stands, and
every row only the other run has is added.

    python3 scripts/merge_history.py THEIRS OURS

THEIRS is the other run's ``history`` directory, OURS is this run's, which
is rewritten in place.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys

# What makes a row itself. A file whose name matches none of these is merged
# on the whole row, which is always safe and only ever misses a genuine
# update to a row both runs hold.
KEYS = {'_ledger.csv': ('game_id',),
        '_games.csv': ('game_id',),
        '_props.csv': ('game_id', 'athlete_id', 'key'),
        '_team_stats.csv': ('date', 'team')}


def key_for(name):
    for suffix, cols in KEYS.items():
        if name.endswith(suffix):
            return cols
    return None


def read_csv(path):
    with io.open(path, encoding='utf-8', newline='') as fh:
        rows = list(csv.DictReader(fh))
    with io.open(path, encoding='utf-8', newline='') as fh:
        header = next(csv.reader(fh), [])
    return header, rows


def identity(row, cols):
    if cols is None:
        return tuple(sorted(row.items()))
    return tuple((row.get(c) or '') for c in cols)


def merge_csv(theirs, ours):
    cols = key_for(os.path.basename(ours))
    header, mine = read_csv(ours)
    _, other = read_csv(theirs)
    have = {identity(r, cols) for r in mine}
    added = [r for r in other if identity(r, cols) not in have]
    if not added:
        return 0
    with io.open(ours, 'a', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction='ignore')
        for row in added:
            # A column one side does not have yet — a build either side of a
            # schema change — is written blank rather than dropping the row.
            writer.writerow({c: row.get(c, '') for c in header})
    return len(added)


def merge_boards(theirs, ours):
    with io.open(ours, encoding='utf-8') as fh:
        mine = json.load(fh)
    with io.open(theirs, encoding='utf-8') as fh:
        other = json.load(fh)
    added = 0
    for gid, board in other.items():
        current = mine.get(gid)
        # A board is frozen at kickoff and must never thaw: if the other run
        # froze one this run still had open, theirs is the published truth.
        if current is None or (board.get('frozen') and not current.get('frozen')):
            mine[gid] = board
            added += 1
    if added:
        with io.open(ours, 'w', encoding='utf-8') as fh:
            json.dump(mine, fh, separators=(',', ':'), sort_keys=True)
    return added


def merge(theirs_dir, ours_dir):
    changes = []
    if not os.path.isdir(theirs_dir):
        return changes
    for name in sorted(os.listdir(theirs_dir)):
        theirs = os.path.join(theirs_dir, name)
        ours = os.path.join(ours_dir, name)
        if not os.path.isfile(theirs):
            continue
        if not os.path.exists(ours):
            # A file only the other run has: take it whole.
            with io.open(theirs, 'rb') as src, io.open(ours, 'wb') as dst:
                dst.write(src.read())
            changes.append(f'{name}: taken from the other run')
            continue
        try:
            if name.endswith('.csv'):
                added = merge_csv(theirs, ours)
            elif name.endswith('.json'):
                added = merge_boards(theirs, ours)
            else:
                continue
        except Exception as exc:                        # noqa: BLE001
            # A file that cannot be merged is left exactly as this run wrote
            # it. Publishing a run's own ledger is always better than
            # failing to publish at all.
            changes.append(f'{name}: could not be merged ({type(exc).__name__}: {exc}); kept ours')
            continue
        if added:
            changes.append(f'{name}: {added} row(s) from the other run')
    return changes


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    for line in merge(argv[0], argv[1]):
        print('  ' + line)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
