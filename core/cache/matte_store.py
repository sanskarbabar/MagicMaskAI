"""Persistent per-frame matte cache.

One matte set = one directory:

    <root>/<set_name>/
        meta.ini            flat key=value (read by the OFX plugin, so no JSON there)
        tracking.json       keyframes, per-frame confidence/flags (service/companion only)
        masks/000123.acm    one file per frame, written atomically (temp + rename)
        previews/           optional thumbnails

Frame file (.acm) layout, little endian, 24-byte header + payload (kept in sync with
plugin/OFX/... matte_store.cpp):

    u8[4] magic "ACM1"
    u32   width
    u32   height
    u32   crc32 of payload
    u8    flags   (bit0 = manual keyframe, bit1 = low confidence)
    u8    confidence (0..255)
    u16   reserved
    u32   payload length
    payload: run-length coded 8-bit alpha, repeated (u8 value, u16 run_length)

A frame whose header, length or CRC does not match is treated as missing and recomputed.
"""
from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import zlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import numpy as np

MAGIC = b"ACM1"
HEADER = struct.Struct("<4sIIIBBHI")
assert HEADER.size == 24
SCHEMA_VERSION = 1

FLAG_MANUAL = 1
FLAG_LOW_CONFIDENCE = 2
FLAG_PROVISIONAL = 4          # shown in the viewer but not yet confirmed by the user

MAX_RUN = 0xFFFF


def rle_encode(alpha: np.ndarray) -> bytes:
    """Encode a 2-D uint8 array as (value, u16 run) triples, vectorised."""
    flat = np.ascontiguousarray(alpha, dtype=np.uint8).ravel()
    if flat.size == 0:
        return b""
    change = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [flat.size]))
    lengths = ends - starts
    values = flat[starts]
    # split runs longer than 65535
    if lengths.max() > MAX_RUN:
        v2, l2 = [], []
        for v, l in zip(values.tolist(), lengths.tolist()):
            while l > MAX_RUN:
                v2.append(v)
                l2.append(MAX_RUN)
                l -= MAX_RUN
            v2.append(v)
            l2.append(l)
        values = np.asarray(v2, dtype=np.uint8)
        lengths = np.asarray(l2, dtype=np.int64)
    out = np.empty((values.size, 3), dtype=np.uint8)
    out[:, 0] = values
    out[:, 1] = (lengths & 0xFF).astype(np.uint8)
    out[:, 2] = ((lengths >> 8) & 0xFF).astype(np.uint8)
    return out.tobytes()


def rle_decode(payload: bytes, width: int, height: int) -> np.ndarray:
    if len(payload) % 3:
        raise ValueError("bad RLE payload length")
    arr = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3)
    lengths = arr[:, 1].astype(np.int64) | (arr[:, 2].astype(np.int64) << 8)
    if int(lengths.sum()) != width * height:
        raise ValueError("RLE run total does not match frame size")
    return np.repeat(arr[:, 0], lengths).reshape(height, width)


def pack_frame(alpha: np.ndarray, confidence: float, flags: int) -> bytes:
    h, w = alpha.shape
    payload = rle_encode(alpha)
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    conf = int(round(max(0.0, min(1.0, confidence)) * 255))
    return HEADER.pack(MAGIC, w, h, crc, flags & 0xFF, conf, 0, len(payload)) + payload


@dataclass
class FrameRecord:
    alpha: np.ndarray
    confidence: float
    flags: int

    @property
    def manual(self) -> bool:
        return bool(self.flags & FLAG_MANUAL)

    @property
    def low_confidence(self) -> bool:
        return bool(self.flags & FLAG_LOW_CONFIDENCE)


def unpack_frame(data: bytes) -> FrameRecord:
    if len(data) < HEADER.size:
        raise ValueError("truncated frame file")
    magic, w, h, crc, flags, conf, _res, plen = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ValueError("bad magic")
    payload = data[HEADER.size:HEADER.size + plen]
    if len(payload) != plen:
        raise ValueError("truncated payload")
    if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
        raise ValueError("CRC mismatch")
    return FrameRecord(rle_decode(payload, w, h), conf / 255.0, flags)


# --------------------------------------------------------------------------- hashing

def file_fingerprint(path: str) -> str:
    """Cheap, stable identity of a media file: path, size, mtime and a sample of its bytes."""
    st = os.stat(path)
    h = hashlib.sha256()
    h.update(os.path.abspath(path).lower().encode())
    h.update(struct.pack("<QQ", st.st_size, int(st.st_mtime)))
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > (2 << 20):
            f.seek(st.st_size // 2)
            h.update(f.read(1 << 20))
            f.seek(max(0, st.st_size - (1 << 20)))
            h.update(f.read(1 << 20))
    return h.hexdigest()[:16]


def settings_hash(**settings) -> str:
    """Hash of every setting that changes the *analysis result* (never edge/feather sliders)."""
    blob = "|".join(f"{k}={settings[k]!r}" for k in sorted(settings))
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def default_cache_root() -> str:
    env = os.environ.get("AICUTOUT_CACHE")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "AICutout", "cache")


def set_name(clip_key: str, settings: str, model_id: str, model_version: str) -> str:
    safe_model = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{model_id}@{model_version}")
    return f"{clip_key}-{settings}-{safe_model}"


# --------------------------------------------------------------------------- matte set

def _atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class MatteSet:
    def __init__(self, root: str, name: str):
        self.root = root
        self.name = name
        self.dir = os.path.join(root, name)
        self.masks_dir = os.path.join(self.dir, "masks")
        self.previews_dir = os.path.join(self.dir, "previews")

    # -- lifecycle
    def create(self, meta: Dict[str, object]) -> None:
        os.makedirs(self.masks_dir, exist_ok=True)
        os.makedirs(self.previews_dir, exist_ok=True)
        meta = dict(meta)
        meta.setdefault("schema", SCHEMA_VERSION)
        self.write_meta(meta)

    def exists(self) -> bool:
        return os.path.isfile(os.path.join(self.dir, "meta.ini"))

    def write_meta(self, meta: Dict[str, object]) -> None:
        text = "".join(f"{k}={v}\n" for k, v in meta.items())
        _atomic_write(os.path.join(self.dir, "meta.ini"), text.encode())

    def read_meta(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        with open(os.path.join(self.dir, "meta.ini"), "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    out[k] = v
        return out

    # -- frames
    def _path(self, idx: int) -> str:
        return os.path.join(self.masks_dir, f"{idx:06d}.acm")

    def write_frame(self, idx: int, alpha: np.ndarray, confidence: float = 1.0, flags: int = 0) -> None:
        _atomic_write(self._path(idx), pack_frame(alpha, confidence, flags))

    def read_frame(self, idx: int) -> Optional[FrameRecord]:
        """Return the frame, or None if missing or corrupt (corrupt files are removed)."""
        p = self._path(idx)
        try:
            with open(p, "rb") as f:
                return unpack_frame(f.read())
        except FileNotFoundError:
            return None
        except ValueError:
            try:
                os.unlink(p)
            except OSError:
                pass
            return None

    def has_frame(self, idx: int) -> bool:
        return os.path.isfile(self._path(idx))

    def delete_frame(self, idx: int) -> None:
        try:
            os.unlink(self._path(idx))
        except FileNotFoundError:
            pass

    def frames(self) -> List[int]:
        if not os.path.isdir(self.masks_dir):
            return []
        return sorted(int(n[:-4]) for n in os.listdir(self.masks_dir) if n.endswith(".acm") and n[:-4].isdigit())

    # -- sidecar json
    def write_json(self, name: str, obj) -> None:
        import json
        _atomic_write(os.path.join(self.dir, name), json.dumps(obj, indent=1).encode())

    def read_json(self, name: str, default=None):
        import json
        try:
            with open(os.path.join(self.dir, name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return default


def list_sets(root: str) -> List[str]:
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n, "meta.ini")))


def set_active(root: str, name: str) -> None:
    os.makedirs(root, exist_ok=True)
    _atomic_write(os.path.join(root, "_active.txt"), name.encode())


def get_active(root: str) -> Optional[str]:
    try:
        with open(os.path.join(root, "_active.txt"), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None
