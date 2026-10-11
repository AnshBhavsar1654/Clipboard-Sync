"""Entry point for the ClipBoardSync Windows desktop client."""

from __future__ import annotations

import asyncio
import base64
import logging
import signal
import sys
from typing import Any

from client.clipboard import ClipboardMonitor
from client.config import Config, load_config, load_prefs
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
        self._prefs = load_prefs(config.config_dir)

        self._clipboard = ClipboardMonitor(
            loop=self._loop,
            on_change=self._on_local_clipboard_change,
            skip_private=not self._prefs.share_private_clips,
            blocked_apps=self._prefs.extra_blocked_apps,
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

    def _resolve_file_url(self, file_url: str) -> str | None:
        """Resolve a hub file_url to a download URL, or None to skip it.

        Absolute URLs are only honored when they point at the sync server we
        are connected to (plan §1.6) — a paired device must not redirect the
        desktop into downloading from an arbitrary host.
        """
        from urllib.parse import urlparse

        if file_url.startswith("http://") or file_url.startswith("https://"):
            try:
                got = urlparse(file_url).hostname or ""
                want = urlparse(self._http_base()).hostname or ""
            except Exception:
                return None
            loopback = ("127.0.0.1", "::1", "localhost")
            if got != want and not (got in loopback and want in loopback):
                logger.warning("Ignoring file link pointing at unexpected host: %s", got)
                return None
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
                    # Keep the URL relative (/api/files/<id>): each device
                    # resolves it against its own server. Absolutizing here
                    # would bake in this computer's loopback address, which
                    # is unreachable (and origin-mismatched) everywhere else.
                    file_url = payload.get("url", "")
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

    def _receive_dir(self) -> Any:
        from pathlib import Path

        if self._prefs.receive_folder:
            return Path(self._prefs.receive_folder).expanduser()
        return Path.home() / "Downloads" / "ClipBoardSync"

    async def _upload_bytes(
        self,
        data: bytes,
        filename: str,
        item_type: str,
    ) -> dict[str, Any] | None:
        """POST in-memory bytes to /api/upload. Returns the server payload."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    f"{self._http_base()}/api/upload",
                    files={"file": (filename, data)},
                    data={"item_type": item_type},
                )
                if resp.status_code != 200:
                    logger.warning("Byte upload failed for %s: %s", filename, resp.status_code)
                    return None
                return resp.json()
        except Exception as exc:
            logger.warning("Failed to upload %s bytes: %s", item_type, exc)
            return None

    async def _share_image_bytes(self, data: bytes, filename: str) -> bool:
        """Share clipboard image bytes as metadata (plan §2.2): the socket
        carries file_url + thumb only, never the pixels."""
        payload = await self._upload_bytes(data, filename, "image")
        if not payload:
            return False
        # Relative URL: resolvable by every device (see _upload_path).
        file_url = payload.get("url", "")
        await self._websocket.send_clipboard_update(
            content=f"Image: {payload.get('filename', filename)}",
            content_type="image",
            filename=payload.get("filename", filename),
            filesize=payload.get("filesize", len(data)),
            file_url=file_url,
            thumb=payload.get("thumb"),
        )
        logger.info("Uploaded and shared image: %s", payload.get("filename"))
        print(f"Shared photo '{payload.get('filename')}'.")
        return True

    async def _download_shared_file(self, message: dict[str, Any]) -> Any:
        """Download a shared file/folder link; returns the saved path or None."""
        import httpx

        from server.filenames import unique_path
        file_url = str(message.get("file_url") or "")
        if not file_url:
            return None
        url = self._resolve_file_url(file_url)
        if not url:
            return None
        filename = str(message.get("filename") or "shared_file")
        dest_dir = self._receive_dir()
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = unique_path(dest_dir, filename)
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code not in (200, 206):
                        logger.warning("Download failed (%s) for %s", resp.status_code, dest.name)
                        return None
                    with dest.open("wb") as out:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            out.write(chunk)
            kind = "Folder (.zip)" if message.get("type") == "folder" else "File"
            print(f"{kind} saved: {dest}")
            logger.info("Downloaded shared %s to %s", message.get("type"), dest)
            return dest
        except Exception as exc:
            logger.warning("Failed to download shared file %s: %s", filename, exc)
            return None

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
            # Zip the folder off the event loop (zipping blocks sharing while
            # it runs — plan §2.11), then stream-upload the zip
            try:
                from server.folder_zip import zip_directory
                from pathlib import Path
                zip_path, zip_name, entries, skipped = await asyncio.to_thread(
                    zip_directory, filepath
                )
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

        if p_type == "image" and payload.get("image_bytes"):
            # Screenshots share metadata only; bytes go via /api/upload (plan §2.2).
            import time as _time
            name = filename or f"screenshot_{_time.strftime('%Y%m%d_%H%M%S')}.png"
            if await self._share_image_bytes(bytes(payload["image_bytes"]), name):
                return
            # Upload failed: fall through to the legacy inline path below.

        await self._websocket.send_clipboard_update(
            content=content,
            content_type=p_type,
            filename=filename,
            filesize=filesize,
            file_url=payload.get("file_url"),
            entry_count=payload.get("entry_count"),
            skipped_count=payload.get("skipped_count"),
        )

    async def _apply_remote_item(self, message: dict[str, Any]) -> None:
        """Land one incoming clip on this computer (plan §2.7): images onto
        the clipboard as pixels, files/folders onto the clipboard as CF_HDROP
        so Explorer Ctrl+V pastes them — the Ctrl+V promise."""
        source_device = message.get("device_id")
        content_type = message.get("type", "text")
        content = message.get("content")

        if message.get("file_url"):
            try:
                declared = int(message.get("filesize") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared and declared > self._prefs.auto_receive_max_bytes:
                logger.info("Skipping auto-download of %s (%d bytes over cap).",
                            message.get("filename"), declared)
                print(f"'{message.get('filename')}' is too big to receive automatically "
                      f"(over {self._prefs.auto_receive_max_bytes // (1024 * 1024)} MB).")
                return

        if content_type == "image" and isinstance(content, str) and content.startswith("data:image/"):
            # Legacy inline photo (small, from older senders).
            logger.info("Applying remote clipboard image update from device %s", source_device)
            print("New photo shared — copied to your clipboard.")
            try:
                self._clipboard.set_image_from_bytes(
                    base64.b64decode(content.split(",", 1)[1])
                )
            except Exception:
                self._clipboard.set_image_from_base64(content)
            return

        if content_type == "image" and message.get("file_url"):
            logger.info("Applying shared photo from device %s", source_device)
            dest = await self._download_shared_file(message)
            if dest is None:
                return
            if self._prefs.put_received_on_clipboard:
                try:
                    if dest.stat().st_size <= self._prefs.auto_receive_max_bytes:
                        self._clipboard.set_image_from_bytes(dest.read_bytes())
                        print("New photo shared — copied to your clipboard.")
                        return
                except Exception as exc:
                    logger.warning("Could not put photo on clipboard: %s", exc)
            print(f"New photo shared — saved: {dest}")
            return

        if content_type in ("file", "folder") and message.get("file_url"):
            logger.info("Applying shared %s from device %s", content_type, source_device)
            dest = await self._download_shared_file(message)
            if dest is None:
                return
            if self._prefs.put_received_on_clipboard:
                try:
                    self._clipboard.set_files([dest])
                    print(f"New {content_type} shared — Ctrl+V to paste it.")
                    return
                except Exception as exc:
                    logger.warning("Could not put file on clipboard: %s", exc)
            print(f"New {content_type} shared — saved: {dest}")
            return

        if content_type == "text" and isinstance(content, str):
            logger.info("Applying remote clipboard text update from device %s", source_device)
            print("New text shared — copied to your clipboard.")
            self._clipboard.set_text(content)
            return

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
            if latest.get("device_id") == self._config.device_id:
                logger.debug("Latest history item originated from self; skipping")
                return
            await self._apply_remote_item(latest)
            return

        await self._apply_remote_item(message)


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
