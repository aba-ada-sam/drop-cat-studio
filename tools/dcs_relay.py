"""
DCS Relay -- runs on the 3060.
HTTP server on port 9999. The 5080 sends commands, gets results instantly.
Zero dependencies beyond Python stdlib.

Start: python dcs_relay.py
"""
import json
import socket
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 9999
LOG  = Path("C:/DCS-satellite/relay_log.txt")


def _log(msg: str) -> None:
    ts  = datetime.now().strftime("%H:%M:%S")
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default access log

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
        if self.path == "/ping":
            self._respond({"ok": True, "machine": "3060"})
        elif self.path == "/log":
            txt = LOG.read_text(encoding="utf-8") if LOG.exists() else ""
            self._respond({"log": txt})
        else:
            self._respond({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_body()
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
    print(f"\n  3060 relay ready: http://{ip}:{PORT}\n", flush=True)

    # Open firewall for the relay port
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
