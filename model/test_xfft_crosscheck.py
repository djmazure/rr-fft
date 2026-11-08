"""SNR-equivalence tier (FFT-P2.2 close-out): oracle vs Xilinx xfft C model.

Empirical baseline on Vivado 2024.1, 256-pt / 24-bit data / 24-bit twiddles
(the v1 reference config): forward scaled agrees within 1 LSB (>108 dB
SNR); unscaled paths within 17 LSB of a 33-bit output (>132 dB SNR); impulse
is bit-exact in both modes; and the scale-schedule ORDERING (sch[0] = the
input-side stage pair) is proven by overflow agreement, not just noise.
Thresholds below carry margin over those measurements.

Skips cleanly on benches without a Vivado install (the C model is located,
never vendored).
"""

import math
import random

import pytest

from rr_fft_oracle import RrFftOracle

try:
    from xfft_cmodel import run_xfft, _find_vivado_root
    _find_vivado_root()
    _HAVE_CMODEL = True
except (FileNotFoundError, RuntimeError):
    _HAVE_CMODEL = False

pytestmark = pytest.mark.skipif(
    not _HAVE_CMODEL, reason="Xilinx xfft C model not found (no Vivado install)")

N_LG = 8  # 256-pt — the v1 reference config


def _frame(seed: int, amp_bits: int):
    rng = random.Random(seed)
    lim = 1 << amp_bits
    return [(rng.randrange(-lim, lim), rng.randrange(-lim, lim))
            for _ in range(1 << N_LG)]


def _compare(frame, *, scaled, inverse, sch=None):
    o = RrFftOracle(N_LG, data_width=24, twiddle_width=24)
    r_o = o.run(frame, inverse=inverse,
                scale_schedule=sch if scaled else None, scaled=scaled)
    r_x = run_xfft([s[0] for s in frame], [s[1] for s in frame],
                   scaled=scaled, scale_schedule=sch, inverse=inverse)
    diffs = [a - b[0] for a, b in zip(r_x.re, r_o.data)] + \
            [a - b[1] for a, b in zip(r_x.im, r_o.data)]
    ref = r_x.re + r_x.im
    ps, pe = sum(v * v for v in ref), sum(d * d for d in diffs)
    snr = float("inf") if pe == 0 else 10 * math.log10(ps / pe)
    return max(map(abs, diffs)), snr, r_x, r_o


@pytest.mark.parametrize("seed", range(5))
def test_forward_scaled_reference_config(seed):
    lsb, snr, r_x, r_o = _compare(_frame(seed, 21), scaled=True,
                                  inverse=False, sch=[2, 2, 2, 2])
    assert not r_x.overflow and not r_o.overflow
    assert lsb <= 4, f"max diff {lsb} LSB"
    assert snr >= 100.0, f"SNR {snr:.1f} dB"


@pytest.mark.parametrize("seed", range(5))
def test_inverse_unscaled_reference_config(seed):
    lsb, snr, _, _ = _compare(_frame(seed + 50, 21), scaled=False, inverse=True)
    assert lsb <= 64, f"max diff {lsb} LSB (33-bit output scale)"
    assert snr >= 120.0, f"SNR {snr:.1f} dB"


def test_forward_unscaled(seed=99):
    lsb, snr, _, _ = _compare(_frame(seed, 21), scaled=False, inverse=False)
    assert lsb <= 64 and snr >= 120.0


def test_impulse_bit_exact_both_modes():
    imp = [(4096000, -102400)] + [(0, 0)] * ((1 << N_LG) - 1)
    for scaled, sch in ((True, [2, 2, 2, 2]), (False, None)):
        lsb, _, _, _ = _compare(imp, scaled=scaled, inverse=False, sch=sch)
        assert lsb == 0, f"impulse not bit-exact (scaled={scaled})"


@pytest.mark.parametrize("sch", [[3, 3, 1, 1], [3, 3, 2, 0], [0, 2, 3, 3]])
def test_nonuniform_schedules_agree(sch):
    # 3-bit headroom keeps every permutation overflow-free
    lsb, snr, r_x, r_o = _compare(_frame(7, 20), scaled=True,
                                  inverse=False, sch=sch)
    assert not r_x.overflow and not r_o.overflow
    assert lsb <= 4 and snr >= 100.0


def test_schedule_ordering_proven_by_overflow():
    # Full-scale input: shifts must land on the INPUT-side pairs or early
    # stage growth wraps. Both engines must agree on which schedules are
    # safe — this pins sch[0] = first pipeline pair as the shared meaning.
    f = _frame(7, 23)
    _, _, r_x, r_o = _compare(f, scaled=True, inverse=False, sch=[3, 3, 0, 0])
    assert not r_x.overflow and not r_o.overflow
    _, _, r_x, r_o = _compare(f, scaled=True, inverse=False, sch=[0, 0, 3, 3])
    assert r_x.overflow and r_o.overflow
