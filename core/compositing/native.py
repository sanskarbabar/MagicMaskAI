"""ctypes binding to the native core (the same C++ code the OFX plugin runs)."""
from __future__ import annotations

import ctypes as C
import os
import sys
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

MODES = {"original": 0, "mask": 1, "alpha": 2, "cutout": 3, "composite": 4, "overlay": 5, "checkerboard": 6}


class _Params(C.Structure):
    _fields_ = [("edgeShift", C.c_float), ("feather", C.c_float), ("smooth", C.c_float), ("refine", C.c_float),
                ("decontaminate", C.c_float), ("spill", C.c_float), ("cleanBlack", C.c_float), ("cleanWhite", C.c_float),
                ("quality", C.c_int), ("mode", C.c_int), ("premultiply", C.c_int), ("checkerSize", C.c_int), ("bgKind", C.c_int),
                ("bgColor", C.c_float * 3), ("overlayColor", C.c_float * 3), ("overlayOpacity", C.c_float),
                ("renderScale", C.c_float)]


@dataclass
class EdgeSettings:
    edge_shift: float = 0.0
    feather: float = 0.0
    smooth: float = 0.0
    refine: float = 0.5
    decontaminate: float = 0.0
    spill: float = 0.0
    clean_black: float = 0.03
    clean_white: float = 0.97
    quality: int = 1
    mode: str = "cutout"
    premultiply: bool = False
    checker_size: int = 32
    bg_kind: str = "checker"          # checker | solid
    bg_color: Tuple[float, float, float] = (0.0, 0.6, 0.0)
    overlay_color: Tuple[float, float, float] = (1.0, 0.1, 0.1)
    overlay_opacity: float = 0.55
    render_scale: float = 1.0


_lib = None


def find_dll() -> Optional[str]:
    cands = []
    if os.environ.get("AICUTOUT_CORE_DLL"):
        cands.append(os.environ["AICUTOUT_CORE_DLL"])
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(os.path.dirname(sys.executable), "aicutout_core.dll"))
        cands.append(os.path.join(getattr(sys, "_MEIPASS", ""), "aicutout_core.dll"))
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.normpath(os.path.join(here, "..", "..", "build", "native", "aicutout_core.dll")))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def lib():
    global _lib
    if _lib is None:
        path = find_dll()
        if not path:
            raise RuntimeError("aicutout_core.dll not found. Build it with scripts/build_native.ps1")
        _lib = C.CDLL(path)
        _lib.acut_version.restype = C.c_int
        _lib.acut_crc32.restype = C.c_uint32
        _lib.acut_crc32.argtypes = [C.c_char_p, C.c_size_t]
        _lib.acut_decode_frame.argtypes = [C.c_char_p, C.c_size_t, C.c_void_p, C.c_size_t, C.POINTER(C.c_int),
                                           C.POINTER(C.c_int), C.POINTER(C.c_float), C.POINTER(C.c_int)]
        _lib.acut_render.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.c_int, C.c_void_p, C.c_int, C.c_int,
                                     C.POINTER(_Params)]
    return _lib


def native_decode(data: bytes, max_pixels: int = 1 << 26):
    buf = np.empty(max_pixels, np.uint8)
    w, h, fl = C.c_int(), C.c_int(), C.c_int()
    conf = C.c_float()
    rc = lib().acut_decode_frame(data, len(data), buf.ctypes.data, max_pixels, C.byref(w), C.byref(h), C.byref(conf), C.byref(fl))
    if rc != 0:
        return None
    return buf[: w.value * h.value].reshape(h.value, w.value).copy(), conf.value, fl.value


def render(src_rgba: np.ndarray, matte_u8: Optional[np.ndarray], s: EdgeSettings) -> np.ndarray:
    """src_rgba: float32 (H,W,4) top-down, straight alpha. Returns float32 (H,W,4)."""
    src = np.ascontiguousarray(src_rgba, dtype=np.float32)
    h, w = src.shape[:2]
    dst = np.empty_like(src)
    p = _Params(s.edge_shift, s.feather, s.smooth, s.refine, s.decontaminate, s.spill, s.clean_black, s.clean_white,
                s.quality, MODES[s.mode], int(s.premultiply), s.checker_size, 1 if s.bg_kind == "solid" else 0,
                (C.c_float * 3)(*s.bg_color),
                (C.c_float * 3)(*s.overlay_color), s.overlay_opacity, s.render_scale)
    if matte_u8 is not None:
        m = np.ascontiguousarray(matte_u8, dtype=np.uint8)
        rc = lib().acut_render(src.ctypes.data, dst.ctypes.data, w, h, m.ctypes.data, m.shape[1], m.shape[0], C.byref(p))
    else:
        rc = lib().acut_render(src.ctypes.data, dst.ctypes.data, w, h, None, 0, 0, C.byref(p))
    if rc != 0:
        raise RuntimeError("native render failed")
    return dst


def bgr8_to_rgba(img_bgr: np.ndarray) -> np.ndarray:
    rgb = img_bgr[..., ::-1].astype(np.float32) / 255.0
    a = np.ones(rgb.shape[:2] + (1,), np.float32)
    return np.concatenate([rgb, a], axis=-1)
