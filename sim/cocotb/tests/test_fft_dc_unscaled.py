from pathlib import Path
import sys

import cocotb
from cocotb.clock import Clock
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine.simulation import run_simulation
from sdk.cocotb_helpers import requires
from fft_dc_driver import dc_case


@cocotb.test()
@requires("FFT-REQ-030")
async def test_dc_unscaled_oracle(dut):
    fixture = Path(__file__).resolve().parents[3] / "sim/goldens/FFT-REQ-030.yml"
    cases = yaml.safe_load(fixture.read_text(encoding="utf-8"))["minted"]
    cases = [case for case in cases if case["vector"]["scaled"] is False]
    assert len(cases) >= 4
    cocotb.start_soon(Clock(dut.ACLK, 4, unit="ns").start())
    for index, case in enumerate(cases):
        actual = await dc_case(dut, case["vector"])
        expected = [case["golden"]["x0_re"], case["golden"]["x0_im"]]
        assert actual == expected, f"FFT-REQ-030 case {index} {case['vector']}: {actual} != {expected}"
    dut._log.info("FFT-REQ-030: %d unscaled DC cases passed", len(cases))


if __name__ == "__main__":
    rtl = Path(__file__).resolve().parents[3] / "rtl"
    run_simulation(
        top_level="rr_fft_top", module=Path(__file__).stem,
        generics={"G_HAS_SCALING": False},
        custom_libraries={"work": [str(rtl / name) for name in [
            "rr_fft_pkg.vhd", "rr_fft_sdf_pair.vhd", "rr_fft_reorder.vhd", "rr_fft_top.vhd"]]},
        waves=True, simulator="nvc",
    )
