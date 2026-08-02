"""Entry point for the ClipBoardSync Windows desktop client."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from client.clipboard import ClipboardMonitor
from client.config import Config, load_config
from client.websocket_client import WebSocketClient

logger = logging.getLogger(__name__)


class ClipBoardSyncApp:
    """
    Orchestrates clipboard monitoring and WebSocket synchronization.

    Local clipboard changes are forwarded to the backend. Remote updates from
    other devices are applied locally. Self-originated and duplicate events
    are filtered to prevent sync loops.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()

        self._clipboard = ClipboardMonitor(
            loop=self._loop,
            on_change=self._on_local_clipboard_change,
        )
        self._websocket = WebSocketClient(
            url=config.websocket_url,
            device_id=config.device_id,
            on_message=self._on_remote_clipboard_update,
            reconnect_base_delay=config.reconnect_base_delay,
            reconnect_max_delay=config.reconnect_max_delay,
        )

    async def run(self) -> None:
        """Run until a shutdown signal is received."""
        logger.info("Starting ClipBoardSync client (device_id=%s)", self._config.device_id)
        print(f"Device ID: {self._config.device_id}")
        print(f"Backend:   {self._config.websocket_url}")
        print("Monitoring clipboard. Press Ctrl+C to stop.\n")

        self._clipboard.start()
        await self._websocket.start()

        await self._shutdown_event.wait()

        logger.info("Shutting down...")
        self._clipboard.stop()
        await self._websocket.stop()

    def request_shutdown(self) -> None:
        """Request a graceful shutdown from any thread."""
        self._loop.call_soon_threadsafe(self._shutdown_event.set)

    async def _on_local_clipboard_change(self, text: str) -> None:
        """Handle a user-initiated clipboard copy detected locally."""
        await self._websocket.send_clipboard_update(text)

    async def _on_remote_clipboard_update(self, message: dict[str, Any]) -> None:
        """Apply a clipboard update or history received from another device."""
        source_device = message.get("device_id")
        if source_device == self._config.device_id:
            logger.debug("Ignored self-originated remote message")
            return

        content_type = message.get("type", "text")

        # Handle onboarding history cache upon initial connection
        if content_type == "history":
            items = message.get("items", [])
            if not isinstance(items, list) or not items:
                return
            latest = items[-1]
            if not isinstance(latest, dict):
                return
            latest_device = latest.get("device_id")
            if latest_device == self._config.device_id:
                logger.debug("Latest history item originated from self; skipping")
                return
            content = latest.get("content")
            if latest.get("type", "text") == "text" and isinstance(content, str):
                logger.info("Synchronizing latest clipboard history item from device %s", latest_device)
                print(f"[HISTORY SYNC] From {latest_device}: {content!r}")
                self._clipboard.set_text(content)
            return

        if content_type != "text":
            logger.debug("Ignoring unsupported clipboard type: %s", content_type)
            return

        content = message.get("content")
        if not isinstance(content, str):
            logger.warning("Ignoring remote message with invalid content")
            return

        logger.info("Applying remote clipboard update from device %s", source_device)
        print(f"[REMOTE] From {source_device}: {content!r}")

        self._clipboard.set_text(content)


def _install_signal_handlers(app: ClipBoardSyncApp) -> None:
    """Register OS signal handlers for graceful shutdown."""

    def _handler(signum: int, _frame: object) -> None:
        logger.info("Received signal %s", signum)
        app.request_shutdown()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


async def _async_main() -> None:
    config = load_config()
    app = ClipBoardSyncApp(config)
    _install_signal_handlers(app)
    await app.run()


def main() -> None:
    """CLI entry point."""
    if sys.platform != "win32":
        print("This client currently supports Windows only.", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
