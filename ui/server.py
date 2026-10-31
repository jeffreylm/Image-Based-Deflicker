#!/usr/bin/env python3
"""
Deflicker tuning UI.

    python3 ui/server.py "<clip.mov>" [--port 8765] [--outdir exports]

Other shots and the output folder can then be changed from the app itself.

Then open http://localhost:8765. Analysis parameters are recomputed here
(a couple of seconds); mixing parameters are shader uniforms in the browser,
using exactly the maths of deflicker.finalize().
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import deflicker as d  # noqa: E402

PRESETS = os.path.join(os.path.dirname(HERE), "presets")
VIDEO_EXT = (".mov", ".mp4", ".mxf", ".m4v")


def log(msg):
    print(msg, flush=True)


class State:
    def __init__(self, src, outdir, preview_w, analysis_w):
        self.outdir = os.path.abspath(outdir)
        self.preview_w, self.analysis_w = preview_w, analysis_w
        self.lock = threading.Lock()
        self.params = dict(d.DEFAULTS)
        self.job = dict(state="idle", log=[], out=None)
        self.load(src)

    def load(self, src):
        """Switch to another shot, keeping the current settings."""
        if self.job["state"] == "running":
            raise RuntimeError("wait for the render to finish before opening another shot")
        src = os.path.abspath(src)
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        with self.lock:
            self.an = self.frames = None  # free the previous shot first
        self.src = src
        self.base = os.path.splitext(os.path.basename(src))[0]
        preview_w, analysis_w = self.preview_w, self.analysis_w
        log(f"analysing {self.base} ...")
        self.an = d.analyse(src, analysis_w, log)
        info = self.an["info"]
        # full resolution unless the clip wouldn't fit in ~1.5 GB of GPU memory (8-bit RGBA);
        # downscaling hides grain, so the preview would understate denoise
        n = info["n"] or 100
        fit = int((1.5e9 / (4 * n * info["h"] / info["w"])) ** 0.5)
        self.pw = min(info["w"], preview_w or info["w"], fit // 2 * 2)
        self.ph = int(round(info["h"] * self.pw / info["w"] / 2) * 2)
        if self.pw < info["w"]:
            log(f"  preview scaled to {self.pw}x{self.ph} to fit GPU memory")
        log("preparing preview frames ...")
        # raw 8-bit RGB, no JPEG: compression would smooth away the grain we're judging
        self.frames = [np.ascontiguousarray((np.clip(f, 0, 1) * 255 + 0.5).astype(np.uint8)[..., ::-1]).tobytes()
                       for f in d.read_frames(src, self.pw, self.ph)]
        self.version = 0
        self.job = dict(state="idle", log=[], out=None)
        self.recompute(self.params)

    def clips(self):
        folder = os.path.dirname(self.src)
        return dict(folder=folder, current=os.path.basename(self.src),
                    files=sorted(f for f in os.listdir(folder)
                                 if f.lower().endswith(VIDEO_EXT) and not f.startswith(".")))

    def compute_params(self, p):
        return {k: type(d.COMPUTE_DEFAULTS[k])(p.get(k, d.COMPUTE_DEFAULTS[k])) for k in d.COMPUTE_DEFAULTS}

    def recompute(self, p):
        cp = self.compute_params(p)
        with self.lock:
            if self.version and cp == self.compute_params(self.params):
                return
            t = time.time()
            comps, E = d.compute(self.an, cp, log=lambda m: None)
            # 8-bit transport, offset 128, symmetric scale per component group
            sy = float(max(np.percentile(np.abs(comps[..., 0]), 99.95), 1e-3))
            sc = float(max(np.percentile(np.abs(comps[..., 1:3]), 99.95), 1e-3))
            sd = float(max(np.percentile(np.abs(comps[..., 3]), 99.95), 1e-3))
            q = np.empty(comps.shape, np.uint8)  # RGBA = luma, chroma B, chroma R, detail
            for ch, s in ((slice(0, 1), sy), (slice(1, 3), sc), (slice(3, 4), sd)):
                q[..., ch] = np.clip(np.round(128 + comps[..., ch] / s * 127), 1, 255)
            emax = float(max(np.percentile(E, 99.9), 0.01))
            self.comps, self.E = comps, E
            self.gains_bytes = q.tobytes()
            self.energy_bytes = np.clip(np.round(E / emax * 255), 0, 255).astype(np.uint8).tobytes()
            self.scales = dict(y=sy, c=sc, e=emax, d=sd)
            self.params = {**self.params, **cp}
            self.version += 1
            log(f"compute v{self.version} {cp} in {time.time() - t:.1f}s")

    def final_params(self, p):
        return {**self.params, **{k: float(p.get(k, d.FINAL_DEFAULTS[k])) for k in d.FINAL_DEFAULTS}}

    def info(self):
        a, i = self.an, self.an["info"]
        return dict(name=self.base, n=len(self.frames), fps=i["fps"], w=i["w"], pw=self.pw, ph=self.ph,
                    aw=a["aw"], ah=a["ah"], auto_period=a["auto_period"], f0=a["f0"], pos=a["pos"].tolist(),
                    version=self.version, scales=self.scales, params=self.params,
                    defaults=d.DEFAULTS, compute_keys=list(d.COMPUTE_DEFAULTS), presets=list_presets(),
                    outdir=self.outdir, src=self.src, clips=self.clips())

    def measure(self, p):
        self.recompute(p)
        fp = self.final_params(p)
        with self.lock:
            gains, _ = d.finalize(self.comps, self.E, fp)
            return d.metrics(self.an, gains, fp["period"] or self.an["auto_period"])

    def start_render(self, p, label):
        if self.job["state"] == "running":
            return False
        label = re.sub(r"[^A-Za-z0-9_-]+", "_", label or time.strftime("%H%M%S")).strip("_")
        self.job = dict(state="running", log=[], out=None, label=label)
        threading.Thread(target=self._render, args=(p, label), daemon=True).start()
        return True

    def _render(self, p, label):
        job = self.job
        jl = lambda m: (job["log"].append(m), log(m))  # noqa: E731
        try:
            self.recompute(p)
            fp = self.final_params(p)
            with self.lock:
                gains, mask = d.finalize(self.comps, self.E, fp)
                m = d.metrics(self.an, gains, fp["period"] or self.an["auto_period"])
            os.makedirs(self.outdir, exist_ok=True)
            stem = os.path.join(self.outdir, f"{self.base}_{label}")
            with open(stem + "_settings.json", "w") as f:
                json.dump(fp, f, indent=2)
            cv2.imwrite(stem + "_mask.png", (mask * 255).astype(np.uint8))
            jl(f"flicker removed (%): {m}")
            info = self.an["info"]
            d.render(self.src, stem + "_DF.mov", info, gains, d.source_profile(info), fp,
                     self.an["pos"], info["w"] / self.an["aw"], jl)
            jl("writing side-by-side compare ...")
            d.preview(self.src, stem + "_DF.mov", stem + "_compare.mp4")
            job.update(state="done", out=stem + "_DF.mov")
            jl("done")
        except Exception:
            job.update(state="error")
            jl(traceback.format_exc())


def choose(kind, start):
    """Native macOS Finder dialog. Returns a POSIX path, or None if cancelled."""
    loc = f' default location (POSIX file "{start}")' if start and os.path.isdir(start) else ""
    what = ('choose file with prompt "Choose a shot" of type {"public.movie"}' if kind == "file"
            else 'choose folder with prompt "Choose the output folder"')
    r = subprocess.run(["osascript", "-e", "activate", "-e", f"POSIX path of ({what}{loc})"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def list_presets():
    if not os.path.isdir(PRESETS):
        return []
    return sorted(f[:-5] for f in os.listdir(PRESETS) if f.endswith(".json"))


class Handler(BaseHTTPRequestHandler):
    state: State = None

    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        s = self.state
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(f.read(), "text/html; charset=utf-8")
        if path == "/api/info":
            return self.send(s.info())
        m = re.fullmatch(r"/frame/(\d+)\.rgb", path)
        if m and int(m.group(1)) < len(s.frames):
            return self.send(s.frames[int(m.group(1))], "application/octet-stream")
        if path == "/api/gains":
            return self.send(s.gains_bytes, "application/octet-stream", headers={"X-Version": str(s.version)})
        if path == "/api/energy":
            return self.send(s.energy_bytes, "application/octet-stream", headers={"X-Version": str(s.version)})
        if path == "/api/job":
            return self.send(s.job)
        m = re.fullmatch(r"/api/preset/([A-Za-z0-9_-]+)", path)
        if m and os.path.exists(os.path.join(PRESETS, m.group(1) + ".json")):
            with open(os.path.join(PRESETS, m.group(1) + ".json")) as f:
                return self.send(f.read())
        self.send({"error": "not found"}, code=404)

    def do_POST(self):
        s = self.state
        try:
            p = self.body()
            if self.path == "/api/compute":
                s.recompute(p)
                return self.send(s.info())
            if self.path == "/api/metrics":
                return self.send(s.measure(p))
            if self.path == "/api/render":
                ok = s.start_render(p.get("params", {}), p.get("label"))
                return self.send({"started": ok, "job": s.job})
            if self.path == "/api/open":
                if p.get("params"):
                    s.params = {**d.DEFAULTS, **{k: v for k, v in p["params"].items() if k in d.DEFAULTS}}
                s.load(p["path"])
                return self.send(s.info())
            if self.path == "/api/outdir":
                path = os.path.abspath(os.path.expanduser(p["path"]))
                os.makedirs(path, exist_ok=True)
                s.outdir = path
                return self.send({"outdir": path})
            if self.path == "/api/choose":
                start = s.outdir if p.get("kind") == "folder" else os.path.dirname(s.src)
                return self.send({"path": choose(p.get("kind"), start)})
            if self.path == "/api/reveal":
                subprocess.run(["open", s.outdir if os.path.isdir(s.outdir) else os.path.dirname(s.outdir)])
                return self.send({"ok": True})
            if self.path == "/api/preset":
                name = re.sub(r"[^A-Za-z0-9_-]+", "_", p.get("name", "")).strip("_")
                if not name:
                    return self.send({"error": "name required"}, code=400)
                os.makedirs(PRESETS, exist_ok=True)
                with open(os.path.join(PRESETS, name + ".json"), "w") as f:
                    json.dump({k: p["params"][k] for k in d.DEFAULTS if k in p["params"]}, f, indent=2)
                return self.send({"saved": name, "presets": list_presets(),
                                  "path": os.path.join(PRESETS, name + ".json")})
        except Exception:
            return self.send({"error": traceback.format_exc()}, code=500)
        self.send({"error": "not found"}, code=404)


def main():
    root = os.path.dirname(os.path.dirname(HERE))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--outdir", default=os.path.join(root, "exports"))
    ap.add_argument("--preview-width", type=int, default=0, help="0 = full resolution (auto-reduced for long clips)")
    ap.add_argument("--analysis-width", type=int, default=640)
    args = ap.parse_args()
    Handler.state = State(args.clip, args.outdir, args.preview_width, args.analysis_width)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log(f"ready: http://localhost:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
