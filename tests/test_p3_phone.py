"""Phase 3: server-assembled folders (§3.5), item endpoints, silence sweep."""

from __future__ import annotations

import pytest

from server.auth import TrustStore
from server.main import hub


@pytest.fixture()
def open_hub(tmp_path, monkeypatch):
    store = TrustStore(tmp_path / "state.json")
    store.require_pin = False
    old_trust = hub._trust
    hub._trust = store
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._auth_failures.clear()
    hub._hosts.clear()
    hub._devices.clear()
    hub._last_seen.clear()
    hub._pinned.clear()
    shared = tmp_path / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    old_uploads = hub.uploads_dir
    hub.uploads_dir = shared
    monkeypatch.setattr("server.main.UPLOADS_DIR", shared)
    yield hub
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._auth_failures.clear()
    hub._hosts.clear()
    hub._devices.clear()
    hub._last_seen.clear()
    hub._pinned.clear()
    hub._trust = old_trust
    hub.uploads_dir = old_uploads


def _stage(client, folder_id, files):
    for relpath, data in files:
        r = client.post(
            "/api/upload",
            files={"file": ("blob", data)},
            data={"folder_id": folder_id, "relpath": relpath},
        )
        assert r.status_code == 200, r.text
        assert r.json()["staged"] is True
    return folder_id


def test_folder_stage_and_commit(open_hub, client):
    fid = "ab12cd34ef56ab78"
    _stage(client, fid, [("a.txt", b"hello"), ("sub/b.txt", b"world")])
    r = client.post("/api/folder/commit", json={"folder_id": fid, "name": "Photos"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["type"] == "folder"
    assert data["filename"] == "Photos.zip"
    assert data["entry_count"] == 2
    assert data["url"].startswith("/api/files/")

    import zipfile

    name = data["url"].rsplit("/", 1)[-1]
    with zipfile.ZipFile(hub.uploads_dir / name) as zf:
        assert sorted(zf.namelist()) == ["a.txt", "sub/b.txt"]
        assert zf.read("a.txt") == b"hello"

    # Staging cleaned up.
    assert not (hub.uploads_dir / ".staging" / fid).exists()


def test_folder_commit_validates(open_hub, client):
    assert client.post("/api/folder/commit", json={"folder_id": "xyz"}).status_code == 404
    assert client.post("/api/folder/commit", json={"folder_id": "ab12cd34ef56ab78"}).status_code == 404
    assert client.post("/api/folder/commit", json={}).status_code in (404, 422)


def test_folder_stage_sanitizes_traversal(open_hub, client):
    fid = "ab12cd34ef56ab78"
    r = client.post(
        "/api/upload",
        files={"file": ("x", b"evil")},
        data={"folder_id": fid, "relpath": "../../evil.txt"},
    )
    assert r.status_code == 200
    assert ".." not in r.json()["relpath"]
    assert (hub.uploads_dir / "evil.txt").exists() is False
    staged = list((hub.uploads_dir / ".staging" / fid).rglob("*"))
    assert staged and all(".staging" in str(p) for p in staged)


def test_folder_commit_broadcasts_to_history(open_hub, client):
    fid = "ab12cd34ef56ab78"
    _stage(client, fid, [("a.txt", b"hi")])
    data = client.post("/api/folder/commit", json={"folder_id": fid}).json()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json(
            {"device_id": "phone", "type": "folder", "content": f"Folder: {data['filename']}",
             "filename": data["filename"], "filesize": data["filesize"],
             "file_url": data["url"], "entry_count": data["entry_count"]}
        )
    items = client.get("/api/history").json()["items"]
    assert items[-1]["filename"] == data["filename"]


def test_staging_swept_when_abandoned(open_hub):
    import os
    import time

    from server.store import sweep_staging

    old = hub.uploads_dir / ".staging" / "oldfolder12345678"
    old.mkdir(parents=True)
    (old / "a.txt").write_bytes(b"x")
    ancient = time.time() - 7200
    os.utime(old, (ancient, ancient))
    assert sweep_staging(hub.uploads_dir) == 1
    assert not old.exists()


def test_info_endpoint(open_hub, client):
    r = client.get("/api/info")
    assert r.status_code == 200
    assert r.json()["computer_name"]
    assert "version" in r.json()


def test_pin_endpoint_roundtrip(open_hub, client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"device_id": "d", "type": "text", "content": "pinnable"})
    clip_id = client.get("/api/history").json()["items"][-1]["id"]
    assert client.post(f"/api/history/{clip_id}/pin", json={"pinned": True}).json() == {"ok": True}
    assert hub.is_pinned(clip_id)
    assert client.post(f"/api/history/{clip_id}/pin", json={"pinned": False}).json() == {"ok": True}
    assert not hub.is_pinned(clip_id)


def test_client_id_echoed_not_stored(open_hub, client):
    with client.websocket_connect("/ws") as ws_a:
        ws_a.receive_json()
        with client.websocket_connect("/ws") as ws_b:
            ws_b.receive_json()
            ws_a.send_json({"device_id": "d", "type": "text", "content": "echo?",
                            "client_id": "c-abc123"})
            got = ws_b.receive_json()
            assert got["client_id"] == "c-abc123"
    stored = client.get("/api/history").json()["items"][-1]
    assert "client_id" not in stored


def test_ping_pong(open_hub, client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


@pytest.mark.asyncio
async def test_silent_sockets_closed(open_hub):
    import time

    class _Sock:
        def __init__(self, host):
            self.client = type("C", (), {"host": host})()
            self.closed = []

        async def accept(self):
            return None

        async def send_json(self, payload):
            return None

        async def close(self, code=1000):
            self.closed.append(code)

    remote, local = _Sock("192.168.1.9"), _Sock("127.0.0.1")
    await open_hub.connect(remote)
    await open_hub.connect(local)
    open_hub._last_seen[remote] = time.time() - 500
    open_hub._last_seen[local] = time.time() - 500
    try:
        assert await open_hub.close_silent_sockets(max_silent_seconds=90) == 1
        assert remote.closed == [1001]
        assert local.closed == []
        assert local in open_hub._connections
    finally:
        await open_hub.disconnect(remote)
        await open_hub.disconnect(local)
