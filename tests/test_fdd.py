# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""Regression guard for FFT-P2.11, asserted at the CLI surface a user runs.

`rr fdd check` is the documentation gate `rr pkg publish` applies: the FDD
must exist (missing-fdd), carry engineer-authored prose in every required
section (placeholder-section / empty-section), and its machine-owned block
must match the declarative inputs (stale-digest, and with --full, drift).
The check runs strict and full, so a requirements.yml or ip.yml edit that is
not followed by `rr fdd generate` turns this red.

The test shells out to `rr`; it FAILS (never skips) when rr is missing, so a
lane without the SDK cannot report it green. The check runs in a scratch copy
of the tree so the checkout under test is never modified.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SCRATCH_SKIP = shutil.ignore_patterns(
    ".git", ".dispatch*", "artifacts", "work", "logs", "waves",
    "__pycache__", "*.lock")


def test_fdd_check_strict_full_is_clean(tmp_path) -> None:
    rr = shutil.which("rr")
    assert rr, "routertl `rr` CLI not on PATH; this guard cannot run"
    scratch = tmp_path / "rr-fft"
    shutil.copytree(ROOT, scratch, ignore=_SCRATCH_SKIP)
    res = subprocess.run(
        [rr, "fdd", "check", "--strict", "--full", "--json"],
        cwd=scratch, capture_output=True, text=True, timeout=300)
    report = json.loads(res.stdout)
    kinds = sorted(f["kind"] for f in report["findings"])
    assert not report["findings"], (
        f"FDD gate findings {kinds}:\n"
        + "\n".join(f"{f['kind']}: {f['detail']}"
                    for f in report["findings"]))
    assert report["checked"] == 1, report
    assert res.returncode == 0, (
        f"rr fdd check exited {res.returncode}:\n{res.stdout}{res.stderr}")
