"""Infinite zoom-IN -- Forge SD detail dive, rot-free.

WHY THIS EXISTS
---------------
The old zoom chained AI video clips: each clip was regenerated from the previous
clip's last frame, so texture decay and skin "pox" compounded and the output
turned to garbage by the end. This rebuild never re-runs a model on a whole
frame. Instead:

    build_zoom_in_stack()  -- dive into the source: each level crops the centre,
                              scales it up, and Forge img2img (low denoise) paints
                              NEW detail in. Levels stay structurally aligned.
    render_zoom_in()       -- continuous crop-zoom that TEMPORALLY CROSSFADES each
                              sharper level in as the camera reaches it, so detail
                              emerges instead of switching -- the joins are invisible.
    run_oz_prep/pipeline   -- job glue: LLM detail prompt (no GPU) + Forge build +
                              render + optional ACE-Step music + output.

History: earlier composite renderers (pyramid / exact-nesting) and an outpaint
zoom-OUT path were tried and removed -- see git tag v1.12-outpaint-zoom and the
10-round rebuild notes. The temporal-crossfade renderer below is what shipped.
"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("zoom.outpaint")


# ---------------------------------------------------------------------------
# Zoom-IN renderer -- temporal crossfade so detail emerges, never switches
# ---------------------------------------------------------------------------

def render_zoom_in(
    stack,
    out_path: str,
    *,
    zoom_factor: float = 0.72,
    fps: int = 30,
    sec_per_level: float = 2.5,
    xfade: float = 0.55,
    log_fn=None,
) -> str | None:
    """Seamless zoom-IN. stack[0] = source; stack[k] = detailed magnification of
    stack[k-1]'s centre (structurally aligned, just sharper). Renders source ->
    deepest, and as the camera reaches each level it CROSSFADES the sharper level
    in over time -- so new detail materialises gradually instead of popping in at
    a spatial boundary. That temporal dissolve is what makes the joins invisible.
    """
    from PIL import Image

    def _log(m):
        log.info(m)
        if log_fn:
            log_fn(m)

    if not stack or len(stack) < 2:
        return None
    f = max(0.30, min(0.90, float(zoom_factor)))
    W, H = stack[0].size
    stack = [im if im.size == (W, H) else im.resize((W, H), Image.LANCZOS) for im in stack]
    N = len(stack) - 1
    per = max(2, int(round(fps * sec_per_level)))
    total = N * per
    xf = max(0.05, min(0.95, float(xfade)))

    def _cs(img, s):
        s = min(1.0, max(1e-3, s))
        cw, ch = max(2, round(W * s)), max(2, round(H * s))
        x0, y0 = (W - cw) // 2, (H - ch) // 2
        return img.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.LANCZOS)

    tmp = Path(tempfile.mkdtemp(prefix="dcs_zin_"))
    paths: list[str] = []
    idx = 0

    def _save(im):
        nonlocal idx
        p = str(tmp / f"f{idx:06d}.png")
        im.save(p)
        paths.append(p)
        idx += 1

    try:
        for _ in range(int(fps * 0.4)):
            _save(stack[0])
        for fi in range(total + 1):
            d = N * fi / total            # continuous depth 0 -> N
            b = min(N, int(d))            # level we're zooming into
            fb = d - b
            base = _cs(stack[b], f ** fb)  # zoom into level b's centre
            # As we enter level b, dissolve it in over the previous (over-magnified)
            # level so its added detail appears gradually, not as a hard ring.
            if b > 0 and fb < xf:
                a = fb / xf                # 0 -> show prev (soft), 1 -> show level b
                prev = _cs(stack[b - 1], f ** (fb + 1.0))
                base = Image.blend(prev, base, a)
            _save(base)
        for _ in range(int(fps * 0.4)):
            _save(stack[-1])

        seq = Path(tempfile.mkdtemp(prefix="dcs_zin_seq_"))
        for j, src in enumerate(paths):
            os.replace(src, str(seq / f"s{j:06d}.png"))
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        r = subprocess.run(
            ["ffmpeg", "-y", "-framerate", str(fps), "-i", str(seq / "s%06d.png"),
             "-c:v", "libx264", "-crf", "16", "-preset", "medium",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
            capture_output=True, timeout=300,
        )
        import shutil
        shutil.rmtree(seq, ignore_errors=True)
        if r.returncode != 0 or not os.path.isfile(out_path):
            _log(f"[zoom-in] ffmpeg failed: {r.stderr.decode(errors='replace')[-200:]}")
            return None
        _log(f"[zoom-in] rendered {idx} frames -> {out_path}")
        return out_path
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Build stage (Forge SD detail dive) -- generates the level stack
# ---------------------------------------------------------------------------

def _b64_png(img) -> str:
    import base64, io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _from_b64(s: str):
    import base64, io
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode(s))).convert("RGB")


def _match_color(img, ref):
    """Per-channel mean/std (Reinhard) transfer: pull img's exposure and colour
    onto ref's. Consecutive zoom levels are separate SD generations with their
    own brightness/contrast/tint; without this they step against each other and
    the zoom reads as a harsh cut every time it crosses a level boundary."""
    try:
        import numpy as np
        from PIL import Image
        a = np.asarray(img.convert("RGB"), dtype=np.float32)
        r = np.asarray(ref.convert("RGB").resize(img.size), dtype=np.float32)
        out = a.copy()
        for c in range(3):
            am, asd = a[..., c].mean(), a[..., c].std() + 1e-5
            rm, rsd = r[..., c].mean(), r[..., c].std() + 1e-5
            out[..., c] = (a[..., c] - am) * (rsd / asd) + rm
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
    except Exception:
        return img


def _gen_size(W: int, H: int, area_target: int = 1024 * 1024, max_edge: int = 1536):
    """SD-friendly canvas size (~1MP, multiples of 8) preserving aspect. SDXL
    quality drops well below ~0.8MP, so we generate near 1MP even if the source
    is smaller (SD invents the extra detail), then the chain works at that res."""
    ar = W / H
    th = (area_target / ar) ** 0.5
    tw = th * ar
    if max(tw, th) > max_edge:
        s = max_edge / max(tw, th)
        tw, th = tw * s, th * s
    W2 = max(64, int(round(tw)) // 8 * 8)
    H2 = max(64, int(round(th)) // 8 * 8)
    return W2, H2






# ---------------------------------------------------------------------------
# Zoom-IN build stage (Forge SD inpaint/detail) -- generates NEW detail inward
# ---------------------------------------------------------------------------
# Zoom-out (above) preserves the source and outpaints rings AROUND it. Zoom-in
# is the opposite problem: if you just magnify the source you hit its resolution
# wall and approach a fixed blurry thumbnail. So each inward level crops the
# centre, scales it up, and runs img2img to PAINT new high-frequency detail into
# it -- the camera keeps discovering fresh detail as it pushes in. Because every
# level magnifies a fresh region and regenerates at full resolution (not a
# downscaled re-encode like the old video chain), it sharpens rather than rots.

def build_zoom_in_stack(
    source_path: str,
    n_levels: int,
    prompt: str,
    *,
    negative_prompt: str = "blurry, low quality, watermark, text, soft, out of focus",
    zoom_factor: float = 0.5,
    steps: int = 24,
    cfg_scale: float = 6.0,
    denoise: float = 0.40,
    sampler: str = "DPM++ 2M",
    scheduler: str = "Karras",
    seed: int = -1,
    max_dim: int = 1024,
    stop_check=None,
    progress_fn=None,
    log_fn=None,
):
    """Build a stack that dives INTO the source, inpainting new detail each level.

    stack[0] = source; stack[k] = the magnified centre of stack[k-1] with fresh
    detail painted in. Returns (stack, (W,H)) or (None, None) if Forge is down.
    Render a zoom-in with: render_infinite_zoom(list(reversed(stack)), direction="in").
    """
    from PIL import Image
    from services import forge_client

    def _log(m):
        log.info(m)
        if log_fn:
            log_fn(m)

    if not forge_client.forge_alive():
        _log("[inpaint] Forge is not running -- cannot build zoom-in stack")
        return None, None

    f = max(0.30, min(0.85, float(zoom_factor)))
    src = Image.open(source_path).convert("RGB")
    W, H = _gen_size(*src.size)
    src = src.resize((W, H), Image.LANCZOS)

    stack = [src]
    iw, ih = max(8, round(W * f)), max(8, round(H * f))
    ox, oy = (W - iw) // 2, (H - ih) // 2
    for k in range(1, n_levels + 1):
        if stop_check and stop_check():
            return None, None
        if progress_fn:
            progress_fn(k, n_levels)
        _log(f"[inpaint] diving + detailing level {k}/{n_levels}")
        prev = stack[-1]
        # magnify the centre region to full frame (soft), then paint detail in
        magnified = prev.crop((ox, oy, ox + iw, oy + ih)).resize((W, H), Image.LANCZOS)
        res = forge_client.img2img(
            init_image_b64=_b64_png(magnified),
            prompt=prompt,
            negative_prompt=negative_prompt,
            denoising_strength=denoise,
            width=W, height=H, steps=steps,
            sampler_name=sampler, scheduler=scheduler,
            cfg_scale=cfg_scale, seed=seed, resize_mode=0,
        )
        if res.get("error") or not res.get("images"):
            _log(f"[inpaint] Forge failed at level {k}: {res.get('error')} -- stopping")
            break
        new = _from_b64(res["images"][0]).resize((W, H), Image.LANCZOS)
        new = _match_color(new, stack[-1])   # keep exposure consistent across levels
        stack.append(new)

    if len(stack) < 2:
        return None, None
    return stack, (W, H)


# ---------------------------------------------------------------------------
# Job pipeline -- prep (LLM detail prompt, no GPU) + run (Forge build + render)
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"

_OZ_DETAIL_SYSTEM = """\
You are directing an infinite zoom-IN into a single image. The camera dives
toward the centre and AI paints ever-finer detail as it magnifies. Write ONE
short prompt (12-25 words) describing the fine texture / micro-detail that should
emerge as the camera pushes into the CENTRE of this image: name the material,
surface, grain, structure. Concrete and physical. No camera words, no story.
Return ONLY the prompt text.\
"""


def run_oz_prep(job, source_path: str, settings: dict) -> None:
    """Phase 0 (no GPU): pick the detail prompt from the image + idea, and a
    music direction if audio is wanted. Results written into settings for the
    GPU phase."""
    import os as _os
    from app import get_llm_router
    from core.llm_client import TIER_FAST, encode_image_b64

    llm = get_llm_router()
    idea = (settings.get("idea") or "").strip()
    job.update(progress=4, message="Planning the zoom detail...")

    b64 = None
    if source_path and _os.path.isfile(source_path):
        try:
            b64 = encode_image_b64(source_path)
        except Exception:
            b64 = None

    prompt = ""
    try:
        if b64:
            msg = idea or "Describe the fine detail to reveal as we dive into the centre."
            text = llm.route_vision(msg, [b64], tier=TIER_FAST,
                                    system=_OZ_DETAIL_SYSTEM, max_tokens=80)
            prompt = (text or "").strip().strip('"').strip()
    except Exception as e:
        log.warning("[oz] detail-prompt vision failed: %s", e)
    if not prompt:
        prompt = ((idea + ", ") if idea else "") + (
            "extreme macro close-up, intricate fine surface detail, sharp texture, highly detailed")
    settings["_oz_prompt"] = prompt
    log.info("[oz] detail prompt: %s", prompt[:100])

    if not settings.get("skip_audio") and not settings.get("music_prompt"):
        try:
            from features.fun_videos import analyzer
            res = analyzer.generate_music_prompt(llm, [b64] if b64 else [], idea)
            if res.get("music_prompt"):
                settings["_oz_music"] = res["music_prompt"]
        except Exception as e:
            log.warning("[oz] music prep failed: %s", e)

    job.update(progress=10, message="Detail ready, waiting for GPU...")


def run_oz_pipeline(job, source_path: str, settings: dict) -> None:
    """GPU phase: Forge builds the detail stack, render the zoom-in, optional
    ACE-Step music, write output + gallery + session."""
    import os as _os
    import time as _time
    from app import gallery_push, get_llm_router
    from core.gpu_orchestrator import gpu
    from core.inbox import copy_to_inbox
    from core.ffmpeg_utils import probe_duration
    from services import forge_client
    from features.fun_videos import audio_generator, video_generator

    def _stopped() -> bool:
        return job.stop_event.is_set()

    n_levels = max(3, min(12, int(settings.get("n_levels", 7))))
    zf = float(settings.get("zoom_factor", 0.72))
    spl = float(settings.get("sec_per_level", 2.2))
    fps = int(settings.get("fps", 30))
    denoise = float(settings.get("denoise", 0.40))
    skip_audio = bool(settings.get("skip_audio", False))
    instrumental = bool(settings.get("instrumental", False))
    prompt = settings.pop("_oz_prompt", "") or "extreme macro detail, sharp texture, highly detailed"
    music_prompt = settings.pop("_oz_music", settings.get("music_prompt", ""))

    ts = _time.strftime("%Y%m%d_%H%M%S")
    job_dir = OUTPUT_DIR / _time.strftime("%Y-%m-%d") / f"zoomin_{Path(source_path).stem[:12]}_{ts}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # -- Forge build ----------------------------------------------------------
    gpu.acquire("forge", reason="outpaint zoom-in")
    if not forge_client.forge_alive():
        job.update(status="error", message="Forge (Stable Diffusion) is not running -- it generates the zoom detail. Start it in Services.")
        return

    job.update(progress=12, message="Diving in + painting detail...")

    def _bp(k, n):
        job.update(progress=12 + int(48 * k / max(n, 1)),
                   message=f"Painting detail level {k}/{n}...")

    stack, _dims = build_zoom_in_stack(
        source_path, n_levels, prompt,
        zoom_factor=zf, denoise=denoise,
        stop_check=_stopped, progress_fn=_bp, log_fn=lambda m: log.info(m),
    )
    if not stack:
        job.update(status="error", message="Zoom build failed (Forge returned nothing)")
        return
    # source is fully read into the stack now -- drop any video-frame temp dir
    _td = settings.get("_tmp_dir")
    if _td:
        import shutil as _sh
        _sh.rmtree(_td, ignore_errors=True)
    if _stopped():
        return

    # -- Render ---------------------------------------------------------------
    job.update(progress=62, message="Rendering the zoom...")
    silent = str(job_dir / f"zoomin_{ts}.mp4")
    out = render_zoom_in(stack, silent, zoom_factor=zf, fps=fps,
                         sec_per_level=spl, xfade=float(settings.get("xfade", 0.6)),
                         log_fn=lambda m: log.info(m))
    if not out:
        job.update(status="error", message="Zoom render failed")
        return
    final = out
    total_dur = probe_duration(out) or (n_levels * spl)

    # -- Audio (optional) -----------------------------------------------------
    music_ok = False
    if not skip_audio and not _stopped():
        try:
            # acquire("acestep") evicts Forge (unloads its checkpoint) and starts
            # ACE-Step. ACE-Step fits alongside the unloaded Forge process and
            # adapts its LLM memory to what's free, so a full Forge stop isn't
            # needed -- the earlier "music failed" was a NameError in start_acestep
            # (ACESTEP_HOST), now fixed, not a VRAM problem.
            gpu.acquire("acestep", reason="zoom-in music")
            if not music_prompt:
                music_prompt = "cinematic ambient, gentle build, a sense of descending into infinite detail"
            lyrics = ""
            if not instrumental:
                try:
                    from features.fun_videos import analyzer
                    lyrics = analyzer.generate_lyrics(get_llm_router(), [], music_prompt, "")
                except Exception:
                    lyrics = ""
            job.update(progress=80, message="Generating music...")
            ap, aerr = audio_generator.generate_audio(
                prompt=music_prompt, duration=min(total_dur + 2.0, 300.0),
                output_dir=str(job_dir), audio_format=settings.get("audio_format", "mp3"),
                steps=int(settings.get("audio_steps", 8)),
                guidance=float(settings.get("audio_guidance", 7.0)),
                seed=-1, lyrics=lyrics, instrumental=instrumental,
                stop_event=job.stop_event,
            )
            if ap and _os.path.isfile(ap):
                merged = str(job_dir / f"zoomin_{ts}_final.mp4")
                m = video_generator.merge_video_audio(out, ap, merged)
                if m:
                    final = m
                    music_ok = True
            else:
                log.warning("[oz] audio failed: %s -- video only", aerr)
        except Exception as e:
            log.warning("[oz] audio stage failed: %s -- video only", e)

    # -- Output + gallery + session ------------------------------------------
    # (Provenance is stamped centrally in gallery_push from the metadata below.)
    job.output = final
    try:
        copy_to_inbox(final)
    except Exception:
        pass
    job.message = "Zoom-in complete!" if (skip_audio or music_ok) else \
        "Zoom-in done (music generation failed -- video saved; see logs)"
    job.meta.update({"final_path": final, "direction": "in", "detail_prompt": prompt, "music_ok": music_ok})
    try:
        from core.session import get_current as _gs
        _gs().add_file(Path(final).name, "video", "zoom", path=final)
    except Exception:
        pass
    try:
        norm = final.replace("\\", "/")
        idx = norm.lower().find("/output/")
        url = norm[idx:] if idx != -1 else f"/output/{Path(final).name}"
        gallery_push(url, tab="zoom", prompt=prompt[:120], model="Outpaint Zoom-in (SD)",
                     metadata={"path": final, "job_id": job.id, "direction": "in",
                               "detail_prompt": prompt, "source": Path(source_path).name,
                               "n_levels": n_levels, "zoom_factor": zf, "sec_per_level": spl,
                               "music": bool(music_ok), "duration_sec": round(total_dur, 2)})
    except Exception as e:
        log.warning("[oz] gallery_push failed: %s", e)

