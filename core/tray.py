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

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
OUTPUT_DIR  = Path(__file__).resolve().parent.parent / "output"
APP_URL     = "http://127.0.0.1:7860"

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


def _build_icon_image() -> "Image.Image":
    """
    Use the existing logo-192.png if it exists; otherwise draw a
    crisp 64×64 'DCG' badge in the app's circus colours.
    """
    logo = STATIC_DIR / "logo-192.png"
    if logo.is_file():
        try:
            img = Image.open(logo).convert("RGBA").resize((64, 64), Image.LANCZOS)
            return img
        except Exception:
            pass

    # Fallback: hand-drawn badge
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background disc — circus crimson
    draw.ellipse([0, 0, size - 1, size - 1], fill=(196, 30, 58, 255))

    # Gold text "DCG"
    gold = (212, 160, 23, 255)
    font = None
    for name in ("arialbd.ttf", "arial.ttf", "calibrib.ttf"):
        try:
            font = ImageFont.truetype(name, 20)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    draw.text((size // 2, size // 2), "DCG", fill=gold, font=font, anchor="mm")
    return img


def _open_app(icon=None, item=None):
    webbrowser.open(APP_URL)


def _show_outputs(icon=None, item=None):
    if OUTPUT_DIR.exists():
        os.startfile(str(OUTPUT_DIR))
    else:
        webbrowser.open(APP_URL)


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
