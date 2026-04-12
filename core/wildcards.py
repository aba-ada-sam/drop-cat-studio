"""Merged wildcard system — inline defaults + filesystem discovery.

Combines the inline wildcard dicts from Fun-Videos (camera, motion, mood,
transform, style, music_genre, music_mood) with the filesystem wildcard
discovery from SD-Prompts (scans .txt files from a configurable directory).
"""
import os
import random
import re
from pathlib import Path

# ── Inline wildcards (from Fun-Videos) ───────────────────────────────────────

INLINE_WILDCARDS: dict[str, list[str]] = {
    "__camera__": [
        "slow zoom in", "slow zoom out", "gentle orbit right",
        "gentle orbit left", "dolly forward", "dolly backward",
        "crane shot rising", "crane shot descending",
        "slow pan right", "slow pan left",
        "tracking shot forward", "steady hovering shot",
    ],
    "__motion__": [
        "gentle sway", "slow drift", "soft ripples spreading",
        "petals scatter rising", "smoke wisps curling",
        "leaves tumbling gently", "fabric flowing wind",
        "water droplets falling", "dust motes floating",
        "flickering light", "aurora shifting", "embers rising",
    ],
    "__mood__": [
        "dreamy and ethereal", "warm golden hour", "mysterious twilight",
        "peaceful serenity", "dramatic intensity", "nostalgic vintage",
        "cosmic wonder", "playful whimsy", "elegant sophistication",
        "raw and powerful", "meditative calm", "energetic vibrance",
    ],
    "__transform__": [
        "colors shift subtly", "fog rolls in slowly",
        "light bloom intensifies", "shadows deepen gradually",
        "saturation pulses gently", "scene dissolves to particles",
        "crystalline frost spreads", "golden light breaks through",
        "silhouette emerges", "texture morphs organic",
    ],
    "__style__": [
        "cinematic film grain", "soft focus dreamy",
        "high contrast dramatic", "muted pastel tones",
        "vibrant saturated colors", "monochrome noir",
        "watercolor wash effect", "anamorphic lens flare",
        "tilt-shift miniature",
    ],
    "__music_genre__": [
        "ambient electronic", "lo-fi hip hop", "orchestral cinematic",
        "acoustic folk", "jazz lounge", "chillwave synth",
        "classical piano", "world fusion", "post-rock",
        "dream pop", "minimal techno", "neo-soul",
    ],
    "__music_mood__": [
        "peaceful and calm", "dark and brooding", "uplifting and bright",
        "mysterious and tense", "romantic and warm", "epic and triumphant",
        "melancholic and reflective", "playful and fun", "ethereal and floating",
    ],
}

# ── Filesystem wildcard discovery (from SD-Prompts) ──────────────────────────

_fs_cache: dict[str, list[str]] = {}
_fs_cache_mtime: dict[str, float] = {}


def discover_filesystem_wildcards(root_dir: str) -> dict[str, list[str]]:
    """Scan a directory for .txt wildcard files.

    Returns dict mapping __filename__ to list of lines.
    """
    wildcards: dict[str, list[str]] = {}
    root = Path(root_dir)
    if not root.exists():
        return wildcards

    for txt_file in sorted(root.rglob("*.txt")):
        try:
            # Build token name from relative path
            rel = txt_file.relative_to(root)
            stem = str(rel.with_suffix("")).replace(os.sep, "/").replace("/", "_")
            token = f"__{stem}__"

            # Cache with mtime check
            mtime = txt_file.stat().st_mtime
            if token in _fs_cache and _fs_cache_mtime.get(token, 0) == mtime:
                wildcards[token] = _fs_cache[token]
                continue

            lines = [
                line.strip()
                for line in txt_file.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if lines:
                wildcards[token] = lines
                _fs_cache[token] = lines
                _fs_cache_mtime[token] = mtime
        except Exception:
            continue

    return wildcards


def get_all(fs_root: str = "") -> dict[str, list[str]]:
    """Get all wildcards: inline defaults + filesystem (if configured)."""
    result = dict(INLINE_WILDCARDS)
    if fs_root:
        result.update(discover_filesystem_wildcards(fs_root))
    return result


def get_tokens(fs_root: str = "") -> list[dict]:
    """Get all wildcard tokens with sample values (for UI display)."""
    all_wc = get_all(fs_root)
    tokens = []
    for token, values in sorted(all_wc.items()):
        tokens.append({
            "token": token,
            "count": len(values),
            "samples": values[:5],
            "source": "inline" if token in INLINE_WILDCARDS else "filesystem",
        })
    return tokens


def expand(text: str, fs_root: str = "") -> str:
    """Expand all __wildcard__ tokens in text with random selections."""
    all_wc = get_all(fs_root)

    def _replace(match):
        token = match.group(0)
        values = all_wc.get(token, [])
        if values:
            return random.choice(values)
        return token  # leave unknown tokens as-is

    return re.sub(r"__\w+__", _replace, text)


def invalidate_cache():
    """Clear the filesystem wildcard cache (after file edits)."""
    _fs_cache.clear()
    _fs_cache_mtime.clear()
