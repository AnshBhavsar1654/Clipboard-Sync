"""LAN auto-discovery: each PC beacons its presence so nearby PCs can find it.

Uses UDP broadcast (no internet needed). Payload is name/IP/port only —
never clipboard data. When UDP is blocked (guest Wi-Fi, firewall, VPN),
the GUI falls back to manual address + code entry.
"""

from __future__ import annotations

import json
import socket
import threading
import time

BEACON_PORT = 37020
BEACON_INTERVAL = 3.0
PEER_TIMEOUT = 12.0
MAGIC = "ClipBoardSync"
PROTOCOL_VERSION = 1


def get_pc_name() -> str:
    try:
        name = socket.gethostname() or "Computer"
    except Exception:
        name = "Computer"
    return name[:40]


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except Exception:
        try:
            return str(socket.gethostbyname(socket.gethostname()))
        except Exception:
            return "127.0.0.1"


class PeerDiscovery:
    """Broadcasts this PC and listens for other PCs on the same network."""

    def __init__(self, port: int = 8000, beacon_port: int = BEACON_PORT) -> None:
        self.port = port
        self.beacon_port = beacon_port
        self._peers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._running = False
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for target in (self._broadcast_loop, self._listen_loop):
            t = threading.Thread(target=target, name=f"discovery-{target.__name__}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._running = False

    def peers(self) -> list[dict]:
        now = time.time()
        with self._lock:
            out = [dict(v) for v in self._peers.values() if now - v.get("last_seen", 0) < PEER_TIMEOUT]
        return sorted(out, key=lambda p: p.get("name", ""))

    def _broadcast_loop(self) -> None:
        payload = json.dumps({
            "magic": MAGIC, "v": PROTOCOL_VERSION,
            "name": get_pc_name(), "port": self.port,
        }).encode("utf-8")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while self._running:
                try:
                    sock.sendto(payload, ("<broadcast>", self.beacon_port))
                except Exception:
                    pass
                for _ in range(int(BEACON_INTERVAL * 10)):
                    if not self._running:
                        break
                    time.sleep(0.1)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _listen_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", self.beacon_port))
            except OSError:
                return
            sock.settimeout(1.0)
            own_ip = get_lan_ip()
            while self._running:
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except Exception:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8", "ignore"))
                except Exception:
                    continue
                if msg.get("magic") != MAGIC or msg.get("v") != PROTOCOL_VERSION:
                    continue
                ip = addr[0]
                if ip == own_ip or ip in ("127.0.0.1", "::1"):
                    continue
                try:
                    port = int(msg.get("port", 8000))
                except (TypeError, ValueError):
                    continue
                name = str(msg.get("name", "Computer"))[:40] or "Computer"
                with self._lock:
                    self._peers[ip] = {"ip": ip, "port": port, "name": name, "last_seen": time.time()}
        finally:
            try:
                sock.close()
            except Exception:
                pass


def parse_beacon(data: bytes) -> dict | None:
    """Parse a beacon payload (unit-testable). Returns {name, port} or None."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    if msg.get("magic") != MAGIC or msg.get("v") != PROTOCOL_VERSION:
        return None
    try:
        port = int(msg.get("port", 8000))
    except (TypeError, ValueError):
        return None
    return {"name": str(msg.get("name", "Computer"))[:40], "port": port}
