"""
manager.pyw -- Drop Cat Go Studio window manager
Run with:  pythonw.exe manager.pyw

- Shows tkinter loading splash instantly while server starts
- Opens app in Chrome --app mode (plain window, no URL bar or tabs)
- Single-instance mutex: double-click while running re-opens the window
- Closing the Chrome window shuts down the server and exits
- Keeps app.py alive, restarts on crash (max 5 in 60s)
"""
from __future__ import annotations

import ctypes
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

# Suppress console windows for all subprocess calls on Windows.
# Without this every git/pip/netstat/taskkill briefly flashes a terminal.
_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# -- Job Object: kill app.py when manager.pyw exits for any reason -------------
# This is the OS-level guarantee that app.py (and its own GPU children) die
# whenever manager.pyw exits -- whether via the quit dialog, Task Manager,
# or a crash.  app.py assigns WanGP/ACE-Step to its own Job Object, so the
# kill chain is: manager.pyw exits -> OS closes this handle -> app.py dies ->
# app.py's Job Object closes -> WanGP/ACE-Step die.
_MGR_JOB: ctypes.c_void_p | None = None

def _init_manager_job() -> None:
    global _MGR_JOB
    if sys.platform != "win32" or _MGR_JOB is not None:
        return
    try:
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation   = 9

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit",     ctypes.c_int64),
                ("LimitFlags",             ctypes.c_uint32),
                ("MinimumWorkingSetSize",   ctypes.c_size_t),
                ("MaximumWorkingSetSize",   ctypes.c_size_t),
                ("ActiveProcessLimit",      ctypes.c_uint32),
                ("Affinity",               ctypes.c_size_t),
                ("PriorityClass",           ctypes.c_uint32),
                ("SchedulingClass",         ctypes.c_uint32),
            ]
        class _IO(ctypes.Structure):
            _fields_ = [(f, ctypes.c_uint64) for f in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount",  "WriteTransferCount",  "OtherTransferCount",
            )]
        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo",                _IO),
                ("ProcessMemoryLimit",    ctypes.c_size_t),
                ("JobMemoryLimit",        ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed",     ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _EXT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return
        _MGR_JOB = job
    except Exception:
        pass


def _assign_app_to_job(proc: subprocess.Popen) -> None:
    if _MGR_JOB is None or sys.platform != "win32":
        return
    try:
        PROCESS_ALL_ACCESS = 0x1F0FFF
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
        if h:
            k32.AssignProcessToJobObject(_MGR_JOB, h)
            k32.CloseHandle(h)
    except Exception:
        pass

# -- Paths ---------------------------------------------------------------------

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


# -- Python interpreter --------------------------------------------------------

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


# -- Port helpers --------------------------------------------------------------

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


def server_responds(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def is_dcs_server(port: int, timeout: float = 1.5) -> bool:
    """Return True only if port has a live DCS server (checks /api/version).

    A plain TCP connect is not enough -- Forge SD also listens in the 7860-7879
    range. Without this check, manager opens Chrome pointing at Forge and the
    user sees Forge's 404 instead of DCS.
    """
    import urllib.request as _ur
    try:
        with _ur.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=timeout) as r:
            body = r.read(200).decode(errors="replace")
            return "Drop Cat" in body or "version" in body.lower()
    except Exception:
        return False


def find_running_server() -> int | None:
    """Find a live DCS server: check .dcs-port first, then scan 7860-7879."""
    port, _ = read_port_file()
    if port and server_responds(port) and is_dcs_server(port):
        return port
    # Port file missing or stale -- scan the full range
    for p in range(7860, 7880):
        if p == port:
            continue  # already checked above
        if server_responds(p) and is_dcs_server(p):
            return p
    return None


def kill_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, **_NW)
        log.info("Killed PID %d", pid)
    except Exception as exc:
        log.warning("taskkill %d failed: %s", pid, exc)


# -- Auto-update (git pull + pip install) -------------------------------------

def _do_git_pull(on_status=None) -> None:
    """Pull latest code; pip-install deps only if the commit changed. Non-fatal."""
    def _status(msg):
        if on_status:
            on_status(msg)

    if not shutil.which("git"):
        log.info("git not on PATH -- skipping update check")
        return

    _status("Checking for updates...")
    try:
        sha_before = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, **_NW,
        ).stdout.strip()
    except Exception:
        sha_before = ""

    try:
        subprocess.run(
            ["git", "-C", str(ROOT), "pull", "--ff-only", "origin", "master"],
            capture_output=True, timeout=30, **_NW,
        )
    except Exception as exc:
        log.warning("git pull skipped: %s", exc)
        return

    try:
        sha_after = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, **_NW,
        ).stdout.strip()
    except Exception:
        sha_after = sha_before

    if sha_before and sha_after and sha_before != sha_after:
        log.info("New code pulled (%s -> %s)", sha_before[:7], sha_after[:7])
        # Only pip-install if requirements.txt itself changed -- avoids a
        # 30-60s dep-resolution crawl on every code-only update.
        req_changed = False
        try:
            diff = subprocess.run(
                ["git", "-C", str(ROOT), "diff", "--name-only", sha_before, sha_after, "--", "requirements.txt"],
                capture_output=True, text=True, timeout=10, **_NW,
            )
            req_changed = bool(diff.stdout.strip())
        except Exception:
            req_changed = True  # can't tell -- be safe and install

        if req_changed:
            log.info("requirements.txt changed -- updating dependencies")
            _status("Updating dependencies...")
            try:
                req = ROOT / "requirements.txt"
                if req.exists():
                    subprocess.run(
                        ["pip", "install", "-q", "-r", str(req)],
                        capture_output=True, timeout=180, **_NW,
                    )
            except Exception as exc:
                log.warning("pip install failed (non-fatal): %s", exc)
        else:
            log.info("requirements.txt unchanged -- skipping pip install")
    else:
        log.info("Already up to date")


# -- Desktop shortcut self-update ----------------------------------------------

def _ensure_shortcut() -> None:
    """Keep the desktop shortcut pointing at launch-silent.vbs via wscript.exe."""
    try:
        desktop_lnk = Path(os.environ["USERPROFILE"]) / "Desktop" / "Drop Cat Go Studio.lnk"
        vbs = ROOT / "launch-silent.vbs"
        ico = ROOT / "dropcat.ico"
        if not vbs.exists():
            return
        ico_str = f"{ico},0" if ico.exists() else ""
        wscript = r"C:\Windows\System32\wscript.exe"
        ps = (
            f'$ws=New-Object -ComObject WScript.Shell;'
            f'$sc=$ws.CreateShortcut("{desktop_lnk}");'
            f'$sc.TargetPath="{wscript}";'
            f'$sc.Arguments=\'"{vbs}"\';'
            f'$sc.WorkingDirectory="{ROOT}";'
            f'$sc.IconLocation="{ico_str}";'
            f'$sc.Description="Drop Cat Go Studio";'
            f'$sc.Save()'
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=15, **_NW,
        )
        log.info("Desktop shortcut updated -> launch-silent.vbs")
    except Exception as exc:
        log.warning("_ensure_shortcut failed (non-fatal): %s", exc)


# -- Open app window -----------------------------------------------------------

def _focus_existing_dcs_window() -> bool:
    """Bring an already-open Drop Cat Go Studio Chrome window to the front.

    Returns True if a window was found and focused, False if none exists.
    Called before launching a new Chrome window to avoid duplicates.
    """
    try:
        ps = (
            "Add-Type -TypeDefinition '"
            "using System; using System.Runtime.InteropServices; "
            "public class WF { "
            "[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h); "
            "[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int n); "
            "}'; "
            "$p = Get-Process -Name chrome -ErrorAction SilentlyContinue | "
            "Where-Object { $_.MainWindowTitle -like '*Drop Cat Go Studio*' } | "
            "Select-Object -First 1; "
            "if ($p) { [WF]::ShowWindow($p.MainWindowHandle, 9); "
            "[WF]::SetForegroundWindow($p.MainWindowHandle); exit 0 } else { exit 1 }"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=8, **_NW,
        )
        if r.returncode == 0:
            log.info("Focused existing Drop Cat Go Studio window")
            return True
    except Exception as exc:
        log.debug("_focus_existing_dcs_window failed: %s", exc)
    return False


def open_app_window(port: int) -> "subprocess.Popen | None":
    """Open the app in Chrome --app mode using a dedicated profile.

    Checks for an existing Drop Cat Go Studio window first -- if one is found
    it is focused and this function returns None (no new process to track).
    This prevents duplicate windows when the shortcut is clicked while the app
    is already open.

    Returns the Popen object, or None if Chrome was not found (default browser)
    or an existing window was focused instead.
    """
    if _focus_existing_dcs_window():
        # Existing window brought to front -- this manager instance has no
        # further role. Exit cleanly so the original manager keeps tracking
        # the Chrome lifecycle and handles shutdown when the window closes.
        log.info("Focused existing window -- this manager instance exiting")
        sys.exit(0)
    url = f"http://127.0.0.1:{port}"
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    profile_dir = str(ROOT / ".chrome_profile")
    for path in chrome_paths:
        if os.path.isfile(path):
            args = [
                path,
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--window-size=1400,900",
                "--window-position=100,50",
            ]
            if ICO_PATH.exists():
                args.append(f"--app-icon={ICO_PATH}")
            proc = subprocess.Popen(args)
            log.info("Opened Chrome --app on port %d (profile: %s)", port, profile_dir)
            return proc
    # Chrome not found -- fall back to default browser (can't track close)
    import webbrowser
    webbrowser.open(url)
    log.warning("Chrome not found, opened default browser")
    return None


# -- Server process manager ----------------------------------------------------

class ServerManager:
    READY_TIMEOUT = 120

    def __init__(self, on_crash=None) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._port: int | None = None
        self._on_crash = on_crash  # callable(exit_code) -- shown to user on unexpected exit

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
        self._stop_event.clear()
        self._spawn()
        threading.Thread(target=self._watch, daemon=True, name="srv-watch").start()

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

        _assign_app_to_job(proc)
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
                time.sleep(1)
                continue
            ret = proc.poll()
            if ret is None:
                time.sleep(2)
                continue
            if self._stop_event.is_set():
                log.info("Server exited (manager stopping)")
                return
            # Unexpected exit -- notify the user instead of silently restarting.
            log.warning("Server exited unexpectedly (code %s)", ret)
            self._ready_event.clear()
            self._port = None
            with self._lock:
                self._proc = None
            if self._on_crash:
                self._on_crash(ret)
            return


# -- Crash notification --------------------------------------------------------

def _show_crash_ui(srv: "ServerManager", exit_code: int) -> None:
    """Pop a crash notification with Restart / Quit buttons.

    Called from the srv._on_crash callback (background thread) via root.after
    so tkinter runs on the main thread. The Chrome window is still open showing
    a dead page -- restarting brings it back without relaunching Chrome.
    """
    try:
        import tkinter as tk
    except ImportError:
        log.error("Server crashed (exit %s) -- tkinter unavailable, cannot show UI", exit_code)
        return

    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg="#0d0606")
    # Pop above everything so the user notices, but release topmost on focus
    # change or after 2s so it doesn't trap their workflow.
    root.attributes("-topmost", True)
    def _drop_topmost(_e=None):
        try: root.attributes("-topmost", False)
        except tk.TclError: pass
    root.bind("<FocusOut>", _drop_topmost)
    root.after(2000, _drop_topmost)

    W, H = 340, 200
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    tk.Label(root, text="DROP CAT GO", bg="#0d0606", fg="#d4a017",
             font=("Arial Black", 16, "bold")).pack(pady=(22, 2))
    tk.Label(root, text="S T U D I O", bg="#0d0606", fg="#8a7a6a",
             font=("Arial", 8)).pack()

    tk.Label(root, text="Server stopped unexpectedly.", bg="#0d0606", fg="#f0e6d0",
             font=("Arial", 10)).pack(pady=(14, 2))
    tk.Label(root, text=f"Exit code: {exit_code}  --  see logs/server.log",
             bg="#0d0606", fg="#6a5a4a", font=("Arial", 8)).pack()

    btn_frame = tk.Frame(root, bg="#0d0606")
    btn_frame.pack(pady=18)

    def _restart():
        root.destroy()
        # Kill GPU workers before restarting -- without this, the old WanGP process
        # survives and a second one gets spawned by the new app.py, causing dual workers.
        _kill_procs_on_port(7899, "WanGP")
        _kill_procs_on_port(8020, "ACE-Step")
        _kill_by_cmdline("wangp_worker.py", "WanGP")
        # show_splash drives the same startup sequence as initial launch
        show_splash(srv)
        if srv.port:
            # Chrome window is still open -- just reload it
            try:
                import urllib.request as _ur
                _ur.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=2)
            except Exception:
                pass

    def _quit():
        root.destroy()
        _shutdown(srv)

    tk.Button(
        btn_frame, text="  Restart  ", bg="#c41e3a", fg="#f0e6d0",
        activebackground="#a01828", activeforeground="#f0e6d0",
        relief="flat", bd=0, font=("Arial", 10, "bold"), cursor="hand2",
        padx=14, pady=6, command=_restart,
    ).pack(side="left", padx=10)

    tk.Button(
        btn_frame, text="  Quit  ", bg="#2a1010", fg="#8a7a6a",
        activebackground="#1a0808", activeforeground="#f0e6d0",
        relief="flat", bd=0, font=("Arial", 10), cursor="hand2",
        padx=14, pady=6, command=_quit,
    ).pack(side="left", padx=10)

    root.mainloop()


# -- Opening splash (already-running path) -------------------------------------

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
    root.after(5000, lambda: root.attributes("-topmost", False))

    W, H = 320, 140
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    tk.Label(root, text="DROP CAT GO", bg="#0d0606", fg="#d4a017",
             font=("Arial Black", 20, "bold")).pack(pady=(28, 2))
    tk.Label(root, text="S T U D I O", bg="#0d0606", fg="#8a7a6a",
             font=("Arial", 8)).pack()
    tk.Label(root, text="Opening...", bg="#0d0606", fg="#6a5a4a",
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


# -- Loading splash (tkinter) --------------------------------------------------

def show_splash(srv: ServerManager) -> None:
    """Frameless loading window: pulls updates -> starts server -> closes.

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
    # Pop above everything on initial paint so the user sees the splash, but
    # release topmost as soon as they click any other window. Also drop after
    # 2s as a fallback in case FocusOut doesn't fire on this overrideredirect
    # window (Windows is inconsistent here).
    root.attributes("-topmost", True)
    def _drop_topmost(_e=None):
        try: root.attributes("-topmost", False)
        except tk.TclError: pass
    root.bind("<FocusOut>", _drop_topmost)
    root.after(2000, _drop_topmost)

    W, H = 320, 180
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    tk.Label(root, text="DROP CAT GO", bg="#0d0606", fg="#d4a017",
             font=("Arial Black", 20, "bold")).pack(pady=(28, 2))
    tk.Label(root, text="S T U D I O", bg="#0d0606", fg="#8a7a6a",
             font=("Arial", 8)).pack()

    status = tk.StringVar(value="Checking for updates...")
    tk.Label(root, textvariable=status, bg="#0d0606", fg="#6a5a4a",
             font=("Arial", 9)).pack(pady=(16, 0))

    dot_var = tk.StringVar(value="*ooo")
    tk.Label(root, textvariable=dot_var, bg="#0d0606", fg="#c41e3a",
             font=("Arial", 13)).pack(pady=4)

    _skip_port = [None]  # shared between skip handler and _bg thread

    def _do_skip():
        # Find any running server so open_app_window gets a valid port
        _skip_port[0] = find_running_server() or srv.port
        try:
            root.destroy()
        except Exception:
            pass

    # Skip button -- hidden until 8s
    skip_btn = tk.Button(
        root, text="Skip waiting", bg="#1a0a0a", fg="#6a5a4a",
        relief="flat", bd=0, font=("Arial", 8), cursor="hand2",
        command=_do_skip,
    )
    skip_btn.pack(pady=(4, 0))
    skip_btn.pack_forget()  # hidden initially

    dots = ["*ooo", "o*oo", "oo*o", "ooo*"]
    idx = [0]

    def _tick():
        idx[0] = (idx[0] + 1) % 4
        dot_var.set(dots[idx[0]])
        root.after(260, _tick)

    def _bg():
        _do_git_pull(on_status=lambda s: root.after(0, lambda: status.set(s)))
        root.after(0, lambda: status.set("Starting server..."))
        srv.start()
        deadline = time.time() + 120
        while not srv.ready and time.time() < deadline:
            # Also accept a server started by an external process (e.g. manual restart)
            ext = find_running_server()
            if ext:
                if not srv.port:
                    srv._port = ext
                break
            time.sleep(0.4)
        try:
            root.after(0, root.destroy)
        except Exception:
            pass

    def _show_skip():
        try:
            skip_btn.pack(pady=(4, 0))
        except Exception:
            pass

    _tick()
    root.after(8000, _show_skip)
    threading.Thread(target=_bg, daemon=True).start()
    root.mainloop()
    # mainloop exited -- use skip port if bg thread didn't set srv.port
    if _skip_port[0] and not srv.port:
        srv._port = _skip_port[0]


def _kill_procs_on_port(port: int, label: str) -> None:
    """Kill all processes listening on port (including non-LISTENING ones via wmic)."""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5, **_NW)
        for line in r.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid_s = parts[-1].strip()
                if pid_s.isdigit() and int(pid_s) > 0:
                    log.info("Killing %s on port %d (PID %s)", label, port, pid_s)
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid_s],
                                   capture_output=True, timeout=5, **_NW)
    except Exception as e:
        log.warning("Port kill %d failed: %s", port, e)


def _kill_by_cmdline(script: str, label: str) -> None:
    """Kill all processes whose command line contains script name.

    Uses PowerShell Get-WmiObject (works on Windows 11 where wmic is removed).
    Catches non-LISTENING orphans that survive the port-based kill.
    """
    own = os.getpid()
    try:
        ps_cmd = (
            "Get-WmiObject Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{script}*' }} | "
            "Select-Object ProcessId | "
            "ForEach-Object { $_.ProcessId }"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15, **_NW,
        )
        for line in r.stdout.splitlines():
            pid_s = line.strip()
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            if pid <= 0 or pid == own:
                continue
            log.info("Killing stale %s (PID %d) by cmdline scan", label, pid)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=5, **_NW)
    except Exception as e:
        log.warning("Cmdline kill for %s failed: %s", label, e)


def _confirm_quit() -> bool:
    """Show a themed confirmation dialog. Returns True if user wants to quit."""
    try:
        import tkinter as tk
    except ImportError:
        return True  # no UI available -- just quit

    result = [False]
    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg="#0d0606")
    root.attributes("-topmost", True)

    W, H = 360, 190
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

    tk.Label(root, text="DROP CAT GO", bg="#0d0606", fg="#d4a017",
             font=("Arial Black", 15, "bold")).pack(pady=(20, 2))
    tk.Label(root, text="S T U D I O", bg="#0d0606", fg="#8a7a6a",
             font=("Arial", 8)).pack()
    tk.Label(root, text="Stop the server and quit?",
             bg="#0d0606", fg="#f0e6d0", font=("Arial", 10)).pack(pady=(12, 2))
    tk.Label(root, text="Any running generation will be cancelled.",
             bg="#0d0606", fg="#6a5a4a", font=("Arial", 8)).pack()

    btn_frame = tk.Frame(root, bg="#0d0606")
    btn_frame.pack(pady=16)

    def _yes():
        result[0] = True
        root.destroy()

    def _no():
        result[0] = False
        root.destroy()

    tk.Button(
        btn_frame, text="  Stop & Quit  ", bg="#c41e3a", fg="#f0e6d0",
        activebackground="#a01828", activeforeground="#f0e6d0",
        relief="flat", bd=0, font=("Arial", 10, "bold"), cursor="hand2",
        padx=14, pady=6, command=_yes,
    ).pack(side="left", padx=8)

    tk.Button(
        btn_frame, text="  Keep Running  ", bg="#1a2a1a", fg="#8abf8a",
        activebackground="#0a1a0a", activeforeground="#aadaaa",
        relief="flat", bd=0, font=("Arial", 10), cursor="hand2",
        padx=14, pady=6, command=_no,
    ).pack(side="left", padx=8)

    root.mainloop()
    return result[0]


def _shutdown(srv: "ServerManager") -> None:
    """Kill all DCS-related processes, then exit."""
    log.info("Shutting down")

    # 1. Kill GPU workers + Forge by port (fast -- catches LISTENING processes)
    _kill_procs_on_port(7899, "WanGP")
    _kill_procs_on_port(8020, "ACE-Step")
    _kill_procs_on_port(7861, "Forge")

    # 2. Kill GPU workers by command-line scan (backstop -- catches non-LISTENING
    #    orphans that port kill misses, e.g. second worker that lost the port race)
    _kill_by_cmdline("wangp_worker.py", "WanGP")
    _kill_by_cmdline("api_server.py", "ACE-Step")
    _kill_by_cmdline("webui.py", "Forge")

    # 3. Also try via services module (uses tracked Popen handles -- most reliable
    #    when app.py spawned the workers through the normal path)
    try:
        from services import manager as svc
        svc.shutdown_all()
    except Exception as e:
        log.warning("Service module shutdown failed (non-fatal): %s", e)

    # 4. Kill app.py: via tracked proc handle if we spawned it...
    srv.stop()

    # 5. ...and via port file PID as backstop for when we attached to a
    #    pre-existing server (srv._proc is None in that path)
    _, server_pid = read_port_file()
    if server_pid:
        log.info("Killing app.py by port file PID %d", server_pid)
        kill_pid(server_pid)

    clear_port_file()
    os._exit(0)


# -- Entry point ---------------------------------------------------------------

def _diag(msg: str) -> None:
    """Append a timestamped line to manager_diag.txt for silent-crash diagnosis."""
    try:
        with open(ROOT / "manager_diag.txt", "a", encoding="utf-8") as _f:
            _f.write(f"{time.strftime('%H:%M:%S')} PID={os.getpid()} {msg}\n")
    except Exception:
        pass


def main() -> None:
    _diag("main() entered")
    log.info("=== manager.pyw starting (PID %d) ===", os.getpid())
    _init_manager_job()

    # Single-instance: Windows named mutex held for the lifetime of the process.
    # _MUTEX_HANDLE is module-level so Python's GC never releases it.
    global _MUTEX_HANDLE
    import ctypes as _ct
    _k32 = _ct.WinDLL("kernel32", use_last_error=True)
    _MUTEX_HANDLE = _k32.CreateMutexW(None, True, "Local\\DropCatGoStudio_Manager_v2")
    if _ct.get_last_error() == 183:  # ERROR_ALREADY_EXISTS -- another manager owns it
        # Find the other manager's PID and check if its server is still alive.
        # If the server is dead the old manager is a zombie -- kill it and take over.
        other_pid = None
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if proc.info["name"] and "pythonw" in proc.info["name"].lower():
                        cl = " ".join(proc.info["cmdline"] or [])
                        if "manager.pyw" in cl and proc.pid != os.getpid():
                            other_pid = proc.pid
                            break
                except Exception:
                    pass
        except ImportError:
            pass

        existing_alive = find_running_server()

        if existing_alive and not other_pid:
            # Mutex held by an undetectable process and server is alive -- just open.
            log.info("Server on port %d alive, no trackable manager -- opening window", existing_alive)
            _show_opening_splash()
            sys.exit(0)
        else:
            # Kill any stuck manager so WE take over Chrome tracking and shutdown.
            if other_pid:
                log.info("Replacing stuck manager PID %d -- taking over Chrome tracking", other_pid)
                kill_pid(other_pid)
                time.sleep(0.5)
            if not existing_alive:
                clear_port_file()
            # Release and re-acquire the mutex under our PID
            _k32.ReleaseMutex(_MUTEX_HANDLE)
            _MUTEX_HANDLE = _k32.CreateMutexW(None, True, "Local\\DropCatGoStudio_Manager_v2")
            # Fall through to normal startup

    _diag("calling _ensure_shortcut")
    # Keep the desktop shortcut pointing at manager.pyw (transition from launch.bat)
    _ensure_shortcut()

    _diag("_ensure_shortcut done -- finding server")

    def _on_crash(exit_code: int) -> None:
        threading.Thread(target=_show_crash_ui, args=(srv, exit_code), daemon=True).start()

    srv = ServerManager(on_crash=_on_crash)

    # Fast check: is a server already recorded in .dcs-port and alive?
    existing_port = find_running_server()

    chrome_proc = None
    if existing_port:
        _diag(f"server already on port {existing_port} -- opening window")
        log.info("Server already on port %d", existing_port)
        srv._port = existing_port
        srv._ready_event.set()
        chrome_proc = open_app_window(existing_port)
    else:
        # show_splash drives the full startup: git pull -> srv.start() -> wait ready
        _diag("no server found -- calling show_splash")
        log.info("Starting fresh -- splash will handle git pull + server start")
        show_splash(srv)
        _diag(f"show_splash returned -- srv.port={srv.port}")
        if srv.port:
            chrome_proc = open_app_window(srv.port)
        else:
            log.error("Server never became ready")

    # Block until the Chrome window closes, then ask before shutting down.
    # If Chrome wasn't found (default browser), keep alive until the server dies.
    _diag("waiting for window close")
    if chrome_proc is not None:
        # Guard: if Chrome exits within 10s of opening it was delegated to an
        # existing instance (same --user-data-dir profile already running).
        # Don't treat that as the user closing the window -- just switch to
        # keepalive mode without close-tracking.
        time.sleep(10)
        if chrome_proc.poll() is not None:
            log.info("Initial Chrome open delegated to existing instance -- keepalive mode")
            srv._stop_event.wait()
        else:
            chrome_proc.wait()
            log.info("App window closed -- shutting down")
            _shutdown(srv)
    else:
        # No trackable window -- keep manager alive as a watchdog indefinitely
        srv._stop_event.wait()


if __name__ == "__main__":
    # Module-level diag: proves the process started and imports succeeded
    try:
        with open(ROOT / "manager_diag.txt", "a", encoding="utf-8") as _f:
            _f.write(f"{time.strftime('%H:%M:%S')} PID={os.getpid()} __main__ block reached\n")
    except Exception:
        pass
    main()
