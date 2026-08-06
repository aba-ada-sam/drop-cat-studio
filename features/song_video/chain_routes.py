"""Routes for the RATIFIED engine wrapper -- /api/song-video/chain/*

Mounted separately from the legacy song_video router (routes.py, still
/api/song-video/*) so the ratified chain.py path and the older pipeline.py
path stay clearly separated in code, matching the UI split (tab-music-
video.js: "Ratified Engine" primary surface, "Legacy pipeline" collapsible).

See features/song_video/chain_runner.py for the subprocess wrapper and
progress-parsing this module drives.
"""
import logging
import os

from fastapi import APIRouter, HTTPException, Request

from features.song_video import chain_runner

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/start")
async def chain_start(request: Request):
    """Start a ratified-engine (chain.py) render.

    Body:
        song_path      str    path to an uploaded/generated song file
                               (optional -- omit/blank to generate one via
                               ACE-Step, same as the legacy /generate route)
        images         list   1+ anchor image paths (required)
        scene_prompts  list   optional per-scene prompts, parallel to images
                               (joined with "||" exactly like the ratified
                               CLI command)
        seeds_per_clip int    default 4 (ratified command)
        judge_select   bool   default True (ratified default, UI-togglable)
        target_length  float  seconds -- only used when song_path is blank
        lyrics_text    str    only used when song_path is blank
        music_prompt   str    only used when song_path is blank
        dof_finish     bool   NOT IMPLEMENTED -- any truthy value is a 400
    """
    if chain_runner.is_running():
        raise HTTPException(
            400, "A ratified-engine render is already running -- wait for it "
                 "to finish or cancel it first.")

    # GPU busy check -- refuse rather than let chain.py's subprocess collide
    # with a live WanGP render on the same physical GPU. NOTE (see
    # VALIDATION_NEEDED.md): this only sees V2's own worker (:7897, via
    # core.gpu_orchestrator -> services.manager.WANGP_WORKER_PORT). V1's
    # worker (:7899) is a separate process this orchestrator cannot see --
    # a render running there (e.g. tonight's v16) will NOT trip this check
    # even though both share the same physical RTX 5080.
    from core.gpu_orchestrator import gpu
    if gpu.is_wangp_rendering():
        raise HTTPException(
            400, "WanGP is actively rendering on the GPU right now -- the "
                 "ratified engine needs the same GPU. Wait for the current "
                 "render to finish (or cancel it) before starting.")

    body = await request.json()

    song_path = (body.get("song_path") or "").strip()
    if song_path and not os.path.isfile(song_path):
        raise HTTPException(400, f"Song file not found: {song_path}")

    raw_images = body.get("images") or []
    if isinstance(raw_images, str):
        raw_images = [raw_images]
    images = [p for p in raw_images if isinstance(p, str) and p.strip()]
    if not images:
        raise HTTPException(400, "At least one anchor image is required")
    for p in images:
        if not os.path.isfile(p):
            raise HTTPException(400, f"Anchor image not found: {p}")

    raw_prompts = body.get("scene_prompts") or []
    if isinstance(raw_prompts, str):
        raw_prompts = [raw_prompts]
    scene_prompts = [s.strip() for s in raw_prompts if isinstance(s, str) and s.strip()]

    if body.get("dof_finish"):
        raise HTTPException(
            400, "DOF finish is not implemented yet (see "
                 "features/song_video/dof_finish.py) -- leave that checkbox off.")

    try:
        seeds_per_clip = max(1, min(10, int(body.get("seeds_per_clip", 4))))
    except (TypeError, ValueError):
        raise HTTPException(400, "seeds_per_clip must be an integer")

    try:
        target_length = max(5.0, min(120.0, float(body.get("target_length", 30))))
    except (TypeError, ValueError):
        raise HTTPException(400, "target_length must be a number")

    try:
        job = chain_runner.start(
            images=images,
            song_path=song_path or None,
            scene_prompts=scene_prompts or None,
            seeds_per_clip=seeds_per_clip,
            judge_select=bool(body.get("judge_select", True)),
            target_length=target_length,
            lyrics_text=body.get("lyrics_text", "") or "",
            music_prompt=body.get("music_prompt", "") or "",
            output_path=body.get("output_path") or None,
        )
    except FileNotFoundError as e:
        # engine/ missing (e.g. a dev worktree) -- distinct from "busy".
        raise HTTPException(503, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return job


@router.get("/status")
async def chain_status():
    """Poll target for the running (or last-run) chain job. See
    chain_runner.ChainJobState.to_dict() for the full shape (status, phase,
    pct, clip_i/clip_n, take_i/take_n, log_tail, output_path, error)."""
    return chain_runner.get_status()


@router.post("/cancel")
async def chain_cancel():
    """Terminate the running chain job (process tree -- chain.py shells out
    to ffmpeg for concat/mux/stabilize/upscale, so a plain terminate() would
    orphan those)."""
    return chain_runner.cancel()
