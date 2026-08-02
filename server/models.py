"""Data models for WebSocket sync messages and clipboard items."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field


def get_utc_now_iso() -> str:
    """Return ISO-formatted current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


class ClipboardItem(BaseModel):
    """Represents a single captured clipboard event across any connected device."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    device_id: str
    timestamp: str = Field(default_factory=get_utc_now_iso)
    type: str = "text"  # "text", "image", "file"
    content: str = ""
    filename: str | None = None
    filesize: int | None = None
    file_url: str | None = None

    def to_message_dict(self) -> dict[str, Any]:
        """Convert to standard broadcast dictionary payload."""
        data = {
            "id": self.id,
            "device_id": self.device_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "content": self.content,
        }
        if self.filename is not None:
            data["filename"] = self.filename
        if self.filesize is not None:
            data["filesize"] = self.filesize
        if self.file_url is not None:
            data["file_url"] = self.file_url
        return data


class HistoryMessage(BaseModel):
    """Payload sent upon initial client connection with recent clipboard items."""

    type: Literal["history"] = "history"
    device_id: str = "server"
    timestamp: str = Field(default_factory=get_utc_now_iso)
    items: list[dict[str, Any]]
