"""End-to-end session workflow on a real video file, using the model-free GrabCut engine (fast, deterministic).
The SAM 2 path is exercised in test_sam.py when weights are installed."""
import numpy as np
import pytest

from core.masking.metrics import iou
from core.segmentation.base import EngineError
from core.segmentation.grabcut import GrabCutEngine
from core.tracking.session import Session
from tests.synth import SCENARIOS, render, write_video


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    d = tmp_path_factory.mktemp("clip")
    sc = SCENARIOS["static"]
    frames, gts = render(sc)
    path = str(d / "static.mp4")
    write_video(path, frames)
    return path, gts


def make_session(path, cache, tier="draft"):
    eng = GrabCutEngine(); eng.load()
    s = Session(path, str(cache), mode="custom", tier=tier, engine=eng)
    s.open()
    return s


def click_of(gt):
    ys, xs = np.nonzero(gt > 127)
    return [(float(xs.mean()), float(ys.mean()), 1)], (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))


def scaled(gt, s):
    import cv2
    w, h = s.frames.size
    return cv2.resize(gt, (w, h), interpolation=cv2.INTER_AREA)


def test_analyze_track_cache_and_correction(clip, tmp_path):
    path, gts = clip
    s = make_session(path, tmp_path)
    n = s.n_frames
    assert n == len(gts) and s.frames.size[0] <= 512

    # 1. analyze on frame 0 with a positive click + box, then confirm as keyframe
    g0 = scaled(gts[0], s)
    pts, box = click_of(g0)
    mask, conf = s.segment(0, pts, box)
    assert iou(mask, g0 / 255.0) > 0.6
    s.commit_keyframe(0, pts, box)

    # 2. track forward; every frame gets a cached matte + a confidence
    s.track("forward")
    assert s.progress.state == "done"
    assert s.matte.frames() == list(range(n))
    ious = [iou(s.alpha(i), scaled(gts[i], s) / 255.0) for i in range(n)]
    assert np.mean(ious) > 0.6

    # 3. an identical second track request reuses the cache (no work)
    s.track("forward")
    assert s.progress.message.startswith("Already tracked")

    # 4. a manual correction becomes a manual keyframe and is never overwritten by re-tracking
    mid = n // 2
    before = s.alpha(mid).copy()
    h, w = before.shape
    s.correct_frame(mid, add_polys=[[(w * 0.5, h * 0.5)]], remove_polys=[], brush=25)
    corrected = s.alpha(mid)
    assert corrected.sum() >= before.sum() - 1 and mid in s.keyframes and s.keyframes[mid].kind == "manual"
    s.track("both", recalc=True)
    assert np.allclose(s.alpha(mid), corrected, atol=1 / 255)      # untouched by re-tracking

    # 5. state survives re-opening the session (persistent cache)
    s2 = make_session(path, tmp_path)
    assert sorted(s2.keyframes) == sorted(s.keyframes)
    assert s2.matte.frames() == list(range(n))


def test_track_without_selection_gives_clear_error(clip, tmp_path):
    s = make_session(clip[0], tmp_path)
    with pytest.raises(EngineError, match="Select a subject"):
        s.track("both")


def test_missing_video_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        Session(str(tmp_path / "nope.mp4"), str(tmp_path), engine=GrabCutEngine()).open()


def test_cancel_stops_tracking(clip, tmp_path):
    path, gts = clip
    s = make_session(path, tmp_path / "c")
    g0 = scaled(gts[0], s); pts, box = click_of(g0)
    s.commit_keyframe(0, pts, box)
    seen = []
    orig = s._report
    def report(res, total, done, t0):
        orig(res, total, done, t0)
        seen.append(res.idx)
        if len(seen) == 3:
            s.cancel.set()
    s._report = report
    s.track("forward")
    assert s.progress.state == "cancelled" and len(s.matte.frames()) < s.n_frames
