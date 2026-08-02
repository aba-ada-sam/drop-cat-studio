"""Copy finished video/audio outputs to a Desktop folder for easy review.

Andrew wants this permanent, not a one-off manual copy: every finished video
or standalone song lands in C:\\Users\\andre\\Desktop\\DCS_Review automatically
so he can just scroll thumbnails there, no per-session copying.
"""
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_INBOX = Path(r"C:\Users\andre\Desktop\DCS_Review")
_MEDIA_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav"}


def copy_to_inbox(path: str | None) -> None:
    """Copy *path* into the Inbox folder if it is a video/audio file that exists."""
    if not path:
        return
    src = Path(path)
    if src.suffix.lower() not in _MEDIA_EXTS or not src.exists():
        return
    try:
        _INBOX.mkdir(parents=True, exist_ok=True)
        dst = _INBOX / src.name
        if dst.exists() and dst.resolve() == src.resolve():
            return
        counter = 1
        while dst.exists():
            dst = _INBOX / f"{src.stem}_{counter}{src.suffix}"
            counter += 1
        shutil.copy2(src, dst)
        log.info("[inbox] %s -> Inbox/%s", src.name, dst.name)
    except Exception as e:
        log.warning("[inbox] Could not copy to Inbox: %s", e)
