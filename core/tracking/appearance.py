"""Colour appearance model of the tracked subject vs. its surroundings.

An independent evidence source for the tracker: optical flow and the segmentation model can both be
fooled by an occluder crossing the subject (they happily segment the occluder as a coherent object with
high confidence). A foreground/background colour-likelihood check notices when the mask contains pixels
that do not look like the subject.

The model is a pair of 8x8x8 BGR histograms (subject interior vs. a ring of background around it),
slowly updated from confident frames so it follows lighting changes.
"""
from __future__ import annotations

import cv2
import numpy as np

_BINS = 8


def _idx(img_bgr: np.ndarray) -> np.ndarray:
    q = (img_bgr >> 5).astype(np.int32)
    return (q[..., 0] * _BINS + q[..., 1]) * _BINS + q[..., 2]


class AppearanceModel:
    def __init__(self):
        self.fg = None
        self.bg = None

    @staticmethod
    def _hist(idx: np.ndarray, sel: np.ndarray) -> np.ndarray:
        h = np.bincount(idx[sel], minlength=_BINS ** 3).astype(np.float64)
        return h / max(h.sum(), 1.0)

    def _regions(self, alpha: np.ndarray):
        m = (alpha > 0.5).astype(np.uint8)
        hh, ww = m.shape
        k_in = max(3, int(0.012 * max(hh, ww))) | 1
        fg = cv2.erode(m, np.ones((k_in, k_in), np.uint8)) > 0
        if fg.sum() < 50:
            fg = m > 0
        k1 = max(5, int(0.03 * max(hh, ww))) | 1
        k2 = max(9, int(0.14 * max(hh, ww))) | 1
        near = cv2.dilate(m, np.ones((k1, k1), np.uint8)) > 0
        far = cv2.dilate(m, np.ones((k2, k2), np.uint8)) > 0
        bg = far & ~near
        if bg.sum() < 50:
            bg = ~near
        return fg, bg

    def fit(self, img_bgr: np.ndarray, alpha: np.ndarray) -> None:
        idx = _idx(img_bgr)
        fg, bg = self._regions(alpha)
        self.fg = self._hist(idx, fg)
        self.bg = self._hist(idx, bg)

    def update(self, img_bgr: np.ndarray, alpha: np.ndarray, rate: float = 0.08) -> None:
        if self.fg is None:
            return self.fit(img_bgr, alpha)
        idx = _idx(img_bgr)
        fg, bg = self._regions(alpha)
        self.fg = (1 - rate) * self.fg + rate * self._hist(idx, fg)
        self.bg = (1 - rate) * self.bg + rate * self._hist(idx, bg)

    def likelihood(self, img_bgr: np.ndarray) -> np.ndarray:
        """Per-pixel P(subject | colour) in 0..1 (0.5 where the colour is uninformative)."""
        idx = _idx(img_bgr)
        eps = 1e-4
        f = self.fg + eps
        b = self.bg + eps
        table = (f / (f + b)).astype(np.float32)
        return table[idx]

    def leak_fraction(self, img_bgr: np.ndarray, alpha: np.ndarray, thr: float = 0.12):
        """Fraction of mask pixels that clearly look like background, plus the smoothed likelihood map."""
        lik = cv2.GaussianBlur(self.likelihood(img_bgr), (0, 0), 1.5)
        inside = alpha > 0.5
        n = int(inside.sum())
        if n == 0:
            return 0.0, lik
        return float((lik[inside] < thr).sum() / n), lik
