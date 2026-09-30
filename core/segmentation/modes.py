"""Mode-specific engines: PersonSegmentation, ObjectSegmentation, FaceSegmentation, custom.

They wrap any backend SegmentationEngine and only add *prompt policy*: when the user has not clicked
anything yet, Person/Face mode auto-detect a box with lightweight OpenCV detectors (HOG people
detector / Haar frontal-face cascade) and hand it to the backend as the initial prompt.
Detector quality is modest; the user's clicks always take priority.
"""
from __future__ import annotations

from typing import List, Optional

import cv2
import numpy as np

from .base import MaskProposal, ModelInfo, Prompts, SegmentationEngine


class _Wrapper(SegmentationEngine):
    mode = "custom"

    def __init__(self, backend: SegmentationEngine):
        self.backend = backend

    def info(self) -> ModelInfo:
        return self.backend.info()

    def load(self, providers: Optional[List[str]] = None) -> None:
        self.backend.load(providers)

    def unload(self) -> None:
        self.backend.unload()

    def set_image(self, image_bgr: np.ndarray) -> None:
        self.backend.set_image(image_bgr)

    def predict(self, prompts: Prompts) -> MaskProposal:
        return self.backend.predict(prompts)


class ObjectSegmentation(_Wrapper):
    mode = "object"


class CustomSegmentation(_Wrapper):
    mode = "custom"


class PersonSegmentation(_Wrapper):
    mode = "person"

    def __init__(self, backend: SegmentationEngine):
        super().__init__(backend)
        self._hog = None
        try:                                    # OpenCV 5.x removed HOG: Person mode then just needs a click
            hog = cv2.HOGDescriptor()
            hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            self._hog = hog
        except (AttributeError, cv2.error):
            pass

    def auto_prompts(self, image_bgr: np.ndarray) -> Optional[Prompts]:
        if self._hog is None:
            return None
        h, w = image_bgr.shape[:2]
        s = min(1.0, 640.0 / max(h, w))
        small = cv2.resize(image_bgr, None, fx=s, fy=s) if s < 1 else image_bgr
        rects, weights = self._hog.detectMultiScale(small, winStride=(8, 8), padding=(8, 8), scale=1.05)
        if len(rects) == 0:
            return None
        i = int(np.argmax([r[2] * r[3] * max(float(wt), 0.1) for r, wt in zip(rects, np.ravel(weights))]))
        x, y, rw, rh = [v / s for v in rects[i]]
        # HOG boxes are loose: shrink slightly and add the box centre as a positive point
        pad_x, pad_y = rw * 0.08, rh * 0.04
        box = (x + pad_x, y + pad_y, x + rw - pad_x, y + rh - pad_y)
        return Prompts(points=[((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, 1)], box=box)


class FaceSegmentation(_Wrapper):
    mode = "face"

    def __init__(self, backend: SegmentationEngine):
        super().__init__(backend)
        self._cascade = None
        try:
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            c = cv2.CascadeClassifier(path)
            if not c.empty():
                self._cascade = c
        except (AttributeError, cv2.error):     # CascadeClassifier / cv2.data missing (OpenCV 5.x): Face mode needs a click
            pass

    def auto_prompts(self, image_bgr: np.ndarray) -> Optional[Prompts]:
        if self._cascade is None:
            return None
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        faces = self._cascade.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))
        if len(faces) == 0:
            return None
        x, y, fw, fh = max(faces, key=lambda r: r[2] * r[3])
        # a face crop; expand a little so hair/ears are inside the box
        box = (x - 0.15 * fw, y - 0.30 * fh, x + 1.15 * fw, y + 1.10 * fh)
        return Prompts(points=[(x + fw / 2, y + fh / 2, 1)], box=box)


MODES = {"person": PersonSegmentation, "object": ObjectSegmentation,
         "face": FaceSegmentation, "custom": CustomSegmentation}


def wrap_for_mode(mode: str, backend: SegmentationEngine) -> SegmentationEngine:
    return MODES.get(mode.lower(), CustomSegmentation)(backend)
