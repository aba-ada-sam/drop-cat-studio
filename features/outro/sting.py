"""Branded outro / logo-sting generator for Drop Cat Studio.

Renders a short (~3.3-3.9s) branded end-card: kinetic type spelling out
"Drop" / "Cat" / "GO" word-by-word in a modern, ephemeral style, followed by
a "Drop Cat / Studio" text wordmark spinning and scaling into place. Pure
ffmpeg filters (drawtext, rotate, scale, gblur, vignette) -- no WanGP, no
ACE-Step, no GPU job queue. Safe to call from any thread at any time; never
competes for GPU with in-flight generation work.

Four variants ship out of the box (see VARIANTS below) so a favourite can be
picked by eye. Once one is chosen:
  - render_variant() / render_samples()  -- standalone review clips (this file's
    __main__ / the /api caller uses these to produce the .mp4s Andrew watches).
  - append_outro()                        -- the wiring point for later: takes a
    finished video + a variant name, renders the sting to match that video's
    exact dimensions/fps, and concatenates it onto the end. Not called from
    any pipeline yet -- that's a follow-up once Andrew picks a favourite.

DO NOT use static/logo-*.png (the app's splash-screen cat mark) anywhere in
this module. Andrew, 2026-08-02: "I do not like the website's cheesey logo
being involved in this video output, that's for me only" -- that icon is
chrome for using the DCS app itself, not a brand mark for content he creates
or shares. The "spin and settle" motion beat that used to carry the logo
image now carries a pure-typography wordmark instead (_wordmark_chain +
_spin_settle_chain below). If you're tempted to reintroduce an icon/mark of
any kind here, don't -- ask first.

--- ffmpeg gotcha discovered while building this (2026-08-02) ---
`overlay` in this ffmpeg build (8.1 gyan.dev essentials) does NOT alpha-blend
against a synthetic `-f lavfi -i "color=c=black@0.0:..."` transparent source --
it silently ignores the alpha plane and paints the RGB (black) opaquely,
blowing away whatever is behind it. A REAL rgba PNG file (even a blank one
written by PIL) composites correctly. So any filter chain here that needs a
"draw on a transparent layer, then overlay" step (blur-in, scale-pop word
styles) sources that transparent layer from a tiny cached PNG on disk
(_blank_rgba_png), never from lavfi color+alpha. rotate()'s own fillcolor
alpha (used for the wordmark's spin padding) is unaffected -- verified
separately that rotate + overlay correctly preserve real per-pixel alpha
end to end.
"""
import logging
import math
import subprocess
import tempfile
import uuid
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
BG_NEAR_BLACK = "0x0d0606"
GOLD = "0xd4a017"
CRIMSON = "0xc41e3a"
CREAM = "0xf0e6d0"

WORDS = ["Drop", "Cat", "GO"]

DEFAULT_W, DEFAULT_H, DEFAULT_FPS = 1280, 720, 30

_TMP_PREFIX = "dcs-outro-"


# -- tiny expression helpers (ffmpeg eval syntax; commas inside nested calls
#    are backslash-escaped -- validated empirically against this ffmpeg build,
#    see module docstring) ------------------------------------------------

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

def _word_windows(start: float, word_dur: float, gap: float, n: int = 3):
    """Return [(t0, t1), ...] for n sequential words, plus the end time."""
    t = start
    windows = []
    for _ in range(n):
        windows.append((t, t + word_dur))
        t += word_dur + gap
    return windows, t - gap


# -- word reveal styles -------------------------------------------------
# Each returns a filter-chain fragment (or list of fragments to comma-join).
# fade_rise/snap_track chain directly onto the background stream (no separate
# compositing needed -- drawtext blends onto whatever frame it's given).
# blur_in/scale_pop need their own real-alpha transparent PNG-sourced canvas
# (see _blank_rgba_png + the per-variant builders below that wire it up with
# its own `-loop 1` input + a single overlay back onto the background).

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
    'enable' timeline option in this ffmpeg build ("Timeline not supported
    with filter 'scale'"), so per-word gating has to happen inside the width
    expression instead of via `enable=`."""
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


# -- wordmark spin+scale ----------------------------------------------------
# Replaces what used to be an app-logo image spin (see docstring at top of
# file for why: the splash-screen mark is off-limits here). Same motion
# beat, built from typography instead of an icon: a two-line "DROP CAT" /
# "S T U D I O" lockup rendered onto a transparent canvas, then spun+scaled
# in exactly like the old logo image was.

def _wordmark_chain(font_main: Path, font_sub: Path, color_main: str, color_sub: str,
                     cw: int, ch: int) -> str:
    """Draw a static two-line 'DROP CAT' / tracked 'S T U D I O' wordmark
    onto a transparent canvas of size cw x ch. Pure typography -- no icon,
    no image asset. The canvas is then handed to _spin_settle_chain() for
    the actual spin/settle motion."""
    line1_size = int(ch * 0.44)
    line2_size = int(ch * 0.20)
    gap = int(ch * 0.10)
    total_h = line1_size + gap + line2_size
    y1 = f"(h-{total_h})/2"
    y2 = f"(h-{total_h})/2+{line1_size + gap}"
    return (
        f"format=rgba,"
        f"drawtext=fontfile='{_ff_path(font_main)}':text='DROP CAT':fontcolor={color_main}:"
        f"fontsize={line1_size}:x=(w-text_w)/2:y='{y1}',"
        f"drawtext=fontfile='{_ff_path(font_sub)}':text='{_esc_text(_tracked('Studio'))}':"
        f"fontcolor={color_sub}:fontsize={line2_size}:x=(w-text_w)/2:y='{y2}'"
    )


def _spin_settle_chain(w: int, h: int, spin_start: float, spin_dur: float, turns: float,
                        overshoot: bool = False) -> str:
    """Filter chain for an already-rgba, already-sized (w x h) input layer:
    rotate with a decelerating spin-in (static padded square canvas -- see
    module docstring for why the canvas size must be a static int, not a
    per-frame expression), then a scale pop-in from small -> full size, then
    an alpha fade-in gated to spin_start. Settles to a static, unrotated,
    full-size layer for the remainder of the clip. Content-agnostic (it used
    to carry the app logo image; now it carries the text wordmark).
    """
    pad = int(math.ceil(math.hypot(w, h) / 2) * 2)  # diagonal, rounded even
    tl = f"(t-{spin_start})"
    # angle: starts at turns*2*PI, eases to 0 by spin_start+spin_dur
    angle = _if3(f"lt(t\\,{spin_start})", "0",
                 _if3(f"lt(t\\,{spin_start + spin_dur:.3f})",
                      f"2*PI*{turns}*(1-{tl}/{spin_dur})", "0"))
    if overshoot:
        # grow past 100% briefly then settle back -- a small "impact" bounce
        peak = spin_start + spin_dur * 0.78
        settle = spin_start + spin_dur
        grow = _if3(f"lt(t\\,{spin_start})", "40",
                    _if3(f"lt(t\\,{peak:.3f})", f"40+({pad}*1.08-40)*{tl}/{spin_dur * 0.78:.3f}",
                         _if3(f"lt(t\\,{settle:.3f})",
                              f"{pad}*1.08-({pad}*0.08)*(t-{peak:.3f})/{spin_dur * 0.22:.3f}",
                              f"{pad}")))
    else:
        grow = _if3(f"lt(t\\,{spin_start})", "40",
                    _if3(f"lt(t\\,{spin_start + spin_dur:.3f})",
                         f"40+({pad}-40)*{tl}/{spin_dur}", f"{pad}"))
    return (
        f"rotate=a='{angle}':fillcolor=black@0:ow={pad}:oh={pad},"
        f"scale=w='{grow}':h=-1:eval=frame,"
        f"fade=t=in:st={spin_start}:d=0.15:alpha=1"
    )


# -- backgrounds ----------------------------------------------------------

def _bg_grain(strength=4) -> str:
    return f"noise=alls={strength}:allf=t+u"


# -- variant registry -------------------------------------------------------
# Each entry is (label, description, builder). Builders take (w, h, fps) and
# return (duration_seconds, ffmpeg_cmd_list).

def _variant_minimal_fade(w, h, fps):
    """Safe, elegant baseline: plain dark bg, soft fade+rise kinetic type in
    wide-tracked light caps, snappy wordmark spin-in."""
    windows, words_end = _word_windows(start=0.24, word_dur=0.44, gap=0.13)
    wm_start = words_end + 0.24
    spin_dur = 0.70
    hold = 0.55
    duration = wm_start + spin_dur + hold + 0.26

    cw, ch = int(w * 0.52), int(h * 0.24)
    bg_in = ["-f", "lavfi", "-i", f"color=c={BG_NEAR_BLACK}:s={w}x{h}:d={duration:.3f}:r={fps}"]
    wm_in = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(_blank_rgba_png(cw, ch))]

    word_chain = ["vignette=PI/2.6"]
    for i, word in enumerate(WORDS):
        t0, t1 = windows[i]
        is_go = word == "GO"
        color = GOLD if is_go else CREAM
        fontsize = int(h * 0.135) if is_go else int(h * 0.115)
        word_chain.append(
            _word_fade_rise(word, FONT_SEGOE_LIGHT, fontsize, color, t0, t1, w, h,
                             text=_tracked(word))
        )

    wm_chain = (_wordmark_chain(FONT_SEGOE_LIGHT, FONT_SEGOE_LIGHT, CREAM, GOLD, cw, ch)
                + "," + _spin_settle_chain(cw, ch, wm_start, spin_dur, turns=1.3))

    filter_complex = (
        f"[0:v]{','.join(word_chain)}[bg];"
        f"[1:v]{wm_chain}[wordmark];"
        f"[bg][wordmark]overlay=x='(main_w-overlay_w)/2':y='(main_h-overlay_h)/2':format=auto,"
        f"fade=t=in:st=0:d=0.18,fade=t=out:st={duration-0.30:.3f}:d=0.30[outv]"
    )
    cmd = [FFMPEG, "-y"] + bg_in + wm_in + ["-filter_complex", filter_complex, "-map", "[outv]"]
    return duration, cmd


def _variant_ink_stamp(w, h, fps):
    """Nods to the app's new bold-ink/high-contrast graphic-novel identity:
    tighter vignette + subtle grain, tracked-caps SNAP reveal, harder
    wordmark spin with a small landing bounce."""
    windows, words_end = _word_windows(start=0.22, word_dur=0.42, gap=0.12)
    wm_start = words_end + 0.22
    spin_dur = 0.55
    hold = 0.70
    duration = wm_start + spin_dur + 0.20 + hold + 0.28

    cw, ch = int(w * 0.52), int(h * 0.24)
    bg_in = ["-f", "lavfi", "-i", f"color=c={BG_NEAR_BLACK}:s={w}x{h}:d={duration:.3f}:r={fps}"]
    wm_in = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(_blank_rgba_png(cw, ch))]

    word_chain = [f"{_bg_grain(4)},vignette=PI/4,eq=contrast=1.08:saturation=1.05"]
    for i, word in enumerate(WORDS):
        t0, t1 = windows[i]
        is_go = word == "GO"
        color = CRIMSON if is_go else CREAM
        fontsize = int(h * 0.16) if is_go else int(h * 0.135)
        word_chain.extend(_word_snap_track(word, FONT_BAHNSCHRIFT, fontsize, color, t0, t1, w, h))

    wm_chain = (_wordmark_chain(FONT_BAHNSCHRIFT, FONT_BAHNSCHRIFT, CREAM, CRIMSON, cw, ch)
                + "," + _spin_settle_chain(cw, ch, wm_start, spin_dur, turns=2.0, overshoot=True))

    filter_complex = (
        f"[0:v]{','.join(word_chain)}[bg];"
        f"[1:v]{wm_chain}[wordmark];"
        f"[bg][wordmark]overlay=x='(main_w-overlay_w)/2':y='(main_h-overlay_h)/2':format=auto,"
        f"fade=t=in:st=0:d=0.12,fade=t=out:st={duration-0.28:.3f}:d=0.28[outv]"
    )
    cmd = [FFMPEG, "-y"] + bg_in + wm_in + ["-filter_complex", filter_complex, "-map", "[outv]"]
    return duration, cmd


def _variant_circus_glow(w, h, fps):
    """Most literally on-brand: warm crimson-to-black radial glow (the app's
    circus palette), soft scale-in pop kinetic type, graceful slow wordmark
    settle with a longer hold -- the most 'graceful' paced variant."""
    windows, words_end = _word_windows(start=0.26, word_dur=0.46, gap=0.14)
    wm_start = words_end + 0.26
    spin_dur = 0.85
    hold = 0.60
    duration = wm_start + spin_dur + hold + 0.30

    cw, ch = int(w * 0.54), int(h * 0.25)
    bg_in = ["-f", "lavfi", "-i", f"color=c=0x3a0d10:s={w}x{h}:d={duration:.3f}:r={fps}"]
    wm_in = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(_blank_rgba_png(cw, ch))]
    png = _blank_rgba_png(w, h)
    words_in = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(png)]

    word_frags = []
    for i, word in enumerate(WORDS):
        t0, t1 = windows[i]
        is_go = word == "GO"
        color = CREAM if is_go else GOLD
        fontsize = int(h * 0.15) if is_go else int(h * 0.125)
        word_frags.extend(_word_scale_pop_layer(word, FONT_SEGOE, fontsize, color, t0, t1))
    grow = _combined_scale_pop_expr(windows, fade_in=0.28)
    word_frags.append(f"scale=w='{grow}':h=-1:eval=frame")
    words_chain = "format=rgba," + ",".join(word_frags)

    wm_chain = (_wordmark_chain(FONT_SEGOE, FONT_SEGOE, GOLD, CREAM, cw, ch)
                + "," + _spin_settle_chain(cw, ch, wm_start, spin_dur, turns=1.0))

    filter_complex = (
        f"[0:v]vignette=PI/3.2[bg];"
        f"[2:v]{words_chain}[wtxt];"
        f"[1:v]{wm_chain}[wordmark];"
        f"[bg][wtxt]overlay=x='(main_w-overlay_w)/2':y='(main_h-overlay_h)/2':format=auto[bgw];"
        f"[bgw][wordmark]overlay=x='(main_w-overlay_w)/2':y='(main_h-overlay_h)/2':format=auto,"
        f"fade=t=in:st=0:d=0.22,fade=t=out:st={duration-0.35:.3f}:d=0.35[outv]"
    )
    cmd = [FFMPEG, "-y"] + bg_in + wm_in + words_in + ["-filter_complex", filter_complex, "-map", "[outv]"]
    return duration, cmd


def _variant_neo_mono(w, h, fps):
    """Most minimal / editorial: flat near-black, tight-tracked genuine
    blur-in kinetic type, fastest cleanest hard-settle wordmark spin -- the
    variant that leans hardest away from anything that could read as dated."""
    windows, words_end = _word_windows(start=0.24, word_dur=0.46, gap=0.14)
    wm_start = words_end + 0.22
    spin_dur = 0.50
    hold = 0.75
    duration = wm_start + spin_dur + hold + 0.28

    cw, ch = int(w * 0.50), int(h * 0.22)
    bg_in = ["-f", "lavfi", "-i", f"color=c=0x0a0808:s={w}x{h}:d={duration:.3f}:r={fps}"]
    wm_in = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(_blank_rgba_png(cw, ch))]
    png = _blank_rgba_png(w, h)
    words_in = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(png)]

    word_frags = []
    for i, word in enumerate(WORDS):
        t0, t1 = windows[i]
        is_go = word == "GO"
        fontsize = int(h * 0.145) if is_go else int(h * 0.12)
        word_frags.extend(_word_blur_in_layer(word, FONT_BAHNSCHRIFT, fontsize, CREAM, t0, t1))
    words_chain = "format=rgba," + ",".join(word_frags)

    wm_chain = (_wordmark_chain(FONT_BAHNSCHRIFT, FONT_BAHNSCHRIFT, CREAM, CREAM, cw, ch)
                + "," + _spin_settle_chain(cw, ch, wm_start, spin_dur, turns=1.0))

    filter_complex = (
        f"[0:v]{_bg_grain(3)}[bg];"
        f"[2:v]{words_chain}[wtxt];"
        f"[1:v]{wm_chain}[wordmark];"
        f"[bg][wtxt]overlay=0:0:format=auto[bgw];"
        f"[bgw][wordmark]overlay=x='(main_w-overlay_w)/2':y='(main_h-overlay_h)/2':format=auto,"
        f"fade=t=in:st=0:d=0.14,fade=t=out:st={duration-0.28:.3f}:d=0.28[outv]"
    )
    cmd = [FFMPEG, "-y"] + bg_in + wm_in + words_in + ["-filter_complex", filter_complex, "-map", "[outv]"]
    return duration, cmd


VARIANTS = {
    "minimal_fade": {
        "label": "Minimal Fade",
        "description": "Plain near-black bg, soft vignette; wide-tracked light caps fade+rise in; snappy 1.3-turn 'Drop Cat / Studio' wordmark spin (text only, no icon). The safe, elegant baseline.",
        "builder": _variant_minimal_fade,
    },
    "ink_stamp": {
        "label": "Ink Stamp",
        "description": "Subtle grain + tighter vignette + contrast boost (nods to the app's new bold-ink graphic-novel style); tracked-caps SNAP into tight caps; punchy 2-turn wordmark spin with a small landing bounce.",
        "builder": _variant_ink_stamp,
    },
    "circus_glow": {
        "label": "Circus Glow",
        "description": "Warm crimson-to-black radial glow (the app's own circus palette, most literal brand match); gold words soft-scale-in; graceful single-turn wordmark settle with a longer hold.",
        "builder": _variant_circus_glow,
    },
    "neo_mono": {
        "label": "Neo Mono",
        "description": "Flattest, most minimal/editorial treatment; genuine stepped blur-to-sharp kinetic type (no color accents); fastest, hardest-settle wordmark spin. Leans furthest from anything dated.",
        "builder": _variant_neo_mono,
    },
}


def list_variants() -> dict:
    return {k: {"label": v["label"], "description": v["description"]} for k, v in VARIANTS.items()}


# -- rendering --------------------------------------------------------------

def render_variant(variant_key: str, output_path: str | Path,
                    width: int = DEFAULT_W, height: int = DEFAULT_H,
                    fps: int = DEFAULT_FPS) -> tuple[Path | None, str | None]:
    """Render one outro variant to output_path. Returns (path, error)."""
    if variant_key not in VARIANTS:
        return None, f"Unknown outro variant '{variant_key}' (have: {', '.join(VARIANTS)})"

    # even dims required for yuv420p encode
    width = width if width % 2 == 0 else width + 1
    height = height if height % 2 == 0 else height + 1

    duration, cmd = VARIANTS[variant_key]["builder"](width, height, fps)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = cmd + video_encode_args(crf=16) + ["-an", "-t", f"{duration:.3f}", str(out_path)]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return None, f"outro render error: {e}"
    if r.returncode != 0:
        log.warning("[outro] render failed for %s: %s", variant_key, r.stderr[-800:])
        return None, f"ffmpeg failed: {r.stderr[-400:]}"
    if not out_path.exists() or out_path.stat().st_size == 0:
        return None, "ffmpeg produced empty output"

    # Verify with ffprobe rather than trusting the exit code alone.
    info = probe_file(out_path)
    if not info["width"] or info["duration"] <= 0:
        return None, f"ffprobe could not validate output: {info}"
    log.info("[outro] rendered %s -> %s (%sx%s, %.2fs)",
              variant_key, out_path.name, info["width"], info["height"], info["duration"])
    return out_path, None


def render_samples(out_dir: str | Path | None = None,
                    width: int = DEFAULT_W, height: int = DEFAULT_H,
                    fps: int = DEFAULT_FPS) -> dict:
    """Render all variants as standalone review clips. Returns
    {variant_key: {"path": ..., "error": ...}}."""
    out_dir = Path(out_dir) if out_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for key in VARIANTS:
        out_path = out_dir / f"outro_sample_{key}.mp4"
        path, err = render_variant(key, out_path, width=width, height=height, fps=fps)
        results[key] = {"path": str(path) if path else None, "error": err}
    return results


# -- appending onto a finished video -----------------------------------------

def _concat_two(video_a: str, video_b: str, out_path: str, w: int, h: int, fps: int) -> bool:
    """Concatenate two already-matching (same codec/dims/fps, no audio)
    clips via the concat demuxer (stream copy), falling back to a
    filter_complex re-encode if stream copy fails. Mirrors the pattern used
    by features/fun_videos/multi_pipeline.py's _concat_clips."""
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
                  output_path: str | Path | None = None) -> tuple[Path | None, str | None]:
    """Render `variant_key` to match input_video_path's dimensions/fps and
    concatenate it onto the end. Preserves the original video's audio track
    (the outro itself is silent -- it just plays out after the source audio
    ends, which is normal for a brand sting). Returns (output_path, error).
    """
    input_video_path = str(input_video_path)
    if not Path(input_video_path).exists():
        return None, f"Input video not found: {input_video_path}"

    info = probe_file(input_video_path)
    w = info["width"] or DEFAULT_W
    h = info["height"] or DEFAULT_H
    fps = int(round(info["fps"])) or DEFAULT_FPS
    w = w if w % 2 == 0 else w + 1
    h = h if h % 2 == 0 else h + 1

    out_path = Path(output_path) if output_path else Path(input_video_path).with_name(
        Path(input_video_path).stem + f"_outro_{variant_key}.mp4")

    with tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as tmp:
        tmp = Path(tmp)
        outro_clip = tmp / "outro.mp4"
        rendered, err = render_variant(variant_key, outro_clip, width=w, height=h, fps=fps)
        if err:
            return None, f"outro render failed: {err}"

        # Normalize the source video's video stream to the same codec/pix_fmt
        # so the concat demuxer's stream-copy path has a fair chance (falls
        # back to a filter_complex re-encode automatically otherwise).
        norm_src = tmp / "src_normalized.mp4"
        r = subprocess.run(
            [FFMPEG, "-y", "-i", input_video_path,
             "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
             "-r", str(fps)] + video_encode_args(crf=16) + ["-an", str(norm_src)],
            capture_output=True, timeout=1800,
        )
        if r.returncode != 0 or not norm_src.exists():
            return None, f"could not normalize source video: {r.stderr[-400:].decode(errors='replace') if isinstance(r.stderr, bytes) else r.stderr}"

        concat_silent = tmp / "concat_silent.mp4"
        if not _concat_two(str(norm_src), str(rendered), str(concat_silent), w, h, fps):
            return None, "concat of source + outro failed"

        # Re-attach the original audio (if any) -- video track is now longer
        # than the audio track by the outro's duration, which is fine: the
        # sting plays out silent, which is normal for a brand sting.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if info["has_audio"]:
            r = subprocess.run(
                [FFMPEG, "-y", "-i", str(concat_silent), "-i", input_video_path,
                 "-map", "0:v", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 str(out_path)],
                capture_output=True, timeout=300,
            )
            if r.returncode != 0 or not out_path.exists():
                log.warning("[outro] audio re-mux failed, falling back to silent output: %s",
                            r.stderr[-400:])
                import shutil
                shutil.copy2(concat_silent, out_path)
        else:
            import shutil
            shutil.copy2(concat_silent, out_path)

    if not out_path.exists() or out_path.stat().st_size == 0:
        return None, "append_outro produced empty output"
    dur = probe_duration(out_path)
    log.info("[outro] appended '%s' to %s -> %s (%.2fs)",
              variant_key, Path(input_video_path).name, out_path.name, dur)
    return out_path, None
