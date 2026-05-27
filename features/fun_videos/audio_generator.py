"""ACE-Step audio generation client for Create Videos.

Calls the ACE-Step REST API on port 8020 to generate music/audio.
Based on the battle-tested DropCatGo-Fun-Videos_w_Audio/audio_generator.py.
"""
import json
import logging
import re as _re_sil
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ACESTEP_PORT = 8020
_ACESTEP_DEFAULT = f"http://127.0.0.1:{ACESTEP_PORT}"

def _api_base() -> str:
    """Return the ACE-Step base URL, reading host from config so remote mode works.

    When the configured host is remote and auto-discovery is enabled, falls back
    to LAN discovery (hostname hints + subnet sweep) so the satellite's DHCP IP
    can change without breaking the link.
    """
    try:
        from core import config as _cfg
        host = (_cfg.get("acestep_host") or "localhost").strip()
        if host.lower() in ("localhost", "127.0.0.1", "0.0.0.0"):
            return f"http://{host}:{ACESTEP_PORT}"

        if not _cfg.get("auto_discover_satellite"):
            return f"http://{host}:{ACESTEP_PORT}"

        from core import satellite_discovery as _disc
        found = _disc.discover(
            port=ACESTEP_PORT,
            health_path="/health",
            cached_host=host,
            hostname_hints=_cfg.get("satellite_hostnames"),
            log_label="acestep",
        )
        if found:
            if found != host:
                try:
                    _cfg.set_val("acestep_host", found)
                except Exception:
                    pass
            return f"http://{found}:{ACESTEP_PORT}"
        return f"http://{host}:{ACESTEP_PORT}"
    except Exception:
        return _ACESTEP_DEFAULT

MAX_DURATION = 120
GENERATION_TIMEOUT = 300  # 5 min
POLL_INTERVAL = 3


def _acestep_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{_api_base()}/health", timeout=3):
            return True
    except Exception:
        return False


def _post(url: str, payload: dict, timeout: int = 30) -> dict | None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


_AUTHENTICITY_MARKERS = (
    "authentic", "raw", "gritty", "organic", "lo-fi", "lo fi",
    "vintage", "underground", "indie", "soul", "real",
    "not generic", "not polished", "not pop", "distinct character",
)


def _lyrics_request_vocals(lyrics: str) -> bool:
    text = (lyrics or "").strip().lower()
    return bool(text) and text not in {"[inst]", "[instrumental]"}


def _add_style_guardrails(prompt: str) -> str:
    """Append authenticity qualifiers if the prompt has no genre-identity signals.

    Steers ACE-Step away from default generic pop/lite-FM production.
    Use ONLY positive descriptors -- diffusion models ignore negation, so "not pop"
    activates "pop" rather than suppressing it.
    """
    if not prompt:
        return prompt
    lower = prompt.lower()
    if not any(m in lower for m in _AUTHENTICITY_MARKERS):
        return prompt.rstrip(", ") + ", distinct character, authentic energy, raw production"
    return prompt


def _normalize_prompt(prompt: str, instrumental: bool, lyrics: str) -> str:
    normalized = (prompt or "").strip()
    if instrumental or not _lyrics_request_vocals(lyrics):
        return _add_style_guardrails(normalized)

    prompt_lower = normalized.lower()
    # Word-boundary match -- substring match falsely triggered on "vib[rap]hone"
    # for "rap", which made ACE-Step skip the "lead vocal" hint and render the
    # track instrumental despite the lyrics block being non-empty.
    import re as _re_v
    vocal_keywords = (
        "vocal", "vocals", "singer", "singing", "sung", "female vocal",
        "male vocal", "choir", "duet", "rap", "spoken word",
    )
    has_vocal_kw = any(_re_v.search(rf"\b{_re_v.escape(kw)}\b", prompt_lower) for kw in vocal_keywords)
    if not has_vocal_kw:
        normalized = (normalized.rstrip(", ") + ", lead vocal") if normalized else "lead vocal"
        prompt_lower = normalized.lower()

    # Force ACE-Step to place vocals at t=0. Without this hint the model adds an
    # instrumental intro that can eat half the track before any lyric is sung.
    pacing_keywords = (
        "from the opening", "from the start", "early vocal", "vocals enter early",
        "immediate vocal", "lead vocal up front", "straight to verse", "no intro",
        "beat 1", "bar 1", "instant",
    )
    if not any(kw in prompt_lower for kw in pacing_keywords):
        normalized = (normalized.rstrip(", ") + ", cold open, no intro bars, voice on beat 1, immediate lyric entry") if normalized else "cold open, no intro bars, voice on beat 1, immediate lyric entry"

    return _add_style_guardrails(normalized)


def _extract_audio_url(item: dict) -> str | None:
    try:
        raw = item.get("result", "[]")
        items = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else {}
            return first.get("file") or first.get("audio_url") or first.get("audio_path")
    except Exception:
        pass
    return None


def _poll(task_id: str, stop_event: threading.Event | None = None, progress_fn=None) -> dict | None:
    deadline = time.time() + GENERATION_TIMEOUT
    started = time.time()
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            return {"status": -1}
        resp = _post(f"{_api_base()}/query_result",
                     {"task_id_list": json.dumps([task_id])}, timeout=15)
        if resp is not None:
            data = resp.get("data", [])
            if data:
                item = data[0] if isinstance(data[0], dict) else {}
                if item.get("status", 0) != 0:
                    return item
        elapsed = int(time.time() - started)
        if progress_fn:
            progress_fn(elapsed)
        time.sleep(POLL_INTERVAL)
    return None


def generate_audio(
    prompt: str,
    *,
    duration: float = 30.0,
    output_dir: str | Path | None = None,
    audio_format: str = "mp3",
    bpm: int | None = None,
    steps: int = 8,
    guidance: float = 7.0,
    seed: int = -1,
    lyrics: str = "",
    instrumental: bool = True,
    stop_event: threading.Event | None = None,
    progress_cb=None,
) -> tuple[str | None, str | None]:
    """Generate audio via ACE-Step. Returns (file_path, error)."""
    if not _acestep_alive():
        # Lazy-start: ACE-Step is deferred to keep VRAM free for Ollama.
        # Start it now and wait for it to be ready.
        from services.manager import start_acestep
        log.info("ACE-Step not running -- starting on demand for audio generation...")
        if progress_cb:
            progress_cb(-1)  # signal "starting service"
        ok, err = start_acestep()
        if not ok:
            return None, f"Failed to start ACE-Step: {err}"
        if not _acestep_alive():
            return None, "ACE-Step started but not responding"

    out_dir = Path(output_dir) if output_dir else Path(__file__).resolve().parent.parent.parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = max(5.0, min(float(duration), MAX_DURATION))
    has_vocals = not instrumental and _lyrics_request_vocals(lyrics)

    # Pass lyrics as-is -- do NOT inject [intro] markup; it causes ACE-Step to
    # produce a long blank intro that eats 2/3 of the track before vocals enter.
    effective_lyrics = lyrics.strip() if has_vocals else ""

    # ACE-Step uses section markers to control vocal onset. The model treats
    # numbered markers like [verse 1] / [chorus 2] / [verse 3] as a multi-section
    # song -- it then allocates an instrumental intro before "verse 1" so the
    # first lyric only enters ~1/3 to 1/2 of the way in. Stripping the numbers
    # collapses those into plain section tags and forces vocals on beat 1.
    # If the lyrics open with [intro] or have no marker at all, also prepend
    # [verse] so there is no silent leader.
    if has_vocals and effective_lyrics:
        import re as _re
        effective_lyrics = _re.sub(
            r'(?im)^\s*\[\s*(verse|chorus|bridge|hook|pre[- ]?chorus|outro)\s*[0-9ivxIVX]*\s*\]\s*$',
            r'[\1]',
            effective_lyrics,
        )
        if not _re.match(r'^\s*\[', effective_lyrics):
            effective_lyrics = '[chorus]\n' + effective_lyrics
        elif _re.match(r'^\s*\[intro\]', effective_lyrics, _re.IGNORECASE):
            effective_lyrics = _re.sub(r'^\s*\[intro\]', '[chorus]', effective_lyrics, count=1, flags=_re.IGNORECASE)
        # Strip any [outro] -- short tracks have no room and ACE-Step often pads
        # silence to "fit" it.
        effective_lyrics = _re.sub(r'(?im)^\s*\[outro\]\s*$', '', effective_lyrics).strip()
        # ACE-Step treats [verse] as requiring an instrumental intro before vocals enter,
        # delaying the first lyric by 1/3 to 1/2 of the track. [chorus] signals
        # immediate vocal entry -- replace the opening section if it is still [verse].
        effective_lyrics = _re.sub(r'^\s*\[verse\]', '[chorus]', effective_lyrics, count=1, flags=_re.IGNORECASE)

    effective_prompt = _normalize_prompt(prompt, instrumental, lyrics)
    effective_prompt = effective_prompt or "atmospheric music, organic texture, instrumental, distinct character"

    payload = {
        "prompt": effective_prompt,
        "lyrics": effective_lyrics,
        # ACE-Step 1.5 dropped 'none' from chunk_mask_mode -- it now only
        # accepts 'explicit' or 'auto' (pydantic literal validation rejects
        # 'none' with HTTP 500). 'auto' is the closest behavioral match: lets
        # ACE-Step decide chunking by itself, same effective output as 'none'
        # for our short single-block tracks.
        "chunk_mask_mode": "auto",
        "thinking": False,
        "audio_duration": duration,
        "audio_format": audio_format,
        "batch_size": 1,
        "seed": seed,
        "use_random_seed": seed < 0,
        "inference_steps": steps,
        "guidance_scale": guidance,
        "lm_backend": "pt",
        "use_cot_caption": False,
        "use_cot_language": False,
    }
    if bpm is not None:
        try:
            bpm_int = int(bpm)
            if 20 <= bpm_int <= 300:
                payload["bpm"] = bpm_int
        except (TypeError, ValueError):
            pass

    # Use a longer timeout: ACE-Step's /release_task should return a task_id
    # quickly, but a slow-but-alive server can take several seconds under load.
    # 60s is still fast enough to distinguish a stuck socket from normal latency.
    resp = _post(f"{_api_base()}/release_task", payload, timeout=60)
    if resp is None:
        # /release_task timed out. Before killing ACE-Step, confirm it is
        # genuinely unresponsive (zombie) rather than just slow. A zombie process
        # answers /health but cannot process tasks; a busy process answers both.
        # Only restart if /health also fails or if we are certain no task was
        # accepted (task_id was never returned so there is nothing to orphan).
        _is_zombie = False
        try:
            _health = _post(f"{_api_base()}/health", {}, timeout=5)
            _is_zombie = (_health is None)  # port open but /health also unresponsive
        except Exception:
            _is_zombie = True
        if _is_zombie:
            log.warning("[audio] release_task timed out AND /health unresponsive "
                        "-- confirmed zombie ACE-Step, force-restarting...")
            try:
                from services.manager import stop_service, start_acestep as _start
                stop_service("acestep")
                ok, _err = _start()
                if ok:
                    log.info("[audio] ACE-Step restarted -- retrying release_task")
                    resp = _post(f"{_api_base()}/release_task", payload, timeout=60)
                else:
                    log.warning("[audio] ACE-Step restart failed: %s", _err)
            except Exception as _exc:
                log.warning("[audio] ACE-Step restart attempt failed: %s", _exc)
        else:
            log.warning("[audio] release_task timed out but /health OK "
                        "-- ACE-Step is alive, task may have been accepted; "
                        "not restarting to avoid duplicate submission")
        if resp is None:
            return None, "Failed to submit task to ACE-Step"

    # Handle both wrapped {"data": {"task_id": ...}} and flat {"task_id": ...} responses
    resp_data = resp.get("data") or resp
    if isinstance(resp_data, list):
        resp_data = resp_data[0] if resp_data else {}
    task_id = resp_data.get("task_id") if isinstance(resp_data, dict) else None
    if not task_id:
        return None, f"No task_id in ACE-Step response: {str(resp)[:200]}"

    log.info("ACE-Step task submitted: %s", task_id)

    item = _poll(task_id, stop_event=stop_event, progress_fn=progress_cb)
    if item is None:
        return None, "ACE-Step generation timed out"
    if item.get("status") == -1:
        return None, "Stopped"
    if item.get("status") != 1:
        return None, f"ACE-Step generation failed (status={item.get('status')})"

    audio_ref = _extract_audio_url(item)
    if not audio_ref:
        return None, "Generation succeeded but no audio URL in response"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = out_dir / f"audio_{ts}.{audio_format}"

    if audio_ref.startswith(("/", "http")):
        url = f"{_api_base()}{audio_ref}" if audio_ref.startswith("/") else audio_ref
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                dest.write_bytes(r.read())
        except Exception as e:
            return None, f"Audio download failed: {e}"
    else:
        if not Path(audio_ref).exists():
            return None, f"Audio file not found: {audio_ref}"
        shutil.copy2(audio_ref, dest)

    if dest.exists() and dest.stat().st_size > 0:
        log.info("Audio saved: %s", dest)
        _trim_silence_tail(str(dest))
        return str(dest), None

    return None, "Downloaded audio file is empty"


def _trim_silence_tail(audio_path: str, threshold_db: float = -45.0) -> None:
    """In-place trim of trailing silence, with 0.8s fade-out.

    ACE-Step frequently pads generated audio with 3-6 seconds of silence at
    the end. Trimming it prevents a silent tail in the final merged video.
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-of", "default",
             "-show_entries", "format=duration", audio_path],
            capture_output=True, text=True, timeout=10,
        )
        total_dur = None
        for line in probe.stdout.splitlines():
            if line.startswith("duration="):
                try:
                    total_dur = float(line.split("=", 1)[1])
                except ValueError:
                    pass
        if not total_dur or total_dur < 4.0:
            return

        r = subprocess.run(
            ["ffmpeg", "-i", audio_path,
             "-af", f"silencedetect=n={threshold_db}dB:d=0.4",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        starts = [float(m) for m in _re_sil.findall(r"silence_start: ([0-9.]+)", r.stderr)]
        if not starts:
            return
        last_start = starts[-1]
        # Only trim if trailing silence is more than 1.5 seconds
        if total_dur - last_start < 1.5:
            return
        trim_end = min(last_start + 0.3, total_dur)
        fade_start = max(0.0, trim_end - 0.8)
        tmp = audio_path + ".strim"
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-t", f"{trim_end:.3f}",
             "-af", f"afade=t=out:st={fade_start:.3f}:d=0.8",
             tmp],
            capture_output=True, timeout=60,
        )
        if r2.returncode == 0 and Path(tmp).exists():
            import os as _os
            _os.replace(tmp, audio_path)
            log.info("[audio] Trimmed trailing silence %.1fs->%.1fs", total_dur, trim_end)
    except Exception as e:
        log.warning("[audio] Silence trim failed: %s", e)
