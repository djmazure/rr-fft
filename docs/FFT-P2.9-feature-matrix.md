# FFT-P2.9 feature matrix

Facts verified against `aa3442f` plus the FFT-P2.9 change on 2026-10-21.
Tools: routertl 5.1.0 (dev, SDK checkout `4fc835e63`), nvc 1.19.3, cocotb 2.1.0, Python 3.10.12.

## Location

| Item | Value |
|---|---|
| Matrix | `requirements.yml`, top-level key `feature_matrix:` (in-schema form) |
| Policy | `no-silent-arms`, `audited_by: FFT-P2.9` |
| Arm state rule | `satisfy: all` (default): an arm is PROVEN when every mapped REQ has a live, non-vacuous `@requires` test |
| Test root pointer | `ip.yml` `build.simulation.test_dir: sim/cocotb/tests` |

`rr sim coverage-map` evaluates only the in-schema key. A sibling `feature_matrix.yml` is read by `rr contract report` only, so the matrix is in `requirements.yml`. The REQ entries in `functional_contract:` are unchanged.

Without the `ip.yml` pointer, coverage-map takes `tests/` (the pytest guards) as the test root when that directory exists, and reports 0/16 REQs covered. `simulation.test_dir` in `project.yml` does not change that choice.

## Scope

| Item | State |
|---|---|
| Implemented RTL | 256 points, 24-bit data, 24-bit twiddle (package constants), truncation, natural order, scaled and unscaled builds, core and shim |
| Planned (FFT-P4.1) | other or runtime transform lengths, other widths, bit-reversed order, convergent rounding, block floating point, multichannel; declared `unexercised`, no REQ binding |
| Oracle-only | `model/rr_fft_oracle.py` takes any `log2_n` and `data_width` and has `bit_reversed()`; not RTL, declared `unexercised` |

## Axes

| Axis | Proven arms | Unexercised arms |
|---|---|---|
| build_variant | 2 | 0 |
| direction | 4 | 0 |
| scale_schedule | 7 | 1 (every legal schedule with non-DC frames) |
| interface | 5 | 2 (shim event outputs, multichannel) |
| frame_configuration | 3 | 6 (planned and oracle-only) |
| stream_control | 5 | 5 (unscaled-build aclken freeze, reset, data-pushed silence; backpressure; self-flush) |
| Total | 26 | 14 |

## Results

| Check | Result |
|---|---|
| `rr contract report` | matrix present; 16/16 REQs proven; arms: 26 PROVEN, 0 UNVERIFIED, 14 UNEXERCISED, 0 DEFECT |
| `rr sim coverage-map` | OK, covered 16/16, 40 arms, 0 REQ holes; 26 stimulus-bin holes, advisory (warn) |
| cocotb, 10 modules, `rr sim run <module> --wait` | 19 testcases, 19 passed, 0 failed, 0 skipped (2026-10-21 16:24 to 16:27 UTC) |
| `tests/test_feature_matrix.py` | 5 passed |
| `rr fdd check --strict --full` | no finding |

PROVEN states that a live, non-vacuous test exists for each mapped REQ. Execution results are the cocotb row.

## Negative control

| Step | Expected |
|---|---|
| `@requires("FFT-REQ-006")` removed from `sim/cocotb/tests/test_fft_core.py` (scratch copy) | — |
| `rr sim coverage-map --json` | `missing: [FFT-REQ-006]`; arm `stream_control/clock-enable freeze / scaled core` UNVERIFIED and listed in `req_holes`; `ok: false`, exit code not 0 |
| `rr contract report --json` | same arm UNVERIFIED; 1 UNVERIFIED arm |

Automated in `tests/test_feature_matrix.py::test_removed_arm_witness_is_detected`.

## Commands

From the repository root:

```sh
rr contract report
rr contract report --json
rr sim coverage-map
rr sim coverage-map --json
python3 -m pytest tests/test_feature_matrix.py -q
rr fdd check --strict --full
rr sim run test_fft_core --wait
```

The other cocotb modules run the same way: `test_fft_unscaled`, `test_fft_dc_scaled`, `test_fft_dc_unscaled`, `test_fft_shim_scaled`, `test_fft_shim_unscaled`, `test_fft_shim_live_scaled`, `test_fft_shim_live_unscaled`, `test_fft_demo_tone`, `test_fft_demo_sweep`.

## Items to confirm

- Stimulus-bin evidence per arm: the cocotb tests do not sample coverage bins, so every bound arm reports 0/1 stimulus hits. The gate is advisory unless `coverage.profile` is strict.
- Coverage-map test-root choice when `tests/` exists beside `requirements.yml`: worked around in `ip.yml`; the SDK behaviour is not changed here.
