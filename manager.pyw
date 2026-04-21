"""
manager.pyw — Drop Cat Go Studio tray manager
Run with:  pythonw.exe manager.pyw
No console window. Keeps app.py alive, shows tray icon, opens Chrome.

Dependencies: pystray, Pillow (PIL) — both in requirements.txt
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
import webbrowser
from pathlib import Path
from urllib.request import urlopen

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT        = Path(__file__).resolve().parent
APP_PY      = ROOT / "app.py"
ICO_PATH    = ROOT / "dropcat.ico"
PORT_FILE   = ROOT / ".dcs-port"
LOG_DIR     = ROOT / "logs"
SERVER_LOG  = LOG_DIR / "server.log"

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=str(SERVER_LOG),
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s [manager] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("manager")

# ── Port constants ─────────────────────────────────────────────────────────────

PORT_START = 7860
PORT_TRIES = 20   # 7860..7879


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

def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def read_port_file() -> tuple[int | None, int | None]:
    try:
        data = json.loads(PORT_FILE.read_text(encoding="utf-8"))
        port = int(data.get("port") or 0) or None
        pid  = int(data.get("pid")  or 0) or None
        return port, pid
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
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        log.info("Killed PID %d", pid)
    except Exception as exc:
        log.warning("taskkill %d failed: %s", pid, exc)


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
        threading.Thread(
            target=self._wait_for_ready,
            args=(proc.pid,),
            daemon=True,
            name="srv-ready",
        ).start()

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
            # Fallback sweep
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
                log.error("Server crashed %d times in %ds — giving up.", self.MAX_RESTARTS, self.RESTART_WINDOW)
                self._gave_up = True
                return

            self._restart_times.append(now)
            log.info("Restarting server in %ds (attempt %d/%d)…",
                     self.RESTART_DELAY, len(self._restart_times), self.MAX_RESTARTS)
            with self._lock:
                self._proc = None

            time.sleep(self.RESTART_DELAY)
            self._spawn()




# ── Tkinter loading splash ───────────────────────────────────────────────────

def show_loading_splash(srv: "ServerManager") -> None:
    """Show a frameless loading window until the server is ready, then open Chrome."""
    try:
        import tkinter as tk
    except ImportError:
        # tkinter not available — fall back to browser loading page
        loading = ROOT / "loading.html"
        webbrowser.open(loading.as_uri())
        srv.wait_ready(timeout=120)
        if srv.port:
            webbrowser.open(f"http://127.0.0.1:{srv.port}")
        return

    root = tk.Tk()
    root.overrideredirect(True)          # no title bar
    root.configure(bg="#0d0606")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    W, H = 340, 200
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    tk.Label(root, text="DROP CAT GO", bg="#0d0606", fg="#d4a017",
             font=("Arial Black", 22, "bold")).pack(pady=(32, 2))
    tk.Label(root, text="S T U D I O", bg="#0d0606", fg="#8a7a6a",
             font=("Arial", 9)).pack()

    status = tk.StringVar(value="Starting server…")
    tk.Label(root, textvariable=status, bg="#0d0606", fg="#6a5a4a",
             font=("Arial", 9)).pack(pady=(20, 0))

    # Dot spinner
    dot_var = tk.StringVar(value="")
    tk.Label(root, textvariable=dot_var, bg="#0d0606", fg="#c41e3a",
             font=("Arial", 14)).pack(pady=6)

    dots = ["●○○○", "○●○○", "○○●○", "○○○●"]
    _dot_idx = [0]

    def _tick():
        _dot_idx[0] = (_dot_idx[0] + 1) % len(dots)
        dot_var.set(dots[_dot_idx[0]])
        root.after(250, _tick)

    _tick()

    def _poll():
        if srv.ready:
            status.set("Ready — opening app…")
            root.after(400, _open_and_close)
        else:
            if threading.active_count() > 2:
                status.set("Loading model…" if _dot_idx[0] > 8 else "Starting server…")
            root.after(500, _poll)

    def _open_and_close():
        port = srv.port or PORT_START
        webbrowser.open(f"http://127.0.0.1:{port}")
        root.destroy()

    root.after(500, _poll)
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


def run_tray(srv: ServerManager, existing_port: int | None) -> None:
    import pystray

    icon_ref: list = []

    def _app_url() -> str:
        return f"http://127.0.0.1:{srv.port or existing_port or PORT_START}"

    def _open(icon=None, item=None) -> None:
        webbrowser.open(_app_url())

    def _restart(icon=None, item=None) -> None:
        threading.Thread(target=_do_restart, daemon=True).start()

    def _do_restart() -> None:
        if not srv._stop_event.is_set():
            srv.restart()
        else:
            srv._gave_up = False
            srv._restart_times.clear()
            srv._stop_event.clear()
            srv.start()
        srv.wait_ready(timeout=120)

    def _exit(icon, item=None) -> None:
        log.info("Exit — stopping server and tray")
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

    icon = pystray.Icon(
        name="DropCatGoStudio",
        icon=_build_icon_image(),
        title="Drop Cat Go Studio",
        menu=menu,
    )
    icon_ref.append(icon)

    # Run tray in background so tkinter splash can own the main thread
    threading.Thread(target=icon.run, daemon=True, name="tray").start()

    if not existing_port:
        show_loading_splash(srv)  # blocks until server ready, then opens Chrome
    # (if existing_port, Chrome was already opened by the VBS / mutex branch)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== manager.pyw starting (PID %d) ===", os.getpid())

    # Single-instance lock — if another manager is already running, just open
    # the browser and exit rather than spawning a second tray + server.
    import ctypes as _ctypes
    _mutex = _ctypes.windll.kernel32.CreateMutexW(None, False, "DropCatGoStudio_Manager_v1")
    if _ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log.info("Another manager instance is running — opening browser and exiting")
        existing = find_running_server()
        if existing:
            webbrowser.open(f"http://127.0.0.1:{existing}")
        sys.exit(0)

    try:
        import pystray
        from PIL import Image
    except ImportError as exc:
        log.error("Missing package: %s — run: pip install pystray Pillow", exc)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Drop Cat Go Studio cannot start:\n\n{exc}\n\nRun: pip install -r requirements.txt",
                "Drop Cat Go Studio",
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)

    srv = ServerManager()
    existing_port = find_running_server()

    if existing_port:
        log.info("Server already on port %d — attaching tray only", existing_port)
        srv._port = existing_port
        srv._ready_event.set()
    else:
        log.info("No server found — starting app.py")
        srv.start()

    try:
        run_tray(srv, existing_port)
        # run_tray returns after the splash closes; keep process alive for the tray
        while True:
            time.sleep(60)
    except Exception as exc:
        log.error("Tray crashed: %s", exc, exc_info=True)
        srv.stop()


if __name__ == "__main__":
    main()
