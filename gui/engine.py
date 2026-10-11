"""Shared background engine: server + clipboard watcher (plan 4.6)."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from pathlib import Path

import uvicorn

from client.config import Config
from client.main import ClipBoardSyncApp
from server.filenames import unique_path
from server.main import app as fastapi_app
from server.main import hub as sync_hub


class BackgroundEngine:
    """Runs the sharing server and clipboard watcher together in the background."""

    def __init__(self, port: int = 8000) -> None:
        self.port = port
        self.is_running = False
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._client_app: ClipBoardSyncApp | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._peer = None
        self.peer_status: str = "disconnected"  # disconnected|connecting|connected|code_rejected|retrying
        self.peer_host: str = ""
        self._log_queue: queue.Queue[str] | None = None

    @property
    def device_id(self) -> str:
        """This computer's sync identity (shared with the clipboard client)."""
        if self._client_app is not None:
            return self._client_app._config.device_id
        return Config().device_id

    def apply_settings(self) -> None:
        """Reload prefs.json into the running client (drawer Save, plan §4.2)."""
        if self._client_app is None:
            return
        try:
            from client.config import load_prefs

            prefs = load_prefs(self._client_app._config.config_dir)
            self._client_app._prefs = prefs
            self._client_app._clipboard._skip_private = not prefs.share_private_clips
            backend = self._client_app._clipboard._backend
            if prefs.extra_blocked_apps and hasattr(backend, "blocked_owner_exes"):
                backend.blocked_owner_exes.update(a.lower() for a in prefs.extra_blocked_apps)
        except Exception as exc:
            logging.getLogger(__name__).warning("Could not apply settings: %s", exc)

    def start(self, log_queue: queue.Queue[str]) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(log_queue,),
            name="clipboardsync-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        if self._server:
            self._server.should_exit = True
        if self._client_app and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._client_app.request_shutdown)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self, log_queue: queue.Queue[str]) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_runner(log_queue))
        except Exception as exc:
            log_queue.put(f"[Error] Sharing stopped unexpectedly: {exc}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self.is_running = False

    async def _async_runner(self, log_queue: queue.Queue[str]) -> None:
        self._log_queue = log_queue
        log_queue.put(f"[Info] Starting sharing on port {self.port}...")
        uv_config = uvicorn.Config(
            app=fastapi_app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        self._server = uvicorn.Server(uv_config)
        server_task = asyncio.create_task(self._server.serve(), name="uvicorn-backend")

        await asyncio.sleep(0.4)
        log_queue.put("[Info] Watching your clipboard for changes...")
        client_config = Config()
        client_config.websocket_url = f"ws://127.0.0.1:{self.port}/ws"
        self._client_app = ClipBoardSyncApp(client_config)

        client_task = asyncio.create_task(self._client_app.run(), name="win32-client")
        log_queue.put("[Info] Sharing is on. Copy on either device to share it.")

        done, pending = await asyncio.wait(
            [server_task, client_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._peer is not None:
            try:
                await self._peer.stop()
            except Exception:
                pass
            self._peer = None
        log_queue.put("[Info] Sharing is off.")

    # -- PC-to-PC peer ------------------------------------------------------
    def connect_peer(self, host_ip: str, port: int, pin: str, device_id: str) -> bool:
        """Join another PC's server. Returns False if sharing isn't running."""
        if not self.is_running or not self._loop or not self._loop.is_running():
            return False
        if self._peer is not None:
            return False
        from client.peer_client import PeerConnection

        def _status(s: str) -> None:
            self.peer_status = s
            if self._log_queue is not None:
                if s == "connected":
                    self._log_queue.put(f"[Info] Connected to other computer at {host_ip}.")
                elif s.startswith("code_rejected"):
                    self._log_queue.put("[Error] That computer code did not match. Check it and try again.")
                elif s == "retrying":
                    self._log_queue.put("[Info] Reconnecting to the other computer…")

        async def _on_peer_message(msg: dict) -> None:
            # Inject peer clips into the local hub so they show in this feed.
            try:
                await sync_hub.handle_message(None, msg)
            except Exception:
                pass
            # Download shared files/folders in the background.
            if msg.get("type") in ("file", "folder") and msg.get("file_url"):
                await self._download_peer_file(host_ip, port, msg)

        async def _start() -> None:
            self._peer = PeerConnection(host_ip, port, device_id, pin, _on_peer_message, _status)
            self.peer_host = f"{host_ip}:{port}"
            self.peer_status = "connecting"
            self._peer.start()

        asyncio.run_coroutine_threadsafe(_start(), self._loop)
        return True

    def disconnect_peer(self) -> None:
        if self._loop and self._loop.is_running() and self._peer is not None:
            peer, self._peer = self._peer, None
            asyncio.run_coroutine_threadsafe(peer.stop(), self._loop)
        else:
            self._peer = None
        self.peer_status = "disconnected"
        self.peer_host = ""
        if self._log_queue is not None:
            self._log_queue.put("[Info] Disconnected from the other computer.")

    def send_to_peer(self, message: dict) -> None:
        """Forward our clips to the connected PC (fire-and-forget)."""
        if self._loop and self._loop.is_running() and self._peer is not None:
            asyncio.run_coroutine_threadsafe(self._peer.send(message), self._loop)

    async def _download_peer_file(self, host_ip: str, port: int, msg: dict) -> None:
        try:
            import httpx
            file_url = str(msg.get("file_url") or "")
            if not file_url.startswith("http"):
                rel = file_url if file_url.startswith("/") else "/" + file_url
                file_url = f"http://{host_ip}:{port}{rel}"
            filename = str(msg.get("filename") or "shared_file")
            dest_dir = Path.home() / "Downloads" / "ClipBoardSync"
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Remote-controlled name: sanitize + keep inside dest_dir (plan §1.8).
            dest = unique_path(dest_dir, filename)
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("GET", file_url) as resp:
                    if resp.status_code not in (200, 206):
                        return
                    with dest.open("wb") as out:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            out.write(chunk)
            if self._log_queue is not None:
                kind = "Folder (.zip)" if msg.get("type") == "folder" else "File"
                self._log_queue.put(f"[Info] {kind} from other computer saved: {dest}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
