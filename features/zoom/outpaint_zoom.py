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
