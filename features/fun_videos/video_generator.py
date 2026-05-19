"""WanGP video generation client for Create Videos.

Supports two modes: persistent worker (port 7899) and subprocess fallback.
Ported from DropCatGo-Fun-Videos_w_Audio/video_generator.py.
"""
import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from core import config as cfg
from core.ffmpeg_utils import parse_resolution

log = logging.getLogger(__name__)

WANGP_WORKER_PORT = 7899

# Negative prompts tuned per model family and motion style.
# Research: 5-8 targeted terms outperform exhaustive lists (crepal.ai).
# LTX HuggingFace card includes "static" in negatives -- this tells the
# model to avoid fully-frozen frames and is correct for dynamic clips.
# For calm/gentle mode, "static" is intentionally absent: adding it pushes
# LTX to hallucinate motion (rain, debris) to fill anchored regions.

# LTX-2 calm/gentle: no "static" -- we want minimal motion, not invented motion.
_NEG_LTX_CALM = (
    "shaky, glitchy, low quality, worst quality, deformed, distorted, "
    "motion smear, motion artifacts, watermark, text, "
    "rain, snow, snowflakes, precipitation, falling particles, falling debris, blizzard, hail"
)
# LTX-2 dynamic (Dev13B or Distilled in dynamic mode): "static" included per
# HuggingFace card -- prevents fully frozen non-animating output.
_NEG_LTX_DYNAMIC = (
    "shaky, glitchy, low quality, worst quality, deformed, distorted, "
    "motion smear, motion artifacts, watermark, text, static, "
    "rain, snow, snowflakes, precipitation, falling particles, falling debris, blizzard, hail"
)
# Wan2.1: strong subject anchoring, handles longer neg lists, but still concise.
# No "static" needed -- Wan doesn't have LTX's empty-region hallucination problem.
_NEG_WAN = (
    "low quality, blurry, distorted faces, unnatural movement, "
    "text, watermark, shaky camera, motion smear, temporal artifacts, "
    "rain, precipitation, falling particles, falling debris"
)


def negative_prompt_for(model_name: str, motion_style: str = "dynamic") -> str:
    """Return the appropriate negative prompt for the given model and motion style."""
    if "ltx" in model_name.lower():
        if motion_style in ("calm", "gentle"):
            return _NEG_LTX_CALM
        return _NEG_LTX_DYNAMIC
    return _NEG_WAN

MODELS = {
    # Per-model defaults sent to the UI so controls change automatically on model switch.
    # default_clips / default_dur: what the multi-clip slider resets to.
    # motion: "calm" (environment-only, no subject movement) or "dynamic" (kinetic).
    # Wan I2V: strong subject anchoring, handles 4 clips + dynamic motion fine.
    # LTX Distilled: 12-step budget cannot maintain complex subject detail across clips;
    #   cap at 2 clips and calm motion to prevent identity drift.
    # LTX Dev13B: 40 steps, strong image conditioning, defaults to dynamic motion.
    # Wan I2V: 80-100 frames at 16fps = 5-6s sweet spot per clip. 25 steps standard.
    # poll_timeout_s: Wan models (15+ GB) stream from RAM when they exceed the 80% VRAM
    # budget (~13 GB on a 16 GB card), resulting in ~14s/step vs ~3s/step for LTX in VRAM.
    # 25 steps x 14s = 350s denoising + overhead; give 1800s (30 min) per clip so jobs
    # complete instead of failing. LTX fits in VRAM cleanly so 600s is plenty.
    "Wan2.1-I2V-14B-480P":    {"res": (854, 480),  "fps": 16, "max_sec": 16, "i2v": True,  "steps": 25, "guidance": 4.5, "default_clips": 4, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 1800},
    "Wan2.1-I2V-14B-720P":    {"res": (1280, 720), "fps": 16, "max_sec": 12, "i2v": True,  "steps": 25, "guidance": 4.5, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 2400},
    # LTX-2 Distilled: two-stage schedule, 4-8 steps optimal (8 max -- more regresses quality).
    # Guidance 3.0. Default 2 clips; chaining beyond 3 compounds softness drift.
    "LTX-2 Dev19B Distilled": {"res": (1032, 580), "fps": 25, "max_sec": 19, "i2v": True,  "steps": 8,  "guidance": 3.0, "default_clips": 2, "default_dur": 6, "motion": "calm",    "poll_timeout_s": 600},
    # LTX-2 Dev13B: 40 steps, strong image conditioning. Handles deliberate motion
    # (strides, gestures, turns). Preferred for painted/illustrated/fantasy subjects.
    "LTX-2 Dev13B":           {"res": (1032, 580), "fps": 25, "max_sec": 19, "i2v": True,  "steps": 40, "guidance": 3.5, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 1200},
    "Wan2.1-T2V-14B":         {"res": (854, 480),  "fps": 16, "max_sec": 16, "i2v": False, "steps": 25, "guidance": 5.5, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 1800},
    "Wan2.1-T2V-1.3B":        {"res": (854, 480),  "fps": 16, "max_sec": 12, "i2v": False, "steps": 20, "guidance": 5.0, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 900},
}


def _worker_alive() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{WANGP_WORKER_PORT}/health", timeout=3) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False


def _resolve_res(model_name: str, resolution: str) -> tuple[int, int]:
    model_info = MODELS.get(model_name, {})
    if model_info.get("res"):
        return model_info["res"]
    return parse_resolution(resolution)


def generate_video(
    image_path: str | None,
    prompt: str,
    out_path: str,
    duration: float = 14.0,
    model_name: str = "LTX-2 Dev19B Distilled",
    resolution: str = "580p",
    override_width: int | None = None,
    override_height: int | None = None,
    mmaudio: bool = False,
    steps: int = 30,
    guidance: float = 7.5,
    seed: int = -1,
    end_image_path: str | None = None,
    start_video_path: str | None = None,
    loras: list | None = None,
    audio_source: str | None = None,
    negative_prompt: str = "",
    stop_check=None,
    log_fn=None,
    progress_fn=None,
) -> str | None:
    """Generate a video via WanGP. Returns output path or None.

    progress_fn(step: int, total_steps: int) is called each time the worker
    reports a new inference step, allowing callers to update a progress bar.
    override_width / override_height bypass the model's native resolution so
    custom aspect ratios work regardless of which model is loaded.
    """
    model_info = MODELS.get(model_name, MODELS["LTX-2 Dev19B Distilled"])
    fps = model_info.get("fps", 16)
    if override_width and override_height:
        res_w, res_h = int(override_width), int(override_height)
    else:
        res_w, res_h = _resolve_res(model_name, resolution)

    num_frames = max(17, int(duration * fps))
    if num_frames % 2 == 0:
        num_frames += 1  # WanGP requires odd frame count

    # Hard-cap to model's max_sec: LTX-2 at 481 frames triggers 2-window mode
    # (doubles generation time). The odd-adjustment above can push 480 -> 481.
    max_sec = model_info.get("max_sec", 19)
    frame_cap = int(max_sec * fps)
    if frame_cap % 2 == 0:
        frame_cap -= 1  # keep cap odd so min() can't land on an even number
    num_frames = min(num_frames, frame_cap)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    # Wait for the persistent worker (it may still be loading after a cold start).
    # Never fall back to subprocess while the worker process is alive -- that would
    # spawn a second WanGP instance that competes for VRAM and causes the "two
    # videos" appearance.
    from services.manager import wangp_worker_alive as _proc_alive
    wait_deadline = time.time() + 180
    while not _worker_alive():
        if stop_check and stop_check():
            return None
        if time.time() > wait_deadline:
            break
        # Log a waiting message periodically
        if log_fn and int(time.time()) % 10 == 0:
            log_fn("[info] Waiting for WanGP worker to load...")
        time.sleep(2)

    if _worker_alive():
        return _generate_via_worker(
            image_path, prompt, out_path, num_frames, res_w, res_h,
            steps, guidance, seed, model_name, end_image_path,
            start_video_path, loras or [], stop_check, log_fn, progress_fn,
            mmaudio=mmaudio, audio_source=audio_source,
            negative_prompt=negative_prompt,
        )

    # Only fall back to subprocess if the worker process is not running at all
    # (i.e. WanGP is not configured or the worker died before startup completed).
    if _proc_alive():
        if log_fn:
            log_fn("[error] WanGP worker timed out during model load")
        return None
    return _generate_via_subprocess(
        image_path, prompt, out_path, num_frames, res_w, res_h,
        steps, guidance, seed, model_name, end_image_path,
        stop_check, log_fn, progress_fn,
    )


def _generate_via_worker(
    image_path, prompt, out_path, num_frames, width, height,
    steps, guidance, seed, model_name, end_image_path,
    start_video_path, loras, stop_check, log_fn, progress_fn=None,
    mmaudio: bool = False,
    audio_source: str | None = None,
    negative_prompt: str = "",
) -> str | None:
    """Generate via persistent worker on port 7899."""
    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "model_name": model_name,
        "output_path": os.path.abspath(out_path),
        "num_frames": num_frames,
        "width": width,
        "height": height,
        "steps": steps,
        "guidance_scale": guidance,
        "seed": seed,
        "mmaudio": mmaudio,
    }
    if audio_source and os.path.isfile(audio_source):
        payload["audio_source"] = os.path.abspath(audio_source)
    if start_video_path and os.path.isfile(start_video_path):
        payload["start_video"] = os.path.abspath(start_video_path)
    elif image_path:
        payload["start_image"] = os.path.abspath(image_path)
    if end_image_path:
        payload["end_image"] = os.path.abspath(end_image_path)
    if loras:
        payload["activated_loras"] = [l["path"] for l in loras]
        payload["loras_multipliers"] = " ".join(str(l.get("multiplier", 1.0)) for l in loras)

    if log_fn:
        log_fn(f"[info] Sending to WanGP worker (port {WANGP_WORKER_PORT})...")

    # Submit with 409-retry: if worker is busy, wait until it's free then retry.
    # On success, capture the generation token so we can reject stale results if
    # this thread outlives its DCS job (e.g. after a timeout).
    _model_timeout = MODELS.get(model_name, {}).get("poll_timeout_s", 600)
    submit_deadline = time.time() + _model_timeout
    my_token = None
    while True:
        if stop_check and stop_check():
            return None
        if time.time() > submit_deadline:
            if log_fn:
                log_fn("[error] Timed out waiting for worker to become available")
            return None
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{WANGP_WORKER_PORT}/generate",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read())
            if not resp.get("ok"):
                if log_fn:
                    log_fn(f"[error] Worker rejected: {resp.get('error')}")
                return None
            my_token = resp.get("token")
            break  # submission accepted
        except urllib.error.HTTPError as e:
            if e.code == 409:
                if log_fn:
                    log_fn("[info] Worker busy -- waiting for current job to finish...")
                # Poll /health until not busy, checking stop_check each iteration
                while time.time() < submit_deadline:
                    if stop_check and stop_check():
                        return None
                    time.sleep(2)
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{WANGP_WORKER_PORT}/health", timeout=5
                        ) as r:
                            health = json.loads(r.read())
                        if not health.get("busy", True):
                            break
                    except Exception:
                        pass
                continue  # retry submit
            if log_fn:
                log_fn(f"[error] Worker request failed: {e}")
            return None
        except Exception as e:
            if log_fn:
                log_fn(f"[error] Worker request failed: {e}")
            return None

    # Poll for completion. We verify the token on each poll so a stale thread
    # can't claim results that belong to the next job.
    _poll_start = time.time()
    deadline = _poll_start + _model_timeout
    while time.time() < deadline:
        if stop_check and stop_check():
            # Tell the worker to abort the running generation so it doesn't
            # keep consuming GPU for a job we no longer care about.
            try:
                abort_req = urllib.request.Request(
                    f"http://127.0.0.1:{WANGP_WORKER_PORT}/abort",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(abort_req, timeout=3)
            except Exception:
                pass
            return None
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{WANGP_WORKER_PORT}/status", timeout=5
            ) as r:
                status = json.loads(r.read())

            # Reject this status if the worker has moved on to a newer job
            if my_token is not None and status.get("token") != my_token:
                if log_fn:
                    log_fn("[error] Worker token mismatch -- our job was superseded")
                return None

            if not status.get("busy"):
                if status.get("error"):
                    if log_fn:
                        log_fn(f"[error] Generation failed: {status['error']}")
                    return None
                result_path = status.get("result")
                if result_path and os.path.isfile(result_path):
                    return result_path
                # Guard: if we've polled for < 6s, worker may not have started yet
                if time.time() - _poll_start < 6:
                    time.sleep(2)
                    continue
                return None
            if log_fn and status.get("progress"):
                log_fn(f"[info] {status['progress']}")
            step = status.get("step", 0)
            total = status.get("total_steps", 0)
            if progress_fn and total > 0:
                progress_fn(step, total)
        except Exception:
            pass
        time.sleep(2)

    if log_fn:
        _timeout_min = _model_timeout // 60
        log_fn(f"[error] Video generation took too long (over {_timeout_min} min). "
               "Try a shorter clip duration, fewer denoise steps, or smaller resolution.")
    return None


def _generate_via_subprocess(
    image_path, prompt, out_path, num_frames, width, height,
    steps, guidance, seed, model_name, end_image_path,
    stop_check, log_fn, progress_fn=None,
) -> str | None:
    """Generate via wan_bridge_client.py subprocess."""
    wan_root = cfg.get("wan2gp_root")
    if not wan_root:
        if log_fn:
            log_fn("[error] WanGP path not configured")
        return None

    python_exe = cfg.get("wan2gp_python") or cfg.find_wan_python(wan_root)
    bridge_script = str(Path(__file__).resolve().parent.parent.parent / "services" / "wan_bridge_client.py")

    cmd = [
        python_exe, bridge_script,
        "--wangp-app", wan_root,
        "--prompt", prompt,
        "--output_path", os.path.abspath(out_path),
        "--num_frames", str(num_frames),
        "--width", str(width),
        "--height", str(height),
        "--steps", str(steps),
        "--guidance_scale", str(guidance),
        "--model_name", model_name,
        "--seed", str(seed),
    ]
    if image_path:
        cmd += ["--start_image", os.path.abspath(image_path)]
    if end_image_path:
        cmd += ["--end_image", os.path.abspath(end_image_path)]

    if log_fn:
        log_fn(f"[info] Launching WanGP subprocess ({model_name})...")

    import queue as _queue
    import re
    import threading as _threading
    _TQDM_STEP_RE = re.compile(r'(\d+)/(\d+)\s*\[')

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd, cwd=wan_root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

        # Drain stdout in a background thread so the main thread can check
        # stop_check every second instead of blocking on stdout reads.
        _line_q: _queue.Queue = _queue.Queue()

        def _drain(p, q):
            try:
                for line in p.stdout:
                    q.put(line)
            except Exception:
                pass
            q.put(None)  # sentinel

        _threading.Thread(target=_drain, args=(proc, _line_q), daemon=True).start()

        while True:
            if stop_check and stop_check():
                proc.terminate()
                return None
            try:
                line = _line_q.get(timeout=1)
            except _queue.Empty:
                if proc.poll() is not None:
                    break  # process exited, queue will drain
                continue
            if line is None:
                break  # sentinel -- subprocess stdout closed
            stripped = line.strip()
            if stripped and log_fn:
                log_fn(f"[info] {stripped}")
            # Parse tqdm step output: "3/30 [00:15<..." -> progress_fn(3, 30)
            if progress_fn and stripped:
                m = _TQDM_STEP_RE.search(stripped)
                if m:
                    n, total = int(m.group(1)), int(m.group(2))
                    if total >= 5:
                        progress_fn(n, total)

        proc.wait()
        if stop_check and stop_check():
            return None
        if proc.returncode != 0:
            if log_fn:
                log_fn(f"[error] WanGP subprocess exited with code {proc.returncode}")
            return None
        if os.path.isfile(out_path):
            return out_path
        return None
    except Exception as e:
        if log_fn:
            log_fn(f"[error] Subprocess failed: {e}")
        return None


def merge_video_audio(video_path: str, audio_path: str, out_path: str, log_fn=None) -> str | None:
    """Merge video and audio via ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        # loudnorm brings the whole track to -14 LUFS so quiet intros
        # play at the same perceived level as the main musical section.
        "-af", "loudnorm=I=-14:TP=-2:LRA=11",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and os.path.isfile(out_path):
            return out_path
        if log_fn:
            log_fn(f"[error] Merge failed: {r.stderr[-300:]}")
    except Exception as e:
        if log_fn:
            log_fn(f"[error] Merge error: {e}")
    return None
