"""AI Cutout local inference service.

Listens on 127.0.0.1 only (never a routable interface), one JSON object per line, every request must carry
the per-user token stored in %LOCALAPPDATA%\\AICutout\\service.json. Nothing here talks to the internet:
video frames and masks stay on this machine.

Requests:  {"token": "...", "id": 1, "cmd": "status", ...args}
Responses: {"id": 1, "ok": true, ...}  or  {"id": 1, "ok": false, "error": "human readable"}
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
import traceback
from typing import Any, Dict, Optional

import cv2
import numpy as np

from core.cache.matte_store import default_cache_root
from core.segmentation.base import EngineError
from core.segmentation.registry import create_engine
from core.tracking.session import Session
from gpu.devices import check_vram, detect_hardware

VERSION = "0.2.0"
log = logging.getLogger("aicutout.service")


def state_dir() -> str:
    base = os.environ.get("AICUTOUT_STATE") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/state"), "AICutout")
    os.makedirs(base, exist_ok=True)
    return base


def _png_b64(arr8: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", arr8)
    return base64.b64encode(buf.tobytes()).decode()


def _jpg_b64(img: np.ndarray, q: int = 88) -> str:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return base64.b64encode(buf.tobytes()).decode()


class Service:
    def __init__(self, cache_root: Optional[str] = None, idle_timeout_s: float = 0):
        self.cache_root = cache_root or default_cache_root()
        os.makedirs(self.cache_root, exist_ok=True)
        self.hw = detect_hardware()
        self.session: Optional[Session] = None
        self.job: Optional[threading.Thread] = None
        self.job_error: Optional[str] = None
        self.lock = threading.RLock()
        self.token = secrets.token_hex(16)
        self.last_activity = time.time()
        self.idle_timeout_s = idle_timeout_s
        self.server: Optional[socketserver.ThreadingTCPServer] = None
        self._engines: Dict[tuple, Any] = {}          # (mode, tier) -> loaded engine (model load takes seconds)
        self._engine_msgs: Dict[tuple, list] = {}

    # ------------------------------------------------------------------ helpers
    def _need_session(self) -> Session:
        if self.session is None or self.session.frames is None:
            raise EngineError("No clip is open. Open a video first.")
        return self.session

    def _busy(self) -> bool:
        return self.job is not None and self.job.is_alive()

    def _start_job(self, fn, *a, **kw) -> None:
        if self._busy():
            raise EngineError("Another job is already running. Wait for it to finish or cancel it.")
        self.job_error = None

        def run():
            try:
                fn(*a, **kw)
            except EngineError as e:
                self.job_error = str(e)
                if self.session:
                    self.session.progress.state, self.session.progress.message = "error", str(e)
            except Exception as e:  # noqa: BLE001
                log.error("job failed: %s", traceback.format_exc())
                self.job_error = f"Unexpected failure: {e}"
                if self.session:
                    self.session.progress.state, self.session.progress.message = "error", self.job_error

        self.job = threading.Thread(target=run, daemon=True, name="aicutout-job")
        self.job.start()

    def _proxy_pts(self, s: Session, pts, normalized: bool):
        w, h = s.frames.size
        out = []
        for p in pts:
            x, y = float(p[0]), float(p[1])
            label = int(p[2]) if len(p) > 2 else 1
            out.append((x * w, y * h, label) if normalized else (x, y, label))
        return out

    def _proxy_box(self, s: Session, box, normalized: bool):
        if not box:
            return None
        w, h = s.frames.size
        b = [float(v) for v in box]
        return (b[0] * w, b[1] * h, b[2] * w, b[3] * h) if normalized else tuple(b)

    # ------------------------------------------------------------------ commands
    def handle(self, req: Dict[str, Any]) -> Dict[str, Any]:
        cmd = req.get("cmd")
        self.last_activity = time.time()
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            raise EngineError(f"Unknown command: {cmd}")
        return fn(req) or {}

    def cmd_hello(self, r):
        return {"version": VERSION, "hardware": self.hw.summary(), "device": self.hw.device_label,
                "cache_root": self.cache_root}

    def _engine_for(self, mode: str, tier: str):
        key = (mode, tier)
        if key not in self._engines:
            eng, msgs = create_engine(mode, tier, self.hw.selected)
            self._engines[key] = eng
            self._engine_msgs[key] = msgs
        return self._engines[key], list(self._engine_msgs[key])

    def cmd_open(self, r):
        video, mode, tier = r["video"], r.get("mode", "person"), r.get("tier", "balanced")
        if not os.path.isfile(video):
            raise FileNotFoundError(f"Source file not found: {video}")
        warn = check_vram(detect_hardware(), tier)
        if warn:
            raise EngineError(warn)
        with self.lock:
            cur = self.session
            if (cur is not None and cur.frames is not None and cur.video_path == video and cur.mode == mode
                    and cur.tier == tier and cur.progress.state != "error"):
                return {"opening": False, "reused": True, "set": cur.matte.name}
            if cur is not None:
                cur.cancel.set()

            def opener():
                eng, msgs = self._engine_for(mode, tier)
                s.engine = eng
                s.messages += msgs
                s.open()

            s = Session(video, self.cache_root, mode, tier, self.hw.selected)
            self.session = s
            set_name = s.plan_set_name()
        self._start_job(opener)
        return {"opening": True, "set": set_name}

    def cmd_status(self, r):
        s = self.session
        out: Dict[str, Any] = {"busy": self._busy(), "error": self.job_error, "device": self.hw.device_label}
        if s is None:
            out.update(state="idle", message="No clip open.")
            return out
        p = s.progress
        out.update(p.to_json())
        out.update(
            open=s.frames is not None, frames=s.n_frames, size=list(s.frames.size) if s.frames else None,
            fps=s.frames.fps if s.frames else 0, keyframes=sorted(s.keyframes),
            cached=len(s.matte.frames()) if s.matte else 0, set=s.matte.name if s.matte else None,
            low_confidence=s.low_confidence_frames() if s.matte else [], notes=s.messages)
        return out

    def cmd_timeline(self, r):
        """Per-frame state for the Companion's timeline strip."""
        s = self._need_session()
        n = s.n_frames
        cached = set(s.matte.frames())
        conf = [round(s.confidence[i], 3) if i in s.confidence else -1 for i in range(n)]
        return {"frames": n, "cached": sorted(cached),
                "keyframes": [{"idx": k.idx, "kind": k.kind} for k in sorted(s.keyframes.values(), key=lambda k: k.idx)],
                "low_confidence": s.low_confidence_frames(), "confidence": conf}

    def cmd_get_frame(self, r):
        s = self._need_session()
        img = s.frame(int(r["idx"]))
        mw = int(r.get("max_w", 0))
        if mw and img.shape[1] > mw:
            img = cv2.resize(img, (mw, int(img.shape[0] * mw / img.shape[1])), interpolation=cv2.INTER_AREA)
        return {"jpg": _jpg_b64(img), "w": int(img.shape[1]), "h": int(img.shape[0])}

    def cmd_get_mask(self, r):
        s = self._need_session()
        idx = int(r["idx"])
        rec = s.matte.read_frame(idx)
        if rec is None:
            return {"png": None}
        return {"png": _png_b64(rec.alpha), "confidence": rec.confidence, "manual": rec.manual,
                "low_confidence": rec.low_confidence}

    def cmd_segment(self, r):
        s = self._need_session()
        idx = int(r["idx"])
        norm = bool(r.get("normalized", False))
        pts = self._proxy_pts(s, r.get("points", []), norm)
        box = self._proxy_box(s, r.get("box"), norm)
        mask, conf = s.segment(idx, pts, box, use_previous=bool(r.get("use_previous", False)))
        return {"png": _png_b64(np.clip(mask * 255, 0, 255).astype(np.uint8)), "confidence": conf}

    def cmd_commit(self, r):
        s = self._need_session()
        idx = int(r["idx"])
        norm = bool(r.get("normalized", False))
        s.commit_keyframe(idx, self._proxy_pts(s, r.get("points", []), norm), self._proxy_box(s, r.get("box"), norm))
        return {}

    def cmd_refine(self, r):
        s = self._need_session()
        norm = bool(r.get("normalized", False))
        m = s.refine_frame(int(r["idx"]), self._proxy_pts(s, r.get("points", []), norm),
                           self._proxy_box(s, r.get("box"), norm))
        return {"png": _png_b64(np.clip(m * 255, 0, 255).astype(np.uint8))}

    def cmd_export(self, r):
        s = self._need_session()
        from core.compositing.export import export_sequence
        from core.compositing.native import EdgeSettings
        out = r.get("dir") or os.path.join(state_dir(), "exports", s.matte.name)
        e = r.get("edge", {}) or {}
        edge = EdgeSettings(edge_shift=float(e.get("edge_shift", 0)), feather=float(e.get("feather", 0)),
                            smooth=float(e.get("smooth", 0)), refine=float(e.get("refine", 0.5)),
                            decontaminate=float(e.get("decontaminate", 0)), spill=float(e.get("spill", 0)),
                            quality={"draft": 0, "balanced": 1, "high": 2}.get(s.tier, 1))
        self._start_job(export_sequence, s, out, edge, tuple(r.get("formats", ["alpha", "cutout"])), s.cancel)
        return {"exporting": True, "dir": out}

    def cmd_track(self, r):
        s = self._need_session()
        d = r.get("direction", "both")
        self._start_job(s.track, d, r.get("start"), r.get("end"), bool(r.get("recalc", False)))
        return {"tracking": True}

    def cmd_cancel(self, r):
        if self.session:
            self.session.cancel.set()
        return {}

    def cmd_reset(self, r):
        s = self._need_session()
        s.reset()
        return {}

    def cmd_shutdown(self, r):
        threading.Thread(target=self.shutdown, daemon=True).start()
        return {}

    # ------------------------------------------------------------------ server
    def serve(self, port: int = 0) -> int:
        svc = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for raw in self.rfile:
                    try:
                        req = json.loads(raw.decode("utf-8"))
                        if not secrets.compare_digest(str(req.get("token", "")), svc.token):
                            resp = {"id": req.get("id"), "ok": False, "error": "unauthorized"}
                        else:
                            try:
                                resp = {"id": req.get("id"), "ok": True, **svc.handle(req)}
                            except (EngineError, FileNotFoundError, IOError, KeyError, ValueError) as e:
                                resp = {"id": req.get("id"), "ok": False,
                                        "error": f"Missing argument: {e}" if isinstance(e, KeyError) else str(e)}
                            except Exception as e:  # noqa: BLE001
                                log.error("request failed: %s", traceback.format_exc())
                                resp = {"id": req.get("id"), "ok": False, "error": f"Unexpected failure: {e}"}
                    except json.JSONDecodeError:
                        resp = {"ok": False, "error": "bad request"}
                    try:
                        self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
                        self.wfile.flush()
                    except OSError:
                        return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = False
            daemon_threads = True

        self.server = Server(("127.0.0.1", port), Handler)
        actual = self.server.server_address[1]
        info = {"port": actual, "token": self.token, "pid": os.getpid(), "version": VERSION}
        path = os.path.join(state_dir(), "service.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(info, f)
        os.replace(tmp, path)
        self._info_path = path
        log.info("AI Cutout service listening on 127.0.0.1:%d", actual)
        if self.idle_timeout_s > 0:
            threading.Thread(target=self._idle_watch, daemon=True).start()
        self.server.serve_forever(poll_interval=0.3)
        return actual

    def _idle_watch(self):
        while True:
            time.sleep(15)
            if self._busy():
                self.last_activity = time.time()
            elif time.time() - self.last_activity > self.idle_timeout_s:
                log.info("idle timeout, shutting down")
                self.shutdown()
                return

    def shutdown(self):
        try:
            os.unlink(getattr(self, "_info_path", ""))
        except OSError:
            pass
        if self.server:
            self.server.shutdown()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="aicutout-service")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--idle-timeout", type=float, default=1800, help="seconds; 0 = never")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--fetch-models", nargs="+", choices=["tiny", "small", "base_plus"], metavar="VARIANT",
                    help="download and SHA-256-verify SAM 2 model files into the shared ProgramData models folder, then exit")
    a = ap.parse_args(argv)
    if a.fetch_models:
        from models.fetch_models import run
        dest = os.path.join(os.environ.get("PROGRAMDATA", "."), "AICutout", "models")
        run(a.fetch_models, dest)
        return
    logdir = os.path.join(state_dir(), "logs")
    os.makedirs(logdir, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(logdir, "service.log"), encoding="utf-8"),
                                  logging.StreamHandler(sys.stderr)])
    Service(a.cache, a.idle_timeout).serve(a.port)


if __name__ == "__main__":
    main()
