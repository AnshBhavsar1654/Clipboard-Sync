"""A restart must not re-apply clips that were already in the stored history."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from client.config import Config
from client.main import ClipBoardSyncApp


def _iso(offset: float) -> str:
    return datetime.fromtimestamp(time.time() + offset, UTC).isoformat()


@pytest.mark.asyncio
async def test_history_frame_only_applies_clips_newer_than_this_session(tmp_path):
    config = Config(websocket_url="ws://127.0.0.1:8000/ws", device_id="me", config_dir=tmp_path)
    app_ = ClipBoardSyncApp(config)
    applied: list[str] = []

    async def fake_apply(item: dict) -> None:
        applied.append(item["content"])

    app_._apply_remote_item = fake_apply  # type: ignore[method-assign]
    try:
        old = {"type": "text", "device_id": "Phone-A", "content": "old", "timestamp": _iso(-3600)}
        await app_._on_remote_clipboard_update({"type": "history", "items": [old]})
        assert applied == []  # stored before we started: ignore

        await asyncio.sleep(0.05)
        new = {"type": "text", "device_id": "Phone-A", "content": "while-offline", "timestamp": _iso(0)}
        await app_._on_remote_clipboard_update({"type": "history", "items": [old, new]})
        assert applied == ["while-offline"]  # arrived after the floor: apply once

        await asyncio.sleep(0.05)
        await app_._on_remote_clipboard_update({"type": "history", "items": [old, new]})
        assert applied == ["while-offline"]  # replay of the same frame: floor moved on
    finally:
        app_._clipboard.stop()
