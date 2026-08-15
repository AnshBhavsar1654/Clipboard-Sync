"""WebSocket connection manager and clipboard history sync hub."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from fastapi import WebSocket

from server.auth import TrustStore, get_store
from server.models import ClipboardItem, get_utc_now_iso

logger = logging.getLogger(__name__)

MAX_AUTH_FAILURES = 5


class SyncHub:
    """
    Manages real-time WebSocket communication and clipboard history across devices.

    Maintains a list of active WebSocket connections, broadcasts incoming clip
    events to all connected devices except the sender, and delivers recent clipboard
    history to newly onboarding clients.

    Device pairing: a new (non-localhost) device must authenticate with the pairing
    PIN once. Successful devices are remembered in the trust store, so a phone that
    re-scans the QR code in the same browser connects without being asked again.
    """

    def __init__(self, max_history: int = 25, trust_store: TrustStore | None = None) -> None:
        self.max_history = max_history
        self._connections: set[WebSocket] = set()
        self._history: list[ClipboardItem] = []
        self._auth: dict[WebSocket, bool] = {}
        self._auth_failures: dict[WebSocket, int] = {}
        self._trust: TrustStore = trust_store or get_store()
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return sum(1 for auth in self._auth.values() if auth)

    def get_history(self) -> list[dict[str, Any]]:
        """Return serialized recent clipboard items."""
        return [item.to_message_dict() for item in self._history]

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _is_localhost(websocket: WebSocket) -> bool:
        client = getattr(websocket, "client", None)
        host = client.host if client is not None else None
        return host in ("127.0.0.1", "::1")

    def _is_authenticated(self, sender: WebSocket | None) -> bool:
        return sender is None or bool(self._auth.get(sender, False))

    async def _send_history(self, websocket: WebSocket) -> None:
        history_payload = {
            "type": "history",
            "device_id": "server",
            "timestamp": get_utc_now_iso(),
            "items": self.get_history(),
        }
        await websocket.send_json(history_payload)

    # -- connection lifecycle ----------------------------------------------
    async def connect(self, websocket: WebSocket) -> None:
        """Register a new WebSocket connection and, if auto-approved, send history."""
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
            self._auth[websocket] = False
            self._auth_failures[websocket] = 0

        # Localhost clients (the desktop app) and open mode (PIN disabled) are trusted at once.
        if self._is_localhost(websocket) or not self._trust.require_pin:
            async with self._lock:
                self._auth[websocket] = True
            try:
                await self._send_history(websocket)
                logger.info("Client connected (auto-approved). Active connections: %d", self.connection_count)
            except Exception as exc:
                logger.warning("Failed to send history to new connection: %s", exc)
                await self.disconnect(websocket)
            return

        # Other clients identify themselves via an auth_request/auth_pin message; the
        # client sends auth_request immediately on open, so no pre-emptive prompt here
        # (avoids flashing the PIN screen for already-trusted devices).
        logger.info("Client connected (awaiting pairing). Active connections: %d", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        """Unregister a disconnected WebSocket connection."""
        async with self._lock:
            self._connections.discard(websocket)
            self._auth.pop(websocket, None)
            self._auth_failures.pop(websocket, None)
        logger.info("Client disconnected. Active connections: %d", self.connection_count)

    # -- auth handling -----------------------------------------------------
    async def _handle_auth_request(self, sender: WebSocket, message: dict[str, Any]) -> None:
        device_id = str(message.get("device_id", "")).strip()
        is_local = self._is_localhost(sender)
        approved = is_local or not self._trust.require_pin or (
            bool(device_id) and self._trust.is_trusted(device_id)
        )
        if approved:
            async with self._lock:
                self._auth[sender] = True
            try:
                await sender.send_json({"type": "auth_success", "device_id": "server"})
                await self._send_history(sender)
                logger.info("Device '%s' authenticated (remembered). Active connections: %d", device_id, self.connection_count)
            except Exception as exc:
                logger.warning("Failed to send history after auth: %s", exc)
                await self.disconnect(sender)
        else:
            await sender.send_json({"type": "auth_required", "device_id": "server"})

    async def _handle_auth_pin(self, sender: WebSocket, message: dict[str, Any]) -> None:
        device_id = str(message.get("device_id", "")).strip()
        pin = str(message.get("pin", ""))
        if self._trust.validate_pin(pin):
            if device_id:
                self._trust.trust(device_id)
            async with self._lock:
                self._auth[sender] = True
            logger.info("Device '%s' authenticated via PIN. Active connections: %d", device_id, self.connection_count)
            try:
                await sender.send_json({"type": "auth_success", "device_id": "server"})
                await self._send_history(sender)
            except Exception as exc:
                logger.warning("Failed to send history after PIN auth: %s", exc)
                await self.disconnect(sender)
            return

        failures = self._auth_failures.get(sender, 0) + 1
        async with self._lock:
            self._auth_failures[sender] = failures
        if failures >= MAX_AUTH_FAILURES:
            logger.warning("Device '%s' failed PIN %d times. Closing connection.", device_id, failures)
            await sender.send_json(
                {"type": "auth_error", "message": "Too many failed attempts. Re-scan the QR code to try again."}
            )
            try:
                await sender.close(code=1013)
            except Exception:
                pass
            await self.disconnect(sender)
        else:
            remaining = MAX_AUTH_FAILURES - failures
            await sender.send_json(
                {"type": "auth_error", "message": f"Incorrect PIN ({failures}/{MAX_AUTH_FAILURES} attempts, {remaining} left)"}
            )

    # -- message handling --------------------------------------------------
    async def handle_message(self, sender: WebSocket, message: dict[str, Any]) -> None:
        """Process an incoming clipboard event and broadcast to other connected peers."""
        msg_type = message.get("type", "text")

        if msg_type == "auth_request":
            await self._handle_auth_request(sender, message)
            return
        if msg_type == "auth_pin":
            await self._handle_auth_pin(sender, message)
            return
        if not self._is_authenticated(sender):
            logger.warning("Ignoring message from unauthenticated connection: %s", message)
            return

        content_type = message.get("type", "text")
        content = message.get("content", "")
        device_id = message.get("device_id", "unknown")

        if content_type not in ("text", "image", "file"):
            logger.warning("Received unsupported message format: %s", message)
            return

        item = ClipboardItem(
            device_id=device_id,
            timestamp=message.get("timestamp") or get_utc_now_iso(),
            type=content_type,
            content=content,
            filename=message.get("filename"),
            filesize=message.get("filesize"),
            file_url=message.get("file_url"),
        )

        async with self._lock:
            # Prevent repetitive consecutive duplicates in history feed
            if not self._history or self._history[-1].content != item.content or self._history[-1].file_url != item.file_url:
                self._history.append(item)
                if len(self._history) > self.max_history:
                    self._history.pop(0)

            peers = [ws for ws in self._connections if ws != sender and self._auth.get(ws, False)]

        payload = item.to_message_dict()
        logger.info("Broadcasting clipboard clip (%s) from device '%s' to %d peers", content_type, device_id, len(peers))

        for ws in peers:
            try:
                await ws.send_json(payload)
            except Exception as exc:
                logger.warning("Error broadcasting to peer: %s", exc)
                await self.disconnect(ws)
