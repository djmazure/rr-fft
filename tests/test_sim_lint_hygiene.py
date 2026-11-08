# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""Regression guards for FFT-P2.10, asserted at the CLI surface a user runs.

(1) `rr linting` must load the profile project.yml declares, and the RTL must
    carry no gating finding under the strict RouteRTL VHDL tier (the ESA
    tier until FFT-P2.13). The scaffold declared
    'esa-strict', a name rr never shipped, so the lint gate always exited 1
    with "Profile 'esa-strict' could not be loaded".
(2) `rr sim run test_fft_core` must emit no NUMERIC_STD.TO_INTEGER warning.
    Each warning class is guarded separately because each has its own cause
    in rr_fft_sdf_pair (names as of FFT-P2.13: p_rom/p_out, s2_n/s2_m before):
      * pr_rom, null detected: s2N_r was a null slice in the G_L = 4 pair;
      * pr_rom, metavalue:     s2N_r / s2M_r unreset between reset release and
                              the pair's first valid beat;
      * pr_out, metavalue:     the scale shift decoded from the cfg sideband,
                              which is 'U' on non-valid beats before the
                              first frame arrives.

These tests shell out to `rr`; they FAIL (never skip) when rr is missing,
so a lane without the SDK cannot report them green. rr writes run state into
the project (sim/results journals, artifacts/, lint status), several of them
tracked files, so every rr call runs in a scratch copy of the tree and the
checkout under test is never modified.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CORE_LOG = "sim/cocotb/logs/test_fft_core/test_fft_core.log"
STRICT_TIER = "routertl-vhdl-strict-provisional"
N_CORE_TESTCASES = 8

_WARN_RE = re.compile(
    r"^\*\* Warning: (?P<time>\S+): NUMERIC_STD\.TO_INTEGER: "
    r"(?P<kind>metavalue|null) detected")
_PROC_RE = re.compile(r"^\s+Process (?P<proc>\S+) at ")


# run-state and VCS paths not copied into the scratch project
_SCRATCH_SKIP = shutil.ignore_patterns(
    ".git", ".dispatch*", "artifacts", "work", "logs", "waves",
    "__pycache__", "*.lock")


@pytest.fixture(scope="module")
def scratch(tmp_path_factory) -> Path:
    """A private copy of the project tree for rr to run in."""
    dest = tmp_path_factory.mktemp("rr_fft") / "rr-fft"
    shutil.copytree(ROOT, dest, ignore=_SCRATCH_SKIP)
    return dest


def _rr(cwd: Path, *args: str,
        timeout: int = 900) -> subprocess.CompletedProcess:
    rr = shutil.which("rr")
    assert rr, "routertl `rr` CLI not on PATH; these guards cannot run"
    return subprocess.run(
        [rr, *args], cwd=cwd, capture_output=True, text=True,
        timeout=timeout)


# ── (2) simulator output ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def core_warnings(scratch) -> list[tuple[str, str, str]]:
    """Run the core suite once; return (kind, time, process) per warning."""
    log = scratch / CORE_LOG
    log.unlink(missing_ok=True)   # never read a previous run's log
    res = _rr(scratch, "sim", "run", "test_fft_core", "--wait",
              timeout=1500)
    assert res.returncode == 0, (
        f"rr sim run test_fft_core exited {res.returncode}:\n"
        f"{res.stdout[-3000:]}\n{res.stderr[-2000:]}")
    lines = log.read_text(errors="replace").splitlines()
    ran = sum(1 for ln in lines if "running test_fft_core." in ln)
    assert ran == N_CORE_TESTCASES, (
        f"sim log shows {ran}/{N_CORE_TESTCASES} testcases run; "
        "a warning count from a partial run proves nothing")
    found = []
    for i, ln in enumerate(lines):
        m = _WARN_RE.match(ln)
        if not m:
            continue
        proc = "?"
        for nxt in lines[i + 1:i + 4]:
            pm = _PROC_RE.match(nxt)
            if pm:
                proc = pm.group("proc")
                break
        found.append((m.group("kind"), m.group("time"), proc))
    return found


def _summarise(hits: list[tuple[str, str, str]]) -> str:
    procs = sorted({p for _, _, p in hits})
    return (f"{len(hits)} warnings, first at {hits[0][1]} in {hits[0][2]}; "
            f"processes: {procs}") if hits else ""


def test_twiddle_rom_address_never_null_range(core_warnings) -> None:
    hits = [w for w in core_warnings
            if w[0] == "null" and w[2].endswith(":pr_rom")]
    assert not hits, (
        "FFT-P2.10 null-range twiddle index: to_integer() of a null "
        "vector in pr_rom — " + _summarise(hits))


def test_twiddle_rom_address_defined_after_reset(core_warnings) -> None:
    hits = [w for w in core_warnings
            if w[0] == "metavalue" and w[2].endswith(":pr_rom")]
    assert not hits, (
        "FFT-P2.10 twiddle ROM address metavalue after reset release in "
        "pr_rom — " + _summarise(hits))


def test_scale_shift_defined_after_reset(core_warnings) -> None:
    hits = [w for w in core_warnings
            if w[0] == "metavalue" and w[2].endswith(":pr_out")]
    assert not hits, (
        "FFT-P2.10 scale-shift metavalue after reset release in pr_out — "
        + _summarise(hits))


def test_core_sim_emits_no_numeric_std_warning(core_warnings) -> None:
    """Catch-all: a NEW to_integer site leaking 'U'/'X' fails here too."""
    assert not core_warnings, (
        "FFT-P2.10 NUMERIC_STD warnings in test_fft_core — "
        + _summarise(core_warnings))


# ── (1) lint profile ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rr_linting(scratch) -> subprocess.CompletedProcess:
    return _rr(scratch, "linting")


def _declared_profile() -> str:
    cfg = yaml.safe_load((ROOT / "project.yml").read_text())
    return cfg["linting"]["profile"]


def test_declared_lint_profile_loads(rr_linting) -> None:
    out = rr_linting.stdout + rr_linting.stderr
    declared = _declared_profile()
    assert "could not be loaded" not in out, (
        f"FFT-P2.10 declared lint profile '{declared}' could not be loaded "
        f"by rr linting:\n{out[-1500:]}")
    assert f"Linting with profile {declared}" in out, (
        f"rr linting never ran the declared profile '{declared}':\n"
        f"{out[-1500:]}")


def test_rtl_has_no_gating_finding_under_strict_tier(scratch) -> None:
    res = _rr(scratch, "lint", "--profile", STRICT_TIER)
    out = res.stdout + res.stderr
    errors = [ln.strip() for ln in out.splitlines()
              if ln.lstrip().startswith("error ")]
    assert res.returncode == 0 and not errors, (
        f"FFT-P2.10 strict-tier gating findings (exit {res.returncode}): "
        + "; ".join(errors[:10]))


def test_rr_linting_exits_clean(rr_linting) -> None:
    """The ticket's surface: `rr linting` with the declared profile."""
    assert rr_linting.returncode == 0, (
        f"rr linting exited {rr_linting.returncode}:\n"
        f"{(rr_linting.stdout + rr_linting.stderr)[-2000:]}")
