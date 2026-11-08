# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# rr_fft_xfft_shim, scaled build — FFT-REQ-020: the xfft v9.1 port shape
# is a transparent adapter (16-bit config passthrough, 48-bit data,
# tlast accepted and ignored, events tied low). Output bit-exact to the
# same goldens the bare core meets.

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

_tb = str(_Path(__file__).resolve().parent)
if _tb not in _sys.path:
    _sys.path.insert(0, _tb)
del _tb

from engine.simulation import run_simulation  # noqa: E402
from sdk.cocotb_helpers import requires  # noqa: E402
from sdk.cocotb_helpers.latency_probe import LatencyProbe  # noqa: E402

from fft_goldens import CONFIG_WORD, EXPECTED, IN_FRAMES, LOG2_N  # noqa: E402

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")

N = 1 << LOG2_N
MASK24 = (1 << 24) - 1


def main() -> None:
    run_simulation(
        top_level="rr_fft_xfft_shim",
        module="test_fft_shim_scaled",
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


def _to_signed24(v: int) -> int:
    v &= MASK24
    return v - (1 << 24) if v >= (1 << 23) else v


@cocotb.test()
@requires("FFT-REQ-020")
async def test_shim_scaled_transparent(dut) -> None:
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    dut.ARESETN.value = 0
    dut.ACLKEN.value = 1
    dut.S_AXIS_CONFIG_TVALID.value = 0
    dut.S_AXIS_DATA_TVALID.value = 0
    dut.S_AXIS_DATA_TDATA.value = 0
    dut.S_AXIS_DATA_TLAST.value = 0
    await ClockCycles(dut.ACLK, 5)
    dut.ARESETN.value = 1
    await ClockCycles(dut.ACLK, 2)

    probe = LatencyProbe(clk_period_ns=4.0)
    probe.add_latency("in_valid", "out_valid")
    probe.add_ii("in_valid")

    dut.S_AXIS_CONFIG_TDATA.value = CONFIG_WORD   # 16-bit xfft-shaped port
    dut.S_AXIS_CONFIG_TVALID.value = 1
    await RisingEdge(dut.ACLK)
    dut.S_AXIS_CONFIG_TVALID.value = 0

    beats = list(IN_FRAMES[0]) + [(0, 0)] * (N + 64)
    got: list = []
    idx = 0
    for i, (re, im) in enumerate(beats + [(0, 0)] * (4 * N)):
        drive = i < len(beats)
        if drive:
            dut.S_AXIS_DATA_TDATA.value = ((im & MASK24) << 24) | (re & MASK24)
            dut.S_AXIS_DATA_TVALID.value = 1
            # tlast exercised on the true frame boundary — must be ignored
            dut.S_AXIS_DATA_TLAST.value = 1 if (i % N) == N - 1 else 0
        else:
            dut.S_AXIS_DATA_TVALID.value = 0
            dut.S_AXIS_DATA_TLAST.value = 0
        await RisingEdge(dut.ACLK)
        if drive and i < N:
            probe.mark("in_valid")
        for ev in ("EVENT_FRAME_STARTED", "EVENT_TLAST_UNEXPECTED",
                   "EVENT_TLAST_MISSING", "EVENT_DATA_IN_CHANNEL_HALT"):
            assert int(getattr(dut, ev).value) == 0, f"{ev} asserted"
        if dut.M_AXIS_DATA_TVALID.value == 1 and idx < N:
            probe.mark("out_valid")
            r = _to_signed24(int(dut.M_AXIS_DATA_TDATA.value) & MASK24)
            q = _to_signed24((int(dut.M_AXIS_DATA_TDATA.value) >> 24) & MASK24)
            assert int(dut.M_AXIS_DATA_TUSER.value) == idx
            got.append((r, q))
            idx += 1
            if idx == N:
                break
    probe.finalize()
    assert len(got) == N, f"timeout: {len(got)}/{N}"
    mism = [k for k, (g, e) in enumerate(zip(got, EXPECTED[0]))
            if g != tuple(e)]
    assert not mism, f"FFT-REQ-020 failed at k={mism[0]}"
    dut._log.info("FFT-REQ-020 PASS — scaled shim transparent, bit-exact")


if __name__ == "__main__":
    main()
