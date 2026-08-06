"""Subprocess wrapper around the RATIFIED engine (engine/chain.py) -- the
DCMVS continuous-performance music-video generator Andrew ratified 2026-08-05
(recipe v3). See REVIEW_FINDINGS_2026-08-05.md ARCHITECTURE section: chain.py
is CLI-only, lives in its own separate git repo (dcmvs-lipsync) deployed
alongside this app at <app_root>/engine, and the ratified decision is to WRAP
IT AS A SUBPROCESS rather than port pieces into the older, buggier
features/song_video/pipeline.py. Do not import chain.py; do not edit anything
under engine/ (out of scope for this app, a separate repo).

Why parsing stdout text instead of chain_video()'s on_progress callback:
chain_video() (chain.py ~2035) accepts on_progress/should_stop/log callables
for IN-PROCESS callers, but main() -- the CLI entry point we actually spawn
-- always calls it with log=print and no on_progress (chain.py ~2798). A
subprocess caller only ever sees stdout text, so this module's progress
parser matches the literal strings chain.py prints:
    "=== clip {i+1}/{n_clips} [{KIND}]  (t=...) ==="   (chain.py ~2350)
    "     seed {sd}: score=..." / "...judge=..."        (chain.py ~1725,1741)
    "  -> {clip_path.name}  ({elapsed}s)"                (chain.py ~2390)
    "ERR: {e}"                                           (chain.py ~2801, on failure)

Ratified defaults (RECIPE.json "production" + review/render_v16_detached.ps1,
the last command Andrew human-approved, 2026-08-05 v16 close-of-night):
    --frames-per-clip 241  --crossfade 0.15  --min-clip-frames 169
    --smart-seams  --judge-select  --seeds-per-clip 4
frames_per_clip/crossfade are read from RECIPE.json via features.song_video.
recipe.load() (falls back to the literal ratified values if the recipe file
is missing/invalid -- this module must still be able to start a job even if
recipe.py's stricter validation trips on an unrelated section). min_clip_frames/
smart_seams/seeds_per_clip are not RECIPE.json fields (RECIPE.json's own
"dcmvs" section says chain.py still carries its own constants un-wired) so
they are hardcoded here to match the ratified command verbatim -- change them
only together with a RECIPE.json + LIPSYNC_LEDGER.md update, per that file's
own rule.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_DIR = APP_ROOT / "engine"
CHAIN_PY = ENGINE_DIR / "chain.py"
UPLOADS_DIR = APP_ROOT / "uploads"
OUTPUT_DIR = APP_ROOT / "output" / "chain"

# -- Ratified defaults (see module docstring for provenance) ------------------

_FALLBACK_FRAMES_PER_CLIP = 241
_FALLBACK_CROSSFADE = 0.15
RATIFIED_MIN_CLIP_FRAMES = 169     # render_v16_detached.ps1:10 (paired with --smart-seams)
RATIFIED_SMART_SEAMS = True        # render_v16_detached.ps1:10
RATIFIED_SEEDS_PER_CLIP = 4        # render_v16_detached.ps1:10


def _recipe_defaults() -> dict:
    """Best-effort load of RECIPE.json's ratified frames_per_clip/crossfade.
    Falls back to the literal ratified-command values on any error -- a
    stricter/unrelated recipe.py validation failure must not block starting
    a render."""
    try:
        from features.song_video import recipe as recipe_mod
        r = recipe_mod.load()
        prod = r.get("production", {})
        return {
            "frames_per_clip": int(prod.get("frames_per_clip", _FALLBACK_FRAMES_PER_CLIP)),
            "crossfade": float(prod.get("crossfade_s", _FALLBACK_CROSSFADE)),
        }
    except Exception as e:
        log.warning("[chain_runner] RECIPE.json load failed (%s) -- using literal "
                    "ratified defaults (frames_per_clip=%d crossfade=%.2f)",
                    e, _FALLBACK_FRAMES_PER_CLIP, _FALLBACK_CROSSFADE)
        return {"frames_per_clip": _FALLBACK_FRAMES_PER_CLIP, "crossfade": _FALLBACK_CROSSFADE}


def resolve_engine() -> Path:
    """Return the path to engine/chain.py, relative to the app root. Raises
    FileNotFoundError with a clear message if the engine repo isn't present
    (e.g. a dev worktree that deliberately excludes it -- see this module's
    docstring)."""
    if not CHAIN_PY.is_file():
        raise FileNotFoundError(
            f"engine/chain.py not found at {CHAIN_PY} -- the engine repo "
            f"(dcmvs-lipsync) must be checked out at <app_root>/engine "
            f"alongside app.py. Not present in this tree."
        )
    return CHAIN_PY


def _wangp_worker_url() -> str:
    """The V2 WanGP worker's own port -- NEVER hardcode this (landmine: V1 is
    7899, V2 is 7897, and the two must stay disjoint so V2 can never touch
    V1's worker). chain.py's own --worker default is 7899 (v1) -- always pass
    this explicitly, never rely on chain.py's default."""
    from services.manager import WANGP_WORKER_PORT
    return f"http://127.0.0.1:{WANGP_WORKER_PORT}"


# -- Progress line parsing -----------------------------------------------------

_RE_CLIP_HEADER = re.compile(r"===\s*clip\s+(\d+)/(\d+)\s*\[(\w+)\]")
_RE_SEED_LINE = re.compile(r"\bseed\s+(-?\d+):\s*(score|judge)=")
_RE_CLIP_DONE = re.compile(r"->\s*(\S.*?)\s+\(([\d.]+)s\)\s*$")
_RE_ERR = re.compile(r"^ERR:\s*(.+)$")

# Fixed-percentage phase markers, mirroring chain.py's own _prog() calls
# (chain.py ~2451-2483) so our estimate matches the engine's own math.
_PHASE_MARKERS = [
    ("Concatenating", "concat", 92),
    ("Muxing original song", "mux", 96),
    ("Stabilizing", "stabilize", 98),
    ("Upscaling to HD", "upscale", 99),
]


class ChainJobState:
    """In-memory state for the single currently-running (or last-run) chain
    job. Deliberately NOT wired into core/job_manager.py's queue -- chain.py
    talks to the WanGP worker directly over HTTP, bypassing this app's GPU
    job queue entirely, so it does not belong in GPU_JOB_TYPES. This means
    chain jobs do NOT show up in the Queue tab; the Music Video tab's own
    progress panel is the only place to watch one. (Flagged in
    VALIDATION_NEEDED.md as worth reconsidering once this path is proven.)"""

    def __init__(self):
        self.lock = threading.RLock()
        self._reset()

    def _reset(self):
        self.id: Optional[str] = None
        self.status: str = "idle"          # idle | starting | running | done | error | cancelled
        self.phase: str = ""
        self.pct: int = 0
        self.message: str = ""
        self.clip_i: Optional[int] = None
        self.clip_n: Optional[int] = None
        self.clip_kind: Optional[str] = None
        self.take_i: int = 0
        self.take_n: Optional[int] = None
        self.log_tail: deque = deque(maxlen=400)
        self.created_at: Optional[float] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.output_path: Optional[str] = None
        self.error: Optional[str] = None
        self.cmd: Optional[list] = None
        self.proc: Optional[subprocess.Popen] = None
        self.cancel_requested: bool = False

    def to_dict(self) -> dict:
        with self.lock:
            elapsed = None
            if self.started_at is not None:
                end = self.finished_at if self.finished_at is not None else time.time()
                elapsed = max(0.0, end - self.started_at)
            return {
                "id": self.id,
                "status": self.status,
                "phase": self.phase,
                "pct": self.pct,
                "message": self.message,
                "clip_i": self.clip_i,
                "clip_n": self.clip_n,
                "clip_kind": self.clip_kind,
                "take_i": self.take_i,
                "take_n": self.take_n,
                "log_tail": list(self.log_tail),
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_seconds": elapsed,
                "output_path": self.output_path,
                "error": self.error,
                "cmd": self.cmd,
            }

    def is_running(self) -> bool:
        with self.lock:
            return self.status in ("starting", "running")

    def _append_line(self, line: str):
        line = line.rstrip("\n")
        if not line:
            return
        with self.lock:
            self.log_tail.append(line)
            self.message = line[:300]

            m = _RE_CLIP_HEADER.search(line)
            if m:
                self.clip_i = int(m.group(1))
                self.clip_n = int(m.group(2))
                self.clip_kind = m.group(3)
                self.take_i = 0
                self.phase = f"clip {self.clip_i}/{self.clip_n} [{self.clip_kind}]"
                # Mirrors chain.py's own progress math exactly (chain.py ~2353):
                # pct = 5 + int(85 * (i / max(1, n_clips))), i = clip_i - 1 (0-based).
                self.pct = 5 + int(85 * ((self.clip_i - 1) / max(1, self.clip_n)))
                return

            if _RE_SEED_LINE.search(line):
                self.take_i += 1
                return

            if _RE_CLIP_DONE.search(line):
                return

            for needle, phase, pct in _PHASE_MARKERS:
                if needle in line:
                    self.phase = phase
                    self.pct = pct
                    return

            m = _RE_ERR.match(line)
            if m:
                self.error = m.group(1)
                return


# Module-level singleton -- one chain job at a time (mirrors the one-GPU
# reality; chain.py itself is not designed for concurrent invocation against
# the same worker).
_state = ChainJobState()


def get_status() -> dict:
    return _state.to_dict()


def is_running() -> bool:
    return _state.is_running()


# -- Song resolution (reuses features/song_video/routes.py:193-220's ACE-Step
# no-song fallback logic verbatim, so "generate" behaves identically to the
# legacy pipeline's no-song path) --------------------------------------------

def _resolve_song_sync(song_path: Optional[str], *, target_length: float,
                        lyrics_text: str, music_prompt: str) -> str:
    """Blocking. Call from the background thread only (not the event loop)."""
    if song_path:
        return song_path

    from core import config as cfg
    from features.fun_videos import audio_generator
    from core.gpu_orchestrator import gpu, GPUBusyError

    config = cfg.load()
    target_length = max(5.0, min(float(target_length or 30),
                                  float(audio_generator.MAX_DURATION)))
    lyrics_text = (lyrics_text or "").strip()
    music_prompt = (music_prompt or "").strip() or "music video, energetic, bold"

    log.info("[chain_runner] No song provided -- generating via ACE-Step (%.0fs target)",
             target_length)
    try:
        gpu.acquire("acestep", reason="chain.py ratified engine -- no-song fallback")
    except GPUBusyError as e:
        raise RuntimeError(str(e)) from e

    gen_path, gen_err = audio_generator.generate_audio(
        music_prompt,
        duration=target_length,
        output_dir=str(UPLOADS_DIR),
        bpm=None,
        steps=int(config.get("fun_audio_steps", 27)),
        guidance=float(config.get("fun_audio_guidance", 7.0)),
        lyrics=lyrics_text,
        instrumental=not bool(lyrics_text),
    )
    if not gen_path:
        raise RuntimeError(f"No song provided and ACE-Step generation failed: {gen_err}")
    return gen_path


# -- Command building -----------------------------------------------------------

def build_command(*, image_paths: list[str], song_path: str, output_path: str,
                   scene_prompts: Optional[list[str]] = None,
                   seeds_per_clip: int = RATIFIED_SEEDS_PER_CLIP,
                   judge_select: bool = True,
                   frames_per_clip: Optional[int] = None,
                   crossfade: Optional[float] = None,
                   min_clip_frames: int = RATIFIED_MIN_CLIP_FRAMES,
                   smart_seams: bool = RATIFIED_SMART_SEAMS) -> list[str]:
    chain_py = resolve_engine()
    defaults = _recipe_defaults()
    fpc = int(frames_per_clip if frames_per_clip is not None else defaults["frames_per_clip"])
    xfade = float(crossfade if crossfade is not None else defaults["crossfade"])

    # -u: unbuffered stdout -- chain.py's main() calls chain_video(log=print);
    # print() block-buffers when stdout is a pipe (not a TTY), so without -u
    # our progress parser would only see output in large delayed chunks
    # instead of near-real-time lines. VALIDATION_NEEDED: confirm live.
    cmd = [
        sys.executable, "-u", str(chain_py),
        "--image", image_paths[0],
        "--song", song_path,
        "--output", output_path,
        "--worker", _wangp_worker_url(),
        "--seeds-per-clip", str(int(seeds_per_clip)),
        "--frames-per-clip", str(fpc),
        "--crossfade", str(xfade),
        "--min-clip-frames", str(int(min_clip_frames)),
    ]
    if smart_seams:
        cmd.append("--smart-seams")
    if judge_select:
        cmd.append("--judge-select")
    if len(image_paths) > 1:
        cmd += ["--images", ",".join(image_paths)]
    if scene_prompts:
        cmd += ["--scene-prompts", "||".join(scene_prompts)]
    return cmd


# -- Job lifecycle --------------------------------------------------------------

def start(*, images: list[str], song_path: Optional[str] = None,
          scene_prompts: Optional[list[str]] = None,
          seeds_per_clip: int = RATIFIED_SEEDS_PER_CLIP,
          judge_select: bool = True,
          target_length: float = 30.0,
          lyrics_text: str = "",
          music_prompt: str = "",
          output_path: Optional[str] = None) -> dict:
    """Start a chain.py render in a background thread. Returns the initial
    status dict immediately (async-friendly: does not block the caller on
    the ACE-Step fallback or the render itself). Raises RuntimeError if a
    job is already running or the engine isn't present."""
    resolve_engine()  # fail fast with a clear error before touching state

    with _state.lock:
        if _state.is_running():
            raise RuntimeError("A ratified-engine render is already running")
        _state._reset()
        _state.id = uuid.uuid4().hex[:12]
        _state.status = "starting"
        _state.phase = "queued"
        _state.created_at = time.time()
        _state.cancel_requested = False
        _state.take_n = int(seeds_per_clip)  # known up front; take_i counts up as seed lines arrive

    if not output_path:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(OUTPUT_DIR / f"chain_{_state.id}.mp4")

    thread = threading.Thread(
        target=_run_job_thread,
        args=(_state.id, images, song_path, scene_prompts, seeds_per_clip,
              judge_select, target_length, lyrics_text, music_prompt, output_path),
        daemon=True, name=f"chain-runner-{_state.id}",
    )
    thread.start()
    return _state.to_dict()


def _run_job_thread(job_id, images, song_path, scene_prompts, seeds_per_clip,
                     judge_select, target_length, lyrics_text, music_prompt,
                     output_path):
    def _still_current() -> bool:
        with _state.lock:
            return _state.id == job_id

    try:
        with _state.lock:
            _state.phase = "Resolving song..."
            _state.output_path = output_path

        resolved_song = _resolve_song_sync(
            song_path, target_length=target_length,
            lyrics_text=lyrics_text, music_prompt=music_prompt,
        )
        if not _still_current():
            return

        with _state.lock:
            if _state.cancel_requested:
                _state.status = "cancelled"
                _state.finished_at = time.time()
                return
            _state.phase = "Acquiring WanGP worker..."

        from core.gpu_orchestrator import gpu, GPUBusyError
        try:
            gpu.acquire("wangp", reason="chain.py ratified engine render")
        except GPUBusyError as e:
            with _state.lock:
                _state.status = "error"
                _state.error = str(e)
                _state.finished_at = time.time()
            return

        cmd = build_command(
            image_paths=images, song_path=resolved_song, output_path=output_path,
            scene_prompts=scene_prompts, seeds_per_clip=seeds_per_clip,
            judge_select=judge_select,
        )

        with _state.lock:
            if _state.cancel_requested:
                _state.status = "cancelled"
                _state.finished_at = time.time()
                return
            _state.cmd = cmd
            _state.phase = "Launching chain.py..."
            _state.started_at = time.time()

        log.info("[chain_runner] launching: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=str(ENGINE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        with _state.lock:
            if not _still_current():
                proc.terminate()
                return
            _state.proc = proc
            _state.status = "running"
            _state.phase = "running"

        for line in proc.stdout:
            if not _still_current():
                _kill_tree(proc)
                return
            with _state.lock:
                cancelled = _state.cancel_requested
            if cancelled:
                _kill_tree(proc)
                break
            _state._append_line(line)

        proc.wait(timeout=30)

        with _state.lock:
            if not _still_current():
                return
            _state.finished_at = time.time()
            if _state.cancel_requested:
                _state.status = "cancelled"
            elif proc.returncode == 0:
                _state.status = "done"
                _state.pct = 100
                _state.phase = "done"
            else:
                _state.status = "error"
                if not _state.error:
                    _state.error = (f"chain.py exited with code {proc.returncode} -- "
                                    f"see log_tail for the last output lines")
    except Exception as e:
        log.exception("[chain_runner] job %s crashed", job_id)
        with _state.lock:
            if _still_current():
                _state.status = "error"
                _state.error = str(e)
                _state.finished_at = time.time()


def _kill_tree(proc: subprocess.Popen):
    """Terminate the subprocess AND its children (chain.py shells out to
    ffmpeg for concat/mux/stabilize/upscale) -- proc.terminate() alone only
    kills the python process and can orphan a running ffmpeg. taskkill /T
    (tree) mirrors the established pattern in services/manager.py."""
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=10,
        )
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def cancel() -> dict:
    with _state.lock:
        if _state.status not in ("starting", "running"):
            return _state.to_dict()
        _state.cancel_requested = True
        proc = _state.proc
        _state.phase = "cancelling..."
    if proc is not None:
        _kill_tree(proc)
    return get_status()
