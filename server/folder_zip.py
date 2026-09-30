"""Shared folder-zip helper: zip a directory for sharing without loading it in RAM."""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

EXCLUDE_DIRS = {".venv", "__pycache__", "node_modules", ".git", ".hg", ".svn"}
EXCLUDE_SUFFIXES = {".part", ".tmp"}


def should_skip(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    if any(part in EXCLUDE_DIRS for part in rel_parts):
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def zip_directory(source_dir: str | Path) -> tuple[Path, str, int, int]:
    """Zip a folder to a temp .zip. Returns (zip_path, zip_name, entry_count, skipped)."""
    root = Path(source_dir).resolve()
    zip_name = f"{root.name}.zip"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = Path(tmp.name)
    tmp.close()
    entries = 0
    skipped = 0
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            if should_skip(path, root):
                skipped += 1
                continue
            arcname = path.relative_to(root).as_posix()
            zf.write(path, arcname)
            entries += 1
    return tmp_path, zip_name, entries, skipped
