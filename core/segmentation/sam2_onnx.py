"""SAM 2 (Apache-2.0) via ONNX Runtime — image encoder + prompt decoder.

Weights: sam2_hiera_{tiny,small,base_plus,large}.{encoder,decoder}.onnx (community ONNX export of Meta's
SAM 2, Apache-2.0; see THIRD_PARTY_LICENSES.md). Preprocessing follows Meta's SAM2Transforms:
stretch-resize to 1024x1024, ImageNet mean/std, RGB.
"""
from __future__ import annotations

import os
import threading
from typing import List, Optional

import cv2
import numpy as np

from .base import EngineError, MaskProposal, ModelInfo, Prompts, SegmentationEngine

_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32) * 255.0
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32) * 255.0
_SIZE = 1024

_VRAM = {"tiny": 900, "small": 1100, "base_plus": 1600, "large": 3200}   # planning estimates, unbenchmarked


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


class Sam2OnnxEngine(SegmentationEngine):
    def __init__(self, variant: str, model_dir: str):
        self.variant = variant
        self.model_dir = model_dir
        self._enc = None
        self._dec = None
        self._lock = threading.Lock()
        self._feats = None
        self._img_size = (0, 0)   # (w, h) of image given to set_image
        self.providers_used: List[str] = []

    def info(self) -> ModelInfo:
        return ModelInfo(f"sam2_hiera_{self.variant}", "onnx-1", "Apache-2.0",
                         _VRAM.get(self.variant, 2000), True, True, True)

    @property
    def paths(self):
        return (os.path.join(self.model_dir, f"sam2_hiera_{self.variant}.encoder.onnx"),
                os.path.join(self.model_dir, f"sam2_hiera_{self.variant}.decoder.onnx"))

    def available(self) -> bool:
        return all(os.path.isfile(p) for p in self.paths)

    def load(self, providers: Optional[List[str]] = None) -> None:
        import onnxruntime as ort
        enc_p, dec_p = self.paths
        for p in (enc_p, dec_p):
            if not os.path.isfile(p):
                raise EngineError(f"Model file missing: {p}\nRun the installer's model step or place the file there.")
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = providers or ["CPUExecutionProvider"]
        last_err = None
        for prov_list in (providers, ["CPUExecutionProvider"]):
            try:
                # DirectML requires sequential execution and no memory-pattern optimisation
                if "DmlExecutionProvider" in prov_list:
                    so.enable_mem_pattern = False
                    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                self._enc = ort.InferenceSession(enc_p, so, providers=prov_list)
                self._dec = ort.InferenceSession(dec_p, so, providers=prov_list)
                self.providers_used = self._enc.get_providers()
                return
            except Exception as e:  # noqa: BLE001 - fall back to CPU, report if that also fails
                last_err = e
        raise EngineError(f"Model failed to load: {last_err}")

    def unload(self) -> None:
        self._enc = self._dec = None
        self._feats = None

    # ------------------------------------------------------------------ encode
    def set_image(self, image_bgr: np.ndarray) -> None:
        if self._enc is None:
            raise EngineError("Model not loaded.")
        h, w = image_bgr.shape[:2]
        rgb = cv2.cvtColor(cv2.resize(image_bgr, (_SIZE, _SIZE), interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2RGB)
        x = ((rgb.astype(np.float32) - _MEAN) / _STD).transpose(2, 0, 1)[None]
        try:
            with self._lock:
                f = self._enc.run(None, {"image": np.ascontiguousarray(x)})
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "memory" in msg.lower() or "E_OUTOFMEMORY" in msg:
                raise EngineError("GPU memory is insufficient for this mode.\n\nTry Balanced mode or Draft mode.") from e
            raise EngineError(f"Inference failure: {msg}") from e
        # order: high_res_feats_0, high_res_feats_1, image_embed
        self._feats = {"high_res_feats_0": f[0], "high_res_feats_1": f[1], "image_embed": f[2]}
        self._img_size = (w, h)

    # ------------------------------------------------------------------ decode
    def _decode(self, coords: np.ndarray, labels: np.ndarray, mask_in: Optional[np.ndarray]):
        has = np.array([1.0 if mask_in is not None else 0.0], dtype=np.float32)
        mi = mask_in if mask_in is not None else np.zeros((1, 1, 256, 256), np.float32)
        with self._lock:
            masks, ious = self._dec.run(None, {
                **self._feats,
                "point_coords": coords, "point_labels": labels,
                "mask_input": mi, "has_mask_input": has,
            })
        return masks[0], ious[0]      # (N,256,256) logits, (N,)

    def predict(self, prompts: Prompts) -> MaskProposal:
        if self._feats is None:
            raise EngineError("No image set.")
        w, h = self._img_size
        sx, sy = _SIZE / w, _SIZE / h
        pts, lab = [], []
        for x, y, l in prompts.points:
            pts.append([x * sx, y * sy]); lab.append(1.0 if l else 0.0)
        if prompts.box is not None:
            x0, y0, x1, y1 = prompts.box
            pts += [[x0 * sx, y0 * sy], [x1 * sx, y1 * sy]]
            lab += [2.0, 3.0]
        if not pts:
            # mask-only prompt: a padding point keeps the decoder graph valid
            pts, lab = [[0.0, 0.0]], [-1.0]
        coords = np.asarray(pts, np.float32)[None]
        labels = np.asarray(lab, np.float32)[None]

        mask_in = None
        if prompts.mask is not None:
            m = cv2.resize(prompts.mask.astype(np.float32), (256, 256), interpolation=cv2.INTER_AREA)
            mask_in = ((m * 2.0 - 1.0) * 6.0)[None, None].astype(np.float32)

        logits, ious = self._decode(coords, labels, mask_in)
        # choose candidate: highest predicted IoU, or best agreement with a guide mask
        if prompts.mask is not None:
            guide = cv2.resize(prompts.mask.astype(np.float32), (256, 256), interpolation=cv2.INTER_AREA) > 0.5
            scores = []
            for i in range(logits.shape[0]):
                cand = logits[i] > 0
                inter = np.logical_and(cand, guide).sum()
                union = np.logical_or(cand, guide).sum() + 1e-6
                scores.append(0.5 * ious[i] + 0.5 * inter / union)
            best = int(np.argmax(scores))
        else:
            best = int(np.argmax(ious))
        prob = _sigmoid(cv2.resize(logits[best], (w, h), interpolation=cv2.INTER_LINEAR))
        return MaskProposal(prob.astype(np.float32), float(np.clip(ious[best], 0, 1)),
                            {"iou_all": ious.tolist(), "chosen": best})
