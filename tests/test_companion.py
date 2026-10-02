"""App smoke test: drives the real Tk app (hidden window) against a real service subprocess, through the four steps
(open, click the subject, track, render) plus fixing a frame by clicking on it."""
import os
import subprocess
import sys
import time
import types

import numpy as np
import pytest

tk = pytest.importorskip("tkinter")

from inference.client import Client, ServiceError
from tests.synth import SCENARIOS, render, write_video


def pump(app, until, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.update()
        if until():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def app(tmp_path):
    state = str(tmp_path / "state")
    env = dict(os.environ, AICUTOUT_STATE=state, AICUTOUT_CACHE=str(tmp_path / "cache"), AICUTOUT_ENGINE="grabcut")
    os.environ["AICUTOUT_STATE"] = state
    os.environ["AICUTOUT_CACHE"] = str(tmp_path / "cache")
    p = subprocess.Popen([sys.executable, "-m", "inference.server", "--idle-timeout", "0"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from plugin.UI.companion import App
    try:
        a = App()
    except tk.TclError:
        p.kill()
        pytest.skip("no display available for Tk")
    a.withdraw()
    a.geometry("1100x760")
    for _ in range(80):
        time.sleep(0.4)
        try:
            c = Client(); c.connect(); c.call("hello"); c.close(); break
        except (ServiceError, OSError):
            continue
    yield a
    try:
        a.client.call("shutdown")
    except Exception:
        pass
    a.destroy()
    p.wait(timeout=20)
    os.environ.pop("AICUTOUT_STATE", None); os.environ.pop("AICUTOUT_CACHE", None)


def ev(x, y):
    return types.SimpleNamespace(x=x, y=y)


def canvas_xy(app, gt, u_frac=None):
    ys, xs = np.nonzero(gt > 127)
    h, w = gt.shape
    ox, oy = app.disp_off
    return ox + xs.mean() / w * (w * app.disp_scale), oy + ys.mean() / h * (h * app.disp_scale)


def test_four_steps_and_fix_a_frame(app, tmp_path):
    frames, gts = render(SCENARIOS["static"])
    video = str(tmp_path / "clip.mp4"); write_video(video, frames)
    assert pump(app, lambda: app.client is not None and "Ready" in app.msg.get("1.0", "end"), timeout=60)

    # 1. open
    app.load_clip(video)
    assert pump(app, lambda: app.cur_bgr is not None and app.frames == len(frames), timeout=90)
    assert app.out_dir.endswith("clip_cutout")

    # 2. click the subject -> a selection preview appears
    app.update()
    app._click(ev(*canvas_xy(app, gts[0])), 1)
    assert pump(app, lambda: app.preview_alpha is not None, timeout=60)
    assert (app.preview_alpha > 127).mean() > 0.01

    # 3. track
    app.track()
    assert pump(app, lambda: len(app.timeline.get("cached", [])) == app.frames, timeout=180)
    assert app.timeline["keyframes"] and app.timeline["keyframes"][0]["idx"] == 0

    # every view renders
    for view in ("Overlay", "Cutout", "Original"):
        app.preview.set(view); app._redraw(); app.update()

    # fix a frame: go to a later frame, click on it -> the matte there is refined and saved as a keyframe
    mid = app.frames // 2
    app._goto(mid)
    assert pump(app, lambda: app.frame_idx == mid and app.cur_alpha is not None, timeout=30)
    app.update()
    app._click(ev(*canvas_xy(app, gts[mid])), 1)
    assert pump(app, lambda: any(k["idx"] == mid for k in app.timeline.get("keyframes", [])), timeout=60)
    assert app.track_btn.cget("text") == "Track again"
    app.track()
    assert pump(app, lambda: app.track_btn.cget("text") == "Track", timeout=180)

    # 4. render
    app.render_out()
    out = app.out_dir
    assert pump(app, lambda: os.path.isfile(os.path.join(out, "cutout", f"{app.frames - 1:06d}.png")), timeout=120)
    assert os.path.isfile(os.path.join(out, "alpha", "000000.png"))
