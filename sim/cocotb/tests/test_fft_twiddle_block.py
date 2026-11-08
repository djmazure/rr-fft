# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# rr_fft_top built with G_TWIDDLE_ROM_STYLE = "block" (FFT-P2.12): the first
# pair's twiddle ROM is a block RAM read (s3) plus a plain register (s4),
# holding the unity word and the inverse conjugates, instead of a LUT ROM
# followed by fabric conjugation and the unity bypass.
#
#   FFT-REQ-040: the block style changes resources only. The output stream is
#                the same bit-exact oracle stream as the default build, in
#                both directions (the inverse half of the block ROM is the
#                conjugate half), and the input-to-output latency equals the
#                default build's, a hard-coded number measured on the
#                distributed build.
#
# Each test first checks that the block RAM path is the one elaborated: a
# simulator that ignored the generic would run the LUT ROM and prove nothing.
# The goldens and the drive loop are the core suite's (fft_goldens.py,
# test_fft_core helpers), so both styles answer to the same oracle.

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock

_tb = str(_Path(__file__).resolve().parent)
if _tb not in _sys.path:
    _sys.path.insert(0, _tb)
del _tb

from engine.simulation import run_simulation  # noqa: E402
from sdk.cocotb_helpers import requires  # noqa: E402
from sdk.cocotb_helpers.latency_probe import LatencyProbe  # noqa: E402

from fft_goldens import (  # noqa: E402
    CONFIG_WORD, EXPECTED, IN_FRAMES, INV_CONFIG_WORD, INV_EXPECTED, INV_IN,
)
from test_fft_core import _check, _reset, _session  # noqa: E402

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")
_MODULE = "test_fft_twiddle_block"

# in_valid -> out_valid worst latency of the DEFAULT (distributed) build,
# measured by test_fft_core.test_back_to_back_ii1 on the three IN_FRAMES
# (ROUTERTL-002: a number, not the block build measuring itself).
DEFAULT_LATENCY_CYCLES = 557


def main() -> None:
    run_simulation(
        top_level="rr_fft_top",
        module=_MODULE,
        custom_libraries={
            "work": [
                f"{_RTL_DIR}/rr_fft_pkg.vhd",
                f"{_RTL_DIR}/rr_fft_sdf_pair.vhd",
                f"{_RTL_DIR}/rr_fft_reorder.vhd",
                f"{_RTL_DIR}/rr_fft_top.vhd",
            ],
        },
        generics={"G_TWIDDLE_ROM_STYLE": "block"},
        waves=True,
        simulator="nvc",
    )


def _assert_block_rom_elaborated(dut) -> None:
    """The first pair must carry the block-RAM generate, not the LUT one."""
    pair0 = dut.GEN_PAIRS[0].i_pair
    try:
        handle = pair0.GEN_TW_BLOCK.s3Wb_r
    except AttributeError as exc:
        raise AssertionError(
            "FFT-REQ-040 failed: the first pair has no block twiddle ROM "
            "(GEN_TW_BLOCK.s3Wb_r); G_TWIDDLE_ROM_STYLE = \"block\" was not "
            f"elaborated ({exc})") from None
    assert len(handle) == 2 * (24 + 1), (
        f"FFT-REQ-040 failed: block ROM word is {len(handle)} bits, "
        "expected 50 (wi, wr at TW + 1 bits each)")


@cocotb.test()
@requires("FFT-REQ-040")
async def test_block_rom_forward_bit_exact_same_latency(dut) -> None:
    """3 back-to-back forward frames, schedule [2,2,2,2]: bit-exact to the
    oracle, and the worst in->out latency equals the default build's."""
    _assert_block_rom_elaborated(dut)
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    probe = LatencyProbe(clk_period_ns=4.0)
    probe.add_latency("in_valid", "out_valid")
    probe.add_ii("in_valid")
    results = await _session(dut, CONFIG_WORD, IN_FRAMES, len(IN_FRAMES),
                             probe=probe)
    meas = probe.finalize()
    _check(results, EXPECTED, "FFT-REQ-040(forward)")
    lat = meas["latency.in_valid__out_valid"]
    assert lat["worst"] == DEFAULT_LATENCY_CYCLES, (
        f"FFT-REQ-040 failed: block-ROM latency worst={lat['worst']} cyc, "
        f"the default build's is {DEFAULT_LATENCY_CYCLES}")
    assert meas["ii.in_valid"]["worst"] == 1, \
        f"FFT-REQ-040 failed: II worst={meas['ii.in_valid']['worst']}"
    dut._log.info(f"FFT-REQ-040 PASS — forward bit-exact, latency "
                  f"{lat['worst']} cyc")


@cocotb.test()
@requires("FFT-REQ-040")
async def test_block_rom_inverse_bit_exact(dut) -> None:
    """Inverse frame (schedule [0,0,0,0]) through the conjugate half of the
    block ROM: bit-exact to the oracle chain."""
    _assert_block_rom_elaborated(dut)
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    results = await _session(dut, INV_CONFIG_WORD, [INV_IN], 1)
    _check(results, [INV_EXPECTED], "FFT-REQ-040(inverse)")
    dut._log.info("FFT-REQ-040 PASS — inverse bit-exact")


if __name__ == "__main__":
    main()
