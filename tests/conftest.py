"""Global test isolation: the hub persists history to SQLite (plan §2.9), so
every test gets a throwaway store — never the real app database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.main import app, hub
from server.store import HistoryStore


def _reset_hub_state() -> None:
    hub._connections.clear()
    hub._history.clear()
    hub._auth.clear()
    hub._auth_failures.clear()
    hub._hosts.clear()
    hub._devices.clear()
    hub._last_seen.clear()
    hub._pinned.clear()


@pytest.fixture()
def client():
    """Single-portal TestClient shared by the whole test.

    A bare TestClient(app) gives every websocket_connect() its OWN portal /
    event loop, so hub broadcasts become cross-loop sends through anyio
    memory streams — racy (broadcasts randomly never arrive). Entering the
    client once makes all sessions share one loop: deterministic.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_hub_store(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    old_store = hub._store
    old_uploads = hub.uploads_dir
    hub._store = store
    hub.uploads_dir = tmp_path / "shared"
    hub.uploads_dir.mkdir(parents=True, exist_ok=True)
    _reset_hub_state()
    yield
    _reset_hub_state()
    hub._store = old_store
    hub.uploads_dir = old_uploads
    store.close()
