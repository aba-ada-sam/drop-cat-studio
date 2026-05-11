"""Fun Videos API routes -- /api/fun/*

Photo -> AI video + audio pipeline with wildcard support.
"""
import asyncio
import io
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from PIL import Image

from core import config as cfg
from core.job_manager import JOB_FUN_VIDEO, JOB_FUN_MULTI_VIDEO
from core.llm_client import encode_image_b64
from core.wangp_models import resolve_model_name
from features.fun_videos.video_generator import MODELS

log = logging.getLogger(__name__)
router = APIRouter()

UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
MAX_IMAGE_MB = 15


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_path(raw: str) -> str:
    """Resolve a URL-style path like /output/file.png to an absolute filesystem path."""
    if not raw or os.path.isfile(raw):
        return raw
    clean = raw.lstrip("/").replace("/", os.sep)
    if clean.startswith(f"output{os.sep}") or clean.startswith("output/"):
        resolved = str(_PROJECT_ROOT / clean.replace("/", os.sep))
        if os.path.isfile(resolved):
            return resolved
    return raw


# Banned motion words that wreck downstream video generation. The story-arc
# system prompt forbids these, but Sonnet/Haiku still slip them in occasionally.
# We rewrite at the brainstorm stage so the user-visible idea stays clean.
_BANNED_MOTION_REWRITES = {
    "transforms into": "shifts toward",
    "transforms":      "shifts",
    "becomes":         "settles into being",
    "reveals":         "shows",
    "establishes":     "holds",
    "unfolds":         "plays out",
    "snaps to":        "moves to",
    "the camera":      "the frame",
    "we see":          "visible",
}


def _scrub_banned_motion_words(text: str) -> str:
    """Replace banned action words with neutral equivalents (case-insensitive)."""
    import re
    if not text:
        return text
    out = text
    for bad, good in _BANNED_MOTION_REWRITES.items():
        out = re.sub(re.escape(bad), good, out, flags=re.IGNORECASE)
    return out


def _validate_image(data: bytes, filename: str) -> Image.Image:
    """Validate image data. Returns the PIL Image on success, raises HTTPException on failure."""
    if len(data) > MAX_IMAGE_MB * 1024 * 1024:
        raise HTTPException(422, f"Image '{filename}' exceeds {MAX_IMAGE_MB}MB limit.")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        # Re-open after verify (verify leaves the file in an unusable state)
        img = Image.open(io.BytesIO(data))
    except Exception:
        raise HTTPException(422, f"File '{filename}' is not a valid image.")
    return img


_LORA_DIR_MAP = {
    "i2v":            "wan_i2v",
    "i2v_720p":       "wan_i2v",
    "t2v":            "wan",
    "t2v_1.3B":       "wan_1.3B",
    "ltx2_distilled": "ltx2",
    "vace_1.3B":      "wan_5B",
}


@router.get("/loras")
async def list_loras(model: str = ""):
    """Return available LoRA .safetensors files for the given model."""
    wan_root = cfg.get("wan2gp_root") or ""
    if not wan_root:
        return {"loras": [], "directory": ""}
    model_type = resolve_model_name(model) if model else "i2v"
    subdir = _LORA_DIR_MAP.get(model_type, "wan")
    lora_dir = Path(wan_root) / "loras" / subdir
    loras = []
    if lora_dir.exists():
        for f in sorted(lora_dir.glob("*.safetensors")):
            loras.append({"name": f.stem, "path": str(f)})
    return {"loras": loras, "directory": str(lora_dir)}


@router.post("/upload")
async def upload_photo(files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in IMAGE_EXTS:
            continue
        data = await f.read()
        img = _validate_image(data, f.filename or "unknown")
        dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        dest.write_bytes(data)
        saved.append({
            "path": str(dest),
            "name": f.filename,
            "width": img.size[0],
            "height": img.size[1],
            "url": f"/uploads/{dest.name}",
        })
    return {"files": saved}


@router.post("/upload-video")
async def upload_video(files: list[UploadFile] = File(...)):
    """Upload a video file to use as WanGP video-to-video source."""
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in VIDEO_EXTS:
            continue
        data = await f.read()
        dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{f.filename}"
        dest.write_bytes(data)
        saved.append({
            "path": str(dest),
            "name": f.filename,
            "url": f"/uploads/{dest.name}",
        })
    return {"files": saved}


def _require_ai():
    """Raise HTTP 503 if no AI provider is available."""
    from core import config as cfg
    from core.keys import get_key, status as ollama_status, get_ollama_models
    provider = cfg.get("llm_provider") or "auto"
    if provider in ("anthropic", "openai"):
        if not get_key(provider):
            raise HTTPException(503, f"{provider.title()} selected but no API key configured")
        return
    if provider == "ollama":
        # Soft check: if Ollama is reachable, verify the model is installed.
        # If Ollama is temporarily unreachable (GPU pressure, etc.), don't
        # pre-fail -- let the actual LLM call handle retries.
        st = ollama_status()
        if st.get("available"):
            needed = cfg.get("ollama_balanced_model") or "qwen3-vl:8b"
            installed = get_ollama_models()
            if installed and not any(m.startswith(needed.split(":")[0]) for m in installed):
                raise HTTPException(
                    503,
                    f"Vision model '{needed}' not found in Ollama. "
                    f"Installed: {', '.join(installed)}. "
                    f"Run: ollama pull {needed}"
                )
        return
    # auto mode: need at least one working provider
    if get_key("anthropic") or get_key("openai"):
        return
    st = ollama_status()
    if not st.get("available"):
        raise HTTPException(503, "No AI provider available -- add an Anthropic/OpenAI key or start Ollama")
    needed = cfg.get("ollama_balanced_model") or "qwen3-vl:8b"
    installed = get_ollama_models()
    if installed and not any(m.startswith(needed.split(":")[0]) for m in installed):
        raise HTTPException(
            503,
            f"Vision model '{needed}' not found in Ollama. "
            f"Installed: {', '.join(installed)}. "
            f"Run: ollama pull {needed}"
        )


@router.post("/analyze-photo")
async def analyze_photo(request: Request):
    from app import get_llm_router; llm_router = get_llm_router()
    from features.fun_videos.analyzer import analyze_photo as _analyze

    _require_ai()
    body = await request.json()
    image_path = body.get("image_path", "")
    if not image_path or not os.path.isfile(image_path):
        raise HTTPException(400, "Image not found")

    _validate_image(Path(image_path).read_bytes(), Path(image_path).name)
    b64 = encode_image_b64(image_path)
    if not b64:
        raise HTTPException(500, "Failed to encode image")

    return await asyncio.to_thread(_analyze, llm_router, b64)


@router.post("/generate-prompts")
async def generate_prompts(request: Request):
    from app import get_llm_router; llm_router = get_llm_router()
    from features.fun_videos.analyzer import generate_video_prompts

    _require_ai()
    body = await request.json()
    image_path = _resolve_path(body.get("image_path", ""))
    if not image_path or not os.path.isfile(image_path):
        raise HTTPException(400, f"Image not found: {image_path}")

    _validate_image(Path(image_path).read_bytes(), Path(image_path).name)
    b64 = encode_image_b64(image_path)
    if not b64:
        raise HTTPException(500, "Failed to encode image")

    config = cfg.load()
    num_prompts = int(body.get("num_prompts", config.get("fun_num_prompts", 4)))
    creativity   = float(body.get("creativity", config.get("fun_creativity", 8.0)))
    user_dir     = body.get("user_direction", "")
    max_tokens   = int(body.get("max_tokens", 400 if num_prompts == 1 else 2048))
    force_prov   = body.get("provider") or None

    try:
        return await asyncio.to_thread(
            generate_video_prompts,
            llm_router,
            b64,
            user_direction=user_dir,
            num_prompts=num_prompts,
            creativity=creativity,
            max_tokens=max_tokens,
            force_provider=force_prov,
        )
    except RuntimeError as e:
        msg = str(e)
        if "unparseable" in msg.lower() or "not able to" in msg.lower() or "won't produce" in msg.lower():
            raise HTTPException(422, "AI declined to generate prompts for this image.")
        raise HTTPException(500, msg)
    except Exception:
        # Ollama vision timed out or failed -- fall back to text-only prompt generation
        # so the user still gets a usable result without needing to restart.
        from features.fun_videos.analyzer import generate_video_prompt_auto
        log.warning("Vision prompt generation failed -- falling back to text-only auto-prompt")
        try:
            text = await asyncio.to_thread(
                generate_video_prompt_auto, llm_router, user_dir,
            )
            if text:
                prompt_obj = {"label": "Auto", "prompt": text, "mood": "dynamic", "style": "kinetic"}
                return {"prompts": [prompt_obj] * num_prompts}
        except Exception as fb_e:
            log.warning("Text-only auto-prompt also failed: %s", fb_e)
        raise HTTPException(503, "Motion prompt generation timed out. Type a prompt manually or try again.")


@router.post("/refine-prompt")
async def refine_prompt(request: Request):
    """Refine an existing motion prompt based on user feedback."""
    from app import get_llm_router; llm_router = get_llm_router()
    from core.llm_client import TIER_FAST

    body = await request.json()
    current_prompt = body.get("current_prompt", "").strip()
    feedback       = body.get("feedback", "").strip()
    image_path     = _resolve_path(body.get("image_path", ""))

    if not feedback:
        raise HTTPException(400, "Feedback is required")
    if not current_prompt:
        raise HTTPException(400, "Current prompt is required")

    system = (
        "You are a cinematic video prompt writer. "
        "Given an existing motion prompt and the user's feedback, "
        "write an improved version. Return only the refined prompt text -- no explanation, no quotes."
    )
    user_msg = f"Current prompt:\n{current_prompt}\n\nUser feedback:\n{feedback}\n\nWrite an improved prompt:"

    def _refine():
        if image_path and os.path.isfile(image_path):
            from core.llm_client import encode_image_b64
            b64 = encode_image_b64(image_path)
            if b64:
                return llm_router.route_vision(user_msg, [b64], tier=TIER_FAST, system=system, max_tokens=200, force_provider="ollama")
        return llm_router.route([{"role": "user", "content": user_msg}], tier=TIER_FAST, system=system, max_tokens=200)

    try:
        result = await asyncio.to_thread(_refine)
        return {"prompt": (result or "").strip()}
    except Exception as e:
        raise HTTPException(500, str(e))


# -- Auto-pick model -----------------------------------------------------------

# Maps the LLM's classification verdict to a (model_name, motion_style) pair.
# These are the same six models advertised in MODELS / wangp_models.py.
#
#   calm        -> LTX-2 Distilled (calm). Fast, holds the source photo, ideal
#                  for breathing-photograph / observational scenes.
#   action      -> Wan2.1-I2V-14B-480P (dynamic). Strong subject anchoring
#                  through kinetic motion. The model that survives action verbs.
#   action_hd   -> Wan2.1-I2V-14B-720P (dynamic). Same character as action but
#                  720p output. Picked only when the LLM thinks delivery quality
#                  matters more than speed.
#   story_action -> Wan2.1-I2V-14B-480P (dynamic). Motion for illustrated/stylized
#                  subjects. LTX-2 Dev13B was preferred here but requires >16GB
#                  VRAM and times out on RTX 5080 (16GB). Wan I2V is the fallback.
#   long_story  -> LTX-2 Distilled (calm). Still/atmospheric multi-clip stories.
#                  Dev13B was preferred but is VRAM-incompatible; Distilled runs
#                  fine and preserves identity well for calm-motion clips.
#
# T2V models are intentionally NOT in the auto-pick set: every Express/Fun-Videos
# job has a photo, so I2V is always the right family. T2V stays manual-only.

_PICK_TO_MODEL = {
    "calm":         ("LTX-2 Dev19B Distilled", "calm"),
    "action":       ("Wan2.1-I2V-14B-480P",    "dynamic"),
    "action_hd":    ("Wan2.1-I2V-14B-720P",    "dynamic"),
    "story_action": ("Wan2.1-I2V-14B-480P",    "dynamic"),
    "long_story":   ("LTX-2 Dev19B Distilled", "calm"),
}

_AUTO_PICK_SYSTEM = """You are picking the best AI video model for a user's idea.

You have five choices. Pick the ONE that fits best.

  action
    Wan2.1 I2V 480P. For REAL PHOTOS of people, animals, or objects doing
    kinetic things -- talking, gesturing, moving, dancing. Strong subject
    anchoring for photographic sources. DO NOT use for paintings, illustrations,
    fantasy art, or subjects made of complex organic/fantastical materials.

  action_hd
    Wan2.1 I2V 720P. Same as 'action' but higher resolution. Pick ONLY when
    the user explicitly asks for HD/sharp/professional output AND the source
    is a real photo with kinetic intent.

  story_action
    Wan I2V 480P with dynamic motion. For PAINTED, ILLUSTRATED, FANTASY, or
    STYLIZED images where the subject should move. Also use for any multi-clip
    (3+ clips) job with a complex or detailed subject, even if the source is
    a real photo. Wan I2V anchors the subject well for motion across clips.

  calm
    LTX-2 Distilled. Subject does NOT move -- only environment moves (light
    shifting, steam rising, water rippling). For atmospheric/still-life ideas
    only: a misty landscape, a quiet object, an explicitly "breathing
    photograph" mood. Do NOT pick just because the photo is a portrait.

  long_story
    LTX-2 Distilled, subject still. For multi-clip atmospheric stories where
    the scene must be preserved and nothing should move.

CRITICAL RULE: If the source image looks like a painting, illustration, digital
art, fantasy scene, or contains a subject made of non-photographic material
(plants, fire, crystal, smoke, leaves, fantasy textures, etc.) -- pick
'story_action' if the idea involves motion, or 'calm' if the idea is still.
NEVER pick 'action' or 'action_hd' for painted/illustrated sources.

When in doubt and the source is a real photo, pick 'action'.
When in doubt and the source is illustrated/painted, pick 'story_action'.

Return ONLY this JSON, no other text:
{"pick": "action" | "action_hd" | "story_action" | "calm" | "long_story", "reason": "one short sentence"}
"""


def _auto_pick_model(
    llm_router,
    idea: str,
    photo_b64: str | None = None,
    n_clips: int = 1,
    user_requested_hd: bool = False,
) -> tuple[str, str, str]:
    """Classify the user's idea and return (model_name, motion_style, reason).

    Fallback default is the 'action' bucket (Wan I2V 480P + dynamic motion),
    NOT calm. When the classifier fails, ship motion -- stationary output is
    the worst possible failure mode for a video tool. If the user actually
    wanted a still-life mood, they can toggle Auto-pick off and choose Photo
    Mood manually.
    """
    from core.llm_client import TIER_FAST, parse_json_response

    idea_clean = (idea or "").strip()
    if not idea_clean and not photo_b64:
        # No idea, no photo -- can't classify. Ship motion-by-default.
        return ("Wan2.1-I2V-14B-480P", "dynamic", "no idea -- action default")

    user_msg = (
        f"User idea: {idea_clean or '(no explicit idea given)'}\n"
        f"Number of clips planned: {n_clips}\n"
        f"User asked for HD/high-quality output: {'yes' if user_requested_hd else 'no'}\n\n"
        f"Pick the best model."
    )

    def _try_parse(text: str) -> tuple[str, str] | None:
        """Return (pick, reason) on success, None on failure."""
        data = parse_json_response(text)
        pick = (data or {}).get("pick", "").strip().lower() if isinstance(data, dict) else ""
        reason = (data or {}).get("reason", "") if isinstance(data, dict) else ""
        if pick in _PICK_TO_MODEL:
            return (pick, reason or pick)
        return None

    def _apply_clip_guard(pick: str, reason: str) -> tuple[str, str]:
        """Wan I2V 720P can drift across 4+ clips -- downgrade to 480P which has
        slightly better image conditioning for longer chains."""
        if pick == "action_hd" and n_clips >= 4:
            log.info(
                "[auto-pick] clip-count guard: %d clips with Wan 720P -> 480P",
                n_clips,
            )
            return ("action", f"Wan 720P drifts across {n_clips} clips -- 480P is more stable")
        return (pick, reason)

    # Step 1: vision call via Ollama (NSFW-safe, photo stays on-device).
    if photo_b64:
        try:
            text = llm_router.route_vision(
                user_msg, [photo_b64],
                tier=TIER_FAST, system=_AUTO_PICK_SYSTEM, max_tokens=120,
                force_provider="ollama", format_json=True,
            )
            log.info("[auto-pick] ollama vision returned %d chars: %r", len(text or ""), (text or "")[:200])
            parsed = _try_parse(text or "")
            if parsed:
                pick, reason = parsed
                pick, reason = _apply_clip_guard(pick, reason)
                model, motion = _PICK_TO_MODEL[pick]
                log.info("[auto-pick] '%s' -> %s (%s) -- %s", idea_clean[:60], model, motion, reason)
                return (model, motion, reason)
            log.warning("[auto-pick] ollama vision returned no usable pick -- trying text-only cloud")
        except Exception as e:
            log.warning("[auto-pick] ollama vision failed (%s) -- trying text-only cloud", e)

    # Step 2: text-only fallback. Safe to use cloud (Anthropic/OpenAI) since we
    # never send the photo on this path -- only the user's idea text. If the
    # primary provider chain runs cloud-first the request goes to Sonnet/GPT;
    # otherwise it hits Ollama text again, which is more reliable than vision.
    try:
        text = llm_router.route(
            [{"role": "user", "content": user_msg}],
            tier=TIER_FAST, system=_AUTO_PICK_SYSTEM, max_tokens=120,
        )
        log.info("[auto-pick] text fallback returned %d chars: %r", len(text or ""), (text or "")[:200])
        parsed = _try_parse(text or "")
        if parsed:
            pick, reason = parsed
            pick, reason = _apply_clip_guard(pick, reason)
            model, motion = _PICK_TO_MODEL[pick]
            log.info("[auto-pick] '%s' -> %s (%s) -- %s [text fallback]", idea_clean[:60], model, motion, reason)
            return (model, motion, reason)
        log.warning("[auto-pick] text fallback returned no usable pick -- defaulting to action")
    except Exception as e:
        log.warning("[auto-pick] text fallback failed (%s) -- defaulting to action", e)

    # Step 3: hard fallback -- Wan I2V 480P works on all supported hardware.
    return ("Wan2.1-I2V-14B-480P", "dynamic", "fallback-action")


@router.post("/make-it")
async def make_it(request: Request):
    from app import get_job_manager; job_manager = get_job_manager()
    from features.fun_videos.pipeline import run_prep, run_pipeline

    body = await request.json()
    photo_path = _resolve_path(body.get("photo_path") or "")
    if photo_path and not os.path.isfile(photo_path):
        raise HTTPException(400, f"Photo not found: {photo_path}")
    if not photo_path and not body.get("video_prompt", "").strip():
        raise HTTPException(400, "Provide either a photo or a video prompt")

    config = cfg.load()
    requested_model = body.get("model") or config.get("wan_model") or "LTX-2 Dev19B Distilled"

    # -- Auto-pick model (off by default for Fun Videos -- advanced users) ------
    # When the caller sets auto_pick_model=True, classify the user's idea via a
    # fast LLM call and override the model + motion style with the best match.
    # Falls back silently to the requested model on any LLM error.
    auto_picked_motion = None
    if body.get("auto_pick_model"):
        from app import get_llm_router
        photo_b64 = None
        if photo_path and os.path.isfile(photo_path):
            try:
                photo_b64 = encode_image_b64(photo_path)
            except Exception:
                photo_b64 = None
        picked_model, picked_motion, pick_reason = await asyncio.to_thread(
            _auto_pick_model,
            get_llm_router(),
            body.get("video_prompt", ""),
            photo_b64,
            1,  # single-clip
            False,  # HD-intent inferred from idea text only for now
        )
        requested_model = picked_model
        auto_picked_motion = picked_motion
        log.info("[make-it] auto-pick chose %s (%s) -- %s", picked_model, picked_motion, pick_reason)

    # Reject T2V + photo upfront -- WanGP would otherwise silently log
    # "This model doesn't accept a Start Image" and the job fails with the
    # opaque "WanGP did not create any tasks" error.
    from features.fun_videos.video_generator import MODELS as _VG_MODELS
    _model_def = _VG_MODELS.get(requested_model)
    if photo_path and _model_def is not None and not _model_def.get("i2v", True):
        raise HTTPException(
            400,
            f"{requested_model} is text-to-video and cannot accept a start image. "
            f"Pick an I2V model (Wan2.1-I2V-* or LTX-2 *), or remove the photo to run text-only.",
        )

    # motion_style: auto_picked_motion wins when auto-pick ran; otherwise use what
    # the client sent (e.g. the Create Videos motion style chips); fall back to
    # None so the pipeline can derive it from the model family at finalize time.
    resolved_motion = auto_picked_motion or body.get("motion_style") or None

    settings = {
        "video_prompt": body.get("video_prompt", ""),
        "music_prompt": body.get("music_prompt", ""),
        "lyric_direction": body.get("lyric_direction", ""),
        "user_direction": body.get("user_direction", ""),
        "use_wildcards": body.get("use_wildcards", False),
        "video_duration": body.get("duration", config.get("fun_video_duration", 14.0)),
        "model_name": requested_model,
        "motion_style": resolved_motion,
        "resolution": body.get("resolution", config.get("resolution", "580p")),
        "override_width":  body.get("output_width"),
        "override_height": body.get("output_height"),
        "video_steps": body.get("steps", config.get("fun_video_steps", 30)),
        "video_guidance": body.get("guidance", config.get("fun_video_guidance", 7.5)),
        "video_seed": body.get("seed", config.get("fun_video_seed", -1)),
        "audio_steps": body.get("audio_steps", config.get("fun_audio_steps", 8)),
        "audio_guidance": body.get("audio_guidance", config.get("fun_audio_guidance", 7.0)),
        "instrumental": body.get("instrumental", config.get("fun_audio_instrumental", True)),
        "audio_format": body.get("audio_format", config.get("fun_audio_format", "mp3")),
        "bpm": body.get("bpm"),
        "skip_audio": body.get("skip_audio", False),
        "audio_provider": body.get("audio_provider", config.get("audio_provider", "acestep")),
        "end_photo_path": body.get("end_photo_path"),
        "start_video_path": _resolve_path(body.get("start_video_path", "")),
        "loras":          body.get("loras", []),
        "upscale":        body.get("upscale", True),
        "upscale_scale":  float(body.get("upscale_scale", 2.0)),
        "upscale_method": body.get("upscale_method", "ffmpeg"),
    }

    if photo_path:
        label = f"Fun Video: {Path(photo_path).stem[:20]}"
    else:
        label = f"T2V: {settings.get('video_prompt', '')[:24]}"
    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_VIDEO, run_prep, run_pipeline, photo_path, settings, label=label,
        )
    except RuntimeError as e:
        raise HTTPException(429, str(e))
    job.meta.update({
        "feature":      "fun_video",
        "source_image": photo_path or "",
        "prompt": settings.get("video_prompt", "")[:120],
        "model": settings.get("model_name", ""),
        "settings": {
            "prompt":       settings.get("video_prompt", "")[:240],
            "steps":        settings.get("video_steps"),
            "guidance":     settings.get("video_guidance"),
            "duration_sec": settings.get("video_duration"),
            "source_image": photo_path or "",
            "model":        settings.get("model_name", ""),
            "seed":         settings.get("video_seed"),
        },
    })
    return {"job_id": job.id}


@router.post("/make-it-multi")
async def make_it_multi(request: Request):
    """Submit a multi-clip story job.

    Generates N sequentially chained video clips (last frame of clip N becomes
    start image of clip N+1), then a single audio pass over the concatenated video.
    """
    from app import get_job_manager; job_manager = get_job_manager()
    from features.fun_videos.multi_pipeline import run_multi_prep, run_multi_pipeline

    body = await request.json()
    photo_path = _resolve_path(body.get("photo_path") or "")
    if photo_path and not os.path.isfile(photo_path):
        raise HTTPException(400, f"Photo not found: {photo_path}")
    if not photo_path and not body.get("video_prompt", "").strip():
        raise HTTPException(400, "Provide either a photo or a video prompt")

    config = cfg.load()
    clip_dur = max(4.0, min(20.0, float(body.get("clip_duration", config.get("fun_multi_clip_duration", 5.0)))))
    # If target_story_length is given, derive n_clips from it; otherwise use num_clips directly
    target_secs = body.get("target_story_length")
    if target_secs is not None:
        target_secs = float(target_secs)
        n_clips = max(2, min(10, round(target_secs / clip_dur)))
    else:
        n_clips = max(2, min(10, int(body.get("num_clips", config.get("fun_multi_num_clips", 2)))))

    # -- Auto-pick model (default ON for Express, OFF for Fun Videos) -----------
    # When auto_pick_model=True, classify the user's idea via a fast LLM call
    # and override the model + motion style with the best match. Falls back
    # silently to the requested model on any LLM error.
    requested_model = body.get("model") or config.get("wan_model") or "LTX-2 Dev19B Distilled"
    requested_motion = body.get("motion_style") or None
    if body.get("auto_pick_model"):
        from app import get_llm_router
        photo_b64 = None
        if photo_path and os.path.isfile(photo_path):
            try:
                photo_b64 = encode_image_b64(photo_path)
            except Exception:
                photo_b64 = None
        picked_model, picked_motion, pick_reason = await asyncio.to_thread(
            _auto_pick_model,
            get_llm_router(),
            body.get("video_prompt", ""),
            photo_b64,
            n_clips,
            False,
        )
        requested_model = picked_model
        requested_motion = picked_motion
        log.info("[make-it-multi] auto-pick chose %s (%s, %d clips) -- %s",
                 picked_model, picked_motion, n_clips, pick_reason)

    settings = {
        "video_prompt":    body.get("video_prompt", ""),
        "music_prompt":    body.get("music_prompt", ""),
        "lyric_direction": body.get("lyric_direction", ""),
        "user_direction":  body.get("user_direction", ""),
        "num_clips":       n_clips,
        "clip_duration":   clip_dur,
        "model_name":      requested_model,
        "resolution":      body.get("resolution", config.get("resolution",       "580p")),
        "override_width":  body.get("output_width"),
        "override_height": body.get("output_height"),
        "video_steps":     body.get("steps",         config.get("fun_video_steps",    30)),
        "video_guidance":  body.get("guidance",       config.get("fun_video_guidance", 7.5)),
        "video_seed":      body.get("seed",           config.get("fun_video_seed",     -1)),
        "audio_steps":     body.get("audio_steps",    config.get("fun_audio_steps",    20)),
        "audio_guidance":  body.get("audio_guidance", config.get("fun_audio_guidance", 7.0)),
        "instrumental":    body.get("instrumental",   config.get("fun_audio_instrumental", False)),
        "audio_format":    body.get("audio_format",   config.get("fun_audio_format",   "mp3")),
        "skip_audio":           body.get("skip_audio", False),
        "bpm":                  body.get("bpm"),
        "target_story_length":  target_secs,
        "upscale":              body.get("upscale", True),
        "upscale_scale":        float(body.get("upscale_scale", 2.0)),
        "upscale_method":       body.get("upscale_method", "ffmpeg"),
        "director_passes":      max(0, min(2, int(body.get("director_passes", config.get("fun_director_passes", 0))))),
        "motion_style":         requested_motion,
    }

    if photo_path:
        label = f"Story ({n_clips} clips): {Path(photo_path).stem[:16]}"
    else:
        label = f"Story ({n_clips} clips): {settings.get('video_prompt', '')[:20]}"

    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_MULTI_VIDEO, run_multi_prep, run_multi_pipeline,
            photo_path, settings, label=label,
        )
    except RuntimeError as e:
        raise HTTPException(429, str(e))

    job.meta.update({
        "feature":       "fun_multi_video",
        "source_image":  photo_path or "",
        "prompt":        settings.get("video_prompt", "")[:120],
        "model":         settings.get("model_name", ""),
        "num_clips":     n_clips,
        "clip_duration": clip_dur,
        "settings": {
            "prompt":       settings.get("video_prompt", "")[:240],
            "steps":        settings.get("video_steps"),
            "guidance":     settings.get("video_guidance"),
            "duration_sec": clip_dur,
            "source_image": photo_path or "",
            "model":        settings.get("model_name", ""),
            "seed":         settings.get("video_seed"),
        },
    })
    return {"job_id": job.id}


@router.get("/models")
async def list_models():
    config = cfg.load()
    configured = config.get("wan_model", "")
    # Find the matching model key (config stores the short name used in MODELS)
    default_key = configured if configured in MODELS else next(
        (k for k in MODELS if configured and configured.lower() in k.lower()), None
    ) or "Wan2.1-I2V-14B-480P"
    return {
        "models": {name: info for name, info in MODELS.items()},
        "default": default_key,
    }


@router.post("/brainstorm")
async def brainstorm(request: Request):
    """Natural-language session that refines video idea + lyric direction (or SD prompt).

    Accepts:
      image_path      - optional path to the source image for vision context
      message         - user's latest message (required)
      history         - [{role, content}] last N conversation turns
      current_idea    - current idea / motion prompt text
      current_lyric   - current lyric direction text
      mode            - "video" (default) or "sd_prompt"
    Returns JSON with updated fields plus a short reply sentence.
    """
    from app import get_llm_router
    # Brainstorm uses BALANCED (Sonnet) not FAST (Haiku): Sonnet is visibly
    # better at concrete physical-action prompts and lyric direction with
    # character. Cost difference for one user-triggered call is negligible.
    from core.llm_client import TIER_BALANCED as _TIER, parse_json_response
    llm_router = get_llm_router()

    body = await request.json()
    raw_image_path = body.get("image_path", "")
    image_path = _resolve_path(raw_image_path)
    message    = body.get("message", "").strip()
    history    = body.get("history", [])
    mode       = body.get("mode", "video")

    if not message:
        raise HTTPException(400, "message required")

    # Loud failure beats silent hallucination: if the client sent an
    # image_path that doesn't resolve to a real file (e.g. a blob: URL leak,
    # a deleted upload, a bad path), and the user's message clearly expects
    # a photo to be present, refuse rather than fall through to text-only
    # mode -- which is what produced "elephant in a sharp red suit" from a
    # photo of Big Buck Bunny.
    photo_expected = any(s in message.lower() for s in (
        "look at the photo", "the photo", "from the image", "from your photo",
        "the image", "from photo", "this picture",
    ))
    if raw_image_path and not (image_path and os.path.isfile(image_path)):
        log.warning(
            "brainstorm: image_path=%r did not resolve to a real file (resolved=%r) -- "
            "the client likely sent a blob: URL or stale path",
            raw_image_path, image_path,
        )
        if photo_expected:
            raise HTTPException(
                400,
                "Image upload not finished -- wait a moment and click again. "
                "(image_path did not resolve to a real file on disk)",
            )

    if mode == "sd_prompt":
        system = (
            "You help users write Stable Diffusion image prompts. "
            "Convert the user's plain-language description into a concise, comma-separated SD prompt "
            "(subject, style, lighting, mood, quality tags). "
            "Keep existing prompt context and refine it based on what the user asks. "
            "Respond ONLY with valid JSON (no other text): "
            '{"prompt": "...", "reply": "one sentence what you changed"}'
        )
    else:
        system = (
            "You help users create AI video generation prompts and ACE-Step music directions.\n\n"
            "IDEA: 1-2 sentences describing vivid physical action in the video (what the subject DOES).\n"
            "  - When a photo is provided, the subject and setting MUST match what is actually visible. "
            "Never invent new characters, animals, or props that are not in the image. "
            "If you can't tell what the subject is, describe motion that fits whatever is plainly visible.\n"
            "  - Describe ONE concrete action a real camera could capture in 5 seconds. "
            "BANNED words: transforms, becomes, reveals, establishes, unfolds, snaps to, the camera, we see. "
            "These break downstream video generation -- use plain motion verbs instead "
            "(swings, leans, lifts, turns, settles, drifts, rises, etc.).\n"
            "LYRIC DIRECTION: <=15 words. Format: \"[genre/energy], [lyric theme or voice]\". "
            "Pick something with real character -- match the mood and subject of the image. "
            "Examples: \"gypsy punk energy, sardonic lyrics about chaos\" | \"dark cabaret wit, ironic lyrics about vanity\" | \"dreamy lo-fi folk, wistful vocals\" | \"raw punk, confrontational\" | \"instrumental, no vocals\".\n\n"
            "Always return BOTH fields with concrete values -- never null, never empty strings. "
            "If the user asks to change only one field, keep the other consistent with the current context. "
            "Return ONLY valid JSON (no preamble, no explanation outside the JSON): "
            '{"idea": "...", "lyric_direction": "...", "reply": "one sentence what you changed"}'
        )

    ctx_parts = []
    if body.get("current_idea"):   ctx_parts.append(f"Current idea: {body['current_idea']}")
    if body.get("current_lyric"):  ctx_parts.append(f"Current lyric direction: {body['current_lyric']}")
    if body.get("current_prompt"): ctx_parts.append(f"Current SD prompt: {body['current_prompt']}")
    context_str = "\n".join(ctx_parts) or "Nothing set yet."
    user_content = f"{context_str}\n\nUser: {message}"

    try:
        if image_path and os.path.isfile(image_path):
            # route_vision takes a single prompt string, not a messages array.
            # Fold the last 8 history turns into the prompt as plain text so the
            # AI has prior-turn context instead of treating every message as the first.
            if history:
                hist_lines = []
                for h in history[-8:]:
                    role = "Assistant" if h.get("role") == "assistant" else "User"
                    hist_lines.append(f"{role}: {h.get('content', '').strip()}")
                vision_prompt = "\n".join(hist_lines) + "\n\n" + user_content
            else:
                vision_prompt = user_content
            b64 = await asyncio.to_thread(encode_image_b64, image_path)
            result = await asyncio.to_thread(
                llm_router.route_vision,
                prompt=vision_prompt,
                images_b64=[b64] if b64 else [],
                system=system,
                tier=_TIER,
            )
        else:
            msgs = [{"role": h["role"], "content": h["content"]} for h in history[-8:]]
            msgs.append({"role": "user", "content": user_content})
            result = await asyncio.to_thread(
                llm_router.route, messages=msgs, system=system, tier=_TIER,
            )
    except Exception as exc:
        msg = str(exc)
        if "rate limit" in msg.lower() or "429" in msg:
            raise HTTPException(429, "AI rate limit reached -- try again in a moment")
        if "connection" in msg.lower() or "refused" in msg.lower():
            raise HTTPException(503, "AI service unavailable -- check that Ollama or your API key is configured")
        raise HTTPException(502, f"AI error -- {msg[:120]}")

    try:
        parsed = parse_json_response(result)
        data = parsed if isinstance(parsed, dict) else {}
    except Exception:
        data = {}

    if mode == "sd_prompt":
        return {
            "prompt": data.get("prompt") or None,
            "reply":  data.get("reply")  or result[:120],
        }

    # Banned-word post-filter: even with the system prompt rule, LLMs
    # occasionally slip in 'transforms', 'becomes', etc. The downstream
    # story-arc generator strips these anyway, so we replace them with
    # plain physical verbs at the source so the user sees a clean idea.
    idea = data.get("idea") or None
    if idea:
        idea = _scrub_banned_motion_words(idea)

    return {
        "idea":            idea,
        "lyric_direction": data.get("lyric_direction") or None,
        "reply":           data.get("reply")           or result[:120],
    }


@router.post("/add-music")
async def add_music(request: Request):
    """Analyze an existing video and add ACE-Step generated music to it.

    Accepts:
        video_path      -- path to existing video file
        music_prompt    -- optional; if blank the LLM derives it from video frames
        user_direction  -- optional free-text guidelines fed to the LLM music director
        instrumental    -- bool (default True)
        bpm             -- optional int
    """
    from app import get_job_manager, get_llm_router
    job_manager = get_job_manager()

    body = await request.json()
    video_path = body.get("video_path", "")
    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, f"Video not found: {video_path}")

    settings = {
        "music_prompt":    body.get("music_prompt", ""),
        "user_direction":  body.get("user_direction", ""),
        "lyric_direction": body.get("lyric_direction", ""),
        "instrumental":    body.get("instrumental", False),
        "bpm":             body.get("bpm"),
        "audio_format":    "mp3",
        "audio_steps":     8,
        "audio_guidance":  7.0,
    }

    def _worker(job, vpath, cfg_settings):
        from app import get_llm_router; llm_router = get_llm_router()
        from features.fun_videos import analyzer, audio_generator
        from features.fun_videos.video_generator import merge_video_audio
        from features.fun_videos.pipeline import _sample_music_frames
        from core.ffmpeg_utils import probe_duration
        import time as _time
        from pathlib import Path as _Path

        job.update(progress=5, message="Analyzing video...")

        music_prompt = cfg_settings.get("music_prompt", "").strip()
        user_direction = cfg_settings.get("user_direction", "").strip()

        if not music_prompt:
            try:
                frames = _sample_music_frames(vpath, llm_router)
                if frames:
                    result = analyzer.generate_music_prompt(llm_router, frames, user_direction)
                    music_prompt = result.get("music_prompt", "")
                    if not cfg_settings.get("bpm") and result.get("bpm"):
                        cfg_settings["bpm"] = result["bpm"]
            except Exception as e:
                log.warning("Music analysis failed: %s", e)

        if not music_prompt:
            music_prompt = "dark cabaret, accordion, upright bass, brushed snare, smoky bistro atmosphere"

        instrumental = cfg_settings.get("instrumental", False)
        lyrics = ""
        if not instrumental:
            job.update(progress=15, message="Writing lyrics...")
            try:
                lyric_direction = cfg_settings.get("lyric_direction", "") or cfg_settings.get("user_direction", "")
                lyrics = analyzer.generate_lyrics(llm_router, [], music_prompt, lyric_direction)
            except Exception as e:
                log.warning("Lyrics generation failed: %s", e)
            if not lyrics:
                lyrics = "[verse]\nSomething moves through the frame\nNothing stays the same\n[chorus]\nLife in motion\nSlipping through the frame"

        job.update(progress=20, message="Generating music...")

        from services.forge_client import unload_checkpoint, reload_checkpoint
        forge_was_unloaded = unload_checkpoint()

        video_dur = probe_duration(vpath)
        audio_dur = min(video_dur + 2.0, 120.0) if video_dur > 0 else 30.0

        def _audio_progress(elapsed_s):
            job.update(progress=20 + min(60, int(elapsed_s / audio_dur * 60)),
                       message=f"Generating music... {elapsed_s:.0f}s elapsed")

        try:
            audio_path, audio_err = audio_generator.generate_audio(
                prompt=music_prompt,
                duration=audio_dur,
                output_dir=str(_Path(vpath).parent),
                audio_format=cfg_settings.get("audio_format", "mp3"),
                bpm=cfg_settings.get("bpm"),
                steps=int(cfg_settings.get("audio_steps", 8)),
                guidance=float(cfg_settings.get("audio_guidance", 7.0)),
                seed=-1,
                lyrics=lyrics,
                instrumental=instrumental,
                stop_event=job.stop_event,
                progress_cb=_audio_progress,
            )
        finally:
            if forge_was_unloaded:
                reload_checkpoint()

        if job.stop_event.is_set():
            return
        if not audio_path:
            raise RuntimeError(f"Audio generation failed: {audio_err}")

        job.update(progress=85, message="Mixing audio into video...")

        stem = _Path(vpath).stem
        final = str(_Path(vpath).parent / f"{stem}_with_music_{_time.strftime('%H%M%S')}.mp4")
        merged = merge_video_audio(vpath, audio_path, final)
        if not merged:
            raise RuntimeError("Audio/video merge failed")

        from core.session import get_current as get_session
        try:
            get_session().add_file(_Path(merged).name, "video", "fun_videos", path=merged)
        except Exception:
            pass

        job.output = merged
        from core.inbox import copy_to_inbox; copy_to_inbox(job.output)
        job.message = f"Done -- music prompt: {music_prompt[:60]}"

    label = f"Add music: {Path(video_path).stem[:24]}"
    try:
        job = job_manager.submit(JOB_FUN_VIDEO, _worker, video_path, settings, label=label)
    except RuntimeError as e:
        raise HTTPException(429, str(e))
    return {"job_id": job.id}


@router.post("/suggest-music")
async def suggest_music(request: Request):
    """LLM-derives a music prompt (and optional lyric direction) from video frames.

    Accepts:
        video_path      -- path to video file
        user_direction  -- optional free-text hint from the user
        instrumental    -- bool, default False
    Returns:
        { music_prompt, lyric_direction, bpm }
    """
    body = await request.json()
    video_path = _resolve_path(body.get("video_path", ""))
    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, f"Video not found: {video_path}")

    user_direction = body.get("user_direction", "")
    instrumental   = bool(body.get("instrumental", False))

    def _run():
        from app import get_llm_router; llm_router = get_llm_router()
        from features.fun_videos import analyzer
        from features.fun_videos.pipeline import _sample_music_frames

        frames = _sample_music_frames(video_path, llm_router)
        result = analyzer.generate_music_prompt(llm_router, frames, user_direction)
        music_prompt = result.get("music_prompt", "")
        bpm = result.get("bpm")

        lyric_direction = ""
        if not instrumental and music_prompt:
            # Generate a SHORT lyric direction hint (3-10 words), NOT full lyrics.
            # Previously called generate_lyrics() and took the first line, which
            # returned "[verse]" -- a section marker, not a direction hint.
            try:
                from core.llm_client import TIER_FAST
                hint_prompt = (
                    f'Music style: "{music_prompt}"'
                    + (f'\nCreative direction: "{user_direction}"' if user_direction else "")
                    + "\n\nWrite a 5-10 word lyric direction hint: theme + mood + voice style."
                    " Format: \"[theme], [mood/energy], [voice]\". Example: \"loss and longing, bittersweet, conversational\"."
                    " Return ONLY the hint -- no quotes, no explanation."
                )
                hint = llm_router.route(
                    [{"role": "user", "content": hint_prompt}],
                    tier=TIER_FAST, max_tokens=40,
                )
                lyric_direction = (hint or "").strip().strip('"').strip("'")[:120]
            except Exception:
                pass

        return {"music_prompt": music_prompt, "lyric_direction": lyric_direction, "bpm": bpm}

    return await asyncio.to_thread(_run)


@router.post("/sync-audio")
async def sync_audio(request: Request):
    """Shift the audio track of a video by offset_ms milliseconds.

    Positive offset_ms: audio starts later (delays the audio).
    Negative offset_ms: audio starts earlier (advances the audio).

    Accepts:
        video_path  -- path to merged video file
        offset_ms   -- integer milliseconds, range -5000..5000
    Returns:
        { output }
    """
    import subprocess
    import time as _time
    body = await request.json()
    video_path = _resolve_path(body.get("video_path", ""))
    if not video_path or not os.path.isfile(video_path):
        raise HTTPException(400, f"Video not found: {video_path}")

    offset_ms = int(body.get("offset_ms", 0))
    if abs(offset_ms) > 5000:
        raise HTTPException(400, "offset_ms must be between -5000 and 5000")
    if offset_ms == 0:
        return {"output": video_path}

    def _run():
        p = Path(video_path)
        out = str(p.parent / f"{p.stem}_sync{offset_ms:+d}ms_{int(_time.time())}.mp4")
        if offset_ms > 0:
            af = f"adelay={offset_ms}|{offset_ms}"
        else:
            sec = abs(offset_ms) / 1000.0
            af = f"atrim=start={sec:.3f},apad"
        cmd = ["ffmpeg", "-y", "-i", video_path, "-af", af, "-c:v", "copy", "-c:a", "aac", out]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg sync failed: {r.stderr.decode()[-400:]}")
        return out

    out_path = await asyncio.to_thread(_run)
    return {"output": out_path}
