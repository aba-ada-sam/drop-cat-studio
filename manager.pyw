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
import shutil
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
_MUTEX_HANDLE = None  # held at module level so GC never releases it


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


def server_responds(port: int, timeout: float = 0.4) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/system", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def find_running_server() -> int | None:
    """Check only the port recorded in .dcs-port — no blind scan."""
    port, _ = read_port_file()
    if port and server_responds(port):
        return port
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


# ── Auto-update (git pull + pip install) ─────────────────────────────────────

def _do_git_pull(on_status=None) -> None:
    """Pull latest code; pip-install deps only if the commit changed. Non-fatal."""
    def _status(msg):
        if on_status:
            on_status(msg)

    if not shutil.which("git"):
        log.info("git not on PATH — skipping update check")
        return

    _status("Checking for updates…")
    try:
        sha_before = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        sha_before = ""

    try:
        subprocess.run(
            ["git", "-C", str(ROOT), "pull", "--ff-only", "origin", "master"],
            capture_output=True, timeout=30,
        )
    except Exception as exc:
        log.warning("git pull skipped: %s", exc)
        return

    try:
        sha_after = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        sha_after = sha_before

    if sha_before and sha_after and sha_before != sha_after:
        log.info("New code pulled (%s → %s); updating dependencies", sha_before[:7], sha_after[:7])
        _status("Updating dependencies…")
        try:
            req = ROOT / "requirements.txt"
            if req.exists():
                subprocess.run(
                    ["pip", "install", "-q", "-r", str(req)],
                    capture_output=True, timeout=180,
                )
        except Exception as exc:
            log.warning("pip install failed (non-fatal): %s", exc)
    else:
        log.info("Already up to date")


# ── Desktop shortcut self-update ──────────────────────────────────────────────

def _ensure_shortcut() -> None:
    """Point the desktop shortcut at manager.pyw (pythonw.exe) instead of launch.bat."""
    try:
        desktop_lnk = Path(os.environ["USERPROFILE"]) / "Desktop" / "Drop Cat Go Studio.lnk"
        pythonw = Path(sys.executable)
        if pythonw.stem.lower() != "pythonw":
            candidate = pythonw.with_name("pythonw.exe")
            pythonw = candidate if candidate.is_file() else pythonw
        mgr = ROOT / "manager.pyw"
        ico = ROOT / "static" / "favicon.ico"
        ico_str = f"{ico},0" if ico.exists() else ""
        ps = (
            f"$ws=New-Object -ComObject WScript.Shell;"
            f"$sc=$ws.CreateShortcut('{desktop_lnk}');"
            f"$sc.TargetPath='{pythonw}';"
            f"$sc.Arguments='\"\"\"' + '{mgr}' + '\"\"\"';"
            f"$sc.WorkingDirectory='{ROOT}';"
            f"$sc.IconLocation='{ico_str}';"
            f"$sc.Description='Drop Cat Go Studio';"
            f"$sc.Save()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=15,
        )
        log.info("Desktop shortcut updated → manager.pyw")
    except Exception as exc:
        log.warning("_ensure_shortcut failed (non-fatal): %s", exc)


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
            env = os.environ.copy()
            env["DCS_MANAGED"] = "1"   # tells app.py to skip its own tray icon
            proc = subprocess.Popen(
                [PYTHON, str(APP_PY)],
                cwd=str(ROOT),
                stdout=log_fh,
                stderr=log_fh,
                creationflags=cflags,
                env=env,
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


# ── Opening splash (already-running path) ─────────────────────────────────────

def _show_opening_splash() -> None:
    """Show 'Opening...' window while finding + opening the existing server.
    Gives immediate visual feedback so the user doesn't retry clicking."""
    try:
        import tkinter as tk
    except ImportError:
        existing = find_running_server()
        if existing:
            open_app_window(existing)
        return

    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg="#0d0606")
    root.attributes("-topmost", True)

    W, H = 320, 140
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    tk.Label(root, text="DROP CAT GO", bg="#0d0606", fg="#d4a017",
             font=("Arial Black", 20, "bold")).pack(pady=(28, 2))
    tk.Label(root, text="S T U D I O", bg="#0d0606", fg="#8a7a6a",
             font=("Arial", 8)).pack()
    tk.Label(root, text="Opening…", bg="#0d0606", fg="#6a5a4a",
             font=("Arial", 9)).pack(pady=(14, 0))

    _done = threading.Event()

    def _bg():
        existing = find_running_server()
        if existing:
            open_app_window(existing)
        _done.set()

    threading.Thread(target=_bg, daemon=True).start()

    def _poll():
        if _done.is_set():
            root.after(500, root.destroy)
        else:
            root.after(200, _poll)

    root.after(100, _poll)
    root.after(8000, root.destroy)  # failsafe
    root.mainloop()


# ── Loading splash (tkinter) ──────────────────────────────────────────────────

def show_splash(srv: ServerManager) -> None:
    """Frameless loading window: pulls updates → starts server → closes.

    Drives the full startup sequence in a background thread so the window
    appears immediately. srv.start() is called from here, not from main().
    """
    try:
        import tkinter as tk
    except ImportError:
        _do_git_pull()
        srv.start()
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

    status = tk.StringVar(value="Checking for updates…")
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
        root.after(260, _tick)

    def _bg():
        _do_git_pull(on_status=lambda s: root.after(0, lambda: status.set(s)))
        root.after(0, lambda: status.set("Starting server…"))
        srv.start()
        while not srv.ready and not srv._gave_up:
            time.sleep(0.4)
        root.after(0, root.destroy)

    _tick()
    threading.Thread(target=_bg, daemon=True).start()
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
        # Kill GPU subprocesses before the server stops so they don't linger.
        try:
            from services import manager as svc
            for svc_name in ("wangp", "acestep"):
                try:
                    svc.stop_service(svc_name)
                except Exception:
                    pass
        except Exception:
            pass
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

    # Single-instance: Windows named mutex held for the lifetime of the process.
    # _MUTEX_HANDLE is module-level so Python's GC never releases it.
    global _MUTEX_HANDLE
    import ctypes as _ct
    _k32 = _ct.WinDLL("kernel32", use_last_error=True)
    _MUTEX_HANDLE = _k32.CreateMutexW(None, True, "Local\\DropCatGoStudio_Manager_v2")
    if _ct.get_last_error() == 183:  # ERROR_ALREADY_EXISTS — another manager owns it
        log.info("Already running — showing opening splash then exiting")
        _show_opening_splash()
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

    # Keep the desktop shortcut pointing at manager.pyw (transition from launch.bat)
    _ensure_shortcut()

    srv = ServerManager()

    # Fast check: is a server already recorded in .dcs-port and alive?
    existing_port = find_running_server()

    if existing_port:
        log.info("Server already on port %d", existing_port)
        srv._port = existing_port
        srv._ready_event.set()
        open_app_window(existing_port)
    else:
        # show_splash drives the full startup: git pull → srv.start() → wait ready
        log.info("Starting fresh — splash will handle git pull + server start")
        show_splash(srv)
        if srv.port:
            open_app_window(srv.port)
        else:
            log.error("Server never became ready")

    run_tray(srv)


if __name__ == "__main__":
    main()
