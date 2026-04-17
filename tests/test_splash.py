"""Tests for the QHO splash widget — pure-function tests and tiny-terminal guards."""
from __future__ import annotations

import math
import unittest

from actor.watch.splash import (
    LOGO,
    LOGO_H,
    N_TEX,
    TAGLINE,
    HINT,
    _AMP_SQ_THRESHOLDS,
    _factorial,
    _hermite,
    _psi1d,
    _psi2d,
    Splash,
)


class QHOMathTests(unittest.TestCase):
    def test_factorial(self):
        self.assertEqual(_factorial(0), 1)
        self.assertEqual(_factorial(1), 1)
        self.assertEqual(_factorial(5), 120)

    def test_hermite_physicists_polynomials(self):
        # H_0(x) = 1, H_1(x) = 2x, H_2(x) = 4x^2 - 2, H_3(x) = 8x^3 - 12x
        for x in [-1.0, 0.0, 0.5, 2.0]:
            self.assertAlmostEqual(_hermite(0, x), 1.0)
            self.assertAlmostEqual(_hermite(1, x), 2.0 * x)
            self.assertAlmostEqual(_hermite(2, x), 4.0 * x * x - 2.0)
            self.assertAlmostEqual(_hermite(3, x), 8.0 * x**3 - 12.0 * x)

    def test_psi1d_symmetry(self):
        # Ground state is even, first excited is odd.
        for x in [0.1, 0.7, 1.5]:
            self.assertAlmostEqual(_psi1d(0, x), _psi1d(0, -x))
            self.assertAlmostEqual(_psi1d(1, x), -_psi1d(1, -x))

    def test_psi2d_factorizes(self):
        self.assertAlmostEqual(_psi2d(2, 3, 0.4, -0.7), _psi1d(2, 0.4) * _psi1d(3, -0.7))


class ThresholdTableTests(unittest.TestCase):
    def test_monotonic_increasing(self):
        self.assertEqual(len(_AMP_SQ_THRESHOLDS), N_TEX)
        for a, b in zip(_AMP_SQ_THRESHOLDS, _AMP_SQ_THRESHOLDS[1:]):
            self.assertLess(a, b)

    def test_zero_at_lowest_bucket(self):
        self.assertEqual(_AMP_SQ_THRESHOLDS[0], 0.0)

    def test_highest_threshold_is_amp_sq_1_over_18(self):
        # bucket = N_TEX-1 corresponds to intensity = 1, i.e. amp²·18 = 1
        self.assertAlmostEqual(_AMP_SQ_THRESHOLDS[-1], 1.0 / 18.0)


class GeometryTests(unittest.TestCase):
    """_ensure_geometry must produce consistent chars/keys lengths or skip the box."""

    @staticmethod
    def _splash_for_geometry() -> Splash:
        # Bypass Widget.__init__ (which needs an app context) — we only exercise
        # _ensure_geometry, which only touches self._geometry / _geometry_size.
        s = Splash.__new__(Splash)
        s._geometry = None
        s._geometry_size = None
        return s

    def test_normal_size_produces_consistent_lengths(self):
        s = self._splash_for_geometry()
        s._ensure_geometry(40, 120)
        geom = s._geometry
        self.assertGreater(geom["box_w"], 0)
        self.assertGreater(geom["box_h"], 0)
        self.assertEqual(len(geom["rows_content"]), geom["box_h"])
        for chars, keys in geom["rows_content"]:
            self.assertEqual(len(chars), geom["box_w"])
            self.assertEqual(len(keys), geom["box_w"])

    def test_tiny_terminal_skips_box(self):
        # cols=1 previously produced box_w=1 with a length-2 chars list and
        # length-1 keys list, which crashed in _ensure_box_segments.
        for rows, cols in [(10, 1), (10, 2), (10, 3), (1, 120), (0, 120), (10, 0)]:
            with self.subTest(rows=rows, cols=cols):
                s = self._splash_for_geometry()
                s._ensure_geometry(rows, cols)
                geom = s._geometry
                self.assertEqual(geom["box_w"], 0)
                self.assertEqual(geom["box_h"], 0)
                self.assertEqual(geom["rows_content"], [])
                self.assertEqual(geom["box_segments"], [])

    def test_cache_respects_size(self):
        s = self._splash_for_geometry()
        s._ensure_geometry(40, 120)
        first = s._geometry
        s._ensure_geometry(40, 120)
        self.assertIs(s._geometry, first)
        s._ensure_geometry(30, 80)
        self.assertIsNot(s._geometry, first)


if __name__ == "__main__":
    unittest.main()
