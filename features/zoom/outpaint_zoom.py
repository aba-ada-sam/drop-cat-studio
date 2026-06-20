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
    feather_frac: float = 0.38,
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
                if iw < W and ih < H:
                    # WIDE feather: the crisp inner must DISSOLVE into the ring,
                    # not sit as a sharp rectangle. inset is a large fraction of
                    # the inner size so the alpha ramps over a big band -- since
                    # the ring's centre is (approximately) this same content, the
                    # blend is invisible and there is no "postage stamp" edge.
                    inset = max(2, int(min(iw, ih) * feather_frac))
                    inset = min(inset, iw // 2 - 1, ih // 2 - 1)
                    mask = Image.new("L", (iw, ih), 0)
                    if inset > 0:
                        mask.paste(255, (inset, inset, iw - inset, ih - inset))
                        mask = mask.filter(ImageFilter.GaussianBlur(radius=max(2, inset * 0.6)))
                    else:
                        mask = Image.new("L", (iw, ih), 255)
                    base.paste(crisp, ((W - iw) // 2, (H - ih) // 2), mask)
                else:
                    # inner fills the frame (segment boundary): feather only the
                    # outermost edge so it blends with the wider level's content.
                    edge = max(2, int(min(W, H) * feather_frac * 0.4))
                    mask = Image.new("L", (W, H), 0)
                    mask.paste(255, (edge, edge, W - edge, H - edge))
                    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(2, edge * 0.6)))
                    base.paste(crisp, (0, 0), mask)

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
# Pyramid renderer -- composite ALL visible levels per frame (truly continuous)
# ---------------------------------------------------------------------------

def render_pyramid(
    stack,
    out_path: str,
    *,
    direction: str = "out",
    zoom_factor: float = 0.6,
    fps: int = 30,
    sec_per_level: float = 2.5,
    feather_frac: float = 0.32,
    log_fn=None,
) -> str | None:
    """Render a continuous infinite zoom by compositing the WHOLE pyramid every
    frame instead of one level at a time.

    At a continuous zoom position z in [0, N], each level k is drawn at its exact
    geometric scale f**(z-k) and centred; the outermost visible level fills the
    frame, every inner level is composited on top (feathered). Because each level
    sits at its own precise scale, there is no per-segment reset and no soft
    "ring" that pulses -- the only mildly-upscaled part is a thin outer sliver, so
    the frame is sharp and the motion is genuinely continuous (no cuts).

    stack[0] = innermost (source); stack[-1] = widest. Renders z:0->N (pull out);
    direction="in" reverses to dive in.
    """
    from PIL import Image, ImageFilter

    def _log(m):
        log.info(m)
        if log_fn:
            log_fn(m)

    if not stack or len(stack) < 2:
        return None
    f = max(0.30, min(0.85, float(zoom_factor)))
    W, H = stack[0].size
    stack = [im if im.size == (W, H) else im.resize((W, H), Image.LANCZOS) for im in stack]
    N = len(stack) - 1
    total = max(2, int(round(N * fps * sec_per_level)))

    def _fill(img, sc):
        # sc >= 1: crop the centre (W/sc x H/sc) and upscale to fill the frame.
        cw = min(W, max(2, round(W / sc)))
        ch = min(H, max(2, round(H / sc)))
        x0, y0 = (W - cw) // 2, (H - ch) // 2
        return img.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.LANCZOS)

    def _inner(img, sc):
        # sc < 1: shrink to (W*sc x H*sc) with a feathered alpha for compositing.
        iw, ih = max(2, round(W * sc)), max(2, round(H * sc))
        r = img.resize((iw, ih), Image.LANCZOS)
        inset = min(max(1, int(min(iw, ih) * feather_frac)), iw // 2 - 1, ih // 2 - 1)
        if inset > 0:
            m = Image.new("L", (iw, ih), 0)
            m.paste(255, (inset, inset, iw - inset, ih - inset))
            m = m.filter(ImageFilter.GaussianBlur(radius=max(1, inset * 0.6)))
        else:
            m = Image.new("L", (iw, ih), 255)
        return r, m

    tmp = Path(tempfile.mkdtemp(prefix="dcs_pyr_"))
    paths: list[str] = []
    idx = 0
    try:
        for fi in range(total + 1):
            z = N * fi / total
            ti = int(z)
            topk = min(N, ti if z == ti else ti + 1)
            base = _fill(stack[topk], max(1.0, f ** (z - topk)))
            for k in range(topk - 1, -1, -1):
                sc = f ** (z - k)
                if sc >= 1.0 or min(W * sc, H * sc) < 4:
                    if sc >= 1.0:
                        base = _fill(stack[k], sc)
                    continue
                r, m = _inner(stack[k], sc)
                base.paste(r, ((W - r.width) // 2, (H - r.height) // 2), m)
            p = str(tmp / f"f{idx:06d}.png")
            base.save(p)
            paths.append(p)
            idx += 1

        if direction == "in":
            paths = list(reversed(paths))
        seq = Path(tempfile.mkdtemp(prefix="dcs_pyr_seq_"))
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
            _log(f"[pyramid] ffmpeg failed: {r.stderr.decode(errors='replace')[-200:]}")
            return None
        _log(f"[pyramid] rendered {idx} frames -> {out_path}")
        return out_path
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Crossfade renderer -- seamless level blending, no hard centre square
# ---------------------------------------------------------------------------

def render_zoom_crossfade(
    stack,
    out_path: str,
    *,
    direction: str = "out",
    zoom_factor: float = 0.6,
    fps: int = 30,
    sec_per_level: float = 2.5,
    feather_frac: float = 0.45,
    log_fn=None,
) -> str | None:
    """Continuous zoom that CROSSFADES consecutive levels so junctions are
    seamless and the inner level dissolves into the outer one (no hard 'postage
    stamp' square). Tolerates non-pixel-exact nesting.

    stack[k]'s centre f-region must approximately equal stack[k-1] (stack[k] is
    the wider view). Renders source -> widest (pull out); reverse for "in".
      zoom-out:  stack = build_zoom_stack() result,                direction="out"
      zoom-in:   stack = list(reversed(build_zoom_in_stack())),    direction="in"
    """
    from PIL import Image, ImageFilter

    def _log(m):
        log.info(m)
        if log_fn:
            log_fn(m)

    if not stack or len(stack) < 2:
        return None
    f = max(0.30, min(0.85, float(zoom_factor)))
    W, H = stack[0].size
    stack = [im if im.size == (W, H) else im.resize((W, H), Image.LANCZOS) for im in stack]
    nfr = max(2, int(round(fps * sec_per_level)))

    tmp = Path(tempfile.mkdtemp(prefix="dcs_opzx_"))
    paths: list[str] = []
    idx = 0

    def _save(img):
        nonlocal idx
        p = str(tmp / f"f{idx:06d}.png")
        img.save(p)
        paths.append(p)
        idx += 1

    try:
        hold = int(fps * 0.4)
        for _ in range(hold):
            _save(stack[0])
        # segments: A=stack[k-1] (inner) dissolves into B=stack[k] (wider) as we pull out
        for k in range(1, len(stack)):
            A, B = stack[k - 1], stack[k]
            for i in range(nfr):
                p = i / nfr
                s = f ** (1.0 - p)                 # B crop scale: f -> 1
                cw, ch = max(2, round(W * s)), max(2, round(H * s))
                x0, y0 = (W - cw) // 2, (H - ch) // 2
                base = B.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.LANCZOS)
                # A occupies the centre (f/s) of base; crossfade it in (crisp) over B
                iw = min(W, max(2, round(W * f / s)))
                ih = min(H, max(2, round(H * f / s)))
                Ar = A.resize((iw, ih), Image.LANCZOS)
                aA = 1.0 - p                       # inner fades out as we reach the wider view
                band = max(1, int(min(iw, ih) * feather_frac * 0.5))
                m = Image.new("L", (iw, ih), 0)
                if iw > 2 * band and ih > 2 * band:
                    m.paste(int(255 * aA), (band, band, iw - band, ih - band))
                    m = m.filter(ImageFilter.GaussianBlur(radius=band * 0.7))
                else:
                    m = Image.new("L", (iw, ih), int(255 * aA))
                base.paste(Ar, ((W - iw) // 2, (H - ih) // 2), m)
                _save(base)
        for _ in range(hold):
            _save(stack[-1])

        if direction == "in":
            paths = list(reversed(paths))

        seq = Path(tempfile.mkdtemp(prefix="dcs_opzx_seq_"))
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
            _log(f"[outpaint] crossfade ffmpeg failed: {r.stderr.decode(errors='replace')[-200:]}")
            return None
        _log(f"[outpaint] crossfade rendered {idx} frames -> {out_path}")
        return out_path
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


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

    # Mask: white = generate. Keep only a core SMALLER than the pasted source
    # (kc) so SD repaints and BLENDS the source's outer edge into the new ring --
    # this is what stops the source reading as a hard pasted square. Heavy blur
    # turns the keep->generate boundary into a wide gradient.
    kc = 0.78
    kw, kh = max(4, round(iw * kc)), max(4, round(ih * kc))
    mask = Image.new("L", (W, H), 255)
    mask.paste(Image.new("L", (kw, kh), 0), ((W - kw) // 2, (H - kh) // 2))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(8, iw // 12)))

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
    # No hard re-paste: SD preserved the masked core and blended the source edge
    # into the ring, so the source is not a pasted square. The crossfade renderer
    # tolerates the (now non-pixel-exact) nesting.
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
    W, H = _gen_size(*src.size)
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
        # Match exposure/colour to the previous level so the boundary doesn't cut.
        nxt = _match_color(nxt, stack[-1])
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
