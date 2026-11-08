# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# Demo: frequency sweep through rr_fft — made for looking at, not just
# asserting. 64 back-to-back frames each carrying a full-scale complex
# tone whose bin walks 0, 4, 8, ... 252: on m_axis the spectral peak
# marches across xk_index frame by frame. The captured VCD is the input
# to demo/routewave_sweep (RouteWave's spectrum/fft view + analog lanes).
#
# Self-checking (a demo that can rot is a lie): per frame, the peak
# |X[k]| must land on the swept bin with dominant energy.

from __future__ import annotations

import math
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

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")

LOG2_N = 8
N = 1 << LOG2_N
MASK24 = (1 << 24) - 1
AMP = (1 << 21) - 1          # full-scale tone (2-bit headroom under [2,2,2,2])
N_FRAMES = 64
BIN_STEP = 4                 # peak walks 0,4,8,...252
CONFIG_WORD = 0x155          # forward, schedule [2,2,2,2]


def main() -> None:
    run_simulation(
        top_level="rr_fft_top",
        module="test_fft_demo_sweep",
        custom_libraries={
            "work": [
                f"{_RTL_DIR}/rr_fft_pkg.vhd",
                f"{_RTL_DIR}/rr_fft_sdf_pair.vhd",
                f"{_RTL_DIR}/rr_fft_reorder.vhd",
                f"{_RTL_DIR}/rr_fft_top.vhd",
            ],
        },
        waves=True,
        simulator="nvc",
    )


def _tone(k: int) -> list:
    return [(round(AMP * math.cos(2 * math.pi * k * n / N)),
             round(AMP * math.sin(2 * math.pi * k * n / N)))
            for n in range(N)]


def _to_signed24(v: int) -> int:
    v &= MASK24
    return v - (1 << 24) if v >= (1 << 23) else v


@cocotb.test()
async def test_sweep_peak_marches(dut) -> None:
    """64-frame bin sweep: hardware spectral peak tracks the stimulus."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    dut.ARESETN.value = 0
    dut.ACLKEN.value = 1
    dut.S_AXIS_CONFIG_TVALID.value = 0
    dut.S_AXIS_DATA_TVALID.value = 0
    dut.S_AXIS_DATA_TDATA.value = 0
    await ClockCycles(dut.ACLK, 5)
    dut.ARESETN.value = 1
    await ClockCycles(dut.ACLK, 2)

    dut.S_AXIS_CONFIG_TDATA.value = CONFIG_WORD
    dut.S_AXIS_CONFIG_TVALID.value = 1
    await RisingEdge(dut.ACLK)
    dut.S_AXIS_CONFIG_TVALID.value = 0

    beats = [b for f in range(N_FRAMES) for b in _tone(f * BIN_STEP)]
    beats += [(0, 0)] * (3 * N)   # flush, N-aligned

    peaks: list = []
    frame: list = []
    i = 0
    for _ in range(len(beats) + 6 * N):
        if i < len(beats):
            re, im = beats[i]
            dut.S_AXIS_DATA_TDATA.value = ((im & MASK24) << 24) | (re & MASK24)
            dut.S_AXIS_DATA_TVALID.value = 1
        else:
            dut.S_AXIS_DATA_TVALID.value = 0
        await RisingEdge(dut.ACLK)
        if i < len(beats):
            i += 1
        if dut.M_AXIS_DATA_TVALID.value == 1:
            r = _to_signed24(int(dut.M_AXIS_DATA_TDATA.value) & MASK24)
            q = _to_signed24((int(dut.M_AXIS_DATA_TDATA.value) >> 24) & MASK24)
            frame.append(r * r + q * q)
            if len(frame) == N:
                peaks.append(max(range(N), key=frame.__getitem__))
                frame = []
                if len(peaks) == N_FRAMES:
                    break
    dut.S_AXIS_DATA_TVALID.value = 0

    assert len(peaks) == N_FRAMES, f"collected {len(peaks)}/{N_FRAMES} frames"
    expect = [f * BIN_STEP for f in range(N_FRAMES)]
    assert peaks == expect, (
        f"peak did not track the sweep; first mismatch at frame "
        f"{next(f for f, (p, e) in enumerate(zip(peaks, expect)) if p != e)}"
    )
    dut._log.info(
        f"SWEEP DEMO PASS — spectral peak marched 0..{expect[-1]} in steps "
        f"of {BIN_STEP} across {N_FRAMES} frames; VCD ready for RouteWave"
    )


if __name__ == "__main__":
    main()
