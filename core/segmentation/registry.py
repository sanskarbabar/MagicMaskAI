"""Engine registry: picks a backend for a quality tier from whatever models are installed."""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from .base import EngineError, SegmentationEngine
from .grabcut import GrabCutEngine
from .modes import wrap_for_mode
from .sam2_onnx import Sam2OnnxEngine

# quality tier -> preferred SAM 2 variants, best first
TIER_VARIANTS = {
    "draft": ["tiny", "small", "base_plus", "large"],
    "balanced": ["small", "tiny", "base_plus", "large"],
    "high": ["base_plus", "large", "small", "tiny"],
}


def model_dirs() -> List[str]:
    dirs = []
    env = os.environ.get("AICUTOUT_MODELS")
    if env:
        dirs.append(env)
    pd = os.environ.get("PROGRAMDATA")
    if pd:
        dirs.append(os.path.join(pd, "AICutout", "models"))
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.normpath(os.path.join(here, "..", "..", "models", "weights")))
    return dirs


def installed_variants() -> List[Tuple[str, str]]:
    out = []
    for d in model_dirs():
        for v in ("tiny", "small", "base_plus", "large"):
            if Sam2OnnxEngine(v, d).available():
                out.append((v, d))
    return out


def create_engine(mode: str = "custom", tier: str = "balanced",
                  providers: Optional[List[str]] = None,
                  allow_fallback: bool = True) -> Tuple[SegmentationEngine, List[str]]:
    """Return (engine, messages). Messages are user-presentable notes (fallbacks, missing models)."""
    msgs: List[str] = []
    have = [] if os.environ.get("AICUTOUT_ENGINE", "").lower() == "grabcut" else installed_variants()
    chosen = None
    for v in TIER_VARIANTS.get(tier, TIER_VARIANTS["balanced"]):
        for hv, d in have:
            if hv == v:
                chosen = (hv, d)
                break
        if chosen:
            break
    if chosen:
        wanted = TIER_VARIANTS[tier][0]
        if chosen[0] != wanted:
            msgs.append(f"Model '{wanted}' is not installed; using '{chosen[0]}' instead.")
        try:
            eng = Sam2OnnxEngine(*chosen)
            eng.load(providers)
            return wrap_for_mode(mode, eng), msgs
        except EngineError as e:
            if not allow_fallback:
                raise
            msgs.append(f"{e}\nFalling back to the classical (non-AI) engine.")
    else:
        if not allow_fallback:
            raise EngineError("No segmentation model installed.")
        msgs.append("No SAM 2 model found. Using the classical (non-AI) engine; quality will be limited.")
    eng = GrabCutEngine()
    eng.load()
    return wrap_for_mode(mode, eng), msgs


def plan_model(tier: str) -> Tuple[str, str]:
    """(model_id, model_version) that create_engine would pick for this tier, without loading it."""
    if os.environ.get("AICUTOUT_ENGINE", "").lower() != "grabcut":
        have = installed_variants()
        for v in TIER_VARIANTS.get(tier, TIER_VARIANTS["balanced"]):
            if any(hv == v for hv, _ in have):
                return f"sam2_hiera_{v}", "onnx-1"
    return "grabcut", "opencv"
