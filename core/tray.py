"""
Drop Cat Go Studio — Windows system tray icon.

Features:
  - DCG icon in system tray (derived from the existing logo)
  - 2-second "running in tray" notification on startup
  - Left-click  → open http://127.0.0.1:7860 in browser
  - Right-click → menu: Open, Show Outputs, ── Exit
  - Exit kills the server cleanly
"""
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = ROOT_DIR / "static"
OUTPUT_DIR  = ROOT_DIR / "output"
ICO_PATH    = ROOT_DIR / "dropcat.ico"


def _app_url() -> str:
    """Resolve the URL dynamically because the server may have picked a
    non-default port (7860 was taken). Fall back to 7860 if the port file
    hasn't been written yet or is unreadable."""
    try:
        from core import port_lock
        port = port_lock.read_port_file() or 7860
    except Exception:
        port = 7860
    return f"http://127.0.0.1:{port}"

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


def _build_icon_image() -> "Image.Image":
    """Load dropcat.ico for the system tray (falls back to a DCG badge if missing)."""
    if ICO_PATH.is_file():
        try:
            img = Image.open(ICO_PATH)
            # Pick the 32×32 frame if the .ico has multiple sizes; else resize
            img = img.convert("RGBA")
            if img.size != (32, 32):
                img = img.resize((32, 32), Image.LANCZOS)
            return img
        except Exception:
            pass

    # Fallback: hand-drawn DCG badge
    size = 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill=(196, 30, 58, 255))
    gold = (212, 160, 23, 255)
    font = None
    for name in ("arialbd.ttf", "arial.ttf", "calibrib.ttf"):
        try:
            font = ImageFont.truetype(name, 12)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    draw.text((size // 2, size // 2), "DCG", fill=gold, font=font, anchor="mm")
    return img


def _open_app(icon=None, item=None):
    webbrowser.open(_app_url())


def _show_outputs(icon=None, item=None):
    if OUTPUT_DIR.exists():
        os.startfile(str(OUTPUT_DIR))
    else:
        webbrowser.open(_app_url())


def _exit_app(icon, item):
    """Stop the tray and kill the server process."""
    try:
        icon.stop()
    except Exception:
        pass
    # Give the icon a moment to clean up, then terminate
    threading.Timer(0.3, lambda: os._exit(0)).start()


def start_tray() -> None:
    """
    Start the system tray icon in a background daemon thread.
    Safe to call even if pystray is not installed — silently skips.
    """
    if not _AVAILABLE:
        return

    def _run():
        try:
            icon_image = _build_icon_image()

            menu = pystray.Menu(
                pystray.MenuItem(
                    "Open Drop Cat Go Studio",
                    _open_app,
                    default=True,          # left-click action
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Show Outputs Folder", _show_outputs),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", _exit_app),
            )

            icon = pystray.Icon(
                name="DropCatGoStudio",
                icon=icon_image,
                title="Drop Cat Go Studio",
                menu=menu,
            )

            # Show a 2-second startup notification once the icon is running
            def _notify_after_start():
                import time
                time.sleep(1.5)          # let the icon fully initialise first
                try:
                    icon.notify(
                        "Drop Cat Go Studio is running.\nLeft-click to open · Right-click for options.",
                        "Drop Cat Go Studio"
                    )
                except Exception:
                    pass                 # notifications optional

            threading.Thread(target=_notify_after_start, daemon=True).start()

            icon.run()                   # blocks this thread (tray event loop)
        except Exception as exc:
            # Tray is nice-to-have; never crash the server over it
            import logging
            logging.getLogger("dropcat").warning("System tray unavailable: %s", exc)

    t = threading.Thread(target=_run, daemon=True, name="tray-icon")
    t.start()
