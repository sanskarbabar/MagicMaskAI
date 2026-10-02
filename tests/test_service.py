"""Service integration: real subprocess, real socket, real video."""
import os
import subprocess
import sys
import time

import numpy as np
import pytest

from inference.client import Client, ServiceError
from tests.synth import SCENARIOS, render, write_video


@pytest.fixture()
def service(tmp_path):
    env = dict(os.environ, AICUTOUT_STATE=str(tmp_path / "state"), AICUTOUT_CACHE=str(tmp_path / "cache"),
               AICUTOUT_ENGINE="grabcut")
    os.environ["AICUTOUT_STATE"] = env["AICUTOUT_STATE"]
    p = subprocess.Popen([sys.executable, "-m", "inference.server", "--idle-timeout", "0"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = Client(timeout=30)
    for _ in range(60):
        time.sleep(0.5)
        try:
            c.connect(); c.call("hello"); break
        except (ServiceError, OSError):
            continue
    else:
        p.kill(); pytest.fail("service did not start")
    yield c, tmp_path
    try:
        c.call("shutdown")
    except Exception:
        pass
    p.wait(timeout=15)
    os.environ.pop("AICUTOUT_STATE", None)


def test_full_workflow_over_socket(service):
    c, tmp = service
    frames, gts = render(SCENARIOS["static"])
    video = str(tmp / "v.mp4"); write_video(video, frames)

    assert "detected" in c.call("hello")["hardware"].lower() or "cpu" in c.call("hello")["hardware"].lower()
    c.call("open", video=video, mode="custom", tier="draft")
    st = c.wait_idle()
    assert st["open"] and st["frames"] == len(frames)

    ys, xs = np.nonzero(gts[0] > 127)
    h, w = gts[0].shape
    cx, cy = xs.mean() / w, ys.mean() / h
    box = [xs.min() / w, ys.min() / h, xs.max() / w, ys.max() / h]
    seg = c.call("segment", idx=0, points=[[cx, cy, 1]], box=box, normalized=True)
    assert seg["png"] and seg["confidence"] > 0
    c.call("commit", idx=0, points=[[cx, cy, 1]], box=box, normalized=True)
    c.call("track", direction="forward")
    st = c.wait_idle()
    assert st["state"] == "done" and st["cached"] == len(frames)
    m = c.decode_png(c.call("get_mask", idx=len(frames) - 1)["png"])
    assert m is not None and (m > 127).mean() > 0.01


def test_rejects_wrong_token_and_bad_commands(service):
    c, _ = service
    c.token = "wrong"
    with pytest.raises(ServiceError, match="unauthorized"):
        c.call("hello")
    c.connect()
    with pytest.raises(ServiceError, match="Unknown command"):
        c.call("nope")
    with pytest.raises(ServiceError, match="No clip is open"):
        c.call("get_frame", idx=0)


def test_missing_video_is_reported_not_crashed(service):
    c, tmp = service
    with pytest.raises(ServiceError, match="not found"):
        c.call("open", video=str(tmp / "missing.mp4"))
    assert c.call("hello")["version"]          # service still alive


def test_app_commands_refine_export_reuse(service):
    """The commands the app uses: open (reused when identical), segment, commit, refine, track, timeline, export."""
    c, tmp = service
    frames, gts = render(SCENARIOS["static"])
    video = str(tmp / "v2.mp4"); write_video(video, frames)
    r1 = c.call("open", video=video, mode="custom", tier="draft")
    assert r1["set"]
    c.wait_idle()
    r2 = c.call("open", video=video, mode="custom", tier="draft")      # identical request re-uses the open session
    assert r2.get("reused") and r2["set"] == r1["set"]

    ys, xs = np.nonzero(gts[0] > 127)
    h, w = gts[0].shape
    pt = [float(xs.mean() / w), float(ys.mean() / h), 1]
    c.call("commit", idx=0, points=[pt], normalized=True)
    c.call("track", direction="both"); st = c.wait_idle()
    assert st["state"] == "done" and st["cached"] == len(frames)
    tl = c.call("timeline")
    assert tl["frames"] == len(frames) and len(tl["cached"]) == len(frames) and tl["keyframes"][0]["idx"] == 0

    mid = len(frames) // 2                                              # fix a frame by clicking on it
    c.call("refine", idx=mid, points=[pt], normalized=True)
    assert c.call("get_mask", idx=mid)["manual"]
    c.call("track", direction="both"); c.wait_idle()
    assert any(k["kind"] == "manual" for k in c.call("timeline")["keyframes"])

    out = tmp / "export"
    c.call("export", dir=str(out), edge={"feather": 2, "refine": 0.5, "decontaminate": 0.6, "spill": 0.3})
    st = c.wait_idle()
    assert st["state"] == "done" and "Render complete" in st["message"]
    import cv2
    a = cv2.imread(str(out / "alpha" / "000005.png"), cv2.IMREAD_UNCHANGED)
    cut = cv2.imread(str(out / "cutout" / "000005.png"), cv2.IMREAD_UNCHANGED)
    assert a.dtype == np.uint16 and a.shape == gts[0].shape
    assert cut.shape == gts[0].shape + (4,) and cut.dtype == np.uint16
    assert (a > 30000).mean() > 0.02 and (a < 1000).mean() > 0.5        # a real matte, not a solid colour
