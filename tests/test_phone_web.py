"""Phone page end-to-end (plan §5.3): pair, send text, upload a file.

Needs `pip install playwright && playwright install chromium`.
Skipped when Playwright is unavailable. Run:
    pytest tests/test_phone_web.py -v
"""

from __future__ import annotations

import threading
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    import uvicorn

    from server.auth import TrustStore
    from server.main import UPLOADS_DIR, app, hub

    store = TrustStore(tmp_path / "state.json")
    pin = store.regenerate_pin()
    old_trust, old_uploads = hub._trust, hub.uploads_dir
    hub._trust = store
    shared = tmp_path / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.main.UPLOADS_DIR", shared)
    hub.uploads_dir = shared

    config = uvicorn.Config(app, host="127.0.0.1", port=8137, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            import httpx

            if httpx.get("http://127.0.0.1:8137/api/health", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    yield {"base": "http://127.0.0.1:8137", "pin": pin}
    server.should_exit = True
    thread.join(timeout=15)
    hub._trust = old_trust
    hub.uploads_dir = old_uploads
    assert UPLOADS_DIR  # silence linters about the import


def test_phone_pair_send_upload(live_server):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(live_server["base"] + "/")
            page.fill("#pin-input", live_server["pin"])
            page.click("#pin-submit")
            page.wait_for_selector("#app-body:not([hidden])", timeout=15000)
            page.fill("#send-input", "hello from playwright")
            page.click("#send-btn")
            page.wait_for_selector(".clip-card", timeout=15000)
            assert "hello from playwright" in page.content()
        finally:
            browser.close()
