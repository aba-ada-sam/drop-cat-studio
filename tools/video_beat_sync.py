#!/usr/bin/env python3
"""
video_beat_sync.py -- Squeeze and stretch video to match visual changes with audio peaks.

Audio stays untouched. Only the video timeline is warped via ffmpeg setpts.

Uses DTW (Dynamic Time Warping) to align the continuous video-motion energy
profile against the audio onset+energy profile, so scene changes land on beats
and loud moments match high-motion moments.

Dependencies:
    pip install librosa scipy opencv-python
    ffmpeg must be on PATH
"""

import os
import sys
import json
import shutil
import tempfile
import threading
import subprocess
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# DTW-based warp computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_dtw_warp(video_path, audio_path, n_ctrl, log):
    """
    Compute a piecewise-linear time warp that maps video time to output time
    by aligning video motion energy with audio onset/beat energy via DTW.

    Returns (v_ctrl, a_ctrl, vid_dur, aud_dur):
      v_ctrl[i] seconds in the video should appear at a_ctrl[i] seconds in output.
    """
    import librosa
    import cv2
    from scipy.ndimage import gaussian_filter1d

    ANALYSIS_FPS = 8.0   # feature frames per second; higher = more detail, slower DTW

    # ── Audio feature ─────────────────────────────────────────────────────────
    log("Loading audio ...")
    y, sr = librosa.load(audio_path, sr=None, mono=True)
    aud_dur = float(len(y)) / sr

    hop = max(1, int(sr / ANALYSIS_FPS))
    real_audio_fps = sr / hop

    # Onset strength: captures beats, transients, note attacks
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    # RMS: captures sustained loudness
    rms_env = librosa.feature.rms(y=y, hop_length=hop)[0]

    n_audio = min(len(onset_env), len(rms_env))
    onset_env = onset_env[:n_audio]
    rms_env   = rms_env[:n_audio]

    audio_feat = (
        0.65 * onset_env / (onset_env.max() + 1e-8) +
        0.35 * rms_env   / (rms_env.max()   + 1e-8)
    )
    audio_times = np.arange(n_audio) * hop / sr
    log(f"  Audio: {aud_dur:.2f}s  ({n_audio} frames @ {real_audio_fps:.1f}fps)")

    # ── Video motion feature ──────────────────────────────────────────────────
    log("Computing video motion profile ...")
    cap = cv2.VideoCapture(video_path)
    vid_fps   = cap.get(cv2.CAP_PROP_FPS) or 24.0
    n_frames  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    vid_dur   = float(n_frames) / vid_fps

    skip = max(1, int(round(vid_fps / ANALYSIS_FPS)))
    real_video_fps = vid_fps / skip

    diffs = []
    prev  = None
    idx   = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % skip == 0:
            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (160, 90))
            if prev is not None:
                d = float(np.mean(np.abs(
                    small.astype(np.float32) - prev.astype(np.float32)
                )))
                diffs.append(d)
            prev = small
        idx += 1
    cap.release()

    if not diffs:
        raise RuntimeError("No video frames could be read.")

    video_feat  = np.array(diffs, dtype=np.float32)
    video_feat /= (video_feat.max() + 1e-8)
    video_times = np.arange(len(video_feat)) * skip / vid_fps
    log(f"  Video: {vid_dur:.2f}s  ({len(video_feat)} frames @ {real_video_fps:.1f}fps)")

    # ── Smooth so DTW focuses on large-scale structure, not noise ─────────────
    # sigma = ~0.6 second window
    audio_smooth = gaussian_filter1d(audio_feat, sigma=max(1.0, real_audio_fps * 0.6))
    video_smooth = gaussian_filter1d(video_feat, sigma=max(1.0, real_video_fps * 0.6))

    # ── DTW ──────────────────────────────────────────────────────────────────
    log(f"Running DTW on ({len(video_smooth)} x {len(audio_smooth)}) feature matrix ...")
    D, wp = librosa.sequence.dtw(
        X=video_smooth.reshape(1, -1),
        Y=audio_smooth.reshape(1, -1),
        subseq=False,
        backtrack=True,
        global_constraints=False,   # no Sakoe-Chiba band -- allow full flexibility
    )
    wp = wp[::-1]   # DTW returns end→start; flip to start→end
    log(f"  Path length: {len(wp)} steps")

    # ── Build control points from DTW path ────────────────────────────────────
    v_path = video_times[np.minimum(wp[:, 0], len(video_times) - 1)]
    a_path = audio_times[np.minimum(wp[:, 1], len(audio_times) - 1)]

    # Sample n_ctrl evenly-spaced points along the DTW path
    idx_samples = np.linspace(0, len(v_path) - 1, n_ctrl, dtype=int)
    v_ctrl = list(v_path[idx_samples])
    a_ctrl = list(a_path[idx_samples])

    # Force bookends
    v_ctrl = [0.0] + v_ctrl + [vid_dur]
    a_ctrl = [0.0] + a_ctrl + [aud_dur]

    # Remove any non-strictly-monotone points (DTW can repeat indices)
    v_out, a_out = [v_ctrl[0]], [a_ctrl[0]]
    for i in range(1, len(v_ctrl)):
        if v_ctrl[i] > v_out[-1] + 0.02 and a_ctrl[i] > a_out[-1] + 0.02:
            v_out.append(v_ctrl[i])
            a_out.append(a_ctrl[i])

    if v_out[-1] < vid_dur - 0.05:
        v_out.append(vid_dur)
        a_out.append(aud_dur)

    log(f"  {len(v_out)} control points after cleanup")
    return v_out, a_out, vid_dur, aud_dur


def warp_summary(v_ctrl, a_ctrl):
    """Return a human-readable list of speed factors per segment."""
    lines = []
    for i in range(len(v_ctrl) - 1):
        vo = v_ctrl[i+1] - v_ctrl[i]
        ao = a_ctrl[i+1] - a_ctrl[i]
        if vo < 0.02 or ao < 0.02:
            continue
        factor = vo / ao   # >1 = video is stretched (plays slower), <1 = compressed (faster)
        direction = "slower" if factor > 1.05 else ("faster" if factor < 0.95 else "same")
        lines.append(
            f"  {a_ctrl[i]:.1f}s-{a_ctrl[i+1]:.1f}s  {factor:.2f}x ({direction})"
        )
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg helpers
# ─────────────────────────────────────────────────────────────────────────────

def ffprobe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True
    )
    return float(json.loads(r.stdout)["format"]["duration"])


def extract_audio_wav(video_path, out_wav, log):
    log("Extracting audio to WAV for analysis ...")
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-ar", "44100", "-ac", "2", out_wav]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Audio extraction failed:\n{r.stderr[-1200:]}")


def build_warped_video(
    input_video,
    v_ctrl, a_ctrl,      # already include 0 and end bookends
    audio_src,
    output_path,
    max_speed,
    log, progress_cb
):
    """
    Apply piecewise-linear time warp to the video track.
    v_ctrl[i] in the video maps to a_ctrl[i] in output time.
    Audio from audio_src is muxed unchanged.
    """
    n_segs = len(v_ctrl) - 1
    log(f"Building {n_segs} time-warped segments ...")

    filter_parts = []
    seg_labels   = []
    speed_clamps = 0

    for i in range(n_segs):
        vs      = v_ctrl[i]
        ve      = v_ctrl[i + 1]
        orig    = ve - vs

        ao      = a_ctrl[i]
        ae      = a_ctrl[i + 1]
        target  = ae - ao

        if orig < 0.015 or target < 0.015:
            continue

        speed   = orig / target     # >1 = fast-forward, <1 = slow-mo
        clamped = max(1.0 / max_speed, min(max_speed, speed))
        if abs(clamped - speed) > 0.02:
            speed_clamps += 1
            target = orig / clamped

        pts_mult = target / orig    # PTS multiplier: <1 = speed up, >1 = slow down

        label = f"s{i}"
        filter_parts.append(
            f"[0:v]trim=start={vs:.6f}:end={ve:.6f},"
            f"setpts={pts_mult:.9f}*(PTS-STARTPTS)[{label}]"
        )
        seg_labels.append(f"[{label}]")

    if not seg_labels:
        raise RuntimeError("No valid segments produced.  Check that input has video frames.")

    if speed_clamps:
        log(f"  Note: {speed_clamps} segments clamped to max {max_speed:.1f}x speed factor.")

    filter_parts.append(
        f"{''.join(seg_labels)}concat=n={len(seg_labels)}:v=1:a=0[outv]"
    )
    filter_complex = ";".join(filter_parts)

    tmp_dir   = tempfile.mkdtemp(prefix="bsync_")
    tmp_video = os.path.join(tmp_dir, "warped_noaudio.mp4")

    try:
        # Step 1 -- encode warped video (no audio)
        log("Encoding warped video track ...")
        progress_cb(10)
        cmd1 = [
            "ffmpeg", "-y", "-i", input_video,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-an", tmp_video
        ]
        r1 = subprocess.run(cmd1, capture_output=True, text=True)
        if r1.returncode != 0:
            raise RuntimeError(f"ffmpeg encode failed:\n{r1.stderr[-2500:]}")
        progress_cb(75)

        # Step 2 -- mux warped video + original audio
        log("Muxing with original audio ...")
        cmd2 = [
            "ffmpeg", "-y",
            "-i", tmp_video,
            "-i", audio_src,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output_path
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed:\n{r2.stderr[-2500:]}")
        progress_cb(100)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    log(f"Done.  Output: {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────────────────────

BG    = "#1a1a2e"
BG2   = "#0d0d1a"
FG    = "#e0e0e0"
GOLD  = "#d4a017"
BLUE  = "#4fc3f7"
RED   = "#c41e3a"
MUTED = "#888888"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video-Audio Beat Sync")
        self.geometry("720x780")
        self.configure(bg=BG)
        self.resizable(True, True)

        self._v_ctrl       = []
        self._a_ctrl       = []
        self._audio_wav    = None   # for analysis (librosa needs WAV)
        self._audio_out    = None   # for final mux
        self._tmp_dir      = None

        self._setup_style()
        self._build_ui()

    # ── Styling ───────────────────────────────────────────────────────────────

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",               background=BG,  foreground=FG,  font=("Segoe UI", 10))
        s.configure("TFrame",          background=BG)
        s.configure("TLabel",          background=BG,  foreground=FG)
        s.configure("TLabelframe",     background=BG,  foreground=MUTED)
        s.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("TEntry",          fieldbackground=BG2, foreground=FG, insertcolor=FG)
        s.configure("TButton",         background="#2d2d4e", foreground=FG, borderwidth=0, padding=4)
        s.map("TButton",               background=[("active", "#3d3d5e")])
        s.configure("Run.TButton",     background=RED, foreground="white",
                    font=("Segoe UI", 11, "bold"), padding=6)
        s.map("Run.TButton",           background=[("active", "#a01528"), ("disabled", "#555")])
        s.configure("TScale",          background=BG, troughcolor=BG2, sliderlength=16)
        s.configure("TProgressbar",    troughcolor=BG2, background=GOLD)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        P = dict(padx=14, pady=5)

        # Header
        hdr = ttk.Frame(self)
        hdr.pack(fill="x", padx=14, pady=(14, 2))
        tk.Label(hdr, text="Video-Audio Beat Sync",
                 bg=BG, fg=GOLD, font=("Segoe UI", 17, "bold")).pack(side="left")
        tk.Label(hdr, text="  align visual changes to audio beats via DTW",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(side="left", pady=(4, 0))

        # Input
        fin = ttk.LabelFrame(self, text="  Input", padding=8)
        fin.pack(fill="x", **P)
        self._var_video = tk.StringVar()
        self._file_row(fin, "Video file:", self._var_video, 52, self._pick_video)
        self._var_alt_audio = tk.StringVar()
        self._file_row(fin, "Alt audio (optional):", self._var_alt_audio, 44, self._pick_audio,
                       note="leave blank to use the audio already in the video")

        # Output
        fout = ttk.LabelFrame(self, text="  Output", padding=8)
        fout.pack(fill="x", **P)
        self._var_output = tk.StringVar()
        self._file_row(fout, "Save as:", self._var_output, 52, self._pick_output)

        # Settings
        fset = ttk.LabelFrame(self, text="  Settings", padding=10)
        fset.pack(fill="x", **P)

        self._var_n = tk.IntVar(value=20)
        self._slider_row(fset, "DTW control points:", self._var_n, 8, 60,
                         lambda v: str(int(float(v))), int)

        self._var_max_speed = tk.DoubleVar(value=4.0)
        self._slider_row(fset, "Max speed factor:", self._var_max_speed, 1.2, 10.0,
                         lambda v: f"{float(v):.1f}x")

        tk.Label(fset,
            text="Control points: more = tighter sync but allows more aggressive warping.  "
                 "Max speed factor: clamps extreme segment stretches.",
            bg=BG, fg=MUTED, font=("Segoe UI", 8), wraplength=640, justify="left"
        ).pack(anchor="w", pady=(4, 0))

        # Buttons
        btn_row = ttk.Frame(self)
        btn_row.pack(pady=8)
        ttk.Button(btn_row, text="1.  Analyze + Compute Warp",
                   command=self._analyze).pack(side="left", padx=6)
        self._btn_run = ttk.Button(btn_row, text="2.  Encode Synced Video",
                                   command=self._run, style="Run.TButton")
        self._btn_run.pack(side="left", padx=6)
        self._btn_run.state(["disabled"])
        ttk.Button(btn_row, text="Open output folder",
                   command=self._open_folder).pack(side="left", padx=6)

        # Warp summary
        fwarp = ttk.LabelFrame(self, text="  Warp preview (video-time -> output-time, speed factor)", padding=8)
        fwarp.pack(fill="x", **P)
        self._lbl_warp = tk.Label(fwarp,
            text="-- run Analyze first --",
            bg=BG, fg=MUTED, font=("Courier", 9), justify="left", wraplength=660)
        self._lbl_warp.pack(anchor="w")

        # Progress
        self._progress = ttk.Progressbar(self, mode="determinate", length=500)
        self._progress.pack(pady=(8, 2))
        self._lbl_status = tk.Label(self, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self._lbl_status.pack()

        # Log
        log_frame = ttk.Frame(self, padding=(14, 0, 14, 12))
        log_frame.pack(fill="both", expand=True)
        self._log_box = tk.Text(log_frame, height=9, bg=BG2, fg="#cccccc",
                                font=("Courier New", 9), state="disabled",
                                relief="flat", insertbackground=FG)
        sb = ttk.Scrollbar(log_frame, command=self._log_box.yview)
        self._log_box.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_box.pack(fill="both", expand=True)

    def _file_row(self, parent, label, var, width, cmd, note=None):
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=22, anchor="e").pack(side="left")
        ttk.Entry(r, textvariable=var, width=width).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(r, text="Browse", command=cmd).pack(side="left")
        if note:
            tk.Label(r, text=f"  ({note})", bg=BG, fg=MUTED,
                     font=("Segoe UI", 8)).pack(side="left")

    def _slider_row(self, parent, label, var, lo, hi, fmt, cast=float):
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text=label, width=22, anchor="e").pack(side="left")
        lbl_val = tk.Label(r, text=fmt(var.get()), bg=BG, fg=GOLD,
                           font=("Segoe UI", 10), width=6)
        def _on_change(*_):
            try:
                lbl_val.config(text=fmt(var.get()))
                var.set(cast(var.get()))
            except Exception:
                pass
        var.trace_add("write", _on_change)
        ttk.Scale(r, variable=var, from_=lo, to=hi, orient="horizontal",
                  length=260).pack(side="left", padx=8)
        lbl_val.pack(side="left")

    # ── File pickers ──────────────────────────────────────────────────────────

    def _pick_video(self):
        p = filedialog.askopenfilename(
            title="Select video",
            filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv *.webm"), ("All", "*.*")]
        )
        if p:
            self._var_video.set(p)
            if not self._var_output.get():
                base, _ = os.path.splitext(p)
                self._var_output.set(base + "_synced.mp4")

    def _pick_audio(self):
        p = filedialog.askopenfilename(
            title="Select audio file (optional)",
            filetypes=[("Audio files", "*.mp3 *.wav *.aac *.m4a *.flac *.ogg"), ("All", "*.*")]
        )
        if p:
            self._var_alt_audio.set(p)

    def _pick_output(self):
        p = filedialog.asksaveasfilename(
            title="Save synced video as",
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")]
        )
        if p:
            self._var_output.set(p)

    def _open_folder(self):
        out = self._var_output.get()
        if out:
            folder = os.path.dirname(os.path.abspath(out))
            if os.path.isdir(folder):
                subprocess.Popen(["explorer", folder])

    # ── Logging / progress ────────────────────────────────────────────────────

    def log(self, msg):
        def _do():
            self._log_box.configure(state="normal")
            self._log_box.insert("end", msg + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        self.after(0, _do)

    def _status(self, msg):
        self.after(0, lambda: self._lbl_status.configure(text=msg))

    def _set_progress(self, pct):
        self.after(0, lambda: self._progress.configure(value=pct))

    # ── Validation / dep check ────────────────────────────────────────────────

    def _validate(self):
        if not self._var_video.get() or not os.path.isfile(self._var_video.get()):
            messagebox.showerror("Missing input", "Please select a valid video file.")
            return False
        if not self._var_output.get():
            messagebox.showerror("Missing output", "Please set an output file path.")
            return False
        return True

    def _check_deps(self):
        missing = []
        for imp, pkg in [("librosa", "librosa"), ("scipy", "scipy"), ("cv2", "opencv-python")]:
            try:
                __import__(imp)
            except ImportError:
                missing.append(pkg)
        if missing:
            raise RuntimeError(
                f"Missing Python packages: {', '.join(missing)}\n\n"
                f"Install with:  pip install {' '.join(missing)}"
            )
        if not shutil.which("ffmpeg"):
            raise RuntimeError(
                "ffmpeg not found on PATH.\n"
                "Download ffmpeg and add it to your system PATH."
            )

    # ── Analyze ───────────────────────────────────────────────────────────────

    def _analyze(self):
        if not self._validate():
            return
        self._btn_run.state(["disabled"])
        self._set_progress(0)
        threading.Thread(target=self._analyze_thread, daemon=True).start()

    def _analyze_thread(self):
        try:
            self._check_deps()

            video_path = self._var_video.get()
            n_ctrl     = int(self._var_n.get())

            # Clean up any previous temp dir
            if self._tmp_dir:
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = tempfile.mkdtemp(prefix="beatsync_")

            alt = self._var_alt_audio.get().strip()
            if alt and os.path.isfile(alt):
                self.log(f"Using alt audio: {os.path.basename(alt)}")
                self._audio_out = alt
                # Convert to WAV for librosa analysis
                self._audio_wav = os.path.join(self._tmp_dir, "analysis.wav")
                self._status("Converting alt audio to WAV ...")
                extract_audio_wav(alt, self._audio_wav, self.log)
            else:
                self._audio_wav = os.path.join(self._tmp_dir, "analysis.wav")
                self._audio_out = os.path.join(self._tmp_dir, "audio_out.aac")
                self._status("Extracting audio from video ...")
                extract_audio_wav(video_path, self._audio_wav, self.log)
                # Also extract native stream for lossless mux
                self.log("Preserving audio stream for output ...")
                r = subprocess.run(
                    ["ffmpeg", "-y", "-i", video_path, "-vn",
                     "-acodec", "aac", "-b:a", "192k", self._audio_out],
                    capture_output=True, text=True
                )
                if r.returncode != 0:
                    self._audio_out = self._audio_wav  # fallback

            self._set_progress(15)
            self._status("Computing DTW warp ...")
            self.log("")
            self.log("=== DTW Warp Analysis ===")

            v_ctrl, a_ctrl, vid_dur, aud_dur = compute_dtw_warp(
                video_path, self._audio_wav, n_ctrl, self.log
            )
            self._v_ctrl = v_ctrl
            self._a_ctrl = a_ctrl

            # Build warp summary text
            summary_lines = warp_summary(v_ctrl, a_ctrl)
            self.log("")
            self.log("Warp segments (output-time  speed):")
            for line in summary_lines[:20]:
                self.log(line)
            if len(summary_lines) > 20:
                self.log(f"  ... ({len(summary_lines) - 20} more segments)")

            # Show a compact version in the UI label
            compact = "  |  ".join(
                f"{a_ctrl[i]:.1f}s: {((v_ctrl[i+1]-v_ctrl[i])/(a_ctrl[i+1]-a_ctrl[i])):.2f}x"
                for i in range(min(len(v_ctrl)-1, 12))
                if (v_ctrl[i+1]-v_ctrl[i]) > 0.02 and (a_ctrl[i+1]-a_ctrl[i]) > 0.02
            )

            def _upd():
                self._lbl_warp.configure(text=compact or "computed")
                self._btn_run.state(["!disabled"])
                self._set_progress(100)
                self._status(
                    f"Ready to encode.  "
                    f"Video {vid_dur:.1f}s -> warped to match {aud_dur:.1f}s audio."
                )
            self.after(0, _upd)
            self.log("")
            self.log("Analysis done.  Press 'Encode Synced Video' to render.")

        except Exception as e:
            self.log(f"\nERROR: {e}")
            self.log(traceback.format_exc())
            self.after(0, lambda: self._status("Analysis failed -- see log"))
            self.after(0, lambda: messagebox.showerror(
                "Analysis Error", f"Analysis failed:\n\n{e}"
            ))

    # ── Encode ────────────────────────────────────────────────────────────────

    def _run(self):
        if not self._validate():
            return
        if not self._audio_out:
            messagebox.showerror("Not ready", "Run Analyze first (step 1).")
            return
        self._btn_run.state(["disabled"])
        self._set_progress(0)
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _run_thread(self):
        try:
            self._status("Encoding ...")
            self.log("")
            self.log("=== Encoding ===")

            build_warped_video(
                input_video = self._var_video.get(),
                v_ctrl      = self._v_ctrl,
                a_ctrl      = self._a_ctrl,
                audio_src   = self._audio_out,
                output_path = self._var_output.get(),
                max_speed   = float(self._var_max_speed.get()),
                log         = self.log,
                progress_cb = self._set_progress,
            )

            self.after(0, lambda: self._status(
                f"Done: {os.path.basename(self._var_output.get())}"
            ))
            self.after(0, lambda: messagebox.showinfo(
                "Sync Complete",
                f"Saved:\n{self._var_output.get()}"
            ))

        except Exception as e:
            self.log(f"\nERROR: {e}")
            self.log(traceback.format_exc())
            self.after(0, lambda: self._status("Encoding failed -- see log"))
            self.after(0, lambda: messagebox.showerror(
                "Encoding Error", f"Encoding failed:\n\n{e}"
            ))
        finally:
            self.after(0, lambda: self._btn_run.state(["!disabled"]))


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        import numpy
    except ImportError:
        print("numpy is required.  pip install numpy")
        sys.exit(1)
    App().mainloop()
