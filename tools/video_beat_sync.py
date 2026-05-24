#!/usr/bin/env python3
"""
video_beat_sync.py -- Squeeze and stretch video to match visual changes with audio peaks.

Audio stays untouched.  Only the video timeline is warped via ffmpeg setpts.

Dependencies:
    pip install librosa scipy opencv-python
    ffmpeg must be on PATH

Usage:
    python tools/video_beat_sync.py
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
# Audio analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyze_audio(audio_path, n_events, sensitivity, log):
    import librosa
    from scipy.signal import find_peaks

    log("Loading audio ...")
    y, sr = librosa.load(audio_path, sr=None, mono=True)
    duration = float(len(y)) / sr
    log(f"  {duration:.2f}s  @ {sr} Hz")

    hop = 512

    # Beat tracking
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    log(f"  Beats: {len(beat_times)} @ {float(tempo):.1f} BPM")

    # Onset strength + detection
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=hop)
    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop).tolist()

    # RMS energy peaks
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    rms_thresh = float(np.percentile(rms, 40 + sensitivity * 45))
    min_dist_rms = max(1, int(sr * 0.3 / hop))
    rms_peaks, _ = find_peaks(rms, height=rms_thresh, distance=min_dist_rms)
    rms_peak_times = rms_times[rms_peaks].tolist()

    # Spectral novelty (half-wave rectified flux)
    spec_flux = np.maximum(np.diff(onset_env, prepend=onset_env[0]), 0)
    sf_thresh = float(np.percentile(spec_flux, 55 + sensitivity * 35))
    sf_peaks, _ = find_peaks(spec_flux, height=sf_thresh, distance=min_dist_rms)
    sf_times = librosa.frames_to_time(sf_peaks, sr=sr, hop_length=hop).tolist()

    all_t = np.sort(np.unique(np.array(
        beat_times + onset_times + rms_peak_times + sf_times, dtype=float
    )))
    # Keep away from edges
    all_t = all_t[(all_t > 0.15) & (all_t < duration - 0.15)]

    # Score each candidate by local RMS energy so we can pick the best ones
    clustered = _cluster(all_t, gap=0.25)

    def rms_at(t):
        idx = int(np.searchsorted(rms_times, t))
        idx = min(idx, len(rms) - 1)
        return float(rms[idx])

    if len(clustered) <= n_events:
        selected = clustered
    else:
        scores = np.array([rms_at(t) for t in clustered])
        top_idx = np.sort(np.argsort(scores)[-n_events:])
        selected = [clustered[i] for i in top_idx]

    log(f"  Audio events selected: {len(selected)}")
    return selected, duration


# ─────────────────────────────────────────────────────────────────────────────
# Video analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyze_video(video_path, n_events, sensitivity, log):
    import cv2
    from scipy.signal import find_peaks

    log("Scanning video frames ...")
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = float(total) / fps
    log(f"  {duration:.2f}s  @ {fps:.2f} fps  ({total} frames)")

    # Sample at most ~12 fps for speed; every skip-th frame
    skip = max(1, int(round(fps / 12)))
    diffs = []
    times = []
    prev = None
    idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % skip == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, (160, 90))
            if prev is not None:
                d = float(np.mean(np.abs(
                    gray.astype(np.float32) - prev.astype(np.float32)
                )))
                diffs.append(d)
                times.append(float(idx) / fps)
            prev = gray
        idx += 1

    cap.release()

    if not diffs:
        log("  No frames read from video.")
        return [], duration

    diffs = np.array(diffs)
    times = np.array(times)

    sample_fps = fps / skip
    thresh = float(np.percentile(diffs, 40 + sensitivity * 45))
    min_dist = max(1, int(sample_fps * 0.35))
    peaks, _ = find_peaks(diffs, height=thresh, distance=min_dist)

    ev_times = times[peaks]
    ev_scores = diffs[peaks]

    # Strip edges
    mask = (ev_times > 0.15) & (ev_times < duration - 0.15)
    ev_times = ev_times[mask]
    ev_scores = ev_scores[mask]

    if len(ev_times) > n_events:
        top_idx = np.sort(np.argsort(ev_scores)[-n_events:])
        ev_times = ev_times[top_idx]

    log(f"  Video events selected: {len(ev_times)}")
    return list(ev_times), duration


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cluster(times, gap=0.25):
    """Merge timestamps within `gap` seconds of each other."""
    if len(times) == 0:
        return []
    result = []
    group = [float(times[0])]
    for t in times[1:]:
        if float(t) - group[-1] < gap:
            group.append(float(t))
        else:
            result.append(float(np.mean(group)))
            group = [float(t)]
    result.append(float(np.mean(group)))
    return result


def ffprobe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True
    )
    return float(json.loads(r.stdout)["format"]["duration"])


def extract_audio_wav(video_path, out_wav, log):
    log("Extracting audio to WAV ...")
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn",
           "-ar", "44100", "-ac", "2", out_wav]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Audio extraction failed:\n{r.stderr[-1200:]}")


# ─────────────────────────────────────────────────────────────────────────────
# Core warp engine
# ─────────────────────────────────────────────────────────────────────────────

def build_warped_video(
    input_video, v_events, a_events,
    audio_src,          # path to audio that goes in the output
    output_path,
    max_speed,
    log, progress_cb
):
    """
    Warp the video track so each v_events[i] timestamp lands at a_events[i].
    Audio from audio_src is muxed in unchanged.
    """
    vid_dur = ffprobe_duration(input_video)
    aud_dur = ffprobe_duration(audio_src)
    log(f"Video duration: {vid_dur:.3f}s   Audio duration: {aud_dur:.3f}s")

    # Build aligned control-point lists with 0 and end bookends
    v_pts = [0.0] + sorted(float(t) for t in v_events) + [vid_dur]
    a_pts = [0.0] + sorted(float(t) for t in a_events) + [aud_dur]

    # Must be same length -- trim to shorter
    n = min(len(v_pts), len(a_pts))
    v_pts = v_pts[:n]
    a_pts = a_pts[:n]

    n_segs = n - 1
    log(f"Building {n_segs} time-warped segments ...")

    filter_parts = []
    seg_labels = []
    speed_report = []

    for i in range(n_segs):
        vs = v_pts[i]
        ve = v_pts[i + 1]
        at = a_pts[i]
        ae = a_pts[i + 1]

        orig_dur = ve - vs
        tgt_dur = ae - at

        if orig_dur < 0.02 or tgt_dur < 0.02:
            continue

        # Clamp extreme speed changes to max_speed factor
        speed = orig_dur / tgt_dur          # >1 = fast-forward, <1 = slow-mo
        clamped = max(1.0 / max_speed, min(max_speed, speed))
        if abs(clamped - speed) > 0.01:
            # Adjust ve so the segment ends at the clamped rate
            tgt_dur = orig_dur / clamped
            ae = at + tgt_dur
            speed_report.append(
                f"  seg {i}: {speed:.2f}x -> clamped to {clamped:.2f}x"
            )

        pts_mult = tgt_dur / orig_dur       # PTS multiplier for setpts

        label = f"s{i}"
        filter_parts.append(
            f"[0:v]trim=start={vs:.6f}:end={ve:.6f},"
            f"setpts={pts_mult:.8f}*(PTS-STARTPTS)[{label}]"
        )
        seg_labels.append(f"[{label}]")

    if not seg_labels:
        raise RuntimeError("No valid segments produced -- check event detection.")

    if speed_report:
        log("Speed clamping applied:")
        for line in speed_report:
            log(line)

    n_valid = len(seg_labels)
    filter_parts.append(
        f"{''.join(seg_labels)}concat=n={n_valid}:v=1:a=0[outv]"
    )
    filter_complex = ";".join(filter_parts)

    tmp_dir = tempfile.mkdtemp(prefix="bsync_mux_")
    tmp_video = os.path.join(tmp_dir, "warped_noaudio.mp4")

    try:
        # Step 1: Encode warped video (no audio)
        log("Encoding warped video ...")
        progress_cb(10)
        cmd1 = [
            "ffmpeg", "-y", "-i", input_video,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-an",
            tmp_video
        ]
        r1 = subprocess.run(cmd1, capture_output=True, text=True)
        if r1.returncode != 0:
            raise RuntimeError(f"ffmpeg encode failed:\n{r1.stderr[-2000:]}")
        progress_cb(75)

        # Step 2: Mux warped video + original audio
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
            raise RuntimeError(f"ffmpeg mux failed:\n{r2.stderr[-2000:]}")
        progress_cb(100)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    log(f"Done. Output: {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────────────────────

BG     = "#1a1a2e"
BG2    = "#0d0d1a"
FG     = "#e0e0e0"
GOLD   = "#d4a017"
BLUE   = "#4fc3f7"
RED    = "#c41e3a"
MUTED  = "#888888"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video-Audio Beat Sync")
        self.geometry("700x760")
        self.configure(bg=BG)
        self.resizable(True, True)

        self._audio_events  = []
        self._video_events  = []
        self._audio_wav     = None   # path to extracted WAV (for analysis)
        self._audio_for_out = None   # path to audio to mux into output
        self._tmp_dir       = None

        self._setup_style()
        self._build_ui()

    # ── Style ─────────────────────────────────────────────────────────────

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",           background=BG,  foreground=FG,   font=("Segoe UI", 10))
        s.configure("TFrame",      background=BG)
        s.configure("TLabel",      background=BG,  foreground=FG)
        s.configure("TLabelframe", background=BG,  foreground=MUTED)
        s.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("TEntry",      fieldbackground=BG2, foreground=FG,  insertcolor=FG)
        s.configure("TButton",     background="#2d2d4e", foreground=FG, borderwidth=0, padding=4)
        s.map("TButton",           background=[("active", "#3d3d5e")])
        s.configure("Run.TButton", background=RED,  foreground="white",
                    font=("Segoe UI", 11, "bold"), padding=6)
        s.map("Run.TButton",       background=[("active", "#a01528"), ("disabled", "#555")])
        s.configure("TScale",      background=BG, troughcolor=BG2, sliderlength=16)
        s.configure("TProgressbar", troughcolor=BG2, background=GOLD)

    # ── Layout ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        P = dict(padx=14, pady=5)

        # Title
        hdr = ttk.Frame(self)
        hdr.pack(fill="x", padx=14, pady=(14, 2))
        tk.Label(hdr, text="Video-Audio Beat Sync",
                 bg=BG, fg=GOLD, font=("Segoe UI", 17, "bold")).pack(side="left")
        tk.Label(hdr, text="  squeeze & stretch video to match audio peaks",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(side="left", pady=(4, 0))

        # ── Input ──
        fin = ttk.LabelFrame(self, text="  Input", padding=8)
        fin.pack(fill="x", **P)

        self._var_video = tk.StringVar()
        self._row(fin, "Video file:", self._var_video, 52, self._pick_video)

        self._var_alt_audio = tk.StringVar()
        self._row(fin, "Alt audio (optional):", self._var_alt_audio, 44, self._pick_audio,
                  note="leave blank to use audio embedded in the video")

        # ── Output ──
        fout = ttk.LabelFrame(self, text="  Output", padding=8)
        fout.pack(fill="x", **P)

        self._var_output = tk.StringVar()
        self._row(fout, "Save as:", self._var_output, 52, self._pick_output)

        # ── Settings ──
        fset = ttk.LabelFrame(self, text="  Settings", padding=10)
        fset.pack(fill="x", **P)

        self._var_n = tk.IntVar(value=10)
        self._slider_row(fset, "Sync points:", self._var_n, 3, 30,
                         lambda v: f"{int(float(v))}", int)

        self._var_sens = tk.DoubleVar(value=0.55)
        self._slider_row(fset, "Sensitivity:", self._var_sens, 0.0, 1.0,
                         lambda v: f"{float(v):.2f}")

        self._var_max_speed = tk.DoubleVar(value=4.0)
        self._slider_row(fset, "Max speed factor:", self._var_max_speed, 1.2, 10.0,
                         lambda v: f"{float(v):.1f}x")

        note = tk.Label(fset,
            text="Higher sync points = tighter alignment  |  "
                 "Max speed factor clamps extreme stretches  |  "
                 "Sensitivity controls event detection threshold",
            bg=BG, fg=MUTED, font=("Segoe UI", 8), wraplength=620, justify="left")
        note.pack(anchor="w", pady=(4, 0))

        # ── Action buttons ──
        btn_row = ttk.Frame(self)
        btn_row.pack(pady=8)

        ttk.Button(btn_row, text="1.  Analyze",
                   command=self._analyze).pack(side="left", padx=6)

        self._btn_run = ttk.Button(btn_row, text="2.  Sync Video",
                                   command=self._run, style="Run.TButton")
        self._btn_run.pack(side="left", padx=6)
        self._btn_run.state(["disabled"])

        ttk.Button(btn_row, text="Open output folder",
                   command=self._open_output_folder).pack(side="left", padx=6)

        # ── Detected events ──
        fev = ttk.LabelFrame(self, text="  Detected sync points", padding=8)
        fev.pack(fill="x", **P)

        ev_cols = ttk.Frame(fev)
        ev_cols.pack(fill="x")

        ca = ttk.Frame(ev_cols)
        ca.pack(side="left", fill="both", expand=True)
        tk.Label(ca, text="Audio moments", bg=BG, fg=GOLD,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self._lbl_a = tk.Label(ca, text="-- run Analyze first --",
                               bg=BG, fg=MUTED, font=("Courier", 9),
                               justify="left", wraplength=300)
        self._lbl_a.pack(anchor="w")

        cv = ttk.Frame(ev_cols)
        cv.pack(side="left", fill="both", expand=True)
        tk.Label(cv, text="Video moments", bg=BG, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self._lbl_v = tk.Label(cv, text="-- run Analyze first --",
                               bg=BG, fg=MUTED, font=("Courier", 9),
                               justify="left", wraplength=300)
        self._lbl_v.pack(anchor="w")

        # ── Progress ──
        self._progress = ttk.Progressbar(self, mode="determinate", length=480)
        self._progress.pack(pady=(8, 2))
        self._lbl_status = tk.Label(self, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self._lbl_status.pack()

        # ── Log ──
        log_frame = ttk.Frame(self, padding=(14, 0, 14, 12))
        log_frame.pack(fill="both", expand=True)
        self._log_box = tk.Text(log_frame, height=9, bg=BG2, fg="#cccccc",
                                font=("Courier New", 9), state="disabled",
                                relief="flat", insertbackground=FG)
        sb = ttk.Scrollbar(log_frame, command=self._log_box.yview)
        self._log_box.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_box.pack(fill="both", expand=True)

    def _row(self, parent, label, var, width, cmd, note=None):
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=20, anchor="e").pack(side="left")
        ttk.Entry(r, textvariable=var, width=width).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(r, text="Browse", command=cmd).pack(side="left")
        if note:
            tk.Label(r, text=f"  ({note})", bg=BG, fg=MUTED,
                     font=("Segoe UI", 8)).pack(side="left")

    def _slider_row(self, parent, label, var, lo, hi, fmt, cast=float):
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text=label, width=20, anchor="e").pack(side="left")
        lbl_val = tk.Label(r, text=fmt(var.get()), bg=BG, fg=GOLD,
                           font=("Segoe UI", 10), width=6)
        def on_change(*_):
            lbl_val.config(text=fmt(var.get()))
            var.set(cast(var.get()))
        var.trace_add("write", on_change)
        ttk.Scale(r, variable=var, from_=lo, to=hi, orient="horizontal",
                  length=240, command=lambda v: on_change()).pack(side="left", padx=8)
        lbl_val.pack(side="left")

    # ── File pickers ──────────────────────────────────────────────────────

    def _pick_video(self):
        p = filedialog.askopenfilename(
            title="Select video file",
            filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv *.webm"), ("All files", "*.*")]
        )
        if p:
            self._var_video.set(p)
            if not self._var_output.get():
                base, _ = os.path.splitext(p)
                self._var_output.set(base + "_synced.mp4")

    def _pick_audio(self):
        p = filedialog.askopenfilename(
            title="Select audio file (optional)",
            filetypes=[("Audio files", "*.mp3 *.wav *.aac *.m4a *.flac *.ogg"),
                       ("All files", "*.*")]
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

    def _open_output_folder(self):
        out = self._var_output.get()
        if out:
            folder = os.path.dirname(os.path.abspath(out))
            if os.path.isdir(folder):
                subprocess.Popen(["explorer", folder])

    # ── Logging / progress ────────────────────────────────────────────────

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

    # ── Validation ────────────────────────────────────────────────────────

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
        for pkg, imp in [("librosa", "librosa"), ("scipy", "scipy"), ("cv2", "opencv-python")]:
            try:
                __import__(imp)
            except ImportError:
                missing.append(pkg if pkg != "cv2" else "opencv-python")
        if missing:
            raise RuntimeError(
                f"Missing Python packages: {', '.join(missing)}\n\n"
                f"Install with:\n  pip install {' '.join(missing)}"
            )
        if not shutil.which("ffmpeg"):
            raise RuntimeError(
                "ffmpeg not found on PATH.\n"
                "Download from https://ffmpeg.org/download.html and add to PATH."
            )

    # ── Analyze ───────────────────────────────────────────────────────────

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
            n = int(self._var_n.get())
            sens = float(self._var_sens.get())

            # Clean up previous tmp dir
            if self._tmp_dir:
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = tempfile.mkdtemp(prefix="beatsync_")

            alt = self._var_alt_audio.get().strip()
            if alt and os.path.isfile(alt):
                # Use alt audio as-is for output; convert to WAV for analysis
                self._audio_for_out = alt
                self._audio_wav = os.path.join(self._tmp_dir, "analysis.wav")
                self.log(f"Alt audio: {os.path.basename(alt)}")
                self._status("Converting alt audio to WAV ...")
                extract_audio_wav(alt, self._audio_wav, self.log)
            else:
                # Extract from video for both analysis and output
                self._audio_wav     = os.path.join(self._tmp_dir, "analysis.wav")
                self._audio_for_out = os.path.join(self._tmp_dir, "audio_out.aac")
                self._status("Extracting audio from video ...")
                extract_audio_wav(video_path, self._audio_wav, self.log)
                # Also extract native audio stream for lossless mux
                self.log("Extracting audio stream for output ...")
                r = subprocess.run(
                    ["ffmpeg", "-y", "-i", video_path, "-vn",
                     "-acodec", "aac", "-b:a", "192k", self._audio_for_out],
                    capture_output=True, text=True
                )
                if r.returncode != 0:
                    # Fallback: use the WAV
                    self._audio_for_out = self._audio_wav

            self._set_progress(20)
            self._status("Analyzing audio ...")
            self.log("")
            self.log("=== Audio Analysis ===")
            self._audio_events, _ = analyze_audio(self._audio_wav, n, sens, self.log)

            self._set_progress(50)
            self._status("Analyzing video ...")
            self.log("")
            self.log("=== Video Analysis ===")
            self._video_events, _ = analyze_video(video_path, n, sens, self.log)

            self._set_progress(80)

            def _update_ui():
                a_str = "  ".join(f"{t:.2f}s" for t in self._audio_events) or "none found"
                v_str = "  ".join(f"{t:.2f}s" for t in self._video_events) or "none found"
                self._lbl_a.configure(text=a_str)
                self._lbl_v.configure(text=v_str)
                self._btn_run.state(["!disabled"])
                self._set_progress(100)
                self._status(
                    f"Ready: {len(self._audio_events)} audio + "
                    f"{len(self._video_events)} video events found"
                )

            self.after(0, _update_ui)
            self.log("")
            self.log(f"Analysis complete. Press 'Sync Video' to encode.")

        except Exception as e:
            self.log(f"\nERROR: {e}")
            self.log(traceback.format_exc())
            self.after(0, lambda: self._status("Analysis failed -- see log"))
            self.after(0, lambda: messagebox.showerror(
                "Analysis Error", f"Analysis failed:\n\n{e}"
            ))

    # ── Run ───────────────────────────────────────────────────────────────

    def _run(self):
        if not self._validate():
            return
        if not self._audio_for_out:
            messagebox.showerror("Not analyzed", "Run Analysis first (step 1).")
            return
        self._btn_run.state(["disabled"])
        self._set_progress(0)
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _run_thread(self):
        try:
            self._status("Encoding warped video ...")
            self.log("")
            self.log("=== Encoding ===")

            build_warped_video(
                input_video    = self._var_video.get(),
                v_events       = self._video_events,
                a_events       = self._audio_events,
                audio_src      = self._audio_for_out,
                output_path    = self._var_output.get(),
                max_speed      = float(self._var_max_speed.get()),
                log            = self.log,
                progress_cb    = self._set_progress,
            )

            self.after(0, lambda: self._status(
                f"Done: {os.path.basename(self._var_output.get())}"
            ))
            self.after(0, lambda: messagebox.showinfo(
                "Sync Complete",
                f"Saved to:\n{self._var_output.get()}"
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
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _check_basic_deps():
    """Give a friendly error before the GUI even starts."""
    missing = []
    for imp, pkg in [("numpy", "numpy"), ("librosa", "librosa"),
                     ("scipy", "scipy")]:
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Missing packages: {', '.join(missing)}")
        print(f"Install with:  pip install {' '.join(missing)}")
        sys.exit(1)
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH. Please install ffmpeg.")
        sys.exit(1)


if __name__ == "__main__":
    # Light dep check -- heavy imports happen inside threads
    try:
        import numpy
    except ImportError:
        print("numpy is required.  pip install numpy")
        sys.exit(1)

    app = App()
    app.mainloop()
