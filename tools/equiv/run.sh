#!/usr/bin/env bash
# Cycle equivalence of the rtl/ of a base revision and of the working tree
# (FFT-P2.13). Both rr_fft_xfft_shim builds are driven by one random stimulus
# (input gaps, ACLKEN low, config writes, short resets) and every output is
# compared on every cycle, in the four builds (scaled/unscaled x
# distributed/block twiddle ROM). Exit 0 only when all four match.
#
# usage: tools/equiv/run.sh [BASE_REV] [CYCLES]    (default origin/main, 200000)
set -eu -o pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
REV=${1:-origin/main}
CYCLES=${2:-200000}
W=$(mktemp -d "${TMPDIR:-/tmp}/rr_fft_equiv.XXXXXX")
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/base"
git -C "$ROOT" archive "$REV" rtl | tar -x -C "$W/base"
F="rr_fft_pkg rr_fft_sdf_pair rr_fft_reorder rr_fft_top rr_fft_xfft_shim"
cd "$W"
nvc --std=2008 --work=rr_fft_old:"$W/rr_fft_old" -a $(for f in $F; do echo "$W/base/rtl/$f.vhd"; done)
nvc --std=2008 --work=rr_fft_new:"$W/rr_fft_new" -a $(for f in $F; do echo "$ROOT/rtl/$f.vhd"; done)
nvc --std=2008 -L "$W" --work=work:"$W/work" -a "$ROOT/tools/equiv/tb_equiv.vhd"
for cfg in "true distributed 1" "false distributed 2" "true block 3" "false block 4"; do
  set -- $cfg
  echo "== base $REV, G_HAS_SCALING=$1 G_TWIDDLE_ROM_STYLE=$2 seed=$3"
  nvc --std=2008 -L "$W" --work=work:"$W/work" -e tb_equiv \
      -gG_HAS_SCALING="$1" -gG_TWIDDLE_ROM_STYLE="$2" -gG_SEED="$3" -gG_CYCLES="$CYCLES" \
      --no-save --jit -r --exit-severity=failure 2>&1 \
    | grep -v "NUMERIC_STD\|^   Process .* at .*rr_fft_" | grep -E "EQUIV|MISMATCH|Error|Failure|vacuous"
done
