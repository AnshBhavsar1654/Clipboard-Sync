"""WebSocket client for real-time clipboard synchronization."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

IncomingMessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class WebSocketClient:
    """
    Maintains a persistent WebSocket connection to the sync backend.

    Automatically reconnects with exponential backoff when the connection
    drops. Outbound clipboard events are queued while disconnected and
    flushed once the connection is restored.
    """

    def __init__(
        self,
        url: str,
        device_id: str,
        on_message: IncomingMessageHandler,
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self._url = url
        self._device_id = device_id
        self._on_message = on_message
        self._reconnect_base_delay = reconnect_base_delay
        self._reconnect_max_delay = reconnect_max_delay

        self._connection: ClientConnection | None = None
        self._send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._running = False
        self._connected = asyncio.Event()

        self._connection_task: asyncio.Task[None] | None = None
        self._sender_task: asyncio.Task[None] | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        """Start connection management and outbound message processing."""
        if self._running:
            return

        self._running = True
        self._connection_task = asyncio.create_task(
            self._connection_loop(),
            name="websocket-connection",
        )
        self._sender_task = asyncio.create_task(
            self._sender_loop(),
            name="websocket-sender",
        )
        logger.info("WebSocket client started (url=%s)", self._url)

    async def stop(self) -> None:
        """Shut down tasks and close the active connection."""
        self._running = False
        self._connected.clear()

        if self._connection:
            await self._connection.close()
            self._connection = None

        for task in (self._connection_task, self._sender_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._connection_task = None
        self._sender_task = None
        logger.info("WebSocket client stopped")

    async def send_clipboard_update(
        self,
        content: str,
        content_type: str = "text",
        filename: str | None = None,
        filesize: int | None = None,
        file_url: str | None = None,
    ) -> None:
        """Queue a clipboard update for transmission to the backend."""
        message = {
            "device_id": self._device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": content_type,
            "content": content,
        }
        if filename:
            message["filename"] = filename
        if filesize is not None:
            message["filesize"] = filesize
        if file_url:
            message["file_url"] = file_url

        await self._send_queue.put(message)
        logger.debug("Queued %s update for send", content_type)

    async def _connection_loop(self) -> None:
        """Connect, receive messages, and reconnect on failure."""
        attempt = 0

        while self._running:
            try:
                logger.info("Connecting to %s ...", self._url)
                async with websockets.connect(self._url) as ws:
                    self._connection = ws
                    self._connected.set()
                    attempt = 0
                    logger.info("Connected to backend")

                    await self._receive_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WebSocket connection error: %s", exc)
            finally:
                self._connection = None
                self._connected.clear()

            if not self._running:
                break

            attempt += 1
            delay = min(
                self._reconnect_base_delay * (2 ** (attempt - 1)),
                self._reconnect_max_delay,
            )
            logger.info("Reconnecting in %.1f seconds (attempt %d)", delay, attempt)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    async def _receive_loop(self, ws: ClientConnection) -> None:
        """Process inbound messages until the connection closes."""
        try:
            async for raw in ws:
                await self._handle_raw_message(raw)
        except ConnectionClosed as exc:
            logger.warning("WebSocket connection closed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error while receiving WebSocket messages")

    async def _handle_raw_message(self, raw: str | bytes) -> None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Ignoring malformed WebSocket message: %s", exc)
            return

        if not isinstance(message, dict):
            logger.warning("Ignoring non-object WebSocket message")
            return

        logger.info(
            "Received clipboard update from device %s",
            message.get("device_id", "<unknown>"),
        )
        await self._on_message(message)

    async def _sender_loop(self) -> None:
        """Send queued messages whenever the connection is available."""
        while self._running:
            message = await self._send_queue.get()
            try:
                await self._connected.wait()
                if not self._connection:
                    await self._send_queue.put(message)
                    continue

                payload = json.dumps(message)
                await self._connection.send(payload)
                logger.info(
                    "Sent clipboard update (%d chars) to backend",
                    len(message.get("content", "")),
                )
            except ConnectionClosed:
                logger.warning("Send failed due to closed connection; re-queuing message")
                await self._send_queue.put(message)
                self._connected.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to send clipboard update; re-queuing message")
                await self._send_queue.put(message)
                await asyncio.sleep(self._reconnect_base_delay)
            finally:
                self._send_queue.task_done()
