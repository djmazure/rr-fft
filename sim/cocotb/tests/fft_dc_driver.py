from cocotb.triggers import RisingEdge
from test_fft_core import _reset


async def dc_case(dut, vector):
    await _reset(dut)
    schedule = sum(shift << (1 + 2 * pair) for pair, shift in enumerate(vector["sch"]))
    dut.S_AXIS_CONFIG_TDATA.value = schedule | int(not vector.get("inverse", False))
    dut.S_AXIS_CONFIG_TVALID.value = 1
    await RisingEdge(dut.ACLK)
    dut.S_AXIS_CONFIG_TVALID.value = 0
    mask = (1 << 24) - 1
    word = ((vector["dc_im"] & mask) << 24) | (vector["dc_re"] & mask)
    width = len(dut.M_AXIS_DATA_TDATA) // 2
    result = []
    for beat in range(4 * 256):
        dut.S_AXIS_DATA_TVALID.value = 1
        dut.S_AXIS_DATA_TDATA.value = word if beat < 256 else 0
        await RisingEdge(dut.ACLK)
        if dut.M_AXIS_DATA_TVALID.value == 1:
            raw = int(dut.M_AXIS_DATA_TDATA.value)
            pair = []
            for offset in [0, width]:
                value = (raw >> offset) & ((1 << width) - 1)
                pair.append(value - (1 << width) if value >= (1 << (width - 1)) else value)
            assert int(dut.M_AXIS_DATA_TUSER.value) == len(result)
            result.append(pair)
            if len(result) == 256:
                break
    dut.S_AXIS_DATA_TVALID.value = 0
    assert len(result) == 256, f"FFT-REQ-030 missing output: {len(result)}/256"
    return result[0]
