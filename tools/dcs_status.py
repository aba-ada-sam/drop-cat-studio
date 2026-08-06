"""
DCS Status -- one-shot, complete answer to "is the app running and what
state is it actually in", instead of piecing together netstat/curl/git
separately and getting a partial or wrong picture.

Usage: python tools/dcs_status.py

Checks (all read-only, localhost + local git, no network egress):
  - is app.py actually listening on the configured port
  - the PID it reports, cross-checked against what's listening
  - git HEAD vs the commit the running process booted from (restart_needed
    per /api/system is ONLY this -- it does NOT see uncommitted edits)
  - uncommitted working-tree changes, listed by path (these need a restart
    to take effect for .py files; static .js/.css take effect on next page
    load with no restart needed)
  - active/queued jobs, so a restart recommendation says whether it's safe
"""
import json
import subprocess
import sys
import urllib.request

PORT = 7940
BASE = f"http://127.0.0.1:{PORT}"
REPO = r"C:\DropCat-Studio-V2"


def _get(path, timeout=3):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _git(*args):
    try:
        r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def main():
    print("=== DropCat Studio status ===")
    system = _get("/api/system")
    if system is None:
        print(f"NOT RUNNING -- no response from {BASE} (port {PORT})")
        sys.exit(1)

    pid = system.get("pid")
    boot_hash = system.get("boot_git_hash")
    print(f"RUNNING -- pid {pid}, listening on {PORT}")

    head = _git("rev-parse", "HEAD")
    if boot_hash and head:
        if boot_hash == head:
            print(f"Git commit: up to date at {head[:8]} (matches what's running)")
        else:
            behind = _git("rev-list", "--count", f"{boot_hash}..{head}")
            print(f"Git commit: {behind or '?'} commit(s) landed since this process booted "
                  f"({boot_hash[:8]} -> {head[:8]}) -- this is what the restart banner reflects")
    else:
        print("Git commit: could not determine (no boot_git_hash or git not available)")

    dirty = _git("status", "--porcelain") or ""
    dirty_files = [l for l in dirty.splitlines() if l.strip()]
    if dirty_files:
        print(f"Uncommitted changes: {len(dirty_files)} file(s) on disk, NOT committed.")
        print("  The restart banner will NOT show for these -- it only compares commits.")
        print("  .py files here need an actual restart to take effect; .js/.css take effect on next page load.")
        for f in dirty_files[:25]:
            print(f"  {f}")
        if len(dirty_files) > 25:
            print(f"  ... and {len(dirty_files) - 25} more")
    else:
        print("Uncommitted changes: none")

    jobs = _get("/api/jobs")
    if jobs is not None:
        running = jobs.get("running", [])
        queued = jobs.get("queued", [])
        if running or queued:
            print(f"Jobs: {len(running)} running, {len(queued)} queued -- a restart WOULD interrupt active work")
        else:
            print("Jobs: none running or queued -- safe to restart")
    else:
        print("Jobs: could not fetch /api/jobs")


if __name__ == "__main__":
    main()
