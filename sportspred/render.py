"""Emit the static site: one data file per league plus very small HTML shells.

The previous generator inlined every game, every stylesheet rule and the whole
script into each league page — ``mlb.html`` had grown past 800 KB, and the four
pages shared nothing, so a visitor downloaded four copies of the same CSS and
JavaScript. Here each page is a few kilobytes of shell, the stylesheet and
script are shared (and therefore cached once), and the data arrives as JSON.
"""
from __future__ import annotations

import hashlib
import json
import os

from . import config
from .config import LEAGUE_ORDER, LEAGUES, SITE_NAME

TAGLINE = 'Model-driven game predictions and player prop projections'


def asset_version():
    """Short hash of the shared assets, used to bust caches on change."""
    h = hashlib.sha256()
    for name in ('app.css', 'app.js'):
        path = os.path.join(config.ASSET_DIR, name)
        if os.path.exists(path):
            with open(path, 'rb') as f:
                h.update(f.read())
    return h.hexdigest()[:8]


def js_safe(text):
    """Make a JSON string safe to sit inside a script.

    The payload is loaded as an external file today, where these characters are
    inert, but escaping them costs nothing and keeps the file safe if it is
    ever inlined. U+2028 and U+2029 are escaped because they are valid JSON but
    illegal raw inside a JavaScript string literal, and a single one anywhere
    in a team or player name would stop the whole page parsing.
    """
    return (text.replace('<', '\\u003c')
                .replace('>', '\\u003e')
                .replace('\u2028', '\\u2028')
                .replace('\u2029', '\\u2029'))


def write_payload(payload):
    """Data as an assignment in a .js file rather than JSON fetched at runtime.

    A ``<script src>`` works from the file system as well as over HTTP, so the
    site can be opened straight from a checkout without a local server.
    """
    league = payload['league']
    history = payload.pop('history', [])
    body = js_safe(json.dumps(payload, separators=(',', ':'), default=str, sort_keys=False))
    js = ('window.SP_DATA=window.SP_DATA||{};\n'
          f'window.SP_DATA[{json.dumps(league)}]={body};\n')
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = os.path.join(config.DATA_DIR, f'{league}.js')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(js)
    os.replace(tmp, path)
    hist_js = ('window.SP_HISTORY=window.SP_HISTORY||{};\n'
               f'window.SP_HISTORY[{json.dumps(league)}]='
               + js_safe(json.dumps(history, separators=(',', ':'), default=str)) + ';\n')
    hist_path = os.path.join(config.DATA_DIR, f'{league}-history.js')
    tmp = hist_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(hist_js)
    os.replace(tmp, hist_path)
    # No separate .json copy: these files are rewritten every hour, and the
    # payload below is already a single JSON object behind a one-line
    # assignment. Anything wanting the raw numbers can strip that prefix.
    return path, len(js) + len(hist_js)


def _shell(title, description, version, default_league, leagues, preload=None):
    preload_tag = (f'\n<script src="data/{preload}.js"></script>' if preload else '')
    league_list = json.dumps(leagues)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="description" content="{description}">
<meta name="color-scheme" content="dark light">
<title>{title}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22><text y=%2213%22 font-size=%2213%22>🎯</text></svg>">
<link rel="preload" as="script" href="assets/app.js?v={version}">
<link rel="stylesheet" href="assets/app.css?v={version}">
<style id="accent-style"></style>
</head>
<body>
<header class="top">
  <div class="brand">
    <span class="brand-mark" aria-hidden="true">🎯</span>
    <span>
      <span class="brand-name">{SITE_NAME}</span>
      <span class="brand-sub" id="brand-sub">{TAGLINE}</span>
    </span>
    <span class="brand-spacer"></span>
    <span class="live-pill" id="live-pill">Loading…</span>
  </div>
  <nav class="sports" id="sports" role="tablist" aria-label="Sport"></nav>
  <nav class="views" id="views" role="tablist" aria-label="View"></nav>
</header>
<main class="wrap" id="main">
  <div class="games"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>
</main>
<script>
window.SP_LEAGUES={league_list};
window.SP_DEFAULT_LEAGUE={json.dumps(default_league)};
</script>{preload_tag}
<script src="assets/app.js?v={version}" defer></script>
</body>
</html>
'''


def pick_default(payloads):
    """Open on a league that actually has something on today, if any."""
    for key in LEAGUE_ORDER:
        p = payloads.get(key)
        if p and any(g['date'] == p['today'] for g in p['games']):
            return key
    best, best_date = None, None
    for key in LEAGUE_ORDER:
        p = payloads.get(key)
        if not p:
            continue
        upcoming = sorted(g['date'] for g in p['games'] if not g['final'])
        if upcoming and (best_date is None or upcoming[0] < best_date):
            best, best_date = key, upcoming[0]
    return best or (LEAGUE_ORDER[0] if not payloads else sorted(payloads)[0])


def write_site(payloads):
    """Write index.html plus one shell per league and return what was built."""
    version = asset_version()
    leagues = [k for k in LEAGUE_ORDER if k in payloads] or LEAGUE_ORDER
    default = pick_default(payloads)
    written = []

    # The hub opens on the cross-sport overview; the league most likely to
    # have games today is preloaded so its board paints immediately.
    index = _shell(
        title=f'{SITE_NAME} — MLB, NFL, NBA & NHL',
        description=f'{TAGLINE} for MLB, NFL, NBA and NHL, refreshed hourly.',
        version=version, default_league='all', leagues=leagues, preload=default)
    _write(os.path.join(config.BASE, 'index.html'), index)
    written.append('index.html')

    for key in leagues:
        cfg = LEAGUES[key]
        page = _shell(
            title=f'{cfg["name"]} Predictions — {SITE_NAME}',
            description=f'{cfg["name"]} game predictions and player prop projections.',
            version=version, default_league=key, leagues=leagues, preload=key)
        _write(os.path.join(config.BASE, f'{key}.html'), page)
        written.append(f'{key}.html')
    return written, version


def _write(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)
