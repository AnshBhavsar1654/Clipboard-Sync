"""FastAPI application initialization for ClipBoardSync server and static web app."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from server.hub import SyncHub

logger = logging.getLogger(__name__)

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
async def upload_file_endpoint(file: UploadFile = File(...)) -> dict[str, Any]:
    """REST endpoint to upload files and images for cross-device sharing."""
    import uuid
    original_name = file.filename or "file"
    ext = Path(original_name).suffix
    safe_name = f"{uuid.uuid4().hex[:10]}{ext}"
    dest_path = UPLOADS_DIR / safe_name

    contents = await file.read()
    dest_path.write_bytes(contents)

    mime = file.content_type or ""
    item_type = "image" if mime.startswith("image/") or ext.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp") else "file"
    relative_url = f"/uploads/{safe_name}"

    logger.info("Uploaded %s (%d bytes) saved as %s", item_type, len(contents), safe_name)

    return {
        "url": relative_url,
        "filename": original_name,
        "filesize": len(contents),
        "type": item_type,
    }


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
