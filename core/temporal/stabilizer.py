"""Temporal stabilisation of a tracked alpha sequence.

Goals: kill flicker / edge jitter / sudden growth or collapse, *without* lagging behind fast motion.

Per frame:
  1. previous final alpha is warped into the current frame with optical flow;
  2. per-pixel blend weight = strength * exp(-(flow_magnitude / motion_sigma)^2) * confidence_factor
     -> strong smoothing where the image is static, almost none where it moves fast (no trailing lag);
  3. area jump limiter: if the mask suddenly grows/shrinks beyond the allowed ratio, the new mask is pulled
     toward the warped previous one instead of being accepted outright;
  4. small disconnected islands with no support in the warped previous mask are removed.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class StabilizerConfig:
    strength: float = 0.6          # max weight given to the warped previous alpha
    motion_sigma: float = 3.0      # px of flow at which smoothing has faded to ~37%
    max_area_ratio: float = 1.45   # allowed frame-to-frame area growth
    min_area_ratio: float = 0.65   # allowed frame-to-frame area shrink
    island_frac: float = 0.004     # islands smaller than this fraction of the largest are dropped
    low_conf_relax: float = 0.5    # confidence below which smoothing weight is reduced


def area(a: np.ndarray) -> float:
    return float((a > 0.5).sum())


def remove_islands(alpha: np.ndarray, support: np.ndarray | None, frac: float) -> np.ndarray:
    binary = (alpha > 0.5).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if n <= 2:
        return alpha
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = areas.max()
    keep = np.zeros(n, bool)
    keep[0] = False
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        if a >= frac * largest:
            keep[i] = True
        elif support is not None and (support[labels == i] > 0.5).mean() > 0.5:
            keep[i] = True          # small but continuing from the previous frame (e.g. a hand)
    keep_map = keep[labels]
    # soften: remove the dropped pixels plus a 1-px halo of alpha that belonged to them
    drop = (binary > 0) & ~keep_map
    if drop.any():
        drop = cv2.dilate(drop.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        alpha = alpha.copy()
        alpha[drop & ~cv2.dilate(keep_map.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)] = 0
    return alpha


class TemporalStabilizer:
    def __init__(self, config: StabilizerConfig | None = None):
        self.cfg = config or StabilizerConfig()

    def stabilize(self, new_alpha: np.ndarray, prev_alpha_warped: np.ndarray | None,
                  flow_mag: np.ndarray | None, confidence: float) -> np.ndarray:
        cfg = self.cfg
        a = new_alpha.astype(np.float32)
        if prev_alpha_warped is None:
            return remove_islands(a, None, cfg.island_frac)
        pw = prev_alpha_warped.astype(np.float32)

        # area jump limiter
        a_new, a_prev = area(a), area(pw)
        if a_prev > 50:
            ratio = a_new / max(a_prev, 1.0)
            if ratio > cfg.max_area_ratio or ratio < cfg.min_area_ratio:
                # pull toward the (motion-compensated) previous mask
                over = ratio / cfg.max_area_ratio if ratio > 1 else cfg.min_area_ratio / max(ratio, 1e-3)
                pull = float(np.clip(1.0 - 1.0 / over, 0.3, 0.9))
                a = pull * pw + (1 - pull) * a

        w = cfg.strength
        if confidence < cfg.low_conf_relax:
            w *= max(confidence / cfg.low_conf_relax, 0.2)
        if flow_mag is not None:
            wmap = w * np.exp(-(flow_mag / cfg.motion_sigma) ** 2)
        else:
            wmap = np.full(a.shape, w * 0.5, np.float32)
        out = wmap * pw + (1.0 - wmap) * a
        return remove_islands(np.clip(out, 0, 1), pw, cfg.island_frac)
