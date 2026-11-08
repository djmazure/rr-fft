# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""Regression guard for FFT-P2.12, asserted at the `rr sim run` surface.

Field measurement 2026-11-07: an rr_fft core costs about 150 logic LUT more
than the xfft cell it replaces, because the first pair's twiddle ROM was
forced into LUTs (rom_style distributed) with no way to choose otherwise. A
design with block RAM to spare and short of logic LUT could not trade one
for the other. The top generic G_TWIDDLE_ROM_STYLE ("distributed" default,
"block") now selects it, on rr_fft_top and on rr_fft_xfft_shim.

These guards check what a consumer of the generic gets:

  * the block style is elaborated and changes nothing observable: the
    dedicated suite (FFT-REQ-040) is bit-exact in both directions and has
    the default build's latency;
  * the style reaches the core through the xfft shim: the shim built with
    "block" carries the block ROM and its deployment stream is bit-exact;
  * the existing regression passes in the block style, on the core and on
    the shim, scaled and unscaled (`rr sim run -g G_TWIDDLE_ROM_STYLE=block`);
  * the generic reaches the elaborated design through both tops: an unknown
    style fails elaboration instead of silently building the LUT ROM.

The resource side of the trade (one more block RAM, fewer logic LUT, same
timing) is guarded by tests/test_routed_timing_evidence.py on the routed
record of the twiddle_block target.

The tests shell out to `rr`; they FAIL (never skip) when rr is missing. rr
writes run state into the project, so every call runs in a scratch copy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERIC = "G_TWIDDLE_ROM_STYLE"

_SCRATCH_SKIP = shutil.ignore_patterns(
    ".git", ".dispatch*", "artifacts", "work", "logs", "waves",
    "__pycache__", "*.lock")

# module -> testcases it holds (a pass count from a partial run proves nothing)
BLOCK_REGRESSION = {
    "test_fft_core": 8,
    "test_fft_unscaled": 1,
    "test_fft_shim_live_scaled": 2,
    "test_fft_shim_live_unscaled": 2,
}


@pytest.fixture(scope="module")
def scratch(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("rr_fft") / "rr-fft"
    shutil.copytree(ROOT, dest, ignore=_SCRATCH_SKIP)
    return dest


def _sim(cwd: Path, module: str, *generics: str) -> tuple[subprocess.CompletedProcess, dict]:
    rr = shutil.which("rr")
    assert rr, "routertl `rr` CLI not on PATH; these guards cannot run"
    result = cwd / "sim" / "results" / module / "latest.json"
    result.unlink(missing_ok=True)   # never read a previous run's result
    args = [rr, "sim", "run", module, "--wait"]
    for g in generics:
        args += ["-g", g]
    res = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                         timeout=1500)
    assert result.is_file(), (
        f"rr sim run {module} wrote no result (rc {res.returncode}):\n"
        f"{res.stdout[-2000:]}\n{res.stderr[-1500:]}")
    return res, json.loads(result.read_text())


def _log(cwd: Path, module: str) -> str:
    log = cwd / "sim" / "cocotb" / "logs" / module / f"{module}.log"
    return log.read_text(errors="replace") if log.is_file() else ""


def _failures(cwd: Path, module: str) -> str:
    return "\n".join(ln.strip() for ln in _log(cwd, module).splitlines()
                     if "Error" in ln or "failed" in ln)[-2500:]


def test_block_style_suite_bit_exact_same_latency(scratch) -> None:
    """FFT-REQ-040 suite: the block ROM is elaborated, both directions are
    bit-exact and the latency equals the default build's."""
    res, doc = _sim(scratch, "test_fft_twiddle_block")
    assert doc["total"] == 2 and doc["passed"] == 2 and res.returncode == 0, (
        f"FFT-REQ-040 block-ROM suite: {doc['passed']}/{doc['total']} passed "
        f"(rc {res.returncode}):\n{_failures(scratch, 'test_fft_twiddle_block')}")


def test_shim_forwards_block_style_to_core(scratch) -> None:
    """FFT-REQ-040 through rr_fft_xfft_shim: the shim's generic reaches the
    core (block ROM elaborated) and the 0xB1 stream is bit-exact."""
    res, doc = _sim(scratch, "test_fft_shim_twiddle_block")
    assert doc["total"] == 1 and doc["passed"] == 1 and res.returncode == 0, (
        f"FFT-REQ-040 shim block-ROM test: {doc['passed']}/{doc['total']} "
        f"passed (rc {res.returncode}):\n"
        f"{_failures(scratch, 'test_fft_shim_twiddle_block')}")


@pytest.mark.parametrize("module", sorted(BLOCK_REGRESSION))
def test_existing_regression_passes_in_block_style(scratch, module) -> None:
    res, doc = _sim(scratch, module, f"{GENERIC}=block")
    want = BLOCK_REGRESSION[module]
    assert doc["total"] == want and doc["passed"] == want \
        and res.returncode == 0, (
            f"{module} with {GENERIC}=block: {doc['passed']}/{doc['total']} "
            f"passed, expected {want} (rc {res.returncode}):\n"
            f"{_failures(scratch, module)}")


@pytest.mark.parametrize("module", ["test_fft_core", "test_fft_shim_scaled"])
def test_unknown_style_fails_elaboration(scratch, module) -> None:
    """The generic reaches rr_fft_top through each top: a value that is
    neither style is refused, never built as the LUT ROM."""
    res, doc = _sim(scratch, module, f"{GENERIC}=blockram")
    log = _log(scratch, module)
    assert "G_TWIDDLE_ROM_STYLE must be distributed or block, got blockram" \
        in log, (
            f"{module} with {GENERIC}=blockram did not fail elaboration "
            f"({doc['passed']}/{doc['total']} passed, rc {res.returncode}); "
            "the generic does not reach rr_fft_top's style check")
    assert doc["passed"] == 0 and res.returncode != 0, doc
