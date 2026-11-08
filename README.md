# rr_fft — first-party pipelined streaming FFT/IFFT

A contract-first, RouteRTL-native FFT/IFFT core. One parameterized entity
covers both directions (FWD/INV is a runtime config field, exactly like the
Xilinx xfft it replaces). Built as a drop-in substitute for xfft v9.1 in
its common streaming configs (256-pt, 24-bit, pipelined streaming,
truncation, natural order — scaled and unscaled builds, each in either
direction), via the
included `rr_fft_xfft_shim` port-map adapter.

## Why first-party

- Kills the encrypted-secureip sim gap: the whole datapath becomes natively
  nvc-simulable (no C-model VHPIDIRECT bridge on the critical path).
- Flagship contract-verified DSP IP for the RouteRTL registry.
- Dogfoods the two-tier contract (functional + technical closure) and the
  `rr loop` convergence steward on a real, timing-aggressive core.

## v1 scope (xfft-equivalent reference config)

| Axis | v1 | Door kept open |
|---|---|---|
| Transform length | generic `LOG2_N` (256 in v1) | runtime N ≤ max (xfft-style) later |
| Data / twiddle width | generics (24/24 in v1) | any widths |
| Direction | runtime FWD/INV via config stream | — |
| Scaling | runtime scale schedule (scaled) + unscaled bit-growth path | block-floating-point later |
| Rounding | truncation | convergent rounding as a generic later |
| Output order | natural (ping-pong reorder RAM) | bit-reversed option (cheaper) as a generic |
| Architecture | radix-2² SDF, 1 sample/clk | SSR / multi-path MDF later — stage interface kept SSR-clean |
| Interfaces | AXIS data in/out + AXIS config, `xk_index` on tuser, aclken | multichannel later |
| Fmax | **375 MHz hard** on xczu5cg-…-2 | — |
| Twiddle ROM | generic `G_TWIDDLE_ROM_STYLE`: `"distributed"` (default, LUTs) or `"block"` (first pair's ROM in block RAM) | — |

## Twiddle ROM style (`G_TWIDDLE_ROM_STYLE`)

A generic on `rr_fft_top` and on `rr_fft_xfft_shim` selects where the
first stage pair's twiddle ROM (192 words, the only large one) is held:

| Value | Twiddle ROM | Logic LUT | Block RAM tiles | Use when |
|---|---|---|---|---|
| `"distributed"` (default) | LUTs | as before | 1 (reorder RAM) | block RAM is short |
| `"block"` | 2 RAMB18 | about 175 fewer | 2 | logic LUT is short |

Output stream and latency are the same in both styles (FFT-REQ-040). Any
other value fails elaboration. Routed numbers per build:
`docs/timing/routed_ooc.json` (`targets/twiddle_block.yml` is the block
build).

## Verification strategy

Bit-exact to **our own fixed-point oracle** (`model/` — Python R2²SDF with
truncation semantics, itself proven against `np.fft` within scaling and
cross-checked against the Xilinx xfft bit-accurate C model). xfft-equivalence
at system level (SNR / tolerance), not bit level — see docs/ARCHITECTURE.md.
FFT→IFFT round-trip identity (within scaling) is a standing adversarial test.

Contract: `requirements.yml` — functional tier (FFT-REQ-*) plus the technical
tier (fmax / II / latency / resource caps) that drives technical closure.

## Workflow

Pure routertl flow throughout: `rr sim run <test>`, `rr linting`
(RouteRTL VHDL style, profile `routertl-vhdl-strict-provisional`),
`rr loop open/step/close` for budgeted convergence sessions, full builds
via `rr queue`.
