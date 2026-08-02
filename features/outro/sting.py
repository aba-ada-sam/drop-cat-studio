"""Branded outro / sign-off generator for Drop Cat Studio.

Andrew, 2026-08-02 (final direction -- this module has been through two
earlier designs, both superseded, see git log if curious):

  "don't even mention drop cat studio, just leave them guessing after the
  'Drop', the 'Cat' and the 'GO'. If they can't figure it out, all the
  better. ... those three bursts should show up at the end superimposed
  over the video, after the music has stopped. kind of a 'sign off'."

So: NO wordmark, NO brand name spelled out, NO icon/logo of any kind --
just the three words "Drop" / "Cat" / "GO" appearing in sequence, deliberately
unexplained. And it is NOT a separate appended clip with its own background
card -- it's composited directly on top of the source video's own real tail
footage, timed to start after that video's audio has gone silent (extending
the tail with a frozen last frame first, if the video doesn't already have
enough silent runway at the end).

Pure ffmpeg filters (drawtext, drawbox, gblur) -- no WanGP, no ACE-Step, no
GPU job queue. Safe to call from any thread at any time; never competes for
GPU with in-flight generation work.

append_outro(input_video_path, variant_key, output_path=None) is the single
entry point:
  1. Probes the source: dimensions/fps/container duration/audio extent.
  2. Works out how much silent runway already exists at the tail (video
     duration minus audio duration) and extends the video with a frozen
     last frame if that isn't enough to fit the word-reveal comfortably
     after the audio ends.
  3. Composites a variant's word-reveal treatment (see VARIANTS) directly
     onto that (possibly-extended) tail: a brief darken/vignette pass for
     legibility against real footage, then "Drop" -> "Cat" -> "GO" in
     sequence, timed to land entirely within the silent portion.
  4. Re-attaches the original audio track (naturally shorter than the new
     video length by design -- that gap is the point: it's silent under
     the sign-off).

render_samples(input_video_path, out_dir=None) runs all four variants
against one real source clip for side-by-side review.

--- ffmpeg gotchas discovered while building this (2026-08-02) ---
`overlay` in this ffmpeg build (8.1 gyan.dev essentials) does NOT alpha-blend
against a synthetic `-f lavfi -i "color=c=...@alpha"` transparent source --
it silently ignores the alpha plane and paints the RGB opaquely, blowing
away whatever is behind it. A REAL rgba PNG file (even a blank one written
by PIL) composites correctly, so the blur-in/scale-pop word styles (which
need an isolated transparent layer to blur/scale without dragging the whole
frame with them) source that layer from a tiny cached PNG on disk
(_blank_rgba_png), never from lavfi color+alpha.
By contrast, `drawbox` and `drawtext` draw DIRECTLY onto whatever frame
they're given and blend their own alpha correctly against real (non-alpha)
video content -- verified empirically -- so the legibility darken pass and
the fade_rise/snap_track word styles are chained straight onto the main
video stream with no separate transparent layer at all.
`scale`'s w/h expressions don't support the `enable` timeline option in this
build ("Timeline not supported with filter 'scale'"); `eq`, `drawbox`,
`gblur` and `rotate` all do.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

from core.ffmpeg_utils import find_ffmpeg, video_encode_args, probe_file, probe_duration

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = APP_DIR / "output" / "outro_samples"
FFMPEG = find_ffmpeg() or "ffmpeg"

FONTS_DIR = Path("C:/Windows/Fonts")
FONT_BAHNSCHRIFT = FONTS_DIR / "bahnschrift.ttf"     # modern geometric grotesque
FONT_SEGOE = FONTS_DIR / "segoeui.ttf"               # regular weight
FONT_SEGOE_LIGHT = FONTS_DIR / "segoeuil.ttf"        # light weight, elegant

# Palette anchored on the app's circus theme (static/css/design-system.css)
GOLD = "0xd4a017"
CRIMSON = "0xc41e3a"
CREAM = "0xf0e6d0"

WORDS = ["Drop", "Cat", "GO"]

DEFAULT_W, DEFAULT_H, DEFAULT_FPS = 1280, 720, 30

_TMP_PREFIX = "dcs-outro-"

# How long the re-attached audio fades to silence before it ends, instead of
# stopping dead -- see append_outro().
_AUDIO_FADE_S = 0.8


# -- tiny expression helpers (ffmpeg eval syntax; commas inside nested calls
#    are backslash-escaped -- validated empirically against this ffmpeg build)

def _if3(cond: str, a: str, b: str) -> str:
    return f"if({cond}\\,{a}\\,{b})"


def _min2(a: str, b: str) -> str:
    return f"min({a}\\,{b})"


def _ff_path(p: Path) -> str:
    """Format a filesystem path for an ffmpeg filter option value."""
    return str(p).replace("\\", "/").replace(":", "\\:")


def _esc_text(text: str) -> str:
    """Escape the handful of chars drawtext's `text=` treats specially."""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")


def _tracked(word: str) -> str:
    """'Drop' -> 'D R O P' -- single-space letter tracking, wide/modern caps look."""
    return " ".join(list(word.upper()))


def _blank_rgba_png(w: int, h: int) -> Path:
    """Write (or reuse) a fully-transparent RGBA PNG at exactly w x h.

    Needed as a real-file workaround for the lavfi color+alpha overlay bug
    documented in the module docstring -- drawtext/gblur/scale then operate
    on genuine per-pixel alpha that `overlay` composites correctly.
    """
    from PIL import Image

    tmp_dir = Path(tempfile.gettempdir()) / f"{_TMP_PREFIX}assets"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"blank_{w}x{h}.png"
    if not path.exists():
        Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(path)
    return path


# -- word timeline ----------------------------------------------------------

def _word_windows(base: float, word_dur: float, gap: float, n: int = 3):
    """Return [(t0, t1), ...] for n sequential words starting at absolute
    time `base`, plus the end time of the last word."""
    t = base
    windows = []
    for _ in range(n):
        windows.append((t, t + word_dur))
        t += word_dur + gap
    return windows, t - gap


# -- word reveal styles -------------------------------------------------
# fade_rise/snap_track chain directly onto the main video stream (no separate
# compositing needed -- drawtext blends onto whatever frame it's given, real
# footage included). blur_in/scale_pop need their own real-alpha transparent
# PNG-sourced canvas (see _blank_rgba_png), composited back with a single
# overlay -- `scale`/gblur inside that canvas must not drag real video
# content along with them.

def _word_fade_rise(word, font, fontsize, color, t0, t1, w, h,
                     fade_in=0.22, fade_out=0.16, rise_px=16, text=None) -> str:
    alpha = _if3(f"lt(t\\,{t0})", "0",
                 _if3(f"lt(t\\,{t0 + fade_in:.3f})", f"(t-{t0})/{fade_in}",
                      _if3(f"lt(t\\,{t1 - fade_out:.3f})", "1", f"({t1}-t)/{fade_out}")))
    prog = _min2(f"(t-{t0})/{fade_in}", "1")
    y = f"(h-text_h)/2+({rise_px})*(1-{prog})"
    display = text if text is not None else word
    return (
        f"drawtext=fontfile='{_ff_path(font)}':text='{_esc_text(display)}':"
        f"fontcolor={color}:fontsize={fontsize}:x=(w-text_w)/2:y='{y}':"
        f"alpha='{alpha}':enable='between(t\\,{t0}\\,{t1})'"
    )


def _word_snap_track(word, font, fontsize, color, t0, t1, w, h,
                      fade_in=0.16, snap_frac=0.55) -> list:
    """Wide-tracked text fades in, then SNAPS to tight caps -- a kinetic-type
    'letter-spacing tightening' beat done as a hard swap (readable + safe;
    a true continuous tracking animation isn't exposed by drawtext)."""
    t_snap = t0 + (t1 - t0) * snap_frac
    alpha_wide = _if3(f"lt(t\\,{t0})", "0", _min2(f"(t-{t0})/{fade_in}", "1"))
    wide = (
        f"drawtext=fontfile='{_ff_path(font)}':text='{_esc_text(_tracked(word))}':"
        f"fontcolor={color}:fontsize={int(fontsize * 0.82)}:x=(w-text_w)/2:y=(h-text_h)/2:"
        f"alpha='{alpha_wide}':enable='between(t\\,{t0}\\,{t_snap})'"
    )
    tight = (
        f"drawtext=fontfile='{_ff_path(font)}':text='{_esc_text(word.upper())}':"
        f"fontcolor={color}:fontsize={fontsize}:x=(w-text_w)/2:y=(h-text_h)/2:"
        f"enable='between(t\\,{t_snap}\\,{t1})'"
    )
    return [wide, tight]


def _word_blur_in_layer(word, font, fontsize, color, t0, t1, fade_in=0.30,
                         steps=(9, 5, 2)) -> list:
    """Chain for the shared transparent word-layer: draw + fade, then a few
    discrete gblur passes (decreasing sigma) gated to the first `fade_in`
    seconds -- a real defocus-to-sharp reveal (gblur's sigma option has no
    usable per-frame time expression in this ffmpeg build, so it's stepped
    instead of continuous; at 3-4 steps in ~0.3s it reads as smooth)."""
    alpha = _if3(f"lt(t\\,{t0})", "0", _min2(f"(t-{t0})/{fade_in}", "1"))
    frags = [
        f"drawtext=fontfile='{_ff_path(font)}':text='{_esc_text(word.upper())}':"
        f"fontcolor={color}:fontsize={fontsize}:x=(w-text_w)/2:y=(h-text_h)/2:"
        f"alpha='{alpha}':enable='between(t\\,{t0}\\,{t1})'"
    ]
    n = len(steps)
    step_dur = fade_in / n
    for i, sigma in enumerate(steps):
        s0 = t0 + i * step_dur
        s1 = t0 + (i + 1) * step_dur
        frags.append(f"gblur=sigma={sigma}:enable='between(t\\,{s0:.3f}\\,{s1:.3f})'")
    return frags


def _word_scale_pop_layer(word, font, fontsize, color, t0, t1, fade_in=0.28) -> list:
    """Drawtext-only fragment for the shared transparent word-layer (fade in,
    hold, fade out). The scale-in pop itself is added once, combined across
    all words, by _combined_scale_pop_expr() -- `scale` does not support the
    'enable' timeline option in this ffmpeg build, so per-word gating has to
    happen inside the width expression instead of via `enable=`."""
    alpha = _if3(f"lt(t\\,{t0})", "0", _min2(f"(t-{t0})/{fade_in}", "1"))
    return [
        f"drawtext=fontfile='{_ff_path(font)}':text='{_esc_text(word)}':"
        f"fontcolor={color}:fontsize={fontsize}:x=(w-text_w)/2:y=(h-text_h)/2:"
        f"alpha='{alpha}':enable='between(t\\,{t0}\\,{t1})'",
    ]


def _combined_scale_pop_expr(windows, fade_in=0.28, start_frac=0.82) -> str:
    """One `scale=w=...:h=-1:eval=frame` width expression covering every
    word window: pops from start_frac -> 1.0 over fade_in seconds at the
    start of each window, defaults to a no-op (iw, i.e. unchanged) the rest
    of the time. `between()` is a plain eval function (unlike `enable=`) so
    it works fine inside a width expression."""
    expr = "iw"
    for (t0, _t1) in windows:
        pop = f"iw*({start_frac}+{1 - start_frac}*(t-{t0})/{fade_in})"
        expr = _if3(f"between(t\\,{t0}\\,{t0 + fade_in:.3f})", pop, expr)
    return expr


# -- legibility darken pass ---------------------------------------------
# Drawn directly onto the real video (drawbox blends its alpha correctly
# against real frame content -- see module docstring). A single drawbox
# can't animate its own alpha smoothly (the "@alpha" is parsed as part of
# the color literal, not a runtime expression), so the fade-in is faked
# with a few stacked drawbox layers of increasing alpha over disjoint
# `enable` windows -- same stepping trick used for the blur-in word style.

def _darken_steps(color_hex: str, max_alpha: float, darken_start: float,
                   clip_end: float, fade_in: float = 0.35, steps: int = 4) -> list:
    frags = []
    for i in range(1, steps + 1):
        a = round(max_alpha * i / steps, 3)
        s0 = darken_start + (i - 1) * fade_in / steps
        s1 = darken_start + i * fade_in / steps if i < steps else clip_end
        frags.append(
            f"drawbox=x=0:y=0:w=iw:h=ih:color={color_hex}@{a}:t=fill:"
            f"enable='between(t\\,{s0:.3f}\\,{s1:.3f})'"
        )
    return frags


# -- per-variant timing (independent of source video dims/fps) --------------
# pre_pad: seconds of held darken before the first word starts fading in.
# post_pad: seconds held after the last word finishes fading out, before the
# clip's true end (breathing room so the sign-off doesn't feel clipped).

_VARIANT_TIMING = {
    "minimal_fade": dict(word_dur=0.46, gap=0.16, pre_pad=0.20, post_pad=0.40),
    "ink_stamp":    dict(word_dur=0.40, gap=0.12, pre_pad=0.12, post_pad=0.30),
    "circus_glow":  dict(word_dur=0.50, gap=0.18, pre_pad=0.25, post_pad=0.45),
    "neo_mono":     dict(word_dur=0.46, gap=0.14, pre_pad=0.16, post_pad=0.32),
}


def _overlay_span(variant_key: str) -> float:
    t = _VARIANT_TIMING[variant_key]
    return t["pre_pad"] + 3 * t["word_dur"] + 2 * t["gap"] + t["post_pad"]


# -- per-variant filter builders ---------------------------------------------
# Each returns (filter_chain_to_append_to_main_video, extra_word_layer_chain).
# extra_word_layer_chain is only non-None for styles that need their own
# transparent word-layer input (blur_in / scale_pop); the caller wires up the
# extra input and a single overlay back onto the main chain.

def _variant_minimal_fade(w, h, windows, darken_start, clip_end):
    """Safe, elegant baseline: gentle darken, wide-tracked light caps
    fade+rise in, one at a time."""
    frags = _darken_steps("black", 0.40, darken_start, clip_end, fade_in=0.35)
    for i, word in enumerate(WORDS):
        t0, t1 = windows[i]
        is_go = word == "GO"
        color = GOLD if is_go else CREAM
        fontsize = int(h * 0.155) if is_go else int(h * 0.13)
        frags.append(_word_fade_rise(word, FONT_SEGOE_LIGHT, fontsize, color, t0, t1, w, h,
                                      text=_tracked(word)))
    return ",".join(frags), None


def _variant_ink_stamp(w, h, windows, darken_start, clip_end):
    """Nods to the app's bold-ink/high-contrast graphic-novel identity:
    stronger darken + a contrast/desaturation push under the text, tracked
    caps SNAP into tight caps."""
    frags = _darken_steps("black", 0.55, darken_start, clip_end, fade_in=0.25)
    frags.append(f"eq=contrast=1.08:saturation=0.85:enable='between(t\\,{darken_start:.3f}\\,{clip_end:.3f})'")
    for i, word in enumerate(WORDS):
        t0, t1 = windows[i]
        is_go = word == "GO"
        color = CRIMSON if is_go else CREAM
        fontsize = int(h * 0.17) if is_go else int(h * 0.145)
        frags.extend(_word_snap_track(word, FONT_BAHNSCHRIFT, fontsize, color, t0, t1, w, h))
    return ",".join(frags), None


def _variant_circus_glow(w, h, windows, darken_start, clip_end):
    """Most literally on-brand: a warm crimson-black darken (not flat black)
    under gold words that soft-scale-in."""
    frags = _darken_steps("0x1a0508", 0.58, darken_start, clip_end, fade_in=0.45)
    word_frags = []
    for i, word in enumerate(WORDS):
        t0, t1 = windows[i]
        is_go = word == "GO"
        color = CREAM if is_go else GOLD
        fontsize = int(h * 0.16) if is_go else int(h * 0.135)
        word_frags.extend(_word_scale_pop_layer(word, FONT_SEGOE, fontsize, color, t0, t1))
    grow = _combined_scale_pop_expr(windows, fade_in=0.28)
    word_frags.append(f"scale=w='{grow}':h=-1:eval=frame")
    words_chain = "format=rgba," + ",".join(word_frags)
    return ",".join(frags), words_chain


def _variant_neo_mono(w, h, windows, darken_start, clip_end):
    """Most minimal / editorial: strongest, flattest darken (nearly a fade
    to black) under genuine stepped blur-to-sharp kinetic type, no color
    accents."""
    frags = _darken_steps("black", 0.68, darken_start, clip_end, fade_in=0.30)
    word_frags = []
    for i, word in enumerate(WORDS):
        t0, t1 = windows[i]
        is_go = word == "GO"
        fontsize = int(h * 0.155) if is_go else int(h * 0.13)
        word_frags.extend(_word_blur_in_layer(word, FONT_BAHNSCHRIFT, fontsize, CREAM, t0, t1))
    words_chain = "format=rgba," + ",".join(word_frags)
    return ",".join(frags), words_chain


VARIANTS = {
    "minimal_fade": {
        "label": "Minimal Fade",
        "description": "Gentle darken under real footage; wide-tracked light caps fade+rise in, one word at a time. The safe, elegant baseline.",
        "builder": _variant_minimal_fade,
    },
    "ink_stamp": {
        "label": "Ink Stamp",
        "description": "Stronger darken + contrast/desaturation push (nods to the app's bold-ink graphic-novel style); tracked-caps SNAP into tight caps.",
        "builder": _variant_ink_stamp,
    },
    "circus_glow": {
        "label": "Circus Glow",
        "description": "Warm crimson-black darken (not flat black) under gold words that soft-scale-in; the most literal brand-palette match.",
        "builder": _variant_circus_glow,
    },
    "neo_mono": {
        "label": "Neo Mono",
        "description": "Strongest, flattest darken (near-fade-to-black) under genuine stepped blur-to-sharp kinetic type, no color accents. Most minimal/editorial.",
        "builder": _variant_neo_mono,
    },
}


def list_variants() -> dict:
    return {k: {"label": v["label"], "description": v["description"]} for k, v in VARIANTS.items()}


# -- source probing -----------------------------------------------------

def _audio_stream_duration(path: str) -> float | None:
    """Precise duration of the first audio stream, or None if it can't be
    determined (caller should then assume the worst case: audio runs to the
    very end of the video, forcing a tail extension)."""
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


def _measure_silence(path: str, start: float, dur: float) -> dict:
    """Run volumedetect over [start, start+dur] of path's audio. Returns
    {"mean_db": float|None, "max_db": float|None, "has_audio_samples": bool}.
    Used to positively verify the overlay window is silent, not just assume
    it structurally should be."""
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}",
             "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        out = r.stderr
        mean_db = max_db = None
        for line in out.splitlines():
            if "mean_volume:" in line:
                mean_db = float(line.split("mean_volume:")[1].strip().split(" ")[0])
            elif "max_volume:" in line:
                max_db = float(line.split("max_volume:")[1].strip().split(" ")[0])
        return {"mean_db": mean_db, "max_db": max_db, "has_audio_samples": mean_db is not None}
    except Exception as e:
        return {"mean_db": None, "max_db": None, "has_audio_samples": False, "error": str(e)}


# -- rendering --------------------------------------------------------------

def _grab_near_end_frame(video_path: str, near_dur: float, out_path: Path) -> bool:
    """Grab a frame close to the end of video_path. `-sseof` was unreliable
    in this ffmpeg build (yielded 0 frames on a real test clip), and even a
    computed `-ss` timestamp can miss if `near_dur` slightly overstates the
    actual last decodable frame (observed on a real lip-sync export: the
    container's format.duration overstated the true video length by over a
    second). So this tries a cascade of seek offsets working backward from
    near_dur until one actually produces a frame, instead of trusting a
    single computed timestamp."""
    for backoff in (0.15, 0.5, 1.0, 2.0, 3.5, 5.0):
        seek = max(0.0, near_dur - backoff)
        r = subprocess.run(
            [FFMPEG, "-y", "-ss", f"{seek:.3f}", "-i", video_path,
             "-frames:v", "1", "-update", "1", str(out_path)],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return True
    return False


def _extend_with_freeze(norm_video_path: str, norm_dur: float, extra_seconds: float,
                         fps: int, tmp_dir: Path) -> Path | None:
    """Grab a frame near the end of norm_video_path (already normalized to
    the target w x h/fps -- see append_outro) and build a silent,
    frozen-frame video-only clip of extra_seconds at the same dims. Returns
    the clip path, or None on failure."""
    last_frame = tmp_dir / "last_frame.png"
    if not _grab_near_end_frame(norm_video_path, norm_dur, last_frame):
        log.warning("[outro] could not grab a near-end frame for freeze extension after trying multiple seek offsets")
        return None
    freeze_clip = tmp_dir / "freeze_tail.mp4"
    r = subprocess.run(
        [FFMPEG, "-y", "-loop", "1", "-t", f"{extra_seconds:.3f}", "-i", str(last_frame),
         "-r", str(fps)] + video_encode_args(crf=16) + ["-an", str(freeze_clip)],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0 or not freeze_clip.exists():
        log.warning("[outro] freeze-tail encode failed: %s", r.stderr[-300:] if r.stderr else "")
        return None
    return freeze_clip


def _concat_two(video_a: str, video_b: str, out_path: str) -> bool:
    """Concatenate two already-matching (same codec/dims/fps, no audio)
    clips via the concat demuxer (stream copy), falling back to a
    filter_complex re-encode if stream copy fails."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for p in (video_a, video_b):
            fwd = str(p).replace("\\", "/").replace("'", "''")
            f.write(f"file '{fwd}'\n")
        list_path = f.name
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", "-an", out_path],
            capture_output=True, timeout=120,
        )
        if r.returncode == 0 and Path(out_path).exists() and Path(out_path).stat().st_size > 0:
            return True
        log.warning("[outro] stream-copy concat failed, falling back to re-encode: %s",
                    (r.stderr or b"").decode(errors="replace")[-400:])
    except Exception as e:
        log.warning("[outro] concat exception: %s", e)

    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-i", video_a, "-i", video_b,
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[outv]",
             "-map", "[outv]"] + video_encode_args(crf=16) + ["-an", out_path],
            capture_output=True, timeout=300,
        )
        return r.returncode == 0 and Path(out_path).exists() and Path(out_path).stat().st_size > 0
    except Exception as e:
        log.warning("[outro] concat re-encode exception: %s", e)
        return False


def append_outro(input_video_path: str | Path, variant_key: str,
                  output_path: str | Path | None = None,
                  min_buffer: float = 0.35) -> tuple[Path | None, str | None]:
    """Composite the "Drop"/"Cat"/"GO" sign-off directly onto the tail of
    input_video_path, timed to land after its audio has gone silent.

    If the source doesn't already have enough silent runway at the end
    (audio duration close to or equal to video duration -- the common case
    for a generated music video), the tail is extended with a frozen last
    frame first. The original audio track is re-attached with a short
    fade-out (_AUDIO_FADE_S) instead of stopping dead, and finishes before
    the overlay's darken pass begins -- genuinely silent under the text,
    not just visually timed to look that way.

    Returns (output_path, error).
    """
    input_video_path = str(input_video_path)
    if not Path(input_video_path).exists():
        return None, f"Input video not found: {input_video_path}"
    if variant_key not in VARIANTS:
        return None, f"Unknown outro variant '{variant_key}' (have: {', '.join(VARIANTS)})"

    info = probe_file(input_video_path)
    w = info["width"] or DEFAULT_W
    h = info["height"] or DEFAULT_H
    fps = int(round(info["fps"])) or DEFAULT_FPS
    w = w if w % 2 == 0 else w + 1
    h = h if h % 2 == 0 else h + 1
    container_dur = info["duration"] or probe_duration(input_video_path)
    if container_dur <= 0:
        return None, "could not determine source video duration"
    has_audio = info["has_audio"]

    overlay_span = _overlay_span(variant_key)

    out_path = Path(output_path) if output_path else Path(input_video_path).with_name(
        Path(input_video_path).stem + f"_outro_{variant_key}.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as tmp:
        tmp = Path(tmp)

        # Always normalize first (re-encode to the target dims/fps, video
        # only) and measure the RESULT's real duration -- container-level
        # duration metadata can overstate the actual decodable video length
        # (observed on a real lip-sync export: format.duration said 30.0s,
        # the true video was ~28.9s). Every timing decision below is anchored
        # to this measured duration and to the audio stream's own measured
        # duration, never to the container's claimed duration, so the darken
        # pass can't accidentally land while audio is still technically
        # present just because a metadata tag was optimistic.
        norm_src = tmp / "src_normalized.mp4"
        r = subprocess.run(
            [FFMPEG, "-y", "-i", input_video_path,
             "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
             "-r", str(fps)] + video_encode_args(crf=16) + ["-an", str(norm_src)],
            capture_output=True, timeout=1800,
        )
        if r.returncode != 0 or not norm_src.exists():
            return None, f"could not normalize source video: {r.stderr[-400:].decode(errors='replace') if isinstance(r.stderr, bytes) else r.stderr}"
        norm_dur = probe_duration(str(norm_src))
        if norm_dur <= 0:
            norm_dur = container_dur

        if has_audio:
            audio_dur = _audio_stream_duration(input_video_path)
            if audio_dur is None:
                audio_dur = norm_dur  # unknown -- assume the worst case, audio runs to the very end
        else:
            audio_dur = 0.0

        # Ensure darken_start (= total_duration - overlay_span) lands at
        # least min_buffer seconds after audio_dur.
        needed_extra = max(0.0, (audio_dur + min_buffer + overlay_span) - norm_dur)

        if needed_extra > 0.05:
            freeze_clip = _extend_with_freeze(str(norm_src), norm_dur, needed_extra, fps, tmp)
            if freeze_clip is None:
                return None, "could not build frozen-frame tail extension"
            base_video = tmp / "base_extended.mp4"
            if not _concat_two(str(norm_src), str(freeze_clip), str(base_video)):
                return None, "could not concatenate frozen tail onto source"
            log.info("[outro] extended tail by %.2fs (freeze frame) -- audio ends at %.2fs, normalized video was %.2fs",
                      needed_extra, audio_dur, norm_dur)
        else:
            base_video = norm_src
            log.info("[outro] existing tail has enough silent runway -- no extension needed "
                      "(audio ends at %.2fs, normalized video is %.2fs)", audio_dur, norm_dur)

        # Re-probe once more: concat (even with the re-encode fallback) can
        # shift the total by a frame or two, and this is the figure every
        # absolute timestamp below is built from.
        total_duration = probe_duration(str(base_video))
        if total_duration <= 0:
            total_duration = norm_dur + (needed_extra if needed_extra > 0.05 else 0.0)
        if total_duration < overlay_span:
            log.warning("[outro] base video (%.2fs) shorter than the overlay span (%.2fs) -- "
                        "sign-off will be tight/may not fully fit", total_duration, overlay_span)

        darken_start = total_duration - overlay_span
        wm_start = darken_start + _VARIANT_TIMING[variant_key]["pre_pad"]
        windows, _ = _word_windows(wm_start, _VARIANT_TIMING[variant_key]["word_dur"],
                                    _VARIANT_TIMING[variant_key]["gap"])

        main_chain, words_layer_chain = VARIANTS[variant_key]["builder"](w, h, windows, darken_start, total_duration)

        cmd = [FFMPEG, "-y", "-i", str(base_video)]
        if words_layer_chain is not None:
            cmd += ["-loop", "1", "-t", f"{total_duration:.3f}", "-i", str(_blank_rgba_png(w, h))]
            filter_complex = (
                f"[0:v]{main_chain}[bg];"
                f"[1:v]{words_layer_chain}[wtxt];"
                f"[bg][wtxt]overlay=0:0:format=auto[outv]"
            )
        else:
            filter_complex = f"[0:v]{main_chain}[outv]"

        filtered_video = tmp / "filtered.mp4"
        cmd += ["-filter_complex", filter_complex, "-map", "[outv]", "-t", f"{total_duration:.3f}"]
        cmd += video_encode_args(crf=16) + ["-an", str(filtered_video)]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            log.warning("[outro] overlay render failed for %s: %s", variant_key, r.stderr[-800:])
            return None, f"ffmpeg overlay failed: {r.stderr[-400:]}"
        if not filtered_video.exists() or filtered_video.stat().st_size == 0:
            return None, "ffmpeg produced empty overlay output"

        # Re-attach the original audio, fading it out over its last
        # _AUDIO_FADE_S seconds instead of letting it stop dead -- ending
        # before darken_start was never meant to mean "hard-cut," a clipped
        # song reads as broken even when the silence timing itself is right.
        if has_audio:
            fade_dur = min(_AUDIO_FADE_S, max(0.1, audio_dur))
            fade_start = max(0.0, audio_dur - fade_dur)
            r = subprocess.run(
                [FFMPEG, "-y", "-i", str(filtered_video), "-i", input_video_path,
                 "-map", "0:v", "-map", "1:a?",
                 "-af", f"afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 str(out_path)],
                capture_output=True, timeout=300,
            )
            if r.returncode != 0 or not out_path.exists():
                log.warning("[outro] audio re-mux failed, falling back to silent output: %s",
                            r.stderr[-400:])
                import shutil
                shutil.copy2(filtered_video, out_path)
        else:
            import shutil
            shutil.copy2(filtered_video, out_path)

    if not out_path.exists() or out_path.stat().st_size == 0:
        return None, "append_outro produced empty output"

    final_info = probe_file(out_path)
    if not final_info["width"] or final_info["duration"] <= 0:
        return None, f"ffprobe could not validate output: {final_info}"

    silence = _measure_silence(str(out_path), darken_start, total_duration - darken_start)
    log.info("[outro] appended '%s' to %s -> %s (%.2fs, overlay window %.2f-%.2fs, "
              "audio under overlay: mean=%s dB max=%s dB)",
              variant_key, Path(input_video_path).name, out_path.name, final_info["duration"],
              darken_start, total_duration, silence.get("mean_db"), silence.get("max_db"))
    return out_path, None


def render_samples(input_video_path: str | Path, out_dir: str | Path | None = None) -> dict:
    """Run all four variants against one real source clip for side-by-side
    review. Returns {variant_key: {"path": ..., "error": ...}}."""
    out_dir = Path(out_dir) if out_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(input_video_path).stem
    results = {}
    for key in VARIANTS:
        out_path = out_dir / f"outro_composite_{key}_{stem}.mp4"
        path, err = append_outro(input_video_path, key, output_path=out_path)
        results[key] = {"path": str(path) if path else None, "error": err}
    return results
