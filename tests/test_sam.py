"""SAM 2 engine tests (skipped when weights are not installed)."""
import cv2
import numpy as np
import pytest

from core.segmentation.base import Prompts
from core.segmentation.registry import installed_variants
from core.segmentation.sam2_onnx import Sam2OnnxEngine
from gpu.devices import detect_hardware

pytestmark = pytest.mark.skipif(not installed_variants(), reason="no SAM 2 weights installed")

DATA = "tests/data"


@pytest.fixture(scope="module")
def engine():
    variant, d = installed_variants()[0]
    e = Sam2OnnxEngine(variant, d)
    e.load(detect_hardware().selected)
    return e


def test_click_gives_object_mask(engine):
    img = cv2.imread(f"{DATA}/truck.jpg")
    engine.set_image(img)
    p = engine.predict(Prompts(points=[(500, 375, 1)]))
    assert p.mask.shape == img.shape[:2] and p.confidence > 0.5
    assert 0.001 < (p.mask > 0.5).mean() < 0.6
    assert p.mask[375, 500] > 0.5                       # the clicked pixel is inside


def test_negative_point_removes_region(engine):
    img = cv2.imread(f"{DATA}/truck.jpg")
    engine.set_image(img)
    a = engine.predict(Prompts(points=[(500, 375, 1)])).mask > 0.5
    ys, xs = np.nonzero(a)
    # exclude a point at the far side of the mask: the mask must not grow toward it
    neg = (float(xs.max() - 5), float(ys[np.argmax(xs)]), 0)
    b = engine.predict(Prompts(points=[(500, 375, 1), neg])).mask > 0.5
    assert not b[int(neg[1]), int(neg[0])]


def test_box_prompt(engine):
    img = cv2.imread(f"{DATA}/truck.jpg")
    engine.set_image(img)
    p = engine.predict(Prompts(box=(75, 275, 1725, 850)))
    assert (p.mask > 0.5).mean() > 0.15                 # whole truck-sized object


def test_multiple_objects_are_separable(engine):
    img = cv2.imread(f"{DATA}/groceries.jpg")
    engine.set_image(img)
    h, w = img.shape[:2]
    m1 = engine.predict(Prompts(points=[(w * 0.25, h * 0.5, 1)])).mask > 0.5
    m2 = engine.predict(Prompts(points=[(w * 0.75, h * 0.5, 1)])).mask > 0.5
    assert m1.any() and m2.any() and (m1 & m2).sum() < 0.2 * min(m1.sum(), m2.sum())
