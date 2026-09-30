"""Measure segmentation quality, temporal stability, throughput, GPU memory and CPU use on the synthetic suite.

    python scripts/bench.py --variants tiny small base_plus --scenarios static,pan,zoom --out build/bench.json

GPU memory is the increase of nvidia-smi's global memory.used while the run is active (an upper bound of what this
process uses; other programs, including Resolve, also move that number). CPU is process CPU time / wall time
(1.0 = one core fully busy). Numbers describe *this machine* and *synthetic* clips; see docs/BENCHMARKS.md.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.segmentation.registry import model_dirs                # noqa: E402
from core.segmentation.sam2_onnx import Sam2OnnxEngine           # noqa: E402
from gpu.devices import detect_hardware                          # noqa: E402
from tests.bench_tracking import run_scenario                    # noqa: E402
from tests.synth import SCENARIOS                                # noqa: E402


def vram_used_mb() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        return int(out[0])
    except Exception:
        return -1


class VramSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.peak = -1
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            v = vram_used_mb()
            self.peak = max(self.peak, v)
            time.sleep(0.25)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", nargs="+", default=["tiny"])
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--tier", default="balanced")
    ap.add_argument("--device", default="auto", help="auto|dml|cpu")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    hw = detect_hardware(a.device)
    print(hw.summary())
    results = {}
    for variant in a.variants:
        eng = None
        for d in model_dirs():
            e = Sam2OnnxEngine(variant, d)
            if e.available():
                eng = e
                break
        if eng is None:
            print(f"[skip] model {variant} not installed")
            continue
        base = vram_used_mb()
        sampler = VramSampler(); sampler.start()
        t0 = time.time(); eng.load(hw.selected); load_s = time.time() - t0
        rows = {}
        cpu0, wall0 = time.process_time(), time.time()
        for name in a.scenarios.split(","):
            s, *_ = run_scenario(name, eng, a.tier, "click")
            rows[name] = s
        cpu = (time.process_time() - cpu0) / max(time.time() - wall0, 1e-6)
        sampler.stop.set(); sampler.join()
        results[variant] = {"load_s": round(load_s, 2), "vram_delta_mb": (sampler.peak - base) if base >= 0 else None,
                            "cpu_cores": round(cpu, 2), "scenarios": rows,
                            "mean_iou": round(sum(r["iou_mean"] for r in rows.values()) / len(rows), 3),
                            "mean_fps": round(sum(r["fps"] for r in rows.values()) / len(rows), 2)}
        r = results[variant]
        print(f"\n== sam2_hiera_{variant}  load {r['load_s']}s  VRAM +{r['vram_delta_mb']} MB  CPU {r['cpu_cores']} cores  mean IoU {r['mean_iou']}  mean {r['mean_fps']} fps")
        print(f"{'scenario':<11}{'IoU':>7}{'minIoU':>8}{'F':>7}{'flicker':>9}{'fps':>7}")
        for n, s in rows.items():
            print(f"{n:<11}{s['iou_mean']:7.3f}{s['iou_min']:8.3f}{s['f_mean']:7.3f}{s['flicker']:9.4f}{s['fps']:7.2f}")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        json.dump({"hardware": hw.summary(), "tier": a.tier, "results": results}, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
