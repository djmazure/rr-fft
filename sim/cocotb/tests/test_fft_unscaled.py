# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# rr_fft_top, G_HAS_SCALING = false — the unscaled bit-growth build
# (the unscaled xfft config runs INVERSE in the reference deployment).
#
#   FFT-REQ-002: inverse transform in unscaled mode is bit-exact to the
#                oracle including full bit growth (24-bit in, 33-bit out
#                per the oracle-proven width plan 24/27/29/31/33). The
#                same frame is also driven FORWARD unscaled — both
#                directions of the growth path against one golden set.
#
# Goldens hard-coded (fft_goldens.py, ROUTERTL-002). Config schedule
# bits are ignored by this build; only the fwd bit matters.

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

from fft_goldens import (  # noqa: E402
    LOG2_N, UNSCALED_FWD_EXPECTED, UNSCALED_IN, UNSCALED_INV_EXPECTED,
    UNSCALED_OW,
)

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")

N = 1 << LOG2_N
MASK24 = (1 << 24) - 1
OW = UNSCALED_OW
MASK_OW = (1 << OW) - 1


def main() -> None:
    run_simulation(
        top_level="rr_fft_top",
        module="test_fft_unscaled",
        generics={"G_HAS_SCALING": False},
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


def _to_signed(v: int, w: int) -> int:
    v &= (1 << w) - 1
    return v - (1 << w) if v >= (1 << (w - 1)) else v


async def _reset(dut) -> None:
    dut.ARESETN.value = 0
    dut.ACLKEN.value = 1
    dut.S_AXIS_CONFIG_TVALID.value = 0
    dut.S_AXIS_DATA_TVALID.value = 0
    dut.S_AXIS_DATA_TDATA.value = 0
    await ClockCycles(dut.ACLK, 5)
    dut.ARESETN.value = 1
    await ClockCycles(dut.ACLK, 2)


async def _run_stream(dut, frames_cfg: list,
                      probe: LatencyProbe | None = None) -> list:
    """Drive frames back-to-back (each with its config word latched at
    its own frame boundary — the word is presented one beat before the
    frame starts), followed by zero flush frames; collect len(frames_cfg)
    33-bit output frames.

    Frame alignment note: every element of the stream (real and flush)
    is exactly N beats, so the pairs' block phase stays frame-aligned
    throughout — flush beats ARE data to the valid-gated pipeline.
    """
    stream: list = []
    cfg_at: dict = {}
    for frame, cfg in frames_cfg:
        # pending-config register loads a beat BEFORE the frame boundary
        cfg_at[len(stream) - 1] = cfg
        stream += list(frame)
    stream += [(0, 0)] * (3 * N)   # flush: > worst latency, N-aligned

    if -1 in cfg_at:               # first frame's config: before the stream
        dut.S_AXIS_CONFIG_TDATA.value = cfg_at.pop(-1)
        dut.S_AXIS_CONFIG_TVALID.value = 1
        await RisingEdge(dut.ACLK)
        dut.S_AXIS_CONFIG_TVALID.value = 0

    results: list = []
    frame_acc: list = []
    idx = 0
    i = 0
    for _ in range(len(stream) + 6 * N):
        if i in cfg_at:
            dut.S_AXIS_CONFIG_TDATA.value = cfg_at[i]
            dut.S_AXIS_CONFIG_TVALID.value = 1
        else:
            dut.S_AXIS_CONFIG_TVALID.value = 0
        if i < len(stream):
            re, im = stream[i]
            dut.S_AXIS_DATA_TDATA.value = ((im & MASK24) << 24) | (re & MASK24)
            dut.S_AXIS_DATA_TVALID.value = 1
        else:
            dut.S_AXIS_DATA_TVALID.value = 0
        await RisingEdge(dut.ACLK)
        if i <= len(stream):
            i += 1
            if probe is not None and i <= len(frames_cfg) * N:
                probe.mark("in_valid")
        if dut.M_AXIS_DATA_TVALID.value == 1:
            raw = int(dut.M_AXIS_DATA_TDATA.value)
            re = _to_signed(raw & MASK_OW, OW)
            im = _to_signed((raw >> OW) & MASK_OW, OW)
            if probe is not None:
                probe.mark("out_valid")
            user = int(dut.M_AXIS_DATA_TUSER.value)
            assert user == idx, f"xk_index {user} != {idx}"
            frame_acc.append((re, im))
            idx += 1
            if idx == N:
                results.append(frame_acc)
                frame_acc = []
                idx = 0
                if len(results) == len(frames_cfg):
                    break
    dut.S_AXIS_DATA_TVALID.value = 0
    dut.S_AXIS_CONFIG_TVALID.value = 0
    assert len(results) == len(frames_cfg), (
        f"timeout: {len(results)}/{len(frames_cfg)} frames collected")
    return results


def _check(got: list, exp: list, tag: str) -> None:
    mism = [(k, g, e) for k, (g, e) in enumerate(zip(got, exp))
            if g != tuple(e)]
    assert not mism, (
        f"FFT-REQ-002 {tag} failed: {len(mism)}/{N} samples differ; first "
        f"at k={mism[0][0]}: got {mism[0][1]}, expected {tuple(mism[0][2])}"
    )


@cocotb.test()
@requires("FFT-REQ-002")
async def test_unscaled_bit_growth_bit_exact(dut) -> None:
    """Inverse (the reference unscaled config) then forward, unscaled,
    full-scale input — 33-bit outputs bit-exact vs oracle."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)

    probe = LatencyProbe(clk_period_ns=4.0)
    probe.add_latency("in_valid", "out_valid")
    probe.add_ii("in_valid")
    inv, fwd = await _run_stream(dut, [
        (UNSCALED_IN, 0x000),   # inverse (the reference unscaled config)
        (UNSCALED_IN, 0x001),   # forward, back-to-back, config at boundary
    ], probe=probe)
    probe.finalize()
    _check(inv, UNSCALED_INV_EXPECTED, "inverse")
    _check(fwd, UNSCALED_FWD_EXPECTED, "forward")

    dut._log.info(
        "FFT-REQ-002 PASS — unscaled inverse + forward bit-exact, "
        f"{OW}-bit outputs, full-scale stimulus"
    )


if __name__ == "__main__":
    main()
