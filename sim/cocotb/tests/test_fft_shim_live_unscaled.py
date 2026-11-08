# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# rr_fft_xfft_shim, unscaled build, in the configuration the reference
# deployment drives: 8-bit config word 0xB1, FWD_INV = bit 0 = 1 (FORWARD);
# bits 7:1 are padding and must have no effect.
#
# test_live_unscaled_forward_bit_exact (FFT-REQ-023): nine frames back to
#   back, bit-exact to the oracle, 80-bit output with each 33-bit component
#   sign-extended to 40 bits, xk_index and tlast per PG109.
# test_live_unscaled_forward_vs_xfft (FFT-REQ-025): the same RTL output
#   against the Xilinx xfft v9.1 bit-accurate C model. Bound: at most 64 LSB
#   of the 33-bit output on every frame, SNR >= 120 dB on the full-scale
#   frames (the tier-2 bound of model/test_xfft_crosscheck.py for the
#   unscaled paths).

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
from fft_live_goldens import (FRAME_NAMES, IN_FRAMES, UNSCALED_CONFIG_WORD,  # noqa: E402
                              UNSCALED_ORACLE, UNSCALED_XFFT)

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")
_MODULE = "test_fft_shim_live_unscaled"

# Hard-coded acceptance bounds (ROUTERTL-002).
XFFT_MAX_LSB = 64
XFFT_MIN_SNR_DB = 120.0
FULL_SCALE = {"tone_bin1_full_scale", "tone_bin37_full_scale",
              "random_full_scale_a", "random_full_scale_b"}


def main() -> None:
    run_simulation(
        top_level="rr_fft_xfft_shim",
        module=_MODULE,
        generics={"G_HAS_SCALING": False},
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
@requires("FFT-REQ-023")
async def test_live_unscaled_forward_bit_exact(dut) -> None:
    assert UNSCALED_CONFIG_WORD == 0xB1
    probe = LatencyProbe(clk_period_ns=4.0)
    probe.add_latency("in_valid", "out_valid")
    probe.add_ii("in_valid")
    got = await run_stream(dut, 0xB1, IN_FRAMES, comp_w=40, value_w=33,
                           probe=probe)
    bad = first_mismatch(got, UNSCALED_ORACLE)
    assert bad is None, (
        f"FFT-REQ-023 failed: frame {FRAME_NAMES[bad[0]]} bin {bad[1]}: "
        f"got {got[bad[0]][bad[1]]}, oracle {tuple(UNSCALED_ORACLE[bad[0]][bad[1]])}")
    dut._log.info("FFT-REQ-023 PASS: config 0xB1, %d frames back to back, "
                  "bit-exact to the oracle", len(got))


@cocotb.test()
@requires("FFT-REQ-025")
async def test_live_unscaled_forward_vs_xfft(dut) -> None:
    got = await run_stream(dut, 0xB1, IN_FRAMES, comp_w=40, value_w=33)
    rows = compare_to_xfft(got, UNSCALED_XFFT)
    log_table(dut._log, FRAME_NAMES, rows)
    path = write_report(_MODULE, 0xB1, FRAME_NAMES, rows)
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
