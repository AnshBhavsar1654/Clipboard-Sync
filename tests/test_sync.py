"""Integration test suite for ClipBoardSync server, history cache, and WebSocket broadcasting."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.hub import SyncHub
from server.main import app, hub


@pytest.fixture(autouse=True)
def reset_server_hub():
    """Reset the in-memory sync hub between tests for isolation."""
    hub._connections.clear()
    hub._history.clear()
    yield
    hub._connections.clear()
    hub._history.clear()


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
