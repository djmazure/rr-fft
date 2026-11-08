# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# rr_fft_xfft_shim, unscaled, built with G_TWIDDLE_ROM_STYLE = "block"
# (FFT-P2.12). The shim is what a design swapping out an xfft instance
# instantiates, so the style must reach the core through it.
#
# test_shim_block_rom_live_unscaled_bit_exact (FFT-REQ-040): the first pair
#   of the core under the shim carries the block-RAM twiddle ROM, and the
#   reference-deployment stream (config 0xB1, forward, unscaled 33-bit
#   output, nine frames back to back) is bit-exact to the oracle, with the
#   default build's latency. Forward only: the inverse half of the block ROM
#   is guarded by test_fft_twiddle_block.

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

import cocotb

_tb = str(_Path(__file__).resolve().parent)
if _tb not in _sys.path:
    _sys.path.insert(0, _tb)
del _tb

from engine.simulation import run_simulation  # noqa: E402
from sdk.cocotb_helpers import requires  # noqa: E402
from sdk.cocotb_helpers.latency_probe import LatencyProbe  # noqa: E402

from fft_live_driver import first_mismatch, run_stream  # noqa: E402
from fft_live_goldens import (FRAME_NAMES, IN_FRAMES,  # noqa: E402
                              UNSCALED_CONFIG_WORD, UNSCALED_ORACLE)

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")
_MODULE = "test_fft_shim_twiddle_block"

# in_valid -> out_valid worst latency of the default (distributed) build on
# this stream (test_fft_shim_live_unscaled, FFT-REQ-023 run)
DEFAULT_LATENCY_CYCLES = 557


def main() -> None:
    run_simulation(
        top_level="rr_fft_xfft_shim",
        module=_MODULE,
        custom_libraries={
            "work": [
                f"{_RTL_DIR}/rr_fft_pkg.vhd",
                f"{_RTL_DIR}/rr_fft_sdf_pair.vhd",
                f"{_RTL_DIR}/rr_fft_reorder.vhd",
                f"{_RTL_DIR}/rr_fft_top.vhd",
                f"{_RTL_DIR}/rr_fft_xfft_shim.vhd",
            ],
        },
        generics={"G_HAS_SCALING": False, "G_TWIDDLE_ROM_STYLE": "block"},
        waves=True,
        simulator="nvc",
    )


@cocotb.test()
@requires("FFT-REQ-040")
async def test_shim_block_rom_live_unscaled_bit_exact(dut) -> None:
    pair0 = dut.i_core.GEN_PAIRS[0].i_pair
    try:
        pair0.GEN_TW_BLOCK.s3Wb_r
    except AttributeError as exc:
        raise AssertionError(
            "FFT-REQ-040 failed: G_TWIDDLE_ROM_STYLE = \"block\" on the shim "
            "did not reach the core: its first pair has no block twiddle ROM "
            f"(GEN_TW_BLOCK.s3Wb_r; {exc})") from None
    assert UNSCALED_CONFIG_WORD == 0xB1
    probe = LatencyProbe(clk_period_ns=4.0)
    probe.add_latency("in_valid", "out_valid")
    got = await run_stream(dut, 0xB1, IN_FRAMES, comp_w=40, value_w=33,
                           probe=probe)
    bad = first_mismatch(got, UNSCALED_ORACLE)
    assert bad is None, (
        f"FFT-REQ-040 failed: frame {FRAME_NAMES[bad[0]]} bin {bad[1]}: "
        f"got {got[bad[0]][bad[1]]}, oracle "
        f"{tuple(UNSCALED_ORACLE[bad[0]][bad[1]])}")
    worst = probe.finalize()["latency.in_valid__out_valid"]["worst"]
    assert worst == DEFAULT_LATENCY_CYCLES, (
        f"FFT-REQ-040 failed: shim block-ROM latency {worst} cyc, the "
        f"default build's is {DEFAULT_LATENCY_CYCLES}")
    dut._log.info("FFT-REQ-040 PASS: shim, block ROM, config 0xB1, "
                  "%d frames bit-exact, latency %d cyc", len(got), worst)


if __name__ == "__main__":
    main()
