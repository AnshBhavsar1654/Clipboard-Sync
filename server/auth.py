"""Device trust store and PIN pairing for the local sync server.

New devices must enter a pairing PIN once. On success the device ID is
persisted and remembered, so reconnecting (e.g. re-scanning the QR code)
requires no PIN. State is stored in a JSON file so it survives restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILE = Path.home() / ".clipboardsync" / "server_state.json"

MAX_PIN_FAILS = 5
LOCKOUT_SECONDS = 300


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_loopback_host(host: str) -> bool:
    """True for loopback hosts (the desktop app's own socket is not a phone)."""
    return host in ("127.0.0.1", "::1", "localhost")


def _default_path() -> Path:
    return Path(os.environ.get("CLIPBOARDSYNC_STATE_FILE", str(DEFAULT_STATE_FILE)))


class TrustStore:
    """Thread-safe store of trusted device IDs, pairing PIN and security settings."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else _default_path()
        self._lock = threading.Lock()
        self._trusted: set[str] = set()
        self._devices: dict[str, dict[str, Any]] = {}
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._require_pin = True
        self._pin = self._generate()
        fresh = not self._path.exists()
        self._load()
        if fresh:
            self._save()  # persist the generated PIN so tools can read it

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            trusted = data.get("trusted_devices", [])
            self._trusted = {str(d) for d in trusted if isinstance(d, str)}
            devices = data.get("devices", {})
            if isinstance(devices, dict):
                self._devices = {
                    str(k): v for k, v in devices.items() if isinstance(v, dict)
                }
            self._require_pin = bool(data.get("require_pin", True))
            pin = str(data.get("pairing_pin", "")).strip()
            if len(pin) == 6 and pin.isdigit():
                self._pin = pin
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "trusted_devices": sorted(self._trusted),
                "devices": self._devices,
                "require_pin": self._require_pin,
                "pairing_pin": self._pin,
            }
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    # -- PIN brute-force throttling (keyed by client IP, survives reconnects)
    def locked_out(self, ip: str) -> bool:
        with self._lock:
            now = time.time()
            recent = [t for t in self._fails.get(ip, []) if now - t < LOCKOUT_SECONDS]
            self._fails[ip] = recent
            return len(recent) >= MAX_PIN_FAILS

    def record_fail(self, ip: str) -> None:
        with self._lock:
            self._fails[ip].append(time.time())

    def clear_fails(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    # -- device tokens (server-issued, stored as hashes) -------------------
    def issue_token(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._devices[_token_hash(token)] = {
                "name": (name or "Phone")[:40],
                "added": time.time(),
                "last_seen": time.time(),
            }
        self._save()
        return token

    def verify_token(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            entry = self._devices.get(_token_hash(token))
            if entry is None:
                return False
            entry["last_seen"] = time.time()
            return True

    def revoke_token(self, token: str) -> None:
        with self._lock:
            self._devices.pop(_token_hash(token), None)
        self._save()

    # -- pairing PIN -------------------------------------------------------
    @staticmethod
    def _generate() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    @property
    def pairing_pin(self) -> str:
        with self._lock:
            return self._pin

    def regenerate_pin(self) -> str:
        with self._lock:
            self._pin = self._generate()
            pin = self._pin
        self._save()
        return pin

    def validate_pin(self, pin: str) -> bool:
        with self._lock:
            return bool(pin) and pin == self._pin

    # -- require-pin policy ------------------------------------------------
    @property
    def require_pin(self) -> bool:
        with self._lock:
            return self._require_pin

    @require_pin.setter
    def require_pin(self, value: bool) -> None:
        with self._lock:
            self._require_pin = bool(value)
        self._save()

    # -- trusted devices ---------------------------------------------------
    def is_trusted(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._trusted

    def trust(self, device_id: str) -> None:
        with self._lock:
            self._trusted.add(device_id)
        self._save()

    def untrust(self, device_id: str) -> None:
        with self._lock:
            self._trusted.discard(device_id)
        self._save()

    def trusted_devices(self) -> list[str]:
        with self._lock:
            return sorted(self._trusted)


_store: TrustStore | None = None


def get_store() -> TrustStore:
    """Return the shared server trust store (lazily created)."""
    global _store
    if _store is None:
        _store = TrustStore()
    return _store
