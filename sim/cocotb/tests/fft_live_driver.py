# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# Shared stimulus and checks for the reference-deployment configuration
# tests (test_fft_shim_live_scaled.py, test_fft_shim_live_unscaled.py).
#
# The stream is driven the way the reference deployment drives its xfft
# instances: S_AXIS_CONFIG_TVALID held at 1 with a constant word from the
# release of ARESETN, ACLKEN at 1, input frames back to back with no idle
# cycle, S_AXIS_DATA_TLAST on the last beat of each frame. Zero beats follow
# the last frame to flush it (FFT-REQ-008, data-pushed core).

from __future__ import annotations

import json
import math
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from sdk.cocotb_helpers.latency_probe import LatencyProbe

MASK24 = (1 << 24) - 1
EVENTS = ("EVENT_FRAME_STARTED", "EVENT_TLAST_UNEXPECTED",
          "EVENT_TLAST_MISSING", "EVENT_DATA_IN_CHANNEL_HALT")


def to_signed(v: int, w: int) -> int:
    v &= (1 << w) - 1
    return v - (1 << w) if v >= (1 << (w - 1)) else v


def unpack(raw: int, comp_w: int, value_w: int) -> tuple[int, int]:
    """Split packed tdata (imaginary high, real low) of comp_w-bit fields.

    Asserts that each field is the sign extension of a value_w-bit result
    (identity when comp_w == value_w).
    """
    mask = (1 << comp_w) - 1
    r = to_signed(raw & mask, comp_w)
    q = to_signed((raw >> comp_w) & mask, comp_w)
    assert r == to_signed(r, value_w) and q == to_signed(q, value_w), (
        f"output field is not a sign-extended {value_w}-bit value: {raw:#x}")
    return r, q


async def run_stream(dut, config_word: int, frames: list, comp_w: int,
                     value_w: int, probe: LatencyProbe | None = None) -> list:
    """Drive every frame back to back; return the output frames.

    Checks on every output beat: M_AXIS_DATA_TUSER equals the natural-order
    bin index (xk_index), M_AXIS_DATA_TLAST is 1 on bin N-1 only, the xfft
    event outputs stay 0.
    """
    n = len(frames[0])
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    dut.ARESETN.value = 0
    dut.ACLKEN.value = 1
    dut.S_AXIS_CONFIG_TDATA.value = config_word
    dut.S_AXIS_CONFIG_TVALID.value = 1
    dut.S_AXIS_DATA_TVALID.value = 0
    dut.S_AXIS_DATA_TDATA.value = 0
    dut.S_AXIS_DATA_TLAST.value = 0
    await ClockCycles(dut.ACLK, 5)
    dut.ARESETN.value = 1
    await ClockCycles(dut.ACLK, 2)

    beats = [s for f in frames for s in f]
    n_in = len(beats)
    beats += [(0, 0)] * (n + 64)
    want = len(frames) * n
    got: list = []
    for i in range(len(beats) + 4 * n):
        drive = i < len(beats)
        if drive:
            re, im = beats[i]
            dut.S_AXIS_DATA_TDATA.value = ((im & MASK24) << 24) | (re & MASK24)
            dut.S_AXIS_DATA_TVALID.value = 1
            dut.S_AXIS_DATA_TLAST.value = 1 if (i % n) == n - 1 else 0
        else:
            dut.S_AXIS_DATA_TVALID.value = 0
            dut.S_AXIS_DATA_TLAST.value = 0
        await RisingEdge(dut.ACLK)
        if probe is not None and drive and i < n_in:
            probe.mark("in_valid")
        for ev in EVENTS:
            assert int(getattr(dut, ev).value) == 0, f"{ev} asserted"
        assert int(dut.S_AXIS_DATA_TREADY.value) == 1, "S_AXIS_DATA_TREADY low"
        if dut.M_AXIS_DATA_TVALID.value == 1 and len(got) < want:
            if probe is not None:
                probe.mark("out_valid")
            k = len(got) % n
            assert int(dut.M_AXIS_DATA_TUSER.value) == k, (
                f"xk_index {int(dut.M_AXIS_DATA_TUSER.value)} at bin {k}")
            assert int(dut.M_AXIS_DATA_TLAST.value) == (1 if k == n - 1 else 0), (
                f"M_AXIS_DATA_TLAST wrong at bin {k}")
            got.append(unpack(int(dut.M_AXIS_DATA_TDATA.value), comp_w, value_w))
            if len(got) == want:
                break
    if probe is not None:
        probe.finalize()
    assert len(got) == want, f"timeout: {len(got)}/{want} output beats"
    return [got[f * n:(f + 1) * n] for f in range(len(frames))]


def first_mismatch(got: list, expected: list) -> tuple[int, int] | None:
    for f, (gf, ef) in enumerate(zip(got, expected)):
        for k, (g, e) in enumerate(zip(gf, ef)):
            if tuple(g) != tuple(e):
                return f, k
    return None


def compare_to_xfft(got: list, xfft: list) -> list[dict]:
    """Per-frame error of the RTL output against the xfft C model.

    err = rtl - xfft per component, in output LSB. SNR = 10 log10 of xfft
    output power over error power, both components.
    """
    rows = []
    for gf, xf in zip(got, xfft):
        d = [g[0] - x[0] for g, x in zip(gf, xf)] + \
            [g[1] - x[1] for g, x in zip(gf, xf)]
        ref = [x[0] for x in xf] + [x[1] for x in xf]
        ps = sum(v * v for v in ref)
        pe = sum(v * v for v in d)
        snr = math.inf if pe == 0 else (10 * math.log10(ps / pe) if ps else -math.inf)
        hist: dict[int, int] = {}
        for v in d:
            hist[v] = hist.get(v, 0) + 1
        rows.append({
            "max_lsb": max(abs(v) for v in d),
            "snr_db": snr,
            "nonzero": sum(1 for v in d if v),
            "mean_err": sum(d) / len(d),
            "histogram": dict(sorted(hist.items())),
            "err_re": [g[0] - x[0] for g, x in zip(gf, xf)],
            "err_im": [g[1] - x[1] for g, x in zip(gf, xf)],
        })
    return rows


def write_report(module: str, config_word: int, names: list, rows: list) -> Path:
    """Per-sample error record beside the run's latest.json."""
    out = Path(__file__).resolve().parents[2] / "results" / module / "xfft_compare.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "config_word": f"{config_word:#x}",
        "reference": "Xilinx xfft v9.1 bit-accurate C model",
        "frames": [
            {"name": nm, **{k: (str(v) if isinstance(v, float) and not math.isfinite(v)
                               else v) for k, v in r.items()}}
            for nm, r in zip(names, rows)
        ],
    }
    out.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
    return out


def log_table(log, names: list, rows: list) -> None:
    log.info("%-24s %8s %10s %8s %9s", "frame", "max LSB", "SNR dB",
             "nonzero", "mean err")
    for nm, r in zip(names, rows):
        log.info("%-24s %8d %10.1f %8d %9.3f", nm, r["max_lsb"], r["snr_db"],
                 r["nonzero"], r["mean_err"])
