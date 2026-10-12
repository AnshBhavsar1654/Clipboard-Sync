"""Shared background engine: server + clipboard watcher."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

import uvicorn

from client.config import Config
from client.main import ClipBoardSyncApp
from client.peer_link import PeerLink
from server.main import UPLOADS_DIR
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
        self.peer: PeerLink | None = None

    @property
    def device_id(self) -> str:
        """This computer's sync identity (shared with the clipboard client)."""
        if self._client_app is not None:
            return self._client_app._config.device_id
        return Config().device_id

    def apply_settings(self) -> None:
        """Reload prefs.json into the running client (drawer Save)."""
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
        self.peer = PeerLink(
            sync_hub, UPLOADS_DIR, client_config.device_id, client_config.config_dir
        )
        if self.peer.resume():
            log_queue.put(f"[Info] Linking to the other computer ({self.peer.host})...")
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
        if self.peer is not None:
            await self.peer.disconnect(forget=False)
        log_queue.put("[Info] Sharing is off.")
