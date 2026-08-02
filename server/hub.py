"""WebSocket connection manager and clipboard history sync hub."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from fastapi import WebSocket

from server.models import ClipboardItem, get_utc_now_iso

logger = logging.getLogger(__name__)


class SyncHub:
    """
    Manages real-time WebSocket communication and clipboard history across devices.

    Maintains a list of active WebSocket connections, broadcasts incoming clip
    events to all connected devices except the sender, and delivers recent clipboard
    history to newly onboarding clients.
    """

    def __init__(self, max_history: int = 25) -> None:
        self.max_history = max_history
        self._connections: set[WebSocket] = set()
        self._history: list[ClipboardItem] = []
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def get_history(self) -> list[dict[str, Any]]:
        """Return serialized recent clipboard items."""
        return [item.to_message_dict() for item in self._history]

    async def connect(self, websocket: WebSocket) -> None:
        """Register a new WebSocket connection and send initial clipboard history."""
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

        history_payload = {
            "type": "history",
            "device_id": "server",
            "timestamp": get_utc_now_iso(),
            "items": self.get_history(),
        }
        try:
            await websocket.send_json(history_payload)
            logger.info("Client connected. Active connections: %d", len(self._connections))
        except Exception as exc:
            logger.warning("Failed to send history to new connection: %s", exc)
            await self.disconnect(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        """Unregister a disconnected WebSocket connection."""
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("Client disconnected. Active connections: %d", len(self._connections))

    async def handle_message(self, sender: WebSocket, message: dict[str, Any]) -> None:
        """Process an incoming clipboard event and broadcast to other connected peers."""
        content_type = message.get("type", "text")
        content = message.get("content")
        device_id = message.get("device_id", "unknown")

        if content_type != "text" or not isinstance(content, str):
            logger.warning("Received invalid or unsupported message format: %s", message)
            return

        item = ClipboardItem(
            device_id=device_id,
            timestamp=message.get("timestamp") or get_utc_now_iso(),
            type=content_type,
            content=content,
        )

        async with self._lock:
            # Prevent repetitive consecutive duplicates in history feed
            if not self._history or self._history[-1].content != item.content:
                self._history.append(item)
                if len(self._history) > self.max_history:
                    self._history.pop(0)

            peers = [ws for ws in self._connections if ws != sender]

        payload = item.to_message_dict()
        logger.info("Broadcasting clipboard clip (%d chars) from device '%s' to %d peers", len(content), device_id, len(peers))

        for ws in peers:
            try:
                await ws.send_json(payload)
            except Exception as exc:
                logger.warning("Error broadcasting to peer: %s", exc)
                await self.disconnect(ws)
