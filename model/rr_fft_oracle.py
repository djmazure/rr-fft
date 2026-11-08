"""rr_fft fixed-point oracle — the bit-exactness reference for every FFT-REQ.

This model IS the arithmetic spec of the rr_fft RTL (FFT-P2.2). The RTL is
required to match it bit-exactly (FFT-REQ-001/002); numpy is only ever a
tolerance cross-check in the tests, never the reference.

Arithmetic spec (mirrors the radix-2^2 SDF pipeline, one truncation per
non-trivial twiddle multiply per stage pair):

  Data        signed integers (re, im), DATA_WIDTH bits at the input.
  Twiddles    W_L^k = exp(-j*2*pi*k/L), quantized to TWIDDLE_WIDTH-bit
              signed Q1.(TW-1), the xfft v9.1 format: w_int =
              floor(w * 2^(TW-1) + 0.5) per component (round-half-up).
              Q1.(TW-1) cannot hold +1.0. Unity (k == 0) is BYPASSED (no
              multiply, no truncation), and no other forward twiddle rounds
              to +1.0 (asserted). -1.0 is representable.
  Cmult       re = xr*wr - xi*wi ; im = xr*wi + xi*wr, then arithmetic
              shift right by (TW-1) — truncation toward -inf, matching
              two's-complement LSB-drop in hardware.
  Inverse     the twiddle is conjugated EXACTLY: wi -> -wi at full
              precision (one bit wider than TW), so the conjugate of -j
              is +1.0 j, not 1 - 2^-(TW-1). This matches the xfft C model
              best (model/test_xfft_live_configs.py).
  Pair step   (radix-2^2 DIF, block size L, n in [0, L/4)):
                A = x[n]        + x[n + L/2]
                C = x[n]        - x[n + L/2]
                B = x[n + L/4]  + x[n + 3L/4]
                D = x[n + L/4]  - x[n + 3L/4]
                branch0 = A + B                          (no twiddle)
                branch1 = (C -/+ jD) * W_L^n             (fwd: -j, inv: +j)
                branch2 = (A - B)    * W_L^(2n)
                branch3 = (C +/- jD) * W_L^(3n)
              X(4k+m) = DFT_{L/4}(branch m)[k]. Inverse conjugates the
              twiddles (negated exponent) and swaps the -j/+j roles.
  Scaling     after each pair, arithmetic shift right by sch[p]
              (0..3, truncation). In SCALED mode the result then wraps
              (two's complement) to DATA_WIDTH; overflow is flagged.
              In UNSCALED mode sch must be all-zero, nothing wraps, and
              the output fits DATA_WIDTH + LOG2_N + 1 signed bits.
  Odd LOG2_N  the final block of size 2 is a single radix-2 stage
              (a+b, a-b) — its schedule entry scales it, no twiddle.
  Ordering    the oracle returns NATURAL order; bit_reversed() gives the
              pre-reorder stream order for probing the RTL ahead of the
              reorder buffer.

Scale-schedule indexing: sch[0] is the FIRST pipeline pair (block size N,
the input side). len(sch) == ceil(LOG2_N / 2). An IFFT with sum(sch) ==
LOG2_N realizes the 1/N normalization.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


Sample = tuple[int, int]  # (re, im) as plain ints — exact arithmetic


def _trunc_shift(v: int, sh: int) -> int:
    """Arithmetic shift right with truncation toward -inf (hardware LSB drop)."""
    return v >> sh


def _wrap(v: int, width: int) -> int:
    """Two's-complement wrap of v to `width` bits."""
    mask = (1 << width) - 1
    v &= mask
    if v >= (1 << (width - 1)):
        v -= 1 << width
    return v


@dataclass
class OracleResult:
    data: list[Sample]        # natural order
    overflow: bool            # any wrap occurred (scaled mode only)


class RrFftOracle:
    """Fixed-point radix-2^2 SDF FFT/IFFT oracle. All state is per-run."""

    def __init__(self, log2_n: int, data_width: int = 24,
                 twiddle_width: int = 24) -> None:
        if log2_n < 1:
            raise ValueError("log2_n must be >= 1")
        self.log2_n = log2_n
        self.n = 1 << log2_n
        self.data_width = data_width
        self.twiddle_width = twiddle_width
        self._tw_shift = twiddle_width - 1
        self._tw_scale = 1 << self._tw_shift
        self.n_pairs = (log2_n + 1) // 2  # schedule length (odd: last is radix-2)
        self._rom: dict[tuple[int, int], Sample] = {}

    # ── twiddles ──────────────────────────────────────────────────────

    def _twiddle(self, k: int, ell: int) -> Sample:
        """Quantized W_ell^k (forward kernel), Q1.(TW-1), round-half-up."""
        key = (k % ell, ell)
        if key not in self._rom:
            ang = -2.0 * math.pi * key[0] / ell
            w = (
                math.floor(math.cos(ang) * self._tw_scale + 0.5),
                math.floor(math.sin(ang) * self._tw_scale + 0.5),
            )
            if key[0] != 0:
                assert max(w) < self._tw_scale, "twiddle rounds to +1.0"
            self._rom[key] = w
        return self._rom[key]

    def _cmult(self, x: Sample, k: int, ell: int, inverse: bool) -> Sample:
        """x * W_ell^(+/-k) with the spec'd single truncation. k==0 bypasses."""
        if k % ell == 0:
            return x
        wr, wi = self._twiddle(k, ell)
        if inverse:
            wi = -wi
        xr, xi = x
        return (
            _trunc_shift(xr * wr - xi * wi, self._tw_shift),
            _trunc_shift(xr * wi + xi * wr, self._tw_shift),
        )

    # ── core ──────────────────────────────────────────────────────────

    def run(self, frame: list[Sample], inverse: bool = False,
            scale_schedule: list[int] | None = None,
            scaled: bool = False) -> OracleResult:
        """Transform one frame. Returns NATURAL-order output + overflow flag."""
        if len(frame) != self.n:
            raise ValueError(f"frame must have {self.n} samples")
        sch = scale_schedule if scale_schedule is not None else [0] * self.n_pairs
        if len(sch) != self.n_pairs:
            raise ValueError(f"scale_schedule must have {self.n_pairs} entries")
        if not scaled and any(sch):
            raise ValueError("unscaled mode requires an all-zero schedule")
        if any(not 0 <= s <= 3 for s in sch):
            raise ValueError("schedule entries must be 0..3")
        lim = 1 << (self.data_width - 1)
        for re, im in frame:
            if not (-lim <= re < lim and -lim <= im < lim):
                raise ValueError("input sample exceeds DATA_WIDTH")

        self._ovflo = False
        self.pair_max = [0] * (self.n_pairs + 1)  # post-pair peak |re/im|
        out = self._blk(list(frame), 0, inverse, sch, scaled)
        if not scaled:
            bound = 1 << (self.data_width + self.log2_n)
            for re, im in out:
                assert -bound <= re < bound and -bound <= im < bound, \
                    "unscaled growth bound violated — spec bug"
        return OracleResult(out, self._ovflo)

    def _post(self, x: Sample, sh: int, scaled: bool,
              pair: int = 0) -> Sample:
        re, im = _trunc_shift(x[0], sh), _trunc_shift(x[1], sh)
        if scaled:
            wre, wim = _wrap(re, self.data_width), _wrap(im, self.data_width)
            if (wre, wim) != (re, im):
                self._ovflo = True
            return (wre, wim)
        # width-plan evidence for the RTL (FFT-REQ-002): peak magnitude
        # observed at each pair boundary, unscaled
        m = max(abs(re), abs(im))
        if m > self.pair_max[pair]:
            self.pair_max[pair] = m
        return (re, im)

    def _blk(self, x: list[Sample], pair: int, inv: bool,
             sch: list[int], scaled: bool) -> list[Sample]:
        ell = len(x)
        if ell == 1:
            return x
        if ell == 2:  # final radix-2 stage (odd log2_n)
            a, b = x
            y = [(a[0] + b[0], a[1] + b[1]), (a[0] - b[0], a[1] - b[1])]
            return [self._post(s, sch[pair], scaled, pair) for s in y]

        q = ell // 4
        br: list[list[Sample]] = [[], [], [], []]
        for n in range(q):
            ar = (x[n][0] + x[n + 2 * q][0], x[n][1] + x[n + 2 * q][1])
            cr = (x[n][0] - x[n + 2 * q][0], x[n][1] - x[n + 2 * q][1])
            bb = (x[n + q][0] + x[n + 3 * q][0], x[n + q][1] + x[n + 3 * q][1])
            dd = (x[n + q][0] - x[n + 3 * q][0], x[n + q][1] - x[n + 3 * q][1])
            # -jD (fwd) / +jD (inv): exact rotation, no rounding
            jd = (dd[1], -dd[0]) if not inv else (-dd[1], dd[0])
            b0 = (ar[0] + bb[0], ar[1] + bb[1])
            b1 = (cr[0] + jd[0], cr[1] + jd[1])
            b2 = (ar[0] - bb[0], ar[1] - bb[1])
            b3 = (cr[0] - jd[0], cr[1] - jd[1])
            sh = sch[pair]
            br[0].append(self._post(b0, sh, scaled, pair))
            br[1].append(self._post(self._cmult(b1, n, ell, inv), sh, scaled, pair))
            br[2].append(self._post(self._cmult(b2, 2 * n, ell, inv), sh, scaled, pair))
            br[3].append(self._post(self._cmult(b3, 3 * n, ell, inv), sh, scaled, pair))

        subs = [self._blk(b, pair + 1, inv, sch, scaled) for b in br]
        out: list[Sample] = [(0, 0)] * ell
        for m in range(4):
            for k in range(q):
                out[4 * k + m] = subs[m][k]
        return out

    # ── helpers ───────────────────────────────────────────────────────

    def bit_reversed(self, natural: list[Sample]) -> list[Sample]:
        """Reorder a natural-order frame into bit-reversed (pre-reorder) order."""
        w = self.log2_n
        return [natural[int(format(i, f"0{w}b")[::-1], 2)] for i in range(self.n)]


if __name__ == "__main__":  # smoke: impulse is exact across sizes
    for lg in (3, 4, 7, 8):
        o = RrFftOracle(lg)
        imp = [(1000, 0)] + [(0, 0)] * (o.n - 1)
        r = o.run(imp)
        assert all(s == (1000, 0) for s in r.data), f"impulse failed lg={lg}"
    print("oracle smoke OK")
