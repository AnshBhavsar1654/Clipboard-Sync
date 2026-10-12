"""History persistence, device identity, private-clip handling and networking helpers."""

from __future__ import annotations

import pytest

from server.auth import TrustStore
from server.main import hub


@pytest.fixture()
def open_hub(tmp_path):
    store = TrustStore(tmp_path / "state.json")
    store.require_pin = False
    old_trust = hub._trust
    hub._trust = store
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._hosts.clear()
    hub._devices.clear()
    hub._pinned.clear()
    yield hub
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._hosts.clear()
    hub._devices.clear()
    hub._pinned.clear()
    hub._trust = old_trust


# -- history survives restarts -----------------------------------------


@pytest.mark.asyncio
async def test_history_persists_across_hub_restart(tmp_path):
    from server.hub import SyncHub
    from server.store import HistoryStore

    db = tmp_path / "history.db"
    hub1 = SyncHub(trust_store=TrustStore(tmp_path / "s.json"), store=HistoryStore(db))
    try:
        await hub1.handle_message(
            None, {"device_id": "d", "type": "text", "content": "remember me"}
        )
        assert len(hub1.get_history()) == 1
    finally:
        hub1._store.close()

    hub2 = SyncHub(trust_store=TrustStore(tmp_path / "s.json"), store=HistoryStore(db))
    try:
        assert [i["content"] for i in hub2.get_history()] == ["remember me"]
    finally:
        hub2._store.close()


@pytest.mark.asyncio
async def test_pin_remove_and_clear(open_hub, client):
    await hub.handle_message(None, {"device_id": "d", "type": "text", "content": "a"})
    await hub.handle_message(None, {"device_id": "d", "type": "text", "content": "b"})
    clip_id = hub.get_history()[0]["id"]
    await hub.set_pinned(clip_id, True)
    assert clip_id in hub._pinned

    assert await hub.remove_item("missing") is False
    assert await hub.remove_item(hub.get_history()[-1]["id"]) is True
    assert [i["content"] for i in hub.get_history()] == ["a"]

    await hub.clear_history()
    assert hub.get_history() == []
    assert hub._store.load_recent(10) == []


def test_history_delete_endpoints(open_hub, client):
    r = client.post("/api/pair", json={"pin": "x", "name": "n"})
    assert r.status_code in (401, 200)  # open mode: pair optional, must not crash
    assert client.get("/api/history").status_code == 200
    assert client.delete("/api/history/nope").status_code == 404
    assert client.delete("/api/history").json() == {"ok": True}


def test_janitor_sweep(tmp_path):
    from server.store import sweep_uploads

    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)  # conftest already creates hub.uploads_dir here
    (shared / "keep1234.pdf").write_bytes(b"keep")
    (shared / "orphan.bin").write_bytes(b"drop me")
    (shared / "stale.part").write_bytes(b"part")
    import os

    old = __import__("time").time() - 7200
    os.utime(shared / "stale.part", (old, old))
    (shared / "fresh.part").write_bytes(b"part")

    removed = sweep_uploads(shared, {"keep1234.pdf"})
    assert removed == 2
    assert (shared / "keep1234.pdf").is_file()
    assert (shared / "fresh.part").is_file()
    assert not (shared / "orphan.bin").exists()
    assert not (shared / "stale.part").exists()


# -- device identity ----------------------------------------------------


@pytest.mark.asyncio
async def test_hello_and_devices_list(open_hub, client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # history
        ws.send_json({"type": "hello", "device_id": "Pixel-1", "name": "Pixel 8", "kind": "phone"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"  # barrier: hello processed
        devices = hub.devices()
        assert len(devices) == 1
        assert devices[0]["id"] == "Pixel-1"
        assert devices[0]["name"] == "Pixel 8"
        assert devices[0]["kind"] == "phone"
        r = client.get("/api/devices")
        assert r.status_code == 200
        assert r.json()["connection_count"] == 1
    # Session exit runs finally-disconnect: no ghost entries.
    assert hub.devices() == []
    assert client.get("/api/devices").json()["connection_count"] == 0


def test_hello_ignored_when_unauthenticated(open_hub, tmp_path):
    import asyncio

    from server.hub import SyncHub

    async def _check():
        h = SyncHub(trust_store=TrustStore(tmp_path / "s.json"), store=__import__(
            "server.store", fromlist=["HistoryStore"]).HistoryStore(":memory:"))
        try:
            assert h._trust.require_pin is True
            fake = type("W", (), {"client": type("C", (), {"host": "10.0.0.9"})()})()
            await h.handle_message(fake, {"type": "hello", "device_id": "x"})
            assert h.devices() == []
        finally:
            h._store.close()

    asyncio.run(_check())


# -- privacy ---------------------------------------------------------------


def test_private_clip_skipped_by_default():
    from client.clipboard_backend import FakeClipboardBackend

    fake = FakeClipboardBackend()
    fake.write_text("s3cr3t")
    fake.simulate_private = True
    assert fake.read_payload() is None
    assert fake.read_payload(skip_private=False) == {"type": "text", "content": "s3cr3t"}


def test_monitor_skips_private_clips():
    import asyncio

    from client.clipboard import ClipboardMonitor
    from client.clipboard_backend import FakeClipboardBackend

    async def _check() -> None:
        loop = asyncio.get_running_loop()
        seen: list = []
        backend = FakeClipboardBackend()
        backend.write_text("password123")
        backend.simulate_private = True
        monitor = ClipboardMonitor(loop, seen.append, backend=backend)
        try:
            monitor._handle_clipboard_update()
            assert seen == []
            monitor._skip_private = False
            monitor._handle_clipboard_update()
            assert [p["content"] for p in seen] == ["password123"]
        finally:
            monitor.stop()

    asyncio.run(_check())


def test_owner_blocklist_matching():
    from client.clipboard_backend import owner_exe_blocked

    assert owner_exe_blocked("C:\\Program Files\\KeePassXC\\KeePassXC.exe")
    assert owner_exe_blocked("keepass.exe")
    assert not owner_exe_blocked("chrome.exe")
    assert not owner_exe_blocked("")


def test_privacy_prefs_roundtrip(tmp_path):
    from client.config import ReceivePrefs, load_prefs, save_prefs

    d = tmp_path / "cfg"
    assert load_prefs(d).share_private_clips is False
    save_prefs(d, ReceivePrefs(share_private_clips=True, extra_blocked_apps=["Vault.exe"]))
    loaded = load_prefs(d)
    assert loaded.share_private_clips is True
    assert loaded.extra_blocked_apps == ["Vault.exe"]


# -- network ---------------------------------------------------------------


def test_find_open_port_skips_busy():
    import socket

    from server.net import find_open_port

    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("0.0.0.0", 0))  # wildcard: conflicts with the probe's bind
    start = busy.getsockname()[1]
    try:
        assert find_open_port(start, count=5) != start
    finally:
        busy.close()


def test_primary_lan_ip_is_ipv4():
    import re

    from server.net import lan_ips, primary_lan_ip

    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", primary_lan_ip())
    assert isinstance(lan_ips(), list)


def test_single_instance_first_call():
    from server.net import ensure_single_instance

    assert ensure_single_instance("ClipBoardSyncTestProbeUnique") is True
