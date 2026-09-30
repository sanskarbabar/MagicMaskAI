"""Every mode must construct and behave, whatever OpenCV build is installed (OpenCV 5 dropped HOG/Haar)."""
import cv2
import numpy as np
import pytest

from core.segmentation.base import EngineError, Prompts
from core.segmentation.grabcut import GrabCutEngine
from core.segmentation.modes import MODES, wrap_for_mode
from core.tracking.session import Session
from tests.synth import SCENARIOS, render, write_video


@pytest.mark.parametrize("mode", list(MODES))
def test_every_mode_constructs_and_predicts(mode):
    eng = wrap_for_mode(mode, GrabCutEngine())
    eng.load()
    img = cv2.imread("tests/data/groceries.jpg")
    eng.set_image(img)
    auto = eng.auto_prompts(img)            # may be None (no detector / nothing found) but must never raise
    assert auto is None or isinstance(auto, Prompts)
    p = eng.predict(Prompts(points=[(img.shape[1] * 0.5, img.shape[0] * 0.5, 1)]))
    assert p.mask.shape == img.shape[:2]


def test_person_and_face_analyze_without_clicks_never_crash(tmp_path):
    frames, _ = render(SCENARIOS["static"])
    video = str(tmp_path / "v.mp4"); write_video(video, frames)
    for mode in ("person", "face"):
        eng = wrap_for_mode(mode, GrabCutEngine()); eng.load()
        s = Session(video, str(tmp_path / mode), mode=mode, tier="draft", engine=eng)
        s.open()
        try:
            mask, conf = s.segment(0, [])
            assert mask.shape == s.frames.size[::-1]
        except EngineError as e:                                   # acceptable: nothing detected -> asks for a click
            assert "Click the subject" in str(e)
