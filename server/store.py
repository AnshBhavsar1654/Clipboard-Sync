"""Persistent history + shared-file lifecycle.

History survives restarts in SQLite (stdlib): clips table with a ``pinned``
column (pins are flags, not snapshots). Uploads live in the shared dir; the
janitor deletes files no clip references (pinned files are exempt) plus stale
``*.part`` leftovers. Defaults: 200 items / 30 days retention.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HISTORY_KEEP_ITEMS = 200
HISTORY_KEEP_DAYS = 30
PART_MAX_AGE_SECONDS = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'text',
    content TEXT NOT NULL DEFAULT '',
    filename TEXT,
    filesize INTEGER,
    file_url TEXT,
    thumb TEXT,
    entry_count INTEGER,
    skipped_count INTEGER,
    pinned INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created);
"""


def _default_db_path() -> Path:
    override = os.environ.get("CLIPBOARDSYNC_HISTORY_DB")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return base / "ClipBoardSync" / "history.db"
    return Path.home() / ".clipboardsync" / "history.db"


class HistoryStore:
    """SQLite-backed clip history with pins and retention."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else _default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    def add(self, item: dict[str, Any]) -> None:
        """Insert (or replace) a clip, then enforce retention."""
        with self._db:
            self._db.execute(
                """INSERT OR REPLACE INTO clips
                   (id, device_id, ts, type, content, filename, filesize,
                    file_url, thumb, entry_count, skipped_count, pinned, created)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                     (SELECT pinned FROM clips WHERE id = ?), 0), ?)""",
                (
                    item.get("id"), item.get("device_id", ""), item.get("timestamp", ""),
                    item.get("type", "text"), item.get("content", ""), item.get("filename"),
                    item.get("filesize"), item.get("file_url"), item.get("thumb"),
                    item.get("entry_count"), item.get("skipped_count"),
                    item.get("id"), time.time(),
                ),
            )
            self._db.execute(
                """DELETE FROM clips WHERE id NOT IN (
                       SELECT id FROM clips ORDER BY created DESC LIMIT ?)""",
                (HISTORY_KEEP_ITEMS,),
            )
            cutoff = time.time() - HISTORY_KEEP_DAYS * 86400
            self._db.execute("DELETE FROM clips WHERE created < ? AND pinned = 0", (cutoff,))

    def load_recent(self, limit: int) -> list[dict[str, Any]]:
        """Newest-first rows mapped back to message dicts (returned oldest-first)."""
        cur = self._db.execute(
            """SELECT id, device_id, ts, type, content, filename, filesize,
                      file_url, thumb, entry_count, skipped_count, pinned
               FROM clips ORDER BY created DESC LIMIT ?""",
            (limit,),
        )
        items = [dict(row) for row in cur.fetchall()]
        out: list[dict[str, Any]] = []
        for row in reversed(items):
            msg: dict[str, Any] = {
                "id": row["id"],
                "device_id": row["device_id"],
                "timestamp": row["ts"],
                "type": row["type"],
                "content": row["content"],
            }
            for key in ("filename", "filesize", "file_url", "thumb", "entry_count", "skipped_count"):
                if row[key] is not None:
                    msg[key] = row[key]
            if row["pinned"]:
                msg["pinned"] = True
            out.append(msg)
        return out

    def set_pinned(self, clip_id: str, pinned: bool) -> None:
        with self._db:
            self._db.execute(
                "UPDATE clips SET pinned = ? WHERE id = ?", (1 if pinned else 0, clip_id)
            )

    def remove(self, clip_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))

    def clear(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM clips")

    def referenced_file_ids(self) -> set[str]:
        """Basenames of /api/files/<id> links still referenced by history."""
        cur = self._db.execute("SELECT file_url FROM clips WHERE file_url IS NOT NULL")
        ids: set[str] = set()
        for (url,) in cur.fetchall():
            if url and "/api/files/" in url:
                ids.add(url.rsplit("/api/files/", 1)[-1].split("?", 1)[0].split("#", 1)[0])
        return ids


def sweep_uploads(uploads_dir: Path, keep_ids: set[str]) -> int:
    """Delete unreferenced shared files + stale *.part leftovers.

    Returns the number of files removed. Pinned files are kept by the caller
    passing their ids in ``keep_ids``.
    """
    removed = 0
    try:
        entries = list(uploads_dir.iterdir())
    except OSError:
        return 0
    now = time.time()
    for path in entries:
        try:
            if not path.is_file():
                continue
            if path.suffix == ".part":
                try:
                    if now - path.stat().st_mtime < PART_MAX_AGE_SECONDS:
                        continue
                except OSError:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            elif path.name not in keep_ids:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logger.debug("Janitor could not remove %s: %s", path, exc)
    if removed:
        logger.info("Janitor removed %d orphaned shared file(s).", removed)
    return removed


STAGING_MAX_AGE_SECONDS = 3600


def sweep_staging(uploads_dir: Path) -> int:
    """Remove abandoned folder-assembly staging dirs."""
    staging = uploads_dir / ".staging"
    try:
        entries = list(staging.iterdir())
    except OSError:
        return 0
    removed, now = 0, time.time()
    for path in entries:
        try:
            if path.is_dir() and now - path.stat().st_mtime > STAGING_MAX_AGE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError as exc:
            logger.debug("Janitor could not remove staging %s: %s", path, exc)
    return removed
