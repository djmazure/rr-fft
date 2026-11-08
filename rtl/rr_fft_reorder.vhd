-------------------------------------------------------------------------------
-- File        : rr_fft_reorder.vhd
-- Description : Bit-reversed to natural order ping-pong buffer.
-- Standard    : VHDL-2008
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
-------------------------------------------------------------------------------
--
-- The SDF pipeline emits X[bitrev(s)] at stream position s (proven for
-- the R2^2 branch emission order [b0, b2, b1, b3] in model/ — see
-- docs/ARCHITECTURE.md). Writing beat s at address bitrev(s) lands X[k]
-- at address k; the read side walks 0..N-1 emitting natural order with
-- xk_index on tuser (FFT-REQ-003) and tlast on the final sample.
--
-- Two N-deep pages. Completed pages are tracked with a small counter so
-- a page completing on the exact cycle a read finishes is never lost —
-- gapless back-to-back frames sustain II=1 (FFT-REQ-005). Pages are
-- consumed strictly in order (rdPage_r toggles per read, independent of
-- wrPage_r).

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

library work;
  use work.rr_fft_pkg.all;

entity rr_fft_reorder is
  generic (
    G_W : natural := C_DW    -- per-component data width
  );
  port (
    CLK        : in  std_logic;
    RSTN       : in  std_logic;
    EN         : in  std_logic;
    RE_IN      : in  signed(G_W - 1 downto 0);
    IM_IN      : in  signed(G_W - 1 downto 0);
    VALID_IN   : in  std_logic;
    TDATA_OUT  : out std_logic_vector(2 * G_W - 1 downto 0);
    TVALID_OUT : out std_logic;
    TUSER_OUT  : out std_logic_vector(C_LOG2N - 1 downto 0);
    TLAST_OUT  : out std_logic
  );
end entity rr_fft_reorder;

architecture rtl of rr_fft_reorder is

  type t_ram is array (0 to 2 * C_N - 1) of
    std_logic_vector(2 * G_W - 1 downto 0);
  signal ram_r : t_ram;

  signal wrCnt_r  : unsigned(C_LOG2N - 1 downto 0);
  signal wrPage_r : std_logic;

  signal rdCnt_r  : unsigned(C_LOG2N - 1 downto 0);
  signal rdPage_r : std_logic;
  signal rdBusy_r : std_logic;
  signal pages_r  : unsigned(1 downto 0);   -- completed, unread pages

begin

  pr_wr : process (CLK) is
  begin
    if rising_edge(CLK) then
      if RSTN = '0' then
        wrCnt_r  <= (others => '0');
        wrPage_r <= '0';
      elsif EN = '1' then
        if VALID_IN = '1' then
          ram_r(to_integer(wrPage_r & f_bitrev(wrCnt_r)))
            <= std_logic_vector(IM_IN) & std_logic_vector(RE_IN);
          wrCnt_r <= wrCnt_r + 1;
          if wrCnt_r = C_N - 1 then
            wrPage_r <= not wrPage_r;
          end if;
        end if;
      end if;
    end if;
  end process pr_wr;

  pr_rd : process (CLK) is
    variable v_done      : boolean;  -- a page completes this cycle
    variable v_finishing : boolean;  -- current read emits its last beat
    variable v_avail     : unsigned(1 downto 0);
  begin
    if rising_edge(CLK) then
      if RSTN = '0' then
        rdCnt_r    <= (others => '0');
        rdPage_r   <= '0';
        rdBusy_r   <= '0';
        pages_r    <= (others => '0');
        TVALID_OUT <= '0';
      elsif EN = '1' then
        v_done      := VALID_IN = '1' and wrCnt_r = C_N - 1;
        v_finishing := rdBusy_r = '1' and rdCnt_r = C_N - 1;
        v_avail     := pages_r;
        if v_done then
          v_avail := v_avail + 1;
        end if;

        -- registered RAM read; sideband registered in step so the
        -- beat at rdCnt_r lands on the *_OUT ports one cycle later, aligned.
        TDATA_OUT  <= ram_r(to_integer(rdPage_r & rdCnt_r));
        TUSER_OUT  <= std_logic_vector(rdCnt_r);
        TVALID_OUT <= rdBusy_r;
        if rdCnt_r = C_N - 1 then
          TLAST_OUT <= '1';
        else
          TLAST_OUT <= '0';
        end if;
        if rdBusy_r = '1' then
          rdCnt_r <= rdCnt_r + 1;
        end if;

        if v_finishing then
          rdPage_r <= not rdPage_r;       -- page consumed, advance
        end if;
        if (rdBusy_r = '0' or v_finishing) and v_avail > 0 then
          rdBusy_r <= '1';
          rdCnt_r  <= (others => '0');
          pages_r  <= v_avail - 1;
        else
          pages_r <= v_avail;
          if v_finishing then
            rdBusy_r <= '0';
          end if;
        end if;
      end if;
    end if;
  end process pr_rd;

end architecture rtl;
