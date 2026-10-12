"""Desktop app: admin API, pause, host guard, shell helpers, tray and hotkey."""

from __future__ import annotations

import pytest

import server.admin as admin
from server.auth import TrustStore
from server.main import hub


@pytest.fixture()
def admin_hub(tmp_path, monkeypatch):
    store = TrustStore(tmp_path / "state.json")
    old_trust = hub._trust
    hub._trust = store
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._hosts.clear()
    hub._devices.clear()
    hub._last_seen.clear()
    hub._pinned.clear()
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.main.UPLOADS_DIR", tmp_path / "shared")
    monkeypatch.setattr("server.admin.client_ip", lambda req: "127.0.0.1")
    admin.configure(8123)
    yield hub
    hub._trust = old_trust


def _t():
    return {"t": admin.admin_token()}


# -- gate ------------------------------------------------------------------------


def test_admin_gate(admin_hub, client):
    assert client.get("/api/admin/state").status_code == 403
    assert client.get("/api/admin/state", params={"t": "wrong"}).status_code == 403
    r = client.get("/api/admin/state", params=_t())
    assert r.status_code == 200
    body = r.json()
    for key in ("version", "engine_running", "port", "pairing_url",
                "pairing_pin", "require_pin", "connection_count", "devices",
                "phones", "peer", "nearby", "history", "settings"):
        assert key in body, key


def test_admin_rejects_remote(admin_hub, client, monkeypatch):
    monkeypatch.setattr("server.admin.client_ip", lambda req: "192.168.1.9")
    assert client.get("/api/admin/state", params=_t()).status_code == 403


# -- pairing + devices --------------------------------------------------------------


def test_admin_pairing_rotate_and_remove(admin_hub, client):
    first = client.post("/api/admin/pairing/new", params=_t()).json()["pin"]
    second = client.post("/api/admin/pairing/new", params=_t()).json()["pin"]
    assert first != second and len(second) == 6

    assert client.delete("/api/admin/devices/nobody", params=_t()).status_code == 404

    token = admin_hub._trust.issue_token("Pixel")
    phones = client.get("/api/admin/state", params=_t()).json()["phones"]
    assert any(p["name"] == "Pixel" for p in phones)
    pid = next(p["id"] for p in phones if p["name"] == "Pixel")
    assert client.delete(f"/api/admin/devices/{pid}", params=_t()).json() == {"ok": True}
    assert not admin_hub._trust.verify_token(token)


def test_admin_settings_roundtrip(admin_hub, client):
    body = {"receive_folder": "X", "auto_receive_max_bytes": 1048576,
            "put_received_on_clipboard": False, "share_private_clips": True,
            "require_pin": False}
    assert client.post("/api/admin/settings", params=_t(), json=body).json() == {"ok": True}
    settings = client.get("/api/admin/state", params=_t()).json()["settings"]
    assert settings["receive_folder"] == "X"
    assert settings["auto_receive_max_bytes"] == 1048576
    assert settings["put_received_on_clipboard"] is False
    assert settings["share_private_clips"] is True
    assert settings["require_pin"] is False


def test_admin_engine_control(admin_hub, client):
    calls: list = []
    admin.set_engine_controller(lambda: calls.append("start"),
                                lambda: calls.append("stop"),
                                lambda: False)
    try:
        assert client.post("/api/admin/engine", params=_t(), json={"action": "stop"}).json() == {
            "ok": True, "running": False}
        assert client.post("/api/admin/engine", params=_t(), json={"action": "start"}).json() == {
            "ok": True, "running": True}
        assert calls == ["stop", "start"]
    finally:
        admin.set_engine_controller(None, None, None)


def test_admin_qr_and_diagnostics(admin_hub, client):
    qr = client.get("/api/admin/qr.png", params=_t())
    assert qr.status_code == 200
    assert qr.headers["content-type"] == "image/png"
    assert qr.content[:8] == b"\x89PNG\r\n\x1a\n"
    diag = client.get("/api/admin/diagnostics", params=_t()).json()
    for key in ("port", "port_free", "lan_ips", "connection_count",
                "history_items", "shared_files", "shared_bytes", "version"):
        assert key in diag, key


def test_admin_share_files(admin_hub, client, tmp_path):
    admin_hub._trust.require_pin = False  # read history back ungated
    target = tmp_path / "hello.txt"
    target.write_text("shared-by-shell")
    r = client.post("/api/admin/share", params=_t(), json={"paths": [str(target), str(tmp_path / "missing")]})
    assert r.status_code == 200, r.text
    assert r.json()["shared"] == [{"filename": "hello.txt", "type": "file"}]
    items = client.get("/api/history").json()["items"]
    assert items and items[-1]["filename"] == "hello.txt"


# -- desktop static ------------------------------------------------------------------


def test_desktop_page_served(admin_hub, client):
    html = client.get("/desktop/").text
    assert 'id="search"' in html and 'id="list"' in html and 'id="drawer"' in html
    assert 'id="connect-dialog"' in html and 'style="' not in html  # CSP: no inline styles
    assert "<script>" not in html  # no inline scripts (CSP script-src 'self')
    assert 'src="desktop.js"' in html
    assert client.get("/desktop/desktop.js").status_code == 200
    assert client.get("/desktop/desktop.css").status_code == 200


# -- shell pieces (no windows opened here) ----------------------------------------------


def test_engine_device_id_and_apply_settings(tmp_path):
    from gui.engine import BackgroundEngine

    engine = BackgroundEngine(port=8123)
    assert isinstance(engine.device_id, str) and engine.device_id
    engine.apply_settings()  # no client yet: must not raise


def test_shell_api_safe_without_engine():
    from gui.shell import ShellApi

    api = ShellApi(engine=type("E", (), {"_client_app": None, "_loop": None})(),
                   get_window=lambda w: None, get_last_hwnd=lambda: 0)
    assert api.save_item("", "x")["ok"] is False
    assert api.save_item("http://evil.example/api/files/ab12cd34", "x")["ok"] is False
    assert api.save_item("javascript:alert", "x")["ok"] is False
    assert api.open_url("file:///c:/windows/system32/calc.exe") is False
    assert api.pick_files() == []
    assert api.share_clipboard_now() is False
    assert api.paste_to_last_app("hi") is False
    api.close_window()
    api.quit()  # engine.stop on a dummy must not raise


def test_tray_builds_without_display():
    from gui.tray import _load_icon, build_menu

    assert _load_icon().size[0] > 0
    menu = build_menu({"open": lambda: None, "connect": lambda: None,
                       "pause": lambda: None, "resume": lambda: None, "quit": lambda: None})
    assert len(list(menu)) == 7  # 5 actions + 2 separators


def test_hotkey_lifecycle():
    from gui.hotkey import last_hotkey_hwnd, start_hotkey

    assert last_hotkey_hwnd() == 0
    thread = start_hotkey(lambda: None)
    try:
        import sys

        if sys.platform == "win32":
            assert thread is not None
    finally:
        if thread is not None:
            thread.stop()


# -- pause + identity ---------------------------------------------------------------


def test_pause_drops_remote_clips_but_keeps_server(admin_hub, client):
    admin.set_engine_controller(lambda: setattr(admin_hub, "paused", False),
                                lambda: setattr(admin_hub, "paused", True),
                                lambda: not admin_hub.paused)
    admin_hub._trust.require_pin = False
    try:
        client.post("/api/admin/engine", params=_t(), json={"action": "stop"})
        assert client.get("/api/admin/state", params=_t()).json()["engine_running"] is False
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # history
            ws.send_json({"type": "text", "content": "while paused", "device_id": "Phone-A"})
            assert ws.receive_json()["type"] == "paused"
        assert admin_hub.get_history() == []
        client.post("/api/admin/engine", params=_t(), json={"action": "start"})
        assert client.get("/api/admin/state", params=_t()).json()["engine_running"] is True
    finally:
        admin_hub.paused = False
        admin.set_engine_controller(None, None, None)


def test_state_reports_identity(admin_hub, client):
    admin.set_identity("pc-123")
    try:
        body = client.get("/api/admin/state", params=_t()).json()
        assert body["this_device_id"] == "pc-123" and body["computer_name"]
    finally:
        admin.set_identity("This computer")


def test_settings_listener_fires(admin_hub, client):
    calls: list = []
    admin.set_settings_listener(lambda: calls.append(1))
    try:
        client.post("/api/admin/settings", params=_t(), json={"receive_folder": "Y"})
        assert calls == [1]
    finally:
        admin.set_settings_listener(None)


# -- DNS-rebinding guard -------------------------------------------------------------


def test_host_allowlist():
    from server.main import _host_allowed

    for ok in ("127.0.0.1:8000", "localhost", "[::1]:8000", "192.168.1.5:8000", "desk.local:8000"):
        assert _host_allowed(ok), ok
    for bad in ("evil.example", "evil.example:8000", "", "127.0.0.1.evil.example"):
        assert not _host_allowed(bad), bad


def test_loopback_with_foreign_host_is_refused():
    import asyncio

    from server.main import RebindingGuard

    sent: list = []

    async def inner(scope, receive, send):
        sent.append("reached-app")

    async def run(host, client_host):
        out: list = []

        async def send(msg):
            out.append(msg)

        scope = {"type": "http", "client": (client_host, 5555), "headers": [(b"host", host)]}
        await RebindingGuard(inner)(scope, None, send)
        return out

    refused = asyncio.run(run(b"evil.example:8000", "127.0.0.1"))
    assert refused[0]["status"] == 403 and not sent
    asyncio.run(run(b"127.0.0.1:8000", "127.0.0.1"))
    asyncio.run(run(b"anything.example", "192.168.1.9"))  # LAN callers are token-gated instead
    assert sent == ["reached-app", "reached-app"]
