"""Security and robustness: URL validation, cookie auth, cross-site writes, safe file names,
clipboard echo suppression, and the folder-zip path."""

from __future__ import annotations

import pytest

from server.auth import TrustStore
from server.main import STATIC_DIR, UPLOADS_DIR, hub


def _patch_uploads_dir(tmp_path, monkeypatch):
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.main.UPLOADS_DIR", tmp_path / "shared")


@pytest.fixture()
def locked_hub(tmp_path, monkeypatch):
    store = TrustStore(tmp_path / "state.json")  # require_pin defaults True
    old_trust = hub._trust
    hub._trust = store
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._hosts.clear()
    _patch_uploads_dir(tmp_path, monkeypatch)
    yield hub
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._hosts.clear()
    hub._trust = old_trust


@pytest.fixture()
def open_hub(tmp_path, monkeypatch):
    store = TrustStore(tmp_path / "state.json")
    store.require_pin = False
    old_trust = hub._trust
    hub._trust = store
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._hosts.clear()
    _patch_uploads_dir(tmp_path, monkeypatch)
    yield hub
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._hosts.clear()
    hub._trust = old_trust


# -- file_url validation -------------------------------------------


def test_hub_drops_untrusted_file_url(open_hub, client):
    c = client
    with c.websocket_connect("/ws") as ws_a:
        ws_a.receive_json()
        with c.websocket_connect("/ws") as ws_b:
            ws_b.receive_json()
            ws_a.send_json(
                {"device_id": "evil", "type": "file", "content": "File: x",
                 "filename": "x.bat", "file_url": "javascript:alert(1)"}
            )
            ws_a.send_json(
                {"device_id": "evil", "type": "file", "content": "File: y",
                 "filename": "y.exe", "file_url": "http://evil.example/payload.exe"}
            )
            ws_a.send_json(
                {"device_id": "friend", "type": "file", "content": "File: ok.pdf",
                 "filename": "ok.pdf", "filesize": 3, "file_url": "/api/files/ab12cd34ef56.pdf"}
            )
    items = c.get("/api/history").json()["items"]
    urls = [i.get("file_url") for i in items]
    assert "javascript:alert(1)" not in urls
    assert "http://evil.example/payload.exe" not in urls
    assert "/api/files/ab12cd34ef56.pdf" in urls


def test_is_allowed_file_url_shapes():
    from server.hub import is_allowed_file_url

    assert is_allowed_file_url("/api/files/ab12cd34ef56.pdf")
    assert is_allowed_file_url("/api/files/ab12cd34ef56.pdf?filename=x.pdf")
    assert not is_allowed_file_url("javascript:alert(1)")
    assert not is_allowed_file_url("http://evil.example/x")
    assert not is_allowed_file_url("/uploads/evil.bat")
    assert not is_allowed_file_url("")


# -- WS cookie auth --------------------------------------------------


def test_websocket_cookie_auth_skips_pin(locked_hub, client):
    c = client
    r = c.post("/api/pair", json={"pin": locked_hub._trust.pairing_pin, "name": "Pixel"})
    assert r.status_code == 200
    with c.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "history"  # auto-approved via cookie, no PIN screen


# -- cross-site write rejection --------------------------------------


def test_cross_site_post_blocked(locked_hub, client):
    c = client
    evil = c.post(
        "/api/pair",
        json={"pin": "000000"},
        headers={"Origin": "http://evil.example", "Host": "testserver"},
    )
    assert evil.status_code == 403
    # Safe methods still pass through to the real handler.
    ok = c.get("/api/history", headers={"Origin": "http://evil.example", "Host": "testserver"})
    assert ok.status_code == 401


# -- uploads outside the static mount --------------------------------


def test_uploads_not_served_from_static(client):
    assert UPLOADS_DIR.resolve() not in STATIC_DIR.resolve().parents
    assert UPLOADS_DIR.resolve() != STATIC_DIR.resolve()
    c = client
    assert c.get("/uploads/anything.bat").status_code == 404


# -- no 1 MiB client receive limit ------------------------------------


def test_websocket_client_has_no_size_limit():
    from client.websocket_client import WebSocketClient

    async def _noop(_msg):
        return None

    client = WebSocketClient(url="ws://127.0.0.1:9999/ws", device_id="t", on_message=_noop)
    assert client._connect_kwargs()["max_size"] is None


# -- safe_name / unique_path ------------------------------------------


def test_safe_name_blocks_traversal_and_keeps_unicode():
    from server.filenames import safe_name

    assert safe_name("../../Startup/x.bat") == "x.bat"
    assert safe_name("..\\..\\x.bat") == "x.bat"
    assert safe_name("CON.txt") == "_CON.txt"
    assert safe_name("") == "file"
    assert safe_name("...") == "file"
    assert safe_name("रिपोर्ट.pdf").endswith(".pdf")
    assert safe_name("写真.jpg") == "写真.jpg"
    assert "/" not in safe_name("a/b") and "\\" not in safe_name("a\\b")


def test_unique_path_stays_inside_and_dedupes(tmp_path):
    from server.filenames import unique_path

    d = tmp_path / "dl"
    d.mkdir()
    first = unique_path(d, "../../evil.bat")
    assert first.resolve().parent == d.resolve()
    first.write_bytes(b"x")
    second = unique_path(d, "../../evil.bat")
    assert second.name == "evil (1).bat"
    assert second.resolve().parent == d.resolve()


# -- monitor dedupe + echo suppression ----------------------------


def _make_monitor():
    import asyncio

    from client.clipboard import ClipboardMonitor
    from client.clipboard_backend import FakeClipboardBackend

    loop = asyncio.new_event_loop()
    seen: list = []

    def _collector(payload):
        seen.append(payload)

    monitor = ClipboardMonitor(loop, _collector, backend=FakeClipboardBackend())
    return loop, monitor, seen


def test_monitor_syncs_prefix_collisions():
    loop, monitor, seen = _make_monitor()
    try:
        prefix = "https://example.com/" + "a" * 80
        assert len(prefix) == 100
        # Direct backend writes model external user copies (no own-write
        # bookkeeping), so both must sync despite the shared 100-char prefix.
        monitor._backend.write_text(prefix + "AAAAAAAAAAAA")
        monitor._handle_clipboard_update()
        monitor._backend.write_text(prefix + "BBBBBBBBBBBB")
        monitor._handle_clipboard_update()
        assert [p["content"] for p in seen] == [prefix + "AAAAAAAAAAAA", prefix + "BBBBBBBBBBBB"]
    finally:
        monitor.stop()
        loop.close()


def test_monitor_suppresses_own_writes_but_not_user_copies():
    loop, monitor, seen = _make_monitor()
    try:
        monitor.set_text("hello from phone")
        monitor._handle_clipboard_update()  # our echo -> suppressed
        assert seen == []
        monitor._backend.write_text("user typed this")  # external copy
        monitor._handle_clipboard_update()
        assert [p["content"] for p in seen] == ["user typed this"]
    finally:
        monitor.stop()
        loop.close()


def test_seq_wrap_compare():
    from client.clipboard import _seq_is_newer

    assert _seq_is_newer(2, 1)
    assert not _seq_is_newer(1, 1)
    assert not _seq_is_newer(1, 2)
    assert _seq_is_newer(0, 0xFFFFFFFF)  # 32-bit wrap
    assert not _seq_is_newer(0xFFFFFFFF, 0)


# -- foreign absolute file hosts rejected ---------------------


@pytest.mark.asyncio
async def test_client_rejects_foreign_file_host(tmp_path):
    from client.config import Config
    from client.main import ClipBoardSyncApp

    config = Config(
        websocket_url="ws://127.0.0.1:8000/ws",
        device_id="test-device",
        config_dir=tmp_path,
    )
    app_ = ClipBoardSyncApp(config)
    try:
        assert app_._resolve_file_url("/api/files/ab12cd34.pdf") == (
            "http://127.0.0.1:8000/api/files/ab12cd34.pdf"
        )
        assert app_._resolve_file_url("http://127.0.0.1:8000/api/files/ab12.pdf") == (
            "http://127.0.0.1:8000/api/files/ab12.pdf"
        )
        assert app_._resolve_file_url("http://evil.example/api/files/ab12.pdf") is None
        assert app_._resolve_file_url("javascript:alert(1)") is not None  # relative join; hub drops it
    finally:
        app_._clipboard.stop()


# -- download failure path must not crash (F821 regression) -------------------------


@pytest.mark.asyncio
async def test_download_failure_is_logged_not_raised(tmp_path, monkeypatch):
    from client.config import Config
    from client.main import ClipBoardSyncApp

    class _Resp:
        status_code = 404

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aiter_bytes(self, _n):
            if False:
                yield b""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *a, **k):
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    config = Config(
        websocket_url="ws://127.0.0.1:8000/ws",
        device_id="test-device",
        config_dir=tmp_path / "cfg",
    )
    app_ = ClipBoardSyncApp(config)
    try:
        await app_._download_shared_file(
            {"type": "file", "filename": "../../evil.bat",
             "file_url": "/api/files/ab12cd34ef56"}
        )  # must not raise
    finally:
        app_._clipboard.stop()


# -- folder zip runs off the loop --------------------------------------


@pytest.mark.asyncio
async def test_folder_share_zips_off_loop(tmp_path, monkeypatch):
    import asyncio

    from client.config import Config
    from client.main import ClipBoardSyncApp

    folder = tmp_path / "Photos"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("hello")

    config = Config(
        websocket_url="ws://127.0.0.1:8000/ws",
        device_id="test-device",
        config_dir=tmp_path / "cfg",
    )
    app_ = ClipBoardSyncApp(config)
    calls: list = []

    async def _fake_upload(path, filename, item_type, entry_count=None, skipped_count=None):
        calls.append((filename, item_type, entry_count))
        assert asyncio.get_running_loop() is app_._loop
        return True

    monkeypatch.setattr(app_, "_upload_path", _fake_upload)
    try:
        await app_._on_local_clipboard_change(
            {"type": "folder", "content": "Folder: Photos", "filename": "Photos",
             "filepath": str(folder)}
        )
    finally:
        app_._clipboard.stop()
    assert calls and calls[0][0] == "Photos.zip" and calls[0][1] == "folder"
    assert calls[0][2] == 1
