"""Hardware detection and ONNX Runtime provider selection.

Order of preference: NVIDIA CUDA -> DirectML (any DX12 GPU: NVIDIA/AMD/Intel) -> CPU.
Nothing here imports torch; only onnxruntime (optional at import time so unit tests run without it).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class GpuInfo:
    vendor: str            # "nvidia" | "amd" | "intel" | "unknown"
    name: str
    vram_total_mb: Optional[int] = None
    vram_free_mb: Optional[int] = None
    driver: str = ""


@dataclass
class HardwareReport:
    gpus: List[GpuInfo] = field(default_factory=list)
    providers: List[str] = field(default_factory=list)      # ORT providers available
    selected: List[str] = field(default_factory=lambda: ["CPUExecutionProvider"])
    device_label: str = "CPU"

    def summary(self) -> str:
        lines = []
        for g in self.gpus:
            vram = f", VRAM: {g.vram_total_mb / 1024:.0f} GB" if g.vram_total_mb else ""
            lines.append(f"{g.vendor.upper()} GPU detected: {g.name}{vram}")
        if not self.gpus:
            lines.append("No GPU detected. Running on CPU (slow).")
        lines.append(f"Inference backend: {self.device_label}")
        return "\n".join(lines)


def _nvidia_smi() -> List[GpuInfo]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.free,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try:
                gpus.append(GpuInfo("nvidia", parts[0], int(float(parts[1])), int(float(parts[2])), parts[3]))
            except ValueError:
                pass
    return gpus


def _wmi_gpus() -> List[GpuInfo]:
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.AdapterRAM + '|' + $_.DriverVersion }"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        name, _, rest = line.partition("|")
        ram, _, drv = rest.partition("|")
        low = name.lower()
        vendor = ("nvidia" if "nvidia" in low else "amd" if ("amd" in low or "radeon" in low)
                  else "intel" if "intel" in low else "unknown")
        try:
            vram = int(ram) // (1024 * 1024) if ram.strip() else None
        except ValueError:
            vram = None
        gpus.append(GpuInfo(vendor, name.strip(), vram, None, drv.strip()))
    return gpus


def detect_hardware(prefer: Optional[str] = None) -> HardwareReport:
    """prefer: None/"auto", "cuda", "dml", "cpu" (also env AICUTOUT_DEVICE)."""
    prefer = (prefer or os.environ.get("AICUTOUT_DEVICE") or "auto").lower()
    rep = HardwareReport()
    nv = _nvidia_smi()
    rep.gpus = nv + [g for g in _wmi_gpus() if not (g.vendor == "nvidia" and nv)]
    try:
        import onnxruntime as ort
        rep.providers = ort.get_available_providers()
    except Exception:
        rep.providers = []

    def has(p: str) -> bool:
        return p in rep.providers

    order: List[str] = []
    if prefer in ("auto", "cuda") and has("CUDAExecutionProvider") and nv:
        order.append("CUDAExecutionProvider")
        rep.device_label = "NVIDIA CUDA"
    elif prefer in ("auto", "dml") and has("DmlExecutionProvider"):
        order.append("DmlExecutionProvider")
        rep.device_label = "DirectML (GPU)"
    if not order:
        rep.device_label = "CPU"
    order.append("CPUExecutionProvider")
    rep.selected = order
    return rep


# VRAM the *inference service* needs per tier. Measured on an RTX 4060 Laptop with DirectML (docs/BENCHMARKS.md):
# tiny +1.3 GB, small +1.3 GB, base_plus +2.5 GB (increase of global GPU memory.used while tracking), rounded up with margin.
TIER_VRAM_MB = {"draft": 1500, "balanced": 1500, "high": 2800}


def check_vram(report: HardwareReport, tier: str) -> Optional[str]:
    """Return a user-facing message when the chosen tier cannot fit, else None."""
    need = TIER_VRAM_MB.get(tier)
    if not need:
        return None
    for g in report.gpus:
        if g.vendor == "nvidia" and g.vram_free_mb is not None and g.vram_free_mb < need:
            label = {"draft": "Draft", "balanced": "Balanced", "high": "High Quality"}[tier]
            hint = "Try Balanced mode or Draft mode." if tier == "high" else "Try Draft mode."
            return f"GPU memory is insufficient for {label} mode.\n\n{hint}"
    return None
