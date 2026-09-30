"""Segmentation quality / temporal stability metrics used by the tests and benchmarks."""
from __future__ import annotations

from typing import Dict, List

import numpy as np


def iou(pred: np.ndarray, gt: np.ndarray, thr: float = 0.5) -> float:
    p, g = pred > thr, gt > thr
    u = np.logical_or(p, g).sum()
    return 1.0 if u == 0 else float(np.logical_and(p, g).sum() / u)


def boundary_f(pred: np.ndarray, gt: np.ndarray, tol: int = 2) -> float:
    """Boundary F-measure (DAVIS 'F'), tolerance in pixels."""
    import cv2
    def edge(m):
        b = (m > 0.5).astype(np.uint8)
        return b - cv2.erode(b, np.ones((3, 3), np.uint8))
    pe, ge = edge(pred), edge(gt)
    k = np.ones((2 * tol + 1, 2 * tol + 1), np.uint8)
    pd, gd = cv2.dilate(pe, k), cv2.dilate(ge, k)
    prec = (pe & gd).sum() / max(pe.sum(), 1)
    rec = (ge & pd).sum() / max(ge.sum(), 1)
    return 0.0 if prec + rec == 0 else float(2 * prec * rec / (prec + rec))


def flicker(preds: List[np.ndarray], gts: List[np.ndarray]) -> float:
    """Excess frame-to-frame alpha change relative to the ground truth's own change.

    0 = as stable as the truth. Higher = flicker / jitter. Measured on float alphas in 0..1.
    """
    ex = []
    for i in range(1, len(preds)):
        dp = np.abs(preds[i].astype(np.float32) - preds[i - 1].astype(np.float32)).mean()
        dg = np.abs(gts[i].astype(np.float32) - gts[i - 1].astype(np.float32)).mean()
        ex.append(max(0.0, dp - dg))
    return float(np.mean(ex)) if ex else 0.0


def area_jumps(preds: List[np.ndarray]) -> float:
    """Largest single-frame relative area change (detects sudden expansion/shrink)."""
    a = np.array([(p > 0.5).sum() for p in preds], np.float64) + 1.0
    return float(np.max(np.abs(np.diff(a)) / a[:-1])) if len(a) > 1 else 0.0


def summarize(preds: List[np.ndarray], gts: List[np.ndarray]) -> Dict[str, float]:
    ious = [iou(p, g) for p, g in zip(preds, gts)]
    fs = [boundary_f(p, g) for p, g in zip(preds, gts)]
    return {
        "iou_mean": float(np.mean(ious)), "iou_min": float(np.min(ious)),
        "f_mean": float(np.mean(fs)),
        "flicker": flicker(preds, gts), "max_area_jump": area_jumps(preds),
    }
