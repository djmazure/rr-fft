"""Xilinx XFFT v9.1 bit-accurate C-model wrapper (cross-check tier only).

rr_fft's contract reference is `rr_fft_oracle.py` — this wrapper exists solely
for the SNR-equivalence tier (docs/ARCHITECTURE.md tier 2): demonstrating the
oracle (and later the RTL) is system-equivalent to the Xilinx xfft IP it
replaces. It is NEVER the bit-exactness reference.

The C model ships inside every Vivado install
(`data/ip/xilinx/xfft_v9_1/cmodel/xfft_v9_1_bitacc_cmodel_lin64.zip`); the
Xilinx redistributable binaries stay out of this repo — located on the bench,
extracted to a user cache on first use. ABI per `xfft_v9_1_bitacc_cmodel.h`.
The libs carry no rpath, so patchelf pins $ORIGIN so siblings resolve.
"""

from __future__ import annotations

import ctypes
import glob
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

_CMODEL_REL = "data/ip/xilinx/xfft_v9_1/cmodel/xfft_v9_1_bitacc_cmodel_lin64.zip"
_CACHE = Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "rr_fft" / "xfft_v9_1_cmodel"


def _find_vivado_root() -> Path:
    cands: list[str] = []
    env = os.environ.get("XILINX_VIVADO") or os.environ.get("VIVADO")
    if env:
        cands.append(env)
    cands += sorted(
        glob.glob("/tools/Xilinx/Vivado/*") + glob.glob("/opt/Xilinx/Vivado/*"),
        reverse=True,
    )
    for c in cands:
        if (Path(c) / _CMODEL_REL).is_file():
            return Path(c)
    raise FileNotFoundError(
        f"XFFT v9.1 C model not found — set $XILINX_VIVADO (needs {_CMODEL_REL})"
    )


def _ensure_cmodel() -> Path:
    ready = _CACHE / ".ready"
    if ready.is_file():
        return _CACHE
    _CACHE.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(_find_vivado_root() / _CMODEL_REL) as zf:
        zf.extractall(_CACHE)
    for stem, ver in (("libgmp.so", "libgmp.so.11"), ("libmpfr.so", "libmpfr.so.4")):
        link = _CACHE / stem
        if not link.exists() and (_CACHE / ver).is_file():
            link.symlink_to(ver)
    if not shutil.which("patchelf"):
        raise RuntimeError("patchelf required to relocate the XFFT C-model libs")
    for so in ("libIp_xfft_v9_1_bitacc_cmodel.so",
               "libIp_floating_point_v7_0_bitacc_cmodel.so"):
        subprocess.run(["patchelf", "--set-rpath", "$ORIGIN", str(_CACHE / so)],
                       check=True)
    ready.write_text("ok\n")
    return _CACHE


_c_int_p = ctypes.POINTER(ctypes.c_int)
_c_dbl_p = ctypes.POINTER(ctypes.c_double)


class _Generics(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in (
        "C_NFFT_MAX", "C_ARCH", "C_HAS_NFFT", "C_USE_FLT_PT",
        "C_INPUT_WIDTH", "C_TWIDDLE_WIDTH", "C_HAS_SCALING",
        "C_HAS_BFP", "C_HAS_ROUNDING", "C_NSSR", "C_SYSTOLICFFT_INV",
    )]


class _Inputs(ctypes.Structure):
    _fields_ = [
        ("nfft", ctypes.c_int),
        ("xn_re", _c_dbl_p), ("xn_re_size", ctypes.c_int),
        ("xn_im", _c_dbl_p), ("xn_im_size", ctypes.c_int),
        ("scaling_sch", _c_int_p), ("scaling_sch_size", ctypes.c_int),
        ("direction", ctypes.c_int),
    ]


class _Outputs(ctypes.Structure):
    _fields_ = [
        ("xk_re", _c_dbl_p), ("xk_re_size", ctypes.c_int),
        ("xk_im", _c_dbl_p), ("xk_im_size", ctypes.c_int),
        ("blk_exp", ctypes.c_int),
        ("overflow", ctypes.c_int),
    ]


_lib = None


def _load() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    d = _ensure_cmodel()
    for dep in ("libgmp.so.11", "libmpfr.so.4",
                "libIp_floating_point_v7_0_bitacc_cmodel.so"):
        ctypes.CDLL(str(d / dep), mode=ctypes.RTLD_GLOBAL)
    lib = ctypes.CDLL(str(d / "libIp_xfft_v9_1_bitacc_cmodel.so"),
                      mode=ctypes.RTLD_GLOBAL)
    lib.xilinx_ip_xfft_v9_1_create_state.argtypes = [_Generics]
    lib.xilinx_ip_xfft_v9_1_create_state.restype = ctypes.c_void_p
    lib.xilinx_ip_xfft_v9_1_destroy_state.argtypes = [ctypes.c_void_p]
    lib.xilinx_ip_xfft_v9_1_bitacc_simulate.argtypes = [
        ctypes.c_void_p, _Inputs, ctypes.POINTER(_Outputs)]
    lib.xilinx_ip_xfft_v9_1_bitacc_simulate.restype = ctypes.c_int
    _lib = lib
    return lib


@dataclass(frozen=True)
class XfftResult:
    re: list[int]
    im: list[int]
    blk_exp: int
    overflow: int


def run_xfft(re: list[int], im: list[int], *, input_width: int = 24,
             twiddle_width: int = 24, scaled: bool = True,
             scale_schedule: list[int] | None = None,
             inverse: bool = False) -> XfftResult:
    """Run the bit-accurate xfft (pipelined-streaming ARCH=3) on integer codes.

    Codes are signed `input_width`-bit values; the C model works in fractional
    reals, so codes scale by 2^(W-1) in/out. `scale_schedule` is per stage
    PAIR ((log2N+1)//2 entries, matching rr_fft_oracle) — default in scaled
    mode is the conservative all-2s (final entry 1 when log2N is odd), i.e.
    an overall 1/N. Unscaled mode ignores the schedule (C_HAS_SCALING=0).
    """
    lib = _load()
    n = len(re)
    nfft = n.bit_length() - 1
    if len(im) != n or (1 << nfft) != n:
        raise ValueError("re/im must be equal power-of-2 length")
    stages = (nfft + 1) // 2
    if scale_schedule is None:
        scale_schedule = [2] * stages
        if scaled and nfft % 2 == 1:
            scale_schedule[-1] = 1
    if len(scale_schedule) != stages:
        raise ValueError(f"scale_schedule needs {stages} entries")
    fp = float(1 << (input_width - 1))

    g = _Generics(
        C_NFFT_MAX=nfft, C_ARCH=3, C_HAS_NFFT=0, C_USE_FLT_PT=0,
        C_INPUT_WIDTH=input_width, C_TWIDDLE_WIDTH=twiddle_width,
        C_HAS_SCALING=1 if scaled else 0, C_HAS_BFP=0, C_HAS_ROUNDING=0,
        C_NSSR=1, C_SYSTOLICFFT_INV=0,
    )
    state = lib.xilinx_ip_xfft_v9_1_create_state(g)
    if not state:
        raise RuntimeError("xfft create_state returned NULL")
    try:
        xn_re = (ctypes.c_double * n)(*(v / fp for v in re))
        xn_im = (ctypes.c_double * n)(*(v / fp for v in im))
        sch_arr = (ctypes.c_int * stages)(*scale_schedule)
        inp = _Inputs(
            nfft=nfft,
            xn_re=xn_re, xn_re_size=n, xn_im=xn_im, xn_im_size=n,
            scaling_sch=sch_arr, scaling_sch_size=stages,
            direction=0 if inverse else 1,
        )
        xk_re = (ctypes.c_double * n)()
        xk_im = (ctypes.c_double * n)()
        out = _Outputs(xk_re=xk_re, xk_re_size=n, xk_im=xk_im, xk_im_size=n)
        rc = lib.xilinx_ip_xfft_v9_1_bitacc_simulate(state, inp, ctypes.byref(out))
        if rc != 0:
            raise RuntimeError(f"xfft simulate failed (rc={rc})")
        return XfftResult(
            re=[int(round(v * fp)) for v in xk_re],
            im=[int(round(v * fp)) for v in xk_im],
            blk_exp=out.blk_exp, overflow=out.overflow,
        )
    finally:
        lib.xilinx_ip_xfft_v9_1_destroy_state(state)
