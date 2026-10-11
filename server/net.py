"""LAN networking helpers (plan §2.12): IP selection, free-port probe,
single-instance guard. No new dependencies (stdlib only)."""

from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)

_VIRTUAL_HINTS = (
    "vethernet", "wsl", "vmware", "virtualbox", "tailscale", "hamachi",
    "zerotier", "vpn", "docker", "hyper-v", "loopback", "teredo", "isatap",
    "bluetooth", "vbox",
)


def _is_private_ipv4(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    return (
        a == 10
        or (a == 172 and 16 <= b <= 31)
        or (a == 192 and b == 168)
    )


def _route_ip() -> str:
    """IP of the interface owning the default route (may be a VPN exit)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except Exception:
        return ""


def lan_ips() -> list[str]:
    """Candidate LAN IPv4 addresses, best first.

    The default-route IP comes first; link-local (169.254.x) and loopback are
    dropped. Adapter-name filtering needs platform APIs, so virtual-adapter
    exclusion is best-effort via interface names where available.
    """
    candidates: list[str] = []
    route = _route_ip()
    if route and not route.startswith("169.254."):
        candidates.append(route)
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if family != socket.AF_INET:
                continue
            ip = sockaddr[0]
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip not in candidates:
                candidates.append(ip)
    except Exception as exc:
        logger.debug("LAN enumeration failed: %s", exc)
    try:
        names = " ".join(n for _, n in socket.if_nameindex()).lower()
        if any(h in names for h in _VIRTUAL_HINTS):
            logger.debug("Virtual adapters present; keeping physical-range IPs first.")
    except Exception:
        pass
    candidates.sort(key=lambda ip: (not _is_private_ipv4(ip), ip))
    if route in candidates:
        candidates.remove(route)
        candidates.insert(0, route)
    return candidates


def primary_lan_ip() -> str:
    """Best guess for the address phones should open (plan §2.12)."""
    ips = lan_ips()
    if ips:
        return ips[0]
    try:
        return str(socket.gethostbyname(socket.gethostname()))
    except Exception:
        return "127.0.0.1"


def find_open_port(start: int = 8000, count: int = 11) -> int:
    """First bindable TCP port in [start, start+count) (plan §2.12).

    The old code died with sys.exit(1) inside a worker thread when 8000 was
    busy and the UI just stopped; now we roll to the next free port.
    """
    for port in range(start, start + count):
        # Plain bind (no SO_REUSEADDR): a busy port must fail here. TIME_WAIT
        # ports are skipped too — harmless, the server sets its own options.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    return start


_instance_mutex = None


def ensure_single_instance(name: str = "ClipBoardSyncSingleton") -> bool:
    """True if this is the only running copy (plan §2.12).

    Windows: a Global named mutex. Anywhere else: always True (no enforcement).
    The handle is kept for the process lifetime on purpose.
    """
    global _instance_mutex
    try:
        import sys as _sys

        if _sys.platform != "win32":
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        handle = kernel32.CreateMutexW(None, False, f"Global\\{name}")
        if not handle:
            return True  # could not create: don't block startup
        _instance_mutex = handle
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True
