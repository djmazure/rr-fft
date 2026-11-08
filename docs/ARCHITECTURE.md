# rr_fft architecture notes (v0 — pre-RTL)

## Core: radix-2² single-path delay feedback (SDF)

`LOG2_N` stages, feedback delay lines N/2 … 1, one non-trivial complex
multiplier per stage pair (~LOG2_N/2 − 1 for the 256-pt build), twiddle
ROMs, per-stage-pair scale/truncate, optional ping-pong reorder RAM
(bit-reversed → natural) sized 2 × N complex samples.

IFFT = same butterflies with conjugated twiddles (twiddle-ROM address/
conjugate mux driven by the runtime FWD_INV field). The genuinely separate
work for inverse is the **unscaled bit-growth path** (per-stage width grows
by 1; output DATA_WIDTH + LOG2_N + 1) — carried as a parallel width plan
through the same stages, not a second pipeline.

## Fmax 375 MHz on ZU+ -2: design rules

- Every stage boundary registered; DSP48E2 fully pipelined (AREG/BREG/MREG/
  PREG) — DSPs themselves close >600 MHz, the risk is fabric control.
- 24×24 twiddle mults exceed the 18-bit B port → 2 cascaded DSP48E2 per real
  mult; recombination adds live inside the DSP cascade, never in fabric.
- Stage-local control only (no global frame counter fanning out to 8 stages);
  each stage regenerates its own schedule from a pipelined frame-phase token.
- Reorder/delay RAMs: output registers ON (2-cycle read), depth ≤ one BRAM.

## Unscaled width plan (FFT-REQ-002) — oracle-validated 2026-07-17

Inter-pair widths for the unscaled (bit-growth) datapath, 24-bit input:
**24 → 27 → 29 → 31 → 33** at the four pair boundaries. Theory:
|partial| ≤ 4^p · √2 · 2^23 (the √2 bit is spent once, at the first
pair's twiddle rotation). Measured by the oracle's `pair_max` tracker
under adversarial full-scale stimulus (coherent DC, most-negative,
full-scale tones, chirp, 400 random-phase frames, FWD and INV): peaks
2^25+1 / 2^27 / 2^29+ / 2^32 — the bound is TIGHT at pairs 2 and 4.
RTL consequence: pair ports are width-generic (G_WIN/G_WOUT from
f_wplan, G_SCALED selects wrap-vs-grow). DSP cost: 29-bit+ data exceeds
the DSP48E2 27-bit A port, but the TWIDDLE is always 24-bit — with the
twiddle on the A port and the grown data split across the 18-bit B port
(17-bit cascade shift), each product stays 2 DSPs up to 44-bit data.
Verify post-synth that Vivado picks this decomposition; hand-structure
the multiply if it doesn't. If the count still grows, the DSP cap in
requirements.yml must become per-variant (technical-tier gap).

## Equivalence tiers (what "comparable to xfft" means here)

1. **Bit-exact to model/ oracle** — the contract, enforced per-REQ in sim.
   The oracle defines truncation position at every stage; RTL matches it.
2. **System-equivalent to xfft** — SNR/tolerance comparison against the
   Xilinx bit-accurate C model over stimulus corpora. NOT bit-exact by
   design: xfft's internal truncation points are undocumented; chasing them
   is reverse-engineering with no convergence guarantee (see the estimate
   discussion, 2026-07-16).
3. **In-system A/B** — deployment-datapath golden vectors through both
   cores; acceptance is the downstream system contract, not the FFT alone.

## Doors deliberately kept open (and what keeping them open costs now)

| Door | Cost now |
|---|---|
| Runtime N ≤ LOG2_N_MAX | stage-bypass muxes designed-in later; today only requires the frame-phase token (not a hardwired counter chain) |
| SSR ≥ 2 (multi-sample/clk) | stage interface = record of (data, phase-token); no stage may assume 1 sample/frame-tick structurally |
| Convergent rounding | truncation isolated in ONE function per stage (`round_stage`), never inlined |
| Block floating point | scale-schedule plumbing is per-frame metadata, which BFP extends |
| Multichannel | frame-phase token carries a channel field (width-0 today) |

## Non-goals for v1

Float, cyclic prefix insertion, runtime transform length, SSR — all doored,
none built. v1 ships the xfft-equivalent reference config, world-class on the axes
that matter there: Fmax, II=1, verified truncation-exact arithmetic.
