"""First-pass "yarn burst from the mouth" effect for the f2 mushroom-creature clip.

Andrew, 2026-08-02 (new direction, on top of the lip-sync + outro work already
in flight): at the end of the clip, have a burst of yarn come out of the
mushroom character's mouth. Explicitly OK as a first-pass -- taste call, give
him something concrete to react to rather than guessing silently.

The "mushroom character" is the f2 subject: a round fuzzy blue crocheted cap
on a thin mottled stalk, planted on a wooden log -- unmistakably a mushroom
silhouette (confirmed independently by C:\\Users\\andre\\Desktop\\DCG\\
STOP_MUSHROOM_LOOP.txt and features/song_video/pipeline.py's own "crocheted
mushroom" example comment). Its mouth is the dark oval in the lower half of
the cap.

Approach (pure ffmpeg + PIL, no GPU, matches the outro sting's own
no-GPU-for-post philosophy):
  1. Paint a single RGBA "burst" sprite -- yarn-colored curved strands radiating
     from a center point, flat colors + solid ink outlines (graphic-novel /
     painted style per the app's usual look, deliberately NOT photoreal),
     with a few small pompom-fleck tips for texture continuity with the
     creature's own crocheted look.
  2. Overlay it centered on the mouth, timed to pop in right as the mouth is
     wide open near the end of the performance, hold briefly, then fade back
     out. Static position, alpha-only animation (fade in fast = "pop", fade
     out slow) -- deliberately simple/robust for a first pass; a later pass
     could add outward-flying motion per strand if Andrew likes the concept.

add_yarn_burst(input_video_path, out_path, mouth_xy=None, burst_time=None)
  is the entry point. mouth_xy auto-defaults to the f2 clip's known mouth
  location; burst_time auto-defaults to (measured audio duration - 0.5s).
  Both are overridable for reuse on a different clip.
"""
import logging
import math
import random
import subprocess
import tempfile
from pathlib import Path

from core.ffmpeg_utils import find_ffmpeg, video_encode_args, probe_file

log = logging.getLogger(__name__)

FFMPEG = find_ffmpeg() or "ffmpeg"
_TMP_PREFIX = "dcs-yarnburst-"

# Yarn palette lifted directly from the actual f2 creature's cap, weighted
# the way the real material reads: mostly dusty blue, cream/gray as minority
# fleck texture, not equal-sized color blocks (v1 gave gold equal billing
# and the render looked like loose marbles/confetti, not one fuzzy mass).
_BLUE_MAIN   = (91, 111, 160, 255)
_BLUE_SHADOW = (66, 80, 122, 255)
_CREAM       = (232, 224, 205, 255)
_GRAY        = (176, 176, 178, 255)
_PALETTE_WEIGHTED = (
    [_BLUE_MAIN] * 5 + [_BLUE_SHADOW] * 3 + [_CREAM] * 3 + [_GRAY] * 2
)

# f2-specific defaults, measured directly off C:\Users\andre\Desktop\f2.mp4 /
# output/2026-08-02/f2_lipsync_184054.mp4 (720x720): the mouth sits at roughly
# (360, 365) at the t=~28.0s peak open-mouth frame, which is also ~0.5s before
# the song's audio content actually ends (~28.5s) -- a natural "last big note"
# moment to burst on.
F2_MOUTH_XY = (360, 365)
F2_DEFAULT_BURST_LEAD = 0.5  # seconds before measured audio end


def _fuzzy_mass(draw, cx: float, cy: float, radius: float, rng: random.Random,
                 density: float = 1.0) -> None:
    """One dense fuzzy mass of small, mostly-same-hue overlapping dots plus
    short curl marks -- reads as a clump of matted/looped yarn fiber, not a
    cluster of separate balls. v2 lesson (confirmed by rendering + looking at
    the actual frame): equal-sized dots in 5 loud colors read as marbles/
    confetti. What actually matches the cap two inches away in the same shot
    is MANY SMALL dots so densely packed they merge into one mass, weighted
    hard toward a single dominant hue with only minority fleck color, plus
    short curved strokes (not more circles) to break up the "pile of balls"
    silhouette with real loop texture."""
    n_dots = int(60 * density)
    for _ in range(n_dots):
        rr = radius * (rng.random() ** 0.4)
        ang = rng.uniform(0, 2 * math.pi)
        x = cx + math.cos(ang) * rr
        y = cy + math.sin(ang) * rr
        r = radius * rng.uniform(0.10, 0.22)
        color = rng.choice(_PALETTE_WEIGHTED)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)

    # Short curl/loop strokes on top -- 2-3px arcs, same weighted palette,
    # scattered across the mass. This is what actually reads as "yarn loop"
    # texture instead of "row of dots": a fuzzy silhouette with visible
    # curved fiber marks, not a smooth-edged blob.
    n_curls = int(28 * density)
    for _ in range(n_curls):
        rr = radius * rng.uniform(0.15, 1.05)
        ang = rng.uniform(0, 2 * math.pi)
        x = cx + math.cos(ang) * rr
        y = cy + math.sin(ang) * rr
        cr = radius * rng.uniform(0.09, 0.16)
        start = rng.uniform(0, 360)
        color = rng.choice(_PALETTE_WEIGHTED)
        bbox = [x - cr, y - cr, x + cr, y + cr]
        draw.arc(bbox, start=start, end=start + rng.uniform(160, 260),
                 fill=color, width=max(2, int(radius * 0.10)))

    # A sparse fringe of small dots past the main radius so the silhouette
    # edge is ragged/hairy instead of a clean circle boundary.
    n_fringe = int(16 * density)
    for _ in range(n_fringe):
        rr = radius * rng.uniform(0.95, 1.35)
        ang = rng.uniform(0, 2 * math.pi)
        x = cx + math.cos(ang) * rr
        y = cy + math.sin(ang) * rr
        r = radius * rng.uniform(0.05, 0.12)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.choice(_PALETTE_WEIGHTED))


def _make_burst_sprite(size: int = 640, seed: int = 7, n_tufts: int = 6) -> "object":
    """Paint one RGBA yarn-burst sprite: one big dense fuzzy core (yarn still
    bunched at the point of origin) plus a handful of smaller fuzzy clumps
    flung outward on short motion trails -- matching how a torn crocheted
    toy would actually shed (uneven matted clumps), not a symmetric vector
    burst. v1 (thin outlined tube strands + flat circle tips) read as
    noodles with meatballs; v2 (loud multi-color same-size dot clusters)
    read as marbles/confetti. This version restricts the palette to the
    cap's own dominant-blue-plus-minority-fleck weighting and builds each
    clump from many small dots + curl strokes + a ragged fringe so it merges
    into one fuzzy mass instead of a set of visible discrete shapes. Verify
    by rendering + actually looking at a frame, not by reading this
    docstring -- that's exactly the mistake that shipped v1."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    rng = random.Random(seed)

    _fuzzy_mass(draw, cx, cy, size * 0.20, rng, density=1.6)

    for i in range(n_tufts):
        ang = (2 * math.pi * i / n_tufts) + rng.uniform(-0.30, 0.30)
        dist = rng.uniform(0.28, 0.72) * size * 0.42
        tx, ty = cx + math.cos(ang) * dist, cy + math.sin(ang) * dist

        # Short motion trail of a handful of small same-palette dots between
        # the core and the clump -- kept short so it can't read as a strand
        # on its own, just a hint of "flung from" direction.
        trail_steps = rng.randint(2, 4)
        for s in range(1, trail_steps):
            t = s / trail_steps
            px = cx + (tx - cx) * t + rng.uniform(-5, 5)
            py = cy + (ty - cy) * t + rng.uniform(-5, 5)
            pr = size * rng.uniform(0.014, 0.03) * (1.0 - 0.3 * t)
            draw.ellipse([px - pr, py - pr, px + pr, py + pr],
                         fill=rng.choice(_PALETTE_WEIGHTED))

        tuft_r = size * rng.uniform(0.055, 0.105)
        _fuzzy_mass(draw, tx, ty, tuft_r, rng, density=rng.uniform(0.7, 1.1))

    # Slight blur softens the individual dots' crisp cut-out edges into a
    # matted/fuzzy silhouette -- without it the dots still read as a pile of
    # distinct circles (v3 pre-blur); this alone was the difference between
    # "bubbles" and "yarn" when actually compared side by side.
    from PIL import ImageFilter
    img = img.filter(ImageFilter.GaussianBlur(size / 400))

    return img


def _audio_stream_duration(path: str) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        val = r.stdout.strip()
        if val and val != "N/A":
            return float(val)
    except Exception:
        pass
    return None


def add_yarn_burst(
    input_video_path: str,
    out_path: str | None = None,
    mouth_xy: tuple[float, float] | None = None,
    burst_time: float | None = None,
    pop_in: float = 0.14,
    hold: float = 0.55,
    fade_out: float = 0.55,
    sprite_size: int = 640,
    seed: int = 7,
) -> tuple[str | None, str | None]:
    """Composite a yarn-burst sprite onto input_video_path, centered on
    mouth_xy, timed to pop in at burst_time. Returns (out_path, error).

    Defaults are tuned for the f2 mushroom clip (720x720); pass mouth_xy /
    burst_time explicitly to reuse on a different source.
    """
    input_video_path = str(input_video_path)
    if not Path(input_video_path).exists():
        return None, f"Input video not found: {input_video_path}"

    info = probe_file(input_video_path)
    w, h = info["width"], info["height"]
    if not w or not h:
        return None, "could not probe source video dimensions"

    if mouth_xy is None:
        mouth_xy = F2_MOUTH_XY
    if burst_time is None:
        audio_dur = _audio_stream_duration(input_video_path)
        if audio_dur is None:
            audio_dur = info["duration"] or 0.0
        burst_time = max(0.0, audio_dur - F2_DEFAULT_BURST_LEAD)

    out_path = Path(out_path) if out_path else Path(input_video_path).with_name(
        Path(input_video_path).stem + "_yarnburst.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    mx, my = mouth_xy
    ox = int(round(mx - sprite_size / 2))
    oy = int(round(my - sprite_size / 2))

    with tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as tmp:
        tmp = Path(tmp)
        sprite_path = tmp / "burst.png"
        _make_burst_sprite(size=sprite_size, seed=seed).save(sprite_path)

        fade_in_start = burst_time
        fade_out_start = burst_time + pop_in + hold
        # fade filter with alpha=1 fades the PNG's own alpha channel, not the
        # frame under it -- verified against this ffmpeg build's fade docs.
        sprite_chain = (
            f"format=rgba,"
            f"fade=t=in:st={fade_in_start:.3f}:d={pop_in:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}:alpha=1"
        )

        container_dur = info["duration"] or 0.0
        has_audio = info.get("has_audio", False)

        filter_complex = (
            f"[1:v]{sprite_chain}[burst];"
            f"[0:v][burst]overlay=x={ox}:y={oy}:format=auto[outv]"
        )
        maps = ["-map", "[outv]"]
        audio_args = ["-an"]
        if has_audio:
            # apad + a hard -t container_dur cap: the source's own audio
            # stream can already be shorter than its video stream (measured
            # on f2_lipsync_184054.mp4: video 29.27s, audio 28.50s -- a
            # pre-existing gap from upstream, not introduced here), and a
            # plain `-c:a copy` just carries that shortfall straight through.
            # Re-encoding through apad guarantees this step's own output
            # never makes the gap worse and, when fed a source that DOES
            # already match, is a no-op beyond a lossless-feeling re-encode.
            filter_complex += ";[0:a]apad[outa]"
            maps += ["-map", "[outa]"]
            audio_args = ["-c:a", "aac", "-b:a", "192k"]

        cmd = [
            FFMPEG, "-y",
            "-i", input_video_path,
            "-loop", "1", "-t", f"{container_dur:.3f}", "-i", str(sprite_path),
            "-filter_complex", filter_complex,
        ] + maps + video_encode_args(crf=16) + audio_args + [
            "-t", f"{container_dur:.3f}", str(out_path),
        ]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            log.warning("[yarn-burst] render failed: %s", r.stderr[-800:])
            return None, f"ffmpeg failed: {r.stderr[-400:]}"

    if not out_path.exists() or out_path.stat().st_size == 0:
        return None, "yarn burst produced empty output"

    log.info("[yarn-burst] composited burst at t=%.2fs (mouth=%s) -> %s",
              burst_time, mouth_xy, out_path)
    return str(out_path), None
