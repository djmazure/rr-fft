# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# rr_fft_top contract tests — bit-exact vs oracle goldens (hard-coded,
# ROUTERTL-002; fft_goldens.py is generated offline by model/gen_golden.py
# and committed).
#
#   FFT-REQ-001: forward scaled transform bit-exact (impulse + 2 random
#                frames, schedule [2,2,2,2], back-to-back — also REQ-005).
#   FFT-REQ-003: natural-order output, xk_index on tuser, tlast per frame.
#   FFT-REQ-006: ACLKEN deassertion freezes ALL state exactly — a randomly
#                gated run yields the identical output stream.
#   FFT-REQ-007: round-trip — frame 1's forward spectrum driven back in
#                inverse mode reconstructs the input within the oracle-
#                predicted truncation (golden chain embeds the identity).
#
# The datapath is valid-gated (data-pushed): trailing flush beats drain
# the final frame from the SDF delay lines (see FFT-P3.1).

from __future__ import annotations

import random
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
    CONFIG_WORD, CONFIG_WORD_B, EXPECTED, EXPECTED_B_FRAME2, IN_FRAMES,
    INV_CONFIG_WORD, INV_EXPECTED, INV_IN, LOG2_N,
)

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")

N = 1 << LOG2_N
MASK24 = (1 << 24) - 1


def main() -> None:
    run_simulation(
        top_level="rr_fft_top",
        module="test_fft_core",
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


def _to_signed24(v: int) -> int:
    v &= MASK24
    return v - (1 << 24) if v >= (1 << 23) else v


async def _reset(dut) -> None:
    dut.ARESETN.value = 0
    dut.ACLKEN.value = 1
    dut.S_AXIS_CONFIG_TVALID.value = 0
    dut.S_AXIS_DATA_TVALID.value = 0
    dut.S_AXIS_DATA_TDATA.value = 0
    await ClockCycles(dut.ACLK, 5)
    dut.ARESETN.value = 1
    await ClockCycles(dut.ACLK, 2)


async def _session(dut, cfg_word: int, in_frames: list, n_out: int,
                   gate_seed: int | None = None,
                   probe: LatencyProbe | None = None,
                   cfg_mid: tuple | None = None) -> list:
    """Drive config + frames (+ flush), collect n_out frames.

    With gate_seed set, ACLKEN is deasserted pseudo-randomly (~30% of
    cycles); inputs are held and outputs sampled only on enabled edges,
    so the collected stream must be IDENTICAL to an ungated run
    (FFT-REQ-006). xk_index/tlast are asserted per beat (FFT-REQ-003).
    """
    rng = random.Random(gate_seed)

    dut.S_AXIS_CONFIG_TDATA.value = cfg_word
    dut.S_AXIS_CONFIG_TVALID.value = 1
    dut.ACLKEN.value = 1
    await RisingEdge(dut.ACLK)
    dut.S_AXIS_CONFIG_TVALID.value = 0

    n_real = sum(len(f) for f in in_frames)
    beats = [b for f in in_frames for b in f] + [(0, 0)] * (N + 64)
    results: list = []
    frame: list = []
    idx = 0
    i = 0
    presented = False
    budget = (len(beats) + 6 * N) * (3 if gate_seed is not None else 1)

    for _ in range(budget):
        en = 1 if gate_seed is None else int(rng.random() < 0.7)
        dut.ACLKEN.value = en
        if cfg_mid is not None and i == cfg_mid[0]:
            dut.S_AXIS_CONFIG_TDATA.value = cfg_mid[1]
            dut.S_AXIS_CONFIG_TVALID.value = 1
        elif cfg_mid is not None:
            dut.S_AXIS_CONFIG_TVALID.value = 0
        if en:
            if i < len(beats):
                re, im = beats[i]
                dut.S_AXIS_DATA_TDATA.value = \
                    ((im & MASK24) << 24) | (re & MASK24)
                dut.S_AXIS_DATA_TVALID.value = 1
                presented = True
            else:
                dut.S_AXIS_DATA_TVALID.value = 0
                presented = False
        await RisingEdge(dut.ACLK)
        if en == 0:
            continue
        if presented:
            i += 1
            if probe is not None and i <= n_real:
                probe.mark("in_valid")   # consumed real beat (flush excluded)
        if dut.M_AXIS_DATA_TVALID.value == 1:
            re = _to_signed24(int(dut.M_AXIS_DATA_TDATA.value) & MASK24)
            im = _to_signed24((int(dut.M_AXIS_DATA_TDATA.value) >> 24) & MASK24)
            if probe is not None:
                probe.mark("out_valid")
            user = int(dut.M_AXIS_DATA_TUSER.value)
            last = int(dut.M_AXIS_DATA_TLAST.value)
            assert user == idx, \
                f"FFT-REQ-003 failed: xk_index {user} != expected {idx}"
            assert last == (1 if idx == N - 1 else 0), \
                f"FFT-REQ-003 failed: tlast={last} at index {idx}"
            frame.append((re, im))
            idx += 1
            if idx == N:
                results.append(frame)
                frame = []
                idx = 0
                if len(results) == n_out:
                    break
    dut.S_AXIS_DATA_TVALID.value = 0
    dut.ACLKEN.value = 1
    assert len(results) == n_out, \
        f"timeout: {len(results)}/{n_out} frames collected"
    return results


def _check(results: list, expected: list, req: str) -> None:
    for fi, (got, exp) in enumerate(zip(results, expected)):
        mism = [(k, g, e) for k, (g, e) in enumerate(zip(got, exp))
                if g != tuple(e)]
        assert not mism, (
            f"{req} failed: frame {fi}, {len(mism)}/{N} samples differ; "
            f"first at k={mism[0][0]}: got {mism[0][1]}, "
            f"expected {tuple(mism[0][2])}"
        )


@cocotb.test()
@requires("FFT-REQ-001")
async def test_forward_scaled_bit_exact(dut) -> None:
    """3 back-to-back frames, forward scaled [2,2,2,2] — bit-exact."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    results = await _session(dut, CONFIG_WORD, IN_FRAMES, len(IN_FRAMES))
    _check(results, EXPECTED, "FFT-REQ-001")
    dut._log.info("FFT-REQ-001 PASS — 3 frames bit-exact vs oracle")


@cocotb.test()
@requires("FFT-REQ-007")
async def test_roundtrip_inverse_bit_exact(dut) -> None:
    """Frame 1's spectrum through the inverse path — bit-exact vs the
    oracle chain (which reconstructs the original input within 123 LSB,
    proven at golden-generation time)."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    results = await _session(dut, INV_CONFIG_WORD, [INV_IN], 1)
    _check(results, [INV_EXPECTED], "FFT-REQ-007")
    dut._log.info("FFT-REQ-007 PASS — inverse round-trip bit-exact")


@cocotb.test()
@requires("FFT-REQ-003")
async def test_natural_order_xk_index(dut) -> None:
    """`M_AXIS_DATA_TUSER` (xk_index) counts 0..N-1 in natural order and
    `M_AXIS_DATA_TLAST` marks exactly the final sample of each frame."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    users: list = []
    lasts: list = []

    async def _watch():
        while len(users) < 2 * N:
            await RisingEdge(dut.ACLK)
            if dut.M_AXIS_DATA_TVALID.value == 1:
                users.append(int(dut.M_AXIS_DATA_TUSER.value))
                lasts.append(int(dut.M_AXIS_DATA_TLAST.value))

    watcher = cocotb.start_soon(_watch())
    await _session(dut, CONFIG_WORD, [IN_FRAMES[0], IN_FRAMES[1]], 2)
    await ClockCycles(dut.ACLK, 2)   # let the watcher drain the final edge
    assert len(users) == 2 * N, f"watcher saw {len(users)}/{2 * N} beats"
    assert users == list(range(N)) + list(range(N)), \
        "FFT-REQ-003 failed: xk_index not natural 0..N-1 per frame"
    assert all((l == 1) == (u == N - 1) for u, l in zip(users, lasts)), \
        "FFT-REQ-003 failed: tlast misplaced"
    dut._log.info("FFT-REQ-003 PASS — natural order + tlast placement")


@cocotb.test()
@requires("FFT-REQ-004")
async def test_config_applies_at_frame_boundary(dut) -> None:
    """A config word written MID-frame must not affect that frame and
    must apply exactly at the next frame boundary (schedule B changes
    the numbers, so a violation is bit-visible either way)."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    results = await _session(
        dut, CONFIG_WORD, [IN_FRAMES[2], IN_FRAMES[2]], 2,
        cfg_mid=(N // 2, CONFIG_WORD_B))   # written mid-frame-1
    _check([results[0]], [EXPECTED[2]], "FFT-REQ-004(frame1 unchanged)")
    _check([results[1]], [EXPECTED_B_FRAME2], "FFT-REQ-004(frame2 uses B)")
    dut._log.info("FFT-REQ-004 PASS — mid-frame write deferred to boundary")


@cocotb.test()
@requires("FFT-REQ-005")
async def test_back_to_back_ii1(dut) -> None:
    """Gapless back-to-back frames: II on `S_AXIS_DATA_TVALID` beats is
    exactly 1 and every sample survives (no loss, no corruption)."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    probe = LatencyProbe(clk_period_ns=4.0)
    probe.add_latency("in_valid", "out_valid")
    probe.add_ii("in_valid")
    results = await _session(dut, CONFIG_WORD, IN_FRAMES, len(IN_FRAMES),
                             probe=probe)
    meas = probe.finalize()
    assert meas["ii.in_valid"]["worst"] == 1, \
        f"FFT-REQ-005 failed: II worst={meas['ii.in_valid']['worst']}"
    _check(results, EXPECTED, "FFT-REQ-005")
    dut._log.info(
        f"FFT-REQ-005 PASS — II=1, 3 frames intact; latency worst="
        f"{meas['latency.in_valid__out_valid']['worst']} cyc")


@cocotb.test()
@requires("FFT-REQ-008")
async def test_data_pushed_flush_semantics(dut) -> None:
    """One frame then input silence: `M_AXIS_DATA_TVALID` must never
    rise while `S_AXIS_DATA_TVALID` is idle (data-pushed pipeline);
    flush beats then push the frame out bit-exact."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    dut.S_AXIS_CONFIG_TDATA.value = CONFIG_WORD
    dut.S_AXIS_CONFIG_TVALID.value = 1
    await RisingEdge(dut.ACLK)
    dut.S_AXIS_CONFIG_TVALID.value = 0
    for re, im in IN_FRAMES[0]:
        dut.S_AXIS_DATA_TDATA.value = ((im & MASK24) << 24) | (re & MASK24)
        dut.S_AXIS_DATA_TVALID.value = 1
        await RisingEdge(dut.ACLK)
    dut.S_AXIS_DATA_TVALID.value = 0
    for _ in range(3 * N):   # silence >> total pipeline latency
        await RisingEdge(dut.ACLK)
        assert dut.M_AXIS_DATA_TVALID.value != 1, \
            "FFT-REQ-008 failed: output advanced without input push"
    got: list = []
    idx = 0
    for i in range(4 * N):
        dut.S_AXIS_DATA_TDATA.value = 0
        dut.S_AXIS_DATA_TVALID.value = 1
        await RisingEdge(dut.ACLK)
        if dut.M_AXIS_DATA_TVALID.value == 1:
            got.append((
                _to_signed24(int(dut.M_AXIS_DATA_TDATA.value) & MASK24),
                _to_signed24((int(dut.M_AXIS_DATA_TDATA.value) >> 24)
                             & MASK24)))
            idx += 1
            if idx == N:
                break
    dut.S_AXIS_DATA_TVALID.value = 0
    assert idx == N, f"flush did not complete the frame: {idx}/{N}"
    _check([got], [EXPECTED[0]], "FFT-REQ-008")
    dut._log.info("FFT-REQ-008 PASS — data-pushed proven, flush bit-exact")


@cocotb.test()
@requires("FFT-REQ-101")
async def test_reset_no_stale_output(dut) -> None:
    """ARESETN mid-stream: pipeline, reorder RAM and config regs are all
    dirty; inputs driven DURING reset must be ignored; the first post-
    reset output frame is bit-exact — no stale/X beat ever emitted."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)

    # dirty the machine: one full frame + half a frame, then yank reset
    dut.S_AXIS_CONFIG_TDATA.value = CONFIG_WORD
    dut.S_AXIS_CONFIG_TVALID.value = 1
    await RisingEdge(dut.ACLK)
    dut.S_AXIS_CONFIG_TVALID.value = 0
    for re, im in IN_FRAMES[0] + IN_FRAMES[1][: N // 2]:
        dut.S_AXIS_DATA_TDATA.value = ((im & MASK24) << 24) | (re & MASK24)
        dut.S_AXIS_DATA_TVALID.value = 1
        await RisingEdge(dut.ACLK)

    dut.ARESETN.value = 0
    for _ in range(4):  # garbage + tvalid held high THROUGH reset
        dut.S_AXIS_DATA_TDATA.value = 0xA5A5A5A5A5A5
        dut.S_AXIS_DATA_TVALID.value = 1
        await RisingEdge(dut.ACLK)
        assert dut.M_AXIS_DATA_TVALID.value != 1, \
            "FFT-REQ-101 failed: m_axis valid during reset"
    dut.ARESETN.value = 1
    dut.S_AXIS_DATA_TVALID.value = 0
    for _ in range(8):  # quiet after deassert: nothing may drain
        await RisingEdge(dut.ACLK)
        assert dut.M_AXIS_DATA_TVALID.value != 1, \
            "FFT-REQ-101 failed: stale beat after reset deassertion"

    # fresh frame: first output frame must be fully post-reset content
    results = await _session(dut, CONFIG_WORD, [IN_FRAMES[1]], 1)
    _check(results, [EXPECTED[1]], "FFT-REQ-101")
    dut._log.info("FFT-REQ-101 PASS — mid-stream reset, no stale/X beat, "
                  "first post-reset frame bit-exact")


@cocotb.test()
@requires("FFT-REQ-006")
async def test_aclken_freeze_exact(dut) -> None:
    """Randomly gated ACLKEN (~30% off) — output identical to ungated."""
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    await _reset(dut)
    results = await _session(dut, CONFIG_WORD, [IN_FRAMES[0]], 1,
                             gate_seed=0xACC)
    _check(results, [EXPECTED[0]], "FFT-REQ-006")
    dut._log.info("FFT-REQ-006 PASS — gated run bit-identical")


if __name__ == "__main__":
    main()
