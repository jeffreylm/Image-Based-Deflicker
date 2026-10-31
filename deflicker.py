#!/usr/bin/env python3
"""
Projection-mapping deflicker.

Removes rolling horizontal bands caused by projector refresh beating against
the camera's rolling shutter. Unlike global deflicker plugins it corrects
brightness and colour locally, only where the flicker actually is.

How it works
  1. Analyse a low-res copy of the clip, measure camera motion (translation)
     and the flicker period (frames per beat cycle).
  2. For every frame, build a reference by averaging motion-aligned neighbours
     over whole flicker periods (a box of one period convolved `periods` times),
     which cancels the beat and its harmonics while keeping the slowly
     changing projected content.
  3. The log-ratio reference/current is split into a luma part and chroma
     parts (the projector's colour wheel makes bands shift hue, not just
     brightness). Each is smoothed mostly along x (bands are sensor rows),
     robustly re-weighted to ignore moving people, and clamped.
  4. finalize() mixes luma/chroma with their strengths and masks the result to
     areas that really flicker. The tuning UI does the same step in a shader.
  5. The full-res clip is re-rendered with the gain map applied, in the same
     ProRes flavour, colour tags and timecode as the source.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
LUMA = np.array([0.0722, 0.7152, 0.2126], np.float32)  # BGR order, Rec.709
EPS = 1e-3

# analysis parameters (changing them needs compute())
COMPUTE_DEFAULTS = dict(period=0, periods=2, smooth_x=0.01, smooth_y=0.004, chroma_smooth=1.0,
                        robust=0.03, clamp=0.3, passes=1, detail_size=4.0, detail_smooth=0.0025,
                        detail_coherence=0.25)
# mixing parameters (cheap, applied by finalize() / the UI shader)
FINAL_DEFAULTS = dict(luma=1.0, chroma=1.0, detail=1.0, strength=1.0, mask_lo=0.002, mask_hi=0.006,
                      dn_luma=0.5, dn_chroma=1.0, dn_radius=2, dn_threshold=0.03, dn_shadows=0.12)
DETAIL_CLAMP = 0.5
DETAIL_EPS = 0.02 ** 2
DEFAULTS = {**COMPUTE_DEFAULTS, **FINAL_DEFAULTS}


def probe(path):
    out = subprocess.check_output([
        FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height,r_frame_rate,nb_frames,profile,pix_fmt:stream_tags=timecode:format_tags=timecode",
        "-of", "json", path])
    info = json.loads(out)
    s = info["streams"][0]
    num, den = map(int, s["r_frame_rate"].split("/"))
    tc = s.get("tags", {}).get("timecode") or info.get("format", {}).get("tags", {}).get("timecode")
    return dict(w=s["width"], h=s["height"], fps=num / den, rate=s["r_frame_rate"],
                n=int(s.get("nb_frames", 0)), profile=s.get("profile", ""),
                pix_fmt=s.get("pix_fmt", ""), timecode=tc)


def read_frames(path, w, h):
    """Yield float32 BGR frames in [0,1], decoded as 16-bit, Rec.709 tv -> full range."""
    cmd = [FFMPEG, "-v", "error", "-i", path, "-map", "0:v:0",
           "-vf", f"scale={w}:{h}:flags=area:in_color_matrix=bt709:in_range=tv:out_range=pc,format=bgr48le",
           "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=w * h * 6 * 4)
    size = w * h * 6
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint16).reshape(h, w, 3).astype(np.float32) / 65535.0
    finally:
        p.stdout.close()
        p.kill()
        p.wait()


# ---------------------------------------------------------------- analysis

def estimate_motion(L):
    """Cumulative (dx, dy) of every frame relative to frame 0, via phase correlation."""
    win = cv2.createHanningWindow(L.shape[1:][::-1], cv2.CV_32F)
    pos = np.zeros((len(L), 2), np.float32)
    for i in range(1, len(L)):
        (dx, dy), _ = cv2.phaseCorrelate(np.exp(L[i - 1]), np.exp(L[i]), win)
        pos[i] = pos[i - 1] + (dx, dy)
    return pos


def shift(img, dx, dy):
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, M, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(np.nan,) * 4)


def estimate_period(L, fps):
    """Dominant beat period in frames, from row-profile residuals of bright areas."""
    rows = []
    bright = np.exp(L).mean(0) > np.percentile(np.exp(L).mean(0), 70)
    for f in L:
        with np.errstate(all="ignore"): rows.append(np.nanmean(np.where(bright, f, np.nan), axis=1))
    R = np.nan_to_num(np.array(rows))
    k = np.ones(9) / 9
    trend = np.apply_along_axis(lambda v: np.convolve(np.pad(v, 4, mode="edge"), k, "valid"), 0, R)
    spec = np.abs(np.fft.rfft(R - trend, axis=0)).mean(1)
    freqs = np.fft.rfftfreq(len(R), 1 / fps)
    band = (freqs > 1.5) & (freqs < fps / 2 - 0.5)
    f0 = freqs[band][np.argmax(spec[band])]
    return max(2, int(round(fps / f0))), f0


def analyse(src, aw=640, log=print):
    """Low-res decode + motion + period. Everything compute() needs."""
    info = probe(src)
    ah = int(round(info["h"] * aw / info["w"] / 2) * 2)
    frames = np.array(list(read_frames(src, aw, ah)), np.float32)
    L = np.log(frames @ LUMA + EPS)
    chan_mean = frames.reshape(-1, 3).mean(0)
    logs = np.log(frames + EPS)
    del frames
    pos = estimate_motion(L)
    period, f0 = estimate_period(L, info["fps"])
    log(f"  frames={len(L)} period={period} frames (~{f0:.2f} Hz) drift={np.ptp(pos, 0).round(1)} px@{aw}")
    return dict(src=src, info=info, aw=aw, ah=ah, logs=logs, pos=pos, chan_mean=chan_mean,
                auto_period=period, f0=f0)


def window(period, periods):
    """Box of one period convolved `periods` times: zeros at the beat and all its harmonics."""
    taps = np.ones(period)
    for _ in range(periods - 1):
        taps = np.convolve(taps, np.ones(period))
    return (taps / taps.sum()).astype(np.float32)


def smooth_weighted(D, W, sx, sy):
    """Normalised anisotropic Gaussian convolution."""
    num = cv2.GaussianBlur(D * W, (0, 0), sigmaX=sx, sigmaY=sy)
    den = cv2.GaussianBlur(W, (0, 0), sigmaX=sx, sigmaY=sy)
    return num / np.maximum(den, 1e-6)


def robust_smooth(D, W, sx, sy, robust):
    S = smooth_weighted(D, W, sx, sy)
    # drop pixels that disagree with the band model (moving people, parallax)
    W2 = W / (1 + ((D - S) / robust) ** 2)
    return smooth_weighted(D, W2, sx, sy)


def frame_components(logs, pos, t, taps, chan_mean, sx, sy, sc, robust, clamp):
    """(h, w, 3) = smoothed log-gain [luma, chroma B, chroma R] for frame t."""
    n = len(logs)
    half = len(taps) // 2
    # window shifted inward at clip edges so cancellation stays exact
    c = min(max(t, half), n - 1 - half) if n > 2 * half else t
    acc = np.zeros_like(logs[t])
    wsum = np.zeros(logs[t].shape[:2], np.float32)
    for k, wt in enumerate(taps):
        j = c - half + k
        if j < 0 or j >= n:
            continue
        d = pos[t] - pos[j]
        # content of frame j sits at pos[j]; move it to where frame t has it
        img = logs[j] if j == t else shift(logs[j], d[0], d[1])
        ok = np.isfinite(img).all(-1)
        acc[ok] += wt * img[ok]
        wsum[ok] += wt
    ref = np.where((wsum > 0.5)[..., None], acc / np.maximum(wsum, 1e-6)[..., None], logs[t])
    D = ref - logs[t]
    Dy = D @ LUMA
    lin = np.exp(logs[t])
    # weights: bright pixels are reliable (log of dark pixels is noise)
    W = np.clip((lin @ LUMA) / (chan_mean @ LUMA), 0, 4).astype(np.float32)
    out = np.empty(D.shape, np.float32)
    out[..., 0] = robust_smooth(Dy, W, sx, sy, robust)
    for o, ch in ((1, 0), (2, 2)):
        Wc = np.minimum(W, np.clip(lin[..., ch] / chan_mean[ch], 0, 4)).astype(np.float32)
        out[..., o] = robust_smooth(D[..., ch] - Dy, Wc, sx * sc, sy * sc, robust)
    return np.clip(out, -clamp, clamp)


def split_detail(img, size):
    """Base/detail split used for the sharpness pulse; size in full-res pixels."""
    lo = cv2.GaussianBlur(img, (0, 0), size)
    return lo, img - lo


def contrast_maps(an, size, log=print):
    """Log local contrast (detail energy / base^2) per frame at analysis res, from full-res
    luma. Contrast is brightness-independent, so the brightness fix doesn't affect it.
    Cached per detail size (needs a full-res decode)."""
    cache = an.setdefault("contrast", {})
    if size not in cache:
        log(f"  measuring detail (size {size}px) at full res ...")
        info, aw, ah = an["info"], an["aw"], an["ah"]
        out = []
        for f in read_frames(an["src"], info["w"], info["h"]):
            lo, hi = split_detail(f @ LUMA, size)
            num = cv2.resize(hi * hi, (aw, ah), interpolation=cv2.INTER_AREA)
            den = cv2.resize(lo, (aw, ah), interpolation=cv2.INTER_AREA) ** 2
            out.append(np.log(num / (den + DETAIL_EPS) + 1e-9))
        cache.clear()  # keep one size in memory
        cache[size] = np.array(out, np.float32)
    return cache[size]


def pulse_mask(an, C, period, coherence):
    """Where the contrast really pulses with the beat. Sensor grain also changes contrast
    every frame, but randomly; correcting it would make the blacks pump. So keep only
    pixels whose contrast energy is concentrated at the beat frequency, and not too dark."""
    n = len(C)
    fps = an["info"]["fps"]
    Cs = np.array([cv2.GaussianBlur(c, (0, 0), 3) for c in C]).reshape(n, -1)
    V = np.vander(np.arange(n), 3)
    Cs = Cs - V @ np.linalg.lstsq(V, Cs, rcond=None)[0]
    sp = np.abs(np.fft.rfft(Cs * np.hanning(n)[:, None], axis=0)) ** 2
    f = np.fft.rfftfreq(n, 1 / fps)
    sel = np.zeros_like(f, bool)
    for k in range(1, int(fps / 2 / (fps / period)) + 1):
        sel |= np.abs(f - k * fps / period) < 0.4
    ratio = (sp[sel].sum(0) / np.maximum(sp[f > 1].sum(0), 1e-12)).reshape(C.shape[1:]).astype(np.float32)
    ratio = cv2.GaussianBlur(ratio, (0, 0), 8)
    Y = np.exp(an["logs"]).mean(0) @ LUMA
    smooth = lambda x, a, b: (lambda m: m * m * (3 - 2 * m))(np.clip((x - a) / (b - a), 0, 1))  # noqa: E731
    return smooth(ratio, coherence, coherence + 0.25) * smooth(Y, 0.02, 0.06)


def detail_gains(an, taps, period, p, log=print):
    """Log amplitude gain for the detail layer that evens out the projector's sharp/soft
    pulse (rows of a frame catching two projected video frames)."""
    C = contrast_maps(an, float(p["detail_size"]), log)
    pos, n = an["pos"], len(C)
    half = len(taps) // 2
    s = p["detail_smooth"] * an["aw"]
    G = np.zeros_like(C)
    for t in range(n):
        c = min(max(t, half), n - 1 - half) if n > 2 * half else t
        acc = np.zeros_like(C[t])
        wsum = np.zeros_like(C[t])
        for k, wt in enumerate(taps):
            j = c - half + k
            if j < 0 or j >= n:
                continue
            d = pos[t] - pos[j]
            img = C[j] if j == t else shift(C[j], d[0], d[1])
            ok = np.isfinite(img)
            acc[ok] += wt * img[ok]
            wsum[ok] += wt
        ref = np.where(wsum > 0.5, acc / np.maximum(wsum, 1e-6), C[t])
        # contrast is energy (squared amplitude), so amplitude gain is half the log ratio
        G[t] = cv2.GaussianBlur(0.5 * (ref - C[t]), (0, 0), s)
    return np.clip(G, -DETAIL_CLAMP, DETAIL_CLAMP) * pulse_mask(an, C, period, p["detail_coherence"])[None]


def compute(an, p, log=print):
    """Unmasked components (n, h, w, 4) [luma, chroma B, chroma R, detail] and flicker energy map E."""
    period = p["period"] or an["auto_period"]
    taps = window(int(period), int(p["periods"]))
    sx, sy = p["smooth_x"] * an["aw"], max(p["smooth_y"] * an["ah"], 0.3)
    logs, n = an["logs"], len(an["logs"])
    comps = np.zeros(logs.shape[:3] + (4,), np.float32)
    with ThreadPoolExecutor(os.cpu_count()) as ex:
        for k in range(int(p["passes"])):
            cur = logs if k == 0 else logs + gains_from(comps, 1, 1)
            res = ex.map(lambda t: frame_components(cur, an["pos"], t, taps, an["chan_mean"], sx, sy,
                                                    p["chroma_smooth"], p["robust"], p["clamp"]), range(n))
            for t, c in enumerate(res):
                comps[t, ..., :3] += c
            log(f"  pass {k + 1}/{int(p['passes'])} done")
    comps[..., 3] = detail_gains(an, taps, int(period), p, log)
    E = np.sqrt((comps[..., 0] ** 2 + 0.5 * (comps[..., 1] ** 2 + comps[..., 2] ** 2)).mean(0))
    E = cv2.GaussianBlur(E, (0, 0), sigmaX=sx / 2, sigmaY=sx / 2)
    return comps, E


def gains_from(comps, luma, chroma):
    """Components -> per-channel BGR log-gains."""
    Dy, Cb, Cr = comps[..., 0], comps[..., 1], comps[..., 2]
    Cg = -(LUMA[0] * Cb + LUMA[2] * Cr) / LUMA[1]  # chroma parts sum to zero luma
    return np.stack([luma * Dy + chroma * Cb, luma * Dy + chroma * Cg, luma * Dy + chroma * Cr], -1)


def flicker_mask(E, lo, hi):
    m = np.clip((E - lo) / max(hi - lo, 1e-6), 0, 1)
    return m * m * (3 - 2 * m)


def finalize(comps, E, p):
    """Mix + mask, exactly like the UI shader. Returns (n, h, w, 4) log-gains
    [B, G, R, detail amplitude] and the mask."""
    mask = flicker_mask(E, p["mask_lo"], p["mask_hi"])
    g = np.concatenate([gains_from(comps, p["luma"], p["chroma"]),
                        p["detail"] * comps[..., 3:]], -1) * (mask[None, ..., None] * p["strength"])
    return g.astype(np.float16), mask


def metrics(an, gains, period):
    """Share of beat-frequency energy removed, measured on 8x8 tiles of the bright half
    of the frame (what the eye sees as local flicker): luma, R-G, B-G and the sharp/soft
    detail pulse (local contrast)."""
    logs = an["logs"]
    fps = an["info"]["fps"]
    f0 = fps / period
    n, h, w, _ = logs.shape
    th, tw = h // 8, w // 8

    def tiles(X):
        # plain block mean: (n, th*tw); works for any clip length (cv2.resize caps at 512 channels)
        return X[:, :th * 8, :tw * 8].reshape(n, th, 8, tw, 8).mean((2, 4)).reshape(n, th * tw)

    lin = tiles(np.exp(logs) @ LUMA).mean(0)
    bright = lin > np.percentile(lin, 50)
    f = np.fft.rfftfreq(n, 1 / fps)
    sel = np.zeros_like(f, bool)
    for k in range(1, int(fps / 2 / f0) + 1):
        sel |= np.abs(f - k * f0) < 0.4
    x = np.arange(n)
    win = np.hanning(n)[:, None]

    def beat(R):
        # remove slow trend + Hann window, else slow content changes leak into the beat bins
        R = R - np.vander(x, 3) @ np.linalg.lstsq(np.vander(x, 3), R, rcond=None)[0]
        return (np.abs(np.fft.rfft(R * win, axis=0)) ** 2)[sel].sum()

    out = {}
    gains = gains.astype(np.float32)
    fixed = logs + gains[..., :3]
    for name, fn in (("luma", lambda v: v @ LUMA), ("red_green", lambda v: v[..., 2] - v[..., 1]),
                     ("blue_green", lambda v: v[..., 0] - v[..., 1])):
        a, b = beat(tiles(fn(logs))[:, bright]), beat(tiles(fn(fixed))[:, bright])
        out[name] = round(float(100 * (1 - b / max(a, 1e-12))), 1)
    for C in an.get("contrast", {}).values():
        # contrast is energy: amplitude gain g changes it by 2g
        a, b = beat(tiles(C)[:, bright]), beat(tiles(C + 2 * gains[..., 3])[:, bright])
        out["detail"] = round(float(100 * (1 - b / max(a, 1e-12))), 1)
    return out


# ---------------------------------------------------------------- render

_APPLE_PRORES = None


def apple_prores_available():
    """Apple's ProRes encoder (VideoToolbox) keeps ~13x more fine detail than ffmpeg's
    prores_ks, which visibly smooths grain in the shadows. Test it once."""
    global _APPLE_PRORES
    if _APPLE_PRORES is None:
        r = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:d=0.04",
                            "-vf", "format=p416le", "-c:v", "prores_videotoolbox", "-profile:v", "5",
                            "-f", "null", "-"], capture_output=True)
        _APPLE_PRORES = r.returncode == 0
    return _APPLE_PRORES


def encoder_cmd(src, info, out, profile):
    tags = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
    if apple_prores_available():
        fmt = "p416le" if profile >= 4 else "p210le"  # 16-bit 4:4:4 / 10-bit 4:2:2 into the encoder
        codec = ["-c:v", "prores_videotoolbox", "-profile:v", str(profile)]
    else:
        fmt = "yuv444p12le" if profile >= 4 else "yuv422p10le"
        codec = ["-c:v", "prores_ks", "-profile:v", str(profile), "-vendor", "apl0", "-bits_per_mb", "8000"]
    cmd = [FFMPEG, "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr48le", "-s", f"{info['w']}x{info['h']}",
           "-r", info["rate"], "-i", "-",
           "-vf", f"scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format={fmt},{tags}",
           *codec,
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
           "-color_range", "tv"]
    if info["timecode"]:
        cmd += ["-timecode", info["timecode"]]
    return cmd + [out]


def source_profile(info):
    return {"XQ": 5, "4444": 4, "HQ": 3}.get(info["profile"], 5)


def denoise(buf, t, pos, scale, p):
    """Temporal denoise of corrected frame t from its aligned neighbours in buf, exactly
    like the UI shader. A neighbour pixel only counts if it looks like this one (weight
    falls off past dn_threshold), so moving content doesn't ghost. Luma and chroma are
    mixed in separately, and only in the shadows (dn_shadows = 1 means everywhere)."""
    c = buf[t]
    R = int(p["dn_radius"])
    if R == 0 or (p["dn_luma"] == 0 and p["dn_chroma"] == 0):
        return c
    H, W = c.shape[:2]
    acc, ws = c.copy(), np.ones((H, W), np.float32)
    for j in range(t - R, t + R + 1):
        if j == t or j not in buf:
            continue
        d = (pos[t] - pos[j]) * scale
        cj = cv2.warpAffine(buf[j], np.float32([[1, 0, d[0]], [0, 1, d[1]]]), (W, H),
                            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        diff = cj - c
        w = np.exp(-(diff * diff).mean(-1) / (2 * p["dn_threshold"] ** 2))
        acc += w[..., None] * cj
        ws += w
    avg = acc / ws[..., None]
    yt, ya = c @ LUMA, avg @ LUMA
    lim = p["dn_shadows"]
    if lim >= 1:
        m = np.ones_like(ya)
    else:
        x = np.clip((ya - lim) / lim, 0, 1)
        m = 1 - x * x * (3 - 2 * x)
    dy = p["dn_luma"] * (ya - yt)
    dc = p["dn_chroma"] * ((avg - ya[..., None]) - (c - yt[..., None]))
    return np.clip(c + m[..., None] * (dy[..., None] + dc), 0, 1)


def render(src, out, info, gains, profile, p, pos, scale, log=print):
    """pos/scale: analysis-res camera motion and full-res/analysis-res ratio (for denoise)."""
    enc = subprocess.Popen(encoder_cmd(src, info, out, profile), stdin=subprocess.PIPE)
    W, H = info["w"], info["h"]
    R = int(p["dn_radius"]) if (p["dn_luma"] > 0 or p["dn_chroma"] > 0) else 0
    buf = {}

    def emit(t):
        fr = denoise(buf, t, pos, scale, p)
        enc.stdin.write((fr * 65535 + 0.5).astype(np.uint16).tobytes())
        for k in [k for k in buf if k < t - R]:
            del buf[k]
        if t % 25 == 0:
            log(f"  render {t}/{len(gains)}")

    total = 0
    for t, fr in enumerate(read_frames(src, W, H)):
        if t >= len(gains):
            g = np.zeros((H, W, 4), np.float32)
        else:
            g = cv2.resize(gains[t].astype(np.float32), (W, H), interpolation=cv2.INTER_CUBIC)
        # same order as the UI shader: even out detail, then brightness/colour gain, then denoise
        lo, hi = split_detail(fr, p["detail_size"])
        buf[t] = np.clip((lo + hi * np.exp(g[..., 3:])) * np.exp(g[..., :3]), 0, 1)
        total = t + 1
        if t - R >= 0:
            emit(t - R)
    for t in range(max(total - R, 0), total):
        emit(t)
    enc.stdin.close()
    if enc.wait() != 0:
        raise RuntimeError("ffmpeg encode failed")


def preview(src, out, dst):
    """Side-by-side H.264 preview: original (left) | deflickered (right)."""
    subprocess.check_call([
        FFMPEG, "-v", "error", "-y", "-i", src, "-i", out, "-filter_complex",
        "[0:v]scale=960:-2[a];[1:v]scale=960:-2[b];[a][b]hstack,format=yuv420p",
        "-c:v", "libx264", "-crf", "16", "-preset", "medium", dst])


# ---------------------------------------------------------------- main

def process(src, outdir, p, aw=640, profile=-1, want_preview=False, log=print):
    base = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(outdir, base + "_DF.mov")
    an = analyse(src, aw, log)
    info = an["info"]
    log(f"{base}: {info['w']}x{info['h']} @ {info['rate']} {info['profile']} tc={info['timecode']}")
    comps, E = compute(an, p, log)
    gains, mask = finalize(comps, E, p)
    log(f"  flicker removed (%): {metrics(an, gains, p['period'] or an['auto_period'])}")
    cv2.imwrite(os.path.join(outdir, base + "_mask.png"), (mask * 255).astype(np.uint8))
    with open(os.path.join(outdir, base + "_settings.json"), "w") as f:
        json.dump(p, f, indent=2)
    render(src, out, info, gains, source_profile(info) if profile < 0 else profile, p,
           an["pos"], info["w"] / an["aw"], log)
    if want_preview:
        preview(src, out, os.path.join(outdir, base + "_compare.mp4"))
    log(f"  -> {out}")
    return out


def main():
    ap = argparse.ArgumentParser(description="Deflicker projection-mapping footage.")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("--preset", help="JSON settings file (saved from the tuning UI)")
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), type=type(v), default=None)
    ap.add_argument("--analysis-width", type=int, default=640)
    ap.add_argument("--profile", type=int, default=-1, help="prores_ks profile (-1 = match source)")
    ap.add_argument("--preview", action="store_true", help="also write side-by-side H.264 comparison")
    args = ap.parse_args()

    p = dict(DEFAULTS)
    if args.preset:
        with open(args.preset) as f:
            p.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    p.update({k: getattr(args, k) for k in DEFAULTS if getattr(args, k) is not None})

    os.makedirs(args.outdir, exist_ok=True)
    for src in args.inputs:
        process(src, args.outdir, p, args.analysis_width, args.profile, args.preview,
                log=lambda m: print(m, flush=True))


if __name__ == "__main__":
    sys.exit(main())
