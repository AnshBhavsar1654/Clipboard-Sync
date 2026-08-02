"""Windows clipboard monitoring and manipulation via native Win32 APIs."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import logging
from pathlib import Path
import threading
import time
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from typing import Any

from PIL import Image, ImageGrab
import win32clipboard
import win32con
import win32gui

logger = logging.getLogger(__name__)

ClipboardChangeHandler = Callable[[str | dict[str, Any]], Awaitable[None] | None]

_WINDOW_CLASS = "ClipBoardSyncHiddenListener"
WM_CLIPBOARDUPDATE = 0x031D

_user32 = ctypes.windll.user32
_user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
_user32.AddClipboardFormatListener.restype = wintypes.BOOL
_user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
_user32.RemoveClipboardFormatListener.restype = wintypes.BOOL


def _add_clipboard_format_listener(hwnd: int) -> bool:
    return bool(_user32.AddClipboardFormatListener(hwnd))


def _remove_clipboard_format_listener(hwnd: int) -> bool:
    return bool(_user32.RemoveClipboardFormatListener(hwnd))


class ClipboardMonitor:
    """
    Monitors the system clipboard using AddClipboardFormatListener.

    Runs a dedicated Win32 message loop in a background thread so clipboard
    notifications do not block the asyncio event loop. Duplicate consecutive
    copies and programmatic updates initiated by this client are suppressed.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_change: ClipboardChangeHandler,
    ) -> None:
        self._loop = loop
        self._on_change = on_change
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._stop_event = threading.Event()

        self._last_content: str | None = None
        self._suppress_next_change = False
        self._state_lock = threading.Lock()

    def start(self) -> None:
        """Start the clipboard listener thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_message_loop,
            name="clipboard-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("Clipboard monitor started")

    def stop(self) -> None:
        """Signal the listener thread to stop and wait for it to exit."""
        self._stop_event.set()
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Clipboard monitor stopped")

    def set_text(self, text: str) -> None:
        """
        Write text to the local clipboard without triggering an outbound sync.

        Sets a suppression flag so the resulting WM_CLIPBOARDUPDATE notification
        is not treated as a user-initiated copy.
        """
        with self._state_lock:
            self._suppress_next_change = True
            self._last_content = text

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()

        logger.debug("Local clipboard updated programmatically (%d chars)", len(text))

    def set_image_from_base64(self, b64_data_uri: str) -> None:
        """Write base64-encoded image data to Win32 clipboard in CF_DIB format."""
        try:
            if "," in b64_data_uri:
                b64_str = b64_data_uri.split(",", 1)[1]
            else:
                b64_str = b64_data_uri

            raw_bytes = base64.b64decode(b64_str)
            img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

            buf = io.BytesIO()
            img.save(buf, "BMP")
            bmp_bytes = buf.getvalue()
            dib_bytes = bmp_bytes[14:]  # Omit 14-byte BMP header to get DIB

            with self._state_lock:
                self._suppress_next_change = True
                self._last_content = b64_data_uri[:100]

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_DIB, dib_bytes)
            finally:
                win32clipboard.CloseClipboard()

            logger.info("Set image (%d bytes DIB) to Windows clipboard programmatically", len(dib_bytes))
        except Exception as exc:
            logger.warning("Failed to write image to Windows clipboard: %s", exc)

    @staticmethod
    def get_clipboard_payload() -> dict[str, Any] | None:
        """Inspect system clipboard for text, images, or file drop lists."""
        # 1. Try reading Image or File list from clipboard using PIL ImageGrab
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
                }
            elif isinstance(grabbed, list) and grabbed:
                first_file = Path(grabbed[0])
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
                        }
                    else:
                        return {
                            "type": "file",
                            "content": f"File: {first_file.name}",
                            "filename": first_file.name,
                            "filesize": filesize,
                            "filepath": str(first_file.resolve()),
                        }
        except Exception as exc:
            logger.debug("ImageGrab inspect exception: %s", exc)

        # 2. Fallback to Unicode text reading
        text = ClipboardMonitor.get_text()
        if text and text.strip():
            return {
                "type": "text",
                "content": text,
            }
        return None

    @staticmethod
    def get_text() -> str | None:
        """Read Unicode text from the clipboard, or None if unavailable."""
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            logger.debug("Clipboard is locked by another process", exc_info=True)
            return None

        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return None
            data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            return data if isinstance(data, str) else None
        finally:
            win32clipboard.CloseClipboard()

    @staticmethod
    def _build_window_class() -> win32gui.WNDCLASS:
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = ClipboardMonitor._static_window_proc
        wc.lpszClassName = _WINDOW_CLASS
        return wc

    def _run_message_loop(self) -> None:
        """Create a hidden window, register for updates, and pump messages."""
        # Instance is bound before the window is created so the static proc can
        # dispatch back to this monitor from the Win32 callback thread.
        ClipboardMonitor._active_instance = self
        try:
            try:
                class_atom = win32gui.RegisterClass(self._build_window_class())
            except win32gui.error as exc:
                if getattr(exc, "winerror", None) != 1410:
                    raise
                class_atom = _WINDOW_CLASS

            self._hwnd = win32gui.CreateWindow(
                class_atom,
                "ClipBoardSync Listener",
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
            )

            if not _add_clipboard_format_listener(self._hwnd):
                raise RuntimeError("AddClipboardFormatListener failed")

            logger.debug("Registered clipboard format listener (hwnd=%s)", self._hwnd)

            while not self._stop_event.is_set():
                try:
                    result, msg = win32gui.GetMessage(None, 0, 0)
                except Exception:
                    break

                if result == 0 or self._stop_event.is_set():
                    break

                win32gui.TranslateMessage(msg)
                win32gui.DispatchMessage(msg)
        finally:
            if self._hwnd:
                try:
                    _remove_clipboard_format_listener(self._hwnd)
                except Exception:
                    logger.debug("Failed to remove clipboard listener", exc_info=True)
                try:
                    win32gui.DestroyWindow(self._hwnd)
                except Exception:
                    pass
                self._hwnd = None

            try:
                win32gui.UnregisterClass(_WINDOW_CLASS, None)
            except Exception:
                pass
            ClipboardMonitor._active_instance = None

    _active_instance: ClipboardMonitor | None = None

    @staticmethod
    def _static_window_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> Any:
        instance = ClipboardMonitor._active_instance
        if instance is None:
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
        return instance._window_proc(hwnd, msg, wparam, lparam)

    def _window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> Any:
        if msg == WM_CLIPBOARDUPDATE:
            self._handle_clipboard_update()
        elif msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        elif msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _handle_clipboard_update(self) -> None:
        with self._state_lock:
            if self._suppress_next_change:
                self._suppress_next_change = False
                logger.debug("Ignored programmatic clipboard update")
                return

        payload = self.get_clipboard_payload()
        if payload is None:
            logger.debug("Clipboard changed but contains unsupported content")
            return

        content_key = str(payload.get("content", ""))[:100]
        with self._state_lock:
            if content_key == self._last_content:
                logger.debug("Ignored duplicate clipboard content")
                return
            self._last_content = content_key

        p_type = payload.get("type", "text")
        logger.info("Clipboard change detected (type=%s)", p_type)
        print(f"[CLIPBOARD] Captured {p_type} update")

        self._dispatch_change(payload)

    def _dispatch_change(self, payload: str | dict[str, Any]) -> None:
        """Schedule the async change handler on the main event loop."""
        result = self._on_change(payload)
        if asyncio.iscoroutine(result):
            asyncio.run_coroutine_threadsafe(result, self._loop)
