"""Generate the hard-coded goldens for the reference-deployment configurations.

The reference deployment instantiates the xfft v9.1 shape in two ways, and
neither is covered by the forward-scaled / unscaled-inverse goldens of
gen_golden.py:

  scaled build,   config 0x156: FWD_INV = 0 (INVERSE), schedule field
                  bits 8:1 = 0xAB -> per pair (input side first) [3, 2, 2, 2]
  unscaled build, config 0xB1:  FWD_INV = 1 (FORWARD); bits 7:1 are padding

For each configuration this writes, per input frame:
  * the oracle output (the bit-exact reference, FFT-REQ-020 / FFT-REQ-021);
  * the Xilinx xfft v9.1 bit-accurate C-model output (the SNR-equivalence
    reference, docs/ARCHITECTURE.md tier 2) and its overflow flag.

Writes sim/cocotb/tests/fft_live_goldens.py, committed and never regenerated
at test runtime (ROUTERTL-002). The C-model columns need a Vivado install
($XILINX_VIVADO); model/test_xfft_live_configs.py checks that the committed
file still matches both engines. Run from the repo root:

    XILINX_VIVADO=<vivado root> python model/gen_live_goldens.py
"""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path

from rr_fft_oracle import RrFftOracle

LOG2_N = 8
N = 1 << LOG2_N
DW = 24
LIM = 1 << (DW - 1)

# Configuration words exactly as the reference deployment drives them.
SCALED_CONFIG_WORD = 0x156     # "0000000101010110"
UNSCALED_CONFIG_WORD = 0xB1    # "10110001"


def config_fields(word: int) -> tuple[bool, list[int]]:
    """Decode an xfft v9.1 scaled config word per PG109 field order.

    No NFFT and no CP_LEN field in this configuration, so FWD_INV is bit 0
    and SCALE_SCH follows it; the first (input-side) stage pair occupies the
    lowest two bits of SCALE_SCH. Returns (forward, schedule[pair]).
    """
    forward = bool(word & 1)
    sch = [(word >> (1 + 2 * p)) & 0x3 for p in range((LOG2_N + 1) // 2)]
    return forward, sch


SCALED_FORWARD, SCALED_SCH = config_fields(SCALED_CONFIG_WORD)
UNSCALED_FORWARD = bool(UNSCALED_CONFIG_WORD & 1)
assert (SCALED_FORWARD, SCALED_SCH) == (False, [3, 2, 2, 2])
assert UNSCALED_FORWARD is True


def _clip(v: int) -> int:
    return max(-LIM, min(LIM - 1, v))


def _tone(k: int, amp: float, ph: float = 0.0) -> list[tuple[int, int]]:
    return [(_clip(math.floor(amp * math.cos(2 * math.pi * k * n / N + ph) + 0.5)),
             _clip(math.floor(amp * math.sin(2 * math.pi * k * n / N + ph) + 0.5)))
            for n in range(N)]


def _random(seed: int) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    return [(rng.randrange(-LIM, LIM), rng.randrange(-LIM, LIM))
            for _ in range(N)]


# One stimulus corpus for both configurations. Driven back to back, with no
# idle cycle between frames, in the cocotb tests.
FRAMES: list[tuple[str, list[tuple[int, int]]]] = [
    ("impulse_n0_full_scale", [(LIM - 1, -LIM)] + [(0, 0)] * (N - 1)),
    ("impulse_n5", [(0, 0)] * 5 + [(0x400000, 0x123456)] + [(0, 0)] * (N - 6)),
    ("tone_bin1_full_scale", _tone(1, LIM - 1)),
    ("tone_bin37_full_scale", _tone(37, LIM - 1, 0.3)),
    ("tone_bin200_half_scale", _tone(200, LIM // 2, 1.1)),
    ("random_full_scale_a", _random(0x5156)),
    ("random_full_scale_b", _random(0x50B1)),
    ("dc_full_scale", [(LIM - 1, -LIM)] * N),
    ("tone_bin37_small", _tone(37, 1 << 12, 0.3)),
]


def _oracle_rows(scaled: bool) -> tuple[list, list]:
    o = RrFftOracle(LOG2_N, data_width=DW, twiddle_width=24)
    out, ovf = [], []
    for _, f in FRAMES:
        if scaled:
            r = o.run(f, inverse=not SCALED_FORWARD, scale_schedule=SCALED_SCH,
                      scaled=True)
        else:
            r = o.run(f, inverse=not UNSCALED_FORWARD)
        out.append(r.data)
        ovf.append(bool(r.overflow))
    return out, ovf


def _xfft_rows(scaled: bool) -> tuple[list, list]:
    from xfft_cmodel import run_xfft
    out, ovf = [], []
    for _, f in FRAMES:
        re = [s[0] for s in f]
        im = [s[1] for s in f]
        if scaled:
            r = run_xfft(re, im, scaled=True, scale_schedule=SCALED_SCH,
                         inverse=not SCALED_FORWARD)
        else:
            r = run_xfft(re, im, scaled=False, inverse=not UNSCALED_FORWARD)
        out.append(list(zip(r.re, r.im)))
        ovf.append(bool(r.overflow))
    return out, ovf


def build() -> dict:
    """Every golden column, recomputed from both engines (C model required)."""
    from xfft_cmodel import _CACHE, _find_vivado_root
    lib = _CACHE / "libIp_xfft_v9_1_bitacc_cmodel.so"
    s_or, s_or_ovf = _oracle_rows(True)
    u_or, u_or_ovf = _oracle_rows(False)
    s_x, s_x_ovf = _xfft_rows(True)
    u_x, u_x_ovf = _xfft_rows(False)
    zipname = (_find_vivado_root() / "data/ip/xilinx/xfft_v9_1/cmodel/"
               "xfft_v9_1_bitacc_cmodel_lin64.zip")
    return {
        "CMODEL_ZIP_SHA256": hashlib.sha256(zipname.read_bytes()).hexdigest(),
        "CMODEL_LIB_SHA256": hashlib.sha256(lib.read_bytes()).hexdigest(),
        "FRAME_NAMES": [n for n, _ in FRAMES],
        "IN_FRAMES": [f for _, f in FRAMES],
        "SCALED_ORACLE": s_or, "SCALED_ORACLE_OVERFLOW": s_or_ovf,
        "SCALED_XFFT": s_x, "SCALED_XFFT_OVERFLOW": s_x_ovf,
        "UNSCALED_ORACLE": u_or, "UNSCALED_ORACLE_OVERFLOW": u_or_ovf,
        "UNSCALED_XFFT": u_x, "UNSCALED_XFFT_OVERFLOW": u_x_ovf,
    }


def main() -> None:
    cols = build()
    assert not any(cols["SCALED_ORACLE_OVERFLOW"]), "scaled inverse corpus wraps"
    out = (Path(__file__).resolve().parents[1]
           / "sim/cocotb/tests/fft_live_goldens.py")
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            '"""AUTO-GENERATED by model/gen_live_goldens.py. Do not hand-edit.\n\n'
            "Goldens for the reference-deployment configurations, 256-point,\n"
            "24-bit input, 24-bit twiddles:\n"
            "  scaled build,   config 0x156: inverse, schedule [3, 2, 2, 2]\n"
            "  unscaled build, config 0xB1:  forward\n"
            "*_ORACLE is the bit-exact reference (model/rr_fft_oracle.py).\n"
            "*_XFFT is the Xilinx xfft v9.1 bit-accurate C model (pipelined\n"
            "streaming, truncation), the SNR-equivalence reference.\n"
            '"""\n\n')
        fh.write(f"LOG2_N = {LOG2_N}\n")
        fh.write(f"SCALED_CONFIG_WORD = {SCALED_CONFIG_WORD:#x}\n")
        fh.write(f"SCALED_SCHEDULE = {SCALED_SCH!r}\n")
        fh.write(f"UNSCALED_CONFIG_WORD = {UNSCALED_CONFIG_WORD:#x}\n")
        fh.write("UNSCALED_OW = 33\n\n")
        for k, v in cols.items():
            fh.write(f"{k} = {v!r}\n\n")
    print(f"wrote {out} ({len(FRAMES)} frames x 2 configurations x 2 references)")


if __name__ == "__main__":
    main()
