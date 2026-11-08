# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# rr_fft_xfft_shim, scaled build, in the configuration the reference
# deployment drives: config word 0x156 = FWD_INV 0 (INVERSE) with schedule
# field 0xAB, per stage pair from the input side [3, 2, 2, 2] (1/512).
#
# test_live_scaled_inverse_bit_exact (FFT-REQ-022): nine frames back to back
#   (impulses, tones in bins 1, 37 and 200, full-scale random, full-scale DC,
#   a small tone), bit-exact to the oracle, xk_index and tlast per PG109.
# test_live_scaled_inverse_vs_xfft (FFT-REQ-024): the same RTL output against
#   the Xilinx xfft v9.1 bit-accurate C model. Bound: at most 1 LSB per
#   component on every frame, SNR >= 100 dB on the full-scale frames (the
#   docs/ARCHITECTURE.md tier-2 bound of model/test_xfft_crosscheck.py).

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

from fft_live_driver import (compare_to_xfft, first_mismatch, log_table,  # noqa: E402
                             run_stream, write_report)
from fft_live_goldens import (FRAME_NAMES, IN_FRAMES, SCALED_CONFIG_WORD,  # noqa: E402
                              SCALED_ORACLE, SCALED_XFFT)

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")
_MODULE = "test_fft_shim_live_scaled"

# Hard-coded acceptance bounds (ROUTERTL-002).
XFFT_MAX_LSB = 1
XFFT_MIN_SNR_DB = 100.0
FULL_SCALE = {"tone_bin1_full_scale", "tone_bin37_full_scale",
              "random_full_scale_a", "random_full_scale_b"}


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
        waves=True,
        simulator="nvc",
    )


@cocotb.test()
@requires("FFT-REQ-022")
async def test_live_scaled_inverse_bit_exact(dut) -> None:
    assert SCALED_CONFIG_WORD == 0x156
    probe = LatencyProbe(clk_period_ns=4.0)
    probe.add_latency("in_valid", "out_valid")
    probe.add_ii("in_valid")
    got = await run_stream(dut, 0x156, IN_FRAMES, comp_w=24, value_w=24,
                           probe=probe)
    bad = first_mismatch(got, SCALED_ORACLE)
    assert bad is None, (
        f"FFT-REQ-022 failed: frame {FRAME_NAMES[bad[0]]} bin {bad[1]}: "
        f"got {got[bad[0]][bad[1]]}, oracle {tuple(SCALED_ORACLE[bad[0]][bad[1]])}")
    dut._log.info("FFT-REQ-022 PASS: config 0x156, %d frames back to back, "
                  "bit-exact to the oracle", len(got))


@cocotb.test()
@requires("FFT-REQ-024")
async def test_live_scaled_inverse_vs_xfft(dut) -> None:
    got = await run_stream(dut, 0x156, IN_FRAMES, comp_w=24, value_w=24)
    rows = compare_to_xfft(got, SCALED_XFFT)
    log_table(dut._log, FRAME_NAMES, rows)
    path = write_report(_MODULE, 0x156, FRAME_NAMES, rows)
    dut._log.info("per-sample error written to %s", path)
    for nm, r in zip(FRAME_NAMES, rows):
        assert r["max_lsb"] <= XFFT_MAX_LSB, (
            f"{nm}: {r['max_lsb']} LSB from xfft (bound {XFFT_MAX_LSB})")
        if nm in FULL_SCALE:
            assert r["snr_db"] >= XFFT_MIN_SNR_DB, (
                f"{nm}: SNR {r['snr_db']:.1f} dB against xfft "
                f"(bound {XFFT_MIN_SNR_DB} dB)")


if __name__ == "__main__":
    main()
