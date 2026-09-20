"""Checks the built site against the code that built it.

The pipeline can finish and still leave something the page cannot show
honestly: a payload missing a block the page reads, a prop whose pick
contradicts its own projection, a page still pointing at a previous version
of the stylesheet. Every one of those has happened at least once.

``run_pipeline.py`` calls :func:`verify` before the workflow publishes, and
the workflow fails rather than push a site that does not hold together, so a
bad build costs one skipped refresh instead of showing wrong numbers until
somebody notices.

Every check answers a question a reader would ask:

* Is this page loading the stylesheet and script that were built with it?
* Does every league have the blocks the page reads?
* Does every game have a pick, a date and an identity?
* Is every prop's lean on the same side as its own projection?
* Is every projection one a player could actually post?
* Does a blank prop really have no line, and a priced one a real line?
* Is every market on the board in exactly one category chip?
"""
from __future__ import annotations

import glob
import json
import os
import re

from . import config
from .props import PER_GAME_MAX

# Blocks the front end reads on every payload. A missing one is a blank
# section or a crash, depending on where it is read.
REQUIRED_KEYS = ('league', 'name', 'emoji', 'accent', 'season', 'espn_path',
                 'generated', 'today', 'model', 'stats', 'prop_groups',
                 'props_status', 'lines_status', 'games', 'accuracy')
# Older games and the team/player records travel in their own files, loaded
# only when the section that needs them is opened.
SIDE_FILES = (('{league}-history.js', 'window.SP_HISTORY'),
              ('{league}-records.js', 'window.SP_RECORDS'))
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class SiteProblem(Exception):
    """A built site that should not be published."""


def _payload(path, marker='window.SP_DATA['):
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    start = text.index('=', text.index(marker)) + 1
    return json.loads(text[start:].strip().rstrip(';'))


def check_pages(base, problems):
    """Every page must load the assets it was built with."""
    from .render import asset_version
    want = asset_version()
    pages = sorted(glob.glob(os.path.join(base, '*.html')))
    if not pages:
        problems.append('no pages were written')
        return
    for page in pages:
        with open(page, encoding='utf-8') as fh:
            html = fh.read()
        for asset in ('app.js', 'app.css'):
            found = re.findall(r'assets/' + re.escape(asset) + r'\?v=([a-f0-9]+)', html)
            if not found:
                problems.append(f'{os.path.basename(page)} does not load {asset}')
            elif any(v != want for v in found):
                # The page was rendered by a different revision of the code
                # than the one in the tree: publishing it serves a cached
                # script against a new payload.
                problems.append(f'{os.path.basename(page)} asks for {asset}?v={found[0]}, '
                                f'but the assets in this build hash to {want}')


def check_payload(league, data, problems):
    where = f'{league}.js'
    for key in REQUIRED_KEYS:
        if key not in data:
            problems.append(f'{where}: no "{key}" block for the page to read')
    if not DATE.match(str(data.get('today', ''))):
        problems.append(f'{where}: "today" is {data.get("today")!r}, not a date')

    sport = config.LEAGUES[league]['sport']
    groups = data.get('prop_groups') or []
    placed = [m for g in groups for m in (g.get('markets') or [])]
    markets = {s['key'] for specs in (config.PROPS.get(sport) or {}).values() for s in specs}
    missing = sorted(markets - set(placed))
    if missing:
        problems.append(f'{where}: markets in no category chip: {", ".join(missing)}')
    if len(placed) != len(set(placed)):
        problems.append(f'{where}: a market appears in two category chips')

    for game in data.get('games') or []:
        gid = game.get('id') or '(no id)'
        if not game.get('id'):
            problems.append(f'{where}: a game has no id')
        if not DATE.match(str(game.get('date', ''))):
            problems.append(f'{where}: game {gid} has date {game.get("date")!r}')
        if not game.get('favored'):
            problems.append(f'{where}: game {gid} has no pick')
        check_board(where, gid, game.get('props') or {}, sport, problems)


def check_board(where, gid, board, sport, problems):
    for side in ('away', 'home'):
        for player in board.get(side) or []:
            name = player.get('name') or '(unnamed)'
            for prop in player.get('props') or []:
                key = prop.get('key', '?')
                tag = f'{where}: {name} {key} in game {gid}'
                line, proj, pick = prop.get('line'), prop.get('proj'), prop.get('pick')
                if prop.get('pending'):
                    if line is not None:
                        problems.append(f'{tag} is shown as waiting on a line but has one')
                    continue
                if line is None:
                    problems.append(f'{tag} is priced but has no line')
                    continue
                if pick not in ('over', 'under'):
                    problems.append(f'{tag} has no side')
                elif proj is not None:
                    # The lean has to be on the side the projection lands on,
                    # or the row argues with itself.
                    if (proj > line and pick == 'under') or (proj < line and pick == 'over'):
                        problems.append(f'{tag} leans {pick} with a projection of {proj} against {line}')
                stat = prop.get('stat') or ''
                stat = stat[:-3] if stat.endswith('_pg') else stat
                cap = PER_GAME_MAX.get(stat)
                if cap is not None and proj is not None and proj > cap:
                    problems.append(f'{tag} projects {proj}, which no {stat} ever reaches')


def verify(base=None, data_dir=None, leagues=None):
    """Return the list of reasons this build should not be published."""
    base = base or config.BASE
    data_dir = data_dir or config.DATA_DIR
    problems = []
    check_pages(base, problems)
    for league in (leagues or config.LEAGUES):
        path = os.path.join(data_dir, f'{league}.js')
        if not os.path.exists(path):
            problems.append(f'{league}.js was not written')
            continue
        try:
            data = _payload(path)
        except Exception as exc:                        # noqa: BLE001
            problems.append(f'{league}.js could not be read back: {exc}')
            continue
        check_payload(league, data, problems)
        for pattern, marker in SIDE_FILES:
            name = pattern.format(league=league)
            side = os.path.join(data_dir, name)
            if not os.path.exists(side):
                problems.append(f'{name} was not written')
                continue
            try:
                _payload(side, marker + '[')
            except Exception as exc:                    # noqa: BLE001
                problems.append(f'{name} could not be read back: {exc}')
    return problems


def report(problems, limit=25):
    if not problems:
        return 'Site checks passed.'
    lines = [f'{len(problems)} problem(s) with this build:']
    lines += ['  - ' + p for p in problems[:limit]]
    if len(problems) > limit:
        lines.append(f'  ... and {len(problems) - limit} more')
    return '\n'.join(lines)
