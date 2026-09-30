import time

import cv2
import numpy as np
import pytest

from core.cache.matte_store import FLAG_MANUAL, pack_frame
from core.compositing import native
from core.compositing.native import EdgeSettings, bgr8_to_rgba, native_decode, render

pytestmark = pytest.mark.skipif(native.find_dll() is None, reason="native core not built")


def scene(w=480, h=320, bg=(0.1, 0.7, 0.1), fg=(0.8, 0.2, 0.2)):
    """Subject disc on a green background with a green-tinted, blurred edge (colour spill)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2)
    a = np.clip((110 - d) / 3.0 + 0.5, 0, 1)                     # true alpha, ~3px soft edge
    img = np.zeros((h, w, 4), np.float32)
    for c in range(3):
        img[..., c] = fg[c] * a + bg[c] * (1 - a)
    img[..., 3] = 1
    return img, a


def matte_from(a, scale=0.5):
    small = cv2.resize(a, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return np.clip(small * 255 + 0.5, 0, 255).astype(np.uint8)


def out_alpha(img, matte, **kw):
    return render(img, matte, EdgeSettings(mode="cutout", **kw))[..., 3]


def test_format_parity_python_writer_cpp_reader():
    rng = np.random.default_rng(0)
    a = (rng.random((90, 130)) > 0.6).astype(np.uint8) * 255
    a[10:40, 20:80] = 255
    got = native_decode(pack_frame(a, 0.7, FLAG_MANUAL))
    assert got is not None and np.array_equal(got[0], a) and abs(got[1] - 0.7) < 0.01 and got[2] == FLAG_MANUAL
    bad = bytearray(pack_frame(a, 0.7, 0)); bad[30] ^= 0x55
    assert native_decode(bytes(bad)) is None
    assert native_decode(b"junk") is None


def test_cutout_alpha_tracks_subject_and_original_is_passthrough():
    img, a = scene()
    m = matte_from(a)
    out = render(img, m, EdgeSettings(mode="cutout", refine=0.5))
    assert np.abs(out[..., 3] - a).mean() < 0.02
    assert out[160, 240, 3] > 0.99 and out[5, 5, 3] < 0.01
    o = render(img, m, EdgeSettings(mode="original"))
    assert np.array_equal(o, img)
    # no matte => untouched RGB, opaque
    n = render(img, None, EdgeSettings(mode="cutout"))
    assert np.array_equal(n[..., :3], img[..., :3]) and n[..., 3].min() == 1


def test_edge_shift_grows_and_shrinks():
    img, a = scene()
    m = matte_from(a)
    base = out_alpha(img, m).sum()
    grown = out_alpha(img, m, edge_shift=6).sum()
    shrunk = out_alpha(img, m, edge_shift=-6).sum()
    r0 = np.sqrt(base / np.pi)
    assert 4.0 < np.sqrt(grown / np.pi) - r0 < 8.0
    assert 4.0 < r0 - np.sqrt(shrunk / np.pi) < 8.0


def test_feather_widens_edge_transition():
    img, a = scene()
    m = matte_from(a)
    def width(al):   # pixels with intermediate alpha along the middle row
        row = al[160]
        return int(((row > 0.05) & (row < 0.95)).sum())
    assert width(out_alpha(img, m, feather=6)) > width(out_alpha(img, m)) + 6


def test_smooth_removes_jagged_edge_without_softening():
    img, a = scene()
    jag = a.copy()
    rng = np.random.default_rng(3)
    ring = (np.abs(np.hypot(*(np.mgrid[0:320, 0:480] - np.array([[[160]], [[240]]]))) - 110) < 3)
    jag[ring & (rng.random(a.shape) > 0.5)] = 0.0             # ragged edge
    m = np.clip(cv2.resize(jag, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA) * 255, 0, 255).astype(np.uint8)
    def roughness(al):
        e = np.abs(np.gradient(cv2.GaussianBlur(al, (0, 0), 0.6))[0]) + 0
        ang = np.linspace(0, 2 * np.pi, 720, endpoint=False)
        rr = []
        for t in ang:
            x, y = 240 + 110 * np.cos(t), 160 + 110 * np.sin(t)
            prof = [al[int(round(160 + r * np.sin(t))), int(round(240 + r * np.cos(t)))] for r in np.arange(98, 122, 0.5)]
            rr.append(np.sum(np.array(prof) > 0.5) * 0.5)      # crossing radius
        return np.std(rr)
    assert roughness(out_alpha(img, m, smooth=4, refine=0.0)) < roughness(out_alpha(img, m, smooth=0, refine=0.0))


def test_decontamination_removes_background_colour_from_edge():
    img, a = scene()
    m = matte_from(a)
    edge = (a > 0.2) & (a < 0.8)
    def green_excess(rgba):
        return float((rgba[..., 1] - 0.5 * (rgba[..., 0] + rgba[..., 2]))[edge].mean())
    plain = render(img, m, EdgeSettings(mode="cutout", decontaminate=0.0))
    dec = render(img, m, EdgeSettings(mode="cutout", decontaminate=1.0))
    assert green_excess(dec) < green_excess(plain) - 0.05
    # spill suppression reduces green in the subject further
    sp = render(img, m, EdgeSettings(mode="cutout", decontaminate=1.0, spill=1.0))
    assert green_excess(sp) <= green_excess(dec) + 1e-4


def test_composite_and_checkerboard_and_overlay_and_premultiply():
    img, a = scene()
    m = matte_from(a)
    comp = render(img, m, EdgeSettings(mode="composite", bg_kind="solid", bg_color=(0, 0, 1), refine=0.5))
    assert comp[5, 5, 2] > 0.95 and comp[5, 5, 0] < 0.05          # background replaced with solid blue
    assert comp[160, 240, 0] > 0.7 and comp[..., 3].min() == 1
    chk = render(img, m, EdgeSettings(mode="checkerboard"))
    assert 0.3 < chk[5, 5, 0] < 0.7
    ov = render(img, m, EdgeSettings(mode="overlay", overlay_opacity=0.6))
    assert ov[160, 240, 0] > img[160, 240, 0] and np.allclose(ov[5, 5, :3], img[5, 5, :3])
    pm = render(img, m, EdgeSettings(mode="cutout", premultiply=True))
    assert pm[5, 5, :3].max() < 0.01
    mask = render(img, m, EdgeSettings(mode="mask"))
    assert mask[160, 240, 0] > 0.99 and mask[5, 5, 0] < 0.01


def test_empty_matte_gives_transparent_output():
    img, _ = scene()
    out = render(img, np.zeros((160, 240), np.uint8), EdgeSettings(mode="cutout"))
    assert out[..., 3].max() == 0.0


def test_performance_report(capsys):
    """Not an assertion of speed targets — prints measured numbers (also gates absurd regressions)."""
    rows = []
    for name, (w, h) in {"1080p": (1920, 1080), "4K": (3840, 2160)}.items():
        img, a = scene(w, h)
        m = matte_from(a, 512 / w)
        for label, kw in {"cutout(default)": dict(mode="cutout"),
                          "full edge pipeline": dict(mode="cutout", edge_shift=3, feather=3, smooth=2, refine=0.8,
                                                     decontaminate=1, spill=0.5, quality=2)}.items():
            t0 = time.time(); render(img, m, EdgeSettings(**kw)); dt = time.time() - t0
            rows.append(f"{name:6s} {label:20s} {dt * 1000:7.0f} ms")
    with capsys.disabled():
        print("\n" + "\n".join(rows))
    assert True
