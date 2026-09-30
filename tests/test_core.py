import os

import numpy as np
import pytest

from core.cache.matte_store import (FLAG_MANUAL, MatteSet, get_active, list_sets, pack_frame, rle_decode, rle_encode,
                                    set_active, settings_hash, unpack_frame)
from core.masking.metrics import iou
from core.temporal.stabilizer import TemporalStabilizer
from gpu.devices import HardwareReport, GpuInfo, check_vram


def test_rle_roundtrip_random_and_edges():
    rng = np.random.default_rng(1)
    for shape in [(1, 1), (7, 13), (300, 500)]:
        a = (rng.random(shape) > 0.5).astype(np.uint8) * 255
        assert np.array_equal(rle_decode(rle_encode(a), shape[1], shape[0]), a)
    solid = np.full((400, 400), 255, np.uint8)     # run longer than 65535 must split
    assert np.array_equal(rle_decode(rle_encode(solid), 400, 400), solid)


def test_frame_pack_detects_corruption():
    a = np.zeros((32, 48), np.uint8); a[5:20, 10:30] = 200
    blob = pack_frame(a, 0.8, FLAG_MANUAL)
    rec = unpack_frame(blob)
    assert np.array_equal(rec.alpha, a) and rec.manual and abs(rec.confidence - 0.8) < 0.01
    bad = bytearray(blob); bad[-2] ^= 0xFF
    with pytest.raises(ValueError):
        unpack_frame(bytes(bad))
    with pytest.raises(ValueError):
        unpack_frame(blob[:10])


def test_matte_set_missing_and_corrupt_frames_are_recomputable(tmp_path):
    ms = MatteSet(str(tmp_path), "clipA-1234-m@1")
    ms.create({"width": 48, "height": 32, "frames": 3})
    a = np.zeros((32, 48), np.uint8); a[4:28, 6:40] = 255; a[10:20, 10:30] = 90
    ms.write_frame(0, a, 0.9)
    assert ms.read_frame(1) is None
    with open(ms._path(0), "r+b") as f:       # corrupt the payload on disk
        f.seek(28); f.write(b"\xff\xff\xff")
    assert ms.read_frame(0) is None            # treated as missing...
    assert not ms.has_frame(0)                 # ...and removed so it gets recomputed
    assert list_sets(str(tmp_path)) == ["clipA-1234-m@1"]
    set_active(str(tmp_path), "clipA-1234-m@1")
    assert get_active(str(tmp_path)) == "clipA-1234-m@1"


def test_settings_hash_depends_on_settings_only():
    assert settings_hash(mode="person", tier="high") == settings_hash(tier="high", mode="person")
    assert settings_hash(mode="person", tier="high") != settings_hash(mode="person", tier="draft")


def test_stabilizer_reduces_flicker_without_lagging_motion():
    rng = np.random.default_rng(0)
    base = np.zeros((120, 160), np.float32); base[40:80, 50:110] = 1
    st = TemporalStabilizer()
    # static subject + noise: output should be steadier than input
    outs, ins = [], []
    prev = None
    for i in range(12):
        noisy = np.clip(base + rng.normal(0, 0.15, base.shape), 0, 1).astype(np.float32)
        mag = np.zeros_like(base)
        out = st.stabilize(noisy, prev, mag, 0.95)
        outs.append(out); ins.append(noisy); prev = out
    d_in = np.mean([np.abs(ins[i] - ins[i - 1]).mean() for i in range(1, 12)])
    d_out = np.mean([np.abs(outs[i] - outs[i - 1]).mean() for i in range(1, 12)])
    assert d_out < 0.7 * d_in
    # fast motion: strong flow => (almost) no temporal blending, so no trailing lag
    moved = np.roll(base, 25, axis=1)
    mag = np.full_like(base, 25.0)
    out = st.stabilize(moved, base, mag, 0.95)
    assert iou(out, moved) > 0.97


def test_vram_message_matches_spec():
    rep = HardwareReport(gpus=[GpuInfo("nvidia", "Test", 8000, 900)])
    msg = check_vram(rep, "high")
    assert msg.startswith("GPU memory is insufficient for High Quality mode.")
    assert "Balanced mode or Draft mode" in msg
    assert check_vram(HardwareReport(gpus=[GpuInfo("nvidia", "T", 8000, 7000)]), "high") is None
