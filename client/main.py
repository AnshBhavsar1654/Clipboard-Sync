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

    async def _on_local_clipboard_change(self, payload: str | dict[str, Any]) -> None:
        """Handle a user-initiated clipboard copy detected locally."""
        if isinstance(payload, str):
            await self._websocket.send_clipboard_update(content=payload, content_type="text")
            return

        if not isinstance(payload, dict):
            return

        p_type = payload.get("type", "text")
        content = payload.get("content", "")
        filename = payload.get("filename")
        filesize = payload.get("filesize")
        filepath = payload.get("filepath")

        if p_type == "file" and filepath:
            # Upload file to server REST endpoint
            try:
                import httpx
                http_url = self._config.websocket_url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")
                upload_endpoint = f"{http_url}/api/upload"
                async with httpx.AsyncClient() as client:
                    with open(filepath, "rb") as f:
                        resp = await client.post(upload_endpoint, files={"file": (filename or "file", f)})
                        if resp.status_code == 200:
                            data = resp.json()
                            await self._websocket.send_clipboard_update(
                                content=f"File: {data.get('filename')}",
                                content_type="file",
                                filename=data.get("filename"),
                                filesize=data.get("filesize"),
                                file_url=data.get("url"),
                            )
                            logger.info("Uploaded and sent file clip: %s", data.get("filename"))
                            return
            except Exception as exc:
                logger.warning("Failed to upload local file clip: %s", exc)

        await self._websocket.send_clipboard_update(
            content=content,
            content_type=p_type,
            filename=filename,
            filesize=filesize,
            file_url=payload.get("file_url"),
        )

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

            c_type = latest.get("type", "text")
            content = latest.get("content")
            if c_type == "text" and isinstance(content, str):
                logger.info("Synchronizing latest clipboard text history item from device %s", latest_device)
                self._clipboard.set_text(content)
            elif c_type == "image" and isinstance(content, str) and content.startswith("data:image/"):
                logger.info("Synchronizing latest clipboard image history item from device %s", latest_device)
                self._clipboard.set_image_from_base64(content)
            return

        content = message.get("content")
        if content_type == "image" and isinstance(content, str) and content.startswith("data:image/"):
            logger.info("Applying remote clipboard image update from device %s", source_device)
            print(f"[REMOTE IMAGE] From {source_device}")
            self._clipboard.set_image_from_base64(content)
            return

        if content_type == "text" and isinstance(content, str):
            logger.info("Applying remote clipboard text update from device %s", source_device)
            print(f"[REMOTE TEXT] From {source_device}: {content!r}")
            self._clipboard.set_text(content)
            return


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
