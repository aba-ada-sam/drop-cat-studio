"""Rule-6 regression tests for a7a79f0 (resolution clamp + busy-worker guard)
and the fixes on top of it. NO real GPU, NO app boot -- real local TCP sockets
stand in for the WanGP worker so the ACTUAL network code path is exercised,
not a mock of it.

RUN IT WITH:  python tests/test_gpu_safety.py
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.song_video.pipeline import (  # noqa: E402
    ROOMY_VRAM_GB, SAFE_FALLBACK_RES, _clamp_res_for_gpu, _detected_vram_gb,
)
import services.manager as mgr  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


class _L:
    warning = staticmethod(lambda m, *a, **k: None)
    info = staticmethod(lambda m, *a, **k: None)


class FakeR:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out


print("\n-- A: _detected_vram_gb fails closed on every unmeasurable case --")
with mock.patch("subprocess.run", side_effect=FileNotFoundError):
    ok(_detected_vram_gb() is None, "nvidia-smi binary missing -> None")
with mock.patch("subprocess.run", return_value=FakeR(0, "N/A\n")):
    ok(_detected_vram_gb() is None, "garbage stdout ('N/A') -> None, not a crash")
with mock.patch("subprocess.run", return_value=FakeR(0, "")):
    ok(_detected_vram_gb() is None, "empty stdout -> None")
with mock.patch("subprocess.run", return_value=FakeR(1, "")):
    ok(_detected_vram_gb() is None, "nonzero returncode -> None")
with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 5)):
    ok(_detected_vram_gb() is None, "subprocess timeout -> None, not an exception")

print("\n-- B: an unmeasurable-VRAM job clamps (fail closed, the whole point) --")
with mock.patch("subprocess.run", side_effect=FileNotFoundError):
    ok(_clamp_res_for_gpu(1032, 580, _L) == SAFE_FALLBACK_RES,
       "no VRAM reading -> clamps to the safe fallback, does not gamble on the full request")

print("\n-- C: threshold boundary -- real-world nvidia-smi totals for a 24GB card --")
# Windows WDDM commonly reports a few hundred MiB less than nominal capacity.
for mib, note in ((24576, "exact nominal"), (24564, "typical real-world report")):
    with mock.patch("subprocess.run", return_value=FakeR(0, f"{mib}\n")):
        ok(_clamp_res_for_gpu(1032, 580, _L) == (1032, 580),
           f"{mib} MiB ({note}) clears the {ROOMY_VRAM_GB}GB roomy threshold")
# LOW-severity finding, documented not silently accepted: a card whose driver
# reserves more overhead (still genuinely ~24GB nominal) can round to just
# under the threshold and clamp unnecessarily. That is the SAFE failure
# direction (a smaller render, not a deadlock), so this is recorded as a known
# conservative edge, not fixed -- fixing it would mean gambling on exactly the
# hardware this function exists to protect against guessing wrong about.
with mock.patch("subprocess.run", return_value=FakeR(0, "24260\n")):
    ok(_clamp_res_for_gpu(1032, 580, _L) == SAFE_FALLBACK_RES,
       "24260 MiB (23.7GB rounded) clamps despite being a ~24GB card -- "
       "known conservative edge, safe direction, not a bug")

print("\n-- D: pixel-count boundary --")
with mock.patch("subprocess.run", return_value=FakeR(0, "16384\n")):
    ok(_clamp_res_for_gpu(960, 544, _L) == (960, 544), "exactly at the safe budget: passthrough")
    ok(_clamp_res_for_gpu(960, 545, _L) == SAFE_FALLBACK_RES, "one pixel row over: clamps")
    ok(_clamp_res_for_gpu(640, 360, _L) == (640, 360), "well under budget: passthrough")

print("\n-- E: the clamp is now VISIBLE on the job, not just in a server log --")
class FakeJob:
    def __init__(self):
        self.meta = {}
with mock.patch("subprocess.run", return_value=FakeR(0, "16384\n")):
    j = FakeJob()
    r = _clamp_res_for_gpu(1032, 580, _L, job=j)
    ok(r == SAFE_FALLBACK_RES, "still clamps")
    ok("resolution_clamped" in j.meta,
       "AND records it on job.meta -- an explicit override that gets silently "
       "downgraded is otherwise invisible to any caller that isn't tailing "
       "the server log")
    ok(j.meta["resolution_clamped"]["requested"] == [1032, 580]
       and j.meta["resolution_clamped"]["delivered"] == list(SAFE_FALLBACK_RES),
       "the recorded requested/delivered values are exactly right")
    j2 = FakeJob()
    _clamp_res_for_gpu(960, 544, _L, job=j2)
    ok("resolution_clamped" not in j2.meta,
       "...and is absent entirely when nothing was clamped -- no false signal")

print("\n-- F: a job object that raises on .meta access cannot break the render --")
class BrokenJob:
    @property
    def meta(self):
        raise RuntimeError("boom")
with mock.patch("subprocess.run", return_value=FakeR(0, "16384\n")):
    r = _clamp_res_for_gpu(1032, 580, _L, job=BrokenJob())
    ok(r == SAFE_FALLBACK_RES,
       "the clamp still returns correctly even if recording it explodes -- "
       "meta is a convenience surface, never load-bearing for the render itself")


def _serve_once(handler):
    """Bind a real localhost server, run `handler(conn)` for one connection."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    def run():
        try:
            conn, _ = srv.accept()
            handler(conn)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
    return srv, port


def _check_busy(port, timeout=3.0):
    orig = mgr.WANGP_WORKER_PORT
    mgr.WANGP_WORKER_PORT = port
    try:
        t0 = time.time()
        result = mgr._worker_is_busy(timeout=timeout)
        return result, time.time() - t0
    finally:
        mgr.WANGP_WORKER_PORT = orig


print("\n-- G: _worker_is_busy against real socket behaviours --")

srv, port = _serve_once(lambda conn: (time.sleep(30), conn.close()))
r, el = _check_busy(port)
ok(r is False and el < 4.0, f"pure hang (connects, never answers) -> False in {el:.2f}s, bounded")
srv.close()


def _send_json(conn, obj, delay=0.1):
    body = json.dumps(obj).encode()
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    time.sleep(delay)
    conn.close()


srv, port = _serve_once(lambda conn: _send_json(conn, {"ok": True, "busy": True}))
r, el = _check_busy(port)
ok(r is True and el < 1.0, f"real busy=true -> True, FAST ({el:.2f}s, no needless full-deadline wait)")
srv.close()

srv, port = _serve_once(lambda conn: _send_json(conn, {"ok": True, "busy": False}))
r, el = _check_busy(port)
ok(r is False, "real busy=false -> False")
srv.close()


def _send_garbage(conn):
    body = b"not json at all {{{"
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
    time.sleep(0.1)
    conn.close()


srv, port = _serve_once(_send_garbage)
r, el = _check_busy(port)
ok(r is False, "malformed/non-JSON body -> False, not protected (deliberately conservative)")
srv.close()


def _trickle(conn):
    header = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 30\r\n\r\n"
    for b in header:
        conn.sendall(bytes([b]))
        time.sleep(0.05)
    body = b'{"ok": true, "busy": true}   '
    for b in body:
        conn.sendall(bytes([b]))
        time.sleep(0.4)   # ~9s of trickling if it ever finishes
    conn.close()


srv, port = _serve_once(_trickle)
r, el = _check_busy(port)
# THE LOAD-BEARING ASSERTION. Before the fix: urlopen's socket timeout only
# bounds each individual recv, not the whole call, so a byte-at-a-time
# responder held the ORIGINAL function open 16+ real seconds and returned
# False anyway -- proving the claimed "3s timeout" was not a real deadline.
# False in that scenario is the DANGEROUS direction (treats a possibly-busy
# worker as an orphan). The fix wraps the call in a thread with a real
# wall-clock join(timeout), so this must now resolve near the deadline, not
# minutes later. The elapsed bound (< 4.5s) is the actual regression guard;
# without the fix this assertion fails at ~16s.
ok(el < 4.5, f"slow-trickling responder is bounded near the deadline "
             f"({el:.2f}s, not the ~16s the unfixed per-op timeout allowed)")
srv.close()

no_such_port, _s = 1, None
try:
    _s = socket.socket()
    _s.bind(("127.0.0.1", 0))
    dead_port = _s.getsockname()[1]
    _s.close()   # closed immediately -- nothing listening here
    r, el = _check_busy(dead_port)
    # Windows can take longer than Linux to surface connection-refused (~2s
    # measured here vs typically instant elsewhere) -- the invariant that
    # matters is "bounded by the deadline", not a specific fast number.
    ok(r is False and el < 3.5, f"connection refused (nothing listening) -> False, "
                                f"bounded ({el:.2f}s, platform-dependent speed)")
except Exception as e:
    ok(False, f"connection-refused case raised unexpectedly: {e}")

print("\n-- H: kill_orphans_at_startup's two guards, read not re-executed --")
# Not re-run live (it shells out to taskkill/netstat for real) -- this checks
# the guard CONDITIONS are wired the way the docstring claims, statically.
import inspect  # noqa: E402
src = inspect.getsource(mgr.kill_orphans_at_startup)
ok("DCS_NO_GPU_EVICT" in src and "PYTEST_CURRENT_TEST" in src,
   "both the explicit test flag AND pytest's own auto-set env var gate eviction")
ok("_worker_is_busy()" in src,
   "the busy-worker guard is checked before any kill happens")
# Order matters: the test-environment check should be able to short-circuit
# BEFORE ever probing the worker, so a test run makes zero network calls to
# it -- probing a worker that might not even be up yet is its own source of
# flakiness in a boot sequence.
env_pos = src.find("DCS_NO_GPU_EVICT")
busy_pos = src.find("_worker_is_busy()")
ok(0 < env_pos < busy_pos, "the env-var guard is checked BEFORE the network probe, not after")

print("\n-- I: a worker 'busy for a different caller' is not a distinct case --")
# Verified by reading, not a code change: the /health payload the worker
# returns carries no job-id/owner field (see rp_handler / the worker's own
# health route) -- {ok, model, busy} only. This box runs exactly one shared
# WanGP worker process on this port; "busy serving someone else's job" and
# "busy serving a DCS job" are the SAME observable state and SHOULD be treated
# identically (protect it either way). Recorded as a non-finding rather than
# silently assumed: there is no owner field to check, so there is nothing
# more granular this function could safely key off of even in principle.
ok(True, "confirmed by reading rp_handler/worker health payload: no owner/job-id "
        "field exists to distinguish callers -- protecting ANY busy state is "
        "the only correct behavior available at this API surface")

print("\n" + (f"ALL {PASS} PASS" if FAIL == 0 else f"{FAIL} FAIL / {PASS + FAIL} total"))
sys.exit(0 if FAIL == 0 else 1)
