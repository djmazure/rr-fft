-------------------------------------------------------------------------------
-- File        : rr_fft_top.vhd
-- Description : Pipelined streaming FFT/IFFT, 256-point, 24-bit.
-- Standard    : VHDL-2008
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
-------------------------------------------------------------------------------
--
-- xfft-comparable interface: AXIS data in (im & re packed, no
-- backpressure — realtime throttle contract), AXIS config (bit 0 FWD_INV,
-- 1 = forward; bits 2p+2:2p+1 = scale schedule for pair p, sch[0] =
-- input-side pair — the ordering proven against the xfft C model in
-- model/test_xfft_crosscheck.py), AXIS data out with xk_index on tuser
-- and tlast per frame.
--
-- A config word applies from the NEXT frame boundary (FFT-REQ-004): the
-- active config is stamped onto each input beat as sideband and travels
-- with the data through every stage — no global control fanout (this is
-- the frame-phase-token door from docs/ARCHITECTURE.md).
--
-- G_HAS_SCALING = true  -> runtime scale schedule, 24-bit output (wrap);
--               = false -> unscaled bit growth per the oracle-proven
--                          width plan 24/27/29/31/33 (FFT-REQ-002); the
--                          schedule bits of the config word are ignored.
--
-- Reset default config: forward, schedule [2,2,2,2] (the conservative
-- 1/N schedule; ignored when G_HAS_SCALING = false).
--
-- G_TWIDDLE_ROM_STYLE = "distributed" (default) -> every twiddle ROM in
--               LUTs; the reorder RAM is the only block RAM;
--             = "block" -> the first pair's twiddle ROM (192 words, the
--               only large one) in one block RAM, about 150 logic LUT
--               fewer. Output stream and latency are identical in both
--               styles (FFT-REQ-040). Any other value fails elaboration.

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

library work;
  use work.rr_fft_pkg.all;

entity rr_fft_top is
  generic (
    -- xfft C_HAS_SCALING equivalent (build-time, like xfft):
    -- true  -> runtime scale schedule, C_DW-wide output, wrap;
    -- false -> unscaled bit growth, f_wplan-planned widths, 33-b out.
    G_HAS_SCALING : boolean := true;
    -- twiddle ROM style of the first pair: "distributed" | "block"
    G_TWIDDLE_ROM_STYLE : string := "distributed"
  );
  port (
    ACLK    : in std_logic;
    ARESETN : in std_logic;
    ACLKEN  : in std_logic;

    S_AXIS_CONFIG_TDATA  : in  std_logic_vector(15 downto 0);
    S_AXIS_CONFIG_TVALID : in  std_logic;
    S_AXIS_CONFIG_TREADY : out std_logic;

    S_AXIS_DATA_TDATA  : in  std_logic_vector(2 * C_DW - 1 downto 0);
    S_AXIS_DATA_TVALID : in  std_logic;
    S_AXIS_DATA_TREADY : out std_logic;

    M_AXIS_DATA_TDATA  : out std_logic_vector(
      2 * f_wplan(C_NPAIRS, G_HAS_SCALING) - 1 downto 0);
    M_AXIS_DATA_TVALID : out std_logic;
    M_AXIS_DATA_TUSER  : out std_logic_vector(C_LOG2N - 1 downto 0);
    M_AXIS_DATA_TLAST  : out std_logic
  );
end entity rr_fft_top;

architecture rtl of rr_fft_top is

  constant C_CFG_RST : std_logic_vector(C_CFG_W - 1 downto 0) := "101010101";

  signal cfgPending_r : std_logic_vector(C_CFG_W - 1 downto 0);
  signal cfgActive_r  : std_logic_vector(C_CFG_W - 1 downto 0);
  signal inCnt_r      : unsigned(C_LOG2N - 1 downto 0);

  -- stamped input beat (registered)
  signal stRe_r, stIm_r : signed(C_DW - 1 downto 0);
  signal stValid_r      : std_logic;
  signal stCfg_r        : std_logic_vector(C_CFG_W - 1 downto 0);

  -- inter-pair chain at the OUTPUT width (widest of the plan); each
  -- pair connects through value-preserving resizes to its planned width
  constant C_OW : natural := f_wplan(C_NPAIRS, G_HAS_SCALING);
  type t_ch_data is array (0 to C_NPAIRS) of signed(C_OW - 1 downto 0);
  type t_ch_cfg  is array (0 to C_NPAIRS) of std_logic_vector(C_CFG_W - 1 downto 0);
  signal chRe, chIm : t_ch_data;
  signal chValid    : std_logic_vector(0 to C_NPAIRS);
  signal chCfg      : t_ch_cfg;

  -- The first pair holds the only large twiddle ROM (3N/4 = 192 words);
  -- the others (48, 12, 3 words) stay in LUTs in either style, where a
  -- block RAM each would cost more than it saves.
  function f_pair_rom_style (p : natural; style : string) return string is
  begin
    if p = 0 then
      return style;
    end if;
    return "distributed";
  end function f_pair_rom_style;

begin

  assert G_TWIDDLE_ROM_STYLE = "distributed"
    or G_TWIDDLE_ROM_STYLE = "block"
    report "rr_fft_top: G_TWIDDLE_ROM_STYLE must be distributed or "
         & "block, got " & G_TWIDDLE_ROM_STYLE
    severity failure;

  S_AXIS_DATA_TREADY   <= '1';   -- realtime throttle: never backpressures
  S_AXIS_CONFIG_TREADY <= '1';

  -- ── config latch + frame-boundary stamping ──────────────────────
  pr_cfg : process (ACLK) is
    variable v_cfg : std_logic_vector(C_CFG_W - 1 downto 0);
  begin
    if rising_edge(ACLK) then
      if ARESETN = '0' then
        cfgPending_r <= C_CFG_RST;
        cfgActive_r  <= C_CFG_RST;
        inCnt_r      <= (others => '0');
        stValid_r    <= '0';
      elsif ACLKEN = '1' then
        if S_AXIS_CONFIG_TVALID = '1' then
          cfgPending_r <= S_AXIS_CONFIG_TDATA(C_CFG_W - 1 downto 0);
        end if;
        stValid_r <= S_AXIS_DATA_TVALID;
        if S_AXIS_DATA_TVALID = '1' then
          if inCnt_r = 0 then
            v_cfg      := cfgPending_r;
            cfgActive_r <= cfgPending_r;
          else
            v_cfg := cfgActive_r;
          end if;
          stCfg_r <= v_cfg;
          stRe_r  <= signed(S_AXIS_DATA_TDATA(C_DW - 1 downto 0));
          stIm_r  <= signed(S_AXIS_DATA_TDATA(2 * C_DW - 1 downto C_DW));
          inCnt_r <= inCnt_r + 1;
        end if;
      end if;
    end if;
  end process pr_cfg;

  chRe(0)    <= resize(stRe_r, C_OW);
  chIm(0)    <= resize(stIm_r, C_OW);
  chValid(0) <= stValid_r;
  chCfg(0)   <= stCfg_r;

  -- ── the four stage pairs: L = 256, 64, 16, 4 ────────────────────
  GEN_PAIRS : for p in 0 to C_NPAIRS - 1 generate
    constant C_WI : natural := f_wplan(p, G_HAS_SCALING);
    constant C_WO : natural := f_wplan(p + 1, G_HAS_SCALING);
    signal diRe, diIm : signed(C_WI - 1 downto 0);
    signal doRe, doIm : signed(C_WO - 1 downto 0);
    -- Each stage pair is kept as its own hierarchy in synthesis. Vivado
    -- 2025.2 with default flattening produces a netlist that differs from
    -- this RTL when two adjacent rr_fft_sdf_pair instances share one
    -- flattened scope (FFT-P2.14); a pair on its own is unaffected.
    attribute keep_hierarchy : string;
    attribute keep_hierarchy of i_pair : label is "yes";
  begin
    diRe <= resize(chRe(p), C_WI);   -- value fits by the width plan
    diIm <= resize(chIm(p), C_WI);
    i_pair : entity work.rr_fft_sdf_pair
      generic map (
        G_L      => C_N / 4 ** p,
        G_PAIR   => p,
        G_WIN    => C_WI,
        G_WOUT   => C_WO,
        G_SCALED => G_HAS_SCALING,
        -- BF2II in DSP48E2 in the first pair only: the DSP budget
        -- (24 multiply, 16 BF2I, 4 here) stays below 46
        G_BF2_DSP => p = 0,
        G_TW_ROM_STYLE => f_pair_rom_style(p, G_TWIDDLE_ROM_STYLE)
      )
      port map (
        CLK       => ACLK,
        RSTN      => ARESETN,
        EN        => ACLKEN,
        RE_IN     => diRe,
        IM_IN     => diIm,
        VALID_IN  => chValid(p),
        CFG_IN    => chCfg(p),
        RE_OUT    => doRe,
        IM_OUT    => doIm,
        VALID_OUT => chValid(p + 1),
        CFG_OUT   => chCfg(p + 1)
      );
    chRe(p + 1) <= resize(doRe, C_OW);
    chIm(p + 1) <= resize(doIm, C_OW);
  end generate GEN_PAIRS;

  -- ── bit-reversed -> natural reorder + AXIS master ───────────────
  i_reorder : entity work.rr_fft_reorder
    generic map (
      G_W => C_OW
    )
    port map (
      CLK        => ACLK,
      RSTN       => ARESETN,
      EN         => ACLKEN,
      RE_IN      => chRe(C_NPAIRS),
      IM_IN      => chIm(C_NPAIRS),
      VALID_IN   => chValid(C_NPAIRS),
      TDATA_OUT  => M_AXIS_DATA_TDATA,
      TVALID_OUT => M_AXIS_DATA_TVALID,
      TUSER_OUT  => M_AXIS_DATA_TUSER,
      TLAST_OUT  => M_AXIS_DATA_TLAST
    );

end architecture rtl;
