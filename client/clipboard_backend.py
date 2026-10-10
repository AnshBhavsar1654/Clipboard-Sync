"""Clipboard backend abstraction (Phase 0.2).

Goal: isolate Windows-only Win32 clipboard code behind a thin interface so
the rest of the client can be unit-tested on CI without a real clipboard.

- ``ClipboardBackend``: minimal interface used by ``ClipboardMonitor``.
- ``Win32ClipboardBackend``: real implementation (Windows only, lazy imports).
- ``FakeClipboardBackend``: in-memory implementation for tests / non-Windows CI.
- ``fingerprint_payload``: pure full-content hash (Phase 2.3 contract). Two
  payloads sharing only a prefix must NOT collide (the old ``[:100]`` key did).
"""

from __future__ import annotations

import base64
import hashlib
import io
import time
from pathlib import Path
from typing import Any, Protocol


def fingerprint_payload(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint of the full clipboard payload.

    Hashes ``type`` plus the full ``content`` / ``filepath`` / ``filename``
    and raw ``image_bytes`` when present. This is the Phase 2.3 replacement
    for the old ``str(content)[:100]`` dedupe key, which collided for two
    different long URLs sharing a 100-char prefix.
    """
    h = hashlib.sha256()
    h.update(str(payload.get("type", "")).encode("utf-8"))
    for key in ("content", "filepath", "filename"):
        v = payload.get(key)
        if v:
            h.update(str(v).encode("utf-8", "surrogatepass"))
    blob = payload.get("image_bytes")
    if isinstance(blob, (bytes, bytearray)) and blob:
        h.update(bytes(blob))
    return h.hexdigest()


class ClipboardBackend(Protocol):
    """Thin interface over OS clipboard access."""

    def read_payload(self) -> dict[str, Any] | None:
        """Inspect the OS clipboard, return a payload dict or None."""
        ...

    def write_text(self, text: str) -> None:
        ...

    def write_image_bytes(self, raw_bytes: bytes) -> None:
        """Write decoded image bytes (PNG/JPEG/...) to the clipboard as DIB."""
        ...

    def write_files(self, paths: list[Path]) -> None:
        """Put file paths on the clipboard as CF_HDROP (Phase 2.7)."""
        ...

    def read_text(self) -> str | None:
        ...

    def sequence_number(self) -> int:
        """Monotonic clipboard sequence number for echo suppression (Phase 2.4)."""
        ...


class FakeClipboardBackend:
    """In-memory clipboard for tests and non-Windows CI."""

    def __init__(self) -> None:
        self._text: str | None = None
        self._image_bytes: bytes | None = None
        self._files: list[Path] = []
        self._seq = 0
        self.writes: list[tuple[str, Any]] = []

    def read_payload(self) -> dict[str, Any] | None:
        if self._files:
            first = self._files[0]
            return {
                "type": "file",
                "content": f"File: {first.name}",
                "filename": first.name,
                "filepath": str(first),
            }
        if self._image_bytes is not None:
            b64 = base64.b64encode(self._image_bytes).decode("ascii")
            return {
                "type": "image",
                "content": f"data:image/png;base64,{b64}",
                "filename": f"screenshot_{int(time.time())}.png",
                "filesize": len(self._image_bytes),
                "image_bytes": self._image_bytes,
            }
        if self._text and self._text.strip():
            return {"type": "text", "content": self._text}
        return None

    def write_text(self, text: str) -> None:
        self._text = text
        self._image_bytes = None
        self._files = []
        self._seq += 1
        self.writes.append(("text", text))

    def write_image_bytes(self, raw_bytes: bytes) -> None:
        self._image_bytes = bytes(raw_bytes)
        self._text = None
        self._files = []
        self._seq += 1
        self.writes.append(("image", len(raw_bytes)))

    def write_files(self, paths: list[Path]) -> None:
        self._files = list(paths)
        self._text = None
        self._image_bytes = None
        self._seq += 1
        self.writes.append(("files", [str(p) for p in paths]))

    def read_text(self) -> str | None:
        return self._text

    def sequence_number(self) -> int:
        return self._seq


class Win32ClipboardBackend:
    """Real Windows clipboard backend. All win32 imports are lazy so this
    module stays importable on Linux CI (instantiation raises on non-Windows).
    """

    def read_payload(self) -> dict[str, Any] | None:
        try:
            from PIL import Image, ImageGrab
        except Exception:
            return self.read_text_payload()
        try:
            grabbed = ImageGrab.grabclipboard()
            if isinstance(grabbed, Image.Image):
                buf = io.BytesIO()
                grabbed.save(buf, format="PNG")
                png_bytes = buf.getvalue()
                b64_str = base64.b64encode(png_bytes).decode("utf-8")
                return {
                    "type": "image",
                    "content": f"data:image/png;base64,{b64_str}",
                    "filename": f"screenshot_{int(time.time())}.png",
                    "filesize": len(png_bytes),
                    "image_bytes": png_bytes,
                }
            elif isinstance(grabbed, list) and grabbed:
                paths = [Path(p) for p in grabbed]
                dirs = [p for p in paths if p.is_dir()]
                files = [p for p in paths if p.is_file()]
                if dirs:
                    folder = dirs[0].resolve()
                    return {
                        "type": "folder",
                        "content": f"Folder: {folder.name}",
                        "filename": folder.name,
                        "filepath": str(folder),
                        "entry_count": len(files) + len(dirs),
                    }
                if len(files) > 1:
                    first_file = files[0]
                    return {
                        "type": "file",
                        "content": f"File: {first_file.name} (+{len(files) - 1} more)",
                        "filename": first_file.name,
                        "filesize": first_file.stat().st_size,
                        "filepath": str(first_file.resolve()),
                    }
                first_file = files[0] if files else Path(grabbed[0])
                if first_file.is_file():
                    ext = first_file.suffix.lower()
                    filesize = first_file.stat().st_size
                    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp") and filesize < 10 * 1024 * 1024:
                        raw = first_file.read_bytes()
                        mime = f"image/{ext[1:]}" if ext != ".jpg" else "image/jpeg"
                        b64_str = base64.b64encode(raw).decode("utf-8")
                        return {
                            "type": "image",
                            "content": f"data:{mime};base64,{b64_str}",
                            "filename": first_file.name,
                            "filesize": filesize,
                            "image_bytes": raw,
                        }
                    return {
                        "type": "file",
                        "content": f"File: {first_file.name}",
                        "filename": first_file.name,
                        "filesize": filesize,
                        "filepath": str(first_file.resolve()),
                    }
        except Exception:
            pass
        return self.read_text_payload()

    def read_text_payload(self) -> dict[str, Any] | None:
        text = self.read_text()
        if text and text.strip():
            return {"type": "text", "content": text}
        return None

    def read_text(self) -> str | None:
        try:
            import win32clipboard
            import win32con
        except ImportError:
            return None
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            return None
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return None
            data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            return data if isinstance(data, str) else None
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

    def _open(self) -> None:
        import win32clipboard

        win32clipboard.OpenClipboard()

    def write_text(self, text: str) -> None:
        import win32clipboard
        import win32con

        self._open()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()

    def write_image_bytes(self, raw_bytes: bytes) -> None:
        import io as _io

        import win32clipboard
        import win32con
        from PIL import Image

        img = Image.open(_io.BytesIO(raw_bytes)).convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, "BMP")
        dib_bytes = buf.getvalue()[14:]
        self._open()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, dib_bytes)
        finally:
            win32clipboard.CloseClipboard()

    def write_files(self, paths: list[Path]) -> None:
        import struct

        import win32clipboard
        import win32con

        wide = ("\0".join(str(p) for p in paths) + "\0\0").encode("utf-16-le")
        dropfiles = struct.pack("IiiII", 20, 0, 0, 0, 1)
        self._open()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_HDROP, dropfiles + wide)
            effect = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
            win32clipboard.SetClipboardData(effect, struct.pack("I", 1))
        finally:
            win32clipboard.CloseClipboard()

    def sequence_number(self) -> int:
        try:
            import win32clipboard

            return int(win32clipboard.GetClipboardSequenceNumber())
        except Exception:
            return 0


def default_backend() -> ClipboardBackend:
    """Return the Win32 backend on Windows, otherwise a Fake (import-safe)."""
    import sys

    if sys.platform == "win32":
        return Win32ClipboardBackend()
    return FakeClipboardBackend()
