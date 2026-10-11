"""FastAPI application initialization for ClipBoardSync server and static web app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from server import __version__
from server.admin import router as admin_router
from server.auth import is_loopback_host
from server.filenames import safe_name
from server.hub import SyncHub

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB hard cap
CHUNK_SIZE = 1024 * 1024  # 1 MB streaming chunks


def _sanitize_filename(name: str) -> str:
    return safe_name(name)[:120] or "file"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def is_loopback(request: Request) -> bool:
    return is_loopback_host(client_ip(request))


async def require_device(request: Request) -> None:
    """HTTP pairing gate (plan §1.2): everything exposing clip data needs a
    paired device, except loopback (the desktop app itself) and open mode."""
    store = hub._trust
    if is_loopback(request) or not store.require_pin:
        return
    if not store.verify_token(request.cookies.get("cs_token")):
        raise HTTPException(status_code=401, detail="Pair this phone first.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Background hygiene (plan §2.9/§3.3): silent-socket sweep every 30 s,
    orphaned-file sweep every 10 min."""
    if hub.uploads_dir is None:
        hub.uploads_dir = UPLOADS_DIR
    hub._sweep_uploads()
    stop = asyncio.Event()

    async def _janitor() -> None:
        try:
            ticks = 0
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=30)
                except TimeoutError:
                    pass
                if stop.is_set():
                    break
                ticks += 1
                try:
                    await hub.close_silent_sockets()
                except Exception as exc:
                    logger.debug("Silent sweep failed: %s", exc)
                if ticks % 20 == 0:
                    hub._sweep_uploads()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_janitor(), name="upload-janitor")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="ClipBoardSync Backend",
    description="Cross-device local Wi-Fi clipboard synchronization engine.",
    version=__version__,
    lifespan=lifespan,
)

# No CORS middleware: the phone page is same-origin and needs none (plan §1.4).
# A wildcard CORS policy with credentials would let any website read this
# PC's clips via loopback; unsafe cross-site writes are rejected below.

hub = SyncHub(max_history=25)

app.include_router(admin_router)


@app.middleware("http")
async def reject_cross_site(request: Request, call_next):
    """Block cross-site writes (CSRF / DNS-rebinding vs 127.0.0.1, plan §1.4)."""
    origin = request.headers.get("origin")
    if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
        if urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    STATIC_DIR = Path(sys._MEIPASS) / "server" / "static"
else:
    STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


def _shared_dir() -> Path:
    override = os.environ.get("CLIPBOARDSYNC_SHARED_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return base / "ClipBoardSync" / "shared"
    return Path.home() / ".clipboardsync" / "shared"


# Shared files live OUTSIDE the static mount so /uploads/<name> is never
# served without auth; downloads go through /api/files/{id} (plan §1.5).
UPLOADS_DIR = _shared_dir()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/history", dependencies=[Depends(require_device)])
async def get_recent_history() -> dict[str, Any]:
    """REST endpoint to inspect active server clipboard history."""
    return {"items": hub.get_history(), "connection_count": hub.connection_count}


@app.get("/api/devices", dependencies=[Depends(require_device)])
async def get_devices() -> dict[str, Any]:
    """Connected devices with names (plan §2.10)."""
    devices = hub.devices()
    return {"devices": devices, "connection_count": len(devices)}


@app.get("/api/health")
async def get_health() -> dict[str, Any]:
    """Unauthenticated liveness probe (used by the exe smoke test)."""
    return {"ok": True, "version": app.version}


@app.get("/api/info")
async def get_info() -> dict[str, Any]:
    """Public computer info for the phone header (plan §3.6)."""
    import socket as _socket

    try:
        name = _socket.gethostname()
    except Exception:
        name = "This computer"
    return {"computer_name": name, "version": app.version}


@app.delete("/api/history/{clip_id}", dependencies=[Depends(require_device)])
async def delete_clip(clip_id: str) -> dict[str, Any]:
    """Delete one clip everywhere, including its file (plan §2.9/§3.6)."""
    if not await hub.remove_item(clip_id):
        raise HTTPException(status_code=404, detail="Clip not found.")
    return {"ok": True}


@app.delete("/api/history", dependencies=[Depends(require_device)])
async def clear_history() -> dict[str, Any]:
    """Really delete history; pinned files are exempt (plan §2.9)."""
    await hub.clear_history()
    return {"ok": True}


@app.post("/api/history/{clip_id}/pin", dependencies=[Depends(require_device)])
async def pin_clip(clip_id: str, body: dict) -> dict[str, Any]:
    """Pin/unpin a clip: {"pinned": true/false} (plan §2.9)."""
    await hub.set_pinned(clip_id, bool(body.get("pinned", True)))
    return {"ok": True}


@app.post("/api/pair")
async def pair_device(request: Request, response: Response, body: dict) -> dict[str, Any]:
    """Pair a phone over HTTP: trade the single-use pairing code for a
    server-issued device token stored in an HttpOnly cookie (plan §1.2)."""
    store, ip = hub._trust, client_ip(request)
    if store.locked_out(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many tries. Wait a few minutes or ask for a new code.",
        )
    if not store.validate_pin(str(body.get("pin", ""))):
        store.record_fail(ip)
        raise HTTPException(status_code=401, detail="That code did not match.")
    token = store.issue_token(str(body.get("name", "Phone"))[:40])
    store.clear_fails(ip)
    store.regenerate_pin()  # single-use code
    response.set_cookie(
        "cs_token", token, max_age=60 * 60 * 24 * 365,
        httponly=True, samesite="lax", path="/",
    )
    return {"ok": True}


_FOLDER_ID_RE = re.compile(r"[0-9a-f]{16,32}")
FOLDER_MAX_BYTES = 1024 * 1024 * 1024  # 1 GB cap for phone folder shares


def _staging_dir(folder_id: str) -> Path:
    return UPLOADS_DIR / ".staging" / folder_id


async def _stream_upload(file: UploadFile, dest: Path) -> int:
    """Stream an upload to ``dest`` via ``.part`` (never fully in RAM).

    Returns bytes written. Aborts leave no ``.part`` behind (plan §2.14).
    """
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    total = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    out.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail="That file is too big to share (over 2 GB).",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Upload interrupted. Try again.")
    finally:
        try:
            await file.close()
        except Exception:
            pass
    shutil.move(str(tmp_path), str(dest))
    return total


@app.post("/api/upload", dependencies=[Depends(require_device)])
async def upload_file_endpoint(
    request: Request,
    file: UploadFile = File(...),
    item_type: str | None = Form(default=None),
    entry_count: int | None = Form(default=None),
    skipped_count: int | None = Form(default=None),
    folder_id: str | None = Form(default=None),
    relpath: str | None = Form(default=None),
) -> dict[str, Any]:
    """Stream an upload to disk (never fully in RAM) and return a fast download link.

    With ``folder_id`` + ``relpath`` the file is staged for a later
    ``POST /api/folder/commit`` instead (plan §3.5: constant phone memory).
    """
    import uuid

    # Early reject from Content-Length before reading anything (plan §2.14).
    try:
        claimed = int(request.headers.get("content-length") or 0)
    except ValueError:
        claimed = 0
    if claimed > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="That file is too big to share (over 2 GB).",
        )

    if folder_id is not None:
        return await _stage_file(file, folder_id, relpath)

    original_name = _sanitize_filename(file.filename or "file")
    ext = Path(original_name).suffix
    stored_name = f"{uuid.uuid4().hex[:10]}{ext}"
    dest_path = UPLOADS_DIR / stored_name
    total = await _stream_upload(file, dest_path)

    mime = file.content_type or ""
    if item_type in ("file", "image", "folder"):
        resolved_type = item_type
    else:
        resolved_type = "image" if mime.startswith("image/") or ext.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp") else "file"
    relative_url = f"/api/files/{stored_name}"

    logger.info("Uploaded %s (%d bytes) saved as %s", resolved_type, total, stored_name)

    payload: dict[str, Any] = {
        "url": relative_url,
        "filename": original_name,
        "filesize": total,
        "type": resolved_type,
    }
    if entry_count is not None:
        payload["entry_count"] = entry_count
    if skipped_count is not None:
        payload["skipped_count"] = skipped_count
    # Instant list rendering for every sender (plan §2.2): small images get a
    # ≤24 KB thumb; full bytes stay behind /api/files/{id}.
    if resolved_type == "image" and 0 < total <= 25 * 1024 * 1024:
        try:
            from server.media import make_thumb

            thumb = make_thumb(dest_path.read_bytes())
            if thumb:
                payload["thumb"] = thumb
        except Exception:
            pass
    return payload


async def _stage_file(file: UploadFile, folder_id: str, relpath: str | None) -> dict[str, Any]:
    """Stage one folder member for a later commit (plan §3.5)."""
    if not _FOLDER_ID_RE.fullmatch(folder_id or ""):
        raise HTTPException(status_code=400, detail="Bad folder share. Start over.")
    parts = [
        p for p in (safe_name(p) for p in (relpath or "").replace("\\", "/").split("/"))
        if p and p not in (".", "..")
    ]
    if not parts:
        raise HTTPException(status_code=400, detail="Missing file path. Start over.")
    root = _staging_dir(folder_id)
    parent = root.joinpath(*parts[:-1]) if len(parts) > 1 else root
    parent.mkdir(parents=True, exist_ok=True)
    try:
        # Segments carry no separators by construction; assert containment anyway.
        assert parent.resolve().is_relative_to(root.resolve())
    except (AssertionError, RuntimeError, ValueError):
        raise HTTPException(status_code=400, detail="Bad file path. Start over.")
    size = await _stream_upload(file, parent / parts[-1])
    return {"staged": True, "folder_id": folder_id, "relpath": "/".join(parts), "size": size}


@app.post("/api/folder/commit", dependencies=[Depends(require_device)])
async def commit_folder(body: dict) -> dict[str, Any]:
    """Assemble staged files into one zip, server-side (plan §3.5)."""
    import uuid
    import zipfile

    folder_id = str(body.get("folder_id", ""))
    if not _FOLDER_ID_RE.fullmatch(folder_id):
        raise HTTPException(status_code=404, detail="Folder share not found.")
    root = _staging_dir(folder_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Folder share not found.")
    try:
        members = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix != ".part")
        total = sum(p.stat().st_size for p in members)
        if not members:
            raise HTTPException(status_code=400, detail="Nothing to share.")
        if total > FOLDER_MAX_BYTES:
            raise HTTPException(status_code=413, detail="That folder is too big to share (over 1 GB).")
        zip_name = f"{uuid.uuid4().hex[:10]}.zip"
        dest = UPLOADS_DIR / zip_name
        tmp = dest.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in members:
                zf.write(path, path.relative_to(root).as_posix())
        shutil.move(str(tmp), str(dest))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    folder_name = _sanitize_filename(str(body.get("name") or "folder"))
    logger.info("Committed folder '%s' (%d files, %d bytes) as %s", folder_name, len(members), total, zip_name)
    return {
        "url": f"/api/files/{zip_name}",
        "filename": f"{folder_name}.zip",
        "filesize": dest.stat().st_size,
        "type": "folder",
        "entry_count": len(members),
    }


@app.get("/api/files/{name}", dependencies=[Depends(require_device)])
async def download_file_endpoint(name: str, request: Request):
    """Fast direct download with resume (Range) + attachment disposition."""
    safe = _sanitize_filename(name)
    # Only allow names we created: 10 hex chars + optional extension
    if not re.fullmatch(r"[0-9a-f]{10}(\.[A-Za-z0-9]{1,10})?", safe):
        return JSONResponse(status_code=404, content={"detail": "File not found."})
    path = UPLOADS_DIR / safe
    if not path.is_file():
        # Back-compat: old /uploads/ links created before this endpoint
        return JSONResponse(status_code=404, content={"detail": "File not found."})
    size = path.stat().st_size
    range_header = request.headers.get("range") if size else None
    download_name = _sanitize_filename(request.query_params.get("filename", safe))
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{download_name}"',
    }
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if m:
            start_s, end_s = m.groups()
            try:
                start = int(start_s) if start_s else max(size - int(end_s or 0), 0)
                end = int(end_s) if end_s else size - 1
            except ValueError:
                start, end = 0, size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            length = end - start + 1

            def _iter_range():
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            from starlette.responses import StreamingResponse
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return StreamingResponse(
                _iter_range(),
                status_code=206,
                media_type="application/octet-stream",
                headers={**headers, "Content-Length": str(length)},
            )
    return FileResponse(
        str(path),
        media_type="application/octet-stream",
        filename=download_name,
        headers=headers,
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Real-time bidirectional WebSocket syncing endpoint for desktop and mobile clients."""
    await hub.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                if isinstance(message, dict):
                    await hub.handle_message(websocket, message)
                else:
                    logger.warning("Received non-dictionary payload on WebSocket: %s", data)
            except json.JSONDecodeError:
                logger.warning("Received malformed JSON on WebSocket: %s", data)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("Unexpected error in WebSocket session: %s", exc)
    finally:
        # Idempotent: guarantees no ghost entries even when the task is
        # cancelled before the disconnect message is processed.
        await hub.disconnect(websocket)


# Mount the frontend web app at root path `/`
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
