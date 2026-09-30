"""Full chain through the *installed* product: mock host -> installed plugin -> (auto-started) installed service
-> SAM 2 on the GPU -> mattes on disk -> plugin render. Skipped unless the installer's per-user variant was installed
to build/inst (see docs/BUILD.md) and SAM 2 weights are present."""
import os
import struct
import subprocess
import sys
import time

import cv2
import numpy as np
import pytest

from inference.client import Client, ServiceError
from tests.synth import SCENARIOS, render, write_video

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INST = os.path.join(ROOT, "build", "inst")
HOST = os.path.join(ROOT, "build", "host", "mock_host.exe")
PLUGIN = os.path.join(INST, "OFX", "AICutout.ofx.bundle", "Contents", "Win64", "AICutout.ofx")
INI = os.path.join(os.environ.get("PROGRAMDATA", ""), "AICutout", "install.ini")

pytestmark = pytest.mark.skipif(not (os.path.isfile(PLUGIN) and os.path.isfile(HOST) and os.path.isfile(INI)),
                                reason="installed per-user build not found (scripts/build_installer.ps1 -DevUser + install to build/inst)")


def test_click_analyze_track_render_through_installed_product(tmp_path):
    frames, gts = render(SCENARIOS["pan"])
    video = str(tmp_path / "clip.mp4"); write_video(video, frames)
    state, cache = str(tmp_path / "state"), str(tmp_path / "cache")
    env = dict(os.environ, AICUTOUT_STATE=state, AICUTOUT_CACHE=cache)
    os.makedirs(state)
    W, H = 480, 320
    with open(tmp_path / "src.f32", "wb") as f:                     # the host's picture (content irrelevant for the matte)
        f.write(struct.pack("<ii", W, H)); f.write(np.full((H, W, 4), 0.5, np.float32).tobytes())

    ys, xs = np.nonzero(gts[0] > 127)
    gh, gw = gts[0].shape
    u, v = xs.mean() / gw, ys.mean() / gh
    px, py = u * W, (1 - v) * H                                     # canonical coords (y up)
    cmds = [f"set:sourceFile={video}", "set:mode=3", "set:quality=0", "set:clickTool=1", f"pen:{px:.1f},{py:.1f}@0",
            "changed:analyze", "wait:25000", "changed:mode", "get:stState", "get:stMessage",
            "changed:trackBoth", "wait:40000", "changed:mode", "get:stState", "get:matteSet",
            "set:output=2", "render:r0@0", "render:r20@20", "render:r39@39"]
    try:
        p = subprocess.run([HOST, PLUGIN, str(tmp_path), *cmds], capture_output=True, text=True, env=env, timeout=180)
        out = p.stdout + p.stderr
        assert p.returncode == 0, out
        assert "stState=Done" in out.replace(" ", "") or "Done" in out, out
        # rendered alpha must follow the moving subject (compare against the ground truth of that frame)
        for idx, name in ((0, "r0"), (20, "r20"), (39, "r39")):
            with open(tmp_path / f"{name}.f32", "rb") as f:
                w, h = struct.unpack("<ii", f.read(8))
                alpha = np.frombuffer(f.read(), np.float32).reshape(h, w, 4)[..., 3]
            gt = cv2.resize(gts[idx].astype(np.float32) / 255, (w, h), interpolation=cv2.INTER_AREA)
            inter = ((alpha > .5) & (gt > .5)).sum(); union = ((alpha > .5) | (gt > .5)).sum()
            iou = inter / max(union, 1)
            print(f"frame {idx}: IoU vs ground truth {iou:.3f}")
            assert iou > 0.75, f"frame {idx} IoU {iou:.3f}\n{out}"
    finally:
        try:
            os.environ["AICUTOUT_STATE"] = state
            c = Client(); c.connect(); c.call("shutdown")
        except Exception:
            pass
        finally:
            os.environ.pop("AICUTOUT_STATE", None)
