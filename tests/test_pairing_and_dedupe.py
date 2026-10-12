"""Pairing gate on the HTTP API, filename handling, clip dedupe and the fake clipboard.

Covers: /api/history and /api/upload need pairing; pairing is throttled per IP;
Unicode filenames keep their extension; this computer's own socket is not
counted as a phone; two long clips with a common prefix are both synced.
"""

from __future__ import annotations

import pytest

from server.auth import TrustStore
from server.main import hub


@pytest.fixture()
def locked_hub(tmp_path, monkeypatch):
    """Isolate the global hub with a PIN-required trust store."""
    store = TrustStore(tmp_path / "state.json")  # require_pin defaults True
    old_trust = hub._trust
    hub._trust = store
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    monkeypatch.setattr("server.main.UPLOADS_DIR", tmp_path / "shared")
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    yield hub
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._trust = old_trust


def test_history_requires_pairing(locked_hub, client):
    assert client.get("/api/history").status_code in (401, 403)


def test_upload_requires_pairing(locked_hub, client):
    r = client.post("/api/upload", files={"file": ("a.txt", b"hi")})
    assert r.status_code in (401, 403)


def test_unicode_filename_keeps_extension():
    from server.main import _sanitize_filename

    assert _sanitize_filename("रिपोर्ट.pdf").endswith(".pdf")
    assert _sanitize_filename("写真.jpg").endswith(".jpg")


class _FakeSocket:
    """Minimal WebSocket double for SyncHub.connect (no network)."""

    def __init__(self, host: str):
        self.client = type("C", (), {"host": host})()
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        return None


@pytest.mark.asyncio
async def test_local_client_not_counted_as_phone(locked_hub):
    locked_hub._trust.require_pin = False  # open mode: auto-approve like desktop app
    local = _FakeSocket("127.0.0.1")
    await locked_hub.connect(local)
    try:
        # Desired: the desktop app's own 127.0.0.1 socket is not a "phone".
        assert locked_hub.connection_count == 0
    finally:
        await locked_hub.disconnect(local)


def test_pair_then_history_and_upload(locked_hub, client):
    """The gate blocks strangers but lets a paired phone through."""
    c = client
    assert c.get("/api/history").status_code == 401
    r = c.post("/api/pair", json={"pin": locked_hub._trust.pairing_pin, "name": "Pixel"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert c.get("/api/history").status_code == 200
    up = c.post("/api/upload", files={"file": ("a.txt", b"hi")})
    assert up.status_code == 200


def test_pair_wrong_pin_rejected_then_throttled(locked_hub, client):
    c = client
    for _ in range(5):
        assert c.post("/api/pair", json={"pin": "000000"}).status_code == 401
    assert c.post("/api/pair", json={"pin": "000000"}).status_code == 429


def test_dedupe_uses_full_content_hash():
    """Two 112-char URLs sharing a 100-char prefix must NOT collide."""
    from client.clipboard_backend import fingerprint_payload

    prefix = "https://example.com/" + "a" * 80  # exactly 100 chars
    assert len(prefix) == 100
    url_a = prefix + "AAAAAAAAAAAA"  # 112 chars
    url_b = prefix + "BBBBBBBBBBBB"  # 112 chars, same first 100 chars
    assert len(url_a) == len(url_b) == 112

    # A prefix-only key would collide.
    assert url_a[:100] == url_b[:100]

    # The full-content hash tells them apart.
    fa = fingerprint_payload({"type": "text", "content": url_a})
    fb = fingerprint_payload({"type": "text", "content": url_b})
    assert fa != fb
    # Same content is stable.
    assert fingerprint_payload({"type": "text", "content": url_a}) == fa


def test_fake_clipboard_backend_roundtrip():
    """FakeClipboard works on CI without Win32."""
    from client.clipboard_backend import FakeClipboardBackend

    fake = FakeClipboardBackend()
    assert fake.read_payload() is None
    fake.write_text("hello")
    assert fake.read_text() == "hello"
    assert fake.read_payload() == {"type": "text", "content": "hello"}
    assert fake.sequence_number() == 1
    fake.write_image_bytes(b"\x89PNG fake bytes")
    payload = fake.read_payload()
    assert payload is not None and payload["type"] == "image"
    assert fake.sequence_number() == 2


def test_clipboard_monitor_uses_backend_without_win32():
    """ClipboardMonitor imports and works with FakeClipboard on any OS."""
    import asyncio

    from client.clipboard import ClipboardMonitor
    from client.clipboard_backend import FakeClipboardBackend

    async def _check() -> None:
        loop = asyncio.get_running_loop()
        seen: list = []
        monitor = ClipboardMonitor(
            loop, lambda p: seen.append(p), backend=FakeClipboardBackend()
        )
        try:
            monitor.set_text("via backend")
            assert monitor._backend.read_text() == "via backend"
        finally:
            monitor.stop()

    asyncio.run(_check())
