"""Download and verify SAM 2 ONNX model files.

    python models/fetch_models.py tiny small base_plus        # default destination: models/weights
    python models/fetch_models.py --dest "%PROGRAMDATA%\\AICutout\\models" small

Source: https://huggingface.co/vietanhdev/segment-anything-2-onnx-models (Apache-2.0, ONNX export of Meta's SAM 2).
Every file is checked against the SHA-256 in models/manifest.json; a mismatch is deleted and reported. Files are
downloaded over HTTPS to your disk only — nothing is uploaded anywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request

HERE = os.path.join(getattr(sys, "_MEIPASS"), "models") if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
BASE = "https://huggingface.co/vietanhdev/segment-anything-2-onnx-models/resolve/main"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(variant: str, dest: str, manifest: dict) -> None:
    for part in ("encoder", "decoder"):
        name = f"sam2_hiera_{variant}.{part}.onnx"
        path = os.path.join(dest, name)
        want = manifest["files"].get(name)
        if os.path.isfile(path) and want and sha256(path) == want:
            print(f"ok      {name} (already present, hash verified)")
            continue
        print(f"fetch   {name} ...", flush=True)
        os.makedirs(dest, exist_ok=True)
        tmp = path + ".part"
        with urllib.request.urlopen(f"{BASE}/{name}") as r, open(tmp, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        got = sha256(tmp)
        if want and got != want:
            os.unlink(tmp)
            raise SystemExit(f"HASH MISMATCH for {name}: expected {want}, got {got}. File deleted.")
        os.replace(tmp, path)
        print(f"ok      {name} sha256={got[:16]}...")


def run(variants, dest: str) -> None:
    with open(os.path.join(HERE, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    for v in variants:
        fetch(v, os.path.expandvars(dest), manifest)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("variants", nargs="+", choices=["tiny", "small", "base_plus"])
    ap.add_argument("--dest", default=os.path.join(HERE, "weights"))
    a = ap.parse_args()
    run(a.variants, a.dest)


if __name__ == "__main__":
    main()
