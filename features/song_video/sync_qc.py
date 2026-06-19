#!/usr/bin/env python3
"""Audio<->mouth lip-sync QC -- the real check qc.py never was.

WHY THIS EXISTS
  orchestrator/qc.py measures raw frame-to-frame motion in a FIXED box and never
  looks at the audio, so it cannot tell a lip-synced mouth from an eye-blink or
  random jitter. That blind spot is what made the multi-segment problem so hard
  to pin down: there was no trustworthy way to tell which clips actually synced.

WHAT THIS DOES
  Builds a per-pixel SYNC HEATMAP: each pixel's frame-to-frame motion is
  correlated (over small temporal lags) with the audio's syllabic-activity
  envelope (spectral-flux onset strength -- it spikes on each new syllable,
  exactly when a real mouth changes shape). A genuine lip-sync shows a coherent
  cluster of audio-correlated motion at the MOUTH (lower-center). A blink shows
  motion at the EYES that does NOT correlate with the audio. A dead render shows
  no motion at all. Three numbers separate the three cases:

    total_motion   low  => static / dead mouth
    sync_contrast  how far the best coherent audio-correlated region stands
                   above the per-clip AI-shimmer floor
    sync_y         vertical position of that region (0=top/forehead .. 1=mouth)

ROOT-CAUSE CONTEXT (2026-05-31)
  The multi-segment failure was NOT a positional bug -- window N is processed
  identically to window 0 everywhere (audio stats, slicing, A/V alignment, WanGP
  code path all verified). At 8 steps the distilled model is unstable: whether
  audio-driven motion lands on the mouth vs the eyes flips with seed x image x
  window. Proof: cat + viagra-window-1 at seeds {777, 354082694, 12345, 2024}
  gave sync_y {0.77(mouth), 0.31, 0.19, 0.03(eyes)}. So the fix is per-clip
  best-of-N seed selection (chain.py) gated/ranked by THIS metric.

ASSUMES a single, roughly centered subject (the product already requires a
clear front-facing face). Multi-subject / drifting compositions confound the
vertical-locus heuristic; use the heatmap PNG to spot-check those.

Deps: ffmpeg+ffprobe on PATH, numpy, scipy, librosa. cv2 optional (heatmap PNG).
"""
from __future__ import annotations
import argparse, json, math, subprocess, warnings
import numpy as np
from scipy.ndimage import uniform_filter

warnings.filterwarnings("ignore")

# --- acceptance thresholds (calibrated on the live A/B, 2026-05-31) ---------
MIN_TOTAL_MOTION  = 1.2     # below => static / dead mouth
MIN_SYNC_CONTRAST = 0.15    # synced region must stand above the shimmer floor
MOUTH_Y_MIN       = 0.50    # synced motion must be in the lower (mouth) half
MOUTH_Y_PEAK      = 0.68    # where a centered subject's mouth typically sits
MOUTH_Y_SIGMA     = 0.18


def probe(path):
    r = subprocess.run(["ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","stream=width,height,r_frame_rate","-of","csv=p=0",path],
        capture_output=True, text=True)
    w, h, rate = r.stdout.strip().split(",")[:3]
    num, den = rate.split("/")
    fps = float(num)/float(den) if float(den) else 24.0
    return int(w), int(h), fps


def _decode_gray(path, dw):
    w, h, fps = probe(path)
    dh = int(round(dw * h / w / 2) * 2)
    r = subprocess.run(["ffmpeg","-v","error","-i",path,
        "-vf",f"scale={dw}:{dh},format=gray","-f","rawvideo","-pix_fmt","gray","-"],
        capture_output=True)
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    n = buf.size // (dw*dh)
    return buf[:n*dw*dh].reshape(n, dh, dw), dw, dh, fps


def _onset_env(path, n_frames, fps, audio_path=None):
    import librosa
    sr = 16000
    y, _ = librosa.load(audio_path or path, sr=sr, mono=True)
    hop = max(1, int(round(sr/fps)))
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    if len(oenv) < 2:
        return np.zeros(n_frames)
    xi = np.linspace(0, len(oenv)-1, n_frames)
    return np.interp(xi, np.arange(len(oenv)), oenv)


def _z(a, axis=0, eps=1e-6):
    return (a - a.mean(axis=axis, keepdims=True)) / (a.std(axis=axis, keepdims=True) + eps)


def analyze(path, dw=220, max_lag=3, save_heatmap=None, audio_path=None):
    """Return the sync metrics dict for one clip (audio read from audio_path if
    given, else from the clip's own muxed track)."""
    frames, dw, dh, fps = _decode_gray(path, dw)
    if len(frames) < 12:
        return {"path": path, "error": "too few frames"}
    f = frames.astype(np.float32)
    diff = uniform_filter(np.abs(f[1:] - f[:-1]), size=(3,5,5), mode="nearest")
    T, H, W = diff.shape
    env = _onset_env(path, T, fps, audio_path=audio_path)

    total_motion = float(diff.mean())
    motion_map = diff.mean(axis=0)

    eb = _z(env)
    M = diff.reshape(T, -1)
    Mz = _z(M, axis=0)
    best = np.full(M.shape[1], -2.0, dtype=np.float32)
    for lag in range(0, max_lag + 1):                   # visual follows audio
        x, e = (Mz, eb) if lag == 0 else (Mz[lag:], eb[:-lag])
        best = np.maximum(best, (x * e[:, None]).mean(axis=0))
    corr = best.reshape(H, W).astype(np.float32)
    moving = motion_map >= 0.5
    corr[~moving] = 0.0
    corr_sm = uniform_filter(corr, size=7, mode="nearest")

    bg = float(np.median(corr_sm[moving])) if moving.any() else 0.0
    peak = float(corr_sm.max())
    py, px = np.unravel_index(int(np.argmax(corr_sm)), corr_sm.shape)
    sync_contrast = round(peak - bg, 3)
    sync_y = round(py / H, 2)
    sync_x = round(px / W, 2)

    k = max(20, motion_map.size // 100)
    my, _ = np.unravel_index(np.argsort(motion_map.ravel())[-k:], motion_map.shape)
    motion_y = round(float(my.mean() / H), 2)

    if total_motion < MIN_TOTAL_MOTION:
        verdict = "static"
    elif sync_contrast >= MIN_SYNC_CONTRAST and sync_y >= MOUTH_Y_MIN:
        verdict = "synced"
    elif sync_contrast >= MIN_SYNC_CONTRAST and sync_y < MOUTH_Y_MIN:
        verdict = "wrong-region(eyes/upper)"
    elif motion_y < 0.50:
        verdict = "wrong-region(eyes/upper)"
    else:
        verdict = "uncorrelated"

    r = {"path": path, "frames": int(T+1), "fps": round(fps, 2),
         "total_motion": round(total_motion, 2),
         "sync_contrast": sync_contrast, "sync_peak": round(peak, 3),
         "sync_y": sync_y, "sync_x": sync_x, "motion_y": motion_y,
         "verdict": verdict}
    r["mouth_sync_score"] = round(mouth_sync_score(r), 4)

    if save_heatmap:
        try:
            import cv2
            hm = np.clip(corr_sm, 0, 1)
            hm = (hm / (hm.max() + 1e-6) * 255).astype(np.uint8)
            hm = cv2.applyColorMap(hm, cv2.COLORMAP_JET)
            base = cv2.cvtColor(frames[len(frames)//2], cv2.COLOR_GRAY2BGR)
            cv2.imwrite(save_heatmap, cv2.addWeighted(base, 0.5, hm, 0.5, 0))
        except Exception:
            pass
    return r


def mouth_sync_score(r: dict) -> float:
    """Single scalar for ranking best-of-N candidates. Rewards real motion that
    is audio-correlated (contrast) AND located at the mouth band (gaussian around
    MOUTH_Y_PEAK, so chest motion below the mouth is NOT rewarded over the mouth).
    0 for static/dead clips."""
    if not r or r.get("error"):
        return 0.0
    if r["total_motion"] < MIN_TOTAL_MOTION:
        return 0.0
    mouth_w = math.exp(-(((r["sync_y"] - MOUTH_Y_PEAK) / MOUTH_Y_SIGMA) ** 2))
    return max(0.0, r["sync_contrast"]) * mouth_w


def is_synced(r: dict) -> bool:
    """Hard gate: enough motion, audio-correlated above the floor, at the mouth."""
    if not r or r.get("error"):
        return False
    return (r["total_motion"] >= MIN_TOTAL_MOTION
            and r["sync_contrast"] >= MIN_SYNC_CONTRAST
            and r["sync_y"] >= MOUTH_Y_MIN)


def main():
    ap = argparse.ArgumentParser(description="Audio<->mouth lip-sync QC")
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--audio", default=None, help="separate audio file (else read from clip)")
    ap.add_argument("--json", default=None)
    ap.add_argument("--heatmap", action="store_true", help="write <clip>.heatmap.png")
    ap.add_argument("--width", type=int, default=220)
    a = ap.parse_args()
    rows = []
    hdr = f"{'clip':<30} {'verdict':<24} {'score':>6} {'contrast':>9} {'syncY':>6} {'totMot':>7}"
    print(hdr); print("-"*len(hdr))
    for c in a.clips:
        try:
            r = analyze(c, dw=a.width, save_heatmap=(c+".heatmap.png" if a.heatmap else None),
                        audio_path=a.audio)
        except Exception as e:
            r = {"path": c, "error": str(e)}
        rows.append(r)
        name = c.replace("\\","/").split("/")[-1]
        if "error" in r:
            print(f"{name:<30} ERROR: {r['error']}"); continue
        print(f"{name:<30} {r['verdict']:<24} {r['mouth_sync_score']:>6} "
              f"{r['sync_contrast']:>9} {r['sync_y']:>6} {r['total_motion']:>7}")
    if a.json:
        with open(a.json, "w") as fh: json.dump(rows, fh, indent=2)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
