"""Thread-safe ring-buffer log with sequence numbers for efficient polling.

Captures Python logging output and stdout prints into a shared buffer.
Frontend polls with a sequence number to get only new entries.
"""
import io
import logging
import sys
import threading
import time
from collections import deque

MAX_ENTRIES = 500

_lock = threading.Lock()
_buffer: deque[dict] = deque(maxlen=MAX_ENTRIES)
_seq = 0


def _now_str() -> str:
    return time.strftime("%H:%M:%S")


def append(level: str, msg: str):
    """Add an entry to the log buffer."""
    global _seq
    with _lock:
        _seq += 1
        _buffer.append({
            "seq": _seq,
            "time": _now_str(),
            "level": level,
            "msg": msg,
        })


def get_since(since_seq: int = 0) -> list[dict]:
    """Return log entries with seq > since_seq."""
    with _lock:
        return [e for e in _buffer if e["seq"] > since_seq]


def get_recent(n: int = 150) -> list[dict]:
    """Return the last n entries."""
    with _lock:
        items = list(_buffer)
        return items[-n:]


def current_seq() -> int:
    """Return the current sequence number."""
    with _lock:
        return _seq


# ── Logging handler ──────────────────────────────────────────────────────────

class BufferHandler(logging.Handler):
    """Python logging handler that writes to the shared ring buffer."""

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            level = record.levelname.lower()
            # Normalize to frontend-friendly levels
            if level == "warning":
                level = "warn"
            elif level not in ("info", "error", "debug", "warn", "success"):
                level = "info"
            append(level, msg)
        except Exception:
            pass


def install_handler(logger_name: str | None = None, level: int = logging.DEBUG):
    """Install the buffer handler on a logger (default: root logger)."""
    handler = BufferHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    return handler


# ── Stdout/stderr capture ────────────────────────────────────────────────────

class StdoutTee(io.TextIOBase):
    """Tee stdout writes to both the original stream and the log buffer.

    Captures print() calls from service subprocesses and library code.
    """

    def __init__(self, original, level: str = "info"):
        self._original = original
        self._level = level
        self._partial = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._original.write(s)
        # Buffer partial lines
        self._partial += s
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            line = line.rstrip()
            if line:
                # Detect level from prefix: [error], [warning], [success], etc.
                lvl = self._level
                for tag in ("error", "warning", "warn", "success", "info", "debug"):
                    if line.lower().startswith(f"[{tag}]"):
                        lvl = tag if tag != "warning" else "warn"
                        break
                append(lvl, line)
        return len(s)

    def flush(self):
        self._original.flush()

    def fileno(self):
        return self._original.fileno()

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")


def capture_stdout():
    """Replace sys.stdout and sys.stderr with tees to the log buffer.

    FLW-09: stderr is captured at 'warn' (not 'error') because most libraries
    write informational/warning messages there; flagging them all as errors
    floods the GUI log panel with false-positive red entries.
    StdoutTee.write() already upgrades individual lines that start with [error].
    """
    if not isinstance(sys.stdout, StdoutTee):
        sys.stdout = StdoutTee(sys.stdout, "info")
    if not isinstance(sys.stderr, StdoutTee):
        sys.stderr = StdoutTee(sys.stderr, "warn")
