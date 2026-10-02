"""AI Cutout app — the whole workflow in one window.

    1. Open a clip      2. Click the subject (left = include, right = exclude)
    3. Track            4. Render   (then press "Update Matte" in the Resolve plugin)

To fix a bad frame: scrub to it and click on it (left = add, right = remove), then Track again.
"""
from __future__ import annotations

import base64
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

from core.compositing import native
from core.compositing.native import EdgeSettings, bgr8_to_rgba
from inference.client import Client, ServiceError

PREVIEWS = {"Overlay": "overlay", "Cutout": "checkerboard", "Original": "original"}
CLEAN = dict(decontaminate=0.6, spill=0.3)        # same fixed defaults as the plugin's "Clean Edge Colors"


def fmt_eta(s: Optional[float]) -> str:
    if s is None or s < 0 or s > 86400:
        return "-"
    s = int(s + .5)
    return f"{s // 3600}:{(s // 60) % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI Cutout")
        self.geometry("1180x800")
        self.minsize(900, 640)
        self.client: Optional[Client] = None
        self.q: "queue.Queue[Callable]" = queue.Queue()
        self.frames = 0
        self.frame_idx = 0
        self.video = ""
        self.points: Dict[int, List[List[float]]] = {}          # frame -> [[u, v, label]] normalised, not yet saved
        self.timeline: Dict = {}
        self.cur_bgr: Optional[np.ndarray] = None
        self.cur_alpha: Optional[np.ndarray] = None             # saved matte for this frame
        self.preview_alpha: Optional[np.ndarray] = None         # unsaved selection preview
        self._preview_frame = -1
        self.disp_scale, self.disp_off = 1.0, (0, 0)
        self.out_dir = ""
        self._photo = None
        self._build()
        self.after(60, self._pump)
        self.after(900, self._poll_status)
        self.after(200, lambda: self._async(self._connect, "Starting..."))

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        left = ttk.Frame(root); left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(root, width=270); right.pack(side="right", fill="y", padx=(10, 0)); right.pack_propagate(False)

        self.canvas = tk.Canvas(left, bg="#1b1b1f", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", lambda e: self._click(e, 1))
        self.canvas.bind("<Button-3>", lambda e: self._click(e, 0))
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.timeline_c = tk.Canvas(left, height=34, bg="#26262b", highlightthickness=0)
        self.timeline_c.pack(fill="x", pady=(6, 0))
        self.timeline_c.bind("<Button-1>", self._timeline_click)
        self.timeline_c.bind("<B1-Motion>", self._timeline_click)
        bar = ttk.Frame(left); bar.pack(fill="x", pady=4)
        self.frame_lbl = ttk.Label(bar, text="", width=14); self.frame_lbl.pack(side="left")
        self.slider = ttk.Scale(bar, from_=0, to=1, orient="horizontal", command=self._slider); self.slider.pack(side="left", fill="x", expand=True)
        for k, d in (("<Left>", -1), ("<Right>", 1), ("<Shift-Left>", -10), ("<Shift-Right>", 10)):
            self.bind(k, lambda e, d=d: self._goto(self.frame_idx + d))

        def step(n, title, hint=""):
            f = ttk.Frame(right); f.pack(fill="x", pady=(0, 10))
            ttk.Label(f, text=f"{n}  {title}", font=("Segoe UI", 11, "bold")).pack(anchor="w")
            if hint:
                ttk.Label(f, text=hint, wraplength=250, foreground="#555").pack(anchor="w")
            return f

        f = step("1", "Open a clip")
        ttk.Button(f, text="Open clip...", command=self.open_clip).pack(fill="x", pady=2)
        f = step("2", "Click the subject", "Left click = include.  Right click = exclude.")
        ttk.Button(f, text="Clear points", command=self.clear_points).pack(fill="x", pady=2)
        f = step("3", "Track", "Follows the subject through the whole clip.")
        self.track_btn = ttk.Button(f, text="Track", command=self.track); self.track_btn.pack(fill="x", pady=2)
        f = step("4", "Render", "Writes the cutout next to your clip.")
        self.render_btn = ttk.Button(f, text="Render", command=self.render_out); self.render_btn.pack(fill="x", pady=2)
        self.folder_btn = ttk.Button(f, text="Show folder", command=self._show_folder)

        f = ttk.LabelFrame(right, text="View", padding=6); f.pack(fill="x", pady=(0, 8))
        self.preview = tk.StringVar(value="Overlay")
        r = ttk.Frame(f); r.pack(fill="x")
        for name in PREVIEWS:
            ttk.Radiobutton(r, text=name, value=name, variable=self.preview, command=self._redraw).pack(side="left", padx=2)
        self.feather = tk.DoubleVar(value=0); self.shift = tk.DoubleVar(value=0)
        for label, var, lo, hi in (("Feather", self.feather, 0, 30), ("Edge shift", self.shift, -30, 30)):
            row = ttk.Frame(f); row.pack(fill="x")
            ttk.Label(row, text=label, width=10).pack(side="left")
            ttk.Scale(row, from_=lo, to=hi, variable=var, orient="horizontal", command=lambda _=None: self._redraw()).pack(side="left", fill="x", expand=True)

        self.progress = ttk.Progressbar(right, maximum=100); self.progress.pack(fill="x", pady=(0, 4))
        self.msg = tk.Text(right, height=9, wrap="word", bg="#f4f4f4", relief="flat"); self.msg.pack(fill="both", expand=True)
        self.msg.configure(state="disabled")

    # ------------------------------------------------------------------ threading helpers
    def _async(self, fn: Callable, busy_msg: str = "", done: Optional[Callable] = None):
        if busy_msg:
            self._set_msg(busy_msg)

        def run():
            try:
                res = fn()
                self.q.put(lambda: done(res) if done else None)
            except Exception as e:  # noqa: BLE001 - shown to the user
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
        self.q.put(lambda: self._set_msg(f"{h['hardware']}\n\nReady. Press 'Open clip...'."))

    def _call(self, cmd, **kw):
        if self.client is None:
            raise ServiceError("The AI service is not ready yet. Wait a moment and try again.")
        return self.client.call(cmd, **kw)

    def open_clip(self):
        path = filedialog.askopenfilename(title="Open clip", filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.mxf *.webm *.m4v"), ("All files", "*.*")])
        if path:
            self.load_clip(path)

    def load_clip(self, path: str):
        self.video = path
        self.out_dir = os.path.splitext(path)[0] + "_cutout"

        def work():
            self._call("open", video=path, mode="custom", tier="balanced")
            return self.client.wait_idle(cb=lambda s: self.q.put(lambda s=s: self._show_status(s)))

        def done(st):
            self.frames = st["frames"]; self.frame_idx = 0; self.points.clear(); self.preview_alpha = None
            self.slider.configure(to=max(1, self.frames - 1))
            self._set_msg("Click the subject you want to cut out.")
            self.folder_btn.pack_forget()
            self._goto(0); self._refresh_timeline()
        self._async(work, "Opening the clip...", done)

    # ------------------------------------------------------------------ frames
    def _goto(self, idx: int):
        if not self.frames:
            return
        idx = max(0, min(self.frames - 1, idx))
        self.frame_idx = idx
        self.frame_lbl.config(text=f"Frame {idx + 1} / {self.frames}")
        self.slider.set(idx)

        def work():
            fr = self._call("get_frame", idx=idx)
            m = self._call("get_mask", idx=idx)
            return Client.decode_jpg(fr["jpg"]), Client.decode_png(m.get("png"))

        def done(res):
            if idx != self.frame_idx:
                return
            self.cur_bgr, self.cur_alpha = res
            if self._preview_frame != idx:
                self.preview_alpha = None
            self._redraw(); self._draw_timeline()
        self._async(work, "", done)

    def _slider(self, v):
        i = int(float(v))
        if self.frames and i != self.frame_idx:
            self._goto(i)

    # ------------------------------------------------------------------ drawing
    def _compose(self) -> Optional[np.ndarray]:
        if self.cur_bgr is None:
            return None
        alpha = self.preview_alpha if self.preview_alpha is not None else self.cur_alpha
        mode = PREVIEWS[self.preview.get()]
        if alpha is None or mode == "original":
            return self.cur_bgr
        es = EdgeSettings(edge_shift=self.shift.get(), feather=self.feather.get(), refine=0.5, quality=1, mode=mode, **CLEAN)
        try:
            out = native.render(bgr8_to_rgba(self.cur_bgr), alpha, es)
        except Exception:  # native core missing: plain overlay
            img = self.cur_bgr.copy(); img[alpha > 127] = (0.5 * img[alpha > 127] + 0.5 * np.array([0, 0, 255])).astype(np.uint8); return img
        return (np.clip(out[..., :3], 0, 1)[..., ::-1] * 255).astype(np.uint8)

    def _redraw(self):
        img = self._compose()
        c = self.canvas
        c.delete("all")
        if img is None:
            c.create_text(c.winfo_width() // 2, c.winfo_height() // 2, fill="#9a9aa2", font=("Segoe UI", 14), text="Open a clip to begin")
            return
        cw, ch = max(c.winfo_width(), 10), max(c.winfo_height(), 10)
        h, w = img.shape[:2]
        s = min(cw / w, ch / h)
        nw, nh = int(w * s), int(h * s)
        self.disp_scale, self.disp_off = s, ((cw - nw) // 2, (ch - nh) // 2)
        disp = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
        ok, buf = cv2.imencode(".png", disp)
        self._photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode())
        ox, oy = self.disp_off
        c.create_image(ox, oy, anchor="nw", image=self._photo)
        for u, v, lab in self.points.get(self.frame_idx, []):
            x, y = ox + u * nw, oy + v * nh
            col = "#28e04a" if lab else "#ff3b3b"
            c.create_oval(x - 7, y - 7, x + 7, y + 7, outline=col, width=2)
            c.create_line(x - 4, y, x + 4, y, fill=col, width=2)
            if lab:
                c.create_line(x, y - 4, x, y + 4, fill=col, width=2)

    def _norm(self, e):
        if self.cur_bgr is None:
            return None
        ox, oy = self.disp_off
        h, w = self.cur_bgr.shape[:2]
        u, v = (e.x - ox) / (w * self.disp_scale), (e.y - oy) / (h * self.disp_scale)
        return (u, v) if 0 <= u <= 1 and 0 <= v <= 1 else None

    # ------------------------------------------------------------------ the workflow
    def _click(self, e, label):
        p = self._norm(e)
        if p is None or not self.frames:
            return
        idx = self.frame_idx
        pts = self.points.setdefault(idx, [])
        pts.append([p[0], p[1], label])
        saved_mask_here = self.cur_alpha is not None and self.preview_alpha is None
        if saved_mask_here:
            # a tracked/saved frame: clicks FIX it (the matte is refined and kept), then Track updates the neighbours
            def work():
                return self._call("refine", idx=idx, points=list(pts), normalized=True)
            self._async(work, "Fixing this frame...", lambda r: (self.points.pop(idx, None), self._goto(idx), self._refresh_timeline(),
                        self._set_msg(f"Frame {idx + 1} fixed. Press Track to update the frames around it."), self._mark_dirty()))
        else:
            self._segment()
        self._redraw()

    def _segment(self):
        idx = self.frame_idx
        pts = list(self.points.get(idx, []))

        def work():
            return self._call("segment", idx=idx, points=pts, normalized=True)

        def done(r):
            self.preview_alpha = Client.decode_png(r["png"]); self._preview_frame = idx
            self._set_msg("Selected. Add more clicks to refine it, or press Track.")
            self._redraw()
        self._async(work, "", done)

    def clear_points(self):
        self.points.pop(self.frame_idx, None); self.preview_alpha = None; self._redraw()

    def _mark_dirty(self):
        self.track_btn.config(text="Track again")

    def track(self):
        if not self.frames:
            self._set_msg("Open a clip first.")
            return
        pend = {i: list(p) for i, p in self.points.items() if p and i == self._preview_frame}

        def work():
            for i, p in pend.items():
                self._call("commit", idx=i, points=p, normalized=True)
            self._call("track", direction="both")
            return self.client.wait_idle(timeout=6 * 3600, cb=lambda s: self.q.put(lambda s=s: self._show_status(s)))

        def done(st):
            self.points.clear(); self.preview_alpha = None
            self.track_btn.config(text="Track")
            low = len(st.get("low_confidence", []))
            self._set_msg("Done tracking." + (f"\n{low} frame(s) are marked red on the timeline: click on them to fix, then Track again." if low else "\nLooks good? Press Render."))
            self._goto(self.frame_idx); self._refresh_timeline()
        self._async(work, "Tracking...", done)

    def render_out(self):
        if not self.frames:
            self._set_msg("Open a clip first.")
            return
        edge = {"edge_shift": self.shift.get(), "feather": self.feather.get(), "refine": 0.5, **CLEAN}
        out = self.out_dir

        def work():
            self._call("export", dir=out, edge=edge)
            return self.client.wait_idle(timeout=6 * 3600, cb=lambda s: self.q.put(lambda s=s: self._show_status(s)))

        def done(st):
            self._set_msg(f"Done. Cutout written to:\n{out}\n\nIn Resolve, press 'Update Matte' on the AI Cutout effect - or import the 'alpha' / 'cutout' image sequences.")
            self.folder_btn.pack(fill="x", pady=2)
        self._async(work, "Rendering...", done)

    def _show_folder(self):
        if self.out_dir and os.path.isdir(self.out_dir):
            os.startfile(self.out_dir)  # noqa: S606 - local folder chosen by us

    # ------------------------------------------------------------------ status / timeline
    def _show_status(self, st: Dict):
        state = st.get("state", "idle")
        if state in ("tracking", "rendering", "preparing"):
            tot = st.get("frames_total") or 1
            self.progress["value"] = 100.0 * st.get("frames_done", 0) / tot
            word = {"tracking": "Tracking", "rendering": "Rendering", "preparing": "Reading clip"}[state]
            extra = f"  -  confidence {st.get('confidence', 1):.0%}" if state == "tracking" else ""
            self._set_msg(f"{word}: frame {st.get('frames_done', 0)} of {tot}{extra}\nTime left: {fmt_eta(st.get('eta_s'))}")
        elif state in ("done", "idle"):
            self.progress["value"] = 100 if state == "done" else 0

    def _poll_status(self):
        if self.client and self.frames:
            self._async(lambda: self._call("status"), "", lambda st: (self._refresh_timeline() if st.get("busy") else None))
        self.after(900, self._poll_status)

    def _refresh_timeline(self):
        if self.client and self.frames:
            self._async(lambda: self._call("timeline"), "", self._store_timeline)

    def _store_timeline(self, t):
        self.timeline = t
        self._draw_timeline()

    def _draw_timeline(self):
        t = self.timeline
        c = self.timeline_c
        c.delete("all")
        if not t:
            return
        w, n = max(c.winfo_width(), 10), max(1, t["frames"])
        px = lambda i: 4 + (w - 8) * i / max(1, n - 1)
        cached, low = set(t["cached"]), set(t["low_confidence"])
        step = max(1.0, (w - 8) / n)
        for i in range(n):
            col = "#3a3a42" if i not in cached else ("#d34a4a" if i in low else "#3f9d5a")
            c.create_rectangle(px(i), 8, px(i) + step, 22, fill=col, outline="")
        for k in t["keyframes"]:
            x = px(k["idx"]); c.create_polygon(x, 24, x - 4, 32, x + 4, 32, fill="#4ea8ff", outline="")
        x = px(self.frame_idx); c.create_line(x, 2, x, 34, fill="#ffffff")

    def _timeline_click(self, e):
        if self.frames:
            self._goto(int(round((e.x - 4) / max(self.timeline_c.winfo_width() - 8, 1) * (self.frames - 1))))


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
