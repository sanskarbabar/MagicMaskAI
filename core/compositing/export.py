"""'Render': full-resolution alpha / cutout PNG sequences from the cached mattes.

The source is decoded again at its original resolution, the analysis-resolution matte is upscaled and refined
by the native edge pipeline (the same C++ code the OFX plugin runs) and written as 16-bit PNGs that can be
imported into Resolve as a matte or as a transparent cutout sequence.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Dict, Optional

import cv2
import numpy as np

from core.compositing import native
from core.compositing.native import EdgeSettings


def export_sequence(session, out_dir: str, edge: Optional[EdgeSettings] = None,
                    formats=("alpha", "cutout"), cancel=None) -> Dict[str, object]:
    edge = edge or EdgeSettings()
    edge.mode = "cutout"
    os.makedirs(out_dir, exist_ok=True)
    dirs = {f: os.path.join(out_dir, f) for f in formats}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)

    cap = cv2.VideoCapture(session.video_path)
    if not cap.isOpened():
        raise IOError(f"Cannot re-open the source video: {session.video_path}")
    total = session.n_frames
    p = session.progress
    p.state, p.frames_done, p.frames_total, p.started, p.eta_s = "rendering", 0, total, time.time(), None
    written = skipped = 0
    idx = 0
    try:
        while True:
            if cancel is not None and cancel.is_set():
                p.state, p.message = "cancelled", "Render cancelled."
                break
            ok, frame = cap.read()
            if not ok:
                break
            rec = session.matte.read_frame(idx)
            if rec is None:
                skipped += 1
            else:
                out = native.render(native.bgr8_to_rgba(frame), rec.alpha, edge)
                a16 = np.clip(out[..., 3] * 65535.0 + 0.5, 0, 65535).astype(np.uint16)
                if "alpha" in dirs:
                    cv2.imwrite(os.path.join(dirs["alpha"], f"{idx:06d}.png"), a16)
                if "cutout" in dirs:
                    bgra = np.empty(out.shape, np.uint16)
                    bgra[..., 0] = np.clip(out[..., 2] * 65535.0 + 0.5, 0, 65535)
                    bgra[..., 1] = np.clip(out[..., 1] * 65535.0 + 0.5, 0, 65535)
                    bgra[..., 2] = np.clip(out[..., 0] * 65535.0 + 0.5, 0, 65535)
                    bgra[..., 3] = a16
                    cv2.imwrite(os.path.join(dirs["cutout"], f"{idx:06d}.png"), bgra)
                written += 1
            idx += 1
            p.frames_done, p.current_frame = idx, idx
            el = time.time() - p.started
            p.eta_s = el / idx * (total - idx) if idx else None
            p.message = f"Rendering frame {idx}/{total}"
    finally:
        cap.release()
    if p.state == "rendering":
        p.state = "done"
        p.message = f"Render complete: {written} frame(s) written" + (f", {skipped} without a matte skipped." if skipped else ".")
    return {"dir": out_dir, "written": written, "skipped": skipped}
