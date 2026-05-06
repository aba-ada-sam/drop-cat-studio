"""ACE-Step audio generation client for Fun Videos.

Calls the ACE-Step REST API on port 8019 to generate music/audio.
Based on the battle-tested DropCatGo-Fun-Videos_w_Audio/audio_generator.py.
"""
import json
import logging
import shutil
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ACESTEP_HOST = "127.0.0.1"
ACESTEP_PORT = 8019
API_BASE = f"http://{ACESTEP_HOST}:{ACESTEP_PORT}"

MAX_DURATION = 120
GENERATION_TIMEOUT = 300  # 5 min
POLL_INTERVAL = 3


def _acestep_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{API_BASE}/health", timeout=3):
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
    """
    if not prompt:
        return prompt
    lower = prompt.lower()
    if not any(m in lower for m in _AUTHENTICITY_MARKERS):
        return prompt.rstrip(", ") + ", distinct character, authentic energy, not generic pop"
    return prompt


def _normalize_prompt(prompt: str, instrumental: bool, lyrics: str) -> str:
    normalized = (prompt or "").strip()
    if instrumental or not _lyrics_request_vocals(lyrics):
        return _add_style_guardrails(normalized)

    prompt_lower = normalized.lower()
    vocal_keywords = (
        "vocal", "vocals", "singer", "singing", "sung", "female vocal",
        "male vocal", "choir", "duet", "rap", "spoken word",
    )
    if not any(kw in prompt_lower for kw in vocal_keywords):
        normalized = (normalized.rstrip(", ") + ", lead vocal") if normalized else "lead vocal"
        prompt_lower = normalized.lower()

    # Always nudge vocals to open immediately -- never gate this on [intro] markup.
    # _ensure_intro() is no longer called, but even if lyrics contain [intro] the user
    # wrote it intentionally; the timing hint still helps ACE-Step start vocals promptly.
    pacing_keywords = (
        "from the opening", "from the start", "early vocal", "vocals enter early",
        "immediate vocal", "lead vocal up front", "straight to verse", "no intro",
    )
    if not any(kw in prompt_lower for kw in pacing_keywords):
        normalized = (normalized.rstrip(", ") + ", vocals from the opening, straight to verse") if normalized else "vocals from the opening, straight to verse"

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
        resp = _post(f"{API_BASE}/query_result",
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
        log.info("ACE-Step not running — starting on demand for audio generation...")
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
    effective_prompt = _normalize_prompt(prompt, instrumental, lyrics)
    effective_prompt = effective_prompt or "atmospheric music, organic texture, instrumental, distinct character"

    payload = {
        "prompt": effective_prompt,
        "lyrics": effective_lyrics,
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
            if bpm_int > 0:
                payload["bpm"] = bpm_int
        except (TypeError, ValueError):
            pass

    resp = _post(f"{API_BASE}/release_task", payload, timeout=30)
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
        url = f"{API_BASE}{audio_ref}" if audio_ref.startswith("/") else audio_ref
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
        return str(dest), None

    return None, "Downloaded audio file is empty"
