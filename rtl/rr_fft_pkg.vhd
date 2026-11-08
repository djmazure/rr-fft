-------------------------------------------------------------------------------
-- File        : rr_fft_pkg.vhd
-- Description : Shared constants, types and twiddle ROM generation.
-- Standard    : VHDL-2008
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
-------------------------------------------------------------------------------
--
-- The twiddle quantization here is THE spec (matches model/rr_fft_oracle.py
-- bit-for-bit): Q1.(TW-1), the xfft v9.1 format, component-wise
-- floor(x * 2^(TW-1) + 0.5) (round-half-up). Both use IEEE-double cos/sin,
-- so ROM contents are bit-identical to the oracle's _twiddle().
-- Q1.(TW-1) cannot hold +1.0: word 0 (W^0 = 1) is never multiplied (the
-- pair bypasses it), so it is stored saturated; no other forward twiddle
-- rounds to +1.0.
--
-- v1 fixes LOG2_N=8 / DW=24 / TW=24 (the reference config) as package constants;
-- the generic-N door stays open architecturally (stage-local control, no
-- global counters) and is plumbed in a later session.

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;
  use ieee.math_real.all;

package rr_fft_pkg is

  constant C_DW     : natural := 24;   -- data width (I and Q each)
  constant C_TW     : natural := 24;   -- twiddle width
  constant C_LOG2N  : natural := 8;    -- 256-pt (even in v1)
  constant C_N      : natural := 2 ** C_LOG2N;
  constant C_NPAIRS : natural := C_LOG2N / 2;

  -- per-beat sideband config: bit 0 = fwd (1 = forward), then 2 bits of
  -- scale schedule per pair, sch[p] at bits (2p+2 downto 2p+1).
  constant C_CFG_W : natural := 1 + 2 * C_NPAIRS;

  -- ROM of W_L^k, k in [0, 3L/4): packed im & re, C_TW bits each.
  type t_tw_rom is array (natural range <>) of
    std_logic_vector(2 * C_TW - 1 downto 0);

  function f_twiddle_rom (l : natural) return t_tw_rom;

  function f_bitrev (v : unsigned) return unsigned;

  function f_clog2 (n : positive) return natural;

  -- Inter-pair width plan (oracle-validated, docs/ARCHITECTURE.md):
  -- scaled -> C_DW everywhere; unscaled -> 24, 27, 29, 31, 33 at the
  -- pair boundaries (|partial| <= 4^p * sqrt2 * 2^(C_DW-1), the sqrt2
  -- bit spent once). pair = 0 is the core input.
  function f_wplan (pair : natural; scaled : boolean) return natural;

  -- xfft v9.1 shim port geometry: config AXIS width (16 scaled / 8
  -- unscaled) and per-component output width padded to the byte
  -- boundary (24 scaled / 40 unscaled, sign-extended).
  function f_shim_cfg_w (scaled : boolean) return natural;
  function f_shim_ow (scaled : boolean) return natural;

end package rr_fft_pkg;

package body rr_fft_pkg is

  function f_twiddle_rom (l : natural) return t_tw_rom is
    variable v_rom : t_tw_rom(0 to 3 * l / 4 - 1);
    variable v_ang : real;
    variable v_c   : real;
    variable v_s   : real;
  begin
    for k in v_rom'range loop
      -- elaboration-time REAL math in a constant-ROM initialiser: no
      -- hardware divider or multiplier exists, so RR-ARITH-001/002
      -- (both fire here) are waived. Kept as one expression so the
      -- rounding of every ROM word is unchanged.
      v_ang := -math_2_pi * real(k) / real(l);  -- rr-lint-disable-line
      v_c   := floor(cos(v_ang) * 2.0 ** (C_TW - 1) + 0.5);
      v_s   := floor(sin(v_ang) * 2.0 ** (C_TW - 1) + 0.5);
      -- W^0 only (bypassed): saturate +1.0 to the largest code
      if v_c > 2.0 ** (C_TW - 1) - 1.0 then
        v_c := 2.0 ** (C_TW - 1) - 1.0;
      end if;
      v_rom(k) := std_logic_vector(to_signed(integer(v_s), C_TW))
                & std_logic_vector(to_signed(integer(v_c), C_TW));
    end loop;
    return v_rom;
  end function f_twiddle_rom;

  function f_bitrev (v : unsigned) return unsigned is
    variable v_r : unsigned(v'range);
  begin
    for i in 0 to v'length - 1 loop
      v_r(v'low + i) := v(v'high - i);
    end loop;
    return v_r;
  end function f_bitrev;

  function f_clog2 (n : positive) return natural is
    variable v_r : natural := 0;
    variable v_v : natural := n - 1;
  begin
    while v_v > 0 loop
      v_r := v_r + 1;
      v_v := v_v / 2;
    end loop;
    return v_r;
  end function f_clog2;

  function f_wplan (pair : natural; scaled : boolean) return natural is
  begin
    if scaled or pair = 0 then
      return C_DW;
    end if;
    return C_DW + 2 * pair + 1;
  end function f_wplan;

  function f_shim_cfg_w (scaled : boolean) return natural is
  begin
    if scaled then
      return 16;
    end if;
    return 8;
  end function f_shim_cfg_w;

  function f_shim_ow (scaled : boolean) return natural is
  begin
    if scaled then
      return C_DW;
    end if;
    return 8 * ((f_wplan(C_NPAIRS, false) + 7) / 8);  -- 33 -> 40
  end function f_shim_ow;

end package body rr_fft_pkg;
