"""Dense optical flow (OpenCV DIS, Apache-2.0) and mask warping."""
from __future__ import annotations

import threading

import cv2
import numpy as np

_PRESETS = {"draft": cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST,
            "balanced": cv2.DISOPTICAL_FLOW_PRESET_FAST,
            "high": cv2.DISOPTICAL_FLOW_PRESET_MEDIUM}


class FlowEstimator:
    def __init__(self, tier: str = "balanced"):
        self._dis = cv2.DISOpticalFlow_create(_PRESETS.get(tier, _PRESETS["balanced"]))
        self._lock = threading.Lock()
        self._grid = None

    def backward_flow(self, prev_bgr: np.ndarray, cur_bgr: np.ndarray) -> np.ndarray:
        """Flow F such that prev(x + F(x)) ~= cur(x), i.e. sampling map from cur -> prev."""
        a = cv2.cvtColor(cur_bgr, cv2.COLOR_BGR2GRAY)
        b = cv2.cvtColor(prev_bgr, cv2.COLOR_BGR2GRAY)
        with self._lock:
            return self._dis.calc(a, b, None)

    def warp(self, prev_map: np.ndarray, flow: np.ndarray) -> np.ndarray:
        """Warp a single/multi-channel map from the previous frame into the current frame."""
        h, w = flow.shape[:2]
        if self._grid is None or self._grid[0].shape != (h, w):
            xs, ys = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
            self._grid = (xs, ys)
        xs, ys = self._grid
        return cv2.remap(prev_map, xs + flow[..., 0], ys + flow[..., 1], cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    @staticmethod
    def magnitude(flow: np.ndarray) -> np.ndarray:
        return np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)


def robustify_flow(flow: np.ndarray, region: np.ndarray, tol_px: float = 3.0, tol_rel: float = 0.25,
                   max_samples: int = 4000, dilate_px: int = 15, prior_M=None, use_prior: bool = False):
    """Returns (flow, area_scale, M).

    area_scale = area(prev)/area(cur) predicted by the fitted model (1.0 if unknown); M = the 2x3 affine used.
    With use_prior=True (subject currently occluded/degraded) the fit is skipped and prior_M (the last good
    model, i.e. constant-velocity extrapolation) is used, because flow inside the region may be dominated by
    the occluder.
    """
    return _robustify(flow, region, tol_px, tol_rel, max_samples, dilate_px, prior_M, use_prior)


def _robustify(flow, region, tol_px, tol_rel, max_samples, dilate_px, prior_M, use_prior):
    """Replace flow vectors that disagree with the subject's dominant (affine) motion.

    Occluders, motion-blur smears and texture-less patches produce flow that follows something other than
    the subject. Inside/near the (warped) subject region we fit a similarity/affine model with RANSAC and
    replace vectors that deviate from it by more than tol_px + tol_rel*|model flow|. Small deviations
    (real articulation: arms, legs) are kept.
    """
    reg = (region > 0.5).astype(np.uint8)
    if reg.sum() < 200:
        return flow, 1.0, prior_M
    ys, xs = np.nonzero(reg)
    if xs.size > max_samples:
        sel = np.random.default_rng(0).choice(xs.size, max_samples, replace=False)
        xs, ys = xs[sel], ys[sel]
    if use_prior and prior_M is not None:
        M = prior_M
    else:
        src = np.stack([xs, ys], 1).astype(np.float32)
        dst = src + flow[ys, xs]
        M, _inl = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=2.0, maxIters=300)
    if M is None:
        return flow, 1.0, prior_M
    det = float(abs(M[0, 0] * M[1, 1] - M[0, 1] * M[1, 0]))
    det = det if 0.3 < det < 3.0 else 1.0
    h, w = flow.shape[:2]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    mx = M[0, 0] * gx + M[0, 1] * gy + M[0, 2] - gx
    my = M[1, 0] * gx + M[1, 1] * gy + M[1, 2] - gy
    model = np.stack([mx, my], -1)
    dev = np.sqrt(((flow - model) ** 2).sum(-1))
    tol = tol_px + tol_rel * np.sqrt((model ** 2).sum(-1))
    k = 2 * dilate_px + 1
    zone = cv2.dilate(reg, np.ones((k, k), np.uint8)) > 0
    bad = zone & (dev > tol)
    if not bad.any():
        return flow, det, M
    out = flow.copy()
    out[bad] = model[bad]
    return out, det, M
