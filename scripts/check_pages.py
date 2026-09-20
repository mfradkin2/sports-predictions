#!/usr/bin/env python3
"""Open the built site in a real browser and check it behaves.

``sportspred/verify.py`` checks the data a build produces. This checks the
pages that data is poured into: that every section renders, that clicking a
section's tab lands where its name promises, that every count on a rail
matches the list underneath it, and that nothing throws or overflows.

    python3 scripts/check_pages.py            # the site in this directory
    python3 scripts/check_pages.py --dir out  # a site built somewhere else

It needs Playwright and a browser, which the hourly refresh deliberately does
not: that build is standard-library Python with nothing to install. So this
is run by hand, or by the daily review, after a change to the front end.
Without Playwright it says so and exits 0 rather than failing a pipeline that
was never meant to depend on it.

Exits non-zero, listing what it found, if anything is wrong.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import socketserver
import sys
import threading

# Text that means a value went missing on the way to the page.
BROKEN_TEXT = re.compile(r'undefined|NaN|\[object |null%|Infinity')
LEAGUES = ('all', 'mlb', 'nfl', 'nba', 'nhl')
# Section, the address that is its front door, and what must be selected when
# you arrive by clicking its tab.
SECTIONS = (('featured', '#{lg}/featured', None),
            ('games', '#{lg}/games', 'today'),
            ('props', '#{lg}/props', 'open'),
            ('results', '#{lg}/results', 'recent'))
BROWSERS = ('/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
            '/opt/pw-browsers/chromium/chrome-linux/chrome')


def serve(directory):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):          # noqa: A003 - the base class names it
            pass
    handler = functools.partial(Quiet, directory=directory)
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, 'http://127.0.0.1:%d' % server.server_address[1]


def selected_rail(page):
    """Which rail option is on, by its identity rather than its label: the
    label is shortened on a phone."""
    out = []
    for tab in page.locator('.rail-tab').all():
        if tab.get_attribute('aria-selected') == 'true':
            out.append(tab.get_attribute('data-tab') or tab.get_attribute('data-value') or '?')
    return out


def check(page, base, problems):
    def visit(hash_, wait=1600):
        page.goto(base + '/index.html' + hash_)
        page.wait_for_timeout(wait)

    for league in LEAGUES:
        for name, address, _ in SECTIONS:
            errors = []
            page.once('pageerror', lambda e: errors.append(str(e)))
            visit(address.format(lg=league))
            text = page.inner_text('#main')
            where = f'{league}/{name}'
            if errors:
                problems.append(f'{where}: the page threw {errors[0][:90]}')
            if len(text.strip()) < 20:
                problems.append(f'{where}: rendered nothing')
            bad = BROKEN_TEXT.findall(text)
            if bad:
                problems.append(f'{where}: shows {bad[0]!r} where a value should be')
            if page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth + 2'):
                problems.append(f'{where}: scrolls sideways at this width')

    # A section's tab must land on its front door, whatever the reader looked
    # at last. This is the check that would have caught the Results tab
    # sticking on Track record and showing no finished games at all.
    visit('#mlb/results')
    page.locator('.rail-tab[data-tab="record"]').click()
    page.wait_for_timeout(1200)
    for name, _, want in SECTIONS:
        page.locator(f'.view-tab[data-view="{name}"]').click()
        page.wait_for_timeout(1400)
        if want and want not in selected_rail(page):
            problems.append(f'the {name} tab opened on {selected_rail(page)}, not {want!r}')

    # Every count on a rail is a promise about the list under it.
    visit('#mlb/games')
    for value in [t.get_attribute('data-value') for t in page.locator('.rail-tab').all()]:
        if not value:
            continue
        page.locator(f'.rail-tab[data-value="{value}"]').click()
        page.wait_for_timeout(700)
        badge = page.locator(f'.rail-tab[data-value="{value}"] .n')
        if not badge.count():
            continue
        claimed, shown = int(badge.inner_text()), page.locator('.game').count()
        if claimed != shown:
            problems.append(f'games/{value}: the rail says {claimed} but the board lists {shown}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument('--width', type=int, nargs='*', default=[1280, 400])
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('Playwright is not installed, so the page checks were skipped.')
        print('  pip install playwright && playwright install chromium')
        return 0
    executable = next((p for p in BROWSERS if os.path.exists(p)), None)

    server, base = serve(args.dir)
    problems = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=executable)
            for width in args.width:
                page = browser.new_page(viewport={'width': width, 'height': 900})
                # The live feeds are the browser's own business and are not
                # what is under test; refuse them so a slow network cannot
                # turn into a spurious failure.
                page.route('**/*.espn*.com/**', lambda route, req: route.fulfill(status=404, body=''))
                before = len(problems)
                check(page, base, problems)
                for i in range(before, len(problems)):
                    problems[i] = f'[{width}px] ' + problems[i]
                page.close()
            browser.close()
    finally:
        server.shutdown()

    if problems:
        print(f'{len(problems)} problem(s) with the built pages:')
        for p in problems:
            print('  - ' + p)
        return 1
    print(f'Page checks passed at {", ".join(str(w) + "px" for w in args.width)}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
