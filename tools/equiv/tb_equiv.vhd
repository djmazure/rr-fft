-------------------------------------------------------------------------------
-- File        : tb_equiv.vhd
-- Description : rr_fft_xfft_shim of a base revision and of the working tree
--               side by side on one random stimulus; every output compared
--               on every cycle.
-- Standard    : VHDL-2008
-- SPDX-License-Identifier: MIT
-------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

library rr_fft_old;
library rr_fft_new;
use rr_fft_new.rr_fft_pkg.all;

entity tb_equiv is
  generic (
    G_HAS_SCALING       : boolean := true;
    G_TWIDDLE_ROM_STYLE : string  := "distributed";
    G_CYCLES            : natural := 200000;
    G_SEED              : positive := 1
  );
end entity tb_equiv;

architecture sim of tb_equiv is
  constant C_CW : natural := f_shim_cfg_w(G_HAS_SCALING);
  constant C_OW : natural := 2 * f_shim_ow(G_HAS_SCALING);
  signal clk, en, rstn : std_logic := '0';
  signal cfg : std_logic_vector(C_CW - 1 downto 0) := (others => '0');
  signal cfgV, dV, dL : std_logic := '0';
  signal din : std_logic_vector(47 downto 0) := (others => '0');
  type t_out is record
    cfgR, dR, oV, oL, e0, e1, e2, e3 : std_logic;
    o : std_logic_vector(C_OW - 1 downto 0);
    u : std_logic_vector(7 downto 0);
  end record;
  signal a, b : t_out;
  signal done : boolean := false;
begin
  clk <= not clk after 1 ns when not done;

  i_old : entity rr_fft_old.rr_fft_xfft_shim
    generic map (G_HAS_SCALING => G_HAS_SCALING, G_TWIDDLE_ROM_STYLE => G_TWIDDLE_ROM_STYLE)
    port map (aclk => clk, aclken => en, aresetn => rstn,
              s_axis_config_tdata => cfg, s_axis_config_tvalid => cfgV, s_axis_config_tready => a.cfgR,
              s_axis_data_tdata => din, s_axis_data_tvalid => dV, s_axis_data_tready => a.dR,
              s_axis_data_tlast => dL,
              m_axis_data_tdata => a.o, m_axis_data_tuser => a.u, m_axis_data_tvalid => a.oV,
              m_axis_data_tlast => a.oL, event_frame_started => a.e0, event_tlast_unexpected => a.e1,
              event_tlast_missing => a.e2, event_data_in_channel_halt => a.e3);

  i_new : entity rr_fft_new.rr_fft_xfft_shim
    generic map (G_HAS_SCALING => G_HAS_SCALING, G_TWIDDLE_ROM_STYLE => G_TWIDDLE_ROM_STYLE)
    port map (ACLK => clk, ACLKEN => en, ARESETN => rstn,
              S_AXIS_CONFIG_TDATA => cfg, S_AXIS_CONFIG_TVALID => cfgV, S_AXIS_CONFIG_TREADY => b.cfgR,
              S_AXIS_DATA_TDATA => din, S_AXIS_DATA_TVALID => dV, S_AXIS_DATA_TREADY => b.dR,
              S_AXIS_DATA_TLAST => dL,
              M_AXIS_DATA_TDATA => b.o, M_AXIS_DATA_TUSER => b.u, M_AXIS_DATA_TVALID => b.oV,
              M_AXIS_DATA_TLAST => b.oL, EVENT_FRAME_STARTED => b.e0, EVENT_TLAST_UNEXPECTED => b.e1,
              EVENT_TLAST_MISSING => b.e2, EVENT_DATA_IN_CHANNEL_HALT => b.e3);

  pr_stim : process
    variable s1, s2 : positive := G_SEED;
    variable r : real;
    variable rstLeft : natural := 4;
    impure function rnd return real is begin uniform(s1, s2, r); return r; end function;
    impure function rbits (n : natural) return std_logic_vector is
      variable v : std_logic_vector(n - 1 downto 0);
    begin
      for i in v'range loop
        if rnd < 0.5 then v(i) := '0'; else v(i) := '1'; end if;
      end loop;
      return v;
    end function;
  begin
    s2 := G_SEED * 7 + 3;
    for c in 0 to G_CYCLES - 1 loop
      wait until falling_edge(clk);
      if rstLeft > 0 then
        rstn <= '0'; rstLeft := rstLeft - 1;
      else
        rstn <= '1';
        if rnd < 0.0002 then rstLeft := 1 + natural(floor(rnd * 4.0)); end if;
      end if;
      -- long ungated stretches keep full frames flowing; short gated bursts
      if rnd < 0.97 then en <= '1'; else en <= '0'; end if;
      if rnd < 0.9 then dV <= '1'; else dV <= '0'; end if;
      dL <= rbits(1)(0);
      din <= rbits(48);
      if rnd < 0.002 then
        cfgV <= '1'; cfg <= rbits(C_CW);
      else
        cfgV <= '0';
      end if;
    end loop;
    done <= true;
    wait;
  end process pr_stim;

  pr_check : process
    variable nBeats, nFrames : natural := 0;
  begin
    while not done loop
      wait until rising_edge(clk);
      wait for 0.5 ns;   -- outputs settled after the edge
      assert a = b
        report "MISMATCH at " & time'image(now) & " old tdata=" & to_hstring(a.o)
             & " new tdata=" & to_hstring(b.o) & " old v/l/u=" & std_logic'image(a.oV)
             & std_logic'image(a.oL) & to_hstring(a.u) & " new v/l/u=" & std_logic'image(b.oV)
             & std_logic'image(b.oL) & to_hstring(b.u)
        severity failure;
      if a.oV = '1' and en = '1' then nBeats := nBeats + 1; end if;
      if a.oV = '1' and a.oL = '1' and en = '1' then nFrames := nFrames + 1; end if;
    end loop;
    report "EQUIV OK cycles=" & integer'image(G_CYCLES) & " out_beats=" & integer'image(nBeats)
         & " frames=" & integer'image(nFrames);
    assert nFrames > 100 report "vacuous: too few output frames" severity failure;
    wait;
  end process pr_check;
end architecture sim;
