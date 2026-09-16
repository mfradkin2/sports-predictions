"""Pure-python regularised logistic regression (IRLS) plus calibration.

No numpy/scipy so the CI job stays a bare `setup-python` step. Feature counts
here are small (<15), so the O(p^3) solve is irrelevant.
"""
from __future__ import annotations

import math

from .util import clamp, log_loss, logistic


# ── linear algebra ───────────────────────────────────────────────────────────
def solve(matrix, rhs):
    """Solve A x = b by Gaussian elimination with partial pivoting."""
    n = len(rhs)
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            aug[col][col] += 1e-8          # nudge a singular system
            pivot = col
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        for r in range(col + 1, n):
            factor = aug[r][col] / pv
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    x = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = aug[row][n] - sum(aug[row][c] * x[c] for c in range(row + 1, n))
        x[row] = total / aug[row][row] if abs(aug[row][row]) > 1e-12 else 0.0
    return x


# ── model ────────────────────────────────────────────────────────────────────
class LogisticModel:
    """Binary logistic regression fitted by iteratively reweighted least squares.

    Features are standardised internally, so coefficients are comparable and
    the L2 penalty means the same thing for every column.
    """

    def __init__(self, feature_names=None, l2=1.0):
        self.feature_names = list(feature_names or [])
        self.l2 = float(l2)
        self.coef = []
        self.intercept = 0.0
        self.mu = []
        self.sd = []
        self.n_train = 0
        self.converged = False

    # standardisation -------------------------------------------------------
    def _standardise_fit(self, X):
        p = len(X[0])
        n = len(X)
        self.mu = [sum(row[j] for row in X) / n for j in range(p)]
        self.sd = []
        for j in range(p):
            var = sum((row[j] - self.mu[j]) ** 2 for row in X) / max(n - 1, 1)
            self.sd.append(math.sqrt(var) if var > 1e-12 else 1.0)

    def _z(self, row):
        return [(row[j] - self.mu[j]) / self.sd[j] for j in range(len(row))]

    # fit -------------------------------------------------------------------
    def fit(self, X, y, weights=None, max_iter=40, tol=1e-7):
        if not X or not y or len(X) != len(y):
            return self
        n, p = len(X), len(X[0])
        self.n_train = n
        self._standardise_fit(X)
        Z = [[1.0] + self._z(row) for row in X]
        w_obs = weights or [1.0] * n
        beta = [0.0] * (p + 1)

        for _ in range(max_iter):
            eta = [sum(b * z for b, z in zip(beta, row)) for row in Z]
            mu = [logistic(e) for e in eta]
            w = [max(w_obs[i] * mu[i] * (1 - mu[i]), 1e-8) for i in range(n)]
            z_work = [eta[i] + (y[i] - mu[i]) / max(mu[i] * (1 - mu[i]), 1e-8)
                      for i in range(n)]

            # (Z' W Z + lambda I) beta = Z' W z   (intercept is not penalised)
            ztwz = [[0.0] * (p + 1) for _ in range(p + 1)]
            ztwz_b = [0.0] * (p + 1)
            for i in range(n):
                row, wi, zi = Z[i], w[i], z_work[i]
                for a in range(p + 1):
                    ra = row[a] * wi
                    ztwz_b[a] += ra * zi
                    for b in range(a, p + 1):
                        ztwz[a][b] += ra * row[b]
            for a in range(p + 1):
                for b in range(a):
                    ztwz[a][b] = ztwz[b][a]
            for a in range(1, p + 1):
                ztwz[a][a] += self.l2

            new_beta = solve(ztwz, ztwz_b)
            shift = max(abs(new_beta[i] - beta[i]) for i in range(p + 1))
            beta = new_beta
            if shift < tol:
                self.converged = True
                break

        self.intercept = beta[0]
        self.coef = beta[1:]
        return self

    # predict ---------------------------------------------------------------
    def decision(self, row):
        z = self._z(row)
        return self.intercept + sum(c * v for c, v in zip(self.coef, z))

    def predict_proba(self, row):
        return logistic(self.decision(row))

    def predict_all(self, X):
        return [self.predict_proba(r) for r in X]

    # importance ------------------------------------------------------------
    def importance(self):
        """Standardised |coefficient| per feature, normalised to sum to 1."""
        if not self.coef:
            return []
        total = sum(abs(c) for c in self.coef) or 1.0
        names = self.feature_names or [f'x{i}' for i in range(len(self.coef))]
        pairs = [(names[i], self.coef[i], abs(self.coef[i]) / total)
                 for i in range(len(self.coef))]
        pairs.sort(key=lambda t: -t[2])
        return pairs

    def to_dict(self):
        return {'feature_names': self.feature_names, 'l2': self.l2,
                'coef': self.coef, 'intercept': self.intercept,
                'mu': self.mu, 'sd': self.sd, 'n_train': self.n_train}

    @classmethod
    def from_dict(cls, d):
        m = cls(d.get('feature_names'), d.get('l2', 1.0))
        m.coef = list(d.get('coef') or [])
        m.intercept = float(d.get('intercept', 0.0))
        m.mu = list(d.get('mu') or [])
        m.sd = list(d.get('sd') or [])
        m.n_train = int(d.get('n_train', 0))
        return m if m.coef and len(m.mu) == len(m.coef) else None


# ── calibration ──────────────────────────────────────────────────────────────
class PlattCalibrator:
    """One-dimensional logistic recalibration on the log-odds scale.

    ``a`` sharpens or flattens the model, ``b`` corrects a systematic bias.
    Fitted on out-of-sample predictions only.
    """

    def __init__(self, a=1.0, b=0.0):
        self.a = float(a)
        self.b = float(b)
        self.n = 0

    def fit(self, probs, outcomes, max_iter=60):
        pairs = [(p, o) for p, o in zip(probs, outcomes) if p is not None and o is not None]
        if len(pairs) < 40:
            return self
        xs = [math.log(clamp(p, 1e-6, 1 - 1e-6) / (1 - clamp(p, 1e-6, 1 - 1e-6)))
              for p, _ in pairs]
        ys = [float(o) for _, o in pairs]
        a, b = 1.0, 0.0
        for _ in range(max_iter):
            g_a = g_b = h_aa = h_ab = h_bb = 0.0
            for x, y in zip(xs, ys):
                mu = logistic(a * x + b)
                w = max(mu * (1 - mu), 1e-9)
                r = mu - y
                g_a += r * x
                g_b += r
                h_aa += w * x * x
                h_ab += w * x
                h_bb += w
            h_aa += 1e-6
            h_bb += 1e-6
            det = h_aa * h_bb - h_ab * h_ab
            if abs(det) < 1e-12:
                break
            da = (h_bb * g_a - h_ab * g_b) / det
            db = (h_aa * g_b - h_ab * g_a) / det
            a -= da
            b -= db
            if max(abs(da), abs(db)) < 1e-9:
                break
        # Guard against a degenerate fit on a small, lopsided sample.
        if 0.25 <= a <= 3.0 and abs(b) <= 1.5:
            self.a, self.b, self.n = a, b, len(pairs)
        return self

    def apply(self, prob):
        if prob is None:
            return None
        x = math.log(clamp(prob, 1e-6, 1 - 1e-6) / (1 - clamp(prob, 1e-6, 1 - 1e-6)))
        return logistic(self.a * x + self.b)

    def to_dict(self):
        return {'a': self.a, 'b': self.b, 'n': self.n}

    @classmethod
    def from_dict(cls, d):
        if not d:
            return cls()
        c = cls(d.get('a', 1.0), d.get('b', 0.0))
        c.n = int(d.get('n', 0))
        return c


def reliability_table(probs, outcomes, bins=(0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 1.01)):
    """Bucket predictions by confidence and report predicted vs actual.

    Reported on the favourite's side, which is how a reader thinks about it.
    """
    buckets = []
    lo = 0.5
    for hi in bins[1:] if bins[0] == 0.5 else bins:
        buckets.append({'lo': lo, 'hi': hi, 'n': 0, 'pred': 0.0, 'hit': 0})
        lo = hi
    for p, o in zip(probs, outcomes):
        if p is None or o is None:
            continue
        fav_p = max(p, 1 - p)
        hit = int((p >= 0.5 and o == 1) or (p < 0.5 and o == 0))
        for b in buckets:
            if b['lo'] <= fav_p < b['hi']:
                b['n'] += 1
                b['pred'] += fav_p
                b['hit'] += hit
                break
    for b in buckets:
        b['pred'] = round(b['pred'] / b['n'], 4) if b['n'] else None
        b['actual'] = round(b['hit'] / b['n'], 4) if b['n'] else None
    return [b for b in buckets if b['n'] > 0]


def score(probs, outcomes):
    """Accuracy / Brier / log loss for a set of predictions."""
    pairs = [(p, o) for p, o in zip(probs, outcomes) if p is not None and o is not None]
    if not pairs:
        return {'n': 0, 'acc': None, 'brier': None, 'logloss': None}
    n = len(pairs)
    acc = sum(1 for p, o in pairs if (p >= 0.5) == (o == 1)) / n
    br = sum((p - o) ** 2 for p, o in pairs) / n
    ll = sum(log_loss(p, o) for p, o in pairs) / n
    return {'n': n, 'acc': round(acc, 4), 'brier': round(br, 4), 'logloss': round(ll, 4)}
