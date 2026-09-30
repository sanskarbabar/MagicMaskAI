"""SegmentationEngine abstraction.

Every model (SAM 2, MobileSAM, GrabCut fallback, future models) implements this small interface so
the tracker, service and plugin never depend on a specific network.

Coordinates are always in *proxy pixel space* of the image passed to set_image().
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np


class EngineError(RuntimeError):
    """User-presentable engine failure (missing model, out of memory, ...)."""


@dataclass(frozen=True)
class ModelInfo:
    id: str
    version: str
    license: str
    vram_mb: int = 0
    supports_points: bool = True
    supports_box: bool = True
    supports_mask_prompt: bool = False


@dataclass
class Prompts:
    """points: (x, y, label) with label 1 = include, 0 = exclude."""
    points: List[Tuple[float, float, int]] = field(default_factory=list)
    box: Optional[Tuple[float, float, float, float]] = None
    mask: Optional[np.ndarray] = None      # previous / guide mask, float32 0..1 at image size

    def is_empty(self) -> bool:
        return not self.points and self.box is None and self.mask is None


@dataclass
class MaskProposal:
    mask: np.ndarray            # float32 probability-like 0..1, image size
    confidence: float           # 0..1, the engine's own belief
    extra: dict = field(default_factory=dict)


class SegmentationEngine(ABC):
    @abstractmethod
    def info(self) -> ModelInfo: ...

    @abstractmethod
    def load(self, providers: Optional[List[str]] = None) -> None: ...

    def unload(self) -> None:
        pass

    @abstractmethod
    def set_image(self, image_bgr: np.ndarray) -> None:
        """Encode / cache the image; subsequent predict() calls refer to it."""

    @abstractmethod
    def predict(self, prompts: Prompts) -> MaskProposal: ...

    def auto_prompts(self, image_bgr: np.ndarray) -> Optional[Prompts]:
        """Optional mode-specific automatic prompt (e.g. person / face detection)."""
        return None
