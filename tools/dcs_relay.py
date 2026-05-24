"""
DCS Relay -- runs on the 3060.
HTTP server on port 9999. The 5080 sends commands and checks service status.
Also proxies WanGP video generation: GET/POST /wangp/* forwards to the local
WanGP worker at 127.0.0.1:7899, so the 5080 can use the 3060 GPU without
needing a separate firewall hole for port 7899.
Zero dependencies beyond Python stdlib.

Start: python dcs_relay.py
"""
import json
import socket
import subprocess
import threading
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from pathlib import Path

PORT = 9999
LOG  = Path("C:/DCS-satellite/relay_log.txt")

SERVICES = {
    "acestep": {"url": "http://localhost:8020/health",            "port": 8020},
    "forge":   {"url": "http://localhost:7861/sdapi/v1/sd-models", "port": 7861},
    "wangp":   {"url": "http://localhost:7899/health",             "port": 7899},
}

WANGP_WORKER_URL = "http://127.0.0.1:7899"
WANGP_PYTHON     = r"C:\pinokio\api\wan.git\app\env\Scripts\python.exe"
WANGP_WORKER_PY  = r"C:\DCS-satellite\wangp_worker.py"
WANGP_APP_DIR    = r"C:\pinokio\api\wan.git\app"
_wangp_proc      = None
_wangp_lock      = threading.Lock()


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run_ps(cmd: str) -> dict:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=300,
        )
        return {"out": r.stdout.strip(), "err": r.stderr.strip(), "rc": r.returncode}
    except subprocess.TimeoutExpired:
        return {"out": "", "err": "TIMEOUT after 300s", "rc": -1}
    except Exception as e:
        return {"out": "", "err": str(e), "rc": -1}


def _check_service(url: str) -> tuple[bool, int]:
    """Return (alive, latency_ms)."""
    import time
    t0 = time.time()
    try:
        urllib.request.urlopen(url, timeout=1)
        return True, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0


def _service_status() -> dict:
    result = {}
    for name, info in SERVICES.items():
        alive, ms = _check_service(info["url"])
        result[name] = {
            "state":      "running" if alive else "not_running",
            "port":       info["port"],
            "latency_ms": ms,
        }
    return result


def _start_service(name):
    py = _run_ps("(Get-Command python -ErrorAction SilentlyContinue).Source").get("out", "").strip()
    if not py:
        py = "Z:/Python310/python.exe"
    cmds = {
        "relay":   [py, r"C:\DCS-satellite\dcs_relay.py"],
        "acestep": ["cmd", "/c", r"C:\DCS-satellite\start_acestep.bat"],
        "forge":   ["cmd", "/c", r"C:\pinokio\api\forge.pinokio\app\webui-user.bat"],
        "wangp":   [WANGP_PYTHON, WANGP_WORKER_PY,
                    "--wangp-app", WANGP_APP_DIR, "--port", "7899", "--host", "127.0.0.1"],
    }
    c = cmds.get(name)
    if c:
        subprocess.Popen(c, cwd=WANGP_APP_DIR, creationflags=0x08000000)
        _log(f"Started {name}")


def _start_wangp_proc():
    """Launch a fresh WanGP worker process. Caller must hold _wangp_lock."""
    global _wangp_proc
    try:
        _wangp_proc = subprocess.Popen(
            [WANGP_PYTHON, WANGP_WORKER_PY,
             "--wangp-app", WANGP_APP_DIR,
             "--port", "7899", "--host", "127.0.0.1"],
            cwd=WANGP_APP_DIR,
            stdout=open(r"C:\DCS-satellite\worker_out.log", "w"),
            stderr=open(r"C:\DCS-satellite\worker_err.log", "w"),
            creationflags=0x08000000,
        )
        _log(f"WanGP worker started (pid {_wangp_proc.pid})")
    except Exception as e:
        _log(f"Failed to start WanGP worker: {e}")


def _wangp_watchdog():
    """Background watchdog: start WanGP on boot, restart if it dies.

    Runs every 30s. If health check fails and the process is gone, starts a
    fresh worker.  This prevents satellite jobs from hanging forever when the
    worker crashes mid-generation.
    """
    import time as _time
    _log("WanGP watchdog starting...")
    while True:
        _time.sleep(30)
        with _wangp_lock:
            alive, _ = _check_service(f"{WANGP_WORKER_URL}/health")
            if alive:
                continue
            # Process dead or unresponsive -- restart
            if _wangp_proc is not None:
                try:
                    _wangp_proc.kill()
                except Exception:
                    pass
            _log("WanGP worker not responding -- restarting...")
            _start_wangp_proc()


def _ensure_wangp():
    """Start WanGP worker on relay startup and launch the watchdog thread."""
    with _wangp_lock:
        alive, _ = _check_service(f"{WANGP_WORKER_URL}/health")
        if alive:
            _log("WanGP worker already running")
        else:
            _log("Starting WanGP worker...")
            _start_wangp_proc()
    # Launch watchdog regardless -- it will keep the worker alive going forward
    threading.Thread(target=_wangp_watchdog, daemon=True, name="wangp-watchdog").start()


def _proxy_wangp(path: str, method: str, body: bytes, content_type: str):
    """Forward a request to the local WanGP worker and return (status, body_bytes)."""
    url = WANGP_WORKER_URL + path
    try:
        req = urllib.request.Request(url, data=body if body else None, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=1200) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as exc:
        err = json.dumps({"error": str(exc)}).encode()
        return 503, err


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _respond(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/wangp/"):
            status, data = _proxy_wangp(self.path[6:], "GET", None, "")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/download?"):
            # Serve a local file back to the main machine.
            # Used after satellite generation: main machine downloads the clip
            # that WanGP wrote to the 3060's local filesystem.
            import urllib.parse, os as _os
            params = dict(urllib.parse.parse_qsl(self.path[10:]))
            fpath  = params.get("path", "")
            if not fpath or not _os.path.isfile(fpath):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = open(fpath, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path in ("/", "/ui"):
            self._send_ui()
        elif self.path == "/ping":
            self._respond({"ok": True, "machine": "3060"})
        elif self.path == "/services":
            self._respond(_service_status())
        elif self.path == "/log":
            txt = LOG.read_text(encoding="utf-8") if LOG.exists() else ""
            self._respond({"log": txt})
        else:
            self._respond({"error": "not found"}, 404)

    def _send_ui(self, output="", cmd=""):
        svcs = _service_status()
        rows = ""
        for name, info in svcs.items():
            alive = info["state"] == "running"
            dot = "on" if alive else "off"
            lat = (str(info["latency_ms"]) + "ms") if alive else "--"
            action = "Restart" if alive else "Start"
            rows += (
                '<div class="s">'
                '<div class="d ' + dot + '"></div>'
                '<div class="n">' + name + '</div>'
                '<div class="l">' + lat + '</div>'
                '<form method="POST" action="/start/' + name + '">'
                '<button>' + action + '</button></form></div>'
            )
        out_html = "<pre>" + output + "</pre>" if output else ""
        ts = datetime.now().strftime("%H:%M:%S")
        css = (
            "body{font-family:monospace;background:#0d0606;color:#f0e6d0;padding:20px;max-width:580px;margin:0 auto}"
            "h2{color:#d4a017;margin:0 0 4px}"
            "p.sub{color:#666;font-size:11px;margin:0 0 12px}"
            ".s{display:flex;align-items:center;gap:10px;padding:8px 12px;margin:5px 0;"
            "background:#1a0f0f;border:1px solid #3a2020;border-radius:6px}"
            ".d{width:9px;height:9px;border-radius:50%;flex-shrink:0}"
            ".on{background:#4caf50;box-shadow:0 0 5px #4caf50}.off{background:#c41e3a}"
            ".n{flex:1}.l{font-size:11px;color:#555;min-width:44px;text-align:right}"
            "form{display:inline}"
            "button{padding:3px 10px;border:1px solid #d4a017;border-radius:3px;"
            "background:transparent;color:#d4a017;cursor:pointer;font-family:monospace;font-size:11px}"
            "button:hover{background:#d4a017;color:#000}"
            ".box{background:#1a0f0f;border:1px solid #3a2020;border-radius:5px;padding:10px;margin-top:14px}"
            "textarea{width:100%;box-sizing:border-box;background:#0d0606;border:1px solid #3a2020;"
            "color:#f0e6d0;padding:6px;font-family:monospace;font-size:11px;border-radius:3px}"
            "pre{background:#0d0606;border:1px solid #222;padding:7px;border-radius:3px;"
            "white-space:pre-wrap;word-break:break-all;font-size:10px;color:#aaa;"
            "max-height:200px;overflow-y:auto;margin:6px 0 0}"
        )
        page = (
            "<!DOCTYPE html><html><head><title>DCS 3060</title>"
            "<meta http-equiv='refresh' content='15'>"
            "<style>" + css + "</style></head><body>"
            "<h2>DCS 3060 Control Panel</h2>"
            "<p class='sub'>Auto-refreshes 15s &bull; " + ts + "</p>"
            + rows +
            "<div class='box'><b style='color:#d4a017'>PowerShell:</b><br><br>"
            "<form method='POST' action='/run'>"
            "<textarea name='cmd' rows='3'>" + cmd + "</textarea><br>"
            "<button type='submit' style='margin-top:5px'>Run</button>"
            "</form>" + out_html + "</div>"
            "</body></html>"
        )
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        # UI form posts (Content-Type: application/x-www-form-urlencoded)
        ct = self.headers.get("Content-Type", "")
        if "urlencoded" in ct:
            import urllib.parse
            params = dict(urllib.parse.parse_qsl(raw.decode()))

            if self.path.startswith("/start/"):
                name = self.path.rsplit("/", 1)[-1]
                _start_service(name)
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif self.path == "/run":
                cmd = params.get("cmd", "").strip()
                res = _run_ps(cmd) if cmd else {}
                out = (res.get("out") or "") + (res.get("err") or "")
                self._send_ui(output=out, cmd=cmd)
            else:
                self._send_ui()
            return

        # WanGP proxy
        if self.path.startswith("/wangp/"):
            ct = self.headers.get("Content-Type", "application/json")
            status, data = _proxy_wangp(self.path[6:], "POST", raw, ct)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # JSON API (original relay protocol)
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        if self.path == "/run":
            cmd = body.get("cmd", "")
            _log(f"RUN: {cmd[:120]}")
            res = _run_ps(cmd)
            _log(f"rc={res['rc']}  {res['out'][:120]}")
            self._respond(res)
        elif self.path.startswith("/start/"):
            name = self.path.rsplit("/", 1)[-1]
            _start_service(name)
            self._respond({"ok": True, "started": name})
        else:
            self._respond({"error": "not found"}, 404)


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ip = socket.gethostbyname(socket.gethostname())
    _log(f"DCS Relay starting on {ip}:{PORT}")
    print(f"\n  3060 relay: http://{ip}:{PORT}\n", flush=True)
    subprocess.run(
        ["netsh", "advfirewall", "firewall", "add", "rule",
         "name=DCS-Relay", "dir=in", "action=allow",
         "protocol=TCP", f"localport={PORT}"],
        capture_output=True,
    )
    # Start WanGP worker in background (binds localhost only; proxied through relay)
    threading.Thread(target=_ensure_wangp, daemon=True).start()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
