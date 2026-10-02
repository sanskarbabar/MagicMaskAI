"""Drives the real AICutout.ofx through a mock OpenFX host (tests/host/mock_host.cpp).

Verifies the OFX contract (describe / instantiate in both contexts, with host validation on), that the plugin stays
small, rendering with bottom-up and negative-stride buffers, every output, the two buttons, and graceful handling of
missing/corrupt mattes. It cannot prove behaviour inside Resolve itself (see docs/RESOLVE_TESTING.md)."""
import os
import shutil
import struct
import subprocess

import cv2
import numpy as np
import pytest

from core.cache.matte_store import MatteSet, set_active

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.path.join(ROOT, "build", "host", "mock_host.exe")
PLUGIN = os.path.join(ROOT, "build", "plugin_validate", "AICutout.ofx.bundle", "Contents", "Win64", "AICutout.ofx")
PROD = os.path.join(ROOT, "build", "plugin", "AICutout.ofx.bundle", "Contents", "Win64", "AICutout.ofx")

pytestmark = pytest.mark.skipif(not (os.path.isfile(HOST) and os.path.isfile(PLUGIN)),
                                reason="mock host / plugin not built (scripts/build_host.sh)")

W, H = 480, 320


def build_scene(tmp):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt((xx - 140) ** 2 + (yy - 90) ** 2)                 # subject in the UPPER-LEFT (catches vertical flips)
    a = np.clip((60 - d) / 3 + .5, 0, 1)
    img = np.zeros((H, W, 4), np.float32)
    fg, bg = (0.8, 0.2, 0.2), (0.1, 0.7, 0.1)
    for c in range(3):
        img[..., c] = fg[c] * a + bg[c] * (1 - a)
    img[..., 3] = 1
    with open(os.path.join(tmp, "src.f32"), "wb") as f:
        f.write(struct.pack("<ii", W, H)); f.write(img.tobytes())
    cache = os.path.join(tmp, "cache")
    ms = MatteSet(cache, "testset-1")
    ms.create({"width": W // 2, "height": H // 2, "frames": 10, "fps": 24, "source": "x.mp4"})
    small = cv2.resize(a, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
    ms.write_frame(3, np.clip(small * 255 + .5, 0, 255).astype(np.uint8), 0.95)
    set_active(cache, "testset-1")
    return img, a, cache


@pytest.fixture()
def scene(tmp_path):
    img, a, cache = build_scene(str(tmp_path))
    return dict(tmp=str(tmp_path), img=img, a=a, cache=cache)


def run(scene, *cmds, negative=False, plugin=PLUGIN, env_extra=None):
    env = dict(os.environ, AICUTOUT_CACHE=scene["cache"], AICUTOUT_STATE=os.path.join(scene["tmp"], "state"))
    env.update(env_extra or {})
    if negative:
        env["MOCK_NEGATIVE_ROWBYTES"] = "1"
    p = subprocess.run([HOST, plugin, scene["tmp"], *cmds], capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout + p.stderr


def load(scene, name):
    with open(os.path.join(scene["tmp"], name + ".f32"), "rb") as f:
        w, h = struct.unpack("<ii", f.read(8))
        return np.frombuffer(f.read(), np.float32).reshape(h, w, 4).copy()


# ---------------------------------------------------------------------------------------------- contract
def test_describe_and_instantiate_both_contexts_with_validation(scene):
    out = run(scene, "dump")
    assert "describe=OK" in out and "createInstance=OK" in out and "getClipPreferences=OK" in out
    assert "describeInContext[OfxImageEffectContextFilter]=OK clips=2" in out
    assert "describeInContext[OfxImageEffectContextGeneral]=OK clips=2" in out
    assert "outputPremult=OfxImageAlphaUnPremultiplied" in out


def test_plugin_stays_simple(scene):
    out = run(scene, "dump")
    params = {l.split(":")[1]: l.split(":")[2] for l in out.splitlines() if l.startswith("param:")}
    visible = {"openApp", "update", "output", "feather", "edgeShift", "cleanEdge", "frameOffset", "status"}
    hidden = {"matteSet", "revision", "Controls"}
    assert set(params) == visible | hidden, set(params) ^ (visible | hidden)
    assert len(visible) <= 8


# ---------------------------------------------------------------------------------------------- render
@pytest.mark.parametrize("negative", [False, True], ids=["bottom-up", "negative-rowbytes"])
def test_cutout_alpha_matches_subject_and_orientation(scene, negative):
    run(scene, "set:cleanEdge=0", "render:cut@3", negative=negative)
    cut = load(scene, "cut")
    assert np.abs(cut[..., 3] - scene["a"]).mean() < 0.01             # right shape...
    assert cut[90, 140, 3] > 0.99 and cut[300, 440, 3] < 0.01         # ...in the right place (not vertically flipped)
    assert np.allclose(cut[90, 140, :3], scene["img"][90, 140, :3], atol=0.02)   # straight RGB from the source


def test_the_five_outputs(scene):
    run(scene, "set:output=1", "render:matte@3", "set:output=2", "render:ov@3", "set:output=3", "render:chk@3",
        "set:output=0", "render:cut@3")
    matte, ov, chk, cut = (load(scene, n) for n in ("matte", "ov", "chk", "cut"))
    a = scene["a"]
    assert np.abs(matte[..., 0] - a).mean() < 0.01 and matte[..., 3].min() == 1        # matte as black/white
    assert ov[90, 140, 0] > scene["img"][90, 140, 0] and np.allclose(ov[300, 440, :3], scene["img"][300, 440, :3])
    assert 0.3 < chk[300, 440, 0] < 0.7 and chk[300, 440, 3] == 1                      # cutout over a checkerboard
    assert cut[300, 440, 3] < 0.01
    assert "identity=yes clip=Source" in run(scene, "set:output=4", "identity@3")       # Original = pass-through
    assert "identity=no" in run(scene, "set:output=0", "identity@3")


def test_edge_controls_change_the_result(scene):
    run(scene, "render:base@3", "set:edgeShift=8", "render:grow@3", "set:edgeShift=-8", "render:shrink@3")
    b, g, s = (load(scene, n)[..., 3].sum() for n in ("base", "grow", "shrink"))
    assert g > b * 1.15 and s < b * 0.85
    run(scene, "set:feather=6", "render:soft@3")
    soft, hard = load(scene, "soft")[..., 3], load(scene, "base")[..., 3]
    assert ((soft > .05) & (soft < .95)).sum() > ((hard > .05) & (hard < .95)).sum() * 1.5


def test_clean_edge_removes_background_color(scene):
    run(scene, "set:cleanEdge=0", "render:off@3", "set:cleanEdge=1", "render:on@3")
    edge = (scene["a"] > 0.2) & (scene["a"] < 0.8)
    def green(n):
        c = load(scene, n); return float((c[..., 1] - 0.5 * (c[..., 0] + c[..., 2]))[edge].mean())
    assert green("on") < green("off") - 0.03


def test_missing_or_corrupt_matte_passes_source_through_without_crashing(scene):
    run(scene, "render:nomatte@7")                                       # frame 7 has no matte
    nm = load(scene, "nomatte")
    assert np.allclose(nm[..., :3], scene["img"][..., :3]) and nm[..., 3].min() == 1
    path = os.path.join(scene["cache"], "testset-1", "masks", "000003.acm")
    with open(path, "r+b") as f:
        f.seek(30); f.write(b"\xff\xff\xff\xff")
    run(scene, "render:corrupt@3")
    assert np.allclose(load(scene, "corrupt")[..., :3], scene["img"][..., :3])
    shutil.rmtree(os.path.join(scene["cache"], "testset-1"))
    os.remove(os.path.join(scene["cache"], "_active.txt"))
    run(scene, "render:noset@3")
    assert load(scene, "noset")[..., 3].min() == 1


def test_frame_offset_shifts_which_matte_is_used(scene):
    run(scene, "set:frameOffset=-3", "render:shifted@6")                 # time 6 + (-3) -> matte 3
    assert load(scene, "shifted")[90, 140, 3] > 0.99


def test_production_build_loads_without_host_validation(scene):
    if not os.path.isfile(PROD):
        pytest.skip("production plugin not built")
    assert "render=0" in run(scene, "render:p@3", plugin=PROD)


# ---------------------------------------------------------------------------------------------- buttons
def test_update_matte_links_latest_clip_and_asks_host_to_rerender(scene):
    out = run(scene, "get:matteSet", "get:revision", "changed:update", "get:matteSet", "get:revision", "get:status")
    lines = {l.split("=")[0]: l.split("=", 1)[1] for l in out.splitlines() if "=" in l and l.split("=")[0] in ("matteSet", "revision", "status")}
    assert "matteSet=testset-1" in out and "Matte linked" in out
    assert out.count("revision=0") == 1 and "revision=1" in out


def test_update_without_any_matte_explains_what_to_do(scene):
    os.remove(os.path.join(scene["cache"], "_active.txt"))
    assert "Open AI Cutout" in run(scene, "changed:update", "get:status")


def test_open_app_button(scene):
    ok = run(scene, "changed:openApp", "get:status", env_extra={"AICUTOUT_APP_CMD": "cmd.exe /c exit"})
    assert "AI Cutout is open" in ok and "message:" not in ok
    empty = os.path.join(scene["tmp"], "nopd"); os.makedirs(empty)
    bad = run(scene, "changed:openApp", env_extra={"PROGRAMDATA": empty, "AICUTOUT_APP_CMD": ""})
    assert "not installed" in bad.lower()
