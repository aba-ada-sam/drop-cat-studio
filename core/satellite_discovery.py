"""Find a satellite service on the LAN when its IP changes.

The 3060 satellite hosts ACE-Step / Ollama / Forge for the 5080 main machine.
Its DHCP IP can change after a router reboot. Instead of forcing a static
reservation, this module:

  1. Tries the cached/configured IP (fast path -- skipped on first miss).
  2. Tries hostname hints via the OS resolver (mDNS/NetBIOS -- Google Nest
     WiFi resolves device names like "study" on the local network).
  3. Sweeps the local /24 subnet concurrently for the service's health
     endpoint as a last resort (~2-5s).

A successful discovery is cached in-memory and also written back to config so
next launch starts at the fast path.

Only the satellite-shaped services (ACE-Step, Ollama, Forge) call this.
Discovery never runs when the configured host is localhost/127.0.0.1 -- that
means the user intends local-only and we should NOT auto-roam onto the LAN.
"""
from __future__ import annotations

import logging
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_HOSTNAME_HINTS = ["study", "study.local", "dcs-satellite", "dcs-satellite.local"]
SUBNET_PROBE_TIMEOUT = 0.8   # seconds per host
SUBNET_SWEEP_WORKERS = 64
HOSTNAME_PROBE_TIMEOUT = 2.0

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

# Cache: maps (port, path) -> last-known-good host
_cache: dict[tuple[int, str], str] = {}


def _is_local(host: str) -> bool:
    return (host or "").strip().lower() in _LOCAL_HOSTS


def _probe(host: str, port: int, path: str, timeout: float) -> bool:
    """Return True if GET http://host:port{path} returns HTTP 200."""
    if not host:
        return False
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _local_subnet() -> Optional[str]:
    """Return the local /24 prefix (e.g. '192.168.86') or None."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        # Doesn't actually connect -- just picks the outbound interface.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    except Exception:
        pass
    return None


def discover(
    port: int,
    health_path: str = "/health",
    cached_host: Optional[str] = None,
    hostname_hints: Optional[list[str]] = None,
    log_label: str = "satellite",
) -> Optional[str]:
    """Find a host serving GET http://host:port{health_path}.

    Order:
      1. In-memory cache for this (port, path)
      2. cached_host argument (the configured host)
      3. hostname_hints via OS resolver
      4. /24 subnet sweep

    Returns the host that worked, or None if nothing reachable.
    Side effect: caches the winner in this module's _cache dict.

    Discovery is skipped entirely when cached_host is a local loopback --
    the caller wants local-only.
    """
    if cached_host and _is_local(cached_host):
        return None  # caller wants local; nothing to discover

    key = (port, health_path)

    # 1. In-memory cache (verified)
    mem = _cache.get(key)
    if mem and _probe(mem, port, health_path, HOSTNAME_PROBE_TIMEOUT):
        return mem

    # 2. Configured/cached host from caller
    if cached_host and _probe(cached_host, port, health_path, HOSTNAME_PROBE_TIMEOUT):
        _cache[key] = cached_host
        return cached_host

    # 3. Hostname hints (Google Nest WiFi / mDNS / NetBIOS)
    for name in (hostname_hints or DEFAULT_HOSTNAME_HINTS):
        try:
            ip = socket.gethostbyname(name)
        except socket.gaierror:
            continue
        if _probe(ip, port, health_path, HOSTNAME_PROBE_TIMEOUT):
            log.info("[discovery:%s] resolved hostname '%s' -> %s", log_label, name, ip)
            _cache[key] = ip
            return ip

    # 4. Subnet sweep
    subnet = _local_subnet()
    if not subnet:
        log.warning("[discovery:%s] could not determine local subnet", log_label)
        return None

    # Probe own IP first cheaply; skip during sweep
    skip_self = ""
    try:
        skip_self = socket.gethostbyname(socket.gethostname())
    except Exception:
        pass

    candidates = [f"{subnet}.{i}" for i in range(1, 255) if f"{subnet}.{i}" != skip_self]
    log.info("[discovery:%s] sweeping %s.0/24 for port %d ...", log_label, subnet, port)

    found: Optional[str] = None
    with ThreadPoolExecutor(max_workers=SUBNET_SWEEP_WORKERS) as ex:
        futures = {ex.submit(_probe, c, port, health_path, SUBNET_PROBE_TIMEOUT): c for c in candidates}
        for fut in as_completed(futures):
            try:
                if fut.result():
                    found = futures[fut]
                    break
            except Exception:
                continue
        # Cancel remaining work
        for fut in futures:
            if not fut.done():
                fut.cancel()

    if found:
        log.info("[discovery:%s] subnet sweep found %s", log_label, found)
        _cache[key] = found
    else:
        log.warning("[discovery:%s] not reachable on %s.0/24", log_label, subnet)
    return found


def remember(port: int, health_path: str, host: str) -> None:
    """Manually populate the in-memory cache (e.g., when a service was found via
    the existing satellite relay on 9999)."""
    if host and not _is_local(host):
        _cache[(port, health_path)] = host


def forget(port: int, health_path: str) -> None:
    """Drop a cached host so the next call re-discovers."""
    _cache.pop((port, health_path), None)


def forget_all() -> None:
    _cache.clear()
