#!/usr/bin/env python3
"""Is the site healthy right now?

``sportspred/verify.py`` asks whether a build holds together, and runs before
anything is published. This asks the different question the owner actually
cares about: is what readers are looking at right now correct, current, and
still honouring the promises the site makes to them.

    python3 scripts/health_check.py           # everything
    python3 scripts/health_check.py mlb nfl   # only these leagues
    python3 scripts/health_check.py --quiet   # just the verdict

Findings come at two levels. A PROBLEM is something a reader would be wrong
to trust — a stale board, a pick that changed after kickoff, finished games
that cannot be reached — and exits non-zero. A WATCH is worth knowing but
not worth waking anyone for: a feed degraded to its fallback, a budget
running down. Exit codes: 0 healthy (watches allowed), 1 problems found, 2
the check itself could not run.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import io
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sportspred import config, verify                   # noqa: E402

# The refresh runs four times an hour. One missed run is ordinary — a feed
# hiccup, a slow build. Missing them for over two hours is not.
STALE_WATCH_MIN = 75
STALE_PROBLEM_MIN = 150
# Below this many Odds API credits the board starts falling back to lines
# derived from season baselines.
ODDS_LOW = 500
# A market the site publishes but cannot score: its picks never reach the
# record and never teach the model anything. Judged on the most recent
# handful rather than all time, which is what makes it useful as an alarm —
# it lights up within about one slate of a market breaking, and goes out as
# soon as a fix actually grades something, instead of staying lit for a week
# over damage already done and unfixable.
RECENT_PICKS = 10
# Markets that cannot be graded, are still published anyway, and are
# understood — reported as watches rather than as news. Empty at present:
# the one case, MLB stolen bases, was retired rather than lived with. Kept
# because the next such market should be a deliberate entry here rather than
# a permanently red alarm.
KNOWN_UNGRADEABLE = {}


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(text):
    """ESPN and the payload use a few shapes; return None rather than raise."""
    if not text:
        return None
    text = str(text).replace('Z', '+00:00')
    for shape in (None, '%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            when = dt.datetime.fromisoformat(text) if shape is None else dt.datetime.strptime(text, shape)
        except ValueError:
            continue
        return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
    return None


def rows(path):
    if not os.path.exists(path):
        return []
    with io.open(path, encoding='utf-8', newline='') as fh:
        return list(csv.DictReader(fh))


class Report:
    def __init__(self):
        self.problems = []
        self.watches = []
        self.notes = []

    def problem(self, where, what):
        self.problems.append(f'{where}: {what}')

    def watch(self, where, what):
        self.watches.append(f'{where}: {what}')

    def note(self, text):
        self.notes.append(text)


def check_freshness(league, data, report):
    """A board nobody has refreshed is the failure readers notice first."""
    generated = parse_time(data.get('generated'))
    if generated is None:
        report.problem(league, f'the board does not say when it was built ({data.get("generated")!r})')
        return
    age = (now() - generated).total_seconds() / 60
    if age > STALE_PROBLEM_MIN:
        report.problem(league, f'the board is {age/60:.1f} hours old; the refresh runs every 15 minutes')
    elif age > STALE_WATCH_MIN:
        report.watch(league, f'the board is {age:.0f} minutes old — a refresh or two has been missed')
    else:
        report.note(f'{league}: built {age:.0f} minutes ago')


def check_locks(league, data, report):
    """A published pick is frozen at kickoff. That is the site's one
    promise that cannot be repaired after the fact, so it is checked
    against the ledger rather than trusted."""
    ledger = {r['game_id']: r for r in rows(os.path.join(config.BASE, 'history', f'{league}_ledger.csv'))}
    late = []
    for game in data.get('games') or []:
        start = parse_time(game.get('start'))
        if not start:
            continue
        # Checked before the ledger lookup on purpose: a game that was never
        # locked is exactly the case where its ledger row may be missing too.
        if start < now() and game.get('final') and not game.get('locked'):
            report.problem(league, f'game {game.get("id")} has finished but was never locked')
        row = ledger.get(str(game.get('id')))
        if not row:
            continue
        predicted = parse_time(row.get('predicted_at'))
        if predicted and predicted > start + dt.timedelta(minutes=5) and row.get('replay') != '1':
            late.append(f'{game.get("id")} ({predicted:%Y-%m-%d %H:%M} vs a {start:%H:%M} start)')
    if late:
        report.problem(league, f'{len(late)} pick(s) were first recorded after the game started: '
                               + ', '.join(late[:3]))


def check_results_reachable(league, data, report):
    """The fault that hid every finished game: plenty of graded rows, none of
    them reachable on the page."""
    graded = [r for r in rows(os.path.join(config.BASE, 'history', f'{league}_ledger.csv'))
              if r.get('graded') == '1' and r.get('preseason') != '1']
    if not graded:
        return
    live_finals = sum(1 for g in data.get('games') or [] if g.get('final'))
    path = os.path.join(config.DATA_DIR, f'{league}-history.js')
    archived = 0
    if os.path.exists(path):
        try:
            archived = len(verify._payload(path, 'window.SP_HISTORY['))
        except Exception as exc:                        # noqa: BLE001
            report.problem(league, f'the finished-games file cannot be read back: {exc}')
            return
    if live_finals + archived == 0:
        report.problem(league, f'{len(graded)} games are graded in the ledger but none can be '
                               'reached in Results')
    else:
        report.note(f'{league}: {live_finals + archived} finished games reachable in Results')


def check_ledgers(league, report):
    """The ledgers are the audit trail. A duplicate means one game has two
    stories; a verdict without a result means the record is counting air."""
    base = os.path.join(config.BASE, 'history')
    ledger = rows(os.path.join(base, f'{league}_ledger.csv'))
    seen = collections.Counter(r['game_id'] for r in ledger if r.get('game_id'))
    dup = [k for k, n in seen.items() if n > 1]
    if dup:
        report.problem(league, f'{len(dup)} game(s) appear twice in the ledger: {", ".join(dup[:3])}')
    for row in ledger:
        if row.get('graded') == '1' and row.get('correct') not in ('0', '1'):
            report.problem(league, f'game {row.get("game_id")} is graded but its verdict is '
                                   f'{row.get("correct")!r}')
            break

    props = rows(os.path.join(base, f'{league}_props.csv'))
    keys = collections.Counter((r.get('game_id'), r.get('athlete_id'), r.get('key')) for r in props)
    dup = [k for k, n in keys.items() if n > 1]
    if dup:
        report.problem(league, f'{len(dup)} player prop(s) appear twice in the ledger')
    # Whether a played pick got a verdict is judged per market, by
    # check_gradeability: one row missing one is a feed gap, a whole market
    # missing them is a fault. A void row (a corrupt feed day) owes nothing.


def check_gradeability(league, report):
    """A market that is published but can never be scored.

    This is quiet damage: the picks look fine on the page, and simply never
    arrive in the record. Pass attempts did it for a whole season because the
    box score gives "20/31" and only the completions were read.
    """
    props = rows(os.path.join(config.BASE, 'history', f'{league}_props.csv'))
    played = [r for r in props if r.get('graded') == '1' and r.get('played') == '1'
              and r.get('push') != '1']
    if not played:
        return
    # Only markets the site still publishes. A retired one keeps its rows in
    # the ledger for ever, and nagging about picks that can no longer be made
    # is how a monitor teaches people to ignore it.
    sport = config.LEAGUES[league]['sport']
    live = {spec['key'] for specs in (config.PROPS.get(sport) or {}).values() for spec in specs}
    recent = collections.defaultdict(list)
    for row in played:                       # the ledger is written in order
        if row.get('key') in live:
            recent[row.get('key')].append(row)
    for key in sorted(recent):
        last = recent[key][-RECENT_PICKS:]
        if len(last) < RECENT_PICKS or any(r.get('hit') in ('0', '1') for r in last):
            continue                         # one scored pick proves it can be
        known = KNOWN_UNGRADEABLE.get((league, key))
        where = (f'none of the last {len(last)} {key} picks were scored, though every '
                 'one of those players took the field')
        if known:
            report.watch(league, f'{where} — {known}')
        else:
            report.problem(league, f'{where}. This market is being published but cannot be '
                                   'graded, so its picks never reach the record')


def check_boards(league, data, report):
    """A board is frozen at kickoff and must never thaw."""
    path = os.path.join(config.BASE, 'history', f'{league}_boards.json')
    if not os.path.exists(path):
        return
    try:
        with io.open(path, encoding='utf-8') as fh:
            boards = json.load(fh)
    except Exception as exc:                            # noqa: BLE001
        report.problem(league, f'the frozen boards file cannot be read: {exc}')
        return
    starts = {str(g.get('id')): parse_time(g.get('start')) for g in data.get('games') or []}
    thawed = [gid for gid, board in boards.items()
              if starts.get(gid) and starts[gid] < now() - dt.timedelta(minutes=30)
              and not board.get('frozen')]
    if thawed:
        report.problem(league, f'{len(thawed)} board(s) for games already under way are not frozen: '
                               + ', '.join(thawed[:3]))


def check_feeds(league, data, report):
    status = data.get('props_status')
    if status and status != 'live':
        report.watch(league, f'the player-statistics feed is {status!r}, not live')
    lines = data.get('lines_status')
    lines = lines if isinstance(lines, dict) else {}
    if lines.get('mode') and lines['mode'] != 'book':
        report.watch(league, f'prop lines are {lines["mode"]!r} — the sportsbook feed is not being used')


def check_odds_budget(report):
    path = os.path.join(config.DATA_DIR, 'odds_budget.json')
    if not os.path.exists(path):
        return
    try:
        with io.open(path, encoding='utf-8') as fh:
            budget = json.load(fh)
    except Exception as exc:                            # noqa: BLE001
        report.watch('odds', f'the credit ledger cannot be read: {exc}')
        return
    left = budget.get('remaining')
    if isinstance(left, (int, float)):
        if left <= 0:
            report.problem('odds', 'the sportsbook credit budget is spent; lines are coming from '
                                   'season baselines instead of the market')
        elif left < ODDS_LOW:
            report.watch('odds', f'{left:.0f} sportsbook credits left this month')
        else:
            report.note(f'odds: {left:.0f} credits left this month')


def check_checkout(report):
    """Everything below is read off this checkout. If it is behind what is
    published, the findings describe this copy and not the site — which is a
    very easy way to spend an hour chasing a fault that does not exist."""
    def git(*args):
        try:
            out = subprocess.run(('git',) + args, cwd=config.BASE, capture_output=True,
                                 text=True, timeout=30)
        except Exception:                               # noqa: BLE001
            return None
        return (out.stdout or '').strip() if out.returncode == 0 else None

    behind = git('rev-list', '--count', 'HEAD..origin/main')
    if behind is None:
        report.watch('checkout', 'could not be compared with what is published')
    elif behind != '0':
        report.problem('checkout', f'this copy is {behind} commit(s) behind origin/main. '
                                   'Run "git fetch origin main && git checkout -B main origin/main" '
                                   'and check again — the findings below may describe this copy '
                                   'rather than the live site')


def check_publishing(report):
    """A refresh that builds but never pushes looks healthy from inside the
    repository and is invisible from outside it."""
    try:
        out = subprocess.run(['git', 'log', '-1', '--format=%cI'], cwd=config.BASE,
                             capture_output=True, text=True, timeout=30)
    except Exception:                                   # noqa: BLE001
        return
    when = parse_time((out.stdout or '').strip())
    if when is None:
        return
    age = (now() - when).total_seconds() / 60
    if age > STALE_PROBLEM_MIN:
        report.problem('publishing', f'nothing has been committed for {age/60:.1f} hours; '
                                     'the refresh may be failing before it pushes')
    elif age > STALE_WATCH_MIN:
        report.watch('publishing', f'nothing has been committed for {age:.0f} minutes')


def _guard(check, report, where, *args):
    """Run one check. A monitor that throws is a monitor that sees nothing,
    so a check that breaks is reported as a broken check rather than taking
    every other check down with it."""
    try:
        check(*args)
    except Exception as exc:                            # noqa: BLE001
        report.problem(where, f'the {check.__name__} check could not run '
                              f'({type(exc).__name__}: {exc})')


def run(leagues):
    report = Report()
    check_checkout(report)
    built = verify.verify(leagues=leagues)
    for problem in built:
        report.problem('build', problem)
    for league in leagues:
        path = os.path.join(config.DATA_DIR, f'{league}.js')
        if not os.path.exists(path):
            report.problem(league, 'has no payload at all')
            continue
        try:
            data = verify._payload(path)
        except Exception as exc:                        # noqa: BLE001
            report.problem(league, f'the payload cannot be read back: {exc}')
            continue
        for check in (check_freshness, check_locks, check_results_reachable,
                      check_boards, check_feeds):
            _guard(check, report, league, league, data, report)
        for check in (check_ledgers, check_gradeability):
            _guard(check, report, league, league, report)
    _guard(check_odds_budget, report, 'odds', report)
    _guard(check_publishing, report, 'publishing', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('leagues', nargs='*', help='leagues to check (default: all)')
    parser.add_argument('--quiet', action='store_true', help='print only the verdict')
    args = parser.parse_args(argv)
    leagues = [x.lower() for x in args.leagues if x.lower() in config.LEAGUES] or list(config.LEAGUES)

    try:
        report = run(leagues)
    except Exception as exc:                            # noqa: BLE001
        print(f'The health check could not run: {type(exc).__name__}: {exc}')
        return 2

    if not args.quiet:
        for note in report.notes:
            print('  ok      ' + note)
        for watch in report.watches:
            print('  WATCH   ' + watch)
        for problem in report.problems:
            print('  PROBLEM ' + problem)
        print()
    if report.problems:
        print(f'{len(report.problems)} problem(s) and {len(report.watches)} watch(es). '
              'The site needs attention.')
        return 1
    if report.watches:
        print(f'Healthy, with {len(report.watches)} watch(es).')
        return 0
    print('Healthy.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
