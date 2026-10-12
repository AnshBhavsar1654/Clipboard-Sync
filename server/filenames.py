"""Shared filename sanitizing.

One helper for every download path: the server upload endpoint, the desktop
client download, and the GUI peer download. Remote-supplied names must never
escape the destination folder (``..\\..\\Startup\\x.bat``) and Unicode names
must keep their extensions (``रिपोर्ट.pdf``).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_BAD_NAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str, fallback: str = "file") -> str:
    """Strip path separators, illegal chars and reserved Windows names.

    Unicode letters are preserved. Never returns ``""`` or ``"."``/``".."``.
    """
    name = unicodedata.normalize("NFC", name or "").replace("\\", "/").split("/")[-1].strip()
    name = _BAD_NAME_CHARS_RE.sub("_", name).strip(" .")
    stem = name.split(".")[0].upper()
    if not name or stem in _RESERVED_NAMES:
        name = f"_{name}" if name else fallback
    return name[:150]


def unique_path(directory: Path, name: str) -> Path:
    """Return ``directory / safe_name(name)``, suffixed with ``(1)`` etc. if
    taken. Guarantees the result stays inside ``directory``."""
    directory = directory.resolve()
    dest = directory / safe_name(name)
    stem, suffix, i = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = directory / f"{stem} ({i}){suffix}"
        i += 1
    assert dest.resolve().parent == directory
    return dest
