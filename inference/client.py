"""Blocking client for the AI Cutout service (used by the Companion and the tests)."""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

import cv2
import numpy as np

from inference.server import state_dir


class ServiceError(RuntimeError):
    pass


class Client:
    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.rfile = None
        self._id = 0
        self._lock = threading.RLock()       # one request/response pair on the wire at a time (UI threads share a client)

    # ---------------------------------------------------------------- connection
    @staticmethod
    def read_info() -> Optional[dict]:
        try:
            with open(os.path.join(state_dir(), "service.json"), "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def connect(self) -> None:
        info = self.read_info()
        if not info:
            raise ServiceError("The AI Cutout service is not running.")
        s = socket.create_connection(("127.0.0.1", info["port"]), timeout=self.timeout)
        s.settimeout(self.timeout)
        self.sock, self.rfile, self.token = s, s.makefile("rb"), info["token"]

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = self.rfile = None

    @classmethod
    def ensure_service(cls, launch_cmd=None, wait: float = 40.0) -> "Client":
        """Connect to a running service, or start one and wait for it."""
        c = cls()
        try:
            c.connect(); c.call("hello")
            return c
        except (ServiceError, OSError):
            pass
        cmd = launch_cmd or default_launch_cmd()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(0.4)
            try:
                c = cls(); c.connect(); c.call("hello")
                return c
            except (ServiceError, OSError):
                continue
        raise ServiceError("The AI Cutout service did not start. See %LOCALAPPDATA%\\AICutout\\logs\\service.log")

    # ---------------------------------------------------------------- calls
    def call(self, cmd: str, **args) -> Dict[str, Any]:
        with self._lock:
            return self._call_locked(cmd, args)

    def _call_locked(self, cmd: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if self.sock is None:
            self.connect()
        self._id += 1
        req = {"token": self.token, "id": self._id, "cmd": cmd, **args}
        try:
            self.sock.sendall((json.dumps(req) + "\n").encode())
            line = self.rfile.readline()
        except OSError as e:
            self.close()
            raise ServiceError(f"Lost connection to the AI Cutout service: {e}")
        if not line:
            self.close()
            raise ServiceError("The AI Cutout service closed the connection.")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise ServiceError(resp.get("error", "unknown error"))
        return resp

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def decode_png(b64: Optional[str]) -> Optional[np.ndarray]:
        if not b64:
            return None
        return cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_UNCHANGED)

    @staticmethod
    def decode_jpg(b64: str) -> np.ndarray:
        return cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)

    def wait_idle(self, timeout: float = 600, poll: float = 0.3, cb=None) -> Dict[str, Any]:
        t0 = time.time()
        while True:
            st = self.call("status")
            if cb:
                cb(st)
            if st.get("error"):
                raise ServiceError(st["error"])
            if not st.get("busy"):
                return st
            if time.time() - t0 > timeout:
                raise ServiceError("Timed out waiting for the service.")
            time.sleep(poll)


def default_launch_cmd():
    here = os.path.dirname(os.path.abspath(sys.argv[0])) if getattr(sys, "frozen", False) else None
    if here:
        exe = os.path.join(here, "aicutout-service.exe")
        if os.path.isfile(exe):
            return [exe]
    return [sys.executable, "-m", "inference.server"]
