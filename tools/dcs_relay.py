"""
DCS Relay -- runs on the 3060.
HTTP server on port 9999. The 5080 sends commands and checks service status.
Zero dependencies beyond Python stdlib.

Start: python dcs_relay.py
"""
import json
import socket
import subprocess
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 9999
LOG  = Path("C:/DCS-satellite/relay_log.txt")

SERVICES = {
    "acestep": {"url": "http://localhost:8019/health",           "port": 8019},
    "ollama":  {"url": "http://localhost:11434/api/tags",        "port": 11434},
    "forge":   {"url": "http://localhost:7861/sdapi/v1/sd-models","port": 7861},
}


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
        "ollama":  ["ollama", "serve"],
        "forge":   ["cmd", "/c", r"C:\pinokio\api\forge.pinokio\app\webui-user.bat"],
    }
    c = cmds.get(name)
    if c:
        subprocess.Popen(c, creationflags=0x08000000)
        _log(f"Started {name}")


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
        if self.path in ("/", "/ui"):
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
                self._send_ui(output=f"Starting {name}...")
            elif self.path == "/run":
                cmd = params.get("cmd", "").strip()
                res = _run_ps(cmd) if cmd else {}
                out = (res.get("out") or "") + (res.get("err") or "")
                self._send_ui(output=out, cmd=cmd)
            else:
                self._send_ui()
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
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
