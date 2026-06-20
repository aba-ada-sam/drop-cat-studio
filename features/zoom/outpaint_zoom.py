"""Outpaint-based infinite zoom -- rot-free replacement for chained AI clips.

WHY THIS EXISTS
---------------
The old zoom chained AI video clips: each clip was regenerated from the
previous clip's last frame, so texture decay and skin "pox" compounded and the
output turned to garbage by the end. This approach never regenerates existing
content. Instead it builds a stack of NESTED images:

    stack[0] = the source image (stays pristine, at the very center, forever)
    stack[k] = stack[k-1] scaled down into the centre + a NEW outpainted ring

Only the outer ring is ever generated; the centre is copied, never re-diffused,
so it cannot rot. The video is then a deterministic, continuous crop-zoom
across the stack -- no model runs per frame, so there is nothing to compound.

TWO STAGES
----------
    build_zoom_stack()      -- Forge SD outpaint: source -> N nested images (GPU)
    render_infinite_zoom()  -- deterministic crop-zoom video from the stack (CPU)

The render stage is fully verifiable without a GPU -- run this file directly
(`python -m features.zoom.outpaint_zoom`) to render a synthetic stack and dump
sample frames you can eyeball.
"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("zoom.outpaint")


# ---------------------------------------------------------------------------
# Render stage (deterministic, CPU-only, no model)
# ---------------------------------------------------------------------------

def render_infinite_zoom(
    stack,
    out_path: str,
    *,
    direction: str = "in",
    zoom_factor: float = 0.5,
    fps: int = 30,
    sec_per_level: float = 2.5,
    feather_px: int = 24,
    log_fn=None,
) -> str | None:
    """Render a smooth infinite-zoom video from a nested image stack.

    stack: list of PIL.Image, all the SAME size (W x H), where stack[k]'s centred
           (W*zoom_factor x H*zoom_factor) region equals stack[k-1]. stack[0] is
           the innermost (the source); stack[-1] is the widest outpainted view.
    direction: "in"  -> start wide (stack[-1]), fly inward, end on the pristine
                        source (stack[0]).
               "out" -> start on the source, pull back to the widest view.
    zoom_factor: how much each level scales the previous (must match build).
    sec_per_level: seconds the camera spends crossing one level.

    Crispness trick: each rendered frame upscales the OUTER ring of stack[k] but
    composites the higher-resolution stack[k-1] back into the centre, so there
    is no resolution "pop" at level boundaries -- at the moment the centre fills
    the frame it IS the crisp previous image, matching the next segment exactly.

    Returns out_path on success, None on failure.
    """
    from PIL import Image, ImageFilter

    def _log(m):
        log.info(m)
        if log_fn:
            log_fn(m)

    if not stack or len(stack) < 2:
        _log("[outpaint] need at least 2 levels to render a zoom")
        return None

    f = max(0.30, min(0.90, float(zoom_factor)))
    W, H = stack[0].size
    stack = [im if im.size == (W, H) else im.resize((W, H), Image.LANCZOS) for im in stack]
    frames_per_seg = max(2, int(round(fps * sec_per_level)))
    n_levels = len(stack) - 1  # number of transitions

    tmp_dir = Path(tempfile.mkdtemp(prefix="dcs_opz_"))
    frame_paths: list[str] = []
    idx = 0

    try:
        # Always render the zoom-OUT sequence (source -> widest); reverse for "in".
        # Transition k (k = 1..n_levels) renders on stack[k], crisp stack[k-1]
        # composited in the centre. s = scale of the centred crop window taken
        # from stack[k]: s = f at the start (centre fills frame) -> 1 at the end
        # (full stack[k]). Exponential in p so the zoom speed is constant.
        for k in range(1, len(stack)):
            outer = stack[k]
            inner = stack[k - 1]
            for i in range(frames_per_seg):
                p = i / frames_per_seg
                s = f ** (1.0 - p)               # f .. 1, geometric
                cw, ch = max(2, round(W * s)), max(2, round(H * s))
                x0, y0 = (W - cw) // 2, (H - ch) // 2
                base = outer.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.LANCZOS)

                # The centre f-fraction of stack[k] is stack[k-1]. Within this
                # scale-s crop it occupies a centred (W*f/s x H*f/s) region.
                iw = min(W, max(2, round(W * f / s)))
                ih = min(H, max(2, round(H * f / s)))
                crisp = inner.resize((iw, ih), Image.LANCZOS)
                if feather_px > 0 and iw < W and ih < H:
                    mask = Image.new("L", (iw, ih), 255)
                    # soft border so the upscaled ring blends into the crisp centre
                    m = ImageFilter.GaussianBlur(radius=feather_px)
                    border = Image.new("L", (iw, ih), 0)
                    inset = min(feather_px * 2, iw // 2 - 1, ih // 2 - 1)
                    if inset > 0:
                        border.paste(255, (inset, inset, iw - inset, ih - inset))
                        mask = border.filter(m)
                    base.paste(crisp, ((W - iw) // 2, (H - ih) // 2), mask)
                else:
                    base.paste(crisp, ((W - iw) // 2, (H - ih) // 2))

                fp = str(tmp_dir / f"f{idx:06d}.png")
                base.save(fp)
                frame_paths.append(fp)
                idx += 1

        # final hold on the widest frame so the end doesn't snap
        for _ in range(int(fps * 0.4)):
            fp = str(tmp_dir / f"f{idx:06d}.png")
            stack[-1].save(fp)
            frame_paths.append(fp)
            idx += 1

        if direction == "in":
            frame_paths = list(reversed(frame_paths))

        # Re-link frames into sequential names for ffmpeg (reversed order needs it)
        seq_dir = Path(tempfile.mkdtemp(prefix="dcs_opz_seq_"))
        for j, src in enumerate(frame_paths):
            os.replace(src, str(seq_dir / f"s{j:06d}.png"))

        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        r = subprocess.run(
            ["ffmpeg", "-y", "-framerate", str(fps),
             "-i", str(seq_dir / "s%06d.png"),
             "-c:v", "libx264", "-crf", "16", "-preset", "medium",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
            capture_output=True, timeout=300,
        )
        import shutil
        shutil.rmtree(seq_dir, ignore_errors=True)
        if r.returncode != 0 or not os.path.isfile(out_path):
            _log(f"[outpaint] ffmpeg assemble failed: {r.stderr.decode(errors='replace')[-300:]}")
            return None
        _log(f"[outpaint] rendered {idx} frames -> {out_path}")
        return out_path
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Build stage (Forge SD outpaint) -- generates the real nested stack
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


def _outpaint_ring(prev_img, prompt, *, negative_prompt, W, H, f,
                   steps, cfg_scale, denoise, sampler, scheduler, seed,
                   mask_blur, inpainting_fill, feather_px, log_fn=None):
    """One outpaint level: shrink prev into the centre, generate the ring around
    it with Forge, then hard-composite the pristine centre back so the nesting
    is EXACT (the render engine depends on centre f-region == prev exactly).

    Returns a new WxH PIL.Image, or None on Forge failure.
    """
    from PIL import Image, ImageFilter
    from services import forge_client

    iw, ih = max(8, round(W * f)), max(8, round(H * f))
    ox, oy = (W - iw) // 2, (H - ih) // 2
    scaled = prev_img.resize((iw, ih), Image.LANCZOS)

    # Backdrop: blurred full-frame copy of prev so the ring starts from matching
    # colours instead of black (SD extends it into real content).
    bg = prev_img.resize((W, H), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(radius=max(8, W // 22)))
    canvas = bg.copy()
    canvas.paste(scaled, (ox, oy))

    # Mask: white = generate (outer ring), black = keep (centre).
    mask = Image.new("L", (W, H), 255)
    mask.paste(Image.new("L", (iw, ih), 0), (ox, oy))

    res = forge_client.img2img(
        init_image_b64=_b64_png(canvas),
        prompt=prompt,
        negative_prompt=negative_prompt,
        denoising_strength=denoise,
        width=W, height=H, steps=steps,
        sampler_name=sampler, scheduler=scheduler,
        cfg_scale=cfg_scale, seed=seed, resize_mode=0,
        mask=_b64_png(mask),
        mask_blur=mask_blur,
        inpainting_fill=inpainting_fill,   # 0 fill,1 original,2 latent noise,3 latent nothing
        inpaint_full_res=False,            # whole-image context -> coherent ring
        inpainting_mask_invert=0,
    )
    if res.get("error") or not res.get("images"):
        if log_fn:
            log_fn(f"[outpaint] Forge failed: {res.get('error')}")
        return None
    out = _from_b64(res["images"][0]).resize((W, H), Image.LANCZOS)

    # Hard re-paste the pristine centre (feathered) so nesting is pixel-exact.
    fmask = Image.new("L", (iw, ih), 255)
    inset = min(feather_px, iw // 2 - 1, ih // 2 - 1)
    if inset > 0:
        fmask = Image.new("L", (iw, ih), 0)
        fmask.paste(255, (inset, inset, iw - inset, ih - inset))
        fmask = fmask.filter(ImageFilter.GaussianBlur(radius=inset / 2))
    out.paste(scaled, (ox, oy), fmask)
    return out


def build_zoom_stack(
    source_path: str,
    n_levels: int,
    prompt: str,
    *,
    negative_prompt: str = "blurry, low quality, watermark, text, frame, border, seam, duplicate",
    zoom_factor: float = 0.5,
    steps: int = 24,
    cfg_scale: float = 6.0,
    denoise: float = 1.0,
    sampler: str = "DPM++ 2M",
    scheduler: str = "Karras",
    seed: int = -1,
    mask_blur: int = 24,
    inpainting_fill: int = 2,
    feather_px: int = 12,
    max_dim: int = 1024,
    stop_check=None,
    progress_fn=None,
    log_fn=None,
):
    """Build a nested image stack by outpainting rings outward from the source.

    Returns (stack, (W,H)) where stack[0] is the source and each stack[k] adds a
    generated ring around stack[k-1]. Returns (None, None) if Forge is down.
    """
    from PIL import Image
    from services import forge_client

    def _log(m):
        log.info(m)
        if log_fn:
            log_fn(m)

    if not forge_client.forge_alive():
        _log("[outpaint] Forge is not running -- cannot build stack")
        return None, None

    src = Image.open(source_path).convert("RGB")
    W, H = src.size
    # clamp to SD-friendly size, multiples of 8, longest edge <= max_dim
    scale = min(1.0, max_dim / max(W, H))
    W, H = int(round(W * scale)) // 8 * 8, int(round(H * scale)) // 8 * 8
    W, H = max(64, W), max(64, H)
    src = src.resize((W, H), Image.LANCZOS)

    stack = [src]
    for k in range(1, n_levels + 1):
        if stop_check and stop_check():
            return None, None
        if progress_fn:
            progress_fn(k, n_levels)
        _log(f"[outpaint] generating ring {k}/{n_levels}")
        nxt = _outpaint_ring(
            stack[-1], prompt, negative_prompt=negative_prompt, W=W, H=H,
            f=zoom_factor, steps=steps, cfg_scale=cfg_scale, denoise=denoise,
            sampler=sampler, scheduler=scheduler, seed=seed, mask_blur=mask_blur,
            inpainting_fill=inpainting_fill, feather_px=feather_px, log_fn=log_fn,
        )
        if nxt is None:
            _log(f"[outpaint] ring {k} failed -- stopping with {len(stack)} levels")
            break
        stack.append(nxt)

    if len(stack) < 2:
        return None, None
    return stack, (W, H)


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
    denoise: float = 0.55,
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
    W, H = src.size
    scale = min(1.0, max_dim / max(W, H))
    W, H = max(64, int(round(W * scale)) // 8 * 8), max(64, int(round(H * scale)) // 8 * 8)
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
        stack.append(_from_b64(res["images"][0]).resize((W, H), Image.LANCZOS))

    if len(stack) < 2:
        return None, None
    return stack, (W, H)


# ---------------------------------------------------------------------------
# Self-test: synthetic nested stack, no GPU -- proves the render engine
# ---------------------------------------------------------------------------

def _make_synthetic_stack(n_levels: int, size=(768, 432), zoom_factor: float = 0.5):
    """Build a nested stack with a recognisable centre and labelled rings so a
    human can verify the zoom direction, smoothness, and that the centre is
    preserved. Mimics exactly what build_zoom_stack() must produce."""
    from PIL import Image, ImageDraw
    W, H = size
    f = zoom_factor

    # innermost = distinctive target
    src = Image.new("RGB", (W, H), (20, 20, 28))
    d = ImageDraw.Draw(src)
    for gx in range(0, W, 48):
        d.line([(gx, 0), (gx, H)], fill=(60, 120, 200), width=2)
    for gy in range(0, H, 48):
        d.line([(0, gy), (W, gy)], fill=(60, 120, 200), width=2)
    d.ellipse([W//2-70, H//2-70, W//2+70, H//2+70], fill=(230, 80, 60))
    d.text((W//2-44, H//2-8), "SOURCE", fill=(255, 255, 255))

    stack = [src]
    ring_colors = [(40, 90, 50), (90, 70, 40), (50, 50, 100), (90, 40, 80),
                   (40, 90, 95), (95, 95, 40), (70, 40, 40), (40, 70, 95)]
    for k in range(1, n_levels + 1):
        canvas = Image.new("RGB", (W, H), ring_colors[(k - 1) % len(ring_colors)])
        dd = ImageDraw.Draw(canvas)
        # concentric marker so motion is obvious
        dd.rectangle([10, 10, W - 10, H - 10], outline=(255, 255, 255), width=3)
        dd.text((20, 20), f"RING {k}", fill=(255, 255, 255))
        dd.text((W - 90, H - 30), f"RING {k}", fill=(255, 255, 255))
        iw, ih = round(W * f), round(H * f)
        prev = stack[-1].resize((iw, ih), Image.LANCZOS)
        canvas.paste(prev, ((W - iw) // 2, (H - ih) // 2))
        stack.append(canvas)
    return stack


def _selftest():
    logging.basicConfig(level=logging.INFO)
    zf = 0.5
    stack = _make_synthetic_stack(4, zoom_factor=zf)
    out = str(Path(__file__).resolve().parent.parent.parent / "_opz_selftest.mp4")
    res = render_infinite_zoom(stack, out, direction="in", zoom_factor=zf,
                               fps=30, sec_per_level=1.5)
    print("RENDER RESULT:", res)
    return res


if __name__ == "__main__":
    _selftest()
