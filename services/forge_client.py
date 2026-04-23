"""Stable Diffusion Forge WebUI API client.

Connects to a local Forge instance (A1111-compatible API) on port 7861.
Supports all major Forge features: HiRes Fix, ADetailer, Forge Couple,
LoRAs, all samplers/schedulers, and the full alwayson_scripts extension API.
"""
import base64
import json
import logging
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# BUG-12: keep FORGE_PORT as a constant (used by manager.py for status display).
# The URL is read from config at call time via _forge_url() so that changes to
# the Settings > Forge URL field actually take effect without a restart.
FORGE_PORT = 7861
_FORGE_DEFAULT = f"http://127.0.0.1:{FORGE_PORT}"


def _forge_url() -> str:
    """Return the current Forge base URL from config (hot-reloads on Settings change)."""
    try:
        from core import config as _cfg
        return (_cfg.get("forge_url") or _FORGE_DEFAULT).rstrip("/")
    except Exception:
        return _FORGE_DEFAULT


def _api_base() -> str:
    return f"{_forge_url()}/sdapi/v1"


# ── Health / discovery ────────────────────────────────────────────────────────

def forge_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{_api_base()}/samplers", timeout=3):
            return True
    except Exception:
        return False


def unload_checkpoint() -> bool:
    """Ask Forge to release its model from VRAM. Returns True on success."""
    try:
        req = urllib.request.Request(
            f"{_api_base()}/unload-checkpoint",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        log.info("Forge checkpoint unloaded from VRAM")
        return True
    except Exception as e:
        log.debug("Forge unload skipped (Forge not running or error): %s", e)
        return False


def reload_checkpoint() -> bool:
    """Ask Forge to reload its model back into VRAM. Returns True on success."""
    try:
        req = urllib.request.Request(
            f"{_api_base()}/reload-checkpoint",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30):
            pass
        log.info("Forge checkpoint reloaded into VRAM")
        return True
    except Exception as e:
        log.debug("Forge reload failed: %s", e)
        return False


def get_models() -> list[dict]:
    """Return list of available checkpoint models."""
    try:
        with urllib.request.urlopen(f"{_api_base()}/sd-models", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return []


def get_samplers() -> list[str]:
    """Return list of available sampler names (live from Forge)."""
    try:
        with urllib.request.urlopen(f"{_api_base()}/samplers", timeout=5) as r:
            return [s["name"] for s in json.loads(r.read())]
    except Exception:
        return ["DPM++ 2M SDE", "Euler", "DPM++ 2M Karras", "DDIM"]


def get_schedulers() -> list[str]:
    """Return list of available scheduler/noise schedule names."""
    try:
        with urllib.request.urlopen(f"{_api_base()}/schedulers", timeout=5) as r:
            return [s["name"] for s in json.loads(r.read())]
    except Exception:
        return ["Karras", "Automatic", "Exponential", "Polyexponential"]


def get_loras() -> list[dict]:
    """Return list of available LoRA models."""
    try:
        with urllib.request.urlopen(f"{_api_base()}/loras", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return []


def get_upscalers() -> list[str]:
    """Return list of available upscaler names."""
    try:
        with urllib.request.urlopen(f"{_api_base()}/upscalers", timeout=5) as r:
            return [u["name"] for u in json.loads(r.read())]
    except Exception:
        return ["ESRGAN_4x", "Latent", "None"]


def get_current_model() -> str:
    """Return name of the currently loaded checkpoint."""
    try:
        with urllib.request.urlopen(f"{_api_base()}/options", timeout=5) as r:
            opts = json.loads(r.read())
            return opts.get("sd_model_checkpoint", "")
    except Exception:
        return ""


def get_options() -> dict:
    """Return full Forge options dict."""
    try:
        with urllib.request.urlopen(f"{_api_base()}/options", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def set_model(model_name: str) -> bool:
    """Switch the loaded checkpoint model."""
    try:
        payload = json.dumps({"sd_model_checkpoint": model_name}).encode()
        req = urllib.request.Request(
            f"{_api_base()}/options",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30):
            return True
    except Exception:
        return False


def get_progress() -> dict:
    try:
        with urllib.request.urlopen(f"{_api_base()}/progress", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return {"progress": 0, "state": {"job": ""}}


def interrupt() -> bool:
    try:
        req = urllib.request.Request(f"{_api_base()}/interrupt", method="POST")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


# ── Extension helpers ─────────────────────────────────────────────────────────

def build_adetailer_args(
    enabled: bool = True,
    sweeps: list | None = None,
    # Legacy single-model params kept for backward compatibility
    model: str = "face_yolov8n.pt",
    confidence: float = 0.3,
    mask_blur: int = 4,
    denoising_strength: float = 0.4,
) -> dict:
    """Build alwayson_scripts entry for ADetailer. Supports 1–4 sweeps.

    Each sweep dict may contain:
      model, confidence, mask_blur, denoise, inpaint_only_masked, padding,
      min_ratio, max_ratio, prompt, negative_prompt
    """
    if not enabled:
        return {}
    if not sweeps:
        sweeps = [{
            "model": model, "confidence": confidence,
            "mask_blur": mask_blur, "denoise": denoising_strength,
        }]
    args = [True, False]  # enabled, skip_img2img
    for s in sweeps[:4]:
        cfg = {
            "ad_model":                    s.get("model",      "face_yolov8n.pt"),
            "ad_confidence":               float(s.get("confidence", 0.3)),
            "ad_mask_blur":                int(s.get("mask_blur",   4)),
            "ad_denoising_strength":       float(s.get("denoise",   0.4)),
            "ad_inpaint_only_masked":      bool(s.get("inpaint_only_masked", True)),
            "ad_inpaint_only_masked_padding": int(s.get("padding",  32)),
            "ad_mask_min_ratio":           float(s.get("min_ratio", 0.0)),
            "ad_mask_max_ratio":           float(s.get("max_ratio", 1.0)),
        }
        if s.get("prompt"):
            cfg["ad_prompt"] = s["prompt"]; cfg["ad_use_prompt"] = True
        if s.get("negative_prompt"):
            cfg["ad_negative_prompt"] = s["negative_prompt"]; cfg["ad_use_negative_prompt"] = True
        args.append(cfg)
    return {"ADetailer": {"args": args}}


def build_forge_couple_args(
    enabled: bool = True,
    mode: str = "Basic",
    direction: str = "Horizontal",
    background: str = "First Line",
    background_weight: float = 0.5,
    separator: str = "\n",
) -> dict:
    """Build alwayson_scripts entry for Forge Couple (regional prompting).

    When enabled, the prompt is split by separator into regional sections.
    Default separator is newline — each line targets a spatial region.

    NOTE: When using Forge Couple with our 3-column SD prompts, join
    columns with '\\n' (not BREAK). The BREAK keyword is for standard
    regional conditioning; Forge Couple uses its own separator.
    """
    if not enabled:
        return {}
    return {
        "forge couple": {
            "args": [
                True,               # enabled
                mode,               # "Basic" or "Advanced"
                direction,          # "Horizontal" or "Vertical"
                background,         # "First Line", "Last Line", or "None"
                background_weight,  # weight for background region
                separator,          # separator character
                False,              # compatibility mode
                "{}",               # extra config JSON
            ]
        }
    }


def build_ultimate_upscale_args(
    enabled: bool = False,
    scale: float = 2.0,
    upscaler: str = "ESRGAN_4x",
    tile_width: int = 512,
    tile_height: int = 512,
    padding: int = 32,
    seams_fix_type: int = 0,
) -> dict:
    """Build script args for Ultimate SD Upscale."""
    if not enabled:
        return {}
    return {
        "script_name": "ultimate sd upscale",
        "script_args": [
            None,           # _
            tile_width,
            tile_height,
            8,              # mask_blur
            padding,
            64,             # seams_fix_width
            0.35,           # seams_fix_denoise
            padding,        # seams_fix_padding
            upscaler,
            True,           # save_upscaled_image
            seams_fix_type,
            0,              # seams_fix_direction
            True,           # save_seams_fix_image
        ]
    }


# ── Generation ────────────────────────────────────────────────────────────────

def txt2img(
    prompt: str,
    negative_prompt: str = "",
    # Core
    width: int = 1440,
    height: int = 810,
    steps: int = 25,
    sampler_name: str = "DPM++ 2M SDE",
    scheduler: str = "Karras",
    cfg_scale: float = 7.0,
    seed: int = -1,
    batch_size: int = 1,
    # HiRes Fix
    enable_hr: bool = False,
    hr_scale: float = 2.0,
    hr_upscaler: str = "ESRGAN_4x",
    hr_second_pass_steps: int = 10,
    hr_denoising_strength: float = 0.3,
    # Extensions (alwayson_scripts)
    adetailer: dict | None = None,       # from build_adetailer_args()
    forge_couple: dict | None = None,    # from build_forge_couple_args()
    extra_scripts: dict | None = None,   # any additional alwayson_scripts
    # Misc
    restore_faces: bool = False,
    save_images: bool = False,
    **kwargs,
) -> dict:
    """Generate image(s) from text prompt with full Forge feature support.

    Returns: {"images": [base64_str, ...], "info": dict, "error": str|None}
    """
    # Merge alwayson_scripts from all sources
    alwayson = {}
    if adetailer:
        alwayson.update(adetailer)
    if forge_couple:
        alwayson.update(forge_couple)
    if extra_scripts:
        alwayson.update(extra_scripts)

    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "sampler_name": sampler_name,
        "scheduler": scheduler,
        "cfg_scale": cfg_scale,
        "seed": seed,
        "batch_size": batch_size,
        "n_iter": 1,
        "restore_faces": restore_faces,
        "save_images": save_images,
        "send_images": True,
    }

    if enable_hr:
        payload.update({
            "enable_hr": True,
            "hr_scale": hr_scale,
            "hr_upscaler": hr_upscaler,
            "hr_second_pass_steps": hr_second_pass_steps,
            "denoising_strength": hr_denoising_strength,
        })

    if alwayson:
        payload["alwayson_scripts"] = alwayson

    payload.update(kwargs)

    return _call_api("/txt2img", payload, timeout=600)


def img2img(
    init_image_b64: str,
    prompt: str,
    negative_prompt: str = "",
    denoising_strength: float = 0.5,
    # Core
    width: int = 1440,
    height: int = 810,
    steps: int = 25,
    sampler_name: str = "DPM++ 2M SDE",
    scheduler: str = "Karras",
    cfg_scale: float = 7.0,
    seed: int = -1,
    # Resize mode: 0=just resize, 1=crop+resize, 2=resize+fill, 3=just resize(latent)
    resize_mode: int = 0,
    # Extensions
    adetailer: dict | None = None,
    forge_couple: dict | None = None,
    extra_scripts: dict | None = None,
    restore_faces: bool = False,
    save_images: bool = False,
    **kwargs,
) -> dict:
    """Refine/transform an image using img2img.

    Returns: {"images": [base64_str, ...], "info": dict, "error": str|None}
    """
    alwayson = {}
    if adetailer:
        alwayson.update(adetailer)
    if forge_couple:
        alwayson.update(forge_couple)
    if extra_scripts:
        alwayson.update(extra_scripts)

    payload = {
        "init_images": [init_image_b64],
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "denoising_strength": denoising_strength,
        "width": width,
        "height": height,
        "steps": steps,
        "sampler_name": sampler_name,
        "scheduler": scheduler,
        "cfg_scale": cfg_scale,
        "seed": seed,
        "resize_mode": resize_mode,
        "batch_size": 1,
        "n_iter": 1,
        "restore_faces": restore_faces,
        "save_images": save_images,
        "send_images": True,
    }

    if alwayson:
        payload["alwayson_scripts"] = alwayson

    payload.update(kwargs)

    return _call_api("/img2img", payload, timeout=600)


# ── Internal ──────────────────────────────────────────────────────────────────

def _call_api(endpoint: str, payload: dict, timeout: int = 300) -> dict:
    """Make a POST call to the Forge API and normalize the response."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_api_base()}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read())

        info = {}
        if result.get("info"):
            try:
                info = json.loads(result["info"])
            except (json.JSONDecodeError, TypeError):
                info = {"raw": result["info"]}

        return {
            "images": result.get("images", []),
            "info": info,
            "error": None,
        }
    except Exception as e:
        return {"images": [], "info": {}, "error": str(e)}


def save_image(b64_data: str, output_dir: str, filename: str = "") -> str | None:
    """Save a base64-encoded image to disk. Returns file path."""
    if not filename:
        filename = f"forge_{time.strftime('%Y%m%d_%H%M%S')}.png"
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    try:
        out_path.write_bytes(base64.b64decode(b64_data))
        return str(out_path)
    except Exception as e:
        log.error("Failed to save image: %s", e)
        return None
