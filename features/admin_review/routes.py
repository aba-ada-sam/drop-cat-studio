"""Admin Review -- /api/admin-review/*

A PIN-gated, standalone review loop for Andrew (not linked from the normal
tab rail): generates a batch of 4 NSFW images from a scene/subject/preset,
lets him pick one (green check) and/or CHAT about the 4 images in plain
English, and folds that into the NEXT batch -- indefinitely, one round at a
time. Built for mobile use (see static/admin_review.html), so every response
is small and every round is a single self-contained request.

Reuses core/image_presets.build_forge_payload -- the same prompt-building
logic as the Image Studio tab -- so this is a review LOOP around the same
proven pipeline, not a second copy of the anatomy/taste wrap.

The chat is genuinely vision-mediated, not string-concatenation: the admin's
message plus the 4 images currently on screen go to a vision LLM (via
core/llm_router.route_vision -- the same mechanism Chat Studio already uses
for NSFW-safe image analysis, see features/chat_studio/routes.py and this
repo's own CLAUDE.md "LLM routing" section), which returns a rewritten scene
prompt, an optional short negative-prompt addition, and an optional pick of
which image to anchor the next batch to. If that call fails for any reason
(provider down, etc.), we fall back to literally appending the admin's raw
text to the prompt rather than silently dropping their input.
"""
import asyncio
import base64
import json
import logging
import secrets
import time
import uuid
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from core import config as cfg
from core import image_presets
from core.minor_safety import nsfw_render_blocked

log = logging.getLogger(__name__)
router = APIRouter()

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
FORGE_TXT2IMG_URL = "http://127.0.0.1:7861/sdapi/v1/txt2img"
PIN_FILE = Path(__file__).resolve().parent.parent.parent / "admin_pin.json"
COOKIE_NAME = "dcs_admin_session"
SESSION_TTL_SECONDS = 12 * 3600
BATCH_SIZE = 4
# A real chat message ("I like #2's lighting, want #4's pose, make her
# taller, change the setting to a rainy street at night") needs more room
# than a short tag -- bounded generously, not accumulated round over round
# (the LLM rewrites the prompt fresh each round, so there's no dilution risk
# the way raw-concat had).
MAX_FEEDBACK_CHARS = 500

# -- Vision-chat system prompt --------------------------------------------------
# Prompt-engineering rules 1-6 below are the SAME wording already proven in
# features/chat_studio's SYSTEM_PROMPT (reused verbatim, not re-derived) --
# see that file for why. Rule 7 is specific to THIS checkpoint
# (perfection25D_illustrious) and this session: proven FOUR times today that
# naming a concept in the negative prompt can summon it rather than suppress
# it (clothing, armor/tattoo, doll-face, and a solo-female render that
# hallucinated a male partner because "penis, male anatomy" was negated --
# see core/image_presets.py's _FEMALE_NEGATIVE comment for the exact
# incidents). Chat Studio's own rules don't cover the negative-prompt half of
# that lesson, so it's added here explicitly rather than assumed.
ADMIN_CHAT_SYSTEM_PROMPT = """You are a prompt-refinement assistant for Andrew's local NSFW image-generation \
admin tool. Each round shows him 4 candidate images generated from one shared scene prompt (numbered 1-4, \
left-to-right then top-to-bottom, matching how they're laid out on his screen). He describes what he wants \
changed -- comparing images, praising or rejecting specific ones, asking for a different setting, pose, look, \
etc. Your job is to turn that into an updated, ready-to-render scene prompt for the NEXT batch.

You ALWAYS reply with ONE JSON object and nothing else -- no prose outside the JSON, no markdown code fences:
{"reply": "<1-3 sentence conversational reply, tell him plainly what you changed>", \
"updated_prompt": "<the full rewritten scene prompt>" or null, \
"negative_additions": "<short addition for a defect he actually described, or empty string>", \
"anchor_image": <1, 2, 3, or 4 -- whichever image his message implies should seed the next batch> or null}

- Set "updated_prompt" to the FULL scene prompt for next round (not a diff/patch) whenever he wants ANY \
change to subject, setting, pose, lighting, style, etc. Leave it null only for pure conversation that asks \
for no change (e.g. "which one do you like better").
- Set "anchor_image" when he clearly prefers one image as the base to iterate from (praising it, saying \
"like #2 but...", "keep the last one"). Leave it null if he's asking for something fresh, rejects all 4, or \
doesn't indicate a preference.
- "reply" is always required -- it's the only thing he reads as your response, so it must stand on its own.

PROMPT RULES -- follow these exactly when writing "updated_prompt":
1. Order tokens subject-first: medium > subject + attributes > action > clothing > setting > framing > \
lighting > palette > 2-3 style anchors max. Keep attributes adjacent to the noun they describe.
2. Stay under 75 tokens for the positive prompt.
3. NEVER use negation in the positive prompt -- "no hat" draws a hat. Describe the presence of what you \
want instead ("clean-shaven" not "no beard", "bare head" not "no hat").
4. Be concrete, not evocative -- describe the photograph you want in frame, not the feeling it should evoke.
5. Assume an SDXL checkpoint at native ~1024x1024 resolution.
6. "negative_additions" starts EMPTY. Only fill it in when he reports an ACTUAL defect he saw (extra fingers, \
warped eyes, an artifact) -- max 0-10 tokens, never speculative, never a body-shape or pose preference (those \
go in the positive prompt instead, per rule 7).
7. NEVER put a body-shape, pose, clothing, or anatomy concept in "negative_additions" just to suppress it, \
even one he explicitly said he dislikes -- on this checkpoint, naming a concept in the negative prompt can \
summon it instead of removing it (proven repeatedly). If he dislikes something, describe the POSITIVE \
opposite in "updated_prompt" instead of negating the disliked thing. "negative_additions" is ONLY for true \
render defects (warped anatomy, extra limbs, garbled text/artifacts), never for content he doesn't want.

You are proposing the next round's prompt, not making the final call -- he can always type again to redirect."""

_SESSIONS: dict[str, dict] = {}
# Serializes the whole "check lockout -> read body -> verify PIN -> maybe
# record a failure" sequence in unlock() below. Without this, the lockout
# check and the failure-recording were on opposite sides of an `await`
# (_read_json_capped) with no atomicity between them -- any number of
# concurrent /unlock requests could all pass the lockout check before any
# of them had registered a failure, so LOCKOUT_THRESHOLD/
# GLOBAL_LOCKOUT_THRESHOLD only bounded SEQUENTIAL guessing, not a
# concurrent burst. PIN checks are rare in normal use, so fully serializing
# them has no real cost.
_pin_lock = asyncio.Lock()
_FAILED_ATTEMPTS: dict[str, list] = {}  # ip -> [timestamps of recent failures]
LOCKOUT_THRESHOLD = 5
LOCKOUT_SECONDS = 300
# Per-IP lockout alone caps brute force at LOCKOUT_THRESHOLD guesses per
# source address, but does nothing against a DISTRIBUTED attempt (many real,
# distinct source IPs each staying under that per-IP cap) -- easy over IPv6
# or a NAT-shared pool. This global cap catches that case; it's cheap and
# harmless in the normal single-user case since real usage never approaches
# it. (Separately, if this ever sits behind an HTTP reverse proxy that
# terminates TLS, request.client.host can collapse to the proxy's own
# loopback for every caller -- that makes the PER-IP cap itself into a
# repeatable self-DoS, 5 bad guesses from anywhere locking out Andrew too, and
# no lockout-counter design fixes that; it needs a real client-IP header from
# a trusted proxy instead. Not a concern for the direct-Tailscale access this
# was built for, since Tailscale doesn't proxy/rewrite the peer address.)
_GLOBAL_FAILURES: list = []
GLOBAL_LOCKOUT_THRESHOLD = 20
# Starlette buffers the whole body before request.json() returns -- cap it so
# a caller can't push oversized POSTs for cheap memory pressure. Generous
# enough for any real prompt/feedback text (MAX_FEEDBACK_CHARS is 200,
# image_presets.MAX_PROMPT_CHARS is 2000).
MAX_BODY_BYTES = 16_384


async def _read_json_capped(request: Request) -> dict:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "request body too large")
    try:
        return json.loads(body or b"{}")
    except Exception:
        raise HTTPException(400, "invalid JSON body")


def _get_or_create_pin() -> str:
    if PIN_FILE.exists():
        try:
            return json.loads(PIN_FILE.read_text(encoding="utf-8"))["pin"]
        except Exception:
            pass
    pin = f"{secrets.randbelow(1_000_000):06d}"
    PIN_FILE.write_text(json.dumps({"pin": pin}), encoding="utf-8")
    log.warning("[admin-review] generated a new admin PIN, saved to %s -- PIN: %s", PIN_FILE, pin)
    return pin


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_locked_out(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _FAILED_ATTEMPTS.get(ip, []) if now - t < LOCKOUT_SECONDS]
    _FAILED_ATTEMPTS[ip] = attempts
    if not attempts:
        _FAILED_ATTEMPTS.pop(ip, None)
    global _GLOBAL_FAILURES
    _GLOBAL_FAILURES = [t for t in _GLOBAL_FAILURES if now - t < LOCKOUT_SECONDS]
    return len(attempts) >= LOCKOUT_THRESHOLD or len(_GLOBAL_FAILURES) >= GLOBAL_LOCKOUT_THRESHOLD


def _record_failure(ip: str) -> None:
    _FAILED_ATTEMPTS.setdefault(ip, []).append(time.time())
    _GLOBAL_FAILURES.append(time.time())


def _require_session(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    sess = _SESSIONS.get(token) if token else None
    if not sess or time.time() - sess["last_seen"] > SESSION_TTL_SECONDS:
        _SESSIONS.pop(token, None)
        raise HTTPException(401, "not authenticated -- enter the PIN again")
    sess["last_seen"] = time.time()
    return sess


@router.post("/unlock")
async def unlock(request: Request, response: Response):
    ip = _client_ip(request)
    async with _pin_lock:
        if _is_locked_out(ip):
            raise HTTPException(429, "too many wrong PIN attempts -- wait a few minutes")
        body = await _read_json_capped(request)
        pin = str(body.get("pin") or "")
        if not secrets.compare_digest(pin, _get_or_create_pin()):
            _record_failure(ip)
            raise HTTPException(403, "wrong PIN")
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {
        "created": time.time(), "last_seen": time.time(),
        "prompt": "", "subject": "auto", "preset": "nsfw", "creature": False,
        "negative_extra": "", "anchor_seed": None, "round": 0,
        "last_seeds": [], "last_image_paths": [], "chat_log": [],
    }
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return {"ok": True}


def _save_image(img_bytes: bytes) -> tuple[str, str]:
    ts = time.strftime("%Y-%m-%d")
    job_slug = f"adminreview_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / ts / job_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"img_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}.png"
    out_path = out_dir / fname
    out_path.write_bytes(img_bytes)
    # Served through OUR OWN authenticated route below, not the app-wide
    # GET /output/{path:path} (which has no auth at all -- fine for the rest
    # of the app since it's local-only by design, but it would silently
    # defeat this feature's whole PIN gate the moment /admin is reached over
    # a tunnel: anyone who ever saw an image_url -- browser history, a
    # screenshot, a proxy log -- could fetch the image directly, no PIN
    # required). Caught by an independent security review 2026-07-31, fixed
    # before this ever left localhost.
    return f"/api/admin-review/image/{ts}/{job_slug}/{fname}", str(out_path)


@router.get("/image/{ts}/{slug}/{fname}")
async def get_image(ts: str, slug: str, fname: str, request: Request):
    _require_session(request)  # 401s if not authenticated -- same gate as /round
    if not slug.startswith("adminreview_"):
        raise HTTPException(404)
    candidate = (OUTPUT_DIR / ts / slug / fname).resolve()
    try:
        candidate.relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        raise HTTPException(404)  # path traversal attempt via ts/slug/fname
    if not candidate.is_file():
        raise HTTPException(404)
    return FileResponse(candidate)


async def _generate_one(prompt, negative_prompt, preset_key, subject, creature, width, height, steps, cfg_scale, seed):
    payload, resolved_subject, creature_applied = image_presets.build_forge_payload(
        preset_key, prompt, negative_prompt, width, height, steps, cfg_scale, seed, subject, creature,
    )
    r = await asyncio.to_thread(requests.post, FORGE_TXT2IMG_URL, json=payload, timeout=600)
    if not r.ok:
        raise RuntimeError(f"Forge returned {r.status_code}: {r.text[:200]}")
    result = r.json()
    images = result.get("images") or []
    if not images:
        raise RuntimeError("Forge returned no image")
    img_bytes = base64.b64decode(images[0])
    actual_seed = seed
    try:
        info = json.loads(result.get("info") or "{}")
        if info.get("seed") is not None:
            actual_seed = info.get("seed")
    except Exception:
        pass
    image_url, image_path = _save_image(img_bytes)
    return {"image_url": image_url, "image_path": image_path, "seed": actual_seed}


def _interpret_chat(sess: dict, chat_message: str) -> dict | None:
    """Send the admin's message + the 4 images currently on screen to a vision
    LLM, get back {reply, updated_prompt, negative_additions,
    anchor_original_index}. anchor_original_index is already resolved to a
    real 0-based index into sess["last_seeds"]/last_image_paths -- not the
    LLM's raw 1-based "image N" answer, which is numbered over whatever
    subset of images actually survived b64 encoding and so cannot be
    assumed to line up positionally with last_seeds.
    Returns None on any failure (bad response, provider error) so the caller
    can fall back to raw text -- never raises, this is a best-effort call."""
    from app import get_llm_router
    from core.llm_client import TIER_BALANCED, encode_image_b64, parse_json_response

    image_paths = [p for p in sess.get("last_image_paths", []) if p]
    b64_images = []
    # last_image_paths is already compacted to stay parallel with
    # sess["last_seeds"] (a failed GENERATION is dropped from both at the
    # same index). But encode_image_b64() can ALSO fail independently here
    # (missing/corrupt file, I/O error) on an image that generated fine --
    # if that happens mid-list, b64_images ends up shorter than
    # image_paths with a gap the vision LLM never sees, so its 1-based
    # "image N" numbering (based on len(b64_images)) would silently drift
    # out of alignment with last_seeds for every image after the gap.
    # Track which ORIGINAL indices survived so anchor_image can be mapped
    # back correctly regardless of where a gap falls.
    survived_indices = []
    for i, p in enumerate(image_paths[:BATCH_SIZE]):
        b64 = encode_image_b64(p)
        if b64:
            b64_images.append(b64)
            survived_indices.append(i)
    if not b64_images:
        return None

    hist_lines = []
    for h in sess.get("chat_log", [])[-6:]:
        hist_lines.append(f"{h['role']}: {h['text']}")
    history_block = ("\n".join(hist_lines) + "\n\n") if hist_lines else ""

    vision_prompt = (
        f"{history_block}Current scene prompt: {sess['prompt']}\n"
        f"Current negative additions: {sess['negative_extra'] or '(none)'}\n"
        f"{len(b64_images)} images shown below, numbered 1-{len(b64_images)} in the order given.\n\n"
        f"Admin: {chat_message}"
    )

    llm_router = get_llm_router()
    try:
        raw = llm_router.route_vision(
            vision_prompt, b64_images, system=ADMIN_CHAT_SYSTEM_PROMPT,
            tier=TIER_BALANCED, max_tokens=700,
        )
        parsed = parse_json_response(raw or "")
    except Exception as e:
        log.warning("[admin-review] vision-chat call failed: %s", e)
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("reply"), str):
        log.warning("[admin-review] vision-chat returned unusable response: %r", raw)
        return None

    updated_prompt = parsed.get("updated_prompt")
    anchor_image = parsed.get("anchor_image")
    anchor_original_index = None
    if isinstance(anchor_image, int) and 1 <= anchor_image <= len(b64_images):
        # anchor_image is 1-based over the (possibly gappy) b64_images the
        # LLM actually saw -- map it back through survived_indices to the
        # ORIGINAL 0-based index into image_paths/sess["last_seeds"],
        # instead of assuming direct positional alignment.
        anchor_original_index = survived_indices[anchor_image - 1]
    return {
        "reply": parsed["reply"].strip()[:600],
        "updated_prompt": updated_prompt.strip() if isinstance(updated_prompt, str) and updated_prompt.strip() else None,
        "negative_additions": (parsed.get("negative_additions") or "").strip()[:150] if isinstance(parsed.get("negative_additions"), str) else "",
        "anchor_original_index": anchor_original_index,
    }


@router.post("/round")
async def round_(request: Request):
    from core.gpu_orchestrator import gpu

    sess = _require_session(request)
    body = await _read_json_capped(request)
    reset = bool(body.get("reset") is True)

    # Check GPU availability BEFORE any session mutation below (the reset
    # block, and especially the chat/LLM-interpretation block, which spends
    # a real vision-LLM call and appends chat_log entries). This check
    # doesn't depend on the prompt at all, so there's no correctness reason
    # to defer it -- doing so meant a 409 aborted the request only AFTER
    # those mutations had already landed, so a retry (the obvious response
    # to "GPU busy, try again") re-ran the chat interpretation a second
    # time on the same feedback_text, duplicating the chat_log entry and
    # spending a second LLM call, with the admin never having gotten
    # confirmation the first attempt did anything. (The nsfw_render_blocked
    # check a bit further down stays where it is -- it must run on the
    # prompt AFTER chat interpretation folds this round's message in, or it
    # would be checking stale content and could miss something this round's
    # message just introduced.)
    if gpu.is_wangp_rendering():
        raise HTTPException(409, "Video render in progress -- image generation waits for the GPU")

    if reset or not sess["prompt"]:
        raw_prompt = body.get("prompt")
        sess["prompt"] = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
        if not sess["prompt"]:
            raise HTTPException(400, "prompt required to start a round")
        subject = body.get("subject")
        sess["subject"] = subject if isinstance(subject, str) and subject in image_presets.SUBJECT_CHOICES else "auto"
        preset_key = body.get("preset")
        sess["preset"] = preset_key if isinstance(preset_key, str) and preset_key in image_presets.PRESETS else "nsfw"
        sess["creature"] = bool(body.get("creature") is True)
        sess["negative_extra"] = ""
        sess["anchor_seed"] = None
        sess["round"] = 0
        sess["last_seeds"] = []
        sess["last_image_paths"] = []
        sess["last_seeds_by_slot"] = []
        sess["chat_log"] = []

    selected_index = body.get("selected_index")
    slots = sess.get("last_seeds_by_slot") or []
    if isinstance(selected_index, int) and 0 <= selected_index < len(slots) and slots[selected_index] is not None:
        sess["anchor_seed"] = slots[selected_index]
        explicit_anchor = True
    elif selected_index is not None:
        raise HTTPException(400, "selected_index out of range")
    else:
        sess["anchor_seed"] = None  # no pick yet this round -- chat may still set one below
        explicit_anchor = False

    chat_message = body.get("feedback_text")
    chat_message = chat_message.strip() if isinstance(chat_message, str) else ""
    assistant_reply = None
    llm_used = False

    if chat_message and sess["last_image_paths"]:
        sess["chat_log"].append({"role": "Admin", "text": chat_message[:MAX_FEEDBACK_CHARS]})
        interpreted = await asyncio.to_thread(_interpret_chat, sess, chat_message[:MAX_FEEDBACK_CHARS])
        if interpreted:
            llm_used = True
            assistant_reply = interpreted["reply"]
            sess["chat_log"].append({"role": "Assistant", "text": assistant_reply})
            if interpreted["updated_prompt"]:
                sess["prompt"] = image_presets.clamp_prompt_text(interpreted["updated_prompt"])
            sess["negative_extra"] = interpreted["negative_additions"]
            if not explicit_anchor and interpreted["anchor_original_index"] is not None:
                idx = interpreted["anchor_original_index"]  # already a real index into last_seeds -- see _interpret_chat
                if 0 <= idx < len(sess["last_seeds"]):
                    sess["anchor_seed"] = sess["last_seeds"][idx]
        else:
            # Vision call unavailable/failed -- degrade gracefully rather than
            # silently drop his message: fold it in as a raw prompt addition,
            # same as this endpoint's original (pre-chat) behavior.
            sess["prompt"] = image_presets.clamp_prompt_text(f"{sess['prompt']}, {chat_message[:MAX_FEEDBACK_CHARS]}")
    elif chat_message and not sess["last_image_paths"]:
        # No prior images to chat about yet (shouldn't normally happen -- the
        # client only sends feedback_text after round 1) -- fold in as text.
        sess["prompt"] = image_presets.clamp_prompt_text(f"{sess['prompt']}, {chat_message[:MAX_FEEDBACK_CHARS]}")

    composed_prompt = sess["prompt"]
    negative_prompt = sess["negative_extra"]

    # Not a redundant repeat of the early check above -- this is a second,
    # deliberate checkpoint: chat interpretation just above can spend a real
    # few-second vision-LLM round trip, during which a video render could
    # have started. Re-checking right before the actual (expensive) image
    # generation catches that window without re-doing the early-exit
    # benefit the first check provides for the common "already busy" case.
    if gpu.is_wangp_rendering():
        raise HTTPException(409, "Video render in progress -- image generation waits for the GPU")

    scene_text = f"{composed_prompt}\n{negative_prompt}".strip()
    if await nsfw_render_blocked(scene_text):
        raise HTTPException(403, "Blocked by safety check -- this request could not be verified as depicting adults only.")

    config = cfg.load()
    width = config.get("chat_image_width", 1024)
    height = config.get("chat_image_height", 1024)
    steps = config.get("chat_image_steps", 25)
    cfg_scale = config.get("chat_image_cfg", 5.0)

    anchor = sess["anchor_seed"]
    if anchor is not None:
        # Close variants of the picked image -- same neighborhood, not identical.
        seeds = [max(0, min(4294967294, anchor + i)) for i in range(BATCH_SIZE)]
    else:
        seeds = [-1] * BATCH_SIZE  # fresh spread, real variety per _pick_appearance

    results = await asyncio.gather(*[
        _generate_one(composed_prompt, negative_prompt, sess["preset"], sess["subject"], sess["creature"],
                       width, height, steps, cfg_scale, s)
        for s in seeds
    ], return_exceptions=True)

    images = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            log.error("[admin-review] round %s image %s failed: %s", sess["round"], i, res)
            images.append({"error": str(res)})
        else:
            images.append({**res, "index": i})

    sess["last_seeds"] = [img.get("seed") for img in images if "seed" in img]
    sess["last_image_paths"] = [img.get("image_path") for img in images if "image_path" in img]
    # Raw-slot-indexed (None for a failed slot, same length/order as `images`)
    # -- selected_index from the client is always a raw index over the full,
    # unfiltered batch (admin_review.html renders one tile per `images` entry
    # including failed ones), so it must not be looked up against last_seeds
    # above, which silently reindexes past any failed tile.
    sess["last_seeds_by_slot"] = [img.get("seed") for img in images]
    sess["round"] += 1

    log.info("[admin-review] round %s: subject=%s preset=%s anchor=%s llm_used=%s prompt=%r",
              sess["round"], sess["subject"], sess["preset"], anchor, llm_used, composed_prompt)

    return {
        "round": sess["round"],
        "images": images,
        "prompt_used": composed_prompt,
        "negative_used": negative_prompt,
        "anchor_used": anchor is not None,
        "subject": sess["subject"],
        "preset": sess["preset"],
        "assistant_reply": assistant_reply,
        "llm_used": llm_used,
    }
