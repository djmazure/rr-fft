# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""Regression guard for FFT-P2.1: the 375 MHz hard contract has routed
evidence for the RTL of this revision.

The epic's hard technical contract is fmax 375 MHz on xczu5cg-sfvc784-2-i.
Up to v0.2.0 the only routed result was the FFT-P2.6 closure on older RTL;
the FDD listed timing for the current RTL as not confirmed. The routed
out-of-context results are now recorded in docs/timing/routed_ooc.json, one
entry per build target. This guard keeps that record honest:

  * every build target (project.yml and targets/*.yml) has a routed entry;
  * each entry is routed, out of context, at a clock period no longer than
    the contract fmax (requirements.yml technical.clocks) and equal to the
    create_clock in constraints/rr_fft.xdc, with WNS and WHS >= 0, no
    unclocked register, no unconstrained internal endpoint and no routing
    error;
  * each entry names the generics its build file sets;
  * the RTL file hashes of each entry equal the files in rtl/ now. Any RTL
    edit turns this red until the builds are re-run and the record updated,
    so the record can never describe RTL that is no longer there.

FFT-P2.12 adds the twiddle_block target (G_TWIDDLE_ROM_STYLE = "block") and
asserts the trade it exists for, on the routed numbers: the block build uses
one block RAM tile more than the scaled default, at least MIN_LUT_SAVED fewer
logic LUT, the same DSP count, at the same clock.

The test reads files only; it needs neither rr nor Vivado.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "timing" / "routed_ooc.json"
CLOCK = "aclk"
BUILDS = ["scaled", "unscaled", "twiddle_block"]
# generics a build file does not set, as rtl/rr_fft_top.vhd defaults them
DEFAULT_GENERICS = {"G_HAS_SCALING": True,
                    "G_TWIDDLE_ROM_STYLE": "distributed"}
# FFT-P2.12: the field shortfall was about 150 logic LUT per core
MIN_LUT_SAVED = 100


def _evidence() -> dict:
    assert EVIDENCE.is_file(), (
        f"no routed timing evidence: {EVIDENCE.relative_to(ROOT)} is missing")
    return json.loads(EVIDENCE.read_text())


def _builds() -> dict[str, dict]:
    builds = _evidence().get("builds") or {}
    assert builds, "no routed timing evidence: the record lists no build"
    return builds


def _contract_period_ns() -> float:
    req = yaml.safe_load((ROOT / "requirements.yml").read_text())
    clocks = {c["name"]: c for c in req["technical"]["clocks"]}
    return 1000.0 / float(clocks[CLOCK]["fmax_mhz"])


def _xdc_period_ns() -> float:
    xdc = (ROOT / "constraints" / "rr_fft.xdc").read_text()
    m = re.search(
        r"create_clock\s+-period\s+([0-9.]+)\s+-name\s+" + CLOCK, xdc)
    assert m, f"no create_clock for {CLOCK} in constraints/rr_fft.xdc"
    return float(m.group(1))


def _build_files() -> list[str]:
    return ["project.yml"] + sorted(
        str(p.relative_to(ROOT)) for p in (ROOT / "targets").glob("*.yml"))


def test_every_build_target_has_routed_evidence() -> None:
    recorded = sorted(b["build_file"] for b in _builds().values())
    assert recorded == sorted(_build_files()), (
        f"no routed timing evidence for some build target: "
        f"recorded {recorded}, targets {_build_files()}")


@pytest.mark.parametrize("name", BUILDS)
def test_routed_timing_meets_contract_fmax(name: str) -> None:
    b = _builds().get(name)
    assert b, f"no routed timing evidence for the {name} build"
    contract = _contract_period_ns()
    assert b["clock"] == CLOCK
    assert b["design_state"] == "routed", b["design_state"]
    assert b["synth_mode"] == "out_of_context", b["synth_mode"]
    assert b["period_ns"] == pytest.approx(_xdc_period_ns()), (
        "routed period differs from constraints/rr_fft.xdc")
    assert b["period_ns"] <= contract + 1e-3, (
        f"{name}: routed at {b['period_ns']} ns, longer than the contract "
        f"period {contract:.4f} ns")
    assert b["wns_ns"] >= 0.0, f"{name}: setup violated, WNS {b['wns_ns']} ns"
    assert b["whs_ns"] >= 0.0, f"{name}: hold violated, WHS {b['whs_ns']} ns"
    assert b["tns_ns"] == 0.0 and b["ths_ns"] == 0.0
    assert b["fmax_mhz"] >= 1000.0 / contract - 1e-6
    assert b["no_clock_pins"] == 0
    assert b["unconstrained_internal_endpoints"] == 0, (
        f"{name}: unconstrained internal endpoints, the WNS does not cover "
        "every path")
    assert b["nets_with_routing_errors"] == 0


@pytest.mark.parametrize("name", BUILDS)
def test_routed_evidence_generics_match_build_file(name: str) -> None:
    b = _builds().get(name)
    assert b, f"no routed timing evidence for the {name} build"
    cfg = yaml.safe_load((ROOT / b["build_file"]).read_text()) or {}
    declared = (cfg.get("build_options") or {}).get("generics") or {}
    effective = {**DEFAULT_GENERICS, **declared}
    assert effective == b["generics"], (
        f"{name}: record generics {b['generics']} but {b['build_file']} "
        f"builds {effective}")


@pytest.mark.parametrize("name", BUILDS)
def test_routed_evidence_matches_current_rtl(name: str) -> None:
    b = _builds().get(name)
    assert b, f"no routed timing evidence for the {name} build"
    excluded = set(_evidence().get("rtl_not_in_build") or {})
    on_disk = {str(p.relative_to(ROOT)) for p in (ROOT / "rtl").glob("*.vhd")}
    hashed = set(b["rtl_sha256"])
    assert hashed == on_disk - excluded, (
        f"{name}: record hashes {sorted(hashed)}, rtl/ holds "
        f"{sorted(on_disk - excluded)} outside the excluded set")
    stale = sorted(
        f for f, h in b["rtl_sha256"].items()
        if hashlib.sha256((ROOT / f).read_bytes()).hexdigest() != h)
    assert not stale, (
        f"routed timing evidence is stale for the {name} build: {stale} "
        "changed since the routed run; re-run synth and impl --ooc and "
        "update docs/timing/routed_ooc.json")


def test_block_twiddle_rom_trades_logic_lut_for_one_bram() -> None:
    """FFT-P2.12: the twiddle_block build moves the first pair's twiddle
    ROM into block RAM. Routed, it must hold exactly one block RAM tile more
    than the scaled default and at least MIN_LUT_SAVED fewer logic LUT, with
    the same DSP count and the contract clock met."""
    builds = _builds()
    base, blk = builds.get("scaled"), builds.get("twiddle_block")
    assert blk, ("no routed evidence for the twiddle_block build "
                 "(G_TWIDDLE_ROM_STYLE = block)")
    assert base, "no routed evidence for the scaled build"
    assert blk["generics"] == {**DEFAULT_GENERICS,
                               "G_TWIDDLE_ROM_STYLE": "block"}, blk["generics"]
    assert base["generics"] == DEFAULT_GENERICS, base["generics"]
    ub, uk = base["utilization"], blk["utilization"]
    assert uk["bram_tile"] == ub["bram_tile"] + 1, (
        f"twiddle_block holds {uk['bram_tile']} block RAM tiles, the scaled "
        f"default {ub['bram_tile']}: the twiddle ROM is not in one block RAM")
    saved = ub["lut_as_logic"] - uk["lut_as_logic"]
    assert saved >= MIN_LUT_SAVED, (
        f"twiddle_block saves {saved} logic LUT over the scaled default "
        f"({uk['lut_as_logic']} vs {ub['lut_as_logic']}), "
        f"less than {MIN_LUT_SAVED}")
    assert uk["dsp"] == ub["dsp"], (uk["dsp"], ub["dsp"])
    assert blk["period_ns"] == base["period_ns"]
    assert blk["wns_ns"] >= 0.0 and blk["whs_ns"] >= 0.0, (
        f"twiddle_block timing not met: WNS {blk['wns_ns']}, "
        f"WHS {blk['whs_ns']}")
