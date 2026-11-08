-------------------------------------------------------------------------------
-- File        : rr_fft_sdf_pair.vhd
-- Description : One radix-2^2 stage pair: BF2I, BF2II, twiddle and scale.
-- Standard    : VHDL-2008
-- SPDX-FileCopyrightText: 2026 Daniel J. Mazure
-- SPDX-License-Identifier: MIT
-------------------------------------------------------------------------------
--
-- Dataflow (block size G_L, counting valid input beats mod G_L):
--   BF2I  (delay G_L/2): first half fill/drain, second half butterfly
--         emitting a+b downstream and storing a-b.
--   BF2II (delay G_L/4): quarters q0 fill/drain(b3_prev), q1 butterfly
--         (emit b0=A+B, store b2=A-B), q2 drain(b2)/fill, q3 butterfly
--         with the trivial rotation (emit b1=C-(+/-j)D... precisely
--         b1 = C + rot(D), store b3 = C - rot(D), rot = -j fwd / +j inv).
--   Twiddle: output-quarter exponent schedule q1->0 (bypass), q2->2n,
--         q3->n, q0->3n against the W_{G_L} ROM; INV conjugates the ROM
--         word. ONE truncation per non-trivial multiply: >> (TW-1+sch),
--         bypass >> sch, then two's-complement wrap to C_DW — exactly
--         the oracle's _cmult/_post composition.
--
-- Config (fwd + schedule) is PER-BEAT SIDEBAND, stamped at the frame
-- boundary by the top and travelling with the data through the delay
-- lines — no global frame counter, per docs/ARCHITECTURE.md doors.
--
-- Contracts: FFT-REQ-001 (scaled fwd bit-exact), FFT-REQ-004 (config at
-- frame boundary only — guaranteed by the sideband scheme).

library ieee;
  use ieee.std_logic_1164.all;
  use ieee.numeric_std.all;

library work;
  use work.rr_fft_pkg.all;

entity rr_fft_sdf_pair is
  generic (
    G_L       : natural := 256;   -- block size at this pair (multiple of 4)
    G_PAIR    : natural := 0;     -- pair index (selects its sch bits)
    G_WIN     : natural := C_DW;  -- input width (f_wplan(G_PAIR))
    G_WOUT    : natural := C_DW;  -- output width (f_wplan(G_PAIR + 1))
    G_SCALED  : boolean := true;  -- scaled: shift+wrap; unscaled: grow
    G_BF2_DSP : boolean := false; -- BF2II adders in DSP48E2 (needs G_L >= 16)
    -- twiddle ROM: "distributed" (LUTs) or "block" (one block RAM, same
    -- latency, FFT-REQ-040)
    G_TW_ROM_STYLE : string := "distributed"
  );
  port (
    CLK       : in  std_logic;
    RSTN      : in  std_logic;
    EN        : in  std_logic;
    RE_IN     : in  signed(G_WIN - 1 downto 0);
    IM_IN     : in  signed(G_WIN - 1 downto 0);
    VALID_IN  : in  std_logic;
    CFG_IN    : in  std_logic_vector(C_CFG_W - 1 downto 0);
    RE_OUT    : out signed(G_WOUT - 1 downto 0);
    IM_OUT    : out signed(G_WOUT - 1 downto 0);
    VALID_OUT : out std_logic;
    CFG_OUT   : out std_logic_vector(C_CFG_W - 1 downto 0)
  );
end entity rr_fft_sdf_pair;

architecture rtl of rr_fft_sdf_pair is

  function f_max1 (n : natural) return positive is
  begin
    if n < 1 then
      return 1;
    end if;
    return n;
  end function f_max1;

  constant C_D1    : natural := G_L / 2;
  constant C_D2    : natural := G_L / 4;
  constant C_LOG2L : natural := f_clog2(G_L);

  constant C_ROM : t_tw_rom(0 to 3 * G_L / 4 - 1) := f_twiddle_rom(G_L);

  -- ── BF2I ────────────────────────────────────────────────────────
  -- Both BF2I adders are DSP48E2 ALUs, inferred: P = C +/- A:B with the
  -- C and A:B input registers, a registered sign (ALUMODE) and the P
  -- register. Operand gating is a synchronous reset of the input
  -- register, so no fabric LUT sits in the data path. Every register
  -- of the two DSPs advances on valid beats only (data-pushed), so the
  -- result of beat j is in P after beat j+1. The feedback line is two
  -- beats shorter than G_L/2: the input registers and P are its first
  -- two stages.
  subtype t_w1 is signed(G_WIN downto 0);          -- +1 bit growth
  constant C_DL1 : natural := C_D1 - 2;            -- SRL part of the line
  type t_line1 is array (0 to f_max1(C_DL1) - 1) of t_w1;
  signal line1Re_r, line1Im_r : t_line1;
  -- Configuration of the previous block. The configuration is constant
  -- over a frame and every block (L beats) lies inside one frame, so a
  -- drained sample carries the configuration of the previous block and
  -- no per-sample configuration delay line is needed.
  signal pcfg1_r   : std_logic_vector(C_CFG_W - 1 downto 0);
  signal cnt1_r    : unsigned(C_LOG2L - 1 downto 0);
  signal primed1_r : std_logic;
  -- s1 DSP: s1 = a + (bf ? b : 0); line DSP: (bf ? a : 0) -/+ b
  signal b1sCRe_r, b1sCIm_r, b1sAbRe_r, b1sAbIm_r : t_w1;
  signal b1lCRe_r, b1lCIm_r, b1lAbRe_r, b1lAbIm_r : t_w1;
  signal b1lSub_r : std_logic;
  signal b1sPRe_r, b1sPIm_r, b1lPRe_r, b1lPIm_r : t_w1;
  attribute use_dsp : string;
  attribute use_dsp of b1sPRe_r : signal is "yes";
  attribute use_dsp of b1sPIm_r : signal is "yes";
  attribute use_dsp of b1lPRe_r : signal is "yes";
  attribute use_dsp of b1lPIm_r : signal is "yes";
  signal b1Pv_r     : std_logic;                       -- beat j yields an output
  signal b1Pcfg_r   : std_logic_vector(C_CFG_W - 1 downto 0);
  signal s1Re, s1Im : t_w1;
  signal s1Valid_r  : std_logic;
  signal s1Cfg_r    : std_logic_vector(C_CFG_W - 1 downto 0);

  -- ── BF2II ───────────────────────────────────────────────────────
  subtype t_w2 is signed(G_WIN + 1 downto 0);      -- +2 bits total
  -- with G_BF2_DSP the DSP input registers and P are the first two
  -- stages of the feedback line, as in BF2I
  function f_dl2 (dsp : boolean) return natural is
  begin
    if dsp then
      return C_D2 - 2;
    end if;
    return C_D2;
  end function f_dl2;
  constant C_DL2 : natural := f_dl2(G_BF2_DSP);
  type t_line2 is array (0 to f_max1(C_DL2) - 1) of t_w2;
  signal line2Re_r, line2Im_r : t_line2;
  -- configuration of the previous block, as pcfg1_r: q0 drains the
  -- previous block, q2 drains q1 of the current block
  signal pcfg2_r        : std_logic_vector(C_CFG_W - 1 downto 0);
  signal cnt2_r         : unsigned(C_LOG2L - 1 downto 0);
  signal primedQ1_r     : std_logic;
  signal primedQ3_r     : std_logic;
  signal s2Re_r, s2Im_r : t_w2;
  signal s2Valid_r      : std_logic;
  signal s2Cfg_r        : std_logic_vector(C_CFG_W - 1 downto 0);
  signal s2M_r          : unsigned(1 downto 0);     -- exponent factor 0/1/2/3
  -- n = beat index within the quarter (cnt2_r mod G_L/4). Held at the full
  -- cnt2_r width and masked, NOT sliced to C_LOG2L-2 bits: in the last pair
  -- (G_L = 4) that slice is a null range and to_integer() of it warns on
  -- every enabled cycle (FFT-P2.10). The masked-off bits are constant 0.
  constant C_NMASK    : unsigned(C_LOG2L - 1 downto 0)
            := to_unsigned(C_D2 - 1, C_LOG2L);
  signal s2N_r         : unsigned(C_LOG2L - 1 downto 0);
  -- G_BF2_DSP: s2 DSP = a +/- (odd ? u : 0); line DSP = (odd ? a : 0) +/- u
  signal b2sCRe_r, b2sCIm_r, b2sAbRe_r, b2sAbIm_r : t_w2;
  signal b2lCRe_r, b2lCIm_r, b2lAbRe_r, b2lAbIm_r : t_w2;
  signal b2sSubRe_r, b2sSubIm_r, b2lSubRe_r, b2lSubIm_r : std_logic;
  signal b2sPRe_r, b2sPIm_r, b2lPRe_r, b2lPIm_r : t_w2;
  attribute use_dsp of b2sPRe_r : signal is "yes";
  attribute use_dsp of b2sPIm_r : signal is "yes";
  attribute use_dsp of b2lPRe_r : signal is "yes";
  attribute use_dsp of b2lPIm_r : signal is "yes";
  signal b2Pv_r   : std_logic;
  signal b2Pcfg_r : std_logic_vector(C_CFG_W - 1 downto 0);
  signal b2Pm_r   : unsigned(1 downto 0);
  signal b2Pn_r   : unsigned(C_LOG2L - 1 downto 0);

  -- ── twiddle ROM + cascaded multiplier (s3, s4, c1..c5, out) ────
  -- s3: ROM read. s4: ROM output register, INV conjugation, and the
  -- bypass (W^0) replaced by the unity twiddle 2^(TW-1) + 0j, so every
  -- beat takes the same multiply and the output needs no bypass mux.
  -- The twiddle is TW+1 bits wide: it holds +1.0 exactly.
  signal s3Re_r, s3Im_r : t_w2;
  signal s3Valid_r      : std_logic;
  signal s3Cfg_r        : std_logic_vector(C_CFG_W - 1 downto 0);
  attribute rom_style : string;

  signal s4Re_r, s4Im_r : t_w2;
  signal s4Valid_r      : std_logic;
  signal s4Cfg_r        : std_logic_vector(C_CFG_W - 1 downto 0);
  signal s4Wr_r, s4Wi_r : signed(C_TW downto 0);

  -- G_TW_ROM_STYLE = "block": the s4 word is stored, not computed. Each
  -- word holds (wi, wr) at TW+1 bits each, address = inv & k over two
  -- halves of G_L words: W^0 is stored as the unity 2^(TW-1) + 0j and
  -- the inverse half holds the exact conjugate. s3 is the block RAM
  -- read and s4 a plain register of the word, so no fabric logic sits
  -- between the RAM and the multiplier operands and the latency equals
  -- the LUT ROM's. Vivado 2024.1 merges s4 into the DSP48E2 A input
  -- registers, not into the RAM output register (Synth 8-7052); the
  -- RAM-to-DSP path has no logic level.
  subtype t_twb is std_logic_vector(2 * C_TW + 1 downto 0);
  type t_twb_rom is array (natural range <>) of t_twb;

  function f_twiddle_rom_blk (l : natural) return t_twb_rom is
    constant C_SRC : t_tw_rom(0 to 3 * l / 4 - 1) := f_twiddle_rom(l);
    variable v_rom : t_twb_rom(0 to 2 * l - 1) := (others => (others => '0'));
    variable v_wr, v_wi : signed(C_TW downto 0);
  begin
    for h in 0 to 1 loop                       -- 0: forward, 1: inverse
      for k in C_SRC'range loop
        if k = 0 then
          v_wr := to_signed(2 ** (C_TW - 1), C_TW + 1);  -- +1.0
          v_wi := (others => '0');
        else
          v_wr := resize(signed(C_SRC(k)(C_TW - 1 downto 0)), C_TW + 1);
          v_wi := resize(signed(C_SRC(k)(2 * C_TW - 1 downto C_TW)),
                         C_TW + 1);
          if h = 1 then
            v_wi := -v_wi;                 -- INV: exact conjugate
          end if;
        end if;
        v_rom(h * l + k) := std_logic_vector(v_wi)       -- rr-lint-disable-line RR-ARITH-002
                          & std_logic_vector(v_wr);
      end loop;
    end loop;
    return v_rom;
  end function f_twiddle_rom_blk;

  -- Each real output is a four-DSP48E2 cascade with the twiddle on the
  -- 27-bit A port and the data split across the 18-bit B port
  -- (x = x_hi * 2^17 + x_lo, x_lo unsigned):
  --   re: P1 = wr*xr_lo; P2 = P1 - wi*xi_lo;
  --       P3 = (P2 >> 17) + wr*xr_hi; P4 = P3 - wi*xi_hi
  --   im: P1 = wi*xr_lo; P2 = P1 + wr*xi_lo;
  --       P3 = (P2 >> 17) + wi*xr_hi; P4 = P3 + wr*xi_hi
  -- The cascade has fixed signs: a per-beat ALUMODE splits it into
  -- separate multiply and add DSPs (measured), so the inverse
  -- conjugates the 25-bit twiddle in fabric, at s4.
  -- re*w = P4 * 2^17 + (P2 mod 2^17) exactly, so the single truncation
  -- >> (TW-1+sch) of the oracle equals P4 >> (TW-1-17+sch).
  -- Stage k of the cascade multiplies operands delayed by k-1 cycles
  -- (systolic); every product has its M register and every sum its P
  -- register, so all DSP pipeline registers are in use.
  constant C_SPLIT : natural := 17;
  constant C_MULT  : boolean := G_L > 4;   -- the last pair only bypasses
  constant C_PW    : natural := 48;
  subtype t_tw  is signed(C_TW downto 0);
  subtype t_p   is signed(C_PW - 1 downto 0);
  subtype t_m   is signed(C_TW + 18 downto 0);     -- (TW+1) x 18
  type t_twd is array (1 to 3) of t_tw;
  type t_xd  is array (1 to 3) of t_w2;
  type t_mv  is array (1 to 4) of t_m;
  type t_pv  is array (1 to 4) of t_p;
  signal dWr_r, dWi_r : t_twd;
  signal dXr_r, dXi_r : t_xd;
  signal mRe_r, mIm_r : t_mv;
  signal pRe_r, pIm_r : t_pv;
  -- s4 -> last P register, cycles: 5 through the cascade, 4 in the
  -- last pair (no multiply)
  function f_cdepth (mult : boolean) return natural is
  begin
    if mult then
      return 5;
    end if;
    return 4;
  end function f_cdepth;
  constant C_CDEPTH : natural := f_cdepth(G_L > 4);
  type t_cfgd is array (1 to C_CDEPTH) of std_logic_vector(C_CFG_W - 1 downto 0);
  signal cValid_r : std_logic_vector(1 to C_CDEPTH);
  signal cCfg_r   : t_cfgd;
  -- this pair's scale shift, decoded from valid beats only: the cfg
  -- sideband rides unreset SRL delay lines and is 'U' on non-valid beats
  -- until the first frame arrives (FFT-P2.10).
  signal cSh_r : unsigned(1 downto 0);

  -- x + y + ci as ONE adder: append an LSB of 1 to x and ci to y, add,
  -- drop the LSB. A separate "+ ci" term synthesises a second adder.
  function f_addc (x, y : signed; ci : std_logic) return signed is
    variable v_s : signed(x'length downto 0);
  begin
    v_s := (x & '1') + (y & ci);
    return v_s(x'length downto 1);
  end function f_addc;

  function f_lo (x : t_w2) return signed is
  begin
    return signed('0' & std_logic_vector(x(C_SPLIT - 1 downto 0)));
  end function f_lo;

  function f_hi (x : t_w2) return signed is
  begin
    return resize(x(x'high downto C_SPLIT), 18);
  end function f_hi;

begin

  -- ── BF2I: fill/drain first half, butterfly second half ──────────
  --   s1   = a + (bf ? b : 0)          (a = delayed sample, b = incoming)
  --   line = (bf ? a : 0) - b if bf, else 0 + b
  s1Re <= b1sPRe_r;
  s1Im <= b1sPIm_r;

  pr_bf1 : process (CLK) is
    variable v_bf       : std_logic;
    variable v_ar, v_ai : t_w1;
  begin
    if rising_edge(CLK) then
      if RSTN = '0' then
        cnt1_r    <= (others => '0');
        primed1_r <= '0';
        b1Pv_r    <= '0';
        s1Valid_r <= '0';
      elsif EN = '1' then
        s1Valid_r <= '0';
        if VALID_IN = '1' then
          v_bf := cnt1_r(C_LOG2L - 1);          -- second half
          if C_DL1 > 0 then
            v_ar := line1Re_r(f_max1(C_DL1) - 1);
            v_ai := line1Im_r(f_max1(C_DL1) - 1);
          else
            v_ar := b1lPRe_r;
            v_ai := b1lPIm_r;
          end if;
          -- DSP input registers (beat j)
          b1sCRe_r <= v_ar;
          b1sCIm_r <= v_ai;
          if v_bf = '1' then
            b1sAbRe_r <= resize(RE_IN, G_WIN + 1);
            b1sAbIm_r <= resize(IM_IN, G_WIN + 1);
            b1lCRe_r  <= v_ar;
            b1lCIm_r  <= v_ai;
          else
            b1sAbRe_r <= (others => '0');
            b1sAbIm_r <= (others => '0');
            b1lCRe_r  <= (others => '0');
            b1lCIm_r  <= (others => '0');
          end if;
          b1lAbRe_r <= resize(RE_IN, G_WIN + 1);
          b1lAbIm_r <= resize(IM_IN, G_WIN + 1);
          b1lSub_r  <= v_bf;
          -- DSP P registers (result of beat j-1)
          b1sPRe_r <= b1sCRe_r + b1sAbRe_r;
          b1sPIm_r <= b1sCIm_r + b1sAbIm_r;
          if b1lSub_r = '1' then
            b1lPRe_r <= b1lCRe_r - b1lAbRe_r;
            b1lPIm_r <= b1lCIm_r - b1lAbIm_r;
          else
            b1lPRe_r <= b1lCRe_r + b1lAbRe_r;
            b1lPIm_r <= b1lCIm_r + b1lAbIm_r;
          end if;
          if C_DL1 > 0 then
            line1Re_r <= b1lPRe_r & line1Re_r(0 to f_max1(C_DL1) - 2);
            line1Im_r <= b1lPIm_r & line1Im_r(0 to f_max1(C_DL1) - 2);
          end if;
          -- output of beat j-1 leaves with this beat
          s1Valid_r <= b1Pv_r;
          s1Cfg_r   <= b1Pcfg_r;
          if v_bf = '1' then
            b1Pcfg_r  <= CFG_IN;
            b1Pv_r    <= '1';
            primed1_r <= '1';
          else
            b1Pcfg_r <= pcfg1_r;
            b1Pv_r   <= primed1_r;
          end if;
          if cnt1_r = G_L - 1 then
            pcfg1_r <= CFG_IN;               -- last beat of the block
          end if;
          cnt1_r   <= cnt1_r + 1;
        end if;
      end if;
    end if;
  end process pr_bf1;

  -- ── BF2II: quarters; q1/q3 butterfly, q3 with the +/-j rotation ─
  -- The rotation is an operand swap u (q3: re <-> im) and a sign:
  --   b = (neg_r ? -u_r : u_r, neg_i ? -u_i : u_i),
  --   neg_r = q3 and inv, neg_i = q3 and fwd  (-j fwd: (im, -re);
  --   +j inv: (-im, re)). Each output is ONE adder with gated operands:
  --   s2   = a + ((odd ? u : 0) xor neg) + neg          (odd: a + b)
  --   line = (odd ? a : 0) + (u xor sl) + sl,  sl = odd and not neg
  --                                                  (odd: a - b, else u)
  GEN_BF2_IN_FABRIC : if not G_BF2_DSP generate
    pr_bf2 : process (CLK) is
      variable v_q                            : unsigned(1 downto 0);
      variable v_odd, v_q3                    : std_logic;
      variable v_nr, v_ni, v_sr, v_si         : std_logic;
      variable v_ur, v_ui                     : t_w2;
      variable v_ar, v_ai                     : t_w2;
      variable v_gu_r, v_gu_i, v_ga_r, v_ga_i : t_w2;
    begin
      if rising_edge(CLK) then
        if RSTN = '0' then
          cnt2_r     <= (others => '0');
          primedQ1_r <= '0';
          primedQ3_r <= '0';
          s2Valid_r  <= '0';
          -- written only on valid beats but read by pr_rom every
          -- enabled cycle: reset so the ROM address is never 'U'
          -- between reset release and the first beat (FFT-P2.10)
          s2M_r <= (others => '0');
          s2N_r <= (others => '0');
        elsif EN = '1' then
          s2Valid_r <= '0';
          if s1Valid_r = '1' then
            v_q   := cnt2_r(C_LOG2L - 1 downto C_LOG2L - 2);
            v_odd := v_q(0);
            v_q3  := v_q(1) and v_q(0);
            v_nr  := v_q3 and not s1Cfg_r(0);
            v_ni  := v_q3 and s1Cfg_r(0);
            v_sr  := v_odd and not v_nr;
            v_si  := v_odd and not v_ni;
            if v_q3 = '1' then
              v_ur := resize(s1Im, G_WIN + 2);
              v_ui := resize(s1Re, G_WIN + 2);
            else
              v_ur := resize(s1Re, G_WIN + 2);
              v_ui := resize(s1Im, G_WIN + 2);
            end if;
            v_ar := line2Re_r(C_DL2 - 1);
            v_ai := line2Im_r(C_DL2 - 1);
            v_gu_r := v_ur and (v_ur'range => v_odd);
            v_gu_i := v_ui and (v_ui'range => v_odd);
            v_ga_r := v_ar and (v_ar'range => v_odd);
            v_ga_i := v_ai and (v_ai'range => v_odd);
            s2Re_r <= f_addc(v_ar, v_gu_r xor (v_gu_r'range => v_nr), v_nr);
            s2Im_r <= f_addc(v_ai, v_gu_i xor (v_gu_i'range => v_ni), v_ni);
            line2Re_r <= f_addc(v_ga_r, v_ur xor (v_ur'range => v_sr), v_sr)
                  & line2Re_r(0 to C_DL2 - 2);
            line2Im_r <= f_addc(v_ga_i, v_ui xor (v_ui'range => v_si), v_si)
                  & line2Im_r(0 to C_DL2 - 2);
            if v_odd = '1' then
              s2Cfg_r   <= s1Cfg_r;
              s2Valid_r <= '1';
              if v_q = "01" then
                primedQ1_r <= '1';
              else
                primedQ3_r <= '1';
              end if;
            else
              if v_q = "10" then
                s2Cfg_r <= s1Cfg_r;
              else
                s2Cfg_r <= pcfg2_r;
              end if;
              if v_q = "10" then
                s2Valid_r <= primedQ1_r;   -- draining b2
              else
                s2Valid_r <= primedQ3_r;   -- draining b3 (prev blk)
              end if;
            end if;
            if cnt2_r = G_L - 1 then
              pcfg2_r <= s1Cfg_r;            -- last beat of the block
            end if;
            -- output-beat twiddle schedule: q1->0, q2->2n, q3->n, q0->3n
            case v_q is
              when "01"   => s2M_r <= "00";
              when "10"   => s2M_r <= "10";
              when "11"   => s2M_r <= "01";
              when others => s2M_r <= "11";
            end case;
            s2N_r <= cnt2_r and C_NMASK;
            cnt2_r <= cnt2_r + 1;
          end if;
        end if;
      end if;
    end process pr_bf2;
  end generate GEN_BF2_IN_FABRIC;

  GEN_BF2_IN_DSP : if G_BF2_DSP generate
    s2Re_r <= b2sPRe_r;
    s2Im_r <= b2sPIm_r;

    pr_bf2d : process (CLK) is
      variable v_q                    : unsigned(1 downto 0);
      variable v_odd, v_q3            : std_logic;
      variable v_nr, v_ni, v_sr, v_si : std_logic;
      variable v_ur, v_ui             : t_w2;
      variable v_ar, v_ai             : t_w2;
    begin
      if rising_edge(CLK) then
        if RSTN = '0' then
          cnt2_r     <= (others => '0');
          primedQ1_r <= '0';
          primedQ3_r <= '0';
          s2Valid_r  <= '0';
          b2Pv_r     <= '0';
          -- read by pr_rom every enabled cycle (FFT-P2.10)
          s2M_r  <= (others => '0');
          s2N_r  <= (others => '0');
          b2Pm_r <= (others => '0');
          b2Pn_r <= (others => '0');
        elsif EN = '1' then
          s2Valid_r <= '0';
          if s1Valid_r = '1' then
            v_q   := cnt2_r(C_LOG2L - 1 downto C_LOG2L - 2);
            v_odd := v_q(0);
            v_q3  := v_q(1) and v_q(0);
            v_nr  := v_q3 and not s1Cfg_r(0);
            v_ni  := v_q3 and s1Cfg_r(0);
            v_sr  := v_odd and not v_nr;
            v_si  := v_odd and not v_ni;
            if v_q3 = '1' then
              v_ur := resize(s1Im, G_WIN + 2);
              v_ui := resize(s1Re, G_WIN + 2);
            else
              v_ur := resize(s1Re, G_WIN + 2);
              v_ui := resize(s1Im, G_WIN + 2);
            end if;
            if C_DL2 > 0 then
              v_ar := line2Re_r(f_max1(C_DL2) - 1);
              v_ai := line2Im_r(f_max1(C_DL2) - 1);
            else
              v_ar := b2lPRe_r;
              v_ai := b2lPIm_r;
            end if;
            -- DSP input registers (beat j)
            b2sCRe_r <= v_ar;
            b2sCIm_r <= v_ai;
            b2lAbRe_r <= v_ur;
            b2lAbIm_r <= v_ui;
            if v_odd = '1' then
              b2sAbRe_r <= v_ur;
              b2sAbIm_r <= v_ui;
              b2lCRe_r  <= v_ar;
              b2lCIm_r  <= v_ai;
            else
              b2sAbRe_r <= (others => '0');
              b2sAbIm_r <= (others => '0');
              b2lCRe_r  <= (others => '0');
              b2lCIm_r  <= (others => '0');
            end if;
            b2sSubRe_r <= v_nr;
            b2sSubIm_r <= v_ni;
            b2lSubRe_r <= v_sr;
            b2lSubIm_r <= v_si;
            -- DSP P registers (result of beat j-1)
            if b2sSubRe_r = '1' then
              b2sPRe_r <= b2sCRe_r - b2sAbRe_r;
            else
              b2sPRe_r <= b2sCRe_r + b2sAbRe_r;
            end if;
            if b2sSubIm_r = '1' then
              b2sPIm_r <= b2sCIm_r - b2sAbIm_r;
            else
              b2sPIm_r <= b2sCIm_r + b2sAbIm_r;
            end if;
            if b2lSubRe_r = '1' then
              b2lPRe_r <= b2lCRe_r - b2lAbRe_r;
            else
              b2lPRe_r <= b2lCRe_r + b2lAbRe_r;
            end if;
            if b2lSubIm_r = '1' then
              b2lPIm_r <= b2lCIm_r - b2lAbIm_r;
            else
              b2lPIm_r <= b2lCIm_r + b2lAbIm_r;
            end if;
            if C_DL2 > 0 then
              line2Re_r <= b2lPRe_r & line2Re_r(0 to f_max1(C_DL2) - 2);
              line2Im_r <= b2lPIm_r & line2Im_r(0 to f_max1(C_DL2) - 2);
            end if;
            -- output of beat j-1 leaves with this beat
            s2Valid_r <= b2Pv_r;
            s2Cfg_r   <= b2Pcfg_r;
            s2M_r     <= b2Pm_r;
            s2N_r     <= b2Pn_r;
            if v_odd = '1' then
              b2Pcfg_r <= s1Cfg_r;
              b2Pv_r   <= '1';
              if v_q = "01" then
                primedQ1_r <= '1';
              else
                primedQ3_r <= '1';
              end if;
            else
              if v_q = "10" then
                b2Pcfg_r <= s1Cfg_r;
              else
                b2Pcfg_r <= pcfg2_r;
              end if;
              if v_q = "10" then
                b2Pv_r <= primedQ1_r;   -- draining b2
              else
                b2Pv_r <= primedQ3_r;   -- draining b3 (prev blk)
              end if;
            end if;
            if cnt2_r = G_L - 1 then
              pcfg2_r <= s1Cfg_r;            -- last beat of the block
            end if;
            case v_q is
              when "01"   => b2Pm_r <= "00";
              when "10"   => b2Pm_r <= "10";
              when "11"   => b2Pm_r <= "01";
              when others => b2Pm_r <= "11";
            end case;
            b2Pn_r <= cnt2_r and C_NMASK;
            cnt2_r <= cnt2_r + 1;
          end if;
        end if;
      end if;
    end process pr_bf2d;
  end generate GEN_BF2_IN_DSP;

  -- ── twiddle ROM in LUTs (G_TW_ROM_STYLE = "distributed") ───────
  GEN_TW_DIST : if G_TW_ROM_STYLE /= "block" generate
    signal s3W_r         : std_logic_vector(2 * C_TW - 1 downto 0);
    -- Twiddle ROM in LUTs: the reorder RAM is the only block RAM of
    -- the core (technical.resources.bram).
    attribute rom_style of s3W_r : signal is "distributed";
    signal s3Bypass_r    : std_logic;
  begin

    -- ── twiddle ROM read (1 cycle) ──────────────────────────────────
    pr_rom : process (CLK) is
      variable v_addr : natural range 0 to 3 * G_L / 4 - 1;
    begin
      if rising_edge(CLK) then
        if RSTN = '0' then
          s3Valid_r <= '0';
        elsif EN = '1' then
          s3Valid_r <= s2Valid_r;
          s3Re_r    <= s2Re_r;
          s3Im_r    <= s2Im_r;
          s3Cfg_r   <= s2Cfg_r;
          -- k = n * m as shift-and-add on the 2-bit m (no multiplier,
          -- RR-ARITH-002). n < G_L/4, so 2n and 3n fit the address.
          case s2M_r is
            when "00"   => v_addr := 0;
            when "01"   => v_addr := to_integer(s2N_r);
            when "10"   => v_addr := to_integer(shift_left(s2N_r, 1));
            when others => v_addr := to_integer(s2N_r)
                                   + to_integer(shift_left(s2N_r, 1));
          end case;
          s3W_r   <= C_ROM(v_addr);
          if v_addr = 0 then
            s3Bypass_r <= '1';
          else
            s3Bypass_r <= '0';
          end if;
        end if;
      end if;
    end process pr_rom;

    -- ── s4: ROM output register, INV conjugation, unity bypass ─────
    pr_romreg : process (CLK) is
      variable v_wi : t_tw;
    begin
      if rising_edge(CLK) then
        if RSTN = '0' then
          s4Valid_r <= '0';
        elsif EN = '1' then
          s4Valid_r <= s3Valid_r;
          s4Re_r    <= s3Re_r;
          s4Im_r    <= s3Im_r;
          s4Cfg_r   <= s3Cfg_r;
          v_wi := resize(signed(s3W_r(2 * C_TW - 1 downto C_TW)), C_TW + 1);
          if s3Cfg_r(0) = '0' then
            v_wi := -v_wi;                     -- INV: exact conjugate
          end if;
          if s3Bypass_r = '1' then
            s4Wr_r <= to_signed(2 ** (C_TW - 1), C_TW + 1);  -- +1.0
            s4Wi_r <= (others => '0');
          else
            s4Wr_r <= resize(signed(s3W_r(C_TW - 1 downto 0)), C_TW + 1);
            s4Wi_r <= v_wi;
          end if;
        end if;
      end if;
    end process pr_romreg;
  end generate GEN_TW_DIST;

  -- ── twiddle ROM in block RAM (G_TW_ROM_STYLE = "block") ─────────
  GEN_TW_BLOCK : if G_TW_ROM_STYLE = "block" generate
    constant C_ROMB : t_twb_rom(0 to 2 * G_L - 1) := f_twiddle_rom_blk(G_L);
    signal s3Wb_r : t_twb;
    attribute rom_style of s3Wb_r : signal is "block";
  begin

    -- s3: block RAM read at address inv & k (the inv bit is the
    -- direction this beat's s4 conjugates for: s3Cfg_r(0) = '0')
    pr_rom : process (CLK) is
      variable v_addr : natural range 0 to 3 * G_L / 4 - 1;
      variable v_inv  : std_logic;
    begin
      if rising_edge(CLK) then
        if RSTN = '0' then
          s3Valid_r <= '0';
        elsif EN = '1' then
          s3Valid_r <= s2Valid_r;
          s3Re_r    <= s2Re_r;
          s3Im_r    <= s2Im_r;
          s3Cfg_r   <= s2Cfg_r;
          case s2M_r is
            when "00"   => v_addr := 0;
            when "01"   => v_addr := to_integer(s2N_r);
            when "10"   => v_addr := to_integer(shift_left(s2N_r, 1));
            when others => v_addr := to_integer(s2N_r)
                                   + to_integer(shift_left(s2N_r, 1));
          end case;
          -- '1' only for a defined '0': s2Cfg_r is 'U' before the
          -- first beat and must not reach to_integer (FFT-P2.10)
          if s2Cfg_r(0) = '0' then
            v_inv := '1';
          else
            v_inv := '0';
          end if;
          s3Wb_r <= C_ROMB(to_integer(v_inv & to_unsigned(v_addr, C_LOG2L)));
        end if;
      end if;
    end process pr_rom;

    -- s4: register of the stored word (no logic)
    pr_romreg : process (CLK) is
    begin
      if rising_edge(CLK) then
        if RSTN = '0' then
          s4Valid_r <= '0';
        elsif EN = '1' then
          s4Valid_r <= s3Valid_r;
          s4Re_r    <= s3Re_r;
          s4Im_r    <= s3Im_r;
          s4Cfg_r   <= s3Cfg_r;
          s4Wr_r    <= signed(s3Wb_r(C_TW downto 0));
          s4Wi_r    <= signed(s3Wb_r(2 * C_TW + 1 downto C_TW + 1));
        end if;
      end if;
    end process pr_romreg;

  end generate GEN_TW_BLOCK;

  -- ── cascaded complex multiply: operand delays, M and P registers ─
  -- RR-ARITH-002 waived per line: every product is registered (M) and
  -- every operand is a register (A/B), the DSP48E2 pipeline.
  pr_cmult : process (CLK) is
  begin
    if rising_edge(CLK) then
      if EN = '1' then
        dWr_r <= s4Wr_r & dWr_r(1 to 2);
        dWi_r <= s4Wi_r & dWi_r(1 to 2);
        dXr_r <= s4Re_r & dXr_r(1 to 2);
        dXi_r <= s4Im_r & dXi_r(1 to 2);
        if C_MULT then
          mRe_r(1) <= s4Wr_r   * f_lo(s4Re_r);    -- rr-lint-disable-line RR-ARITH-002
          mRe_r(2) <= dWi_r(1) * f_lo(dXi_r(1));  -- rr-lint-disable-line RR-ARITH-002
          mRe_r(3) <= dWr_r(2) * f_hi(dXr_r(2));  -- rr-lint-disable-line RR-ARITH-002
          mRe_r(4) <= dWi_r(3) * f_hi(dXi_r(3));  -- rr-lint-disable-line RR-ARITH-002
          mIm_r(1) <= s4Wi_r   * f_lo(s4Re_r);    -- rr-lint-disable-line RR-ARITH-002
          mIm_r(2) <= dWr_r(1) * f_lo(dXi_r(1));  -- rr-lint-disable-line RR-ARITH-002
          mIm_r(3) <= dWi_r(2) * f_hi(dXr_r(2));  -- rr-lint-disable-line RR-ARITH-002
          mIm_r(4) <= dWr_r(3) * f_hi(dXi_r(3));  -- rr-lint-disable-line RR-ARITH-002
          pRe_r(1) <= resize(mRe_r(1), C_PW);
          pRe_r(2) <= pRe_r(1) - resize(mRe_r(2), C_PW);
          pRe_r(3) <= shift_right(pRe_r(2), C_SPLIT) + resize(mRe_r(3), C_PW);
          pRe_r(4) <= pRe_r(3) - resize(mRe_r(4), C_PW);
          pIm_r(1) <= resize(mIm_r(1), C_PW);
          pIm_r(2) <= pIm_r(1) + resize(mIm_r(2), C_PW);
          pIm_r(3) <= shift_right(pIm_r(2), C_SPLIT) + resize(mIm_r(3), C_PW);
          pIm_r(4) <= pIm_r(3) + resize(mIm_r(4), C_PW);
        else
          -- last pair (G_L = 4): every beat is W^0, x * 2^(TW-1)
          -- expressed directly, one stage shorter (C_CDEPTH)
          pRe_r(4) <= shift_left(resize(dXr_r(3), C_PW), C_TW - 1 - C_SPLIT);
          pIm_r(4) <= shift_left(resize(dXi_r(3), C_PW), C_TW - 1 - C_SPLIT);
        end if;
      end if;
    end if;
  end process pr_cmult;

  -- ── control sideband alongside the cascade ──────────────────────
  pr_ctl : process (CLK) is
  begin
    if rising_edge(CLK) then
      if RSTN = '0' then
        cValid_r <= (others => '0');
        cSh_r    <= (others => '0');
      elsif EN = '1' then
        cValid_r <= s4Valid_r & cValid_r(1 to C_CDEPTH - 1);
        cCfg_r   <= s4Cfg_r & cCfg_r(1 to C_CDEPTH - 1);
        if G_SCALED and cValid_r(C_CDEPTH - 1) = '1' then
          cSh_r <= unsigned(cCfg_r(C_CDEPTH - 1)(2 * G_PAIR + 2 downto 2 * G_PAIR + 1));
        elsif not G_SCALED then
          cSh_r <= (others => '0');
        end if;
      end if;
    end if;
  end process pr_ctl;

  -- ── output: single truncation + scale + two's-complement wrap ───
  pr_out : process (CLK) is
    variable v_r, v_i : t_p;
  begin
    if rising_edge(CLK) then
      if RSTN = '0' then
        VALID_OUT <= '0';
      elsif EN = '1' then
        v_r := shift_right(pRe_r(4), C_TW - 1 - C_SPLIT + to_integer(cSh_r));
        v_i := shift_right(pIm_r(4), C_TW - 1 - C_SPLIT + to_integer(cSh_r));
        if G_SCALED then
          RE_OUT <= v_r(G_WOUT - 1 downto 0);  -- two's-comp wrap
          IM_OUT <= v_i(G_WOUT - 1 downto 0);
        else
          -- value fits G_WOUT by the oracle-proven width plan
          RE_OUT <= resize(v_r, G_WOUT);
          IM_OUT <= resize(v_i, G_WOUT);
        end if;
        CFG_OUT   <= cCfg_r(C_CDEPTH);
        VALID_OUT <= cValid_r(C_CDEPTH);
      end if;
    end if;
  end process pr_out;

end architecture rtl;
