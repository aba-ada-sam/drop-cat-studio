"""Create Videos API routes -- /api/fun/*

Photo -> AI video + audio pipeline with wildcard support.
"""
import asyncio
import io
import logging
import os
import uuid
from pathlib import Path

import tempfile

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
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


import re as _re

# Matches an absolute Windows path: drive letter + colon + slash + everything
# up to the next quote character. Used to recover a real path from a string
# that also contains error-message prefixes like "Not a folder: C:\...\".
_WIN_ABS_PATH_RE = _re.compile(r'[A-Za-z]:[/\\][^\'"]*')


def _clean_user_path(p: str) -> str:
    """Extract a clean absolute filesystem path from raw user input.

    Handles three common mess cases:
    1. Windows Explorer 'Copy as path': '"C:\\Users\\...\\folder"' -- outer quotes
    2. Error message re-pasted: 'Not a folder: C:\\DCS\\... "C:\\Users\\...\\folder"'
       (user copy-pasted the server error back into the input field)
    3. Single-quoted shell paths: "'C:\\Users\\...\\folder'"

    Strategy:
    - Strip surrounding whitespace and quotes first.
    - If the result already looks like an absolute path, return it.
    - Otherwise scan the string for any embedded Windows absolute path and
      return the LAST one found (the real target is usually at the end of
      an error message).
    - Normalize the result with os.path.normpath so ".." sequences are
      collapsed before the caller's os.path.isdir check.
    """
    import os as _os
    s = (p or "").strip().strip('"').strip("'").strip()
    # Already an absolute path?
    if _re.match(r'^[A-Za-z]:[/\\]', s) or s.startswith('/'):
        return _os.path.normpath(s)
    # Scan for embedded absolute path(s) (handles pasted error messages)
    matches = _WIN_ABS_PATH_RE.findall(s)
    if matches:
        return _os.path.normpath(matches[-1].strip().strip('"').strip("'").strip())
    return s


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


@router.get("/list-folder")
async def list_folder(path: str = ""):
    """List image files in a local folder, sorted by name.

    Used by the 'Loop Folder' button in the Express tab to iterate a
    user's photo directory. Returns absolute paths so the generation
    pipeline can read them directly without an upload round-trip.

    Args:
        path: Absolute filesystem path to a folder containing images.

    Returns:
        {
          "folder": str (the resolved path),
          "images": [{"path": str, "name": str, "size_kb": int}, ...]
        }

    Raises 400 if the path is missing, not a directory, or contains
    no recognised images. Hidden files and subdirectories are skipped.
    """
    path = _clean_user_path(path)
    if not path:
        raise HTTPException(400, "Missing 'path' query parameter")

    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise HTTPException(400, f"Not a folder: {folder}")

    images: list[dict] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file():
            continue
        if entry.name.startswith("."):
            continue
        if entry.suffix.lower() not in IMAGE_EXTS:
            continue
        try:
            size_kb = entry.stat().st_size // 1024
        except OSError:
            size_kb = 0
        images.append({
            "path": str(entry),
            "name": entry.name,
            "size_kb": size_kb,
        })

    if not images:
        raise HTTPException(
            400,
            f"No images found in {folder} (looking for {', '.join(sorted(IMAGE_EXTS))})",
        )

    return {"folder": str(folder), "images": images}


@router.post("/folder-loop/start")
async def folder_loop_start(request: Request):
    """Start a server-side folder loop.

    Body:
      folder:   absolute path to the folder
      settings: full settings dict (same shape /make-it or /make-it-multi
                accepts; photo_path is supplied per-image by the loop)
      multi_video: bool -- True routes through /api/fun/make-it-multi pipeline,
                          False through /api/fun/make-it pipeline
      repeat:   bool -- True restarts at image 0 after the last image

    The loop is tied to a heartbeat from the browser: if /status is
    not polled for ~60s, the loop self-stops. This is intentional --
    closing the browser tab must kill the loop. See folder_loop.py.
    """
    from features.fun_videos import folder_loop
    body = await request.json()

    folder = _clean_user_path(body.get("folder", ""))
    if not folder:
        raise HTTPException(400, "Missing 'folder'")

    # Reuse the same listing logic the GET endpoint uses so we filter to
    # IMAGE_EXTS and sort alphabetically consistently.
    folder_path = Path(folder).expanduser().resolve()
    if not folder_path.is_dir():
        raise HTTPException(400, f"Not a folder: {folder_path}")
    images: list[dict] = []
    for entry in sorted(folder_path.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_file() and not entry.name.startswith(".") \
                and entry.suffix.lower() in IMAGE_EXTS:
            images.append({"path": str(entry), "name": entry.name})
    if not images:
        raise HTTPException(400, f"No images found in {folder_path}")

    settings = body.get("settings") or {}
    if not isinstance(settings, dict):
        raise HTTPException(400, "'settings' must be an object")

    multi_video = bool(body.get("multi_video", True))
    repeat = bool(body.get("repeat", False))
    endpoint = "/api/fun/make-it-multi" if multi_video else "/api/fun/make-it"

    snap = folder_loop.start(str(folder_path), images, settings, endpoint, repeat)
    return snap


@router.get("/folder-loop/status")
async def folder_loop_status():
    """Snapshot of the current folder-loop state.

    POLLING THIS ENDPOINT ACTS AS A HEARTBEAT. If polls stop for ~60s,
    the loop terminates itself. Client must poll regularly while the
    loop should be alive.
    """
    from features.fun_videos import folder_loop
    return folder_loop.status()


@router.post("/folder-loop/stop")
async def folder_loop_stop():
    """Signal the loop to stop at the next checkpoint."""
    from features.fun_videos import folder_loop
    return folder_loop.stop()


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


@router.get("/video-frame")
async def video_frame(
    path: str = Query(...),
    t: float = Query(default=None),
):
    """Return a JPEG of the video frame at time t (seconds).

    Used by the frontend frame-picker to preview the selected continuation point.
    Path must be inside the uploads directory.
    """
    from core.ffmpeg_utils import extract_last_frame_to_file

    abs_path = Path(path).resolve()
    if not str(abs_path).startswith(str(UPLOADS_DIR.resolve())):
        raise HTTPException(400, "path outside uploads directory")
    if not abs_path.is_file():
        raise HTTPException(404, "video not found")

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        ok = await asyncio.to_thread(
            extract_last_frame_to_file, abs_path, tmp_path, t
        )
        if not ok:
            raise HTTPException(500, "frame extraction failed")
        return Response(Path(tmp_path).read_bytes(), media_type="image/jpeg")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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
                return llm_router.route_vision(user_msg, [b64], tier=TIER_FAST, system=system, max_tokens=200)
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

# SAFETY: every auto-pick bucket resolves to LTX-2 Distilled (fits 16GB
# cleanly). Wan I2V 14B at int8 is 15.87 GB which is larger than the
# 13 GB safety budget WanGP allows on a 16GB card -- attempting it
# causes the GPU to thrash at 100% util and 97% VRAM, dragging the
# whole machine to a halt. We will NOT let auto-pick reach for Wan
# until this DCS install detects a 20GB+ card. Power users who want
# Wan I2V can disable auto-pick in the Express tab and pick it from
# the dropdown manually.
#
# Motion style still varies by bucket; LTX Distilled handles all of
# them via its calm denoising character. Even prompts that read as
# action (sprint, leap) render as atmospheric motion -- not the
# spastic AI slop we saw when LTX was in 'dynamic' mode.
_PICK_TO_MODEL = {
    "calm":         ("LTX-2 Dev19B Distilled", "calm"),
    # action buckets: use "gentle" motion so the subject visibly moves
    # (head turns, gestures, breath) instead of being frozen. "calm" produced
    # "boring results" complaints. "dynamic" causes spastic micro-jitter on
    # LTX at 8 steps. "gentle" is the sweet spot: visible but artifact-free.
    "action":       ("LTX-2 Dev19B Distilled", "gentle"),
    "action_hd":    ("LTX-2 Dev19B Distilled", "gentle"),
    "story_action": ("LTX-2 Dev19B Distilled", "gentle"),
    "long_story":   ("LTX-2 Dev19B Distilled", "calm"),
}
# Hardware reality on 16GB VRAM cards (RTX 5080):
#   * LTX-2 Dev19B Distilled  int8 ~ 9 GB  -- fits cleanly, ~3-4s/step,
#                                              CALM motion only (atmospheric).
#                                              In dynamic mode on action prompts
#                                              it produces spastic micro-jitter.
#   * Wan2.1-I2V-14B          int8 ~ 16 GB -- doesn't fit in 13GB budget,
#                                              streams 22% of layers from RAM.
#                                              ~5-15s/step (slow), but real
#                                              kinetic motion that holds the
#                                              subject through action verbs.
#                                              MUST run with compile = "" or
#                                              the recompile loop hangs.
# Action buckets route to Wan I2V because the visual result on action prompts
# is the only thing that matters; users will trade time for a coherent video
# over a fast spastic one. Atmospheric buckets stay on LTX because that's its
# native mode AND it's 5-10x faster. The user-visible knob remains the same:
# auto-pick on -> system picks based on prompt energy.

_AUTO_PICK_SYSTEM = """You are picking the best AI video model for a user's idea.

You have five choices. Pick the ONE that fits best.

IMPORTANT BIAS: prefer 'calm' or 'long_story' (LTX) unless the prompt
EXPLICITLY requires kinetic body motion. The user pays a 5-10x time
penalty for the 'action' choices, so use them only when calm motion
would clearly fail the scene. Atmospheric movement, environmental
effects (wind, water, light, fabric, smoke, hair sway), and slow
breathing-photograph energy all belong in calm/long_story.

  calm
    LTX-2 Distilled. PREFERRED CHOICE in most cases. Subject moves
    SUBTLY -- breathing, slight head turn, fabric drift -- while
    environment animates: light shifting, steam rising, water rippling,
    leaves trembling, fabric in wind. Picks for: portraits, landscapes,
    objects, mood pieces, anything that is not a pure 'X is sprinting'
    action shot. Fast (~30s/clip).

  long_story
    LTX-2 Distilled, same calm character, for multi-clip ATMOSPHERIC
    stories where the scene must be preserved across clips. Pick this
    over 'calm' when the user wants a 3+ clip narrative with mood.
    Fast (~30s/clip).

  action
    Wan2.1 I2V 480P. For REAL PHOTOS where the subject must do an
    EXPLICITLY KINETIC action that calm motion cannot fake: sprinting,
    leaping, throwing a punch, dancing energetically. The prompt must
    contain action verbs (sprint, run, jump, slam, hit, throw, leap,
    dance, kick). SLOW: ~3-5 min/clip on a 16GB card. Use only when
    necessary.

  action_hd
    Wan2.1 I2V 720P. Same as 'action' but 720p output. Pick ONLY when
    user explicitly asks for HD/sharp output. SLOWER: ~5-8 min/clip.

  story_action
    Wan I2V 480P with dynamic motion across multiple clips. For
    multi-clip stories where every clip needs kinetic action that
    calm motion cannot render. Same VRAM and time cost as 'action'.

DECISION TREE:
  1. Does the prompt contain explicit kinetic action verbs that calm
     motion cannot reasonably fake (sprint, leap, slam, jump, dance,
     run, kick)? -> action / story_action / action_hd.
  2. Otherwise -> calm / long_story.

When in doubt: pick 'calm' (single clip) or 'long_story' (multi-clip).
Speed matters more than maximum motion for most users.

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
        return ("LTX-2 Dev19B Distilled", "calm", "no idea -- LTX safe default (Wan I2V won't fit 16GB)")

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

    # Step 1: vision call (cloud-first; only goes to Ollama if user enabled
    # the fallback in Settings or explicitly chose Ollama as the provider).
    if photo_b64:
        try:
            text = llm_router.route_vision(
                user_msg, [photo_b64],
                tier=TIER_FAST, system=_AUTO_PICK_SYSTEM, max_tokens=120,
                format_json=True,
            )
            log.info("[auto-pick] vision returned %d chars: %r", len(text or ""), (text or "")[:200])
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
    return ("LTX-2 Dev19B Distilled", "calm", "fallback-LTX (Wan I2V blocked on 16GB)")


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

    # -- Auto-pick model (off by default for Create Videos -- advanced users) ---
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

    # Apply per-model step floor (auto-pick may have changed the model since
    # the UI rendered the steps slider).
    _MODEL_MIN_STEPS_SINGLE = {
        "LTX-2 Dev19B Distilled": 4,
        "LTX-2 Dev13B":            20,
        "Wan2.1-I2V-14B-480P":     20,
        "Wan2.1-I2V-14B-720P":     20,
        "Wan2.1-T2V-14B":          20,
        "Wan2.1-T2V-1.3B":         15,
    }
    _ui_steps_s = int(body.get("steps", config.get("fun_video_steps", 30)))
    _min_s = _MODEL_MIN_STEPS_SINGLE.get(requested_model, 20)
    _final_steps_s = max(_ui_steps_s, _min_s)
    if _final_steps_s != _ui_steps_s:
        log.info("[make-it] step floor: ui=%d -> %d for %s",
                 _ui_steps_s, _final_steps_s, requested_model)

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
        "video_steps": _final_steps_s,
        "video_guidance": body.get("guidance", config.get("fun_video_guidance", 7.5)),
        "video_seed": body.get("seed", config.get("fun_video_seed", -1)),
        # 27 steps is the floor for clearly-sung vocals from ACE-Step. Below
        # ~20 the model produces music beds without intelligible singing.
        "audio_steps": body.get("audio_steps", config.get("fun_audio_steps", 27)),
        "audio_guidance": body.get("audio_guidance", config.get("fun_audio_guidance", 7.0)),
        "instrumental": body.get("instrumental", config.get("fun_audio_instrumental", True)),
        "audio_format": body.get("audio_format", config.get("fun_audio_format", "mp3")),
        "bpm": body.get("bpm"),
        "skip_audio": body.get("skip_audio", False),
        "audio_provider": body.get("audio_provider", config.get("audio_provider", "acestep")),
        "end_photo_path": body.get("end_photo_path"),
        "start_video_path":          _resolve_path(body.get("start_video_path", "")),
        "video_mode":                body.get("video_mode", "continuation"),
        "start_video_seek_seconds":  body.get("start_video_seek_seconds"),
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
    # Hard cap at 5.0s per clip: 5s * 25fps = 125 frames, below WanGP's 129-frame
    # sliding window threshold. Anything >= 5.17s triggers a 2-window split which
    # roughly doubles per-clip generation time. n_clips grows instead -- a 30s
    # story becomes 6 fast clips instead of 5 slow ones.
    clip_dur = max(4.0, min(5.0, float(body.get("clip_duration", config.get("fun_multi_clip_duration", 5.0)))))
    # If target_story_length is given, derive n_clips from it; otherwise use num_clips directly
    target_secs = body.get("target_story_length")
    if target_secs is not None:
        target_secs = float(target_secs)
        n_clips = max(2, min(10, round(target_secs / clip_dur)))
    else:
        n_clips = max(2, min(10, int(body.get("num_clips", config.get("fun_multi_num_clips", 2)))))

    # -- Auto-pick model (default ON for Express, OFF for Create Videos) --------
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

    # Per-model step minimums. Auto-pick can change the model from what the UI
    # configured, so we recompute the step count here using the picked model's
    # actual sweet spot rather than blindly trusting the slider value (which
    # was tuned for whatever model the user had selected manually). LTX
    # Distilled needs 4-8; Wan I2V needs 20-25 minimum or output is a blob.
    _MODEL_MIN_STEPS = {
        "LTX-2 Dev19B Distilled": 4,
        "LTX-2 Dev13B":            20,
        "Wan2.1-I2V-14B-480P":     20,
        "Wan2.1-I2V-14B-720P":     20,
        "Wan2.1-T2V-14B":          20,
        "Wan2.1-T2V-1.3B":         15,
    }
    _ui_steps = int(body.get("steps", config.get("fun_video_steps", 30)))
    _min_for_model = _MODEL_MIN_STEPS.get(requested_model, 20)
    _final_steps = max(_ui_steps, _min_for_model)
    if _final_steps != _ui_steps:
        log.info("[make-it-multi] step floor: ui=%d -> %d for %s",
                 _ui_steps, _final_steps, requested_model)

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
        "video_steps":     _final_steps,
        "video_guidance":  body.get("guidance",       config.get("fun_video_guidance", 7.5)),
        "video_seed":      body.get("seed",           config.get("fun_video_seed",     -1)),
        # 27 steps is the floor for intelligible sung vocals from ACE-Step.
        "audio_steps":     body.get("audio_steps",    config.get("fun_audio_steps",    27)),
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
        "start_video_path":          _resolve_path(body.get("start_video_path", "")),
        "video_mode":                body.get("video_mode", "continuation"),
        "start_video_seek_seconds":  body.get("start_video_seek_seconds"),
    }

    # Surface the chosen model + expected pace in the label so users see what
    # they're getting before sitting through a 20-minute Wan I2V run unaware.
    _model_short = (
        "Wan I2V (slow, kinetic)" if "wan" in requested_model.lower() else
        "LTX (fast, atmospheric)"
    )
    if photo_path:
        label = f"Story ({n_clips} clips, {_model_short}): {Path(photo_path).stem[:16]}"
    else:
        label = f"Story ({n_clips} clips, {_model_short}): {settings.get('video_prompt', '')[:20]}"

    # Scale the overall job timeout to clip count x per-clip model timeout so the
    # job manager does not kill a legitimate long Wan I2V run mid-clip. Add 300s
    # buffer for audio generation and final merge steps.
    _per_clip_timeout = MODELS.get(requested_model, {}).get("poll_timeout_s", 600)
    _job_timeout = _per_clip_timeout * n_clips + 300

    try:
        job = job_manager.submit_with_prep(
            JOB_FUN_MULTI_VIDEO, run_multi_prep, run_multi_pipeline,
            photo_path, settings, label=label, timeout_seconds=_job_timeout,
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

    # Build the shared vision prompt and text messages once so both the primary
    # and cloud-fallback attempts use identical content.
    if history:
        hist_lines = []
        for h in history[-8:]:
            role = "Assistant" if h.get("role") == "assistant" else "User"
            hist_lines.append(f"{role}: {h.get('content', '').strip()}")
        vision_prompt = "\n".join(hist_lines) + "\n\n" + user_content
    else:
        vision_prompt = user_content
    msgs = [{"role": h["role"], "content": h["content"]} for h in history[-8:]]
    msgs.append({"role": "user", "content": user_content})

    has_image = bool(image_path and os.path.isfile(image_path))
    b64 = await asyncio.to_thread(encode_image_b64, image_path) if has_image else None

    from features.fun_videos.multi_pipeline import _pick_cloud_provider
    _cloud_force = _pick_cloud_provider(llm_router)

    result = None
    last_exc = None
    # Try configured provider first, then force cloud if Ollama times out.
    for _force in (None, _cloud_force):
        if _force is not None and _force == llm_router._provider():
            continue  # already tried this provider
        try:
            if has_image and b64:
                result = await asyncio.to_thread(
                    llm_router.route_vision,
                    vision_prompt, [b64],
                    system=system, tier=_TIER,
                    force_provider=_force,
                )
            else:
                result = await asyncio.to_thread(
                    llm_router.route, msgs,
                    system=system, tier=_TIER,
                    force_provider=_force,
                )
            break  # success
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            is_timeout = "timeout" in msg or "timed out" in msg
            is_ollama  = (llm_router._provider() if not _force else _force) == "ollama"
            if is_timeout and is_ollama and _cloud_force and _force is None:
                log.info("[brainstorm] Ollama timeout -- retrying via %s", _cloud_force)
                continue
            break  # non-retryable error

    if result is None:
        msg = str(last_exc) if last_exc else "unknown error"
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

    # If the LLM didn't return parseable JSON, that means either it refused
    # the request or it spoke in plain prose. Log the raw response so we can
    # see exactly what 'go away' looked like next time. Truncated to 600
    # chars to keep the log readable.
    if not data:
        log.warning("[brainstorm] LLM returned non-JSON response (likely a refusal "
                    "or plain prose). Mode=%s, message=%r. Raw response: %s",
                    mode, message[:120], (result or "")[:600])
        # Cloud APIs refuse NSFW images with a plain-English refusal. The
        # router already auto-falls-back to Ollama when it's running, so if
        # we still got a refusal here it means Ollama wasn't available
        # either. Replace the literal refusal text with actionable guidance.
        from core.llm_router import _looks_like_safety_refusal
        if _looks_like_safety_refusal(result):
            try:
                from services.manager import ollama_alive
                ollama_running = ollama_alive()
            except Exception:
                ollama_running = False
            if ollama_running:
                friendly = ("The cloud AI couldn't analyse this image and the local "
                            "fallback didn't produce a useful description either. "
                            "Try a different image, or write the brief yourself.")
            else:
                friendly = ("The cloud AI declined to analyse this image (content policy). "
                            "Start Ollama (Services tab) so DCS can fall back to a local "
                            "vision model that handles all images, or type the brief "
                            "yourself in the textbox.")
            return {"idea": None, "lyric_direction": None, "reply": friendly}
    else:
        log.info("[brainstorm] mode=%s reply=%r idea=%r lyric=%r",
                 mode, str(data.get("reply") or "")[:80],
                 bool(data.get("idea")), bool(data.get("lyric_direction")))

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
        "audio_steps":     27,  # below ~20 ACE-Step renders music bed without intelligible vocals
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

        # Orchestrator: acquire ACE-Step (evicts Forge / WanGP / Ollama).
        from core.gpu_orchestrator import gpu
        gpu.acquire("acestep", reason="add-audio job")

        video_dur = probe_duration(vpath)
        audio_dur = min(video_dur + 2.0, 120.0) if video_dur > 0 else 30.0

        def _audio_progress(elapsed_s):
            job.update(progress=20 + min(60, int(elapsed_s / audio_dur * 60)),
                       message=f"Generating music... {elapsed_s:.0f}s elapsed")

        audio_path, audio_err = audio_generator.generate_audio(
            prompt=music_prompt,
            duration=audio_dur,
            output_dir=str(_Path(vpath).parent),
            audio_format=cfg_settings.get("audio_format", "mp3"),
            bpm=cfg_settings.get("bpm"),
            steps=int(cfg_settings.get("audio_steps", 27)),
            guidance=float(cfg_settings.get("audio_guidance", 7.0)),
            seed=-1,
            lyrics=lyrics,
            instrumental=instrumental,
            stop_event=job.stop_event,
            progress_cb=_audio_progress,
        )

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


# Per-model/motion system prompts for the AI prompt enhancer.
# Keys are (model_name, motion_style) tuples. Fallback tries (model, "dynamic"),
# then the first matching model prefix, then a generic default.
_ENHANCE_SYSTEMS: dict[tuple[str, str], str] = {
    ("LTX-2 Dev19B Distilled", "calm"): (
        "You are a prompt engineer for the LTX-Video model (cinematic AI video generation).\n"
        "Rewrite the user's rough idea as a polished LTX prompt for CALM / atmospheric motion.\n\n"
        "Rules:\n"
        "- 40-60 words total.\n"
        "- Present tense, third-person (e.g. 'A woman stands...').\n"
        "- The SUBJECT IS STILL. Only the environment moves: light shifts, steam rises, fabric drifts, leaves tremble.\n"
        "- ONE environmental motion verb.\n"
        "- Cinematographic terms (golden-hour light, soft bokeh, shallow depth of field).\n"
        "- Structure: Subject description + static pose -> environment animation -> scene anchor -> end with 'Static shot, fixed camera.'\n"
        "- No camera moves. No action verbs on the subject.\n"
        "- Output ONLY the prompt text -- no quotes, no preamble."
    ),
    ("LTX-2 Dev19B Distilled", "gentle"): (
        "You are a prompt engineer for the LTX-Video model (cinematic AI video generation).\n"
        "Rewrite the user's rough idea as a polished LTX prompt for GENTLE / subtle motion.\n\n"
        "Rules:\n"
        "- 45-65 words total.\n"
        "- Present tense, third-person.\n"
        "- The subject makes ONE subtle movement: a slow exhale, a slight head turn, a gentle hand raise.\n"
        "- The environment makes ONE complementary motion: soft light shifts, a gentle breeze, fabric ripples.\n"
        "- Structure: Subject + subtle gesture -> environment response -> scene anchor -> end with 'Fixed camera.'\n"
        "- No rapid actions, no camera moves, no dramatic motion.\n"
        "- Output ONLY the prompt text -- no quotes, no preamble."
    ),
    ("LTX-2 Dev19B Distilled", "narrative"): (
        "You are a prompt engineer for the LTX-Video model (cinematic AI video generation).\n"
        "Rewrite the user's rough idea as a polished LTX prompt for NARRATIVE / story-beat motion.\n\n"
        "Rules:\n"
        "- 50-70 words total.\n"
        "- Present tense, third-person.\n"
        "- Describe a purposeful action that advances a story: picking up an object, turning to face someone, reading a letter.\n"
        "- ONE environmental detail that reinforces mood.\n"
        "- Structure: Scene context -> subject action with narrative weight -> mood detail -> camera note (slow push-in OR static).\n"
        "- Output ONLY the prompt text -- no quotes, no preamble."
    ),
    ("LTX-2 Dev13B", "dynamic"): (
        "You are a prompt engineer for LTX-Video 13B (cinematic AI video).\n"
        "Rewrite the user's idea as a prompt for DYNAMIC physical motion.\n\n"
        "Rules:\n"
        "- 45-65 words total.\n"
        "- Present tense, third-person.\n"
        "- Start with exact visual markers: hair color, clothing color, setting lighting.\n"
        "- ONE deliberate physical action with clear kinetic consequence (hair whips, jacket billows, dust kicks up).\n"
        "- ONE scene anchor (cobblestones, rain-slicked street, forest path).\n"
        "- End with ONE camera note (dolly-in, slow pan, static wide).\n"
        "- No camera-transform verbs ('reveals', 'transitions', 'establishes'). No 'we see'.\n"
        "- Output ONLY the prompt text -- no quotes, no preamble."
    ),
    ("Wan2.1-I2V-14B-480P", "dynamic"): (
        "You are a prompt engineer for Wan2.1 Image-to-Video 480P (AI video generation).\n"
        "Rewrite the user's idea as a Wan I2V prompt for DYNAMIC motion.\n\n"
        "Rules:\n"
        "- 80-100 words total.\n"
        "- Begin with a STRONG ACTION VERB in present tense: 'Sprints', 'Leaps', 'Spins'.\n"
        "- Re-state key visual markers from the image: hair color, clothing, setting light.\n"
        "- ONE camera move that REACTS to the action (camera pulls back as they leap, follows the sprint).\n"
        "- Describe environment's response to the action (dust cloud, splashing water, whipping fabric).\n"
        "- End with: 'Photorealistic, smooth motion, high quality.'\n"
        "- Dolly-in works; dolly-out causes artifacts -- avoid dolly-out.\n"
        "- Output ONLY the prompt text -- no quotes, no preamble."
    ),
    ("Wan2.1-I2V-14B-720P", "dynamic"): (
        "You are a prompt engineer for Wan2.1 Image-to-Video 720P (AI video generation).\n"
        "Rewrite the user's idea as a Wan I2V 720P prompt for DYNAMIC high-definition motion.\n\n"
        "Rules:\n"
        "- 80-100 words total.\n"
        "- Begin with a STRONG ACTION VERB in present tense: 'Sprints', 'Leaps', 'Spins'.\n"
        "- Re-state key visual markers with fine textural detail: fabric weave, skin texture, specific light quality.\n"
        "- ONE camera move reacting to the action (no dolly-out -- it causes artifacts).\n"
        "- Environment response to the action.\n"
        "- End with: 'Photorealistic, fine detail, cinematic 720P, smooth motion.'\n"
        "- Output ONLY the prompt text -- no quotes, no preamble."
    ),
    ("Wan2.1-T2V-14B", "dynamic"): (
        "You are a prompt engineer for Wan2.1 Text-to-Video 14B (AI video generation).\n"
        "There is NO reference image -- you must describe EVERYTHING visually.\n"
        "Rewrite the user's idea as a complete T2V prompt.\n\n"
        "Rules:\n"
        "- 80-100 words total.\n"
        "- Open with time of day + setting + atmosphere: 'Late afternoon, rain-slicked city street, neon reflections.'\n"
        "- Explicit subject description: appearance, clothing, position.\n"
        "- ONE clear action the subject performs.\n"
        "- ONE camera move (dolly-in, slow pan, tracking shot -- no dolly-out).\n"
        "- End with: 'Photorealistic, smooth motion, cinematic color grade.'\n"
        "- Output ONLY the prompt text -- no quotes, no preamble."
    ),
    ("Wan2.1-T2V-1.3B", "dynamic"): (
        "You are a prompt engineer for Wan2.1 Text-to-Video 1.3B (fast lightweight AI video).\n"
        "This is a SMALL model -- keep prompts tight and unambiguous.\n"
        "Rewrite the user's idea as a simple, direct T2V prompt.\n\n"
        "Rules:\n"
        "- 50-70 words total.\n"
        "- ONE subject, ONE action, ONE setting -- no sub-plots.\n"
        "- Plain descriptive language; no flowery adjectives.\n"
        "- ONE camera note max.\n"
        "- End with: 'Smooth motion, high quality.'\n"
        "- Output ONLY the prompt text -- no quotes, no preamble."
    ),
}

# Generic fallback used when no (model, motion) pair matches.
_ENHANCE_SYSTEM_DEFAULT = (
    "You are a prompt engineer for AI video generation.\n"
    "Rewrite the user's rough idea as a polished, specific video prompt.\n"
    "60-80 words. Present tense, third-person. Strong visual verbs. ONE camera note.\n"
    "Output ONLY the prompt text -- no quotes, no preamble."
)


def _get_enhance_system(model_name: str, motion_style: str) -> str:
    key = (model_name, motion_style)
    if key in _ENHANCE_SYSTEMS:
        return _ENHANCE_SYSTEMS[key]
    # Try dynamic fallback for the same model
    dyn_key = (model_name, "dynamic")
    if dyn_key in _ENHANCE_SYSTEMS:
        return _ENHANCE_SYSTEMS[dyn_key]
    return _ENHANCE_SYSTEM_DEFAULT


@router.post("/enhance-prompt")
async def enhance_prompt(request: Request):
    """Rewrite a rough user idea into a polished, model-appropriate video prompt.

    Body:
        prompt      -- the user's rough idea (required)
        model       -- model name (optional, used to select system prompt style)
        motion      -- motion style: calm/gentle/dynamic/narrative (optional)

    Returns:
        { "prompt": "<enhanced text>" }
    """
    from app import get_llm_router
    from core.llm_client import TIER_FAST
    body = await request.json()
    raw = (body.get("prompt") or "").strip()
    if not raw:
        raise HTTPException(400, "Missing 'prompt'")
    model_name = (body.get("model") or "LTX-2 Dev19B Distilled").strip()
    motion_style = (body.get("motion") or "dynamic").strip()

    system = _get_enhance_system(model_name, motion_style)
    llm = get_llm_router()

    def _call():
        return llm.route(
            system=system,
            user=raw,
            tier=TIER_FAST,
            max_tokens=200,
        )

    result = await asyncio.to_thread(_call)
    enhanced = (result or "").strip().strip('"').strip("'").strip()
    if not enhanced:
        raise HTTPException(500, "LLM returned empty response")
    return {"prompt": enhanced}
