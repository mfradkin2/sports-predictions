"""Distributions, scoring rules and the timezone helper."""
import math
import unittest
from datetime import date

from sportspred import util
from sportspred.glm import LogisticModel, PlattCalibrator, reliability_table, score


class TestDistributions(unittest.TestCase):
    def test_poisson_matches_closed_form(self):
        for lam in (0.2, 1.0, 3.5, 9.0):
            self.assertAlmostEqual(util.poisson_sf(0, lam), 1 - math.exp(-lam), places=9)

    def test_poisson_cdf_is_monotone_and_bounded(self):
        prev = -1.0
        for k in range(0, 12):
            v = util.poisson_cdf(k, 3.0)
            self.assertGreaterEqual(v, prev)
            self.assertLessEqual(v, 1.0)
            prev = v

    def test_negbin_falls_back_to_poisson_when_not_overdispersed(self):
        self.assertAlmostEqual(util.negbin_sf(2, 3.0, 3.0), util.poisson_sf(2, 3.0), places=9)

    def test_negbin_is_flatter_than_poisson(self):
        # Extra variance moves mass away from the centre, so the tail above the
        # mean is heavier than Poisson's.
        self.assertGreater(util.negbin_sf(8, 5.0, 9.0), util.poisson_sf(8, 5.0))

    def test_normal_tail(self):
        self.assertAlmostEqual(util.norm_sf(10, 10, 3), 0.5, places=9)
        self.assertAlmostEqual(util.norm_sf(13, 10, 3), 0.158655, places=5)

    def test_logistic_and_logit_round_trip(self):
        for p in (0.01, 0.3, 0.5, 0.87, 0.99):
            self.assertAlmostEqual(util.logistic(util.logit(p)), p, places=9)

    def test_logistic_does_not_overflow(self):
        self.assertAlmostEqual(util.logistic(5000), 1.0, places=9)
        self.assertAlmostEqual(util.logistic(-5000), 0.0, places=9)


class TestParsing(unittest.TestCase):
    def test_num_tolerates_feed_junk(self):
        for raw in ('', 'NA', None, '--', 'TBD', 'n/a'):
            self.assertIsNone(util.num(raw))
        self.assertEqual(util.num('12.5%'), 12.5)
        self.assertEqual(util.num('1,234'), 1234.0)
        self.assertEqual(util.num('7'), 7.0)
        self.assertEqual(util.num(None, 3.0), 3.0)


class TestShortNames(unittest.TestCase):
    """Phone layouts show the nickname; the mapping has to be right."""

    def test_drops_the_city(self):
        self.assertEqual(util.short_name('Los Angeles Dodgers'), 'Dodgers')
        self.assertEqual(util.short_name('Kansas City Chiefs'), 'Chiefs')
        self.assertEqual(util.short_name('Denver Broncos'), 'Broncos')

    def test_keeps_two_word_nicknames(self):
        for full, want in (('Boston Red Sox', 'Red Sox'),
                           ('Chicago White Sox', 'White Sox'),
                           ('Toronto Blue Jays', 'Blue Jays'),
                           ('Toronto Maple Leafs', 'Maple Leafs'),
                           ('Columbus Blue Jackets', 'Blue Jackets'),
                           ('Detroit Red Wings', 'Red Wings'),
                           ('Vegas Golden Knights', 'Golden Knights'),
                           ('Portland Trail Blazers', 'Trail Blazers')):
            self.assertEqual(util.short_name(full), want, full)

    def test_single_word_names_are_unchanged(self):
        self.assertEqual(util.short_name('Athletics'), 'Athletics')

    def test_empty_input(self):
        self.assertEqual(util.short_name(''), '')
        self.assertEqual(util.short_name(None), '')


class TestEastern(unittest.TestCase):
    def test_summer_is_utc_minus_four(self):
        self.assertEqual(util.format_eastern('2026-07-04T23:10Z'), '7:10 PM ET')

    def test_winter_is_utc_minus_five(self):
        self.assertEqual(util.format_eastern('2026-01-04T23:10Z'), '6:10 PM ET')

    def test_legacy_mislabelled_time_is_reinterpreted(self):
        # The R ingest formatted UTC but wrote "ET" after it; a late game shows
        # up as an implausible 1am, and must come back as the previous evening.
        self.assertEqual(util.format_eastern('01:38 AM ET', '2026-09-16'), '9:38 PM ET')

    def test_unparseable_input_is_passed_through(self):
        self.assertEqual(util.format_eastern('Postponed'), 'Postponed')
        self.assertEqual(util.format_eastern(''), '')

    def test_dst_boundaries(self):
        # 2026: DST starts 8 March, ends 1 November.
        self.assertEqual(util.eastern_offset(util.datetime(2026, 3, 9, 12, tzinfo=util.timezone.utc)), -4)
        self.assertEqual(util.eastern_offset(util.datetime(2026, 3, 1, 12, tzinfo=util.timezone.utc)), -5)
        self.assertEqual(util.eastern_offset(util.datetime(2026, 11, 5, 12, tzinfo=util.timezone.utc)), -5)


class TestLogisticModel(unittest.TestCase):
    def test_recovers_known_coefficients(self):
        import random
        rng = random.Random(1)
        X, y = [], []
        for _ in range(4000):
            a, b, c = rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)
            p = util.logistic(0.3 + 1.1 * a - 0.7 * b)
            X.append([a, b, c])
            y.append(1 if rng.random() < p else 0)
        m = LogisticModel(['a', 'b', 'c'], l2=0.5).fit(X, y)
        self.assertTrue(m.converged)
        self.assertAlmostEqual(m.coef[0], 1.1, delta=0.15)
        self.assertAlmostEqual(m.coef[1], -0.7, delta=0.15)
        self.assertAlmostEqual(m.coef[2], 0.0, delta=0.12)
        # The irrelevant feature must rank last.
        self.assertEqual(m.importance()[-1][0], 'c')

    def test_survives_degenerate_input(self):
        m = LogisticModel(['x'], l2=1.0).fit([[1.0]] * 20, [1] * 20)
        self.assertTrue(0.0 <= m.predict_proba([1.0]) <= 1.0)
        self.assertEqual(LogisticModel(['x']).fit([], []).coef, [])

    def test_round_trips_through_dict(self):
        m = LogisticModel(['a', 'b'], l2=2.0).fit([[0, 1], [1, 0], [1, 1], [0, 0]] * 12,
                                                  [1, 0, 1, 0] * 12)
        back = LogisticModel.from_dict(m.to_dict())
        self.assertAlmostEqual(back.predict_proba([1, 0]), m.predict_proba([1, 0]), places=9)


class TestCalibration(unittest.TestCase):
    def test_sharpens_a_squashed_forecast(self):
        import random
        rng = random.Random(2)
        truth = [rng.random() for _ in range(2000)]
        outcomes = [1 if rng.random() < p else 0 for p in truth]
        squashed = [0.5 + (p - 0.5) * 0.5 for p in truth]
        cal = PlattCalibrator().fit(squashed, outcomes)
        self.assertGreater(cal.a, 1.2)
        before = score(squashed, outcomes)['logloss']
        after = score([cal.apply(p) for p in squashed], outcomes)['logloss']
        self.assertLess(after, before)

    def test_refuses_to_fit_on_a_tiny_sample(self):
        cal = PlattCalibrator().fit([0.6, 0.4], [1, 0])
        self.assertEqual((cal.a, cal.b), (1.0, 0.0))

    def test_reliability_buckets_by_favourite_probability(self):
        rows = reliability_table([0.9, 0.1, 0.52], [1, 0, 1])
        self.assertTrue(all(b['n'] > 0 for b in rows))
        self.assertEqual(sum(b['n'] for b in rows), 3)

    def test_score_reports_none_on_empty_input(self):
        self.assertEqual(score([], [])['n'], 0)


if __name__ == '__main__':
    unittest.main()
