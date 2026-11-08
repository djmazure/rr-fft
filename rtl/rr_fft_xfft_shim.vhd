-------------------------------------------------------------------------------
-- File        : rr_fft_xfft_shim.vhd
-- Description : Drop-in port-map adapter for the Xilinx xfft v9.1 instance shape.
-- Standard    : VHDL-2008
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
-------------------------------------------------------------------------------
--
-- v9.1 instance shape (as commonly instantiated by integration wrappers
-- around scaled-forward and unscaled-inverse xfft configs): swapping an
-- xfft instance for this entity is a one-line component change.
--
-- Port geometry per build (matches the xfft configs it replaces):
--   scaled   (G_HAS_SCALING=true):  config 16-bit (FWD_INV + 8-bit
--            schedule), data out 48-bit (2 x 24).
--   unscaled (G_HAS_SCALING=false): config 8-bit (FWD_INV only), data
--            out 80-bit (2 x 40: the 33-bit results SIGN-EXTENDED to
--            the byte boundary, xfft padding convention).
--
-- CONFIG LAYOUT: the scaled config word is passed through unmodified.
-- xfft v9.1 and rr_fft_top use the same field packing: bit 0 = FWD_INV
-- (1 = forward), bits 8:1 = scale schedule with the INPUT-side stage
-- pair at bits 2:1. Confirmed by an xsim run of the generated xfft v9.1
-- IP (Vivado 2024.2) with config word 0x156: its output is bit-exact to
-- the C model run as inverse with schedule [3, 2, 2, 2] over the
-- FFT-REQ-022 corpus. The schedule array ordering is also proven by
-- overflow agreement in model/test_xfft_crosscheck.py.
--
-- S_AXIS_DATA_TLAST is accepted and ignored: rr_fft frames by sample
-- count (realtime contract); the xfft framing-event outputs it feeds
-- (event_tlast_*) are unconnected in the deployment design and are tied
-- '0' here.

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

library work;
  use work.rr_fft_pkg.all;

entity rr_fft_xfft_shim is
  generic (
    G_HAS_SCALING : boolean := true;
    -- passed to rr_fft_top: "distributed" (default) | "block"
    G_TWIDDLE_ROM_STYLE : string := "distributed"
  );
  port (
    ACLK    : in std_logic;
    ACLKEN  : in std_logic;
    ARESETN : in std_logic;

    S_AXIS_CONFIG_TDATA  : in  std_logic_vector(
      f_shim_cfg_w(G_HAS_SCALING) - 1 downto 0);
    S_AXIS_CONFIG_TVALID : in  std_logic;
    S_AXIS_CONFIG_TREADY : out std_logic;

    S_AXIS_DATA_TDATA  : in  std_logic_vector(2 * C_DW - 1 downto 0);
    S_AXIS_DATA_TVALID : in  std_logic;
    S_AXIS_DATA_TREADY : out std_logic;
    S_AXIS_DATA_TLAST  : in  std_logic;

    M_AXIS_DATA_TDATA  : out std_logic_vector(
      2 * f_shim_ow(G_HAS_SCALING) - 1 downto 0);
    M_AXIS_DATA_TUSER  : out std_logic_vector(C_LOG2N - 1 downto 0);
    M_AXIS_DATA_TVALID : out std_logic;
    M_AXIS_DATA_TLAST  : out std_logic;

    EVENT_FRAME_STARTED        : out std_logic;
    EVENT_TLAST_UNEXPECTED     : out std_logic;
    EVENT_TLAST_MISSING        : out std_logic;
    EVENT_DATA_IN_CHANNEL_HALT : out std_logic
  );
end entity rr_fft_xfft_shim;

architecture rtl of rr_fft_xfft_shim is

  constant C_COW : natural := f_wplan(C_NPAIRS, G_HAS_SCALING);
  constant C_POW : natural := f_shim_ow(G_HAS_SCALING);

  signal coreCfg    : std_logic_vector(15 downto 0);
  signal coreMTdata : std_logic_vector(2 * C_COW - 1 downto 0);

begin

  -- framing events: rr_fft frames by count; the deployment design
  -- leaves these unconnected on the xfft instances it replaces
  EVENT_FRAME_STARTED        <= '0';
  EVENT_TLAST_UNEXPECTED     <= '0';
  EVENT_TLAST_MISSING        <= '0';
  EVENT_DATA_IN_CHANNEL_HALT <= '0';

  GEN_CFG_SCALED : if G_HAS_SCALING generate
    coreCfg <= S_AXIS_CONFIG_TDATA;   -- identical packing (see note)
  end generate GEN_CFG_SCALED;

  GEN_CFG_UNSCALED : if not G_HAS_SCALING generate
    -- FWD_INV only; the core ignores schedule bits in this build
    coreCfg <= (15 downto 1 => '0') & S_AXIS_CONFIG_TDATA(0);
  end generate GEN_CFG_UNSCALED;

  i_core : entity work.rr_fft_top
    generic map (
      G_HAS_SCALING       => G_HAS_SCALING,
      G_TWIDDLE_ROM_STYLE => G_TWIDDLE_ROM_STYLE
    )
    port map (
      ACLK                 => ACLK,
      ARESETN              => ARESETN,
      ACLKEN               => ACLKEN,
      S_AXIS_CONFIG_TDATA  => coreCfg,
      S_AXIS_CONFIG_TVALID => S_AXIS_CONFIG_TVALID,
      S_AXIS_CONFIG_TREADY => S_AXIS_CONFIG_TREADY,
      S_AXIS_DATA_TDATA    => S_AXIS_DATA_TDATA,
      S_AXIS_DATA_TVALID   => S_AXIS_DATA_TVALID,
      S_AXIS_DATA_TREADY   => S_AXIS_DATA_TREADY,
      M_AXIS_DATA_TDATA    => coreMTdata,
      M_AXIS_DATA_TVALID   => M_AXIS_DATA_TVALID,
      M_AXIS_DATA_TUSER    => M_AXIS_DATA_TUSER,
      M_AXIS_DATA_TLAST    => M_AXIS_DATA_TLAST
    );

  GEN_OUT_SCALED : if G_HAS_SCALING generate
    M_AXIS_DATA_TDATA <= coreMTdata;
  end generate GEN_OUT_SCALED;

  GEN_OUT_UNSCALED : if not G_HAS_SCALING generate
    -- xfft padding: each 33-bit component sign-extended to 40 bits
    M_AXIS_DATA_TDATA(C_POW - 1 downto 0) <= std_logic_vector(resize(
      signed(coreMTdata(C_COW - 1 downto 0)), C_POW));
    M_AXIS_DATA_TDATA(2 * C_POW - 1 downto C_POW) <= std_logic_vector(
      resize(signed(coreMTdata(2 * C_COW - 1 downto C_COW)), C_POW));
  end generate GEN_OUT_UNSCALED;

end architecture rtl;
