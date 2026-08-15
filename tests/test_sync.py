"""Integration test suite for ClipBoardSync server, history cache, and WebSocket broadcasting."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.auth import TrustStore
from server.hub import SyncHub
from server.main import app, hub


@pytest.fixture(autouse=True)
def reset_server_hub(tmp_path):
    """Reset the in-memory sync hub and isolate the trust store between tests."""
    store = TrustStore(tmp_path / "server_state.json")
    store.require_pin = False  # legacy tests assume open mode
    hub._trust = store
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._auth_failures.clear()
    yield
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._auth_failures.clear()
    hub._trust.require_pin = True


def test_rest_endpoints_and_static() -> None:
    client = TestClient(app)

    # Test static web app HTML served at root
    response = client.get("/")
    assert response.status_code == 200
    assert "ClipBoardSync" in response.text

    # Test initial empty history REST API
    hist_response = client.get("/api/history")
    assert hist_response.status_code == 200
    data = hist_response.json()
    assert data["items"] == []
    assert data["connection_count"] == 0


def test_websocket_broadcast_and_history_onboarding() -> None:
    client = TestClient(app)

    # Connect Device A (e.g. Windows Laptop)
    with client.websocket_connect("/ws") as ws_a:
        initial_a = ws_a.receive_json()
        assert initial_a["type"] == "history"
        assert initial_a["items"] == []

        # Connect Device B (e.g. Mobile Phone)
        with client.websocket_connect("/ws") as ws_b:
            initial_b = ws_b.receive_json()
            assert initial_b["type"] == "history"
            assert initial_b["items"] == []

            # Device A copies text and transmits over WebSocket
            clip_payload = {
                "device_id": "Desktop-Laptop",
                "type": "text",
                "content": "Hello across devices!",
            }
            ws_a.send_json(clip_payload)

            # Device B should immediately receive the real-time broadcast
            broadcast_b = ws_b.receive_json()
            assert broadcast_b["type"] == "text"
            assert broadcast_b["device_id"] == "Desktop-Laptop"
            assert broadcast_b["content"] == "Hello across devices!"
            assert "id" in broadcast_b

        # Connect Device C later after copy occurred
        with client.websocket_connect("/ws") as ws_c:
            initial_c = ws_c.receive_json()
            assert initial_c["type"] == "history"
            assert len(initial_c["items"]) == 1
            assert initial_c["items"][0]["content"] == "Hello across devices!"


def test_consecutive_deduplication() -> None:
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws_1:
        ws_1.receive_json()  # consume initial history

        # Send same clip content twice consecutively
        msg1 = {"device_id": "Device-1", "type": "text", "content": "Duplicate Clip"}
        msg2 = {"device_id": "Device-2", "type": "text", "content": "Duplicate Clip"}

        ws_1.send_json(msg1)
        ws_1.send_json(msg2)

    # Inspect server history cache
    response = client.get("/api/history")
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["content"] == "Duplicate Clip"


def test_file_upload_endpoint() -> None:
    client = TestClient(app)
    files = {"file": ("test_doc.txt", b"Sample file content for cross device sync", "text/plain")}
    response = client.post("/api/upload", files=files)
    assert response.status_code == 200
    data = response.json()
    assert data["filename"] == "test_doc.txt"
    assert data["type"] == "file"
    assert "url" in data


def test_trust_store_persistence(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = TrustStore(path)
    store.trust("Device-A")
    pin = store.regenerate_pin()
    store.require_pin = True

    assert store.is_trusted("Device-A")
    assert store.validate_pin(pin)
    assert not store.validate_pin("000000")

    reloaded = TrustStore(path)
    assert reloaded.is_trusted("Device-A")
    assert reloaded.pairing_pin == pin
    assert reloaded.require_pin is True

    reloaded.untrust("Device-A")
    assert not reloaded.is_trusted("Device-A")


def test_new_device_requires_pin_and_is_remembered() -> None:
    client = TestClient(app)
    hub._trust.require_pin = True

    with client.websocket_connect("/ws") as ws:
        # The client always identifies itself on open; an untrusted device is asked for a PIN
        ws.send_json({"type": "auth_request", "device_id": "Phone-ABC"})
        msg = ws.receive_json()
        assert msg["type"] == "auth_required"

        # Wrong PIN -> auth_error
        ws.send_json({"type": "auth_pin", "device_id": "Phone-ABC", "pin": "000000"})
        err = ws.receive_json()
        assert err["type"] == "auth_error"

        # Correct PIN -> auth_success then empty history
        ws.send_json({"type": "auth_pin", "device_id": "Phone-ABC", "pin": hub._trust.pairing_pin})
        ok = ws.receive_json()
        assert ok["type"] == "auth_success"
        hist = ws.receive_json()
        assert hist["type"] == "history"
        assert hist["items"] == []

        # Device is now remembered in the trust store
        assert hub._trust.is_trusted("Phone-ABC")

        # Authenticated device can broadcast normally
        ws.send_json({"type": "text", "device_id": "Phone-ABC", "content": "from phone"})

    # Reconnecting from the same device (e.g. re-scanning the QR code) needs no PIN
    with client.websocket_connect("/ws") as ws2:
        ws2.send_json({"type": "auth_request", "device_id": "Phone-ABC"})
        ok2 = ws2.receive_json()
        assert ok2["type"] == "auth_success"
        hist2 = ws2.receive_json()
        assert hist2["type"] == "history"
        assert len(hist2["items"]) == 1
        assert hist2["items"][0]["content"] == "from phone"


def test_trusted_device_skips_pin() -> None:
    client = TestClient(app)
    hub._trust.require_pin = True
    hub._trust.trust("Phone-XYZ")

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth_request", "device_id": "Phone-XYZ"})
        ok = ws.receive_json()
        assert ok["type"] == "auth_success"
        hist = ws.receive_json()
        assert hist["type"] == "history"


def test_open_mode_skips_pin() -> None:
    client = TestClient(app)
    hub._trust.require_pin = False

    with client.websocket_connect("/ws") as ws:
        initial = ws.receive_json()
        assert initial["type"] == "history"
        assert initial["items"] == []

