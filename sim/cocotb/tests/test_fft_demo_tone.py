# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# Demo: steady single tone through rr_fft — the classic clean-spike
# spectrum picture. One full-scale complex tone at a fixed bin, phase-
# continuous across frames, driven through the flush region too so
# RouteWave's latest-window line plot shows the peak (not silence).
#
# Self-checking: every frame's hardware spectral peak must sit on
# TONE_BIN.

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
TONE_BIN = 16                # single steady tone: one clean spectral line
CONFIG_WORD = 0x155          # forward, schedule [2,2,2,2]


def main() -> None:
    run_simulation(
        top_level="rr_fft_top",
        module="test_fft_demo_tone",
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

    # tone everywhere, including the flush region: the pipeline drains
    # while the stimulus continues, so the NEWEST spectrum window (the
    # line plot) shows the tone, not silence
    beats = [b for _ in range(N_FRAMES + 3) for b in _tone(TONE_BIN)]

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
    assert peaks == [TONE_BIN] * N_FRAMES, (
        f"peak not steady at bin {TONE_BIN}: {sorted(set(peaks))}"
    )
    dut._log.info(
        f"TONE DEMO PASS — steady spectral peak at bin {TONE_BIN} across "
        f"{N_FRAMES} frames; VCD ready for RouteWave"
    )


if __name__ == "__main__":
    main()
