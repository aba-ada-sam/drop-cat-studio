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


# ── Windows toast notification ────────────────────────────────────────────────

def _win_toast(title: str, message: str) -> None:
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
        "$t = [Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($t);"
        "$n = $x.GetElementsByTagName('text');"
        f"$n[0].AppendChild($x.CreateTextNode('{title}')) | Out-Null;"
        f"$n[1].AppendChild($x.CreateTextNode('{message}')) | Out-Null;"
        "$tn = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "$m = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('DropCatGoStudio');"
        "$m.Show($tn);"
    )
    try:
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception as exc:
        log.debug("Toast failed: %s", exc)


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
        if srv.wait_ready(timeout=120):
            port = srv.port or PORT_START
            _win_toast("Drop Cat Go Studio", f"Server restarted on port {port}")
            try:
                if icon_ref:
                    icon_ref[0].notify(f"Restarted on port {port}.", "Drop Cat Go Studio")
            except Exception:
                pass
        else:
            _win_toast("Drop Cat Go Studio", "Restart timed out — check logs")

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

    def _notify_when_ready() -> None:
        if existing_port:
            time.sleep(1.5)
            try:
                icon.notify("Already running. Left-click to open.", "Drop Cat Go Studio")
            except Exception:
                pass
            return
        if srv.wait_ready(timeout=120):
            port = srv.port or PORT_START
            _win_toast("Drop Cat Go Studio", f"Ready — opening browser")
            webbrowser.open(f"http://127.0.0.1:{port}")
            try:
                icon.notify(f"Ready on port {port}.", "Drop Cat Go Studio")
            except Exception:
                pass
        else:
            _win_toast("Drop Cat Go Studio", "Server did not start — check logs")

    threading.Thread(target=_notify_when_ready, daemon=True, name="notify").start()

    icon.run()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== manager.pyw starting (PID %d) ===", os.getpid())

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
    except Exception as exc:
        log.error("Tray crashed: %s", exc, exc_info=True)
        srv.stop()


if __name__ == "__main__":
    main()
