"""Detect the NVIDIA GPU at startup and provide hardware-appropriate defaults.

Called once during app.py lifespan. Results are module-level singletons so
routes and services can read them cheaply without re-probing the GPU.

Tier table (VRAM-based):
  tiny  < 6 GB  -- stream everything, minimal models
  low   6-10 GB -- LTX-2 Distilled streams ~1 GB from RAM (profile 4)
  mid  10-15 GB -- LTX-2 Distilled fits cleanly (profile 3)
  high  15+ GB  -- full capability, Wan I2V available (profile 3, large VAE tiles)
"""
import json
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

# Populated once by detect()
vram_gb: float = 0.0
gpu_name: str = "unknown"
tier: str = "low"
wgp_profile: int = 4
vae_cfg: int = 0

# (min_vram_gb, tier_name, wgp_profile, vae_config)
_TIER_DEFS = [
    (0,   "tiny", 4, 0),
    (6,   "low",  4, 0),
    (10,  "mid",  3, 0),
    (15,  "high", 3, 1),
]

# Model auto-pick per tier.  Buckets mirror _AUTO_PICK_SYSTEM in routes.py.
# "high" is the only tier where Wan I2V fits alongside OS/driver overhead.
_PICK_TABLES: dict[str, dict[str, tuple[str, str]]] = {
    "tiny": {k: ("LTX-2 Dev19B Distilled", "calm")
             for k in ("calm", "action", "action_hd", "story_action", "long_story")},
    "low":  {k: ("LTX-2 Dev19B Distilled", "calm")
             for k in ("calm", "action", "action_hd", "story_action", "long_story")},
    "mid":  {k: ("LTX-2 Dev19B Distilled", "calm")
             for k in ("calm", "action", "action_hd", "story_action", "long_story")},
    "high": {
        "calm":         ("LTX-2 Dev19B Distilled", "calm"),
        "action":       ("Wan2.1-I2V-14B-480P",    "dynamic"),
        "action_hd":    ("Wan2.1-I2V-14B-480P",    "dynamic"),
        "story_action": ("Wan2.1-I2V-14B-480P",    "dynamic"),
        "long_story":   ("LTX-2 Dev19B Distilled", "calm"),
    },
}


def detect(wgp_config_path: "Path | None" = None) -> None:
    """Probe the GPU and write optimal WanGP settings. Call once at startup."""
    global vram_gb, gpu_name, tier, wgp_profile, vae_cfg

    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            vram_gb = round(props.total_memory / 1024 ** 3, 1)
            gpu_name = props.name
    except Exception as e:
        _log.warning("GPU detection failed (%s) -- using conservative defaults", e)

    tier = "tiny"
    for min_v, name, prof, vae in _TIER_DEFS:
        if vram_gb >= min_v:
            tier, wgp_profile, vae_cfg = name, prof, vae

    _log.info(
        "GPU: %s  %.1f GB VRAM  →  tier=%s  wgp_profile=%d  vae_config=%d",
        gpu_name, vram_gb, tier, wgp_profile, vae_cfg,
    )

    if wgp_config_path:
        _patch_wgp_config(wgp_config_path)


def _patch_wgp_config(path: Path) -> None:
    """Update profile/vae_config in wgp_config.json if they don't match the GPU tier."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning("wgp_config.json unreadable: %s", e)
        return

    want = {
        "profile":       wgp_profile,
        "video_profile": wgp_profile,
        "image_profile": wgp_profile,
        "vae_config":    vae_cfg,
    }
    changes = {k: v for k, v in want.items() if data.get(k) != v}
    if not changes:
        _log.info("wgp_config.json already optimal for this GPU tier")
        return

    data.update(changes)
    try:
        path.write_text(json.dumps(data, indent=4), encoding="utf-8")
        _log.info("wgp_config.json updated for %s tier: %s", tier, changes)
    except Exception as e:
        _log.warning("Could not write wgp_config.json: %s", e)


def pick_table() -> dict[str, tuple[str, str]]:
    """Return the model auto-pick table for the detected GPU tier."""
    return _PICK_TABLES.get(tier, _PICK_TABLES["low"])


def safe_default_model() -> str:
    """Best single-model default for this GPU (used when auto-pick is off)."""
    return "LTX-2 Dev19B Distilled"


def info() -> dict:
    return {
        "gpu_name":    gpu_name,
        "vram_gb":     vram_gb,
        "tier":        tier,
        "wgp_profile": wgp_profile,
        "vae_config":  vae_cfg,
    }
