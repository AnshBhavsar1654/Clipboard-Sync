"""FastAPI application initialization for ClipBoardSync server and static web app."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from server.hub import SyncHub

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB hard cap
CHUNK_SIZE = 1024 * 1024  # 1 MB streaming chunks

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str) -> str:
    name = (name or "file").strip().replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME_RE.sub("_", name).strip("._") or "file"
    return name[:120]

app = FastAPI(
    title="ClipBoardSync Backend",
    description="Cross-device local Wi-Fi clipboard synchronization engine.",
    version="1.0.0",
)

# Permit local developers and private subnet connections
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

hub = SyncHub(max_history=25)

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    STATIC_DIR = Path(sys._MEIPASS) / "server" / "static"
else:
    STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

UPLOADS_DIR = STATIC_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/history")
async def get_recent_history() -> dict[str, Any]:
    """REST endpoint to inspect active server clipboard history."""
    return {"items": hub.get_history(), "connection_count": hub.connection_count}


@app.post("/api/upload")
async def upload_file_endpoint(
    file: UploadFile = File(...),
    item_type: str | None = Form(default=None),
    entry_count: int | None = Form(default=None),
    skipped_count: int | None = Form(default=None),
) -> dict[str, Any]:
    """Stream an upload to disk (never fully in RAM) and return a fast download link."""
    import uuid
    original_name = _sanitize_filename(file.filename or "file")
    ext = Path(original_name).suffix
    safe_name = f"{uuid.uuid4().hex[:10]}{ext}"
    dest_path = UPLOADS_DIR / safe_name
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")

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
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "That file is too big to share (over 2 GB)."},
                    )
                out.write(chunk)
    finally:
        try:
            await file.close()
        except Exception:
            pass
    shutil.move(str(tmp_path), str(dest_path))

    mime = file.content_type or ""
    if item_type in ("file", "image", "folder"):
        resolved_type = item_type
    else:
        resolved_type = "image" if mime.startswith("image/") or ext.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp") else "file"
        if original_name.lower().endswith(".zip") and resolved_type == "file":
            pass
    relative_url = f"/api/files/{safe_name}"

    logger.info("Uploaded %s (%d bytes) saved as %s", resolved_type, total, safe_name)

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
    return payload


@app.get("/api/files/{name}")
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
    range_header = request.headers.get("range")
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
        await hub.disconnect(websocket)
    except Exception as exc:
        logger.exception("Unexpected error in WebSocket session: %s", exc)
        await hub.disconnect(websocket)


# Mount the frontend web app at root path `/`
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
