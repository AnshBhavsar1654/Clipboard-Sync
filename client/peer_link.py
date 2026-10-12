"""Link this computer to another computer running ClipBoardSync.

The other computer is the host: this one pairs with it like a phone does (the
host's pairing code, then a token cookie) and keeps a WebSocket open to its
hub. Clips from the host are imported into this computer's own hub, so they
reach this clipboard, this window and this computer's phones. Clips accepted
here are sent to the host the same way. Files are copied across over HTTP, so
each side keeps serving its own copy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import socket
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets

from server.hub import SyncHub

logger = logging.getLogger(__name__)

CLIP_TYPES = ("text", "image", "file", "folder")
_FILE_URL = re.compile(r"^/api/files/[0-9a-f]{8,64}(\.[A-Za-z0-9]{1,10})?$")
_CONFIG_NAME = "peer.json"
_MAX_RETRY_DELAY = 20.0


class PeerError(Exception):
    """A problem worth showing to the user as-is."""


class PeerLink:
    def __init__(self, hub: SyncHub, uploads_dir: Path, device_id: str, state_dir: Path) -> None:
        self._hub = hub
        self._uploads_dir = Path(uploads_dir)
        self._device_id = device_id
        self._config_path = Path(state_dir) / _CONFIG_NAME
        self._computer_name = _own_name()
        self.host = ""
        self.port = 0
        self.name = ""
        self.status = "disconnected"  # connecting | connected | retrying | code_rejected | disconnected
        self._token = ""
        self._task: asyncio.Task | None = None
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # -- public ---------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        return {"status": self.status, "host": self.host, "port": self.port, "name": self.name}

    async def connect(self, host: str, port: int, code: str) -> None:
        """Pair with ``host`` and start syncing. Raises PeerError with a plain message."""
        host = host.strip()
        if not host or not 0 < int(port) < 65536:
            raise PeerError("Enter the other computer's address and port.")
        await self.disconnect(forget=False)
        base = f"http://{host}:{int(port)}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                info = (await client.get(f"{base}/api/info")).json()
                if (await client.get(f"{base}/api/history")).status_code == 200:
                    token = ""  # host is in open mode
                else:
                    resp = await client.post(
                        f"{base}/api/pair", json={"pin": code.strip(), "name": self._computer_name}
                    )
                    if resp.status_code == 429:
                        raise PeerError("Too many tries. Wait a few minutes or ask for a new code.")
                    if resp.status_code != 200:
                        raise PeerError("That code did not match. Check the code on the other computer.")
                    token = resp.cookies.get("cs_token") or ""
        except PeerError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("Peer pairing failed: %s", exc)
            raise PeerError(
                "Could not reach that computer. Check its address and that ClipBoardSync is running."
            ) from exc
        self.host, self.port, self._token = host, int(port), token
        self.name = str(info.get("computer_name") or host)[:60]
        self._save()
        self._start()

    def resume(self) -> bool:
        """Reconnect to the computer paired in an earlier session, if any."""
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            self.host, self.port = str(data["host"]), int(data["port"])
            self._token, self.name = str(data.get("token", "")), str(data.get("name", ""))
        except (OSError, ValueError, KeyError):
            return False
        self._start()
        return True

    async def disconnect(self, forget: bool = True) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._hub.remove_listener(self._on_local_clip)
        self.status = "disconnected"
        if forget:
            self._config_path.unlink(missing_ok=True)
            self.host, self.port, self.name, self._token = "", 0, "", ""

    # -- persistence ----------------------------------------------------------
    def _save(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps({"host": self.host, "port": self.port, "token": self._token, "name": self.name}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not remember the linked computer: %s", exc)

    # -- connection loop ------------------------------------------------------
    def _start(self) -> None:
        self._hub.add_listener(self._on_local_clip)
        self.status = "connecting"
        self._task = asyncio.create_task(self._run(), name="peer-link")

    async def _run(self) -> None:
        delay = 2.0
        headers = {"Cookie": f"cs_token={self._token}"} if self._token else {}
        url = f"ws://{self.host}:{self.port}/ws"
        while True:
            try:
                async with websockets.connect(
                    url, additional_headers=headers, max_size=None, ping_interval=20, ping_timeout=20
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "hello", "device_id": self._device_id,
                        "name": self._computer_name, "kind": "computer",
                    }))
                    delay = 2.0
                    sender = asyncio.create_task(self._send_loop(ws))
                    try:
                        await self._receive_loop(ws)
                    finally:
                        sender.cancel()
                if self.status == "code_rejected":
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Link to %s dropped: %s", self.host, exc)
            self.status = "retrying"
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, _MAX_RETRY_DELAY)

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "auth_required":
                self.status = "code_rejected"  # revoked on the host: pair again
                return
            if kind == "history":
                self.status = "connected"
            elif kind in CLIP_TYPES:
                self.status = "connected"
                await self._import(msg)

    async def _send_loop(self, ws: Any) -> None:
        """Send queued clips; ping every 25 s so the host never drops us as silent."""
        while True:
            try:
                msg = await asyncio.wait_for(self._outbox.get(), timeout=25)
            except TimeoutError:
                await ws.send(json.dumps({"type": "ping"}))
                continue
            try:
                out = await self._prepare_outgoing(msg)
                if out is not None:
                    await ws.send(json.dumps(out))
            except Exception as exc:
                logger.warning("Could not send a clip to %s: %s", self.name or self.host, exc)

    # -- host -> this computer ---------------------------------------------------
    async def _import(self, msg: dict[str, Any]) -> None:
        if msg.get("device_id") == self._device_id:
            return
        clip = {k: msg[k] for k in (
            "type", "content", "device_id", "timestamp", "filename", "filesize",
            "entry_count", "skipped_count", "thumb",
        ) if k in msg}
        file_url = msg.get("file_url")
        if file_url:
            if not _FILE_URL.match(str(file_url)):
                return
            local = await self._download(str(file_url), str(msg.get("filename") or ""))
            if local is None:
                return
            clip["file_url"] = f"/api/files/{local}"
        await self._hub.handle_message(None, clip, relayed=True)

    async def _download(self, file_url: str, filename: str) -> str | None:
        stored = uuid.uuid4().hex[:10] + _suffix(filename or file_url)
        dest = self._uploads_dir / stored
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            async with httpx.AsyncClient(timeout=300.0, cookies=self._cookies()) as client:
                async with client.stream("GET", f"http://{self.host}:{self.port}{file_url}") as resp:
                    if resp.status_code != 200:
                        return None
                    with part.open("wb") as out:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            await asyncio.to_thread(out.write, chunk)
            part.replace(dest)
            return stored
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Could not copy a file from %s: %s", self.name or self.host, exc)
            part.unlink(missing_ok=True)
            return None

    # -- this computer -> host ---------------------------------------------------
    async def _on_local_clip(self, payload: dict[str, Any]) -> None:
        await self._outbox.put(payload)

    async def _prepare_outgoing(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        out = {k: payload[k] for k in (
            "type", "content", "device_id", "timestamp", "filename", "filesize",
            "entry_count", "skipped_count", "thumb",
        ) if k in payload}
        file_url = payload.get("file_url")
        if not file_url:
            return out
        name = str(file_url).rsplit("/", 1)[-1]
        path = self._uploads_dir / name
        if not path.is_file():
            return None
        async with httpx.AsyncClient(timeout=None, cookies=self._cookies()) as client:
            with path.open("rb") as handle:
                resp = await client.post(
                    f"http://{self.host}:{self.port}/api/upload",
                    files={"file": (payload.get("filename") or name, handle)},
                    data={"item_type": payload.get("type", "file")},
                )
        if resp.status_code != 200:
            raise PeerError(f"The other computer refused the file ({resp.status_code}).")
        uploaded = resp.json()
        out["file_url"] = uploaded["url"]
        out["filesize"] = uploaded.get("filesize", out.get("filesize"))
        if uploaded.get("thumb"):
            out["thumb"] = uploaded["thumb"]
        return out

    def _cookies(self) -> dict[str, str]:
        return {"cs_token": self._token} if self._token else {}


def _own_name() -> str:
    try:
        return socket.gethostname()[:40] or "Computer"
    except OSError:
        return "Computer"


def _suffix(name: str) -> str:
    ext = Path(name.split("?", 1)[0]).suffix
    return ext[:10] if re.fullmatch(r"\.[A-Za-z0-9]{1,9}", ext) else ""
