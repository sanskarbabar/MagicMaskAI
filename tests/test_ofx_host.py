"""Drives the real AICutout.ofx through a mock OpenFX host (tests/host/mock_host.cpp).

Verifies the OFX contract (Describe/DescribeInContext/CreateInstance for both contexts, every parameter the spec
requires), the render action with bottom-up and negative-stride image buffers, output modes, isIdentity,
premultiplication, overlay clicks/draw, and graceful handling of missing/corrupt mattes. It cannot prove behaviour
inside Resolve itself (see docs/RESOLVE_TESTING.md)."""
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


def run(scene, *cmds, negative=False, plugin=PLUGIN, expect_ok=True):
    env = dict(os.environ, AICUTOUT_CACHE=scene["cache"], AICUTOUT_STATE=os.path.join(scene["tmp"], "state"))
    if negative:
        env["MOCK_NEGATIVE_ROWBYTES"] = "1"
    p = subprocess.run([HOST, plugin, scene["tmp"], *cmds], capture_output=True, text=True, env=env, timeout=60)
    if expect_ok:
        assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout + p.stderr


def load(scene, name):
    with open(os.path.join(scene["tmp"], name + ".f32"), "rb") as f:
        w, h = struct.unpack("<ii", f.read(8))
        return np.frombuffer(f.read(), np.float32).reshape(h, w, 4).copy()


# ---------------------------------------------------------------------------------------------- protocol
def test_describe_and_instantiate_both_contexts_with_validation(scene):
    out = run(scene, "dump")
    assert "describe=OK" in out and "createInstance=OK" in out and "getClipPreferences=OK" in out
    assert "describeInContext[OfxImageEffectContextFilter]=OK clips=2" in out
    assert "describeInContext[OfxImageEffectContextGeneral]=OK clips=3" in out      # + optional Background clip


def test_all_spec_controls_exist(scene):
    out = run(scene, "dump")
    params = {l.split(":")[1] for l in out.splitlines() if l.startswith("param:")}
    required = {
        # Mode / Selection
        "mode", "addSelection", "removeSelection", "brushSize", "brushFeather", "edgeRefinement",
        # Tracking
        "trackForward", "trackBackward", "trackBoth", "recalculate",
        # Quality / Edge
        "quality", "feather", "smooth", "edgeShift", "spillSuppression", "decontaminateEdge",
        # Output + preview
        "output", "preview",
        # Buttons
        "analyze", "track", "previewButton", "render", "reset",
        # Status readouts
        "progress", "stState", "stFrame", "stTracking", "stConfidence", "stEta", "stMessage",
        # Manual correction
        "paintMode", "correctFrame", "propagateCorrection",
    }
    assert required <= params, required - params


# ---------------------------------------------------------------------------------------------- render
@pytest.mark.parametrize("negative", [False, True], ids=["bottom-up", "negative-rowbytes"])
def test_cutout_alpha_matches_subject_and_orientation(scene, negative):
    run(scene, "set:output=2", "render:cut@3", negative=negative)
    cut = load(scene, "cut")
    assert np.abs(cut[..., 3] - scene["a"]).mean() < 0.003            # right shape...
    assert cut[90, 140, 3] > 0.99 and cut[300, 440, 3] < 0.01         # ...in the right place (not vertically flipped)
    assert np.allclose(cut[90, 140, :3], scene["img"][90, 140, :3])   # RGB is the source (straight alpha)


def test_output_modes(scene):
    run(scene, "set:output=0", "render:mask@3", "set:output=1", "render:alpha@3", "set:output=3", "set:bgKind=1",
        "set:bgColor=0,0,1", "render:comp@3", "set:output=2", "set:preview=5", "render:ov@3", "set:preview=4", "render:chk@3")
    mask, alpha, comp, ov, chk = (load(scene, n) for n in ("mask", "alpha", "comp", "ov", "chk"))
    a = scene["a"]
    assert np.abs(mask[..., 0] - a).mean() < 0.01 and mask[..., 3].min() == 1          # raw matte as gray
    assert np.abs(alpha[..., 0] - a).mean() < 0.003 and alpha[..., 3].min() == 1       # final alpha as gray
    assert comp[300, 440, 2] > 0.95 and comp[300, 440, 0] < 0.05                       # background replaced by blue
    assert comp[90, 140, 0] > 0.7                                                       # subject kept
    assert ov[90, 140, 0] > scene["img"][90, 140, 0] and np.allclose(ov[300, 440, :3], scene["img"][300, 440, :3])
    assert 0.3 < chk[300, 440, 0] < 0.7                                                # transparent checkerboard


def test_identity_for_original_preview_and_not_otherwise(scene):
    assert "identity=yes clip=Source" in run(scene, "set:preview=1", "identity@3")
    assert "identity=no" in run(scene, "set:preview=0", "identity@3")


def test_premultiply_flag_reaches_host(scene):
    assert "outputPremult=OfxImageAlphaUnPremultiplied" in run(scene, "dump")
    out = run(scene, "set:premultiply=1", "changed:premultiply", "render:pm@3", "set:output=2")
    pm = load(scene, "pm")
    assert pm[300, 440, :3].max() < 0.01 and pm[90, 140, 3] > 0.99


def test_edge_controls_change_the_result(scene):
    run(scene, "set:output=2", "render:base@3", "set:edgeShift=8", "render:grow@3", "set:edgeShift=-8", "render:shrink@3")
    b, g, s = (load(scene, n)[..., 3].sum() for n in ("base", "grow", "shrink"))
    assert g > b * 1.15 and s < b * 0.85
    run(scene, "set:edgeShift=0", "set:feather=6", "render:soft@3")
    soft = load(scene, "soft")[..., 3]
    hard = load(scene, "base")[..., 3]
    assert ((soft > .05) & (soft < .95)).sum() > ((hard > .05) & (hard < .95)).sum() * 1.5


def test_missing_or_corrupt_matte_passes_source_through_without_crashing(scene):
    out = run(scene, "set:output=2", "render:nomatte@7")                # frame 7 has no matte
    nm = load(scene, "nomatte")
    assert np.allclose(nm[..., :3], scene["img"][..., :3]) and nm[..., 3].min() == 1
    path = os.path.join(scene["cache"], "testset-1", "masks", "000003.acm")
    with open(path, "r+b") as f:
        f.seek(30); f.write(b"\xff\xff\xff\xff")
    run(scene, "set:output=2", "render:corrupt@3")
    assert np.allclose(load(scene, "corrupt")[..., :3], scene["img"][..., :3])
    # no matte set at all
    shutil.rmtree(os.path.join(scene["cache"], "testset-1"))
    os.remove(os.path.join(scene["cache"], "_active.txt"))
    run(scene, "set:output=2", "render:noset@3")
    assert load(scene, "noset")[..., 3].min() == 1


def test_production_build_loads_without_host_validation(scene):
    if not os.path.isfile(PROD):
        pytest.skip("production plugin not built")
    out = run(scene, "set:output=2", "render:p@3", plugin=PROD)
    assert "render=0" in out


# ---------------------------------------------------------------------------------------------- overlay
def test_overlay_click_selection_stores_normalized_topdown_points(scene):
    out = run(scene, "pen:140,230@3", "get:clicks")                     # tool is Off: click must NOT be consumed
    assert "clicks=\n" in out or out.rstrip().endswith("clicks=") or "clicks=\n" in out + "\n"
    out = run(scene, "set:clickTool=1", "pen:140,230@3", "set:clickTool=2", "pen:400,50@3", "get:clicks")
    line = [l for l in out.splitlines() if l.startswith("clicks=")][0]
    pts = [p for p in line[len("clicks="):].split(";") if p]
    assert len(pts) == 2
    f, u, v, lab = pts[0].split(",")
    assert (int(f), int(lab)) == (3, 1) and abs(float(u) - 140 / W) < 1e-3 and abs(float(v) - 90 / H) < 1e-3   # top-down v
    assert pts[1].split(",")[3] == "0"                                    # remove-selection => negative label


def test_overlay_draws_points_and_status(scene):
    out = run(scene, "set:clickTool=1", "pen:140,230@3", "draw@3", "changed:clearSelection", "draw@3")
    assert "AI Cutout:" in out and "[click = include subject]" in out
    draws = [int(l.split("calls=")[1]) for l in out.splitlines() if l.startswith("draw=")]
    assert draws[0] > draws[1]                                            # the point marker disappears after clearing


def test_analyze_without_source_file_shows_a_clear_error(scene):
    out = run(scene, "changed:analyze")
    assert "Set 'Source File'" in out


def test_no_matte_hint_is_shown_when_nothing_is_analyzed(scene):
    shutil.rmtree(os.path.join(scene["cache"], "testset-1"))
    os.remove(os.path.join(scene["cache"], "_active.txt"))
    out = run(scene, "draw@3")
    assert "No matte yet" in out
