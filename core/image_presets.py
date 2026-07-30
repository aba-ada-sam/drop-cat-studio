"""Forge image-generation presets for the Image tab (features/image_studio).

Each preset wraps -- never replaces -- whatever prompt the user typed. The
user's own text is always the subject; a preset only brackets it with a
checkpoint choice, a short style/safety lead-in, and a short negative floor.
See reference_forge_prompt_best_practices: short and concrete beats long and
evocative, so keep every wrap lean.
"""

DEFAULT_CHECKPOINT = "zavychromaxl_v90.safetensors"
NSFW_CHECKPOINT = "perfection25D_illustrious.safetensors"

# Forge prompts are meant to stay well under 75 tokens (see
# reference_forge_prompt_best_practices) -- 2000 chars is already generous.
# Capping here, BEFORE the minor-safety judge ever sees the text, guarantees
# the judge always reviews the full string that actually reaches Forge (the
# judge's own 4000-char window in core/minor_safety.py is otherwise a blind
# spot padding could hide content behind).
MAX_PROMPT_CHARS = 2000

# Proven 2026-07-11 M/F anatomy-fix recipe (perfection25D + light Fumetti for a
# 2.5D semi-realistic look + rating_explicit) plus the locked body-taste and
# safety-floor negatives from user_ai_art_style.
_NSFW_POSITIVE_LEAD = (
    "<lora:nemo_fumetti_eurocomic:0.6>, rating_explicit, "
    "(2.5D semi-realistic illustration:1.15),"
)
_NSFW_POSITIVE_TRAIL = (
    "adult woman, thick, stout, broad-shouldered, sturdy solid build, "
    "realistic natural proportions, modest natural bust, mature, perfect hands, normal feet"
)
_NSFW_NEGATIVE_FLOOR = (
    "anime, chibi, cel shading, waifu, petite, loli, child, teen, teenager, "
    "young girl, youthful, underage, malformed genitalia, deformed penis, extra penis, "
    "extra limbs, fused limbs, deformed, mutated hands, extra fingers"
)

PRESETS = {
    "default": {
        "label": "Default",
        "description": "Whatever checkpoint is normally loaded (zavychromaxl) -- no wrap, no ADetailer.",
        "checkpoint": DEFAULT_CHECKPOINT,
        "positive_lead": "",
        "positive_trail": "",
        "negative_floor": "",
        "adetailer": False,
        "nsfw": False,
    },
    "nsfw": {
        "label": "NSFW (Explicit)",
        "description": "perfection25D_illustrious + nemo_fumetti_eurocomic 2.5D recipe, body-taste + safety-floor wrap, ADetailer face fix.",
        "checkpoint": NSFW_CHECKPOINT,
        "positive_lead": _NSFW_POSITIVE_LEAD,
        "positive_trail": _NSFW_POSITIVE_TRAIL,
        "negative_floor": _NSFW_NEGATIVE_FLOOR,
        "adetailer": True,
        "nsfw": True,
    },
}

DEFAULT_PRESET_KEY = "default"


def list_presets() -> list[dict]:
    return [
        {"id": key, "label": p["label"], "description": p["description"], "nsfw": p["nsfw"]}
        for key, p in PRESETS.items()
    ]


def get_preset(preset_key) -> dict:
    if not isinstance(preset_key, str):
        return PRESETS[DEFAULT_PRESET_KEY]
    return PRESETS.get(preset_key, PRESETS[DEFAULT_PRESET_KEY])


def clamp_prompt_text(text: str) -> str:
    return (text or "")[:MAX_PROMPT_CHARS]


def _join(*parts: str) -> str:
    return ", ".join(p.strip().strip(",").strip() for p in parts if p and p.strip())


def build_forge_payload(
    preset_key: str,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    steps: int,
    cfg_scale: float,
    seed: int,
) -> dict:
    """Build a Forge /sdapi/v1/txt2img payload for the given preset."""
    preset = get_preset(preset_key)

    final_prompt = _join(preset["positive_lead"], clamp_prompt_text(prompt), preset["positive_trail"])
    final_negative = _join(preset["negative_floor"], clamp_prompt_text(negative_prompt))

    payload = {
        "prompt": final_prompt,
        "negative_prompt": final_negative,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "seed": seed,
        "batch_size": 1,
        "sampler_name": "DPM++ 3M SDE",
        "override_settings": {"sd_model_checkpoint": preset["checkpoint"]},
        # Left at Forge's default (True): a request-scoped override that
        # restores whatever checkpoint was loaded before this call. False was
        # tried as a speed optimization (avoids a double swap on consecutive
        # same-preset renders) but it left the NSFW checkpoint loaded on Forge
        # afterward -- and Chat Studio's separate, ungated
        # /api/chat-studio/generate-image endpoint has no checkpoint override
        # or safety judge of its own, so it silently inherited whatever Forge
        # last had loaded. Correctness over speed here.
        "override_settings_restore_afterwards": True,
    }
    if preset["adetailer"]:
        payload["alwayson_scripts"] = {
            "ADetailer": {"args": [{"ad_model": "mediapipe_face_full", "ad_mask_k": 1}]},
        }
    return payload
