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
        print(f"Sharing as: {self._config.device_id}")
        print(f"Sharing with: {self._config.websocket_url}")
        print("Watching your clipboard. Copy anything to share it. Press Ctrl+C to stop.\n")

        self._clipboard.start()
        await self._websocket.start()

        await self._shutdown_event.wait()

        logger.info("Shutting down...")
        self._clipboard.stop()
        await self._websocket.stop()

    def request_shutdown(self) -> None:
        """Request a graceful shutdown from any thread."""
        self._loop.call_soon_threadsafe(self._shutdown_event.set)

    def _http_base(self) -> str:
        return (
            self._config.websocket_url.replace("ws://", "http://")
            .replace("wss://", "https://")
            .replace("/ws", "")
        )

    def _resolve_file_url(self, file_url: str) -> str:
        if file_url.startswith("http://") or file_url.startswith("https://"):
            return file_url
        return f"{self._http_base()}{file_url if file_url.startswith('/') else '/' + file_url}"

    async def _upload_path(
        self,
        path: str,
        filename: str,
        item_type: str,
        entry_count: int | None = None,
        skipped_count: int | None = None,
    ) -> bool:
        """Stream a local file to the server and broadcast the download link."""
        import httpx
        upload_endpoint = f"{self._http_base()}/api/upload"
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                with open(path, "rb") as f:
                    files = {"file": (filename, f)}
                    data: dict[str, Any] = {"item_type": item_type}
                    if entry_count is not None:
                        data["entry_count"] = str(entry_count)
                    if skipped_count is not None:
                        data["skipped_count"] = str(skipped_count)
                    resp = await client.post(upload_endpoint, files=files, data=data)
                    if resp.status_code == 413:
                        logger.warning("Shared %s is too big (server refused).", filename)
                        print(f"'{filename}' is too big to share (over 2 GB).")
                        return False
                    if resp.status_code != 200:
                        logger.warning("Upload failed for %s: %s", filename, resp.status_code)
                        return False
                    payload = resp.json()
                    file_url = payload.get("url", "")
                    # Store absolute URL so a second PC can download directly
                    if file_url.startswith("/"):
                        file_url = f"{self._http_base()}{file_url}"
                    label = "Folder" if item_type == "folder" else "File"
                    content_label = f"{label}: {payload.get('filename')}"
                    if entry_count is not None:
                        content_label = f"{label}: {payload.get('filename')} ({entry_count} items)"
                    await self._websocket.send_clipboard_update(
                        content=content_label,
                        content_type=item_type,
                        filename=payload.get("filename"),
                        filesize=payload.get("filesize"),
                        file_url=file_url,
                        entry_count=entry_count,
                        skipped_count=skipped_count,
                    )
                    logger.info("Uploaded and shared %s: %s", item_type, payload.get("filename"))
                    print(f"Shared {label.lower()} '{payload.get('filename')}'.")
                    return True
        except Exception as exc:
            logger.warning("Failed to upload local %s clip: %s", item_type, exc)
            return False

    async def _download_shared_file(self, message: dict[str, Any]) -> None:
        """Download a shared file/folder link to ~/Downloads/ClipBoardSync/."""
        import httpx
        from pathlib import Path
        file_url = str(message.get("file_url") or "")
        if not file_url:
            return
        url = self._resolve_file_url(file_url)
        filename = str(message.get("filename") or "shared_file")
        safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename).strip() or "shared_file"
        dest_dir = Path.home() / "Downloads" / "ClipBoardSync"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / safe
            # Avoid overwriting: add (1), (2), ...
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                i = 1
                while (dest_dir / f"{stem} ({i}){suffix}").exists():
                    i += 1
                dest = dest_dir / f"{stem} ({i}){suffix}"
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code not in (200, 206):
                        logger.warning("Download failed (%s) for %s", resp.status_code, safe)
                        return
                    with dest.open("wb") as out:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            out.write(chunk)
            kind = "Folder (.zip)" if message.get("type") == "folder" else "File"
            print(f"{kind} saved: {dest}")
            logger.info("Downloaded shared %s to %s", message.get("type"), dest)
        except Exception as exc:
            logger.warning("Failed to download shared file %s: %s", filename, exc)

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

        if p_type == "folder" and filepath:
            # Zip the folder, then stream-upload the zip
            try:
                from server.folder_zip import zip_directory
                from pathlib import Path
                zip_path, zip_name, entries, skipped = zip_directory(filepath)
                try:
                    size = zip_path.stat().st_size
                    if size > 1024 * 1024 * 1024:
                        logger.warning("Folder '%s' too big to share (%d bytes).", zip_name, size)
                        print(f"Folder '{Path(filepath).name}' is too big to share (over 1 GB zipped).")
                        return
                    ok = await self._upload_path(
                        str(zip_path), zip_name, "folder",
                        entry_count=entries, skipped_count=skipped,
                    )
                    # _upload_path already broadcast the folder link; nothing more to do.
                finally:
                    try:
                        zip_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                return
            except Exception as exc:
                logger.warning("Failed to share local folder: %s", exc)
                return

        if p_type == "file" and filepath:
            # Stream-upload the file (never fully in RAM)
            try:
                from pathlib import Path
                if await self._upload_path(str(filepath), filename or Path(str(filepath)).name, "file"):
                    return
            except Exception as exc:
                logger.warning("Failed to upload local file clip: %s", exc)

        await self._websocket.send_clipboard_update(
            content=content,
            content_type=p_type,
            filename=filename,
            filesize=filesize,
            file_url=payload.get("file_url"),
            entry_count=payload.get("entry_count"),
            skipped_count=payload.get("skipped_count"),
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
            print(f"New photo shared — copied to your clipboard.")
            self._clipboard.set_image_from_base64(content)
            return

        if content_type == "image" and message.get("file_url"):
            # Large photo shared as a download link (fast HTTP path)
            logger.info("Downloading shared photo from device %s", source_device)
            await self._download_shared_file(message)
            return

        if content_type in ("file", "folder") and message.get("file_url"):
            logger.info("Downloading shared %s from device %s", content_type, source_device)
            await self._download_shared_file(message)
            return

        if content_type == "text" and isinstance(content, str):
            logger.info("Applying remote clipboard text update from device %s", source_device)
            print(f"New text shared — copied to your clipboard.")
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
        print("ClipBoardSync clipboard sharing needs Windows for this mode.", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
