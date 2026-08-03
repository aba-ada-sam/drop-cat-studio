"""Copy finished video/audio outputs to a Desktop folder for easy review.

Andrew wants this permanent, not a one-off manual copy: every finished video
or standalone song lands in C:\\Users\\andre\\Desktop\\DCS_Review automatically
so he can just scroll thumbnails there, no per-session copying.
"""
import logging
import shutil
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_INBOX = Path(r"C:\Users\andre\Desktop\DCS_Review")
_MEDIA_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav"}

# job_manager runs every non-GPU job type (image2video, video_tools, retime,
# lipsync, video_bridges, ...) on its own independent thread, and each calls
# copy_to_inbox() on completion. The collision-avoidance loop below is
# check-then-act (while dst.exists(): ...) with no lock -- if two jobs from
# different threads finish close together and land on the same candidate
# destination name, both can pass the `exists()` check for that name before
# either has finished the (non-trivial, for video files) shutil.copy2(), and
# the second copy silently clobbers the first with no exception and nothing
# logged, defeating this module's whole point (a permanent, always-complete
# review folder).
_lock = threading.Lock()


def copy_to_inbox(path: str | None) -> None:
    """Copy *path* into the Inbox folder if it is a video/audio file that exists."""
    if not path:
        return
    src = Path(path)
    if src.suffix.lower() not in _MEDIA_EXTS or not src.exists():
        return
    try:
        with _lock:
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
