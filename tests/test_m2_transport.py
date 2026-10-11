"""M2 part 1: metadata-only socket (plan §2.2) + Ctrl+V promise (plan §2.7)."""

from __future__ import annotations

import io

import pytest

from server.auth import TrustStore
from server.main import hub


def _png_bytes(size=(64, 64), color=(200, 30, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


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
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.main.UPLOADS_DIR", tmp_path / "shared")
    yield hub
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._auth_failures.clear()
    hub._hosts.clear()
    hub._trust = old_trust


# -- server: upload thumbs ------------------------------------------------------


def test_upload_returns_thumb_for_images(open_hub, client):
    c = client
    r = c.post(
        "/api/upload",
        files={"file": ("shot.png", _png_bytes(), "image/png")},
        data={"item_type": "image"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "image"
    assert data["thumb"].startswith("data:image/jpeg;base64,")
    assert len(data["thumb"]) <= 24 * 1024 + 100


def test_upload_has_no_thumb_for_plain_files(open_hub, client):
    c = client
    r = c.post("/api/upload", files={"file": ("a.txt", b"hi")})
    assert r.status_code == 200
    assert "thumb" not in r.json()


def test_make_thumb_rejects_non_images():
    from server.media import make_thumb

    assert make_thumb(b"definitely not an image") is None
    thumb = make_thumb(_png_bytes((800, 600)))
    assert thumb is not None and thumb.startswith("data:image/jpeg;base64,")


# -- server: oversize socket payloads dropped ------------------------------------


def test_hub_drops_oversize_text(open_hub, client):
    c = client
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"device_id": "d", "type": "text", "content": "x" * (300 * 1024)})
        ws.send_json({"device_id": "d", "type": "text", "content": "small"})
    items = c.get("/api/history").json()["items"]
    assert [i["content"] for i in items] == ["small"]


# -- client: image shares carry metadata, not pixels ------------------------------


def _make_app(tmp_path, device_id="pc-1"):
    """App under test with a fake clipboard (never touch the real one)."""
    from client.clipboard import ClipboardMonitor
    from client.clipboard_backend import FakeClipboardBackend
    from client.config import Config
    from client.main import ClipBoardSyncApp

    config = Config(
        websocket_url="ws://127.0.0.1:8000/ws",
        device_id=device_id,
        config_dir=tmp_path / "cfg",
    )
    app_ = ClipBoardSyncApp(config)
    orig = app_._clipboard
    app_._clipboard = ClipboardMonitor(
        app_._loop, app_._on_local_clipboard_change, backend=FakeClipboardBackend()
    )
    orig.stop()
    return app_


@pytest.mark.asyncio
async def test_share_image_sends_metadata_not_pixels(tmp_path, monkeypatch):
    app_ = _make_app(tmp_path)
    sent: list = []

    async def _fake_upload(data, filename, item_type):
        assert len(data) > 0
        return {
            "url": "/api/files/ab12cd34ef56.png",
            "filename": filename,
            "filesize": len(data),
            "thumb": "data:image/jpeg;base64,AAA",
        }

    async def _capture(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(app_, "_upload_bytes", _fake_upload)
    monkeypatch.setattr(app_._websocket, "send_clipboard_update", _capture)
    try:
        ok = await app_._share_image_bytes(_png_bytes(), "shot.png")
    finally:
        app_._clipboard.stop()
    assert ok
    assert len(sent) == 1
    msg = sent[0]
    assert msg["content_type"] == "image"
    assert msg["file_url"].endswith("/api/files/ab12cd34ef56.png")
    assert msg["thumb"] == "data:image/jpeg;base64,AAA"
    assert "base64" not in msg["content"]  # small label, not pixels


@pytest.mark.asyncio
async def test_shared_file_url_stays_relative(tmp_path, monkeypatch):
    """Shared file_urls must be relative: absolutizing against this computer's
    loopback address makes them unreachable (and origin-mismatched, so the
    phone falls back to the tiny thumbnail) on every other device."""
    app_ = _make_app(tmp_path)
    sent: list = []
    src = tmp_path / "a.bin"
    src.write_bytes(b"012")

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"url": "/api/files/ab12cd34ef56.bin", "filename": "a.bin", "filesize": 3}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            return _FakeResp()

    async def _capture(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(app_._websocket, "send_clipboard_update", _capture)
    try:
        assert await app_._upload_path(str(src), "a.bin", "file") is True
        assert await app_._share_image_bytes(b"012", "a.png") is True
    finally:
        app_._clipboard.stop()
    assert len(sent) == 2
    for msg in sent:
        assert msg["file_url"] == "/api/files/ab12cd34ef56.bin"
        assert "127.0.0.1" not in msg["file_url"]


# -- client: incoming shares land on the clipboard ---------------------------------


@pytest.mark.asyncio
async def test_remote_photo_lands_on_clipboard(tmp_path, monkeypatch):
    from pathlib import Path

    app_ = _make_app(tmp_path)
    blob = _png_bytes()
    saved = tmp_path / "shot.png"
    saved.write_bytes(blob)

    async def _fake_download(_message):
        return Path(saved)

    monkeypatch.setattr(app_, "_download_shared_file", _fake_download)
    try:
        await app_._apply_remote_item(
            {"device_id": "phone", "type": "image", "content": "Image: shot.png",
             "filename": "shot.png", "filesize": len(blob),
             "file_url": "/api/files/ab12cd34ef56.png"}
        )
        payload = app_._clipboard._backend.read_payload()
    finally:
        app_._clipboard.stop()
    assert payload is not None and payload["type"] == "image"
    assert payload["image_bytes"] == blob


@pytest.mark.asyncio
async def test_remote_file_lands_on_clipboard_as_hdrop(tmp_path, monkeypatch):
    from pathlib import Path

    app_ = _make_app(tmp_path)
    saved = tmp_path / "Report.pdf"
    saved.write_bytes(b"%PDF fake")

    async def _fake_download(_message):
        return Path(saved)

    monkeypatch.setattr(app_, "_download_shared_file", _fake_download)
    try:
        await app_._apply_remote_item(
            {"device_id": "phone", "type": "file", "content": "File: Report.pdf",
             "filename": "Report.pdf", "filesize": 9,
             "file_url": "/api/files/ab12cd34ef56.pdf"}
        )
        writes = app_._clipboard._backend.writes
    finally:
        app_._clipboard.stop()
    assert writes and writes[-1][0] == "files"
    assert writes[-1][1] == [str(Path(saved))]


@pytest.mark.asyncio
async def test_auto_receive_cap_skips_big_files(tmp_path, monkeypatch):
    from client.config import ReceivePrefs

    app_ = _make_app(tmp_path)
    app_._prefs = ReceivePrefs(auto_receive_max_bytes=10)

    async def _boom(_message):
        raise AssertionError("must not download over the cap")

    monkeypatch.setattr(app_, "_download_shared_file", _boom)
    try:
        await app_._apply_remote_item(
            {"device_id": "phone", "type": "file", "content": "File: big.zip",
             "filename": "big.zip", "filesize": 10**9,
             "file_url": "/api/files/ab12cd34ef56.zip"}
        )
    finally:
        app_._clipboard.stop()


# -- client: prefs -----------------------------------------------------------------


def test_prefs_roundtrip_and_bad_file(tmp_path):
    from client.config import ReceivePrefs, load_prefs, save_prefs

    d = tmp_path / "cfg"
    assert load_prefs(d).put_received_on_clipboard is True  # defaults
    save_prefs(d, ReceivePrefs(receive_folder="X", auto_receive_max_bytes=123,
                               put_received_on_clipboard=False))
    loaded = load_prefs(d)
    assert (loaded.receive_folder, loaded.auto_receive_max_bytes,
            loaded.put_received_on_clipboard) == ("X", 123, False)
    (d / "prefs.json").write_text("{broken", encoding="utf-8")
    assert load_prefs(d).auto_receive_max_bytes == 50 * 1024 * 1024
