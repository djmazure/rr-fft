"""Oracle-seam adapters — scalar-shaped goldens for `rr sim golden mint`.

RTL-P2.1065. `model/rr_fft_oracle.py` is the arithmetic spec (FFT-P2.2) and
stays the single source of truth; this module only RESHAPES it for the
routertl oracle seam (RTL-P3.1468), which mints one fixture per REQ and
requires each case to be:

    vector: a mapping whose drive-mapped entries are SCALARS (int/bool)
    golden: a value that resolves to a SCALAR at the declared golden_key

That scalar contract is what `rr_test_fill` (RTL-P3.1479) consumes to write a
runnable test, so a frame-valued golden cannot be filled today — see
FFT-REQ-030's note and the follow-up ticket. Nothing here re-implements
arithmetic: every expected value comes back out of RrFftOracle.

Determinism: pure functions of the vector, no RNG, no clock, no file reads.
Re-minting with an unchanged oracle + vectors is byte-identical.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The oracle lives beside this file; keep the import path explicit rather than
# relying on the caller's cwd (golden_mint imports us by file path).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from rr_fft_oracle import RrFftOracle  # noqa: E402


def _oracle(vector: dict) -> RrFftOracle:
    return RrFftOracle(
        int(vector["log2_n"]),
        data_width=int(vector.get("data_width", 24)),
        twiddle_width=int(vector.get("twiddle_width", 24)),
    )


def _two_c(value: int, width: int) -> int:
    """Two's-complement bit pattern of *value* in *width* bits."""
    return value & ((1 << width) - 1)


def dc_response(vector: dict) -> dict:
    """X[0] of a constant (DC) input frame — the scalar-expressible property.

    Why DC: `rr_test_fill` drives one scalar per signal and holds it for
    `cycles` beats, so the only input frame it can present is a constant. For
    a constant input A the transform is exact and non-trivial at k=0
    (X[0] = sum of the frame, then the schedule's truncating shifts), which
    exercises the adder tree, the scale schedule, and the output packing —
    every path except the twiddle multipliers, which a DC frame bypasses
    (W^0 is unity and is skipped by construction; see the oracle docstring).

    vector keys: log2_n, dc_re, dc_im, sch (list), scaled (bool),
                 inverse (bool, optional), data_width/twiddle_width (optional)
    golden keys: x0_re, x0_im, tdata_scaled (packed 2 x DATA_WIDTH),
                 overflow
    """
    orc = _oracle(vector)
    frame = [(int(vector["dc_re"]), int(vector["dc_im"]))] * orc.n
    result = orc.run(
        frame,
        inverse=bool(vector.get("inverse", False)),
        scale_schedule=list(vector["sch"]),
        scaled=bool(vector["scaled"]),
    )
    re, im = result.data[0]
    width = int(vector.get("data_width", 24))
    return {
        "x0_re": int(re),
        "x0_im": int(im),
        # Packed exactly as the scaled shim presents it: imag in the high
        # half, real in the low half, each two's-complement DATA_WIDTH.
        "tdata_scaled": (_two_c(int(im), width) << width) | _two_c(int(re), width),
        "overflow": bool(result.overflow),
    }


def shim_sign_extend(vector: dict) -> dict:
    """Unscaled-shim output packing: each core component sign-extended.

    Mirrors rtl/rr_fft_xfft_shim.vhd g_out_unscaled, which does
    ``resize(signed(core_comp), C_POW)`` per component and packs imag high /
    real low. core_width = DATA_WIDTH + 2*NPAIRS + 1 (f_wplan, unscaled);
    out_width = f_shim_ow(false) = 40. Pure bit manipulation — no transform.

    vector keys: core_re, core_im, core_width, out_width
    golden keys: tdata (packed 2 x out_width), re_ext, im_ext
    """
    core_w = int(vector["core_width"])
    out_w = int(vector["out_width"])
    if out_w < core_w:
        raise ValueError(f"out_width {out_w} narrower than core_width {core_w}")
    re = int(vector["core_re"])
    im = int(vector["core_im"])
    lim = 1 << (core_w - 1)
    if not (-lim <= re < lim) or not (-lim <= im < lim):
        raise ValueError(
            f"core component out of signed {core_w}-bit range: re={re} im={im}"
        )
    re_ext = _two_c(re, out_w)
    im_ext = _two_c(im, out_w)
    return {
        "tdata": (im_ext << out_w) | re_ext,
        "re_ext": re_ext,
        "im_ext": im_ext,
    }
