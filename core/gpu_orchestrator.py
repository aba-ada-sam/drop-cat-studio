"""GPU/VRAM orchestrator -- one tool at a time, automatically.

Andrew's GPU has 16GB VRAM. WanGP (8-13GB), ACE-Step (6-8GB), Forge (4-6GB),
and Ollama (4-8GB) cannot coexist; loading two at once forces the loser into
CPU offloading mode (model on system RAM, 30s/step instead of <1s -- the
"never finishes" symptom).

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

ServiceName = Literal["wangp", "acestep", "forge", "ollama"]
_ALL_SERVICES: tuple[ServiceName, ...] = ("wangp", "acestep", "forge", "ollama")


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
        """Background thread: release GPU services after prolonged idle."""
        while True:
            time.sleep(60)
            with self._lock:
                if (self._current is not None
                        and self._last_acquire > 0
                        and time.time() - self._last_acquire > _IDLE_EVICT_SECS):
                    idle_min = int((time.time() - self._last_acquire) / 60)
                    log.info("[gpu] Idle eviction after %d min -- releasing %s to free VRAM/RAM",
                             idle_min, self._current)
                    self.release_all()

    def acquire(self, service: ServiceName, reason: str = "") -> None:
        """Ensure `service` owns the GPU exclusively. Evicts everyone else.

        Idempotent when `service` already holds. If the holder process died
        out-of-band (crash, OOM kill), this transparently restarts it.
        """
        with self._lock:
            self._last_acquire = time.time()
            if self._current == service:
                if not self._is_alive(service):
                    log.info("[gpu] holder %s is dead -- restarting", service)
                    self._start(service)
                return

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
            if service == "ollama":
                from services.manager import ollama_alive
                return ollama_alive()
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
                # Forge is external (separate launch). If it's down we cannot
                # auto-start; just attempt a checkpoint reload so the next call
                # works.
                from services.forge_client import forge_alive, reload_checkpoint
                if forge_alive():
                    try:
                        reload_checkpoint()
                    except Exception as e:
                        log.debug("[gpu] forge reload skipped: %s", e)
                return
            if service == "ollama":
                # Ollama auto-starts on first request; nothing to pre-warm.
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
            if service == "ollama":
                self._ollama_unload()
                return
        except Exception as e:
            log.warning("[gpu] stop %s failed: %s", service, e)

    def _ollama_unload(self) -> None:
        """Force Ollama to drop loaded models by issuing keep_alive=0 pings."""
        try:
            with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=2) as r:
                running = json.loads(r.read()).get("models", [])
        except Exception as e:
            log.debug("[gpu] ollama /api/ps failed: %s", e)
            return
        for m in running:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            try:
                body = json.dumps({"model": name, "keep_alive": 0}).encode()
                req = urllib.request.Request(
                    "http://localhost:11434/api/generate",
                    data=body, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3).read()
                log.info("[gpu] ollama unload: %s", name)
            except Exception as e:
                log.debug("[gpu] ollama unload %s failed: %s", name, e)


# Singleton
gpu = GPUOrchestrator()
