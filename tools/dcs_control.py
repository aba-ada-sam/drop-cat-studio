"""
DCS Control -- runs on the 5080 (or used by Claude Code here).
Finds the 3060 relay on the LAN and sends commands.

Usage:
  python dcs_control.py scan              -- find the 3060 relay IP
  python dcs_control.py run "cmd here"   -- run a PowerShell command
  python dcs_control.py log              -- show relay log
  python dcs_control.py                  -- interactive shell
"""
import json
import socket
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PORT      = 9999
CACHE_FILE = Path("C:/DCS-satellite/relay_ip.txt")


# -- Discovery -----------------------------------------------------------------

def _ping(ip: str, timeout: float = 0.5) -> str | None:
    try:
        url = f"http://{ip}:{PORT}/ping"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read())
            if data.get("machine") == "3060":
                return ip
    except Exception:
        pass
    return None


def find_relay(force: bool = False) -> str | None:
    if not force and CACHE_FILE.exists():
        ip = CACHE_FILE.read_text().strip()
        if _ping(ip, timeout=2):
            return ip

    # Scan local /24 subnet
    my_ip = socket.gethostbyname(socket.gethostname())
    prefix = ".".join(my_ip.split(".")[:3])
    print(f"Scanning {prefix}.0/24 for relay...", flush=True)

    candidates = [f"{prefix}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as pool:
        futures = {pool.submit(_ping, ip): ip for ip in candidates}
        for f in as_completed(futures):
            result = f.result()
            if result:
                print(f"Found relay at {result}", flush=True)
                CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                CACHE_FILE.write_text(result)
                return result

    print("Relay not found on LAN. Is dcs_relay.py running on the 3060?")
    return None


# -- Commands ------------------------------------------------------------------

def _call(ip: str, path: str, body: dict | None = None) -> dict:
    url = f"http://{ip}:{PORT}{path}"
    if body is None:
        req = urllib.request.Request(url)
    else:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=310) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        return {"out": "", "err": str(e), "rc": -1}


def run(cmd: str, ip: str | None = None) -> str:
    ip = ip or find_relay()
    if not ip:
        return "ERROR: relay not found"
    res = _call(ip, "/run", {"cmd": cmd})
    parts = []
    if res.get("out"):
        parts.append(res["out"])
    if res.get("err"):
        parts.append("STDERR: " + res["err"])
    parts.append(f"(rc={res.get('rc', '?')})")
    return "\n".join(parts)


def get_log(ip: str | None = None) -> str:
    ip = ip or find_relay()
    if not ip:
        return "ERROR: relay not found"
    return _call(ip, "/log").get("log", "")


# -- Main ----------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "scan":
        ip = find_relay(force=True)
        if ip:
            print(f"3060 relay is at {ip}:{PORT}")

    elif args[0] == "run":
        print(run(" ".join(args[1:])))

    elif args[0] == "log":
        print(get_log())

    else:
        # Interactive shell
        ip = find_relay()
        if not ip:
            sys.exit(1)
        print(f"Connected to 3060 at {ip}. Blank line to quit.")
        while True:
            try:
                cmd = input("3060> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not cmd:
                break
            print(run(cmd, ip))
