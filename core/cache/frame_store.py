"""Analysis-resolution proxy frames.

Decoding the source once into a JPEG store at the analysis resolution gives O(1) random access
(backward tracking, corrections) without re-seeking inter-frame codecs, and keeps 4K sources cheap
to analyse: the AI only ever sees the proxy, the original resolution is only used at render time.
"""
from __future__ import annotations

import os
import struct
import threading
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np

QUALITY_LONG_EDGE = {"draft": 512, "balanced": 768, "high": 1024}


def proxy_size(src_w: int, src_h: int, long_edge: int) -> Tuple[int, int]:
    scale = min(1.0, long_edge / float(max(src_w, src_h)))
    w = max(2, int(round(src_w * scale / 2)) * 2)
    h = max(2, int(round(src_h * scale / 2)) * 2)
    return w, h


class FrameStore:
    """Append-only JPEG store: frames.bin + frames.idx (u64 offsets, u32 sizes)."""

    def __init__(self, directory: str):
        self.dir = directory
        self.bin_path = os.path.join(directory, "proxy.bin")
        self.idx_path = os.path.join(directory, "proxy.idx")
        self.size: Tuple[int, int] = (0, 0)
        self.source_size: Tuple[int, int] = (0, 0)
        self.fps = 0.0
        self.offsets: List[Tuple[int, int]] = []
        self._lock = threading.Lock()
        self._fh = None

    # ---------------------------------------------------------------- build
    @classmethod
    def build(cls, video_path: str, directory: str, long_edge: int = 768,
              progress: Optional[Callable[[int, int], None]] = None,
              cancel: Optional[threading.Event] = None) -> "FrameStore":
        os.makedirs(directory, exist_ok=True)
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise IOError(f"cannot open video: {video_path}")
        sw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        sh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        pw, ph = proxy_size(sw, sh, long_edge)
        store = cls(directory)
        store.size, store.source_size, store.fps = (pw, ph), (sw, sh), fps
        offsets: List[Tuple[int, int]] = []
        pos = 0
        with open(store.bin_path, "wb") as out:
            i = 0
            while True:
                if cancel is not None and cancel.is_set():
                    break
                ok, frame = cap.read()
                if not ok:
                    break
                if (frame.shape[1], frame.shape[0]) != (pw, ph):
                    frame = cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 93])
                if not ok:
                    raise IOError("JPEG encode failed")
                b = buf.tobytes()
                out.write(b)
                offsets.append((pos, len(b)))
                pos += len(b)
                i += 1
                if progress:
                    progress(i, total)
        cap.release()
        with open(store.idx_path, "wb") as f:
            f.write(struct.pack("<IIIIf", len(offsets), pw, ph, 0, fps))
            f.write(struct.pack("<II", sw, sh))
            for off, sz in offsets:
                f.write(struct.pack("<QI", off, sz))
        store.offsets = offsets
        return store

    # ---------------------------------------------------------------- open
    @classmethod
    def open(cls, directory: str) -> "FrameStore":
        store = cls(directory)
        with open(store.idx_path, "rb") as f:
            n, pw, ph, _, fps = struct.unpack("<IIIIf", f.read(20))
            sw, sh = struct.unpack("<II", f.read(8))
            rec = struct.Struct("<QI")
            raw = f.read(rec.size * n)
        store.size, store.source_size, store.fps = (pw, ph), (sw, sh), fps
        store.offsets = [rec.unpack_from(raw, i * rec.size) for i in range(n)]
        return store

    @classmethod
    def exists(cls, directory: str) -> bool:
        return os.path.isfile(os.path.join(directory, "proxy.idx")) and os.path.isfile(os.path.join(directory, "proxy.bin"))

    def __len__(self) -> int:
        return len(self.offsets)

    def get(self, idx: int) -> np.ndarray:
        idx = max(0, min(len(self.offsets) - 1, idx))
        off, sz = self.offsets[idx]
        with self._lock:
            if self._fh is None:
                self._fh = open(self.bin_path, "rb")
            self._fh.seek(off)
            data = self._fh.read(sz)
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise IOError(f"corrupt proxy frame {idx}")
        return img

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None
