"""SD Prompts API routes — /api/prompts/*

Image → SD prompt generation with wildcard support and iterative refinement.
Ported from DropCatGo-SD-Prompts (Gradio → FastAPI REST).
"""
import logging
import os
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from core import config as cfg
from core.wildcards import discover_filesystem_wildcards, invalidate_cache
from features.sd_prompts.prompt_engine import AVAILABLE_MODELS, generate_prompts, refine_prompts
from features.sd_prompts.wildcard_manager import (
    _read_file_lines,
    _write_entries,
    ai_audit,
    ai_expand,
    ai_merge,
    ai_prune,
    curator_analyze,
    curator_plan,
    parse_plan_actions,
)

log = logging.getLogger(__name__)
router = APIRouter()

# Server-side conversation state keyed by session
_conv_states: dict[str, dict] = {}


def _get_llm_router():
    """Return the global llm_router (Ollama-backed)."""
    from app import get_llm_router
    try:
        return get_llm_router()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


def _get_wildcards_dir() -> str:
    return cfg.get("sd_wildcards_dir") or ""


def _build_entries_summary(wc_dir: str) -> str:
    """Build a summary of all wildcard files for AI operations."""
    wildcards = discover_filesystem_wildcards(wc_dir)
    lines = []
    for token, values in sorted(wildcards.items()):
        sample = ", ".join(values[:6])
        lines.append(f"{token} ({len(values)} entries): {sample}")
    return "\n".join(lines) if lines else "(no wildcard files found)"


# ── Prompt Generation ────────────────────────────────────────────────────────

@router.post("/generate")
async def gen_prompts(request: Request):
    body = await request.json()
    llm_router = _get_llm_router()

    image_path = body.get("image_path")
    concept = body.get("concept", "")
    extra = body.get("extra_instructions", "")
    model = body.get("model", cfg.get("sd_model") or "claude-sonnet-4-6")
    session_id = body.get("session_id", "default")

    # Get wildcard info
    wc_dir = _get_wildcards_dir()
    wc_labels = []
    wc_samples = {}
    if wc_dir:
        wildcards = discover_filesystem_wildcards(wc_dir)
        for token, values in wildcards.items():
            label = token.strip("_")
            wc_labels.append(label)
            wc_samples[label] = values[:15]

    # Also include selected wildcards from request
    selected = body.get("selected_wildcards", [])
    if selected:
        wc_labels = [l for l in wc_labels if l in selected]

    parsed, conv_state = generate_prompts(
        llm_router,
        image_path=image_path,
        concept=concept,
        wildcard_labels=wc_labels,
        wildcard_samples=wc_samples,
        extra_instructions=extra,
        model=model,
    )

    # Store conversation state server-side
    _conv_states[session_id] = conv_state

    return {
        "base_prompt": parsed.get("base_prompt", ""),
        "columns": parsed.get("columns", ["", "", ""]),
        "raw": parsed.get("raw", ""),
        "session_id": session_id,
    }


@router.post("/refine")
async def refine(request: Request):
    body = await request.json()
    llm_router = _get_llm_router()

    feedback = body.get("feedback", "")
    session_id = body.get("session_id", "default")
    model = body.get("model", cfg.get("sd_model") or "claude-sonnet-4-6")

    conv_state = _conv_states.get(session_id)
    if not conv_state:
        raise HTTPException(400, "No active session — generate prompts first")
    if not feedback:
        raise HTTPException(400, "Feedback required")

    parsed, new_state = refine_prompts(llm_router, conv_state, feedback, model)
    _conv_states[session_id] = new_state

    return {
        "base_prompt": parsed.get("base_prompt", ""),
        "columns": parsed.get("columns", ["", "", ""]),
        "raw": parsed.get("raw", ""),
        "session_id": session_id,
    }


@router.get("/models")
async def list_models():
    from core.keys import get_ollama_models
    models = get_ollama_models() or AVAILABLE_MODELS
    return {"models": models}


# ── Wildcard Management ──────────────────────────────────────────────────────

@router.get("/wildcards")
async def list_wildcard_files():
    wc_dir = _get_wildcards_dir()
    if not wc_dir:
        return {"files": [], "error": "Wildcards directory not configured"}
    wildcards = discover_filesystem_wildcards(wc_dir)
    files = []
    for token, values in sorted(wildcards.items()):
        files.append({"token": token, "count": len(values), "samples": values[:5]})
    return {"files": files, "directory": wc_dir}


@router.post("/prune")
async def prune_wildcard(request: Request):
    body = await request.json()
    llm_router = _get_llm_router()
    path = body.get("path", "")
    level = int(body.get("level", 3))
    model = body.get("model", cfg.get("sd_model") or "claude-sonnet-4-6")

    if not path or not os.path.isfile(path):
        raise HTTPException(400, "File not found")

    entries = _read_file_lines(path)
    label = Path(path).stem
    result = ai_prune(llm_router, label, entries, level, model)

    if body.get("apply", False) and result["kept"]:
        _write_entries(path, result["kept"])
        invalidate_cache()

    return result


@router.post("/expand")
async def expand_wildcard(request: Request):
    body = await request.json()
    llm_router = _get_llm_router()
    path = body.get("path", "")
    count = int(body.get("count", 20))
    model = body.get("model", cfg.get("sd_model") or "claude-sonnet-4-6")

    if not path or not os.path.isfile(path):
        raise HTTPException(400, "File not found")

    entries = _read_file_lines(path)
    label = Path(path).stem
    new_entries = ai_expand(llm_router, label, entries, count, model)

    if body.get("apply", False) and new_entries:
        all_entries = entries + new_entries
        _write_entries(path, all_entries)
        invalidate_cache()

    return {"new_entries": new_entries, "count": len(new_entries)}


@router.post("/merge")
async def merge_wildcards(request: Request):
    body = await request.json()
    llm_router = _get_llm_router()
    paths = body.get("paths", [])
    model = body.get("model", cfg.get("sd_model") or "claude-sonnet-4-6")

    if len(paths) < 2:
        raise HTTPException(400, "Need at least 2 files to merge")

    files_data = []
    for p in paths:
        if not os.path.isfile(p):
            raise HTTPException(400, f"File not found: {p}")
        files_data.append((Path(p).stem, _read_file_lines(p)))

    merged = ai_merge(llm_router, files_data, model)

    output_path = body.get("output_path")
    if output_path and merged:
        _write_entries(output_path, merged)
        invalidate_cache()

    return {"merged": merged, "count": len(merged)}


@router.post("/audit")
async def audit_library(request: Request):
    body = await request.json()
    llm_router = _get_llm_router()
    model = body.get("model", cfg.get("sd_model") or "claude-sonnet-4-6")

    wc_dir = _get_wildcards_dir()
    summary = _build_entries_summary(wc_dir)
    report = ai_audit(llm_router, summary, model)
    return {"report": report}


# ── Auto Curator ─────────────────────────────────────────────────────────────

@router.post("/curator/analyze")
async def curator_analyze_endpoint(request: Request):
    body = await request.json()
    llm_router = _get_llm_router()
    model = body.get("model", cfg.get("sd_model") or "claude-sonnet-4-6")
    instructions = body.get("instructions", "")

    wc_dir = _get_wildcards_dir()
    summary = _build_entries_summary(wc_dir)
    analysis = curator_analyze(llm_router, summary, instructions, model)
    return {"analysis": analysis}


@router.post("/curator/plan")
async def curator_plan_endpoint(request: Request):
    body = await request.json()
    llm_router = _get_llm_router()
    model = body.get("model", cfg.get("sd_model") or "claude-sonnet-4-6")

    wc_dir = _get_wildcards_dir()
    summary = _build_entries_summary(wc_dir)
    analysis = body.get("analysis", "")
    answers = body.get("answers", "")
    instructions = body.get("instructions", "")

    plan_text = curator_plan(llm_router, summary, analysis, answers, instructions, model)
    actions = parse_plan_actions(plan_text)
    return {"plan_text": plan_text, "actions": actions}


# ── Forge Integration (SD Image Generation) ─────────────────────────────────

def _save_and_register(images_b64: list[str]) -> list[str]:
    """Save generated images to disk and register in the current session."""
    output_dir = str(Path(__file__).resolve().parent.parent.parent / "output")
    from services.forge_client import save_image
    from core.session import get_current as get_session
    paths = []
    for b64 in images_b64:
        path = save_image(b64, output_dir)
        if path:
            paths.append(path)
            get_session().add_file(Path(path).name, "image", "sd_prompts", path=path)
    return paths


@router.get("/forge/status")
async def forge_status():
    """Return Forge availability plus all live option lists."""
    from services.forge_client import (
        forge_alive, get_models, get_samplers, get_schedulers,
        get_loras, get_upscalers, get_current_model, _forge_url,
    )
    alive = forge_alive()
    forge_url = _forge_url()
    if not alive:
        return {"alive": False, "url": forge_url}

    return {
        "alive": True,
        "url": forge_url,
        "current_model": get_current_model(),
        "models": [
            {"title": m.get("title", ""), "name": m.get("model_name", "")}
            for m in get_models()
        ],
        "samplers": get_samplers(),
        "schedulers": get_schedulers(),
        "loras": [{"name": l.get("name", ""), "alias": l.get("alias", "")} for l in get_loras()],
        "upscalers": get_upscalers(),
    }


@router.post("/forge/set-model")
async def forge_set_model(request: Request):
    """Switch the loaded checkpoint model."""
    from services.forge_client import set_model
    body = await request.json()
    name = body.get("model", "")
    if not name:
        raise HTTPException(400, "Model name required")
    ok = set_model(name)
    return {"ok": ok}


@router.post("/forge/txt2img")
async def forge_txt2img(request: Request):
    """Generate image(s) via Forge txt2img.

    Supports: HiRes Fix, ADetailer, Forge Couple (regional), all samplers/schedulers.
    Prompt can include __wildcard__ tokens — Forge's dynamic-prompts extension
    resolves them automatically.

    For Forge Couple (regional prompting), pass use_forge_couple=true and
    the backend will join the SD prompt columns with '\\n' instead of BREAK.
    """
    from services.forge_client import (
        txt2img, build_adetailer_args, build_forge_couple_args,
    )

    body = await request.json()

    # Build prompt — handle Forge Couple column joining
    prompt = body.get("prompt", "")
    columns = body.get("columns", [])     # [left, center, right] from SD Prompts
    use_forge_couple = body.get("use_forge_couple", False)

    if columns and len([c for c in columns if c.strip()]) > 1:
        non_empty = [c.strip() for c in columns if c.strip()]
        if use_forge_couple:
            # Forge Couple: join with newline, first line is the background/global prompt
            if prompt:
                prompt = prompt + "\n" + "\n".join(non_empty)
            else:
                prompt = "\n".join(non_empty)
        else:
            # Standard BREAK regional conditioning
            if prompt:
                prompt = prompt + "\nBREAK\n" + "\nBREAK\n".join(non_empty)
            else:
                prompt = "\nBREAK\n".join(non_empty)

    if not prompt:
        raise HTTPException(400, "Prompt required")

    # Build extension args
    adetailer_args = build_adetailer_args(
        enabled=body.get("adetailer", False),
        model=body.get("adetailer_model", "face_yolov8n.pt"),
        denoising_strength=float(body.get("adetailer_denoise", 0.4)),
    ) if body.get("adetailer") else None

    forge_couple_args = build_forge_couple_args(
        enabled=use_forge_couple,
        background_weight=float(body.get("forge_couple_bg_weight", 0.5)),
    ) if use_forge_couple else None

    result = txt2img(
        prompt=prompt,
        negative_prompt=body.get("negative_prompt", ""),
        width=int(body.get("width", 1024)),
        height=int(body.get("height", 1024)),
        steps=int(body.get("steps", 25)),
        sampler_name=body.get("sampler", "DPM++ 2M SDE"),
        scheduler=body.get("scheduler", "Karras"),
        cfg_scale=float(body.get("cfg_scale", 7.0)),
        seed=int(body.get("seed", -1)),
        batch_size=int(body.get("batch_size", 1)),
        restore_faces=bool(body.get("restore_faces", False)),
        # HiRes Fix
        enable_hr=bool(body.get("enable_hr", False)),
        hr_scale=float(body.get("hr_scale", 2.0)),
        hr_upscaler=body.get("hr_upscaler", "ESRGAN_4x"),
        hr_second_pass_steps=int(body.get("hr_steps", 10)),
        hr_denoising_strength=float(body.get("hr_denoise", 0.3)),
        # Extensions
        adetailer=adetailer_args,
        forge_couple=forge_couple_args,
    )

    if result["error"]:
        raise HTTPException(500, f"Forge generation failed: {result['error']}")

    saved_paths = _save_and_register(result["images"])

    return {
        "images": result["images"],
        "saved_paths": saved_paths,
        "info": result["info"],
        "seed": result["info"].get("seed", -1),
        "prompt_sent": prompt,     # so UI can show what was actually sent
    }


@router.post("/forge/img2img")
async def forge_img2img(request: Request):
    """Refine an image using Forge img2img.

    Useful for iterating on a generated image or transforming an uploaded photo.
    """
    from services.forge_client import img2img, build_adetailer_args

    body = await request.json()
    init_image = body.get("init_image", "")
    prompt = body.get("prompt", "")

    if not init_image:
        raise HTTPException(400, "Input image required (base64)")
    if not prompt:
        raise HTTPException(400, "Prompt required")

    adetailer_args = build_adetailer_args(
        enabled=body.get("adetailer", False),
        model=body.get("adetailer_model", "face_yolov8n.pt"),
        denoising_strength=float(body.get("adetailer_denoise", 0.4)),
    ) if body.get("adetailer") else None

    result = img2img(
        init_image_b64=init_image,
        prompt=prompt,
        negative_prompt=body.get("negative_prompt", ""),
        denoising_strength=float(body.get("denoising_strength", 0.5)),
        width=int(body.get("width", 1024)),
        height=int(body.get("height", 1024)),
        steps=int(body.get("steps", 25)),
        sampler_name=body.get("sampler", "DPM++ 2M SDE"),
        scheduler=body.get("scheduler", "Karras"),
        cfg_scale=float(body.get("cfg_scale", 7.0)),
        seed=int(body.get("seed", -1)),
        resize_mode=int(body.get("resize_mode", 0)),
        restore_faces=bool(body.get("restore_faces", False)),
        adetailer=adetailer_args,
    )

    if result["error"]:
        raise HTTPException(500, f"Forge img2img failed: {result['error']}")

    saved_paths = _save_and_register(result["images"])

    return {
        "images": result["images"],
        "saved_paths": saved_paths,
        "info": result["info"],
        "seed": result["info"].get("seed", -1),
    }


@router.post("/forge/interrupt")
async def forge_interrupt():
    """Cancel the current Forge generation."""
    from services.forge_client import interrupt
    ok = interrupt()
    return {"ok": ok}


@router.get("/forge/progress")
async def forge_progress():
    """Get current Forge generation progress."""
    from services.forge_client import get_progress
    return get_progress()
