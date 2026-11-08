"""Oracle proof suite (FFT-P2.2) — numpy is the tolerance cross-check here;
the oracle itself is the bit-exact reference the RTL will be held to."""

import random

import numpy as np
import pytest

from rr_fft_oracle import RrFftOracle


def _rand_frame(o: RrFftOracle, headroom_bits: int = 0, seed: int = 1):
    rng = random.Random(seed)
    lim = 1 << (o.data_width - 1 - headroom_bits)
    return [(rng.randrange(-lim, lim), rng.randrange(-lim, lim))
            for _ in range(o.n)]


def _to_np(frame):
    return np.array([re + 1j * im for re, im in frame])


def _rel_err(got, ref):
    denom = max(np.max(np.abs(ref)), 1.0)
    return np.max(np.abs(got - ref)) / denom


# ── exactness anchors ────────────────────────────────────────────────

@pytest.mark.parametrize("lg", [3, 4, 7, 8])
def test_impulse_exact(lg):
    o = RrFftOracle(lg)
    imp = [(12345, -777)] + [(0, 0)] * (o.n - 1)
    r = o.run(imp)
    assert r.data == [(12345, -777)] * o.n  # flat spectrum, bit-exact


@pytest.mark.parametrize("lg", [4, 8])
def test_dc_bin_exact(lg):
    o = RrFftOracle(lg)
    r = o.run([(1000, 250)] * o.n)
    assert r.data[0] == (1000 * o.n, 250 * o.n)  # DC path is all-bypass


def test_determinism():
    o = RrFftOracle(8)
    f = _rand_frame(o, seed=7)
    assert o.run(f).data == o.run(f).data


# ── numpy tolerance cross-checks ─────────────────────────────────────

@pytest.mark.parametrize("lg", [4, 7, 8, 10])
def test_forward_unscaled_vs_numpy(lg):
    o = RrFftOracle(lg)
    f = _rand_frame(o, seed=lg)
    got = _to_np(o.run(f).data)
    ref = np.fft.fft(_to_np(f))
    assert _rel_err(got, ref) < 2e-4


@pytest.mark.parametrize("lg", [4, 8])
def test_inverse_unscaled_vs_numpy(lg):
    # oracle inverse carries no 1/N: compare against N * ifft
    o = RrFftOracle(lg)
    f = _rand_frame(o, seed=lg + 100)
    got = _to_np(o.run(f, inverse=True).data)
    ref = np.fft.ifft(_to_np(f)) * o.n
    assert _rel_err(got, ref) < 2e-4


def test_scaled_schedule_vs_numpy():
    # sum(sch) == log2_n realizes 1/N; conservative [2,2,2,2] for N=256
    o = RrFftOracle(8)
    f = _rand_frame(o, headroom_bits=1, seed=5)
    r = o.run(f, scale_schedule=[2, 2, 2, 2], scaled=True)
    assert not r.overflow
    ref = np.fft.fft(_to_np(f)) / o.n
    assert _rel_err(_to_np(r.data), ref) < 2e-3  # coarser: output LSB is fatter


def test_roundtrip_identity():
    # IFFT(FFT(x)) == N*x within accumulated truncation noise
    o = RrFftOracle(8)
    f = _rand_frame(o, headroom_bits=2, seed=9)
    fwd = o.run(f).data
    # feed back through a wider oracle input check: bit growth is legal input
    o2 = RrFftOracle(8, data_width=o.data_width + o.log2_n + 1)
    back = _to_np(o2.run(fwd, inverse=True).data) / o.n
    assert _rel_err(back, _to_np(f)) < 2e-4


# ── spec properties ──────────────────────────────────────────────────

def test_unscaled_growth_bound_adversarial():
    # max-amplitude coherent frame — worst-case growth still inside
    # DATA_WIDTH + LOG2_N + 1 (asserted inside run())
    o = RrFftOracle(8, data_width=16)
    lim = (1 << 15) - 1
    o.run([(lim, lim)] * o.n)
    o.run([(-(lim + 1), -(lim + 1))] * o.n)


def test_scaled_overflow_flagged():
    o = RrFftOracle(8, data_width=16)
    lim = (1 << 15) - 1
    r = o.run([(lim, lim)] * o.n, scale_schedule=[0, 0, 0, 0], scaled=True)
    assert r.overflow  # DC bin alone is N*lim >> 16 bits


def test_bit_reversed_helper():
    o = RrFftOracle(3)
    nat = [(i, 0) for i in range(8)]
    assert [s[0] for s in o.bit_reversed(nat)] == [0, 4, 2, 6, 1, 5, 3, 7]


def test_input_validation():
    o = RrFftOracle(4, data_width=8)
    with pytest.raises(ValueError):
        o.run([(200, 0)] * 16)                      # exceeds width
    with pytest.raises(ValueError):
        o.run([(0, 0)] * 16, scale_schedule=[1, 1])  # sch in unscaled
    with pytest.raises(ValueError):
        o.run([(0, 0)] * 8)                          # wrong frame length
