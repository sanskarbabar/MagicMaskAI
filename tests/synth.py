"""Synthetic test clips with exact ground-truth mattes.

A textured 'world' with a person-like textured sprite (torso, head, thin arms) is rendered through a
per-frame camera transform (pan / zoom / rotation), optionally with motion blur, an occluder, low light
or a background whose colour matches the subject.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np


@dataclass
class Scenario:
    name: str
    frames: int = 40
    size: Tuple[int, int] = (640, 360)
    pan: float = 0.0            # px per frame camera pan
    zoom: float = 0.0           # scale change per frame (e.g. 0.004)
    rot: float = 0.0            # degrees per frame
    speed: float = 3.0          # subject speed in px/frame
    blur: int = 1               # motion-blur sub-steps
    occluder: bool = False
    exposure: float = 1.0
    similar_bg: bool = False
    hair: bool = False


def _texture(h, w, seed, base, amp=40):
    rng = np.random.default_rng(seed)
    n = rng.normal(0, 1, (h // 4 + 1, w // 4 + 1, 3)).astype(np.float32)
    n = cv2.resize(n, (w, h), interpolation=cv2.INTER_CUBIC)
    fine = rng.normal(0, 1, (h, w, 3)).astype(np.float32) * 0.35
    img = np.array(base, np.float32)[None, None] + amp * (n + fine)
    return np.clip(img, 0, 255).astype(np.uint8)


def _sprite(scale: float, hair: bool, seed=7):
    """Return (bgr sprite, alpha sprite) with transparent background."""
    H, W = int(260 * scale), int(140 * scale)
    a = np.zeros((H, W), np.uint8)
    cx = W // 2
    cv2.ellipse(a, (cx, int(H * 0.62)), (int(W * 0.30), int(H * 0.30)), 0, 0, 360, 255, -1)         # torso
    cv2.circle(a, (cx, int(H * 0.17)), int(W * 0.19), 255, -1)                                        # head
    cv2.line(a, (int(W * 0.20), int(H * 0.45)), (int(W * 0.02), int(H * 0.75)), 255, max(3, int(9 * scale)))   # arm L
    cv2.line(a, (int(W * 0.80), int(H * 0.45)), (int(W * 0.98), int(H * 0.72)), 255, max(3, int(9 * scale)))   # arm R
    cv2.rectangle(a, (int(W * 0.30), int(H * 0.85)), (int(W * 0.46), H - 1), 255, -1)                 # leg L
    cv2.rectangle(a, (int(W * 0.54), int(H * 0.85)), (int(W * 0.70), H - 1), 255, -1)                 # leg R
    if hair:
        rng = np.random.default_rng(3)
        for _ in range(60):
            ang = rng.uniform(-2.9, -0.25)
            r0, r1 = W * 0.19, W * 0.19 + rng.uniform(6, 18) * scale
            p0 = (int(cx + r0 * np.cos(ang)), int(H * 0.17 + r0 * np.sin(ang)))
            p1 = (int(cx + r1 * np.cos(ang)), int(H * 0.17 + r1 * np.sin(ang)))
            cv2.line(a, p0, p1, 255, 1, cv2.LINE_AA)
    a = cv2.GaussianBlur(a, (0, 0), 0.8)
    color = _texture(H, W, seed, (40, 60, 200), 35)
    # different colours for head vs torso so the subject is not uniform
    color[: int(H * 0.30)] = _texture(int(H * 0.30), W, seed + 1, (90, 150, 210), 25)
    return color, a


def render(sc: Scenario) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    w, h = sc.size
    wh, ww = h * 2, w * 2 + int(abs(sc.pan) * sc.frames) + 200
    bg_base = (60, 170, 70) if not sc.similar_bg else (55, 75, 190)
    world_bg = _texture(wh, ww, 1, bg_base, 45)
    # a few distinct landmarks so optical flow has structure
    rng = np.random.default_rng(5)
    for _ in range(40):
        c = (int(rng.integers(0, ww)), int(rng.integers(0, wh)))
        cv2.circle(world_bg, c, int(rng.integers(6, 22)), tuple(int(v) for v in rng.integers(20, 235, 3)), -1)
    world_bg = cv2.GaussianBlur(world_bg, (0, 0), 0.7)

    spr, spa = _sprite(1.0, sc.hair)
    sh, sw = spa.shape
    frames, gts = [], []
    for t in range(sc.frames):
        sub_frames, sub_gts = [], []
        for s in range(sc.blur):
            tt = t + (s / max(sc.blur, 1)) - (0.5 if sc.blur > 1 else 0)
            # subject path in world coordinates: walks right with a vertical bob
            x = ww * 0.5 - 60 + sc.speed * (tt - sc.frames / 2) + 20 * np.sin(tt * 0.3)
            y = wh * 0.5 - sh * 0.5 + 8 * np.sin(tt * 0.45)
            world = world_bg.copy()
            galpha = np.zeros((wh, ww), np.uint8)
            x0, y0 = int(round(x)), int(round(y))
            # sprite bob rotation-free; composite
            ys0, xs0 = max(0, y0), max(0, x0)
            ye, xe = min(wh, y0 + sh), min(ww, x0 + sw)
            if ye > ys0 and xe > xs0:
                sp = spr[ys0 - y0: ye - y0, xs0 - x0: xe - x0].astype(np.float32)
                sa = spa[ys0 - y0: ye - y0, xs0 - x0: xe - x0].astype(np.float32) / 255.0
                reg = world[ys0:ye, xs0:xe].astype(np.float32)
                world[ys0:ye, xs0:xe] = (sp * sa[..., None] + reg * (1 - sa[..., None])).astype(np.uint8)
                galpha[ys0:ye, xs0:xe] = (sa * 255).astype(np.uint8)
            if sc.occluder:
                ox = int(ww * 0.5 - 200 + 9 * (tt))
                cv2.rectangle(world, (ox, wh // 2 - 200), (ox + 34, wh // 2 + 200), (30, 30, 30), -1)
                cv2.rectangle(galpha, (ox, wh // 2 - 200), (ox + 34, wh // 2 + 200), 0, -1)
            # camera: centre on world centre + pan, with zoom/rot
            cx = ww / 2 + sc.pan * (tt - sc.frames / 2)
            cy = wh / 2
            scale = 1.0 + sc.zoom * (tt - sc.frames / 2)
            M = cv2.getRotationMatrix2D((cx, cy), sc.rot * (tt - sc.frames / 2), scale)
            M[0, 2] += w / 2 - cx
            M[1, 2] += h / 2 - cy
            fr = cv2.warpAffine(world, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            gt = cv2.warpAffine(galpha, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
            sub_frames.append(fr.astype(np.float32))
            sub_gts.append(gt.astype(np.float32))
        fr = np.mean(sub_frames, axis=0)
        gt = np.mean(sub_gts, axis=0)
        fr = np.clip(fr * sc.exposure, 0, 255)
        if sc.exposure < 0.5:
            fr = fr + np.random.default_rng(t).normal(0, 3.0, fr.shape)     # sensor noise
        frames.append(np.clip(fr, 0, 255).astype(np.uint8))
        gts.append(np.clip(gt, 0, 255).astype(np.uint8))
    return frames, gts


SCENARIOS = {
    "static": Scenario("static", speed=2.5),
    "pan": Scenario("pan", pan=4.0, speed=2.0),
    "zoom": Scenario("zoom", zoom=0.006, speed=2.0),
    "rotation": Scenario("rotation", rot=0.7, speed=2.0),
    "fast": Scenario("fast", speed=11.0),
    "blur": Scenario("blur", speed=9.0, blur=6),
    "occlusion": Scenario("occlusion", occluder=True, speed=2.0, frames=50),
    "lowlight": Scenario("lowlight", exposure=0.30, speed=2.5),
    "similar_bg": Scenario("similar_bg", similar_bg=True, speed=2.5),
    "hair": Scenario("hair", hair=True, speed=2.5),
}


def write_video(path: str, frames: List[np.ndarray], fps: float = 24.0) -> None:
    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not vw.isOpened():
        raise IOError("VideoWriter failed")
    for f in frames:
        vw.write(f)
    vw.release()


class ListFrames:
    """Frame provider over an in-memory list (same interface as FrameStore)."""
    def __init__(self, frames):
        self.frames = frames

    def __len__(self):
        return len(self.frames)

    def get(self, i):
        return self.frames[max(0, min(len(self.frames) - 1, i))]
