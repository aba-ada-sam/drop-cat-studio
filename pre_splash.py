"""pre_splash.py — Immediate startup indicator for Drop Cat Go Studio.

Launched by launch.bat before anything else (git pull, Python server start).
Shows a branded borderless window in under a second so Andrew knows his
double-click registered. Closes automatically when .dcs-port is written.
"""
import json
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

ROOT      = Path(__file__).resolve().parent
PORT_FILE = ROOT / ".dcs-port"

BG      = "#0d0606"
GOLD    = "#d4a017"
CRIMSON = "#c41e3a"
CREAM   = "#f0e6d0"
DIM     = "#7a6a4a"
W, H    = 360, 210


def _poll(root: tk.Tk, status: tk.StringVar) -> None:
    """Background thread: watch for .dcs-port then close the window."""
    start = time.monotonic()
    while time.monotonic() - start < 120:
        if PORT_FILE.exists():
            try:
                data = json.loads(PORT_FILE.read_text(encoding="utf-8"))
                if data.get("port"):
                    root.after(0, lambda: status.set("Opening browser…"))
                    time.sleep(0.7)
                    root.after(0, root.destroy)
                    return
            except Exception:
                pass
        time.sleep(0.4)
    root.after(0, root.destroy)


def main() -> None:
    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg=BG)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.97)

    # Center on primary monitor
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

    # Gold top accent bar
    tk.Frame(root, bg=GOLD, height=3).pack(fill=tk.X, side=tk.TOP)

    # Title block
    tk.Label(root, text="Drop Cat Go Studio",
             bg=BG, fg=GOLD, font=("Segoe UI", 19, "bold")).pack(pady=(22, 2))
    tk.Label(root, text="Andrew’s AI Video Production",
             bg=BG, fg=CREAM, font=("Segoe UI", 10)).pack()

    # Spinner canvas
    canvas = tk.Canvas(root, width=44, height=44, bg=BG, highlightthickness=0)
    canvas.pack(pady=(18, 6))
    arc = canvas.create_arc(4, 4, 40, 40, start=0, extent=260,
                             outline=GOLD, width=3, style=tk.ARC)
    _angle = [0]

    def _spin() -> None:
        _angle[0] = (_angle[0] + 9) % 360
        canvas.itemconfig(arc, start=_angle[0])
        root.after(28, _spin)

    _spin()

    # Status line
    status = tk.StringVar(value="Starting up…")
    tk.Label(root, textvariable=status,
             bg=BG, fg=DIM, font=("Segoe UI", 9)).pack()

    # Crimson bottom accent bar
    tk.Frame(root, bg=CRIMSON, height=2).pack(fill=tk.X, side=tk.BOTTOM)

    threading.Thread(target=_poll, args=(root, status), daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    main()
