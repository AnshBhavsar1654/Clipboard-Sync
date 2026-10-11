"""WebSocket connection manager and clipboard history sync hub."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlparse
from fastapi import WebSocket

from server.auth import MAX_PIN_FAILS, TrustStore, get_store, is_loopback_host
from server.models import ClipboardItem, get_utc_now_iso

logger = logging.getLogger(__name__)

MAX_AUTH_FAILURES = MAX_PIN_FAILS

# The socket carries metadata only (plan §2.2): bytes go through /api/upload.
# Anything bigger is a legacy blob or abuse — dropped, never stored.
MAX_MESSAGE_BYTES = 256 * 1024

# Only URLs this server handed out via /api/upload are forwarded (plan §1.6).
# The desktop client uploads bytes first and shares metadata, so a file_url
# of any other shape (javascript:, http://evil/…) is dropped, never forwarded.
_FILE_URL_RE = re.compile(r"^/api/files/[0-9a-f]{8,64}(\.[A-Za-z0-9]{1,10})?([?#].*)?$")
_MAX_FILENAME_LEN = 255


def is_allowed_file_url(file_url: str) -> bool:
    """True for relative /api/files/<id> links and absolute http(s) URLs
    whose path is one (host checking happens on the downloading client)."""
    if not file_url:
        return False
    if file_url.startswith("/"):
        return bool(_FILE_URL_RE.match(file_url))
    if file_url.startswith("http://") or file_url.startswith("https://"):
        try:
            return bool(_FILE_URL_RE.match(urlparse(file_url).path or ""))
        except Exception:
            return False
    return False


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
        self._hosts: dict[WebSocket, str] = {}
        self._trust: TrustStore = trust_store or get_store()
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        """Authenticated remote devices only; the desktop app's own loopback
        socket is not a phone (plan §2.10)."""
        return sum(
            1
            for ws, auth in self._auth.items()
            if auth and not is_loopback_host(self._hosts.get(ws, ""))
        )

    def get_history(self) -> list[dict[str, Any]]:
        """Return serialized recent clipboard items."""
        return [item.to_message_dict() for item in self._history]

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _client_host(websocket: WebSocket) -> str:
        client = getattr(websocket, "client", None)
        host = getattr(client, "host", "") if client is not None else ""
        return host or ""

    @staticmethod
    def _is_localhost(websocket: WebSocket) -> bool:
        return is_loopback_host(SyncHub._client_host(websocket))

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
        # Same-origin check on the handshake (plan §1.4): a browser page from
        # another site must not be able to open a socket to us.
        try:
            headers = getattr(websocket, "headers", {}) or {}
            origin = headers.get("origin")
            host = headers.get("host", "")
            if origin and host and urlparse(origin).netloc != host:
                await websocket.close(code=1008)
                return
        except Exception:
            pass
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
            self._auth[websocket] = False
            self._auth_failures[websocket] = 0
            self._hosts[websocket] = self._client_host(websocket)

        try:
            cookies = getattr(websocket, "cookies", None) or {}
            cookie_token = cookies.get("cs_token")
        except Exception:
            cookie_token = None

        # Trusted at once: localhost (the desktop app), open mode, or a phone
        # presenting the server-issued pairing cookie (plan §1.2). The legacy
        # device_id/PIN messages below stay for older cached phone pages.
        if (
            self._is_localhost(websocket)
            or not self._trust.require_pin
            or (cookie_token and self._trust.verify_token(cookie_token))
        ):
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
            self._hosts.pop(websocket, None)
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
        ip = self._client_host(sender)
        # Throttle by client IP so reconnecting does not reset the limit (plan §1.1).
        if self._trust.locked_out(ip):
            logger.warning("Locked-out IP '%s' attempted PIN auth.", ip)
            await sender.send_json(
                {"type": "auth_error", "message": "Too many tries. Wait a few minutes or ask for a new code."}
            )
            return
        if self._trust.validate_pin(pin):
            self._trust.clear_fails(ip)
            if device_id:
                self._trust.trust(device_id)
            self._trust.regenerate_pin()  # single-use code (plan §1.1)
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

        self._trust.record_fail(ip)
        if self._trust.locked_out(ip):
            logger.warning("IP '%s' failed PIN %d times. Locking out.", ip, MAX_AUTH_FAILURES)
            await sender.send_json(
                {"type": "auth_error", "message": "Too many tries. Wait a few minutes or ask for a new code."}
            )
            try:
                await sender.close(code=1013)
            except Exception:
                pass
            await self.disconnect(sender)
        else:
            await sender.send_json(
                {"type": "auth_error", "message": "That code did not match."}
            )

    # -- message handling --------------------------------------------------
    async def handle_message(self, sender: WebSocket, message: dict[str, Any]) -> None:
        """Process an incoming clipboard event and broadcast to other connected peers."""
        msg_type = message.get("type", "text")

        if msg_type == "ping":
            try:
                await sender.send_json({"type": "pong", "device_id": "server"})
            except Exception:
                pass
            return
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

        if content_type not in ("text", "image", "file", "folder"):
            logger.warning("Received unsupported message format: %s", message)
            return

        if isinstance(content, str) and len(content.encode("utf-8", "ignore")) > MAX_MESSAGE_BYTES:
            logger.warning(
                "Dropping oversize %s clip from '%s' (%d chars): share bytes via /api/upload.",
                content_type, device_id, len(content),
            )
            return

        # Never forward a file_url this server did not hand out (plan §1.6):
        # javascript:… runs script on the phone, http://evil/… pulls malware
        # onto the desktop. Relative links stay relative so PC-to-PC relays
        # keep working against the right host.
        file_url = message.get("file_url")
        if file_url and not is_allowed_file_url(str(file_url)):
            logger.warning("Dropping clip with untrusted file_url from '%s'", device_id)
            return

        filename = message.get("filename")
        if isinstance(filename, str) and len(filename) > _MAX_FILENAME_LEN:
            filename = filename[:_MAX_FILENAME_LEN]

        thumb = message.get("thumb")
        if not isinstance(thumb, str) or len(thumb) > 64 * 1024 or not thumb.startswith("data:image/"):
            thumb = None

        item = ClipboardItem(
            device_id=device_id,
            timestamp=message.get("timestamp") or get_utc_now_iso(),
            type=content_type,
            content=content,
            filename=filename,
            filesize=message.get("filesize"),
            file_url=file_url,
            entry_count=message.get("entry_count"),
            skipped_count=message.get("skipped_count"),
            thumb=thumb,
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

        # One slow phone must not delay the rest, and a failing peer must not
        # disturb iteration over the others (plan §2.14).
        async def _send_to(ws: WebSocket) -> WebSocket | None:
            try:
                await ws.send_json(payload)
                return None
            except Exception as exc:
                logger.warning("Error broadcasting to peer: %s", exc)
                return ws

        failed = await asyncio.gather(*(_send_to(ws) for ws in peers))
        for ws in failed:
            if ws is not None:
                await self.disconnect(ws)
