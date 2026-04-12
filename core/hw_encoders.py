"""Hardware encoder detection for ffmpeg.

Extracted from Github Video Editor/github_bot_video_editor.py (lines 100-178).
Probes ffmpeg for available GPU-accelerated encoders (NVENC, AMF, QuickSync).
"""
import re
import subprocess

# All known encoders: (id, display_label, is_hw)
KNOWN_ENCODERS = [
    ("libx264",     "H.264 (CPU — libx264)",       False),
    ("libx265",     "H.265 (CPU — libx265)",       False),
    ("h264_nvenc",  "H.264 (NVIDIA NVENC)",         True),
    ("hevc_nvenc",  "H.265 (NVIDIA NVENC)",         True),
    ("h264_amf",    "H.264 (AMD AMF)",              True),
    ("hevc_amf",    "H.265 (AMD AMF)",              True),
    ("h264_qsv",    "H.264 (Intel QuickSync)",      True),
    ("hevc_qsv",    "H.265 (Intel QuickSync)",      True),
]


def detect_encoders() -> list[tuple[str, str, bool]]:
    """Probe ffmpeg for available encoders. Returns [(id, label, is_hw), ...]."""
    available = []
    try:
        r = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout
        for enc_id, label, is_hw in KNOWN_ENCODERS:
            if re.search(r"\b" + re.escape(enc_id) + r"\b", out):
                available.append((enc_id, label, is_hw))
    except Exception:
        pass
    return available


def hw_encoders(available: list) -> list[tuple[str, str, bool]]:
    """Return only hardware-accelerated encoders from a list."""
    return [e for e in available if e[2]]


def best_encoder(available: list) -> str | None:
    """Pick the best available HW encoder: prefer HW H.264, then HW H.265.

    Returns encoder ID string or None if no hardware encoder available.
    """
    hw264 = [e for e in available if e[2] and "264" in e[0]]
    if hw264:
        return hw264[0][0]
    hw265 = [e for e in available if e[2] and ("265" in e[0] or "hevc" in e[0])]
    if hw265:
        return hw265[0][0]
    return None


def is_hw_encoder(enc_id: str) -> bool:
    """Check if an encoder ID is hardware-accelerated."""
    for eid, _, is_hw in KNOWN_ENCODERS:
        if eid == enc_id:
            return is_hw
    return False


def encoder_label(enc_id: str, available: list | None = None) -> str:
    """Return a friendly label for an encoder ID."""
    for eid, label, _ in (available or KNOWN_ENCODERS):
        if eid == enc_id:
            return label
    return enc_id or "Unknown encoder"


def enc_args(enc_id: str, quality: int = 19) -> list[str]:
    """Return ffmpeg encoder args for the given encoder.

    HW encoders use -cq, software encoders use -crf.
    """
    if enc_id in ("libx264", "libx265"):
        return ["-c:v", enc_id, "-crf", str(quality)]
    else:
        return ["-c:v", enc_id, "-cq", str(quality)]
