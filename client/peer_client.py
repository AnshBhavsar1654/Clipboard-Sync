"""Peer connection: lets this PC join another PC's server like a trusted device.

The other PC stays the host (its hub relays both ways). This PC runs a
persistent WebSocket to the peer, answers the pairing-code challenge once,
downloads shared files over fast HTTP, and forwards its own clips there.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PeerMessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class PeerConnection:
    def __init__(
        self,
        host_ip: str,
        port: int,
        device_id: str,
        pin: str,
        on_message: PeerMessageHandler,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.host_ip = host_ip
        self.port = port
        self.device_id = device_id
        self.pin = pin
        self._on_message = on_message
        self._on_status = on_status or (lambda _s: None)
        self._running = False
        self._task: asyncio.Task | None = None
        self._ws = None
        self._send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.connected = False

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host_ip}:{self.port}/ws"

    @property
    def http_base(self) -> str:
        return f"http://{self.host_ip}:{self.port}"

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="peer-connection")

    async def stop(self) -> None:
        self._running = False
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:
            pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.connected = False

    async def send(self, message: dict[str, Any]) -> None:
        await self._send_queue.put(message)

    async def _run(self) -> None:
        import websockets
        delay = 2.0
        while self._running:
            try:
                self._on_status("connecting")
                async with websockets.connect(self.ws_url, max_size=64 * 1024 * 1024) as ws:
                    self._ws = ws
                    # Identify; host replies auth_success / auth_required
                    await ws.send(json.dumps({"type": "auth_request", "device_id": self.device_id}))
                    authed = False
                    sender = asyncio.create_task(self._sender_loop(ws), name="peer-sender")
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            if not isinstance(msg, dict):
                                continue
                            mtype = msg.get("type")
                            if mtype == "auth_required":
                                await ws.send(json.dumps({
                                    "type": "auth_pin",
                                    "device_id": self.device_id,
                                    "pin": self.pin,
                                }))
                                continue
                            if mtype == "auth_error":
                                self._on_status(f"code_rejected:{msg.get('message', '')}")
                                await asyncio.sleep(5)
                                continue
                            if mtype == "auth_success":
                                authed = True
                                self.connected = True
                                self._on_status("connected")
                                delay = 2.0
                                continue
                            if mtype == "history" and not authed:
                                authed = True
                                self.connected = True
                                self._on_status("connected")
                                continue
                            if not authed:
                                continue
                            await self._on_message(msg)
                    finally:
                        sender.cancel()
                        try:
                            await sender
                        except asyncio.CancelledError:
                            pass
                        self._ws = None
                        self.connected = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Peer connection error: %s", exc)
                self._on_status("retrying")
            if not self._running:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 15.0)

    async def _sender_loop(self, ws) -> None:
        while self._running:
            msg = await self._send_queue.get()
            try:
                await ws.send(json.dumps(msg))
            except Exception as exc:
                logger.debug("Peer send failed, re-queuing: %s", exc)
                await self._send_queue.put(msg)
                return
            finally:
                self._send_queue.task_done()

    async def download_to(self, file_url: str, dest: Path) -> bool:
        """Download a peer-hosted file (supports resume via Range)."""
        import httpx
        url = file_url if file_url.startswith("http") else f"{self.http_base}{file_url}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            start = dest.stat().st_size if dest.exists() else 0
            headers = {"Range": f"bytes={start}-"} if start else {}
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code not in (200, 206):
                        return False
                    mode = "ab" if resp.status_code == 206 and start else "wb"
                    with dest.open(mode) as out:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            out.write(chunk)
            return True
        except Exception as exc:
            logger.debug("Peer download failed: %s", exc)
            return False
