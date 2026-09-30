"""Tracking benchmark over the synthetic scenarios.

    python -m tests.bench_tracking --engine grabcut
    python -m tests.bench_tracking --engine sam:tiny --scenarios static,pan,occlusion --sheet out.png
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import cv2
import numpy as np

from core.masking.metrics import iou, summarize
from core.segmentation.base import Prompts
from core.segmentation.grabcut import GrabCutEngine
from core.segmentation.registry import model_dirs
from core.segmentation.sam2_onnx import Sam2OnnxEngine
from core.tracking.tracker import MaskTracker
from gpu.devices import detect_hardware
from tests.synth import SCENARIOS, ListFrames, render


def make_engine(spec: str):
    if spec == "grabcut":
        e = GrabCutEngine(); e.load(); return e
    kind, _, variant = spec.partition(":")
    assert kind == "sam"
    for d in model_dirs():
        e = Sam2OnnxEngine(variant or "tiny", d)
        if e.available():
            e.load(detect_hardware().selected)
            return e
    raise SystemExit("SAM model not found")


def run_scenario(name: str, engine, tier: str = "balanced", init: str = "click"):
    sc = SCENARIOS[name]
    frames, gts = render(sc)
    prov = ListFrames(frames)
    gt0 = gts[0].astype(np.float32) / 255
    ys, xs = np.nonzero(gt0 > 0.5)
    cx, cy = float(xs.mean()), float(ys.mean())
    if init == "gt":
        m0 = gt0
    else:
        engine.set_image(frames[0])
        m0 = engine.predict(Prompts(points=[(cx, cy, 1)], box=(xs.min() - 6, ys.min() - 6, xs.max() + 6, ys.max() + 6))).mask
    tr = MaskTracker(engine, prov, tier)
    preds = [m0]
    conf = [1.0]
    t0 = time.time()
    for r in tr.propagate(0, m0, 1, len(frames) - 1):
        preds.append(r.alpha); conf.append(r.confidence)
    dt = time.time() - t0
    gtf = [g.astype(np.float32) / 255 for g in gts]
    s = summarize(preds, gtf)
    s["fps"] = (len(frames) - 1) / dt
    s["init_iou"] = iou(m0, gt0)
    s["min_conf"] = float(min(conf))
    return s, frames, preds, gtf


def contact_sheet(frames, preds, gts, idxs):
    tiles = []
    for i in idxs:
        f = frames[i].copy()
        ov = f.copy()
        ov[preds[i] > 0.5] = (0, 0, 255)
        g = cv2.cvtColor((gts[i] * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
        row = np.hstack([cv2.addWeighted(f, 0.5, ov, 0.5, 0), g])
        tiles.append(cv2.resize(row, None, fx=0.5, fy=0.5))
    return np.vstack(tiles)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="grabcut")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--tier", default="balanced")
    ap.add_argument("--init", default="click", choices=["click", "gt"])
    ap.add_argument("--sheet", default="")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    eng = make_engine(a.engine)
    results = {}
    print(f"engine={a.engine} tier={a.tier} init={a.init}")
    print(f"{'scenario':<11}{'IoU':>7}{'minIoU':>8}{'F':>7}{'flicker':>9}{'areaJump':>9}{'fps':>7}{'minConf':>8}")
    for name in a.scenarios.split(","):
        s, frames, preds, gts = run_scenario(name, eng, a.tier, a.init)
        results[name] = s
        print(f"{name:<11}{s['iou_mean']:7.3f}{s['iou_min']:8.3f}{s['f_mean']:7.3f}{s['flicker']:9.4f}{s['max_area_jump']:9.3f}{s['fps']:7.2f}{s['min_conf']:8.2f}")
        if a.sheet:
            n = len(frames)
            cv2.imwrite(a.sheet.replace(".png", f"_{name}.png"), contact_sheet(frames, preds, gts, [0, n // 3, 2 * n // 3, n - 1]))
    if a.json:
        json.dump(results, open(a.json, "w"), indent=1)
