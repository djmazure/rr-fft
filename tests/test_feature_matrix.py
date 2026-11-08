# SPDX-FileCopyrightText: 2026 Daniel J. Mazure
# SPDX-License-Identifier: MIT
"""Regression guard for FFT-P2.9, asserted at the rr CLI surface.

`rr contract report` reported "feature matrix: absent" and, once the pytest
guards in tests/ existed, every REQ without a live test: coverage-map took
tests/ as the cocotb test root. The feature_matrix block in requirements.yml
and the test_dir pointer in ip.yml fix both.

The expected arm table below is hard-coded. A supported arm that loses its
REQ binding or its live test, a planned (FFT-P4.1) or oracle-only arm that
gets bound to a REQ, or an arm that disappears turns this red. The negative
control removes one arm's witness (the @requires tag of the clock-enable
freeze test) and checks that both commands report the arm UNVERIFIED.

The tests shell out to `rr`; they FAIL (never skip) when rr is missing. Each
runs in a scratch copy of the tree, because `rr contract report` writes
sim/results/contract_witnesses.json.
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

# (axis, arm) -> mapped REQs. Every arm here must be PROVEN.
PROVEN = {
    ("build_variant", "scaled (G_HAS_SCALING=true, 24-bit out)"): ["FFT-REQ-001"],
    ("build_variant", "unscaled (G_HAS_SCALING=false, 33-bit out)"): ["FFT-REQ-002"],
    ("twiddle_rom_style", "distributed (default, twiddle ROMs in LUTs)"):
        ["FFT-REQ-001", "FFT-REQ-007"],
    ("twiddle_rom_style", "block (first pair's twiddle ROM in block RAM)"):
        ["FFT-REQ-040"],
    ("direction", "forward / scaled"): ["FFT-REQ-001"],
    ("direction", "inverse / scaled"): ["FFT-REQ-007", "FFT-REQ-022"],
    ("direction", "forward / unscaled"): ["FFT-REQ-002", "FFT-REQ-023"],
    ("direction", "inverse / unscaled"): ["FFT-REQ-002", "FFT-REQ-021"],
    ("scale_schedule", "[2,2,2,2] (0x155), full frames"): ["FFT-REQ-001"],
    ("scale_schedule", "[0,0,0,0] (0x000), inverse full frame"): ["FFT-REQ-007"],
    ("scale_schedule", "[3,2,2,2] (0x156), full frames"): ["FFT-REQ-022"],
    ("scale_schedule", "[3,2,2,2] forward (0x157), full frame"): ["FFT-REQ-004"],
    ("scale_schedule", "every legal schedule, DC frames"): ["FFT-REQ-030"],
    ("scale_schedule", "schedule change at frame boundary"): ["FFT-REQ-004"],
    ("scale_schedule", "schedule bits ignored in the unscaled build (0xB1)"):
        ["FFT-REQ-023"],
    ("interface", "core rr_fft_top"): ["FFT-REQ-001", "FFT-REQ-003"],
    ("interface", "shim scaled (16-bit config, 48-bit data)"):
        ["FFT-REQ-020", "FFT-REQ-022"],
    ("interface", "shim unscaled (8-bit config, 80-bit data)"):
        ["FFT-REQ-021", "FFT-REQ-023"],
    ("interface", "shim xfft equivalence, config 0x156"): ["FFT-REQ-024"],
    ("interface", "shim xfft equivalence, config 0xB1"): ["FFT-REQ-025"],
    ("frame_configuration", "256 points, 24-bit data, 24-bit twiddle"):
        ["FFT-REQ-001", "FFT-REQ-002"],
    ("frame_configuration", "natural output order with xk_index and tlast"):
        ["FFT-REQ-003"],
    ("frame_configuration", "truncation rounding"): ["FFT-REQ-001", "FFT-REQ-002"],
    ("stream_control", "back-to-back frames at II = 1 / scaled core"): ["FFT-REQ-005"],
    ("stream_control", "clock-enable freeze / scaled core"): ["FFT-REQ-006"],
    ("stream_control", "reset mid-stream, no stale output / scaled core"):
        ["FFT-REQ-101"],
    ("stream_control", "data-pushed flush / scaled core"): ["FFT-REQ-008"],
    ("stream_control", "back-to-back frames / unscaled build"):
        ["FFT-REQ-002", "FFT-REQ-023"],
}

# Declared gaps: not implemented in RTL, oracle-only, or not tested. Each must
# stay UNEXERCISED with no REQ binding.
UNEXERCISED = {
    ("scale_schedule", "every legal schedule, non-DC frames"),
    ("interface", "shim event outputs"),
    ("interface", "multichannel"),
    ("frame_configuration", "other transform lengths (generic LOG2_N)"),
    ("frame_configuration", "runtime transform length (xfft NFFT)"),
    ("frame_configuration", "other data or twiddle widths"),
    ("frame_configuration", "bit-reversed output order"),
    ("frame_configuration", "convergent rounding"),
    ("frame_configuration", "block floating point"),
    ("stream_control", "clock-enable freeze / unscaled build"),
    ("stream_control", "reset mid-stream / unscaled build"),
    ("stream_control", "data-pushed silence / unscaled build"),
    ("stream_control", "backpressure (m_axis_data_tready)"),
    ("stream_control", "self-flush on aclk"),
}

# Planned features and oracle-only configurations: never implemented RTL.
PLANNED = {
    ("interface", "multichannel"),
    ("frame_configuration", "other transform lengths (generic LOG2_N)"),
    ("frame_configuration", "runtime transform length (xfft NFFT)"),
    ("frame_configuration", "other data or twiddle widths"),
    ("frame_configuration", "bit-reversed output order"),
    ("frame_configuration", "convergent rounding"),
    ("frame_configuration", "block floating point"),
}

WITNESS_ARM = ("stream_control", "clock-enable freeze / scaled core")
WITNESS_TAG = '@requires("FFT-REQ-006")\n'
WITNESS_FILE = Path("sim/cocotb/tests/test_fft_core.py")


def _rr() -> str:
    rr = shutil.which("rr")
    assert rr, "routertl `rr` CLI not on PATH; this guard cannot run"
    return rr


def _scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / "rr-fft"
    shutil.copytree(ROOT, scratch, ignore=_SCRATCH_SKIP)
    return scratch


def _run_json(cwd: Path, *args: str) -> tuple[int, dict]:
    res = subprocess.run(
        [_rr(), *args, "--json"], cwd=cwd, capture_output=True, text=True,
        timeout=600)
    try:
        doc = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"rr {' '.join(args)} --json emitted no JSON (rc {res.returncode}):"
            f"\n{res.stdout[-2000:]}{res.stderr[-2000:]}") from exc
    return res.returncode, doc["data"]


def _report_arms(data: dict) -> dict:
    return {(axis["axis"], arm["arm"]): arm
            for axis in data["feature_matrix"] for arm in axis["arms"]}


def _coverage_arms(data: dict) -> tuple[dict, dict]:
    report = data["project_report"]
    return report, {(arm["axis"], arm["arm"]): arm
                    for arm in report["feature_matrix"]["arms"]}


def test_contract_report_renders_the_feature_matrix(tmp_path) -> None:
    _, data = _run_json(_scratch(tmp_path), "contract", "report")
    assert data["matrix_present"], "feature matrix absent from rr contract report"
    arms = _report_arms(data)
    assert set(arms) == set(PROVEN) | UNEXERCISED, (
        f"arm set drifted: missing {sorted(set(PROVEN) | UNEXERCISED - set(arms))}"
        f", unexpected {sorted(set(arms) - set(PROVEN) - UNEXERCISED)}")
    rebound = {key: arms[key]["reqs"] for key, reqs in PROVEN.items()
               if arms[key]["reqs"] != reqs}
    assert not rebound, f"supported arms bound to other REQs: {rebound}"
    gaps = {key: arms[key]["state"] for key in UNEXERCISED
            if arms[key]["state"] != "UNEXERCISED" or arms[key]["reqs"]}
    assert not gaps, f"declared gaps not UNEXERCISED: {gaps}"
    assert data["summary"]["requirements_unreferenced_by_matrix"] == [], \
        data["summary"]


def test_planned_and_oracle_only_arms_are_not_advertised(tmp_path) -> None:
    _, data = _run_json(_scratch(tmp_path), "contract", "report")
    arms = _report_arms(data)
    advertised = {key: arms.get(key) for key in PLANNED
                  if key not in arms or arms[key]["reqs"]
                  or arms[key]["state"] != "UNEXERCISED"}
    assert not advertised, f"planned arms advertised as implemented: {advertised}"
    for key in PLANNED:
        assert "FFT-P4.1" in arms[key]["declaration"], (key, arms[key])


def test_coverage_map_proves_every_supported_arm(tmp_path) -> None:
    rc, data = _run_json(_scratch(tmp_path), "sim", "coverage-map",
                         "--tests", "sim/cocotb/tests")
    report, arms = _coverage_arms(data)
    assert {key: arm["reqs"] for key, arm in arms.items() if arm["reqs"]} \
        == PROVEN, "coverage-map arm bindings differ from the expected table"
    unproven = {key: arms[key]["state"] for key in PROVEN
                if arms[key]["state"] != "PROVEN"}
    assert not unproven, f"supported arms not PROVEN: {unproven}"
    holes = report["feature_matrix"]["req_holes"]
    assert holes == [], f"coverage-map arm holes: {holes}"
    assert report["ok"] and rc == 0, f"coverage-map rc {rc}"


def test_default_discovery_finds_the_cocotb_witnesses(tmp_path) -> None:
    """Without --tests, both commands must find the @requires tests under
    sim/cocotb/tests, not the pytest guards in tests/."""
    scratch = _scratch(tmp_path)
    rc, data = _run_json(scratch, "sim", "coverage-map")
    report = data["project_report"]
    assert report["missing"] == [], (
        f"coverage-map found no @requires test for {report['missing']} "
        f"(tests_dir {report['tests_dir']})")
    assert report["tests_dir"].endswith("sim/cocotb/tests"), report["tests_dir"]
    assert len(report["covered"]) == 17, report["covered"]
    assert report["ok"] and rc == 0, f"coverage-map rc {rc}"
    _, data = _run_json(scratch, "contract", "report")
    assert data["summary"]["requirements_untested"] == [], data["summary"]


def test_removed_arm_witness_is_detected(tmp_path) -> None:
    """Negative control: delete the only @requires tag witnessing the
    clock-enable freeze arm; both commands must report the arm UNVERIFIED."""
    scratch = _scratch(tmp_path)
    test_file = scratch / WITNESS_FILE
    source = test_file.read_text(encoding="utf-8")
    assert source.count(WITNESS_TAG) == 1, "witness tag not found once"
    test_file.write_text(source.replace(WITNESS_TAG, ""), encoding="utf-8")

    rc, data = _run_json(scratch, "sim", "coverage-map",
                         "--tests", "sim/cocotb/tests")
    report, arms = _coverage_arms(data)
    assert report["missing"] == ["FFT-REQ-006"], report["missing"]
    assert arms[WITNESS_ARM]["state"] == "UNVERIFIED", arms[WITNESS_ARM]
    assert {"axis": WITNESS_ARM[0], "arm": WITNESS_ARM[1]} in \
        report["feature_matrix"]["req_holes"], report["feature_matrix"]
    assert not report["ok"] and rc != 0, f"coverage-map passed, rc {rc}"

    _, data = _run_json(scratch, "contract", "report")
    assert _report_arms(data)[WITNESS_ARM]["state"] == "UNVERIFIED", data
