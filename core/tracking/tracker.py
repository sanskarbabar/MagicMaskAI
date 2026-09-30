"""Propagate-and-refine mask tracker.

For every new frame the previous mask is *propagated* with optical flow (temporal continuity, handles
camera motion / zoom / rotation to the degree the flow does) and then *refined* by the segmentation
engine using prompts derived from the propagated mask (box + interior points + the mask itself as a
guide). This is intentionally not independent per-frame segmentation.

Two masks are carried from frame to frame:
  * ``prev``  the last output (what the user sees; may be partially occluded / degraded);
  * ``ref``   a *clean reference*: updated with the output only when the frame is confident, otherwise
              only motion-compensated. Prompts, the expected-area model and re-acquisition all use
              ``ref``, so an occlusion or a blurry patch cannot erode the subject model.

Reliability per frame combines the engine's own confidence with its agreement with the propagated
reference and with the area predicted from the subject's own motion. When the subject is lost
(occluded / left the frame) the tracker keeps motion-compensating ``ref``, retries re-acquisition every
frame, and flags the frames so the user can correct them.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

import cv2
import numpy as np

from core.segmentation.base import Prompts, SegmentationEngine
from core.temporal.stabilizer import StabilizerConfig, TemporalStabilizer
from .appearance import AppearanceModel
from .flow import FlowEstimator, robustify_flow


@dataclass
class TrackerConfig:
    low_conf: float = 0.55           # below -> frame flagged low-confidence
    lost_conf: float = 0.30          # below -> subject considered lost
    good_conf: float = 0.80          # at/above -> output becomes the new clean reference
    reacquire_conf: float = 0.72
    lost_area_frac: float = 0.0004   # mask area (fraction of frame) below which the subject is "gone"
    box_expand: float = 0.04
    n_positive: int = 3
    area_band_hi: float = 1.28       # allowed area vs motion-predicted area (growth beyond this is clamped)
    area_band_lo: float = 0.72       # shrink below this is flagged (may be a true occlusion), never inflated
    ref_ratio_lo: float = 0.90       # output redefines the clean reference only if its area is within this
    ref_ratio_hi: float = 1.22       # band of the motion-predicted area
    leak_frac: float = 0.22          # share of mask pixels that look like background -> trim + flag
    leak_thr: float = 0.12
    reacquire_after: int = 3         # degraded frames in a row before appearance search starts
    reacquire_ratio: Tuple[float, float] = (0.5, 1.7)


@dataclass
class FrameResult:
    idx: int
    alpha: np.ndarray            # float32 0..1, proxy resolution
    confidence: float
    low_confidence: bool = False
    lost: bool = False


def iou(a: np.ndarray, b: np.ndarray) -> float:
    A, B = a > 0.5, b > 0.5
    u = np.logical_or(A, B).sum()
    return float(np.logical_and(A, B).sum() / u) if u else 1.0


def mask_bbox(m: np.ndarray, thr: float = 0.5) -> Optional[Tuple[int, int, int, int]]:
    ys, xs = np.nonzero(m > thr)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def interior_points(m: np.ndarray, n: int):
    """n well-spread positive points deep inside the mask (distance-transform maxima with suppression)."""
    binary = (m > 0.5).astype(np.uint8)
    if binary.sum() == 0:
        return []
    dist = cv2.distanceTransform(binary, cv2.DIST_L2, 3)
    pts = []
    for _ in range(n):
        _, mx, _, loc = cv2.minMaxLoc(dist)
        if mx < 1.5:
            break
        pts.append((float(loc[0]), float(loc[1]), 1))
        cv2.circle(dist, loc, max(3, int(mx * 1.6)), 0, -1)
    return pts


class MaskTracker:
    def __init__(self, engine: SegmentationEngine, frames, tier: str = "balanced",
                 config: Optional[TrackerConfig] = None,
                 stabilizer: Optional[TemporalStabilizer] = None):
        self.engine = engine
        self.frames = frames                 # object with get(idx) -> BGR uint8
        self.cfg = config or TrackerConfig()
        self.flow = FlowEstimator(tier)
        self.stab = stabilizer or TemporalStabilizer(StabilizerConfig())
        self.appearance = AppearanceModel()
        self._centroids = []
        self._step_M = None

    def _velocity_model(self):
        """Constant-velocity translation prior (backward sampling model) from recent confident frames."""
        c = self._centroids[-6:]
        if len(c) < 2:
            return np.array([[1, 0, 0], [0, 1, 0]], np.float32)
        vx = (c[-1][0] - c[0][0]) / (len(c) - 1)
        vy = (c[-1][1] - c[0][1]) / (len(c) - 1)
        return np.array([[1, 0, -vx], [0, 1, -vy]], np.float32)

    def _reacquire(self, cur_img, ref_area: float):
        """Search the frame for a region that matches the subject's colour model and verify it with the engine."""
        lik = cv2.GaussianBlur(self.appearance.likelihood(cur_img), (0, 0), 2.0)
        cand = (lik > 0.65).astype(np.uint8)
        cand = cv2.morphologyEx(cand, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        cand = cv2.morphologyEx(cand, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
        if n <= 1:
            return None
        h, w = cand.shape
        order = sorted(range(1, n), key=lambda i: -stats[i, cv2.CC_STAT_AREA])[:3]
        best = None
        for i in order:
            a = stats[i, cv2.CC_STAT_AREA]
            if a < 0.25 * ref_area:
                continue
            comp = (labels == i).astype(np.float32)
            x0, y0 = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
            x1, y1 = x0 + stats[i, cv2.CC_STAT_WIDTH], y0 + stats[i, cv2.CC_STAT_HEIGHT]
            ex, ey = (x1 - x0) * 0.10, (y1 - y0) * 0.10
            prompts = Prompts(points=interior_points(comp, self.cfg.n_positive),
                              box=(max(0, x0 - ex), max(0, y0 - ey), min(w, x1 + ex), min(h, y1 + ey)))
            self.engine.set_image(cur_img)
            prop = self.engine.predict(prompts)
            area = float((prop.mask > 0.5).sum())
            leak, _ = self.appearance.leak_fraction(cur_img, prop.mask, self.cfg.leak_thr)
            r = area / max(ref_area, 1.0)
            lo, hi = self.cfg.reacquire_ratio
            if prop.confidence >= self.cfg.reacquire_conf and leak < 0.15 and lo <= r <= hi:
                score = prop.confidence - leak
                if best is None or score > best[0]:
                    best = (score, prop.mask, prop.confidence)
        return None if best is None else (best[1], best[2])

    # ------------------------------------------------------------------ one step
    def _step(self, prev_img, cur_img, prev, ref, lost: bool, degraded: bool = False):
        """Returns (alpha_new, conf, lost, flow_mag, warped_prev, warped_ref, area_ratio)."""
        f32 = np.float32
        flow = self.flow.backward_flow(prev_img, cur_img)
        raw_ref = self.flow.warp(ref.astype(f32), flow)
        flow, _det, M = robustify_flow(flow, raw_ref, prior_M=self._velocity_model(), use_prior=degraded)
        self._step_M = M
        mag = self.flow.magnitude(flow)
        warped_ref = self.flow.warp(ref.astype(f32), flow)
        warped_prev = self.flow.warp(prev.astype(f32), flow)
        h, w = warped_ref.shape
        min_area = self.cfg.lost_area_frac * h * w

        box = mask_bbox(warped_ref)
        if box is None or (warped_ref > 0.5).sum() < min_area:
            return warped_ref * 0, 0.0, True, mag, warped_prev, warped_ref, 0.0

        x0, y0, x1, y1 = box
        ex, ey = (x1 - x0) * self.cfg.box_expand, (y1 - y0) * self.cfg.box_expand
        prompts = Prompts(
            points=interior_points(warped_ref, self.cfg.n_positive),
            box=(max(0, x0 - ex), max(0, y0 - ey), min(w, x1 + ex), min(h, y1 + ey)),
            mask=warped_ref,
        )
        self.engine.set_image(cur_img)
        prop = self.engine.predict(prompts)
        new = prop.mask
        agree = iou(new, warped_ref)
        a_new = float((new > 0.5).sum())
        expected = float((warped_ref > 0.5).sum()) + 1e-6
        conf = 0.4 * prop.confidence + 0.6 * min(1.0, agree / 0.8)
        if a_new < min_area:
            conf = min(conf, 0.1)

        ratio = a_new / expected
        if ratio > self.cfg.area_band_hi:
            # growth beyond what the subject's motion explains = leak into the background: clamp to a thin band
            k = (max(3, int(0.012 * max(h, w)))) | 1
            zone = cv2.dilate((warped_ref > 0.5).astype(np.uint8), np.ones((k, k), np.uint8)).astype(f32)
            new = new * zone
            conf = min(conf * 0.6, 0.5)
        elif ratio < self.cfg.area_band_lo:
            # shrink: could be a true occlusion. Keep the engine's (visible-part) mask, flag for review.
            conf = min(conf * 0.7, 0.5)

        # appearance gate: does the mask contain pixels that clearly do not look like the subject?
        leak, lik = self.appearance.leak_fraction(cur_img, new, self.cfg.leak_thr)
        if leak > self.cfg.leak_frac:
            new = new * (lik > self.cfg.leak_thr).astype(f32)
            conf = min(conf * 0.6, 0.45)
            ratio = float((new > 0.5).sum()) / expected

        if lost:
            if conf >= self.cfg.reacquire_conf and agree > 0.3:
                return new, conf, False, mag, warped_prev, warped_ref, ratio
            return new * 0, min(conf, 0.2), True, mag, warped_prev, warped_ref, ratio

        if agree < 0.35:
            new = 0.7 * warped_ref + 0.3 * new
            conf = min(conf, 0.45)
        return new, conf, conf < self.cfg.lost_conf, mag, warped_prev, warped_ref, ratio

    # ------------------------------------------------------------------ propagate
    def propagate(self, start_idx: int, start_alpha: np.ndarray, direction: int, stop_idx: int,
                  cancel: Optional[threading.Event] = None) -> Iterator[FrameResult]:
        """Yield results for frames start_idx+dir .. stop_idx (inclusive), in tracking order."""
        assert direction in (1, -1)
        n = len(self.frames)
        stop_idx = max(0, min(n - 1, stop_idx))
        idx = start_idx
        prev_img = self.frames.get(idx)
        prev = start_alpha.astype(np.float32)
        ref = prev.copy()
        self._centroids = []
        degraded = False
        streak = 0
        ref_area = float((prev > 0.5).sum())
        ys0, xs0 = np.nonzero(prev > 0.5)
        if xs0.size:
            self._centroids.append((float(xs0.mean()), float(ys0.mean())))
        if (prev > 0.5).any():
            self.appearance.fit(prev_img, prev)
        lost = False
        while True:
            idx += direction
            if (direction == 1 and idx > stop_idx) or (direction == -1 and idx < stop_idx):
                return
            if cancel is not None and cancel.is_set():
                return
            cur_img = self.frames.get(idx)
            new, conf, lost_now, mag, warped_prev, warped_ref, ratio = self._step(prev_img, cur_img, prev, ref, lost, degraded)
            if lost_now:
                alpha = new
            else:
                alpha = self.stab.stabilize(new, warped_prev if conf >= self.cfg.low_conf else None, mag, conf)
            lost = lost_now
            if (lost_now or conf < self.cfg.low_conf) and streak >= self.cfg.reacquire_after and ref_area > 100:
                found = self._reacquire(cur_img, ref_area)
                if found is not None:
                    alpha, conf = found[0].astype(np.float32), min(0.85, found[1])
                    lost_now = lost = False
                    warped_ref = alpha
                    ratio = 1.0
                    self._centroids = []
            # clean reference: only confident frames redefine it, otherwise it is just motion-compensated
            ref_ok = (not lost_now and conf >= self.cfg.good_conf and (alpha > 0.5).any()
                      and self.cfg.ref_ratio_lo <= ratio <= self.cfg.ref_ratio_hi)
            degraded = not ref_ok
            if ref_ok:
                ys1, xs1 = np.nonzero(alpha > 0.5)
                self._centroids.append((float(xs1.mean()), float(ys1.mean())))
                ref = alpha
                self.appearance.update(cur_img, alpha)
            else:
                ref = warped_ref
            streak = streak + 1 if (not ref_ok) else 0
            if ref_ok:
                ref_area = float((alpha > 0.5).sum())
            low = conf < self.cfg.low_conf or (not lost_now and ratio < self.cfg.ref_ratio_lo * 0.95)
            yield FrameResult(idx, alpha.astype(np.float32), float(conf), low or lost_now, lost_now)
            prev_img, prev = cur_img, alpha
