"""AI Cutout Companion — the window for everything OFX cannot show (progress, timeline, frame-accurate picking).

Works in every Resolve edition (it is a separate program that talks to the same local service as the plugin).
Workflow: Open clip -> click the subject (left = include, right = exclude) -> Analyze -> Confirm -> Track ->
inspect in Overlay/Cutout -> paint corrections -> Propagate -> Render.
"""
from __future__ import annotations

import base64
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

from core.compositing import native
from core.compositing.native import EdgeSettings, bgr8_to_rgba
from inference.client import Client, ServiceError

PREVIEWS = ["Original", "Mask", "Alpha", "Transparent Checkerboard", "Overlay", "Cutout"]
PREVIEW_MODE = {"Original": "original", "Mask": "mask", "Alpha": "alpha", "Transparent Checkerboard": "checkerboard",
                "Overlay": "overlay", "Cutout": "cutout"}


def fmt_eta(s: Optional[float]) -> str:
    if s is None or s < 0 or s > 86400:
        return "-"
    s = int(s + .5)
    return f"{s // 3600}:{(s // 60) % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI Cutout Companion")
        self.geometry("1280x860")
        self.minsize(980, 700)
        self.client: Optional[Client] = None
        self.q: "queue.Queue[Callable]" = queue.Queue()
        self.frames = 0
        self.frame_idx = 0
        self.video = ""
        self.points: Dict[int, List[List[float]]] = {}        # frame -> [[u, v, label], ...] normalized
        self.timeline: Dict = {}
        self.cur_bgr: Optional[np.ndarray] = None
        self.cur_alpha: Optional[np.ndarray] = None
        self.preview_alpha: Optional[np.ndarray] = None        # unconfirmed segmentation result
        self.disp_scale = 1.0
        self.disp_off = (0, 0)
        self.stroke: List[List[float]] = []
        self.busy = False
        self._photo = None
        self._build()
        self.after(60, self._pump)
        self.after(400, self._poll_status)
        self.after(200, lambda: self._async(self._connect, "Starting the AI service..."))

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = ttk.Frame(self, padding=6)
        root.pack(fill="both", expand=True)
        left = ttk.Frame(root); left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(root, width=330); right.pack(side="right", fill="y", padx=(8, 0)); right.pack_propagate(False)

        self.canvas = tk.Canvas(left, bg="#1b1b1f", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", lambda e: self._click(e, 1))
        self.canvas.bind("<Button-3>", lambda e: self._click(e, 0))
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.timeline_c = tk.Canvas(left, height=64, bg="#26262b", highlightthickness=0)
        self.timeline_c.pack(fill="x", pady=(6, 0))
        self.timeline_c.bind("<Button-1>", self._timeline_click)
        self.timeline_c.bind("<B1-Motion>", self._timeline_click)
        bar = ttk.Frame(left); bar.pack(fill="x", pady=4)
        self.frame_lbl = ttk.Label(bar, text="Frame - / -", width=16); self.frame_lbl.pack(side="left")
        self.slider = ttk.Scale(bar, from_=0, to=1, orient="horizontal", command=self._slider); self.slider.pack(side="left", fill="x", expand=True)
        self.bind("<Left>", lambda e: self._step(-1)); self.bind("<Right>", lambda e: self._step(1))
        self.bind("<Shift-Left>", lambda e: self._step(-10)); self.bind("<Shift-Right>", lambda e: self._step(10))

        def group(title):
            f = ttk.LabelFrame(right, text=title, padding=6); f.pack(fill="x", pady=3); return f

        g = group("Clip")
        ttk.Button(g, text="Open video...", command=self.open_clip).pack(fill="x")
        row = ttk.Frame(g); row.pack(fill="x", pady=2)
        self.mode = tk.StringVar(value="Person"); self.tier = tk.StringVar(value="Balanced")
        ttk.Combobox(row, textvariable=self.mode, values=["Person", "Object", "Face", "Custom"], width=9, state="readonly").pack(side="left")
        ttk.Combobox(row, textvariable=self.tier, values=["Draft", "Balanced", "High Quality"], width=12, state="readonly").pack(side="left", padx=4)

        g = group("Selection   (left click = include, right = exclude)")
        b = ttk.Frame(g); b.pack(fill="x")
        ttk.Button(b, text="Analyze", command=self.analyze).pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="Confirm", command=self.confirm).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(b, text="Clear points", command=self.clear_points).pack(side="left", expand=True, fill="x")

        g = group("Manual correction")
        self.paint = tk.StringVar(value="Off")
        for t in ("Off", "Add Mask", "Remove Mask"):
            ttk.Radiobutton(g, text=t, value=t, variable=self.paint).pack(side="left")
        self.brush = tk.DoubleVar(value=18)
        ttk.Scale(g, from_=2, to=80, variable=self.brush, orient="horizontal", length=90).pack(side="left", padx=4)
        b = ttk.Frame(g); b.pack(fill="x", pady=(28, 0))
        ttk.Button(b, text="Propagate Correction", command=lambda: self.track("both", recalc=True)).pack(fill="x")
        self.refine_click = tk.BooleanVar(value=False)
        ttk.Checkbutton(g, text="Clicks refine the current mask (AI)", variable=self.refine_click).place(x=0, y=28)

        g = group("Tracking")
        b = ttk.Frame(g); b.pack(fill="x")
        for label, d in (("Forward", "forward"), ("Backward", "backward"), ("Both", "both")):
            ttk.Button(b, text=f"Track {label}", command=lambda d=d: self.track(d)).pack(side="left", expand=True, fill="x")
        b = ttk.Frame(g); b.pack(fill="x", pady=2)
        ttk.Button(b, text="Recalculate", command=lambda: self.track("both", recalc=True)).pack(side="left", expand=True, fill="x")
        ttk.Button(b, text="Cancel", command=self.cancel).pack(side="left", expand=True, fill="x")

        g = group("Preview")
        self.preview = tk.StringVar(value="Overlay")
        cb = ttk.Combobox(g, textvariable=self.preview, values=PREVIEWS, state="readonly"); cb.pack(fill="x")
        cb.bind("<<ComboboxSelected>>", lambda e: self._redraw())
        self.sliders: Dict[str, tk.DoubleVar] = {}
        for name, lo, hi, dv in (("Feather", 0, 30, 0), ("Smooth", 0, 20, 0), ("Edge Shift", -30, 30, 0),
                                 ("Edge Refinement", 0, 1, .5), ("Spill Suppression", 0, 1, 0), ("Decontaminate Edge", 0, 1, 0)):
            r = ttk.Frame(g); r.pack(fill="x")
            ttk.Label(r, text=name, width=18).pack(side="left")
            v = tk.DoubleVar(value=dv); self.sliders[name] = v
            s = ttk.Scale(r, from_=lo, to=hi, variable=v, orient="horizontal", command=lambda _=None: self._redraw()); s.pack(side="left", fill="x", expand=True)

        g = group("Output")
        b = ttk.Frame(g); b.pack(fill="x")
        ttk.Button(b, text="Render (PNG alpha + cutout)", command=self.render_out).pack(fill="x")
        ttk.Button(b, text="Reset", command=self.reset).pack(fill="x", pady=2)

        g = group("Status")
        self.st_state = tk.StringVar(value="Starting..."); self.st_frame = tk.StringVar(value="-")
        self.st_track = tk.StringVar(value="-"); self.st_conf = tk.StringVar(value="-"); self.st_eta = tk.StringVar(value="-")
        for label, var in (("State", self.st_state), ("Current frame", self.st_frame), ("Tracking status", self.st_track),
                           ("AI confidence", self.st_conf), ("Time remaining", self.st_eta)):
            r = ttk.Frame(g); r.pack(fill="x")
            ttk.Label(r, text=label, width=16).pack(side="left"); ttk.Label(r, textvariable=var).pack(side="left")
        self.progress = ttk.Progressbar(g, maximum=100); self.progress.pack(fill="x", pady=4)
        self.msg = tk.Text(g, height=7, wrap="word", bg="#f4f4f4", relief="flat"); self.msg.pack(fill="x")
        self.msg.configure(state="disabled")

    # ------------------------------------------------------------------ threading helpers
    def _async(self, fn: Callable, busy_msg: str = "", done: Optional[Callable] = None):
        if busy_msg:
            self._set_msg(busy_msg)

        def run():
            try:
                res = fn()
                self.q.put(lambda: done(res) if done else None)
            except Exception as e:  # noqa: BLE001 - presented to the user
                self.q.put(lambda e=e: self._error(str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _pump(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(60, self._pump)

    def _error(self, text: str):
        self._set_msg(text)
        messagebox.showerror("AI Cutout", text)

    def _set_msg(self, text: str):
        self.msg.configure(state="normal"); self.msg.delete("1.0", "end"); self.msg.insert("1.0", text); self.msg.configure(state="disabled")

    # ------------------------------------------------------------------ service
    def _connect(self):
        self.client = Client.ensure_service()
        h = self.client.call("hello")
        self.q.put(lambda: (self._set_msg(f"AI Cutout\n\n{h['hardware']}\n\nReady."), self.st_state.set("Ready")))

    def _call(self, cmd, **kw):
        if self.client is None:
            raise ServiceError("The AI service is not connected yet.")
        return self.client.call(cmd, **kw)

    def open_clip(self):
        path = filedialog.askopenfilename(title="Open video", filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.mxf *.webm *.m4v"), ("All files", "*.*")])
        if not path:
            return
        self.video = path
        mode = self.mode.get().lower(); tier = {"Draft": "draft", "Balanced": "balanced", "High Quality": "high"}[self.tier.get()]

        def work():
            self._call("open", video=path, mode=mode, tier=tier)
            st = self.client.wait_idle(cb=lambda s: self.q.put(lambda s=s: self._show_status(s)))
            return st

        def done(st):
            self.frames = st["frames"]; self.frame_idx = 0; self.points.clear(); self.preview_alpha = None
            self.slider.configure(to=max(1, self.frames - 1)); self._set_msg("\n".join([st.get("message", "")] + st.get("notes", [])))
            self._goto(0); self._refresh_timeline()
        self._async(work, "Opening clip (decoding proxy frames)...", done)

    # ------------------------------------------------------------------ frames
    def _goto(self, idx: int):
        if not self.frames:
            return
        idx = max(0, min(self.frames - 1, idx))
        self.frame_idx = idx
        self.frame_lbl.config(text=f"Frame {idx} / {self.frames - 1}")
        self.slider.set(idx)

        def work():
            fr = self._call("get_frame", idx=idx)
            m = self._call("get_mask", idx=idx)
            return Client.decode_jpg(fr["jpg"]), Client.decode_png(m.get("png")), m

        def done(res):
            if idx != self.frame_idx:
                return
            self.cur_bgr, self.cur_alpha, meta = res
            self.preview_alpha = None if self.preview_alpha is None or getattr(self, "_preview_frame", -1) != idx else self.preview_alpha
            self._redraw()
        self._async(work, "", done)

    def _slider(self, v):
        i = int(float(v))
        if i != self.frame_idx and self.frames:
            self._goto(i)

    def _step(self, d):
        self._goto(self.frame_idx + d)

    # ------------------------------------------------------------------ drawing
    def _compose(self) -> Optional[np.ndarray]:
        if self.cur_bgr is None:
            return None
        alpha = self.preview_alpha if self.preview_alpha is not None else self.cur_alpha
        mode = PREVIEW_MODE[self.preview.get()]
        if alpha is None:
            return self.cur_bgr
        v = {k: s.get() for k, s in self.sliders.items()}
        es = EdgeSettings(edge_shift=v["Edge Shift"], feather=v["Feather"], smooth=v["Smooth"], refine=v["Edge Refinement"],
                          decontaminate=v["Decontaminate Edge"], spill=v["Spill Suppression"],
                          quality={"Draft": 0, "Balanced": 1, "High Quality": 2}[self.tier.get()], mode=mode)
        try:
            out = native.render(bgr8_to_rgba(self.cur_bgr), alpha, es)
        except Exception:  # native core missing: simple overlay fallback
            img = self.cur_bgr.copy(); img[alpha > 127] = (0.5 * img[alpha > 127] + 0.5 * np.array([0, 0, 255])).astype(np.uint8); return img
        rgb = np.clip(out[..., :3], 0, 1)
        if mode == "cutout":            # show the transparent result over a checkerboard
            h, w = rgb.shape[:2]
            yy, xx = np.mgrid[0:h, 0:w]; chk = (((xx // 16) + (yy // 16)) & 1) * 0.24 + 0.38
            rgb = rgb * out[..., 3:4] + chk[..., None] * (1 - out[..., 3:4])
        return (rgb[..., ::-1] * 255).astype(np.uint8)

    def _redraw(self):
        img = self._compose()
        c = self.canvas
        c.delete("all")
        if img is None:
            c.create_text(c.winfo_width() // 2, c.winfo_height() // 2, fill="#9a9aa2", font=("Segoe UI", 13),
                          text="Open a video, then click the subject you want to cut out.")
            return
        cw, ch = max(c.winfo_width(), 10), max(c.winfo_height(), 10)
        h, w = img.shape[:2]
        s = min(cw / w, ch / h)
        nw, nh = int(w * s), int(h * s)
        self.disp_scale, self.disp_off = s, ((cw - nw) // 2, (ch - nh) // 2)
        disp = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
        ok, buf = cv2.imencode(".png", disp)
        self._photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode())
        c.create_image(self.disp_off[0], self.disp_off[1], anchor="nw", image=self._photo)
        ox, oy = self.disp_off
        for u, v, lab in self.points.get(self.frame_idx, []):
            x, y = ox + u * nw, oy + v * nh
            col = "#28e04a" if lab else "#ff3b3b"
            c.create_oval(x - 7, y - 7, x + 7, y + 7, outline=col, width=2)
            c.create_line(x - 4, y, x + 4, y, fill=col, width=2)
            if lab:
                c.create_line(x, y - 4, x, y + 4, fill=col, width=2)
        if len(self.stroke) > 1:
            pts = [(ox + u * nw, oy + v * nh) for u, v in self.stroke]
            c.create_line(*[q for p in pts for q in p], fill="#33d6ff" if self.paint.get() == "Add Mask" else "#ff9a1f", width=3)

    def _norm(self, e):
        ox, oy = self.disp_off
        if self.cur_bgr is None:
            return None
        h, w = self.cur_bgr.shape[:2]
        u, v = (e.x - ox) / (w * self.disp_scale), (e.y - oy) / (h * self.disp_scale)
        return (u, v) if 0 <= u <= 1 and 0 <= v <= 1 else None

    # ------------------------------------------------------------------ interaction
    def _click(self, e, label):
        p = self._norm(e)
        if p is None or not self.frames:
            return
        if self.paint.get() != "Off" and label == 1:
            self.stroke = [list(p)]
            return
        idx = self.frame_idx
        self.points.setdefault(idx, []).append([p[0], p[1], label])
        if self.refine_click.get() and self.cur_alpha is not None:
            pts = self.points[idx]
            self._async(lambda: self._call("refine", idx=idx, points=pts, normalized=True), "Refining...",
                        lambda r: (self._goto(idx), self._refresh_timeline()))
        else:
            self._segment()
        self._redraw()

    def _drag(self, e):
        if self.stroke:
            p = self._norm(e)
            if p:
                self.stroke.append(list(p)); self._redraw()

    def _release(self, e):
        if not self.stroke:
            return
        add = self.paint.get() == "Add Mask"
        stroke, self.stroke = self.stroke, []
        idx = self.frame_idx
        w = self.cur_bgr.shape[1]
        b = self.brush.get() / (w * self.disp_scale)
        self._async(lambda: self._call("paint", frame=idx, add=[stroke] if add else [], remove=[] if add else [stroke], brush_norm=b),
                    "Applying correction...", lambda r: (self._goto(idx), self._refresh_timeline()))

    def _segment(self, commit: bool = False):
        idx = self.frame_idx
        pts = list(self.points.get(idx, []))

        def work():
            return self._call("segment", idx=idx, points=pts, normalized=True)

        def done(r):
            self.preview_alpha = Client.decode_png(r["png"]); self._preview_frame = idx
            if self.preview_alpha is not None and self.preview_alpha.dtype != np.uint8:
                self.preview_alpha = self.preview_alpha.astype(np.uint8)
            self.st_conf.set(f"{r['confidence']:.0%}")
            self._set_msg(f"Frame {idx}: selection ready (confidence {r['confidence']:.0%}). Add more points to refine, or press Confirm, then Track.")
            self._redraw()
        self._async(work, "Segmenting...", done)

    def analyze(self):
        if self.frames:
            self._segment()

    def confirm(self):
        idx = self.frame_idx
        pts = list(self.points.get(idx, []))
        self._async(lambda: self._call("commit", idx=idx, points=pts, normalized=True), "Saving keyframe...",
                    lambda r: (setattr(self, "preview_alpha", None), self._goto(idx), self._refresh_timeline(),
                               self._set_msg(f"Frame {idx} is now a keyframe. Press Track to follow the subject.")))

    def clear_points(self):
        self.points.pop(self.frame_idx, None); self.preview_alpha = None; self._redraw()

    def track(self, direction: str, recalc: bool = False):
        if not self.frames:
            return
        for idx, pts in list(self.points.items()):     # confirm any unconfirmed selections first
            pass
        idx = self.frame_idx
        pend = {i: list(p) for i, p in self.points.items() if p}

        def work():
            for i, p in pend.items():
                self._call("commit", idx=i, points=p, normalized=True)
            self.points.clear()
            self._call("track", direction=direction, recalc=recalc)
            return self.client.wait_idle(timeout=6 * 3600, cb=lambda s: self.q.put(lambda s=s: self._show_status(s)))
        self._async(work, "Tracking...", lambda st: (self._goto(self.frame_idx), self._refresh_timeline(), self._show_status(st)))

    def cancel(self):
        if self.client:
            self._async(lambda: self._call("cancel"))

    def reset(self):
        if not self.frames or not messagebox.askyesno("Reset", "Remove all selections, keyframes and cached mattes for this clip?"):
            return
        self.points.clear(); self.preview_alpha = None
        self._async(lambda: self._call("reset"), "", lambda r: (self._goto(self.frame_idx), self._refresh_timeline()))

    def render_out(self):
        if not self.frames:
            return
        d = filedialog.askdirectory(title="Render folder (alpha/ and cutout/ PNG sequences)")
        if not d:
            return
        v = {k: s.get() for k, s in self.sliders.items()}
        edge = {"edge_shift": v["Edge Shift"], "feather": v["Feather"], "smooth": v["Smooth"], "refine": v["Edge Refinement"],
                "decontaminate": v["Decontaminate Edge"], "spill": v["Spill Suppression"]}

        def work():
            self._call("export", dir=d, edge=edge)
            return self.client.wait_idle(timeout=6 * 3600, cb=lambda s: self.q.put(lambda s=s: self._show_status(s)))
        self._async(work, "Rendering full-resolution alpha and cutout...", lambda st: self._set_msg(st.get("message", "")))

    # ------------------------------------------------------------------ status / timeline
    def _show_status(self, st: Dict):
        state = st.get("state", "idle")
        self.st_state.set({"idle": "Ready", "tracking": "Tracking", "done": "Done", "preparing": "Opening", "cancelled": "Cancelled",
                           "error": "Error", "rendering": "Rendering"}.get(state, state))
        if state in ("tracking", "rendering", "preparing"):
            tot = st.get("frames_total") or 1
            self.progress["value"] = 100.0 * st.get("frames_done", 0) / tot
            self.st_frame.set(f"{st.get('current_frame', 0)} / {tot}")
            self.st_eta.set(fmt_eta(st.get("eta_s")))
        elif state == "done":
            self.progress["value"] = 100; self.st_eta.set("0:00")
        if state == "tracking":
            self.st_track.set("in progress"); self.st_conf.set(f"{st.get('confidence', 1):.0%}")
        elif state in ("done", "cancelled", "error"):
            self.st_track.set(state)
        if st.get("message"):
            self._set_msg(st["message"])

    def _poll_status(self):
        if self.client and self.frames and not self.busy:
            def work():
                return self._call("status")

            def done(st):
                if st.get("busy"):
                    self._show_status(st)
                    self._refresh_timeline()
            self._async(work, "", done)
        self.after(900, self._poll_status)

    def _refresh_timeline(self):
        if not self.client or not self.frames:
            return
        self._async(lambda: self._call("timeline"), "", self._draw_timeline)

    def _draw_timeline(self, t):
        self.timeline = t
        c = self.timeline_c
        c.delete("all")
        w, h = max(c.winfo_width(), 10), c.winfo_height()
        n = max(1, t["frames"])
        px = lambda i: 4 + (w - 8) * i / max(1, n - 1)
        cached = set(t["cached"]); low = set(t["low_confidence"])
        step = max(1.0, (w - 8) / n)
        for i in range(n):
            x = px(i)
            col = "#3a3a42"
            if i in cached:
                col = "#d34a4a" if i in low else "#3f9d5a"
            c.create_rectangle(x, 30, x + step, 44, fill=col, outline="")
        conf = t["confidence"]
        pts = [(px(i), 28 - 22 * max(0, v)) for i, v in enumerate(conf) if v >= 0]
        if len(pts) > 1:
            c.create_line(*[q for p in pts for q in p], fill="#9ad0ff", width=1)
        for k in t["keyframes"]:
            x = px(k["idx"]); col = "#ffb02e" if k["kind"] == "manual" else "#4ea8ff"
            c.create_polygon(x, 47, x - 5, 58, x + 5, 58, fill=col, outline="")
        x = px(self.frame_idx)
        c.create_line(x, 2, x, h - 2, fill="#ffffff")
        c.create_text(6, 4, anchor="nw", fill="#9a9aa2", font=("Segoe UI", 8),
                      text="green = tracked   red = low confidence   blue = AI keyframe   orange = manual keyframe   line = confidence")

    def _timeline_click(self, e):
        if self.frames:
            w = max(self.timeline_c.winfo_width() - 8, 1)
            self._goto(int(round((e.x - 4) / w * (self.frames - 1))))
            if self.timeline:
                self._draw_timeline(self.timeline)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
