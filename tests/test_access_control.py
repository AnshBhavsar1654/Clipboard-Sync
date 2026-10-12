"""Who may reach which endpoint, history retention, and oversized frames."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server.auth import TrustStore
from server.main import hub, require_device


@pytest.fixture()
def locked_hub(tmp_path):
    store = TrustStore(tmp_path / "state.json")  # require_pin defaults True
    old_trust = hub._trust
    hub._trust = store
    yield hub
    hub._trust = old_trust


def _req(host="testserver", cookie="", client=("testclient", 50000)):
    headers = [(b"host", host.encode())]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": headers, "client": client, "query_string": b""})


@pytest.mark.asyncio
async def test_require_device_matrix(locked_hub):
    # Loopback (desktop app itself) always passes.
    await require_device(_req(client=("127.0.0.1", 50000)))
    # Open mode passes everyone.
    locked_hub._trust.require_pin = False
    await require_device(_req())
    locked_hub._trust.require_pin = True
    # No cookie -> 401, bad cookie -> 401.
    with pytest.raises(HTTPException) as e1:
        await require_device(_req())
    assert e1.value.status_code == 401
    with pytest.raises(HTTPException) as e2:
        await require_device(_req(cookie="cs_token=nope"))
    assert e2.value.status_code == 401
    # Fresh server token passes.
    token = locked_hub._trust.issue_token("Pixel")
    await require_device(_req(cookie=f"cs_token={token}"))
    # Revoked token fails again.
    locked_hub._trust.revoke_token(token)
    with pytest.raises(HTTPException):
        await require_device(_req(cookie=f"cs_token={token}"))


def test_store_retention_and_pins(tmp_path):
    from server.store import HISTORY_KEEP_ITEMS, HistoryStore

    store = HistoryStore(tmp_path / "h.db")
    try:
        for i in range(HISTORY_KEEP_ITEMS + 5):
            store.add({"id": f"c{i}", "device_id": "d", "timestamp": "",
                       "type": "text", "content": f"clip {i}"})
        assert len(store.load_recent(10000)) == HISTORY_KEEP_ITEMS
        assert store.load_recent(10000)[0]["content"] == "clip 5"

        # Age out: old unpinned clips go, old pinned clips stay.
        store._db.execute("UPDATE clips SET created = 1 WHERE id IN ('c5', 'c6')")
        store._db.commit()
        store.set_pinned("c6", True)
        store.add({"id": "fresh", "device_id": "d", "timestamp": "",
                   "type": "text", "content": "fresh"})
        ids = {i["id"] for i in store.load_recent(10000)}
        assert "c5" not in ids and "c6" in ids and "fresh" in ids
    finally:
        store.close()


@pytest.mark.asyncio
async def test_big_history_frame_does_not_break_client():
    """5 MB frames must reach on_message."""
    from client.websocket_client import WebSocketClient

    seen: list = []

    async def _on_message(msg):
        seen.append(msg)

    client = WebSocketClient(url="ws://127.0.0.1:9/ws", device_id="t", on_message=_on_message)
    big = json.dumps({"device_id": "x", "type": "history",
                      "items": [{"content": "A" * (5 * 1024 * 1024)}]})
    await client._handle_raw_message(big)
    assert len(seen) == 1 and len(seen[0]["items"][0]["content"]) == 5 * 1024 * 1024
