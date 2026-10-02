"""The installed product, end to end: installed service + installed plugin.

The service (started from the installed folder) analyses and tracks a real clip with SAM 2 on the GPU; the mock host
then loads the *installed* plugin, which renders the cutout from the cache. Skipped unless the installer's per-user
variant was installed to build/inst (see BUILD.md) and SAM 2 weights are present."""
import os
import struct
import subprocess
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
SERVICE = os.path.join(INST, "aicutout-service.exe")
INI = os.path.join(os.environ.get("PROGRAMDATA", ""), "AICutout", "install.ini")

pytestmark = pytest.mark.skipif(not all(os.path.isfile(p) for p in (PLUGIN, SERVICE, HOST, INI)),
                                reason="installed per-user build not found (scripts/build_installer.ps1 -DevUser, install to build/inst)")


def test_installed_service_and_plugin_cut_out_a_moving_subject(tmp_path):
    frames, gts = render(SCENARIOS["pan"])
    video = str(tmp_path / "clip.mp4"); write_video(video, frames)
    state, cache = str(tmp_path / "state"), str(tmp_path / "cache")
    os.makedirs(state)
    env = dict(os.environ, AICUTOUT_STATE=state, AICUTOUT_CACHE=cache)
    os.environ["AICUTOUT_STATE"] = state
    svc = subprocess.Popen([SERVICE, "--idle-timeout", "0"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        c = Client(timeout=120)
        for _ in range(120):
            time.sleep(0.5)
            try:
                c.connect(); c.call("hello"); break
            except (ServiceError, OSError):
                continue
        else:
            pytest.fail("installed service did not start")
        c.call("open", video=video, mode="custom", tier="balanced"); c.wait_idle()
        ys, xs = np.nonzero(gts[0] > 127)
        gh, gw = gts[0].shape
        c.call("commit", idx=0, points=[[xs.mean() / gw, ys.mean() / gh, 1]], normalized=True)
        c.call("track", direction="both"); st = c.wait_idle()
        assert st["state"] == "done" and st["cached"] == len(frames)

        W, H = 480, 320
        with open(tmp_path / "src.f32", "wb") as f:
            f.write(struct.pack("<ii", W, H)); f.write(np.full((H, W, 4), 0.5, np.float32).tobytes())
        p = subprocess.run([HOST, PLUGIN, str(tmp_path), "render:r0@0", "render:r20@20", "render:r39@39"],
                           capture_output=True, text=True, env=env, timeout=120)
        assert p.returncode == 0, p.stdout + p.stderr
        for idx, name in ((0, "r0"), (20, "r20"), (39, "r39")):
            with open(tmp_path / f"{name}.f32", "rb") as f:
                w, h = struct.unpack("<ii", f.read(8))
                alpha = np.frombuffer(f.read(), np.float32).reshape(h, w, 4)[..., 3]
            gt = cv2.resize(gts[idx].astype(np.float32) / 255, (w, h), interpolation=cv2.INTER_AREA)
            iou = ((alpha > .5) & (gt > .5)).sum() / max(((alpha > .5) | (gt > .5)).sum(), 1)
            print(f"frame {idx}: IoU vs ground truth {iou:.3f}")
            assert iou > 0.7, f"frame {idx} IoU {iou:.3f}"
    finally:
        try:
            c.call("shutdown")
        except Exception:
            pass
        svc.wait(timeout=20) if svc.poll() is None else None
        os.environ.pop("AICUTOUT_STATE", None)
