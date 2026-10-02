"""Tracking session: one clip, its proxy frames, keyframes, corrections and the matte cache.

Keyframes
---------
A keyframe is a frame whose mask is authoritative: either produced by the AI from the user's click
prompts ("ai") or hand-corrected ("manual"). Between two keyframes the tracker runs forward from the left
one and backward from the right one and blends the two passes by distance and confidence; beyond the
outermost keyframes it just propagates outward. Correcting frame 120 therefore recalculates only the
frames between the neighbouring keyframes ("recalculate 121 -> 180").
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from core.cache.frame_store import QUALITY_LONG_EDGE, FrameStore
from core.cache.matte_store import (FLAG_LOW_CONFIDENCE, FLAG_MANUAL, MatteSet, file_fingerprint, set_active,
                                    set_name, settings_hash)
from core.segmentation.base import EngineError, Prompts, SegmentationEngine
from core.segmentation.registry import create_engine
from core.tracking.tracker import FrameResult, MaskTracker, TrackerConfig


@dataclass
class Keyframe:
    idx: int
    kind: str                                   # "ai" | "manual"
    points: List[Tuple[float, float, int]] = field(default_factory=list)   # proxy px
    box: Optional[Tuple[float, float, float, float]] = None

    def to_json(self):
        return {"idx": self.idx, "kind": self.kind, "points": self.points, "box": self.box}


@dataclass
class Progress:
    state: str = "idle"          # idle | preparing | segmenting | tracking | done | cancelled | error
    message: str = ""
    frames_done: int = 0
    frames_total: int = 0
    current_frame: int = 0
    confidence: float = 1.0
    started: float = 0.0
    eta_s: Optional[float] = None
    low_confidence_frames: int = 0

    def to_json(self):
        return dict(self.__dict__)


class Session:
    def __init__(self, video_path: str, cache_root: str, mode: str = "person", tier: str = "balanced",
                 providers: Optional[List[str]] = None, engine: Optional[SegmentationEngine] = None):
        self.video_path = video_path
        self.cache_root = cache_root
        self.mode = mode
        self.tier = tier
        self.providers = providers
        self.progress = Progress()
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.messages: List[str] = []
        self.keyframes: Dict[int, Keyframe] = {}
        self.confidence: Dict[int, float] = {}
        self.flags: Dict[int, int] = {}
        self.engine = engine
        self.frames: Optional[FrameStore] = None
        self.matte: Optional[MatteSet] = None
        self.tracker: Optional[MaskTracker] = None
        self.preview_mask: Optional[np.ndarray] = None
        self.preview_frame = -1
        self.tracked_sig = ""

    # ------------------------------------------------------------------ setup
    def _settings(self) -> str:
        return settings_hash(mode=self.mode, tier=self.tier, long_edge=QUALITY_LONG_EDGE[self.tier])

    def plan_set_name(self) -> str:
        """Cache set name for this clip/settings/model, computed without loading anything heavy."""
        from core.segmentation.registry import plan_model
        mid, mver = plan_model(self.tier)
        return set_name(file_fingerprint(self.video_path), self._settings(), mid, mver)

    def open(self, progress_cb: Optional[Callable[[int, int], None]] = None) -> None:
        p = self.progress
        p.state, p.message, p.started = "preparing", "Reading video...", time.time()
        if not os.path.isfile(self.video_path):
            raise FileNotFoundError(f"Video not found: {self.video_path}")
        if self.engine is None:
            self.engine, msgs = create_engine(self.mode, self.tier, self.providers)
            self.messages += msgs
        info = self.engine.info()
        clip_key = file_fingerprint(self.video_path)
        name = self.plan_set_name()
        self.matte = MatteSet(self.cache_root, name)
        proxy_dir = os.path.join(self.matte.dir, "proxy")
        if FrameStore.exists(proxy_dir):
            self.frames = FrameStore.open(proxy_dir)
        else:
            os.makedirs(self.matte.dir, exist_ok=True)

            def cb(i, total):
                p.frames_done, p.frames_total = i, total
                p.message = f"Reading video... {i}/{total or '?'}"
                if progress_cb:
                    progress_cb(i, total)

            self.frames = FrameStore.build(self.video_path, proxy_dir, QUALITY_LONG_EDGE[self.tier], cb, self.cancel)
        n = len(self.frames)
        if n == 0:
            raise IOError("The video contains no readable frames.")
        pw, ph = self.frames.size
        if not self.matte.exists():
            self.matte.create({
                "width": pw, "height": ph, "frames": n, "fps": f"{self.frames.fps:.4f}",
                "source_width": self.frames.source_size[0], "source_height": self.frames.source_size[1],
                "source": self.video_path, "model": info.id, "model_version": info.version,
                "mode": self.mode, "tier": self.tier, "clip_key": clip_key,
            })
        self._load_tracking_state()
        set_active(self.cache_root, self.matte.name)
        self.tracker = MaskTracker(self.engine, self.frames, self.tier)
        p.state, p.message, p.frames_total = "idle", self.hardware_message(), n
        p.frames_done = len(self.matte.frames())

    def hardware_message(self) -> str:
        info = self.engine.info() if self.engine else None
        return f"Model loaded: {info.id}. Ready." if info else "Ready."

    def _load_tracking_state(self) -> None:
        st = self.matte.read_json("tracking.json", {})
        self.keyframes = {int(k["idx"]): Keyframe(int(k["idx"]), k["kind"], [tuple(p) for p in k.get("points", [])],
                                                   tuple(k["box"]) if k.get("box") else None)
                          for k in st.get("keyframes", [])}
        self.confidence = {int(k): float(v) for k, v in st.get("confidence", {}).items()}
        self.flags = {int(k): int(v) for k, v in st.get("flags", {}).items()}
        self.tracked_sig = st.get("tracked_sig", "")

    def save_state(self) -> None:
        self.matte.write_json("tracking.json", {
            "keyframes": [k.to_json() for k in sorted(self.keyframes.values(), key=lambda k: k.idx)],
            "confidence": self.confidence, "flags": self.flags, "tracked_sig": self.tracked_sig})

    # ------------------------------------------------------------------ helpers
    @property
    def n_frames(self) -> int:
        return len(self.frames) if self.frames else 0

    def frame(self, idx: int) -> np.ndarray:
        return self.frames.get(idx)

    def alpha(self, idx: int) -> Optional[np.ndarray]:
        rec = self.matte.read_frame(idx)
        return None if rec is None else rec.alpha.astype(np.float32) / 255.0

    def _write(self, idx: int, alpha: np.ndarray, conf: float, flags: int) -> None:
        a8 = np.clip(alpha * 255.0 + 0.5, 0, 255).astype(np.uint8)
        self.matte.write_frame(idx, a8, conf, flags)
        self.confidence[idx] = float(conf)
        self.flags[idx] = flags

    # ------------------------------------------------------------------ segmentation (Analyze)
    def segment(self, idx: int, points: List[Tuple[float, float, int]],
                box: Optional[Tuple[float, float, float, float]] = None,
                use_previous: bool = False) -> Tuple[np.ndarray, float]:
        """Segment frame idx from the user's positive/negative points (proxy pixel coordinates)."""
        img = self.frame(idx)
        with self.lock:
            self.engine.set_image(img)
            prompts = Prompts(points=list(points), box=box)
            if prompts.is_empty():
                auto = self.engine.auto_prompts(img)
                if auto is None:
                    raise EngineError("Click the subject first (left click = include, right click = exclude).")
                prompts = auto
            if use_previous and self.preview_mask is not None and self.preview_frame == idx:
                prompts.mask = self.preview_mask
            prop = self.engine.predict(prompts)
        self.preview_mask, self.preview_frame = prop.mask, idx
        return prop.mask, prop.confidence

    def commit_keyframe(self, idx: int, points, box=None, manual=False, mask: Optional[np.ndarray] = None,
                        confidence: float = 1.0) -> None:
        m = mask if mask is not None else (self.preview_mask if self.preview_frame == idx else None)
        if m is None:
            m, confidence = self.segment(idx, points, box)
        kind = "manual" if manual else "ai"
        self.keyframes[idx] = Keyframe(idx, kind, list(points), box)
        self._write(idx, m, confidence, FLAG_MANUAL if manual else 0)
        self.save_state()

    # ------------------------------------------------------------------ manual correction
    def refine_frame(self, idx: int, points, box=None) -> np.ndarray:
        """AI refinement of the current mask using extra include/exclude points (a 'soft' correction)."""
        cur = self.alpha(idx)
        img = self.frame(idx)
        with self.lock:
            self.engine.set_image(img)
            prop = self.engine.predict(Prompts(points=list(points), box=box, mask=cur))
        self.keyframes[idx] = Keyframe(idx, "manual", list(points), box)
        self._write(idx, prop.mask, max(prop.confidence, 0.9), FLAG_MANUAL)
        self.preview_mask, self.preview_frame = prop.mask, idx
        self.save_state()
        return prop.mask

    # ------------------------------------------------------------------ tracking
    def _neighbours(self, idx: int) -> Tuple[Optional[int], Optional[int]]:
        ks = sorted(self.keyframes)
        left = max((k for k in ks if k < idx), default=None)
        right = min((k for k in ks if k > idx), default=None)
        return left, right

    def _report(self, res: FrameResult, total: int, done: int, t0: float) -> None:
        p = self.progress
        p.current_frame, p.confidence = res.idx, res.confidence
        p.frames_done, p.frames_total = done, total
        if res.low_confidence:
            p.low_confidence_frames += 1
        el = time.time() - t0
        p.eta_s = (el / done) * (total - done) if done else None
        p.message = f"Tracking frame {res.idx} - confidence {res.confidence:.0%}"

    def track(self, direction: str = "both", start: Optional[int] = None,
              end: Optional[int] = None, recalc: bool = False) -> None:
        """Track from keyframes. direction: forward | backward | both.

        Frames between two keyframes are computed by tracking both ways and blending; frames outside the
        outermost keyframes follow the requested direction. recalc=True recomputes frames even if cached.
        """
        p = self.progress
        self.cancel.clear()
        if not self.keyframes:
            raise EngineError("Select a subject and press Analyze before tracking.")
        n = self.n_frames
        lo = 0 if start is None else max(0, start)
        hi = n - 1 if end is None else min(n - 1, end)
        ks = sorted(self.keyframes)
        for k in ks:
            if not self.matte.has_frame(k):     # keyframe file missing (corrupt / cleared): cannot track from it
                raise EngineError(f"Keyframe {k} has no mask; re-run Analyze on that frame.")

        jobs: List[Tuple[str, int, int]] = []      # (kind, a, b)
        for a, b in zip(ks, ks[1:]):
            if b - a > 1 and hi >= a and lo <= b:
                jobs.append(("between", a, b))
        if direction in ("forward", "both") and ks[-1] < hi:
            jobs.append(("forward", ks[-1], hi))
        if direction in ("backward", "both") and ks[0] > lo:
            jobs.append(("backward", ks[0], lo))

        sig = settings_hash(kf=repr([(k.idx, k.kind, k.points, k.box) for k in sorted(self.keyframes.values(), key=lambda k: k.idx)]),
                            direction=direction, lo=lo, hi=hi)
        if (not recalc and self.tracked_sig == sig
                and all(self.matte.has_frame(i) for i in range(lo, hi + 1))):
            p.state, p.message, p.frames_done, p.frames_total = "done", "Already tracked (cached).", hi - lo + 1, hi - lo + 1
            return
        total = sum(abs(b - a) - (1 if kind == "between" else 0) for kind, a, b in jobs) or 1
        p.state, p.frames_done, p.frames_total, p.started, p.low_confidence_frames = "tracking", 0, total, time.time(), 0
        done = 0
        try:
            for kind, a, b in jobs:
                if self.cancel.is_set():
                    break
                if kind == "between":
                    done = self._track_between(a, b, done, total, recalc)
                else:
                    d = 1 if kind == "forward" else -1
                    start_alpha = self.alpha(a)
                    for res in self.tracker.propagate(a, start_alpha, d, b, self.cancel):
                        if self.cancel.is_set():
                            break
                        self._store(res)
                        done += 1
                        self._report(res, total, done, p.started)
            if not self.cancel.is_set():
                self.tracked_sig = sig
            self.save_state()
            p.state = "cancelled" if self.cancel.is_set() else "done"
            p.message = "Tracking cancelled." if self.cancel.is_set() else "Tracking complete."
        except EngineError as e:
            p.state, p.message = "error", str(e)
            raise
        except Exception as e:  # noqa: BLE001
            p.state, p.message = "error", f"Tracking failed: {e}"
            raise

    def _store(self, res: FrameResult) -> None:
        if res.idx in self.keyframes and self.keyframes[res.idx].kind == "manual":
            return       # never overwrite a manual correction
        flags = FLAG_LOW_CONFIDENCE if res.low_confidence else 0
        self._write(res.idx, res.alpha, res.confidence, flags)

    def _track_between(self, a: int, b: int, done: int, total: int, recalc: bool) -> int:
        """Forward from a, backward from b, blend by distance x confidence."""
        fwd: Dict[int, FrameResult] = {}
        bwd: Dict[int, FrameResult] = {}
        for res in self.tracker.propagate(a, self.alpha(a), 1, b - 1, self.cancel):
            fwd[res.idx] = res
            done += 1
            self._report(res, total, done, self.progress.started)
        for res in self.tracker.propagate(b, self.alpha(b), -1, a + 1, self.cancel):
            bwd[res.idx] = res
        span = float(b - a)
        for i in range(a + 1, b):
            f, g = fwd.get(i), bwd.get(i)
            if f is None and g is None:
                continue
            if f is None:
                self._store(g); continue
            if g is None:
                self._store(f); continue
            wf = ((b - i) / span) * max(f.confidence, 0.05)
            wb = ((i - a) / span) * max(g.confidence, 0.05)
            s = wf + wb
            alpha = (wf * f.alpha + wb * g.alpha) / s
            conf = (wf * f.confidence + wb * g.confidence) / s
            low = f.low_confidence and g.low_confidence
            self._store(FrameResult(i, alpha, conf, low, f.lost and g.lost))
        return done

    def reset(self) -> None:
        for i in self.matte.frames():
            self.matte.delete_frame(i)
        self.keyframes.clear(); self.confidence.clear(); self.flags.clear(); self.tracked_sig = ""
        self.preview_mask, self.preview_frame = None, -1
        self.save_state()
        self.progress = Progress(state="idle", message="Reset.", frames_total=self.n_frames)

    def low_confidence_frames(self) -> List[int]:
        return sorted(i for i, f in self.flags.items() if f & FLAG_LOW_CONFIDENCE)
