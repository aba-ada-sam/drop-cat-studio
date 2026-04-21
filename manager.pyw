"""
manager.pyw — Drop Cat Go Studio tray + window manager
Run with:  pythonw.exe manager.pyw

- Shows tkinter loading splash instantly while server starts
- Opens app in Chrome --app mode (plain window, no URL bar or tabs)
- Single-instance mutex: double-click while running re-opens the window
- Tray: Open / Restart Server / Exit
- Keeps app.py alive, restarts on crash (max 5 in 60s)

Dependencies: pystray, Pillow  (pip install pystray Pillow)
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).resolve().parent
APP_PY     = ROOT / "app.py"
ICO_PATH   = ROOT / "dropcat.ico"
PORT_FILE  = ROOT / ".dcs-port"
LOG_DIR    = ROOT / "logs"
SERVER_LOG = LOG_DIR / "server.log"

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=str(SERVER_LOG),
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s [manager] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("manager")

PORT_START = 7860
PORT_TRIES = 20


# ── Python interpreter ────────────────────────────────────────────────────────

def _python_exe() -> str:
    exe = Path(sys.executable)
    if exe.stem.lower() == "pythonw":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    if exe.is_file():
        return str(exe)
    return r"C:\Users\andre\AppData\Local\Programs\Python\Python310\python.exe"


PYTHON = _python_exe()


# ── Port helpers ──────────────────────────────────────────────────────────────

def read_port_file() -> tuple[int | None, int | None]:
    try:
        data = json.loads(PORT_FILE.read_text(encoding="utf-8"))
        return int(data.get("port") or 0) or None, int(data.get("pid") or 0) or None
    except Exception:
        return None, None


def clear_port_file() -> None:
    try:
        PORT_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def server_responds(port: int, timeout: float = 1.5) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/system", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def find_running_server() -> int | None:
    port, _ = read_port_file()
    if port and server_responds(port):
        return port
    for offset in range(PORT_TRIES):
        p = PORT_START + offset
        if server_responds(p):
            return p
    return None


def kill_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        log.info("Killed PID %d", pid)
    except Exception as exc:
        log.warning("taskkill %d failed: %s", pid, exc)


# ── Open app window ───────────────────────────────────────────────────────────

def open_app_window(port: int) -> None:
    """Open the app in Chrome --app mode: plain window, no URL bar or tabs."""
    url = f"http://127.0.0.1:{port}"
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in chrome_paths:
        if os.path.isfile(path):
            subprocess.Popen([
                path,
                f"--app={url}",
                "--window-size=1400,900",
                "--window-position=100,50",
            ])
            log.info("Opened Chrome --app on port %d", port)
            return
    # Chrome not found — fall back to default browser
    import webbrowser
    webbrowser.open(url)
    log.warning("Chrome not found, opened default browser")


# ── Server process manager ────────────────────────────────────────────────────

class ServerManager:
    MAX_RESTARTS   = 5
    RESTART_WINDOW = 60
    RESTART_DELAY  = 3
    READY_TIMEOUT  = 120

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._port: int | None = None
        self._restart_times: list[float] = []
        self._gave_up = False

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def ready(self) -> bool:
        return self._ready_event.is_set()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready_event.wait(timeout)

    def start(self) -> None:
        self._stop_event.clear()
        self._spawn()
        threading.Thread(target=self._watch, daemon=True, name="srv-watch").start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._kill_current()

    def restart(self) -> None:
        log.info("Manual restart requested")
        self._ready_event.clear()
        self._port = None
        with self._lock:
            self._kill_current()

    def _kill_current(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            log.info("Terminating server PID %d", proc.pid)
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:
                log.warning("Error terminating server: %s", exc)
        self._proc = None

    def _spawn(self) -> None:
        _, old_pid = read_port_file()
        if old_pid:
            kill_pid(old_pid)
            time.sleep(0.5)
        clear_port_file()

        log_fh = open(SERVER_LOG, "a", encoding="utf-8", errors="replace")
        cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        try:
            proc = subprocess.Popen(
                [PYTHON, str(APP_PY)],
                cwd=str(ROOT),
                stdout=log_fh,
                stderr=log_fh,
                creationflags=cflags,
            )
        except Exception as exc:
            log.error("Failed to spawn app.py: %s", exc)
            log_fh.close()
            return

        with self._lock:
            self._proc = proc
        log.info("Spawned app.py as PID %d", proc.pid)
        threading.Thread(target=self._wait_for_ready, args=(proc.pid,),
                         daemon=True, name="srv-ready").start()

    def _wait_for_ready(self, expected_pid: int) -> None:
        deadline = time.monotonic() + self.READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return
            port, pid = read_port_file()
            if port and pid == expected_pid and server_responds(port):
                self._port = port
                self._ready_event.set()
                log.info("Server ready on port %d", port)
                return
            for offset in range(PORT_TRIES):
                p = PORT_START + offset
                if server_responds(p, timeout=0.5):
                    self._port = p
                    self._ready_event.set()
                    log.info("Server ready on port %d (sweep)", p)
                    return
            time.sleep(2)
        log.warning("Server did not become ready within %ds", self.READY_TIMEOUT)

    def _watch(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                proc = self._proc
            if proc is None:
                if not self._stop_event.is_set() and not self._gave_up:
                    time.sleep(self.RESTART_DELAY)
                    self._spawn()
                time.sleep(1)
                continue
            ret = proc.poll()
            if ret is None:
                time.sleep(2)
                continue
            if self._stop_event.is_set():
                log.info("Server exited (manager stopping)")
                return
            log.warning("Server exited with code %s — considering restart", ret)
            self._ready_event.clear()
            self._port = None
            now = time.monotonic()
            self._restart_times = [t for t in self._restart_times if now - t < self.RESTART_WINDOW]
            if len(self._restart_times) >= self.MAX_RESTARTS:
                log.error("Server crashed %d times in %ds — giving up.",
                          self.MAX_RESTARTS, self.RESTART_WINDOW)
                self._gave_up = True
                return
            self._restart_times.append(now)
            log.info("Restarting server in %ds (attempt %d/%d)…",
                     self.RESTART_DELAY, len(self._restart_times), self.MAX_RESTARTS)
            with self._lock:
                self._proc = None
            time.sleep(self.RESTART_DELAY)
            self._spawn()


# ── Loading splash (tkinter) ──────────────────────────────────────────────────

def show_splash(srv: ServerManager) -> None:
    """Frameless loading window that closes when the server is ready."""
    try:
        import tkinter as tk
    except ImportError:
        srv.wait_ready(timeout=120)
        return

    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg="#0d0606")
    root.attributes("-topmost", True)

    W, H = 320, 180
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    tk.Label(root, text="DROP CAT GO", bg="#0d0606", fg="#d4a017",
             font=("Arial Black", 20, "bold")).pack(pady=(28, 2))
    tk.Label(root, text="S T U D I O", bg="#0d0606", fg="#8a7a6a",
             font=("Arial", 8)).pack()

    status = tk.StringVar(value="Starting…")
    tk.Label(root, textvariable=status, bg="#0d0606", fg="#6a5a4a",
             font=("Arial", 9)).pack(pady=(16, 0))

    dot_var = tk.StringVar(value="●○○○")
    tk.Label(root, textvariable=dot_var, bg="#0d0606", fg="#c41e3a",
             font=("Arial", 13)).pack(pady=4)

    dots = ["●○○○", "○●○○", "○○●○", "○○○●"]
    idx = [0]

    def _tick():
        idx[0] = (idx[0] + 1) % 4
        dot_var.set(dots[idx[0]])
        if idx[0] > 6:
            status.set("Loading model…")
        root.after(260, _tick)

    def _poll():
        if srv.ready:
            root.destroy()
        else:
            root.after(400, _poll)

    _tick()
    root.after(400, _poll)
    root.mainloop()


# ── Tray icon ─────────────────────────────────────────────────────────────────

def _build_icon_image():
    from PIL import Image, ImageDraw, ImageFont
    if ICO_PATH.is_file():
        try:
            img = Image.open(ICO_PATH).convert("RGBA")
            if img.size != (32, 32):
                img = img.resize((32, 32), Image.LANCZOS)
            return img
        except Exception:
            pass
    size = 32
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill=(196, 30, 58, 255))
    font = None
    for name in ("arialbd.ttf", "arial.ttf", "calibrib.ttf"):
        try:
            font = ImageFont.truetype(name, 12); break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    draw.text((size // 2, size // 2), "DCG", fill=(212, 160, 23, 255), font=font, anchor="mm")
    return img


def run_tray(srv: ServerManager) -> None:
    import pystray

    def _open(icon=None, item=None):
        if srv.port:
            open_app_window(srv.port)

    def _do_restart():
        srv.restart()
        srv.wait_ready(120)
        if srv.port:
            open_app_window(srv.port)

    def _restart(icon=None, item=None):
        threading.Thread(target=_do_restart, daemon=True).start()

    def _exit(icon, item=None):
        log.info("Exit from tray")
        srv.stop()
        clear_port_file()
        try:
            icon.stop()
        except Exception:
            pass
        threading.Timer(0.5, lambda: os._exit(0)).start()

    menu = pystray.Menu(
        pystray.MenuItem("Open Drop Cat Go Studio", _open, default=True),
        pystray.MenuItem("Restart Server", _restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", _exit),
    )
    icon = pystray.Icon("DropCatGoStudio", _build_icon_image(), "Drop Cat Go Studio", menu)
    icon.run()   # blocks until Exit is clicked


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== manager.pyw starting (PID %d) ===", os.getpid())

    # Single-instance lock via a bound socket — the OS releases it the instant
    # this process dies, so there is no way for a stale lock to persist.
    import socket as _socket
    _lock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _lock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
    try:
        _lock.bind(("127.0.0.1", 17860))
    except OSError:
        log.info("Already running — opening app window")
        _lock.close()
        existing = find_running_server()
        if existing:
            open_app_window(existing)
        sys.exit(0)

    try:
        import pystray
        from PIL import Image
    except ImportError as exc:
        log.error("Missing: %s — run: pip install pystray Pillow", exc)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Cannot start:\n\n{exc}\n\nRun: pip install -r requirements.txt",
                "Drop Cat Go Studio", 0x10,
            )
        except Exception:
            pass
        sys.exit(1)

    srv = ServerManager()
    existing_port = find_running_server()

    if existing_port:
        log.info("Server already on port %d", existing_port)
        srv._port = existing_port
        srv._ready_event.set()
        open_app_window(existing_port)
    else:
        log.info("Starting app.py")
        srv.start()
        # Show splash while server loads, then open the window
        show_splash(srv)
        if srv.port:
            open_app_window(srv.port)
        else:
            log.error("Server never became ready")

    # Keep process alive for the tray icon
    run_tray(srv)


if __name__ == "__main__":
    main()
