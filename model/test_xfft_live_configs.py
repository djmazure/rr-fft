"""Reference-deployment configurations: fixture integrity and xfft divergence.

sim/cocotb/tests/fft_live_goldens.py holds, for config 0x156 (scaled,
inverse, schedule [3, 2, 2, 2]) and config 0xB1 (unscaled, forward), the
oracle output and the Xilinx xfft v9.1 C-model output of one stimulus
corpus. These tests check that the committed file still matches both
engines, and record how the twiddle format sets the distance to xfft.

  The oracle uses Q1.23 twiddles (the xfft format) with an exact inverse
  conjugation. It agrees with xfft within 1 LSB (0x156) and 3 LSB of the
  33-bit output (0xB1) on this corpus. The same structure with Q2.22
  twiddles differs from xfft by up to 121 LSB in the unscaled build.

The C-model tests skip on benches without a Vivado install.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

import rr_fft_oracle as ro
from gen_live_goldens import (FRAMES, SCALED_CONFIG_WORD, SCALED_SCH,
                              UNSCALED_CONFIG_WORD, config_fields)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim/cocotb/tests"))
import fft_live_goldens as G  # noqa: E402

try:
    from xfft_cmodel import _find_vivado_root, run_xfft
    _find_vivado_root()
    _HAVE_CMODEL = True
except (FileNotFoundError, RuntimeError):
    _HAVE_CMODEL = False

needs_cmodel = pytest.mark.skipif(
    not _HAVE_CMODEL, reason="FFT-T3.1: Xilinx xfft C model not found (no Vivado install)")


def test_config_words_decode():
    assert SCALED_CONFIG_WORD == 0x156 and UNSCALED_CONFIG_WORD == 0xB1
    assert config_fields(0x156) == (False, [3, 2, 2, 2])
    assert config_fields(0x157) == (True, [3, 2, 2, 2])
    assert config_fields(0x155) == (True, [2, 2, 2, 2])
    assert UNSCALED_CONFIG_WORD & 1 == 1


def test_fixture_matches_corpus():
    assert G.FRAME_NAMES == [n for n, _ in FRAMES]
    assert G.IN_FRAMES == [f for _, f in FRAMES]
    assert G.SCALED_SCHEDULE == SCALED_SCH


def test_fixture_oracle_columns_current():
    o = ro.RrFftOracle(8)
    for i, (_, f) in enumerate(FRAMES):
        s = o.run(f, inverse=True, scale_schedule=[3, 2, 2, 2], scaled=True)
        assert s.data == [tuple(v) for v in G.SCALED_ORACLE[i]]
        assert not s.overflow
        u = o.run(f, inverse=False)
        assert u.data == [tuple(v) for v in G.UNSCALED_ORACLE[i]]


@needs_cmodel
def test_fixture_xfft_columns_current():
    for i, (_, f) in enumerate(FRAMES):
        re, im = [s[0] for s in f], [s[1] for s in f]
        s = run_xfft(re, im, scaled=True, scale_schedule=[3, 2, 2, 2], inverse=True)
        assert list(zip(s.re, s.im)) == [tuple(v) for v in G.SCALED_XFFT[i]]
        u = run_xfft(re, im, scaled=False, inverse=False)
        assert list(zip(u.re, u.im)) == [tuple(v) for v in G.UNSCALED_XFFT[i]]


class _Q222Oracle(ro.RrFftOracle):
    """The oracle with Q2.22 twiddles (analysis only, not the spec)."""

    _FB = 22

    def _twiddle(self, k, ell):
        ang = -2.0 * math.pi * (k % ell) / ell
        q = lambda x: math.floor(x * (1 << self._FB) + 0.5)  # noqa: E731
        return (q(math.cos(ang)), q(math.sin(ang)))

    def _cmult(self, x, k, ell, inverse):
        if k % ell == 0:
            return x
        wr, wi = self._twiddle(k, ell)
        if inverse:
            wi = -wi
        xr, xi = x
        return (ro._trunc_shift(xr * wr - xi * wi, self._FB),
                ro._trunc_shift(xr * wi + xi * wr, self._FB))


def _max_err(rows_a, rows_b):
    return max(max(abs(a[0] - b[0]), abs(a[1] - b[1]))
               for fa, fb in zip(rows_a, rows_b) for a, b in zip(fa, fb))


def test_unscaled_forward_within_three_lsb():
    assert _max_err(G.UNSCALED_ORACLE, G.UNSCALED_XFFT) == 3


def test_q2_22_twiddles_diverge_from_xfft():
    o = _Q222Oracle(8)
    rows = [o.run(f, inverse=False).data for _, f in FRAMES]
    assert _max_err(rows, G.UNSCALED_XFFT) == 121


def test_scaled_inverse_within_one_lsb():
    assert _max_err(G.SCALED_ORACLE, G.SCALED_XFFT) == 1
