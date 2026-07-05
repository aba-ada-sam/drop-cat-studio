"""GPU/VRAM orchestrator -- one tool at a time, automatically.

Andrew's GPU has 16GB VRAM. WanGP (8-13GB), ACE-Step (6-8GB), and Forge (4-6GB)
cannot coexist; loading two at once forces the loser into CPU offloading mode
(model on system RAM, 30s/step instead of <1s -- the "never finishes" symptom).

The uncensored LLM is no longer a GPU service here: it runs on Featherless
(cloud) by default, so it never competes for local VRAM. (A user-run local
KoboldCpp is an external server this orchestrator does not manage.)

This orchestrator enforces strict serialization. Pipelines say "I need WanGP"
and the orchestrator evicts whatever else is on the GPU first. A service holds
the GPU until something else acquires -- consecutive same-service jobs share
the loaded model without paying the load cost again.

Usage:
    from core.gpu_orchestrator import gpu

    gpu.acquire("wangp", reason="multi-clip 1/5")
    # ... do WanGP work ...
    gpu.acquire("acestep", reason="audio gen")  # auto-evicts WanGP
    # ... do ACE-Step work ...

Status:
    GET /api/gpu/status   -> { current, history }
"""

import json
import logging
import threading
import time
import urllib.request
from contextlib import contextmanager
from typing import Literal, Optional

log = logging.getLogger("gpu_orchestrator")

ServiceName = Literal["wangp", "acestep", "forge"]
_ALL_SERVICES: tuple[ServiceName, ...] = ("wangp", "acestep", "forge")


class GPUBusyError(RuntimeError):
    """Raised by acquire() when taking the GPU would evict an actively-rendering
    WanGP video job. Callers should surface a 'video is rendering' message rather
    than kill the render. Pass force=True to acquire() to override."""


def _is_remote(service: ServiceName) -> bool:
    """Return True if this service is configured to run on another machine.

    Remote services manage their own GPU -- no local eviction needed.
    """
    try:
        from core import config as _cfg
        if service == "acestep":
            h = (_cfg.get("acestep_host") or "localhost").strip().lower()
            return h not in ("localhost", "127.0.0.1", "")
        if service == "forge":
            u = (_cfg.get("forge_url") or "").lower()
            return "localhost" not in u and "127.0.0.1" not in u and u != ""
    except Exception:
        pass
    return False


_IDLE_EVICT_SECS = 1800  # 30 min -- release GPU services when nothing has run for this long


class GPUOrchestrator:
    """Single source of truth for which service owns the GPU right now.

    Thread-safe via RLock so reentrant acquire (same service) is cheap.
    Idle eviction: if no acquire() is called for _IDLE_EVICT_SECS, all GPU
    services are released so their VRAM + RAM is returned to the OS.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: Optional[ServiceName] = None
        self._transitions: list[dict] = []
        self._max_history = 50
        self._last_acquire: float = 0.0
        threading.Thread(target=self._idle_eviction_loop, daemon=True,
                         name="gpu-idle-evict").start()

    # -- public api ---------------------------------------------------------

    @property
    def current(self) -> Optional[ServiceName]:
        with self._lock:
            return self._current

    def status(self) -> dict:
        with self._lock:
            return {
                "current": self._current,
                "history": list(self._transitions[-20:]),
            }

    def _idle_eviction_loop(self) -> None:
        """Background thread: release GPU services after prolonged idle.

        Skips eviction if WanGP is actively generating (busy=True from /status).
        A 20-min clip at 25 steps would otherwise get killed mid-generation.
        """
        while True:
            time.sleep(60)
            with self._lock:
                if not (self._current is not None
                        and self._last_acquire > 0
                        and time.time() - self._last_acquire > _IDLE_EVICT_SECS):
                    continue
                # Don't evict if WanGP is actively generating
                if self._current == "wangp" and self._wangp_busy():
                    log.debug("[gpu] Idle eviction deferred -- WanGP is still generating")
                    self._last_acquire = time.time()  # reset clock, check again in 30 min
                    continue
                idle_min = int((time.time() - self._last_acquire) / 60)
                log.info("[gpu] Idle eviction after %d min -- releasing %s to free VRAM/RAM",
                         idle_min, self._current)
                self.release_all()

    def _wangp_busy(self) -> bool:
        """Return True if WanGP reports an active generation in progress."""
        try:
            with urllib.request.urlopen("http://127.0.0.1:7899/status", timeout=2) as r:
                return json.loads(r.read()).get("busy", False)
        except Exception:
            return False  # unreachable = idle

    def is_wangp_rendering(self) -> bool:
        """True if WanGP holds the GPU and is actively generating a video."""
        if not self._is_alive("wangp"):
            return False
        return self._wangp_busy()

    def acquire(self, service: ServiceName, reason: str = "", force: bool = False) -> None:
        """Ensure `service` owns the GPU exclusively. Evicts everyone else.

        Idempotent when `service` already holds. If the holder process died
        out-of-band (crash, OOM kill), this transparently restarts it.

        Remote services (another machine) manage their own GPU -- no eviction.

        Refuses to evict an actively-rendering WanGP (raises GPUBusyError) unless
        force=True. This stops a manual Forge/image-gen start from killing a
        running video job and leaving it hung (the "frozen render" bug). Internal
        pipeline transitions (e.g. acestep after the video phase) run when WanGP
        is already idle, so they are unaffected.
        """
        self._last_acquire = time.time()
        if _is_remote(service):
            log.debug("[gpu] acquire %s -- remote, skipping local eviction", service)
            return
        with self._lock:
            if self._current == service:
                if not self._is_alive(service):
                    log.info("[gpu] holder %s is dead -- restarting", service)
                    self._start(service)
                return

            # Don't yank the GPU out from under a live render.
            if (not force and service != "wangp"
                    and self._is_alive("wangp") and self._wangp_busy()):
                log.warning("[gpu] refused: acquiring %s would interrupt an active "
                            "WanGP render (use force=True to override)", service)
                raise GPUBusyError(
                    f"WanGP is rendering -- refusing to start '{service}' and "
                    f"interrupt the video. Wait for it to finish or cancel it."
                )

            prev = self._current
            t0 = time.time()

            for other in _ALL_SERVICES:
                if other != service and self._is_alive(other):
                    log.info("[gpu] evict %s (acquiring %s)", other, service)
                    self._stop(other)

            log.info("[gpu] acquire %s%s (was: %s)",
                     service, f" -- {reason}" if reason else "", prev or "none")
            self._start(service)
            self._current = service

            self._transitions.append({
                "from": prev,
                "to": service,
                "reason": reason,
                "ms": int((time.time() - t0) * 1000),
                "at": time.time(),
            })
            if len(self._transitions) > self._max_history:
                self._transitions = self._transitions[-self._max_history:]

    @contextmanager
    def using(self, service: ServiceName, reason: str = ""):
        """Context manager form. Does NOT release on exit -- the holder stays
        loaded until something else calls acquire. Consecutive same-service
        work shares the loaded model."""
        self.acquire(service, reason=reason)
        yield

    def release_all(self) -> None:
        """Force-evict every GPU service. Use when DCS should sit idle and
        leave VRAM completely free (e.g. user is doing other GPU work)."""
        with self._lock:
            for s in _ALL_SERVICES:
                if self._is_alive(s):
                    log.info("[gpu] release_all: stopping %s", s)
                    self._stop(s)
            self._current = None

    # -- per-service plumbing ----------------------------------------------

    def _is_alive(self, service: ServiceName) -> bool:
        try:
            if service == "wangp":
                from services.manager import wangp_worker_alive
                return wangp_worker_alive()
            if service == "acestep":
                from services.manager import acestep_alive
                return acestep_alive()
            if service == "forge":
                from services.forge_client import forge_alive
                return bool(forge_alive())
        except Exception as e:
            log.debug("[gpu] alive check %s failed: %s", service, e)
        return False

    def _start(self, service: ServiceName) -> None:
        try:
            if service == "wangp":
                from services.manager import start_wangp_worker, wangp_worker_alive
                if not wangp_worker_alive():
                    start_wangp_worker()
                return
            if service == "acestep":
                from services.manager import start_acestep, acestep_alive
                if not acestep_alive():
                    start_acestep()
                return
            if service == "forge":
                from services.forge_client import forge_alive, reload_checkpoint
                if not forge_alive():
                    # Auto-start Forge -- same deferred pattern as ACE-Step.
                    # start_forge() blocks until the API is ready (up to 5 min).
                    # Called from a thread via asyncio.to_thread in the route
                    # handler so it doesn't stall the event loop.
                    from services.manager import start_forge
                    log.info("[gpu] Forge not running -- auto-starting...")
                    start_forge()
                else:
                    try:
                        reload_checkpoint()
                    except Exception as e:
                        log.debug("[gpu] forge reload skipped: %s", e)
                return
        except Exception as e:
            log.warning("[gpu] start %s failed: %s", service, e)

    def _stop(self, service: ServiceName) -> None:
        try:
            if service == "wangp":
                from services.manager import stop_service
                stop_service("wangp")
                return
            if service == "acestep":
                from services.manager import stop_service
                stop_service("acestep")
                return
            if service == "forge":
                # Don't kill Forge (external process); just unload its model.
                from services.forge_client import unload_checkpoint
                unload_checkpoint()
                return
        except Exception as e:
            log.warning("[gpu] stop %s failed: %s", service, e)


# Singleton
gpu = GPUOrchestrator()
