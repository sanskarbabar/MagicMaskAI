"""Classical fallback engine (OpenCV GrabCut). Needs no model weights.

Used when no neural model is installed or loadable, and by unit tests. Quality is far below SAM 2 — it
is a fallback, and the UI reports which engine is active.
"""
from __future__ import annotations

from typing import List, Optional

import cv2
import numpy as np

from .base import MaskProposal, ModelInfo, Prompts, SegmentationEngine


class GrabCutEngine(SegmentationEngine):
    WORK = 480   # long edge for GrabCut work image

    def __init__(self):
        self._img = None

    def info(self) -> ModelInfo:
        return ModelInfo("grabcut", "opencv", "Apache-2.0 (OpenCV)", 0, True, True, True)

    def load(self, providers: Optional[List[str]] = None) -> None:
        pass

    def set_image(self, image_bgr: np.ndarray) -> None:
        self._img = image_bgr

    def predict(self, prompts: Prompts) -> MaskProposal:
        img = self._img
        h, w = img.shape[:2]
        s = min(1.0, self.WORK / max(h, w))
        sw, sh = max(8, int(w * s)), max(8, int(h * s))
        small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)
        gc = np.full((sh, sw), cv2.GC_PR_BGD, np.uint8)

        if prompts.mask is not None:
            g = cv2.resize(prompts.mask.astype(np.float32), (sw, sh), interpolation=cv2.INTER_AREA)
            gc[g > 0.5] = cv2.GC_PR_FGD
            gc[g > 0.9] = cv2.GC_FGD
            # trust the interior and far exterior, let the band around the edge be re-estimated
            k = max(3, int(0.02 * max(sw, sh)))
            fg = cv2.erode((g > 0.5).astype(np.uint8), np.ones((k, k), np.uint8))
            bg = cv2.dilate((g > 0.5).astype(np.uint8), np.ones((3 * k, 3 * k), np.uint8)) == 0
            gc[fg > 0] = cv2.GC_FGD
            gc[bg] = cv2.GC_BGD
        elif prompts.box is not None:
            x0, y0, x1, y1 = [int(v * s) for v in prompts.box]
            gc[:] = cv2.GC_BGD
            gc[max(0, y0):min(sh, y1), max(0, x0):min(sw, x1)] = cv2.GC_PR_FGD
        else:
            # points only: a broad probable-foreground disc around the positives
            gc[:] = cv2.GC_PR_BGD
            for x, y, l in prompts.points:
                if l:
                    r = int(0.18 * max(sw, sh))
                    cv2.circle(gc, (int(x * s), int(y * s)), r, cv2.GC_PR_FGD, -1)

        rad = max(3, int(0.012 * max(sw, sh)))
        for x, y, l in prompts.points:
            cv2.circle(gc, (int(x * s), int(y * s)), rad, cv2.GC_FGD if l else cv2.GC_BGD, -1)

        if not (gc == cv2.GC_FGD).any() and not (gc == cv2.GC_PR_FGD).any():
            return MaskProposal(np.zeros((h, w), np.float32), 0.0)
        if not (gc == cv2.GC_BGD).any() and not (gc == cv2.GC_PR_BGD).any():
            return MaskProposal(np.ones((h, w), np.float32), 0.0)

        bgd = np.zeros((1, 65), np.float64)
        fgd = np.zeros((1, 65), np.float64)
        try:
            cv2.grabCut(small, gc, None, bgd, fgd, 4, cv2.GC_INIT_WITH_MASK)
        except cv2.error:
            return MaskProposal(np.zeros((h, w), np.float32), 0.0)
        m = ((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD)).astype(np.float32)
        m = cv2.GaussianBlur(m, (0, 0), 1.2)
        out = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
        area = float(out.mean())
        conf = 0.55 if 0.001 < area < 0.95 else 0.1
        return MaskProposal(out, conf, {"engine": "grabcut"})
