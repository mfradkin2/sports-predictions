"""Small shared helpers: parsing, math, dates, JSON/CSV IO, HTTP with cache."""
from __future__ import annotations

import csv
import json
import math
import os
import random
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── numbers ──────────────────────────────────────────────────────────────────
def num(value, default=None):
    """Parse anything to float, tolerating '', 'NA', None, '12.3%'."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return default if isinstance(value, float) and math.isnan(value) else float(value)
    s = str(value).strip().replace('%', '').replace(',', '')
    if s == '' or s.upper() in ('NA', 'N/A', 'NAN', 'NULL', '-', '--', 'TBD'):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def logistic(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-min(x, 700.0)))
    e = math.exp(max(x, -700.0))
    return e / (1.0 + e)


def logit(p, eps=1e-6):
    p = clamp(p, eps, 1 - eps)
    return math.log(p / (1 - p))


def mean(xs, default=0.0):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else default


def stdev(xs, default=0.0):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return default
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# ── scoring rules ────────────────────────────────────────────────────────────
def brier(prob, outcome):
    return (prob - outcome) ** 2


def log_loss(prob, outcome, eps=1e-9):
    p = clamp(prob, eps, 1 - eps)
    return -(outcome * math.log(p) + (1 - outcome) * math.log(1 - p))


# ── distributions (pure python, no scipy) ────────────────────────────────────
def poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    if k < 0:
        return 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def poisson_cdf(k, lam):
    """P(X <= k)."""
    if k < 0:
        return 0.0
    total, term = 0.0, None
    for i in range(0, int(k) + 1):
        term = poisson_pmf(i, lam)
        total += term
        if total >= 1.0:
            return 1.0
    return clamp(total, 0.0, 1.0)


def poisson_sf(k, lam):
    """P(X > k)."""
    return clamp(1.0 - poisson_cdf(k, lam), 0.0, 1.0)


def binom_sf(k, n, p):
    """P(X > k) for a binomial with (possibly fractional) n trials.

    Fractional trials are interpolated between the two whole-number cases so
    a 3.8-at-bat hitter sits between 3 and 4, not at one of them.
    """
    p = clamp(p, 0.0, 1.0)
    lo = int(math.floor(n))
    frac = n - lo

    def sf_int(m):
        if m <= 0:
            return 0.0
        total = 0.0
        for i in range(0, min(k, m) + 1):
            total += math.comb(m, i) * (p ** i) * ((1 - p) ** (m - i))
        return clamp(1.0 - total, 0.0, 1.0)

    if frac < 1e-9:
        return sf_int(lo)
    return (1 - frac) * sf_int(lo) + frac * sf_int(lo + 1)


def negbin_sf(k, mu, var):
    """P(X > k) for a negative binomial with the given mean and variance.

    Falls back to Poisson when the requested variance is not over-dispersed.
    """
    if var <= mu * 1.001 or mu <= 0:
        return poisson_sf(k, mu)
    r = mu * mu / (var - mu)
    p = r / (r + mu)          # P(success); X counts failures
    total = 0.0
    log_pr = r * math.log(p)
    for i in range(0, int(k) + 1):
        lp = log_pr + math.lgamma(i + r) - math.lgamma(r) - math.lgamma(i + 1) + i * math.log(1 - p)
        total += math.exp(lp)
        if total >= 1.0:
            return 0.0
    return clamp(1.0 - total, 0.0, 1.0)


def norm_cdf(x, mu=0.0, sigma=1.0):
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def norm_ppf(p, mu=0.0, sigma=1.0):
    """The inverse of :func:`norm_cdf` — the value a probability sits at.

    Turns a win probability into the margin that implies: a 62% favourite in
    a league whose margins scatter by fourteen points is favoured by about
    four and a half. Rational approximation from Wichura's AS241, accurate to
    well past the precision anything here needs.
    """
    p = clamp(float(p), 1e-9, 1 - 1e-9)
    q = p - 0.5
    if abs(q) <= 0.425:
        r = 0.180625 - q * q
        num = (((((((2509.0809287301226727 * r + 33430.575583588128105) * r
                    + 67265.770927008700853) * r + 45921.953931549871457) * r
                  + 13731.693765509461125) * r + 1971.5909503065514427) * r
                + 133.14166789178437745) * r + 3.387132872796366608)
        den = (((((((5226.495278852854561 * r + 28729.085735721942674) * r
                    + 39307.89580009271061) * r + 21213.794301586595867) * r
                  + 5394.1960214247511077) * r + 687.1870074920579083) * r
                + 42.313330701600911252) * r + 1.0)
        return mu + sigma * q * num / den
    r = p if q < 0 else 1.0 - p
    r = math.sqrt(-math.log(r))
    if r <= 5.0:
        r -= 1.6
        num = (((((((7.7454501427834140764e-4 * r + 0.0227238449892691845833) * r
                    + 0.24178072517745061177) * r + 1.27045825245236838258) * r
                  + 3.64784832476320460504) * r + 5.7694972214606914055) * r
                + 4.6303378461565452959) * r + 1.42343711074968357734)
        den = (((((((1.05075007164441684324e-9 * r + 5.475938084995344946e-4) * r
                    + 0.0151986665636164571966) * r + 0.14810397642748007459) * r
                  + 0.68976733498510000455) * r + 1.6763848301838038494) * r
                + 2.05319162663775882187) * r + 1.0)
    else:
        r -= 5.0
        num = (((((((2.01033439929228813265e-7 * r + 2.71155556874348757815e-5) * r
                    + 0.0012426609473880784386) * r + 0.026532189526576123093) * r
                  + 0.29656057182850489123) * r + 1.7848265399172913358) * r
                + 5.4637849111641143699) * r + 6.6579046435011037772)
        den = (((((((2.04426310338993978564e-15 * r + 1.4215117583164458887e-7) * r
                    + 1.8463183175100546818e-5) * r + 7.868691311456132591e-4) * r
                  + 0.0148753612908506148525) * r + 0.13692988092273580531) * r
                + 0.59983220655588793769) * r + 1.0)
    value = num / den
    return mu + sigma * (-value if q < 0 else value)


def norm_sf(x, mu=0.0, sigma=1.0):
    return clamp(1.0 - norm_cdf(x, mu, sigma), 0.0, 1.0)


# ── dates ────────────────────────────────────────────────────────────────────
def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def parse_iso(value):
    """Parse an ISO-8601 UTC timestamp; returns None on anything unexpected."""
    if not value:
        return None
    text = str(value).strip().replace('Z', '+0000')
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M%z',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M'):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def days_between(a, b):
    if not a or not b:
        return None
    return (a - b).days


def today_utc():
    return datetime.now(timezone.utc).date()


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _nth_weekday(year, month, weekday, n):
    """Date of the n-th given weekday of a month (Monday == 0)."""
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift + 7 * (n - 1))


def eastern_offset(dt_utc):
    """Hours to add to UTC for US Eastern time on this instant.

    Computed from the DST rule rather than read from a timezone database, so
    the build does not depend on tzdata being present in the runner image.
    DST runs from 2am local on the second Sunday in March to 2am local on the
    first Sunday in November.
    """
    y = dt_utc.year
    start = datetime.combine(_nth_weekday(y, 3, 6, 2), datetime.min.time(),
                             tzinfo=timezone.utc) + timedelta(hours=7)   # 2am EST
    end = datetime.combine(_nth_weekday(y, 11, 6, 1), datetime.min.time(),
                           tzinfo=timezone.utc) + timedelta(hours=6)     # 2am EDT
    return -4 if start <= dt_utc < end else -5


def format_eastern(iso_or_time, game_date=None):
    """Render a kickoff time as US Eastern.

    Accepts a full ISO timestamp, or the legacy ``HH:MM AM ET`` string the R
    ingest used to emit — which was actually UTC wearing an ET label, so it is
    reinterpreted here rather than displayed as-is.
    """
    if not iso_or_time:
        return ''
    text = str(iso_or_time).strip()
    dt = None
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%MZ', '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if dt is None:
        cleaned = text.replace(' ET', '').replace('ET', '').strip()
        base = parse_date(game_date) or today_utc()
        for fmt in ('%I:%M %p', '%H:%M'):
            try:
                t = datetime.strptime(cleaned, fmt).time()
                dt = datetime.combine(base, t, tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if dt is None:
        return text
    local = dt + timedelta(hours=eastern_offset(dt))
    return local.strftime('%-I:%M %p ET')


# ── file IO ──────────────────────────────────────────────────────────────────
def read_csv(path):
    if not os.path.exists(path) or os.path.getsize(path) < 5:
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path, rows, fieldnames=None):
    if not rows:
        return
    if fieldnames is None:
        fieldnames = list(rows[0].keys())
        for r in rows:
            for k in r:
                if k not in fieldnames:
                    fieldnames.append(k)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL,
                           extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def write_json(path, obj, indent=1):
    """Write JSON atomically. ``indent=None`` gives the compact form, which
    matters for files this repository rewrites every hour."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    tmp = path + '.tmp'
    separators = (',', ':') if indent is None else None
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=indent, separators=separators,
                  sort_keys=True, default=str)
    os.replace(tmp, path)


# ── HTTP ─────────────────────────────────────────────────────────────────────
# ESPN serves the same documents from two hosts. From GitHub's runners the
# first refuses or fails while the second answers, so every request to the
# first is retried on the second before it is called a failure.
ALT_HOSTS = {'site.api.espn.com': 'site.web.api.espn.com'}

_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/124.0 Safari/537.36')
_SESSION_CACHE: dict[str, object] = {}


class Http:
    """Tiny JSON fetcher: in-process memo, retry with backoff, hard budget.

    Every network failure returns ``None`` rather than raising, so a partial
    outage degrades the site instead of breaking the build.
    """

    def __init__(self, timeout=15, retries=2, pause=0.12, budget_s=None):
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self.budget_s = budget_s
        self.started = time.time()
        self.calls = 0
        self.failures = 0
        self.errors = {}              # url -> why the last attempt failed
        self.last_headers = {}        # response headers of the last success
        self.ctx = ssl.create_default_context()
        self.dump_dir = os.environ.get('SP_DEBUG_DUMP', '').strip() or None

    def out_of_budget(self):
        return self.budget_s is not None and (time.time() - self.started) > self.budget_s

    def get_json(self, url, cache=True):
        data = self._get_json(url, cache)
        if data is not None:
            return data
        for host, alt in ALT_HOSTS.items():
            if f'//{host}/' in url:
                alt_url = url.replace(f'//{host}/', f'//{alt}/', 1)
                data = self._get_json(alt_url, cache)
                if data is not None:
                    self.errors.pop(url, None)
                    if cache:
                        _SESSION_CACHE[url] = data
                    return data
                self.errors[url] = (self.errors.get(url, '') + ' | alt host: '
                                    + self.errors.get(alt_url, 'no answer'))
        return None

    def _get_json(self, url, cache=True):
        if cache and url in _SESSION_CACHE:
            return _SESSION_CACHE[url]
        if self.out_of_budget():
            self.errors[url] = 'request budget exhausted'
            return None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers={
                    'User-Agent': _UA, 'Accept': 'application/json, text/plain, */*',
                    'Accept-Encoding': 'identity', 'Accept-Language': 'en-US,en;q=0.9'})
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=self.ctx) as resp:
                    raw = resp.read()
                    self.last_headers = {k.lower(): v for k, v in resp.headers.items()}
                text = raw.decode('utf-8', 'replace')
                try:
                    data = json.loads(text)
                except ValueError:
                    self.errors[url] = f'not JSON ({len(raw)} bytes): {text[:160]!r}'
                    break
                self.calls += 1
                self.errors.pop(url, None)
                if cache:
                    _SESSION_CACHE[url] = data
                self._dump(url, raw)
                time.sleep(self.pause)
                return data
            except urllib.error.HTTPError as exc:
                body = ''
                try:
                    body = exc.read().decode('utf-8', 'replace')[:160]
                except Exception:          # noqa: BLE001
                    pass
                self.errors[url] = f'HTTP {exc.code}: {body!r}'
                if exc.code in (400, 401, 403, 404, 410):
                    break                  # a retry will not change the answer
            except Exception as exc:       # noqa: BLE001 - never break the build
                self.errors[url] = f'{type(exc).__name__}: {exc}'
            if attempt < self.retries:
                time.sleep(min(2 ** attempt * 0.5, 4.0) + random.random() * 0.2)
        self.failures += 1
        # Failures are deliberately not memoised: a later step may succeed.
        return None

    def why(self, url):
        return self.errors.get(url, '')

    def _dump(self, url, raw):
        """With SP_DEBUG_DUMP=<dir>, keep the first response per endpoint
        family so the live shapes can be read back after a cloud run."""
        if not self.dump_dir:
            return
        try:
            name = re.sub(r'[^a-z0-9]+', '_', url.split('//', 1)[-1].lower())[:120]
            os.makedirs(self.dump_dir, exist_ok=True)
            path = os.path.join(self.dump_dir, name + '.json')
            if not os.path.exists(path):
                with open(path, 'wb') as f:
                    f.write(raw[:400000])
        except OSError:
            pass


# ── team names ───────────────────────────────────────────────────────────────
# Nicknames that are two words; everything else is the final word of the
# display name. Used to keep matchups readable on a phone, where
# "Los Angeles Dodgers @ Cincinnati Reds" has to truncate.
TWO_WORD_NICKNAMES = (
    'red sox', 'white sox', 'blue jays',
    'maple leafs', 'blue jackets', 'red wings', 'golden knights',
    'trail blazers',
)


def short_name(full):
    """'Los Angeles Dodgers' -> 'Dodgers'; 'Boston Red Sox' -> 'Red Sox'."""
    name = (full or '').strip()
    if not name:
        return ''
    parts = name.split()
    if len(parts) < 2:
        return name
    last_two = ' '.join(parts[-2:]).lower()
    if last_two in TWO_WORD_NICKNAMES:
        return ' '.join(parts[-2:])
    return parts[-1]


# ── dict digging ─────────────────────────────────────────────────────────────
def dig(obj, *path, default=None):
    """Walk nested dicts/lists safely: dig(d, 'a', 0, 'b')."""
    cur = obj
    for key in path:
        if cur is None:
            return default
        try:
            if isinstance(key, int):
                cur = cur[key]
            else:
                cur = cur.get(key)
        except (KeyError, IndexError, TypeError, AttributeError):
            return default
    return default if cur is None else cur


def first(seq, pred, default=None):
    for item in seq or []:
        try:
            if pred(item):
                return item
        except Exception:  # noqa: BLE001
            continue
    return default
