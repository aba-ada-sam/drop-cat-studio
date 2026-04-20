"""Port discovery + port file for Drop Cat Go Studio.

At startup the server picks the first free port in PORT_RANGE and writes
the chosen port to PORT_FILE. Launchers and the tray read that file so
they don't need to assume 7860 is available (it often isn't on a machine
that also runs Forge, WanGP, Gradio, etc.).

The file is gitignored and best-effort removed on graceful shutdown.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path

log = logging.getLogger("dropcat")

ROOT = Path(__file__).resolve().parent.parent
PORT_FILE = ROOT / ".dcs-port"

PORT_START = 7860
PORT_TRIES = 20  # 7860..7879


def _port_free(port: int) -> bool:
    """True if we can bind to 127.0.0.1:port right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_free_port(start: int = PORT_START, tries: int = PORT_TRIES) -> int:
    """Return the first free TCP port in [start, start+tries).

    Raises RuntimeError if the whole range is occupied — highly unlikely,
    but honest failure beats silently colliding with another app.
    """
    for offset in range(tries):
        port = start + offset
        if _port_free(port):
            return port
    raise RuntimeError(
        f"no free port in {start}..{start + tries - 1}. Close a program using these ports."
    )


def write_port_file(port: int) -> None:
    """Atomically write the chosen port + our PID for launchers/tray to read."""
    data = {"port": port, "pid": os.getpid()}
    tmp = PORT_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, PORT_FILE)
    except Exception as e:
        log.warning("port_lock: couldn't write %s (%s) — launchers may default to 7860", PORT_FILE, e)


def read_port_file() -> int | None:
    """Return the port written by the running server, or None if the file is
    missing or malformed. Callers should fall back to 7860."""
    try:
        raw = PORT_FILE.read_text(encoding="utf-8").strip()
        data = json.loads(raw)
        port = int(data.get("port") or 0)
        return port if 1 <= port <= 65535 else None
    except Exception:
        return None


def clear_port_file() -> None:
    """Best-effort removal on shutdown."""
    try:
        PORT_FILE.unlink(missing_ok=True)
    except Exception:
        pass
