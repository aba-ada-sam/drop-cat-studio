"""Project/session state management — tracks files flowing between features.

A session is a working context where uploads and outputs are shared.
When Fun Videos generates a clip, it auto-appears in Bridges' input picker.
Sessions persist to projects/{session_id}/session.json.
"""
import json
import logging
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"


class FileEntry:
    """A file registered in the session (upload or output)."""

    def __init__(self, filename: str, kind: str, source: str, **meta):
        self.filename = filename
        self.kind = kind          # "image", "video", "audio", "prompt"
        self.source = source      # feature that created it: "upload", "fun_videos", "bridges", etc.
        self.added_at = time.time()
        self.meta = meta          # duration, width, height, etc.

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "kind": self.kind,
            "source": self.source,
            "added_at": self.added_at,
            **self.meta,
        }


class Session:
    """Tracks files and outputs across a working session."""

    def __init__(self, session_id: str | None = None):
        self.id = session_id or uuid.uuid4().hex[:10]
        self.created_at = time.time()
        self.files: dict[str, FileEntry] = {}
        self._dir = PROJECTS_DIR / self.id
        # BUG-10: create the directory eagerly in __init__, not lazily on every
        # .dir access. The property is now a cheap read-only accessor.
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        # BUG-10: no side effects — just return the path.
        return self._dir

    def add_file(self, filename: str, kind: str, source: str, **meta):
        """Register a file (upload or output)."""
        self.files[filename] = FileEntry(filename, kind, source, **meta)
        self._save()

    def remove_file(self, filename: str):
        self.files.pop(filename, None)
        self._save()

    def get_videos(self) -> list[dict]:
        """Get all video files (for Bridges input picker)."""
        return [
            f.to_dict() for f in self.files.values()
            if f.kind == "video"
        ]

    def get_images(self) -> list[dict]:
        """Get all image files (for Fun Videos, SD Prompts)."""
        return [
            f.to_dict() for f in self.files.values()
            if f.kind == "image"
        ]

    def get_all(self) -> list[dict]:
        """Get all files sorted by add time."""
        items = [f.to_dict() for f in self.files.values()]
        items.sort(key=lambda f: f["added_at"], reverse=True)
        return items

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "file_count": len(self.files),
            "files": self.get_all(),
        }

    def _save(self):
        """Persist to disk."""
        data = {
            "id": self.id,
            "created_at": self.created_at,
            "files": {k: v.to_dict() for k, v in self.files.items()},
        }
        path = self.dir / "session.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, session_id: str) -> "Session | None":
        """Load a session from disk."""
        path = PROJECTS_DIR / session_id / "session.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session = cls(data["id"])
            session.created_at = data.get("created_at", time.time())
            for fname, fdata in data.get("files", {}).items():
                session.files[fname] = FileEntry(
                    filename=fdata["filename"],
                    kind=fdata["kind"],
                    source=fdata["source"],
                    **{k: v for k, v in fdata.items()
                       if k not in ("filename", "kind", "source", "added_at")},
                )
                session.files[fname].added_at = fdata.get("added_at", 0)
            return session
        except Exception as e:
            log.warning("Failed to load session %s: %s", session_id, e)
            return None


# ── Session manager (singleton) ──────────────────────────────────────────────
# BUG-09: initialize eagerly at module load to eliminate the check-then-set
# race condition under asyncio concurrency.
_current_session: Session = Session()


def get_current() -> Session:
    """Get the current session."""
    return _current_session


def set_current(session_id: str) -> Session | None:
    """Switch to an existing session."""
    global _current_session
    session = Session.load(session_id)
    if session:
        _current_session = session
    return session


def list_sessions() -> list[dict]:
    """List all saved sessions."""
    sessions = []
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir(), reverse=True):
            if d.is_dir() and (d / "session.json").exists():
                session = Session.load(d.name)
                if session:
                    sessions.append({
                        "id": session.id,
                        "created_at": session.created_at,
                        "file_count": len(session.files),
                    })
    return sessions[:20]


def new_session() -> Session:
    """Create and switch to a new session."""
    global _current_session
    _current_session = Session()
    return _current_session
