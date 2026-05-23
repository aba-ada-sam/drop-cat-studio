"""
DCS Backup Control Panel -- runs on the 3060, port 9998.
Standalone web UI, independent of the main relay (port 9999).
Open http://192.168.86.49:9998 in any browser on the LAN.
Auto-refreshes. Has Start/Restart buttons and a PowerShell terminal.
"""
import socket
import subprocess
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 9998

SERVICES = {
    "relay":   "http://localhost:9999/ping",
    "acestep": "http://localhost:8020/health",
    "ollama":  "http://localhost:11434/api/tags",
    "forge":   "http://localhost:7861/sdapi/v1/sd-models",
}

# CSS stored without curly braces conflict -- use .replace to embed safely
_CSS = (
    "body{font-family:monospace;background:#0d0606;color:#f0e6d0;"
    "padding:24px;max-width:640px;margin:0 auto}"
    "h1{color:#d4a017}p.sub{color:#666;font-size:11px;margin-top:0}"
    ".svc{display:flex;align-items:center;gap:12px;padding:10px 14px;"
    "margin:6px 0;background:#1a0f0f;border:1px solid #3a2020;border-radius:8px}"
    ".dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}"
    ".on{background:#4caf50;box-shadow:0 0 6px #4caf50}.off{background:#c41e3a}"
    ".name{flex:1}.lat{font-size:11px;color:#666;min-width:50px;text-align:right}"
    "form{display:inline}"
    "button{padding:4px 12px;border:1px solid #d4a017;border-radius:4px;"
    "background:transparent;color:#d4a017;cursor:pointer;font-family:monospace;font-size:12px}"
    "button:hover{background:#d4a017;color:#000}"
    ".box{background:#1a0f0f;border:1px solid #3a2020;border-radius:6px;"
    "padding:12px;margin-top:16px}"
    "textarea{width:100%;box-sizing:border-box;background:#0d0606;"
    "border:1px solid #3a2020;color:#f0e6d0;padding:8px;"
    "font-family:monospace;font-size:12px;border-radius:4px}"
    "pre{background:#0d0606;border:1px solid #222;padding:8px;border-radius:4px;"
    "white-space:pre-wrap;word-break:break-all;font-size:11px;color:#aaa;"
    "max-height:240px;overflow-y:auto;margin:8px 0 0}"
    ".ts{font-size:10px;color:#444;margin-top:16px}"
)


def _ping(url):
    t0 = time.time()
    try:
        urllib.request.urlopen(url, timeout=2)
        return True, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0


def _run_ps(cmd):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=30,
        )
        return (r.stdout + r.stderr).strip() or "(no output)"
    except Exception as e:
        return str(e)


def _page(cmd="", output=""):
    rows = []
    for name, url in SERVICES.items():
        alive, ms = _ping(url)
        dot_cls = "on" if alive else "off"
        lat = (str(ms) + "ms") if alive else "--"
        action = "Restart" if alive else "Start"
        row = (
            '<div class="svc">'
            '<div class="dot ' + dot_cls + '"></div>'
            '<div class="name">' + name + '</div>'
            '<div class="lat">' + lat + '</div>'
            '<form method="POST" action="/start/' + name + '">'
            '<button>' + action + '</button></form>'
            '</div>'
        )
        rows.append(row)

    out_html = "<pre>" + output + "</pre>" if output else ""
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "localhost"

    html = (
        "<!DOCTYPE html><html><head><title>DCS 3060</title>"
        "<meta http-equiv='refresh' content='15'>"
        "<style>" + _CSS + "</style></head><body>"
        "<h1>DCS 3060 Control Panel</h1>"
        "<p class='sub'>Backup panel &mdash; auto-refreshes 15s | " + ts + "</p>"
        + "".join(rows) +
        "<div class='box'><b style='color:#d4a017'>Run PowerShell on this machine:</b><br><br>"
        "<form method='POST' action='/run'>"
        "<textarea name='cmd' rows='3'>" + cmd + "</textarea><br>"
        "<button type='submit' style='margin-top:6px'>Run</button>"
        "</form>" + out_html + "</div>"
        "<p class='ts'>http://" + ip + ":" + str(PORT) + "</p>"
        "</body></html>"
    )
    return html.encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self._send(_page())
        except Exception:
            err = traceback.format_exc().encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode()
            params = {}
            for part in raw.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = urllib.parse.unquote_plus(v)

            if self.path.startswith("/start/"):
                name = self.path.rsplit("/", 1)[-1]
                py = _run_ps("(Get-Command python -ErrorAction SilentlyContinue).Source").strip()
                if not py or "not recognized" in py:
                    py = "Z:/Python310/python.exe"

                if name == "relay":
                    subprocess.Popen([py, r"C:\DCS-satellite\dcs_relay.py"],
                                     creationflags=0x08000000)
                    out = "Relay starting on port 9999..."
                elif name == "ollama":
                    subprocess.Popen(["ollama", "serve"], creationflags=0x08000000)
                    out = "Ollama starting on port 11434..."
                elif name == "acestep":
                    subprocess.Popen(["cmd", "/c", r"C:\DCS-satellite\start_acestep.bat"],
                                     creationflags=0x08000000)
                    out = "ACE-Step starting on port 8020..."
                elif name == "forge":
                    subprocess.Popen(["cmd", "/c",
                                      r"C:\pinokio\api\forge.pinokio\app\webui-user.bat"],
                                     creationflags=0x08000000)
                    out = "Forge starting on port 7861 (first run: 10-15 min)..."
                else:
                    out = "Unknown service: " + name
                self._send(_page(output=out))

            elif self.path == "/run":
                cmd = params.get("cmd", "").strip()
                out = _run_ps(cmd) if cmd else ""
                self._send(_page(cmd=cmd, output=out))
            else:
                self._send(b"Not found", 404)

        except Exception:
            err = traceback.format_exc().encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)


def main():
    subprocess.run(
        ["netsh", "advfirewall", "firewall", "add", "rule",
         "name=DCS-Backup", "dir=in", "action=allow",
         "protocol=TCP", "localport=" + str(PORT)],
        capture_output=True,
    )
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "localhost"
    print("\n  DCS Backup Panel: http://" + ip + ":" + str(PORT) + "\n", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
