"""Unified config management for Drop Cat Go Studio.

Merges settings from all original apps into a single namespaced config.
Uses mtime caching (from BRIDGES) for fast repeated reads and thread-safe
writes. Global keys (wan2gp_root, wan_model, resolution, acestep_root) are
shared across features; feature-specific keys use prefixes (i2v_, fun_,
bridge_, sd_, tools_).
"""
import json
import logging
import threading
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"

DEFAULTS: dict = {
    # ── Global (shared across features) ──────────────────────────────────
    "debug_mode": False,            # show service terminal windows when True
    "gpu_job_timeout_seconds": 600, # max seconds before a GPU job is killed
    "wan2gp_root": "",
    "wan2gp_python": "",            # auto-detected if blank
    "wan_model": "LTX-2 Dev19B Distilled",
    "resolution": "580p",
    "acestep_root": "",

    # ── LLM Provider ─────────────────────────────────────────────────────
    "llm_provider": "anthropic",      # anthropic | openai | ollama | auto
    "anthropic_key": "",
    "openai_key": "",

    # ── Image Provider ────────────────────────────────────────────────────
    "image_provider": "forge",        # forge | openai

    # ── Image-to-Video (i2v_) ────────────────────────────────────────────
    "i2v_ken_burns_zoom": 5,        # 0-20 %
    "i2v_img_dur": 3.0,
    "i2v_fade_dur": 0.5,
    "i2v_output_res": "1440x810",
    "i2v_aspect_mode": "auto",      # auto | fixed | source
    "i2v_fit_mode": "contain",      # contain | cover
    "i2v_motion_mode": "random",    # zoom_in | zoom_out | random | still
    "i2v_output_mode": "combined",  # combined | separate
    "i2v_crf": 18,
    "i2v_fps": 30,

    # ── Fun Videos (fun_) ────────────────────────────────────────────────
    "fun_video_duration": 14.0,
    "fun_video_steps": 30,
    "fun_video_guidance": 7.5,
    "fun_video_seed": -1,
    "fun_audio_steps": 8,
    "fun_audio_guidance": 7.0,
    "fun_audio_format": "mp3",
    "fun_audio_instrumental": False,
    "fun_num_prompts": 4,
    "fun_creativity": 8.0,

    # ── Video Bridges (bridge_) ──────────────────────────────────────────
    "bridge_duration": 10.0,
    "bridge_image_duration": 2.5,
    "bridge_steps": 20,
    "bridge_guidance": 10.0,
    "bridge_creativity": 9.0,
    "bridge_transition_mode": "cinematic",
    "bridge_prompt_mode": "ai_informed",
    "bridge_prompt_guidance": "",
    "bridge_start_frame_pos": 0.97,
    "bridge_end_frame_pos": 0.03,
    "bridge_seed": -1,
    "bridge_use_end_frame": True,
    "bridge_allow_fallback": True,
    "bridge_auto_analyze": True,

    # ── SD Prompts (sd_) ─────────────────────────────────────────────────
    "sd_wildcards_dir": "",  # FLW-01: blank default; configure path in Settings
    "sd_model": "ollama",  # uses ollama_power_model via llm_router
    "forge_url": "http://127.0.0.1:7861",
    "forge_default_sampler": "DPM++ 2M SDE",
    "forge_default_scheduler": "Karras",
    "forge_default_steps": 30,
    "forge_default_cfg": 2.5,
    "forge_default_width": 1440,
    "forge_default_height": 810,

    # Step 1 front-door defaults (SD Prompts tab)
    "sd_step1_default_shape":    "single",   # "single" | "regional"
    "sd_step1_default_source":   "vague",    # "vague" | "paste"
    "sd_step1_default_suffix":   "(depth blur)",
    "sd_step1_default_provider": "local",    # "local" (Ollama) | "cloud" (Anthropic/OpenAI)

    # ── Video Tools (tools_) ─────────────────────────────────────────────
    "tools_crf": 18,
    "tools_out_format": "mp4",
    "tools_out_dir": "",

    # ── Ollama (local AI — no API keys required) ─────────────────────────
    # Gemma 4 models for RTX 5080 (16GB VRAM):
    #   gemma4:e4b  (4B, ~9.6 GB)  -- fast, multimodal vision, fits easily
    #   gemma4:26b  (MoE, ~18 GB)  -- power; active params ~4B, may need offload
    # Switch to qwen3-vl:8b / qwen3-vl:30b if Gemma 4 not yet pulled in Ollama.
    "ollama_host":           "http://localhost:11434",
    "ollama_fast_model":     "gemma4:e4b",
    "ollama_balanced_model": "gemma4:e4b",
    "ollama_power_model":    "gemma4:26b",

    # ── AI model aliases (mapped to Ollama) ───────────────────────────────
    "ai_model_fast":     "gemma4:e4b",
    "ai_model_balanced": "gemma4:e4b",
    "ai_model_power":    "gemma4:26b",

}

_lock = threading.RLock()  # BUG-05: RLock so load() inside save() doesn't deadlock
_cache: dict | None = None
_cache_mtime: float = 0.0
_validated = False

_log = logging.getLogger(__name__)


def _validate_config(data: dict) -> dict:
    """Warn on type mismatches vs DEFAULTS and coerce where safe. Returns cleaned dict."""
    for key, value in list(data.items()):
        if key not in DEFAULTS:
            continue
        expected = type(DEFAULTS[key])
        if expected == type(value) or value == "" or DEFAULTS[key] == "":
            continue
        # int/float are interchangeable
        if expected in (int, float) and isinstance(value, (int, float)):
            continue
        _log.warning("[Config] Key '%s' expected %s, got %s (%r) — using default",
                     key, expected.__name__, type(value).__name__, value)
        data[key] = DEFAULTS[key]
    return data


def _invalidate():
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = 0.0


def load() -> dict:
    """Load config with file-mtime caching to avoid repeated disk reads.

    BUG-05: acquire _lock for both the cache check and the cache write to
    prevent TOCTOU races on concurrent requests.
    """
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.exists() else 0.0
        except OSError:
            mtime = 0.0
        if _cache is not None and mtime == _cache_mtime:
            return dict(_cache)
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                global _validated
                if not _validated:
                    data = _validate_config(data)
                    _validated = True
                merged = {**DEFAULTS, **data}
                _cache = merged
                _cache_mtime = mtime
                return dict(merged)
            except Exception:
                pass
        result = dict(DEFAULTS)
        _cache = result
        _cache_mtime = mtime
        return dict(result)


def save(data: dict):
    """Merge data into existing config and write to disk."""
    with _lock:
        current = load()
        current.update(data)
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
        _invalidate()


def get(key: str):
    """Get a single config value."""
    return load().get(key, DEFAULTS.get(key))


def set_val(key: str, value):
    """Set a single config value."""
    save({key: value})


# ── WanGP path helpers ───────────────────────────────────────────────────────

WAN_INDICATORS = ["app.py", "wan_video", "wgp.py", "wgp"]


def find_wan_python(wan_root: str) -> str:
    """Try to find the Python executable inside a Wan2GP/Pinokio install."""
    root = Path(wan_root)
    candidates = [
        root / "env" / "Scripts" / "python.exe",
        root / "env" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        Path(r"C:\pinokio\env\python.exe"),
        Path(r"C:\pinokio\bin\python.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "python"


def validate_wan2gp(path: str) -> tuple[bool, str]:
    """Validate that path looks like a Wan2GP installation."""
    if not path:
        return False, "Path is empty"
    p = Path(path)
    if not p.exists():
        return False, f"Directory not found: {path}"
    if (p / "wgp.py").exists():
        return True, "OK — found wgp.py"
    has_indicator = any((p / ind).exists() for ind in WAN_INDICATORS)
    if not has_indicator:
        return False, (
            f"Does not look like a Wan2GP directory "
            f"(expected wgp.py or one of: {', '.join(WAN_INDICATORS)})"
        )
    return True, "OK — Wan2GP directory found"


# ── ACE-Step path helpers ────────────────────────────────────────────────────

def get_acestep_root() -> Path | None:
    """Return ACE-Step root as Path if configured and exists, else None."""
    path_str = load().get("acestep_root", "").strip()
    if path_str:
        p = Path(path_str)
        if p.exists():
            return p
    return None


def validate_acestep(path: str) -> tuple[bool, str]:
    """Validate that path looks like an ACE-Step installation.

    Accepts both venv-based (.venv/Scripts/python.exe) and uv-based installs
    (where uv run is used instead of activating a venv directly).
    """
    import shutil
    path = path.strip().strip('"').strip("'")
    if not path:
        return False, "No path provided."
    p = Path(path)
    if not p.exists():
        return False, f"Folder not found: {p}"
    api_script = p / "acestep" / "api_server.py"
    if not api_script.exists():
        return False, f"api_server.py not found at: {api_script}"
    # Accept venv-based OR uv-based installs
    python = p / ".venv" / "Scripts" / "python.exe"
    if not python.exists() and not shutil.which("uv"):
        return False, "Neither .venv\\Scripts\\python.exe nor 'uv' found — cannot start ACE-Step"
    return True, f"ACE-Step found at: {p}"


# Common locations to auto-detect ACE-Step without user configuration
ACESTEP_AUTO_DETECT_PATHS = [
    r"C:\DropCatGo-Music\ACE-Step-1.5",
    r"C:\DropCatGo-Music\ACE-Step",
    r"C:\ACE-Step-1.5",
    r"C:\ACE-Step",
]


def auto_detect_acestep() -> str | None:
    """Return path to ACE-Step if found in known locations, else None."""
    for candidate in ACESTEP_AUTO_DETECT_PATHS:
        p = Path(candidate)
        if (p / "acestep" / "api_server.py").exists():
            return str(p)
    return None


# ── Config migration from old apps ──────────────────────────────────────────

_MIGRATION_MAP = {
    # Fun-Videos keys → unified keys
    "video_duration": "fun_video_duration",
    "video_steps": "fun_video_steps",
    "video_guidance": "fun_video_guidance",
    "video_seed": "fun_video_seed",
    "audio_steps": "fun_audio_steps",
    "audio_guidance": "fun_audio_guidance",
    "audio_format": "fun_audio_format",
    "audio_instrumental": "fun_audio_instrumental",
    "num_prompts": "fun_num_prompts",
    # Image2Video keys → unified keys
    "ken_burns_zoom": "i2v_ken_burns_zoom",
    "img_dur": "i2v_img_dur",
    "fade_dur": "i2v_fade_dur",
    "output_res": "i2v_output_res",
    "aspect_mode": "i2v_aspect_mode",
    "fit_mode": "i2v_fit_mode",
    "motion_mode": "i2v_motion_mode",
    "output_mode": "i2v_output_mode",
    "fps": "i2v_fps",
}


def migrate_from_old_apps():
    """Import settings from original app config files (run once on first start)."""
    if CONFIG_FILE.exists():
        return  # already have a config — don't overwrite

    migrated: dict = {}
    ai_editors = CONFIG_FILE.parent.parent  # AI Editors directory

    old_configs = [
        ai_editors / "DropCatGo-Fun-Videos_w_Audio" / "config.json",
        ai_editors / "DropCatGo-Video-BRIDGES" / "config.json",
        ai_editors / "DropCat-Image-2-Video" / "config.json",
    ]

    for old_path in old_configs:
        if not old_path.exists():
            continue
        try:
            old_data = json.loads(old_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        for old_key, value in old_data.items():
            # Map renamed keys
            new_key = _MIGRATION_MAP.get(old_key, old_key)
            # Only import keys we recognize
            if new_key in DEFAULTS and new_key not in migrated:
                migrated[new_key] = value

    if migrated:
        save(migrated)
