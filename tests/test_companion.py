"""Companion smoke test: drives the real Tk app (hidden window) against a real service subprocess."""
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


def pump(app, seconds=0.0, until=None, timeout=90):
    t0 = time.time()
    while True:
        app.update()
        if until and until():
            return True
        if not until and time.time() - t0 >= seconds:
            return True
        if until and time.time() - t0 > timeout:
            return False
        time.sleep(0.02)


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


def test_companion_end_to_end(app, tmp_path):
    frames, gts = render(SCENARIOS["static"])
    video = str(tmp_path / "clip.mp4"); write_video(video, frames)
    assert pump(app, until=lambda: app.st_state.get() == "Ready", timeout=60)
    assert "detected" in app.msg.get("1.0", "end").lower() or "cpu" in app.msg.get("1.0", "end").lower()

    # open the clip (bypass the file dialog)
    app.video = video
    app.mode.set("Custom"); app.tier.set("Draft")

    def work():
        app._call("open", video=video, mode="custom", tier="draft")
        return app.client.wait_idle()
    app._async(work, "", lambda st: (setattr(app, "frames", st["frames"]), app.slider.configure(to=st["frames"] - 1), app._goto(0), app._refresh_timeline()))
    assert pump(app, until=lambda: app.cur_bgr is not None, timeout=90)
    assert app.frames == len(frames)

    # click the subject at the ground-truth centroid (map image coords -> canvas coords)
    app.update()
    ys, xs = np.nonzero(gts[0] > 127)
    h, w = gts[0].shape
    ox, oy = app.disp_off
    cx = ox + xs.mean() / w * (w * app.disp_scale)
    cy = oy + ys.mean() / h * (h * app.disp_scale)
    app._click(ev(cx, cy), 1)
    assert pump(app, until=lambda: app.preview_alpha is not None, timeout=60)
    assert (app.preview_alpha > 127).mean() > 0.01                        # a mask preview appeared

    app.confirm()
    assert pump(app, until=lambda: app.timeline.get("keyframes"), timeout=60)
    app.track("forward")
    assert pump(app, until=lambda: len(app.timeline.get("cached", [])) == app.frames, timeout=180)
    assert app.st_state.get() in ("Done", "Ready", "Tracking")

    # every preview mode renders without error
    for mode in ("Original", "Mask", "Alpha", "Transparent Checkerboard", "Overlay", "Cutout"):
        app.preview.set(mode); app._redraw(); app.update()
    # paint correction on the last frame
    last = app.frames - 1
    app._goto(last)
    assert pump(app, until=lambda: app.frame_idx == last and app.cur_alpha is not None, timeout=30)
    app.paint.set("Add Mask")
    app.stroke = [[0.05, 0.05], [0.12, 0.05]]
    app._release(ev(0, 0))
    assert pump(app, until=lambda: any(k["kind"] == "manual" for k in (app.client.call("timeline")["keyframes"])), timeout=30)
    # render (export) through the same client
    out = str(tmp_path / "out")
    app.client.call("export", dir=out)
    st = app.client.wait_idle()
    assert st["state"] == "done"
    assert os.path.isfile(os.path.join(out, "alpha", "000000.png")) and os.path.isfile(os.path.join(out, "cutout", f"{last:06d}.png"))
