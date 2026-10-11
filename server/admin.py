"""Loopback-only admin API for the desktop shell (plan §4.1).

The pywebview window talks to these endpoints with a per-launch random token
(``?t=`` or ``X-Admin-Token``). Non-loopback callers are rejected outright;
loopback callers still need the token. No token, no admin — even locally.
"""

from __future__ import annotations

import io
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from server import __version__
from server.auth import is_loopback_host

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")

_admin_token: str = secrets.token_urlsafe(32)
_port: int = 8000
_started_at: float = time.time()

_engine_start = None
_engine_stop = None
_engine_running = None


def set_engine_controller(start, stop, is_running) -> None:
    """Shell registers engine control so the drawer can pause/resume sharing."""
    global _engine_start, _engine_stop, _engine_running
    _engine_start, _engine_stop, _engine_running = start, stop, is_running


def admin_token() -> str:
    return _admin_token


def configure(port: int) -> None:
    """Called by the desktop shell/engine at startup (and by tests)."""
    global _port
    _port = port


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


async def require_admin(request: Request) -> None:
    if not is_loopback_host(client_ip(request)):
        raise HTTPException(status_code=403, detail="Admin is local only.")
    token = request.query_params.get("t") or request.headers.get("x-admin-token")
    if not token or not secrets.compare_digest(token, _admin_token):
        raise HTTPException(status_code=403, detail="Bad admin token.")


def _hub():
    from server.main import hub

    return hub


def _pairing_url() -> str:
    from server.main import hub
    from server.net import primary_lan_ip

    base = f"http://{primary_lan_ip()}:{_port}"
    if hub._trust.require_pin:
        return f"{base}/?pair={hub._trust.pairing_pin}"
    return base


@router.get("/state", dependencies=[Depends(require_admin)])
async def admin_state() -> dict[str, Any]:
    """Everything the desktop window needs to render (plan §4.1)."""
    from client.config import load_prefs
    from server.main import hub

    prefs = load_prefs(hub._trust._path.parent)
    store = hub._trust
    return {
        "version": __version__,
        "running": True,
        "engine_running": bool(_engine_running and _engine_running()),
        "port": _port,
        "pairing_url": _pairing_url(),
        "pairing_pin": store.pairing_pin if store.require_pin else "",
        "require_pin": store.require_pin,
        "connection_count": hub.connection_count,
        "devices": hub.devices(),
        "phones": store.phone_tokens(),
        "trusted_ids": store.trusted_devices(),
        "history": hub.get_history(),
        "settings": {
            "receive_folder": prefs.receive_folder,
            "auto_receive_max_bytes": prefs.auto_receive_max_bytes,
            "put_received_on_clipboard": prefs.put_received_on_clipboard,
            "share_private_clips": prefs.share_private_clips,
            "require_pin": store.require_pin,
        },
        "uptime_seconds": int(time.time() - _started_at),
    }


@router.post("/pairing/new", dependencies=[Depends(require_admin)])
async def new_pairing_code() -> dict[str, Any]:
    from server.main import hub

    return {"pin": hub._trust.regenerate_pin(), "pairing_url": _pairing_url()}


@router.delete("/devices/{device_id}", dependencies=[Depends(require_admin)])
async def remove_device(device_id: str) -> dict[str, Any]:
    """Unpair a phone by token id (12-hex) or legacy device id."""
    from server.main import hub

    store = hub._trust
    if store.revoke_token_prefix(device_id):
        return {"ok": True}
    store.untrust(device_id)
    return {"ok": True}


@router.post("/settings", dependencies=[Depends(require_admin)])
async def update_settings(body: dict) -> dict[str, Any]:
    """Persist receive/privacy settings (plan §4.2 drawer)."""
    from client.config import load_prefs, save_prefs
    from server.main import hub

    prefs = load_prefs(hub._trust._path.parent)
    if isinstance(body.get("receive_folder"), str):
        prefs.receive_folder = body["receive_folder"][:260]
    if isinstance(body.get("auto_receive_max_bytes"), int):
        prefs.auto_receive_max_bytes = max(0, body["auto_receive_max_bytes"])
    for key in ("put_received_on_clipboard", "share_private_clips"):
        if isinstance(body.get(key), bool):
            setattr(prefs, key, body[key])
    save_prefs(hub._trust._path.parent, prefs)
    if isinstance(body.get("require_pin"), bool):
        hub._trust.require_pin = body["require_pin"]
    return {"ok": True}


@router.get("/qr.png", dependencies=[Depends(require_admin)])
async def pairing_qr() -> Response:
    """Black-on-white QR of the pairing URL for the connect dialog."""
    import qrcode

    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(_pairing_url())
    qr.make(fit=True)
    img = qr.make_image(fill_color="#111827", back_color="#FFFFFF").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@router.post("/share", dependencies=[Depends(require_admin)])
async def share_paths(body: dict) -> dict[str, Any]:
    """Share local files/folders picked in the shell (plan §4.4 drag-drop).

    The shell resolves user picks to absolute paths; the server validates,
    stores, and broadcasts metadata. Loopback + token gate the trust.
    """
    import asyncio
    import uuid

    from server.folder_zip import zip_directory
    from server.main import UPLOADS_DIR, hub
    from server.models import ClipboardItem, get_utc_now_iso

    raw = body.get("paths")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="No files chosen.")
    shared: list[dict[str, Any]] = []
    for entry in raw[:50]:
        path = Path(str(entry)).expanduser()
        if not path.exists():
            continue
        try:
            if path.is_dir():
                zip_path, zip_name, entries, skipped = await asyncio.to_thread(
                    zip_directory, path
                )
                try:
                    if zip_path.stat().st_size > 1024 * 1024 * 1024:
                        continue
                    stored = f"{uuid.uuid4().hex[:10]}.zip"
                    (UPLOADS_DIR / stored).write_bytes(zip_path.read_bytes())
                    size = (UPLOADS_DIR / stored).stat().st_size
                finally:
                    zip_path.unlink(missing_ok=True)
                item = ClipboardItem(
                    device_id="This computer", timestamp=get_utc_now_iso(), type="folder",
                    content=f"Folder: {zip_name} ({entries} items)", filename=zip_name,
                    filesize=size, file_url=f"/api/files/{stored}",
                    entry_count=entries, skipped_count=skipped,
                )
            else:
                if path.stat().st_size > 2 * 1024 * 1024 * 1024:
                    continue
                stored = f"{uuid.uuid4().hex[:10]}{path.suffix[:10]}"
                with open(path, "rb") as src, open(UPLOADS_DIR / stored, "wb") as out:
                    import shutil as _shutil

                    _shutil.copyfileobj(src, out, length=1024 * 1024)
                item = ClipboardItem(
                    device_id="This computer", timestamp=get_utc_now_iso(), type="file",
                    content=f"File: {path.name}", filename=path.name,
                    filesize=path.stat().st_size, file_url=f"/api/files/{stored}",
                )
            await hub.handle_message(None, item.to_message_dict())
            shared.append({"filename": item.filename, "type": item.type})
        except Exception as exc:
            logger.warning("Admin share failed for %s: %s", entry, exc)
    return {"ok": True, "shared": shared}


@router.post("/engine", dependencies=[Depends(require_admin)])
async def engine_control(body: dict) -> dict[str, Any]:
    """Pause / resume sharing from the tray or settings drawer."""
    action = str(body.get("action", ""))
    if action == "stop" and _engine_stop is not None:
        _engine_stop()
        return {"ok": True, "running": False}
    if action == "start" and _engine_start is not None:
        _engine_start()
        return {"ok": True, "running": True}
    running = bool(_engine_running and _engine_running())
    return {"ok": False, "running": running}


@router.get("/diagnostics", dependencies=[Depends(require_admin)])
async def diagnostics() -> dict[str, Any]:
    """Port/firewall/storage self-check backing Test connection (plan §2.12)."""
    from server.main import UPLOADS_DIR, hub
    from server.net import find_open_port, lan_ips

    total = sum(p.stat().st_size for p in UPLOADS_DIR.glob("*") if p.is_file())
    return {
        "port": _port,
        "port_free": find_open_port(_port, count=1) == _port,
        "lan_ips": lan_ips(),
        "connection_count": hub.connection_count,
        "history_items": len(hub.get_history()),
        "shared_files": len([p for p in UPLOADS_DIR.iterdir() if p.is_file()]),
        "shared_bytes": total,
        "version": __version__,
    }
