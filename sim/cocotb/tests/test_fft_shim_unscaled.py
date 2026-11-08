# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
#
# rr_fft_xfft_shim, unscaled build — FFT-REQ-021: 8-bit config (FWD_INV
# only), 80-bit output tdata with each 33-bit component SIGN-EXTENDED to
# 40 bits (xfft byte-padding convention), values bit-exact to the
# oracle's unscaled inverse (the unscaled-inverse xfft config).

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

import yaml

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

from fft_goldens import LOG2_N, UNSCALED_IN, UNSCALED_INV_EXPECTED  # noqa: E402

_RTL_DIR = str(_Path(__file__).resolve().parents[3] / "rtl")

N = 1 << LOG2_N
MASK24 = (1 << 24) - 1
MASK40 = (1 << 40) - 1


def main() -> None:
    run_simulation(
        top_level="rr_fft_xfft_shim",
        module="test_fft_shim_unscaled",
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


def _to_signed(v: int, w: int) -> int:
    v &= (1 << w) - 1
    return v - (1 << w) if v >= (1 << (w - 1)) else v


@cocotb.test()
@requires("FFT-REQ-021")
async def test_shim_unscaled_padding_bit_exact(dut) -> None:
    fixture = _Path(__file__).resolve().parents[3] / "sim/goldens/FFT-REQ-021.yml"
    minted = yaml.safe_load(fixture.read_text(encoding="utf-8"))["minted"][-N:]
    assert len(minted) == N
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

    dut.S_AXIS_CONFIG_TDATA.value = 0x00   # 8-bit port: inverse
    dut.S_AXIS_CONFIG_TVALID.value = 1
    await RisingEdge(dut.ACLK)
    dut.S_AXIS_CONFIG_TVALID.value = 0

    beats = list(UNSCALED_IN) + [(0, 0)] * (N + 64)
    got: list = []
    idx = 0
    for i in range(len(beats) + 4 * N):
        if i < len(beats):
            re, im = beats[i]
            dut.S_AXIS_DATA_TDATA.value = ((im & MASK24) << 24) | (re & MASK24)
            dut.S_AXIS_DATA_TVALID.value = 1
        else:
            dut.S_AXIS_DATA_TVALID.value = 0
        await RisingEdge(dut.ACLK)
        if i < N:
            probe.mark("in_valid")
        if dut.M_AXIS_DATA_TVALID.value == 1 and idx < N:
            probe.mark("out_valid")
            raw = int(dut.M_AXIS_DATA_TDATA.value)
            assert raw == minted[idx]["golden"]["tdata"], f"FFT-REQ-021 minted packing mismatch at {idx}"
            r40 = raw & MASK40
            q40 = (raw >> 40) & MASK40
            r = _to_signed(r40, 40)
            q = _to_signed(q40, 40)
            # sign-extension check: 40-bit value must equal its own
            # 33-bit truncation interpreted signed (upper bits = sign)
            assert r == _to_signed(r40, 33) and q == _to_signed(q40, 33), (
                f"FFT-REQ-021 failed: bad sign-extension at idx {idx}"
            )
            assert int(dut.M_AXIS_DATA_TUSER.value) == idx
            got.append((r, q))
            idx += 1
            if idx == N:
                break
    probe.finalize()
    assert len(got) == N, f"timeout: {len(got)}/{N}"
    mism = [k for k, (g, e) in enumerate(zip(got, UNSCALED_INV_EXPECTED))
            if g != tuple(e)]
    assert not mism, f"FFT-REQ-021 failed: value mismatch at k={mism[0]}"
    dut._log.info("FFT-REQ-021 PASS — 80-bit padded output bit-exact, "
                  "sign-extension verified")


if __name__ == "__main__":
    main()
