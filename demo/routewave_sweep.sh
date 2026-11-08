#!/usr/bin/env bash
# rr_fft frequency-sweep demo in RouteWave.
#
# 1. Generate the waves (self-checking sweep: 64 frames, peak walks
#    bins 0,4,...252):   cd rr-fft && rr sim run test_fft_demo_sweep
# 2. Run this script. Waves tab: analog chirp (in_re) + hardware
#    spectrum stream (out_re). Spectrum tab: RouteWave's own FFT of the
#    input — the sweep as a waterfall diagonal (with a textbook Nyquist
#    fold past mid-sweep).
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
FST="$HERE/sim/waves/test_fft_demo_sweep/rr_fft_top.fst"
[ -f "$FST" ] || { echo "run 'rr sim run test_fft_demo_sweep' first"; exit 1; }
exec routewave "$FST" -c "show s_axis_data_tdata[47:0]; show m_axis_data_tdata[47:0]; show m_axis_data_tuser[7:0]; show st_re[23:0]; slice s_axis_data_tdata[47:0] in_re 23:0; slice m_axis_data_tdata[47:0] out_re 23:0; radix in_re dec_s; radix out_re dec_s; radix st_re[23:0] dec_s; analog in_re on; analog out_re on; zoom fit; fft st_re[23:0]"
