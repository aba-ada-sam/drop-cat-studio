"""WanGP video generation client for Create Videos.

Supports two modes: persistent worker (port 7899) and subprocess fallback.
Ported from DropCatGo-Fun-Videos_w_Audio/video_generator.py.
"""
import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from core import config as cfg
from core.ffmpeg_utils import parse_resolution

log = logging.getLogger(__name__)

WANGP_WORKER_PORT     = 7899
WANGP_LOCAL_URL       = f"http://127.0.0.1:{WANGP_WORKER_PORT}"
# Satellite WanGP is proxied through the relay on the 3060.
# /wangp/* on the relay forwards to the local worker at 127.0.0.1:7899 there.
WANGP_SATELLITE_RELAY = "http://192.168.86.49:9999"
WANGP_SATELLITE_URL   = f"{WANGP_SATELLITE_RELAY}/wangp"
# Satellite (second-box) generation is a FUTURE-DEV feature, NOT current ops.
# Hard-off so no job ever routes off this machine. Flip to True (and configure
# satellite_host) to re-enable for development.
SATELLITE_ENABLED = False

# Negative prompts tuned per model family and motion style.
#
# LTX-2 research (ltx.io official + community testing):
#   - 5-8 terms max. Stacking 20+ terms produces weird outputs (quality degrades).
#   - Particle artifacts (snow, dust, debris) are NOT suppressed by negative prompts --
#     they're caused by positive prompt wording that asks for particle-generating motion.
#     The fix is in the positive prompt, not the negative.
#   - "static" in neg forces LTX to prove it's not static, which triggers hallucinated
#     particle motion on anchored/chained frames. Include only for unconstrained dynamic.
#
# Wan2.1 research: negative prompts are inconsistent in 2.1. Keep them minimal and
# reactive (suppress problems you observe, not problems you fear).

# Negative prompts tuned per model family per official research + community findings.
# LTX-2: 8-10 terms max -- stacking 20+ terms degrades output quality.
#   anime/cartoon must be blocked -- LTX drifts to anime style on energetic prompts.
#   morphing/warping blocks the "warble" artifact (frame-to-frame micro-deformation).
#   bad face/deformed blocks the character quality degradation in chained clips.
_NEG_LTX_CALM = (
    "worst quality, low quality, blurry, distorted, temporal artifacts, "
    "watermark, text, anime, cartoon, 2d, morphing, warping, "
    "bad face, deformed, different person, "
    "zoom in, zoom out, zooming, camera zoom, camera pan, panning, dolly, "
    "camera motion, camera movement, ken burns, push in, pull back"
)
_NEG_LTX_DYNAMIC = (
    "worst quality, low quality, blurry, distorted, temporal artifacts, "
    "watermark, text, static, anime, cartoon, 2d, morphing, warping, "
    "bad face, deformed, different person, "
    "zoom in, zoom out, zooming, camera zoom, camera pan, panning, dolly, "
    "camera motion, camera movement, ken burns, push in, pull back"
)
# Wan2.1: negative prompts are more effective than LTX -- include quality + character terms.
# bad hands/face are common Wan artifacts in character sequences.
_NEG_WAN = (
    "low quality, blurry, distorted, unnatural movement, watermark, text, "
    "temporal artifacts, anime, cartoon, 2d, morphing, "
    "bad face, bad hands, deformed, mutated, extra fingers, different person"
)


def negative_prompt_for(model_name: str, motion_style: str = "dynamic") -> str:
    """Return the appropriate negative prompt for the given model and motion style."""
    if "ltx" in model_name.lower():
        if motion_style in ("calm", "gentle"):
            return _NEG_LTX_CALM
        return _NEG_LTX_DYNAMIC
    return _NEG_WAN


# Infinite Zoom negative prompts.
#
# The standard negative prompts (above) BAN all camera motion -- correct for
# Create Videos (subject moves, camera holds), catastrophic for Zoom (the whole
# point is camera motion). A zoom job that uses _NEG_LTX_CALM is told "zoom in,
# zoom out, push in, pull back, camera motion" are forbidden, so the model
# produces an uncontrolled drift -- which reads as a slow zoom-out no matter
# which direction was selected.
#
# Zoom needs the OPPOSITE: keep the quality + identity terms, allow the wanted
# camera move, and ban the WRONG direction so the model can't reverse it.
_NEG_ZOOM_BASE = (
    "worst quality, low quality, blurry, distorted, temporal artifacts, "
    "watermark, text, anime, cartoon, 2d, morphing, warping, "
    "bad face, deformed, different person, scene change, different location, "
    "cut, jump cut, teleport"
)
_NEG_ZOOM_OPPOSITE = {
    # zoom IN job -> forbid any pull-back so the camera can't reverse to zoom out
    "in":  "zoom out, zooming out, pull back, pulling back, pullback, dolly out, "
           "camera retreating, camera pulls away, widening view, revealing surroundings",
    # zoom OUT job -> forbid any push-in
    "out": "zoom in, zooming in, push in, pushing in, push forward, dolly in, "
           "camera advancing, camera moves closer, narrowing view, close-up crash",
}


def zoom_negative_prompt(model_name: str, direction: str) -> str:
    """Negative prompt for an Infinite Zoom clip.

    Unlike negative_prompt_for(), this ALLOWS camera motion (zoom is camera
    motion) and instead bans the opposite direction so the model holds the
    selected zoom. model_name is accepted for parity/future per-model tuning;
    the base terms are safe for both LTX and Wan.
    """
    opp = _NEG_ZOOM_OPPOSITE.get(direction, "")
    return f"{_NEG_ZOOM_BASE}, {opp}" if opp else _NEG_ZOOM_BASE

MODELS = {
    # Per-model defaults sent to the UI so controls change automatically on model switch.
    # vram_min_gb: minimum VRAM to run this model without system thrashing.
    #   Wan 14B: 11.95 GB preloaded (77.5% of 15.87 GB int8) + working memory = 12 GB min.
    #   Wan 720P: higher resolution needs more headroom = 16 GB min.
    #   LTX-2: ~9 GB, so 10 GB min for comfortable headroom.
    #   Wan T2V 1.3B: tiny model, 4 GB min.
    # poll_timeout_s: Wan 14B streams 22.5% from RAM via async shuttle (~14s/step on 16GB);
    #   LTX fits fully in VRAM (~2s/step). Timeouts sized accordingly.
    # vram_min_gb: empirically confirmed on RTX 5080 (15.9 GB). WanGP caps its
    # VRAM budget at 80% = 13 GB. Wan I2V 14B async shuttle preloads 11.95 GB
    # but the first denoising step needs working memory that pushes past 13 GB,
    # causing a consistent Step 0 deadlock. Minimum confirmed working: 20 GB.
    "Wan2.1-I2V-14B-480P":    {"res": (854, 480),  "fps": 16, "max_sec": 16, "i2v": True,  "steps": 25, "guidance": 4.5, "default_clips": 4, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 1800, "vram_min_gb": 20},
    "Wan2.1-I2V-14B-720P":    {"res": (1280, 720), "fps": 16, "max_sec": 12, "i2v": True,  "steps": 25, "guidance": 4.5, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 2400, "vram_min_gb": 24},
    "LTX-2 Dev19B Distilled": {"res": (1032, 580), "fps": 25, "max_sec": 19, "i2v": True,  "steps": 8,  "guidance": 3.0, "default_clips": 2, "default_dur": 6, "motion": "calm",    "poll_timeout_s": 600,  "vram_min_gb": 10},
    "LTX-2 Dev13B":           {"res": (1032, 580), "fps": 25, "max_sec": 19, "i2v": True,  "steps": 40, "guidance": 3.5, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 1200, "vram_min_gb": 20},
    # 360P variant: same model, resolution capped to 640x360 so step-0 VRAM
    # drops ~75% vs native 580P -- reliably fits 16GB WanGP budget. Real-ESRGAN
    # 4x post-processing (auto-enabled by DCS) recovers to 2560x1440.
    # If it still deadlocks, halve max_sec to 4 to cut frame count in half.
    "LTX-2 Dev13B 360P":      {"res": (640, 360),  "fps": 25, "max_sec": 8,  "i2v": True,  "steps": 20, "guidance": 3.5, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 1200, "vram_min_gb": 10},
    "Wan2.1-T2V-14B":         {"res": (854, 480),  "fps": 16, "max_sec": 16, "i2v": False, "steps": 25, "guidance": 5.5, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 1800, "vram_min_gb": 12},
    "Wan2.1-T2V-1.3B":        {"res": (854, 480),  "fps": 16, "max_sec": 12, "i2v": False, "steps": 20, "guidance": 5.0, "default_clips": 3, "default_dur": 6, "motion": "dynamic", "poll_timeout_s": 900,  "vram_min_gb": 4},
}


def _worker_alive(base_url: str = WANGP_LOCAL_URL) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=3) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False


def satellite_alive() -> bool:
    """Return True if the 3060 satellite WanGP proxy is reachable and healthy.
    Always False in current ops (SATELLITE_ENABLED) -- future-dev feature."""
    if not SATELLITE_ENABLED:
        return False
    return _worker_alive(WANGP_SATELLITE_URL)


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
    worker_url: str | None = None,
) -> str | None:
    """Generate a video via WanGP. Returns output path or None.

    worker_url: routes to that WanGP HTTP endpoint instead of the local worker.
    progress_fn(step, total_steps) is called on each inference step.
    override_width/height bypass the model's native resolution.
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

    # Satellite shortcut: if caller supplied a worker_url, skip local worker
    # entirely and route directly to that endpoint. Gated by SATELLITE_ENABLED
    # (off in current ops) so a stale satellite worker_url falls through to local.
    if SATELLITE_ENABLED and worker_url and worker_url != WANGP_LOCAL_URL:
        return _generate_via_worker(
            image_path, prompt, out_path, num_frames, res_w, res_h,
            steps, guidance, seed, model_name, end_image_path,
            start_video_path, loras or [], stop_check, log_fn, progress_fn,
            mmaudio=mmaudio, audio_source=audio_source,
            negative_prompt=negative_prompt,
            worker_url=worker_url,
        )

    if _worker_alive():
        return _generate_via_worker(
            image_path, prompt, out_path, num_frames, res_w, res_h,
            steps, guidance, seed, model_name, end_image_path,
            start_video_path, loras or [], stop_check, log_fn, progress_fn,
            mmaudio=mmaudio, audio_source=audio_source,
            negative_prompt=negative_prompt,
            worker_url=WANGP_LOCAL_URL,
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


def generate_video_satellite(
    image_path, prompt, out_path, **kwargs
) -> str | None:
    """Like generate_video() but routes to the 3060 satellite worker via relay proxy.

    Falls back to the local worker if the satellite is not reachable.
    The output file is written locally (out_path on this machine); the satellite
    generates it and streams the result back via the status/output_path mechanism.
    """
    if not satellite_alive():
        log.warning("[satellite] not reachable -- falling back to local worker")
        return generate_video(image_path, prompt, out_path, **kwargs)

    model_name  = kwargs.get("model_name", "LTX-2 Dev19B Distilled")
    model_info  = MODELS.get(model_name, MODELS["LTX-2 Dev19B Distilled"])
    fps         = model_info.get("fps", 25)
    duration    = kwargs.get("duration", 14.0)
    override_w  = kwargs.get("override_width")
    override_h  = kwargs.get("override_height")
    resolution  = kwargs.get("resolution", "580p")

    if override_w and override_h:
        res_w, res_h = int(override_w), int(override_h)
    else:
        res_w, res_h = _resolve_res(model_name, resolution)

    num_frames = max(17, int(duration * fps))
    if num_frames % 2 == 0:
        num_frames += 1
    max_sec   = model_info.get("max_sec", 19)
    frame_cap = int(max_sec * fps)
    if frame_cap % 2 == 0:
        frame_cap -= 1
    num_frames = min(num_frames, frame_cap)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    return _generate_via_worker(
        image_path, prompt, out_path, num_frames, res_w, res_h,
        kwargs.get("steps", 8),
        kwargs.get("guidance", 2.5),
        kwargs.get("seed", -1),
        model_name,
        kwargs.get("end_image_path"),
        kwargs.get("start_video_path"),
        kwargs.get("loras") or [],
        kwargs.get("stop_check"),
        kwargs.get("log_fn"),
        kwargs.get("progress_fn"),
        mmaudio=kwargs.get("mmaudio", False),
        audio_source=kwargs.get("audio_source"),
        negative_prompt=kwargs.get("negative_prompt", ""),
        worker_url=WANGP_SATELLITE_URL,
    )


def _download_satellite_file(remote_path: str, local_dest: str,
                              worker_url: str, log_fn=None) -> str | None:
    """Download a file from the satellite machine via the relay /download endpoint.

    When the 3060 generates a clip, it saves to its own local filesystem.
    The relay at port 9999 exposes GET /download?path=... to serve any local file.
    We download it and write it to local_dest on the main machine.

    Returns local_dest on success, None on failure.
    """
    try:
        # Derive relay base URL from worker_url (strip /wangp suffix)
        relay_base = worker_url.replace("/wangp", "").rstrip("/")
        url = f"{relay_base}/download?path={urllib.parse.quote(remote_path)}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        if not data:
            if log_fn:
                log_fn(f"[warn] Satellite download returned empty file: {remote_path}")
            return None
        os.makedirs(os.path.dirname(local_dest) or ".", exist_ok=True)
        with open(local_dest, "wb") as f:
            f.write(data)
        if log_fn:
            log_fn(f"[info] Downloaded satellite clip ({len(data)//1024}KB) -> {os.path.basename(local_dest)}")
        return local_dest
    except Exception as exc:
        if log_fn:
            log_fn(f"[warn] Satellite file download failed: {exc}")
        return None


def _generate_via_worker(
    image_path, prompt, out_path, num_frames, width, height,
    steps, guidance, seed, model_name, end_image_path,
    start_video_path, loras, stop_check, log_fn, progress_fn=None,
    mmaudio: bool = False,
    audio_source: str | None = None,
    negative_prompt: str = "",
    worker_url: str = WANGP_LOCAL_URL,
) -> str | None:
    """Generate via a WanGP worker HTTP endpoint. worker_url selects local or satellite."""
    # Satellite: use a temp path that EXISTS on the 3060's filesystem.
    # C:\DropCat-Studio\output\... doesn't exist on the 3060, so WanGP would
    # generate but silently fail to save.  C:\DCS-satellite\tmp\ is guaranteed
    # to exist there (created during satellite setup).
    _is_satellite = worker_url and worker_url != WANGP_LOCAL_URL
    _local_out    = os.path.abspath(out_path)
    if _is_satellite:
        import uuid as _uuid
        _sat_tmp_path = r"C:\DCS-satellite\tmp\\" + _uuid.uuid4().hex[:12] + ".mp4"
        _effective_out = _sat_tmp_path
    else:
        _effective_out = _local_out

    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "model_name": model_name,
        "output_path": _effective_out,
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
        label = "satellite" if "192.168" in worker_url else f"port {WANGP_WORKER_PORT}"
        log_fn(f"[info] Sending to WanGP worker ({label})...")

    # Submit with 409-retry: if worker is busy, wait until it's free then retry.
    # On success, capture the generation token so we can reject stale results if
    # this thread outlives its DCS job (e.g. after a timeout).
    _model_timeout = MODELS.get(model_name, {}).get("poll_timeout_s", 600)
    # 3060 satellite has smaller VRAM and needs more RAM offloading -- give it 2x time
    if worker_url and worker_url != WANGP_LOCAL_URL:
        _model_timeout = _model_timeout * 2
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
                f"{worker_url}/generate",
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
                            f"{worker_url}/health", timeout=5
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
                    f"{worker_url}/abort",
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
                f"{worker_url}/status", timeout=5
            ) as r:
                status = json.loads(r.read())

            # Reject this status if the worker is busy with a DIFFERENT job.
            # Only check token while busy -- when the worker finishes it resets
            # token to 0, which would falsely look like a mismatch otherwise.
            if my_token is not None and status.get("busy") and status.get("token") != my_token:
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
                # Satellite: result file lives on the 3060, not locally.
                # Download it via the relay's /download endpoint and save
                # to the locally-requested output_path.
                if result_path and _is_satellite:
                    local_path = _download_satellite_file(
                        result_path, _local_out, worker_url, log_fn
                    )
                    if local_path:
                        return local_path
                # Fallback 1: result_path exists but file missing -- check _tmp variant
                if result_path and os.path.isfile(result_path.replace(".mp4", "_tmp.mp4")):
                    return result_path.replace(".mp4", "_tmp.mp4")
                # Fallback 2: result_path is None -- check _tmp of requested path
                tmp_of_requested = _local_out.replace(".mp4", "_tmp.mp4")
                if os.path.isfile(tmp_of_requested):
                    if log_fn:
                        log_fn(f"[info] Using _tmp fallback for {os.path.basename(_local_out)}")
                    return tmp_of_requested
                # Guard: if we've polled for < 6s, worker may not have started yet
                if time.time() - _poll_start < 6:
                    time.sleep(2)
                    continue
                if log_fn:
                    log_fn(f"[error] generate_video returning None: result={result_path} error={status.get('error')}")
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
    """Merge video and audio via ffmpeg.

    Duration rule: the output is always exactly as long as the video.
    Audio is padded with silence if it ends before the video (apad),
    and truncated if it runs longer (-t video_dur). This prevents
    '-shortest' from chopping the video when _trim_silence_tail trimmed
    a few seconds off the audio tail -- which was cutting lyrics mid-word.
    """
    from core.ffmpeg_utils import probe_duration as _probe
    video_dur = _probe(video_path) or 0.0
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        # loudnorm + apad: normalise loudness then pad silence to fill any
        # gap between end-of-audio and end-of-video (so the full video plays).
        "-af", "loudnorm=I=-14:TP=-2:LRA=11,apad",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path,
    ]
    # Pin output to exact video duration so we never go longer than the video
    # (apad could theoretically run forever without a stop condition).
    if video_dur > 0:
        cmd = cmd[:-1] + ["-t", str(video_dur), cmd[-1]]
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
