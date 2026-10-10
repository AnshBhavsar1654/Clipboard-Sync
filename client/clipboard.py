"""Windows clipboard monitoring and manipulation via native Win32 APIs.

Phase 0.2: Win32 access lives in :mod:`client.clipboard_backend` behind the
``ClipboardBackend`` interface. This monitor keeps its public API but
delegates reads/writes to the backend so tests can inject ``FakeClipboard``.
Win32 GUI imports are lazy so this module imports on Linux CI.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from client.clipboard_backend import (
    ClipboardBackend,
    FakeClipboardBackend,
    default_backend,
    fingerprint_payload,
)

logger = logging.getLogger(__name__)

ClipboardChangeHandler = Callable[[str | dict[str, Any]], Awaitable[None] | None]

# Back-compat alias used by tests / external code.
FakeClipboard = FakeClipboardBackend

_WINDOW_CLASS = "ClipBoardSyncHiddenListener"
WM_CLIPBOARDUPDATE = 0x031D


def _add_clipboard_format_listener(hwnd: int) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
    user32.AddClipboardFormatListener.restype = wintypes.BOOL
    return bool(user32.AddClipboardFormatListener(hwnd))


def _remove_clipboard_format_listener(hwnd: int) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
    user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
    return bool(user32.RemoveClipboardFormatListener(hwnd))


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
        backend: ClipboardBackend | None = None,
    ) -> None:
        self._loop = loop
        self._on_change = on_change
        self._backend: ClipboardBackend = backend or default_backend()
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._stop_event = threading.Event()

        self._last_content: str | None = None
        self._last_fingerprint: str | None = None
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
            try:
                import win32con
                import win32gui

                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except ImportError:
                pass
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
            self._last_fingerprint = fingerprint_payload({"type": "text", "content": text})

        self._backend.write_text(text)

        logger.debug("Local clipboard updated programmatically (%d chars)", len(text))

    def set_image_from_base64(self, b64_data_uri: str) -> None:
        """Write base64-encoded image data to clipboard in CF_DIB format."""
        try:
            if "," in b64_data_uri:
                b64_str = b64_data_uri.split(",", 1)[1]
            else:
                b64_str = b64_data_uri

            raw_bytes = base64.b64decode(b64_str)

            with self._state_lock:
                self._suppress_next_change = True
                # TODO(Phase 2.3/2.4): replace prefix key with full fingerprint +
                # clipboard sequence-number echo suppression. Kept as-is in Phase 0
                # so the regression test documents the collision.
                self._last_content = b64_data_uri[:100]
                self._last_fingerprint = fingerprint_payload(
                    {"type": "image", "content": b64_data_uri}
                )

            self._backend.write_image_bytes(raw_bytes)

            logger.info("Set image (%d bytes) to clipboard programmatically", len(raw_bytes))
        except Exception as exc:
            logger.warning("Failed to write image to clipboard: %s", exc)

    def set_files(self, paths: list[Path]) -> None:
        """Put files on the clipboard as CF_HDROP (Phase 2.7 helper, via backend)."""
        with self._state_lock:
            self._suppress_next_change = True
        self._backend.write_files(list(paths))

    def get_clipboard_payload(self=None) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
        """Inspect system clipboard for text, images, files, or folders.

        Works both as ``monitor.get_clipboard_payload()`` (uses its backend)
        and as ``ClipboardMonitor.get_clipboard_payload()`` (default backend).
        """
        try:
            if isinstance(self, ClipboardMonitor):
                return self._backend.read_payload()
            return default_backend().read_payload()
        except Exception as exc:
            logger.debug("Backend read_payload failed: %s", exc)
            return None

    @staticmethod
    def get_text() -> str | None:
        """Read Unicode text from the clipboard, or None if unavailable."""
        try:
            return default_backend().read_text()
        except Exception:
            logger.debug("Clipboard read failed", exc_info=True)
            return None

    @staticmethod
    def _build_window_class():  # type: ignore[no-untyped-def]
        import win32gui

        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = ClipboardMonitor._static_window_proc
        wc.lpszClassName = _WINDOW_CLASS
        return wc

    def _run_message_loop(self) -> None:
        """Create a hidden window, register for updates, and pump messages."""
        try:
            import win32gui
        except ImportError:
            logger.warning("Clipboard monitor needs Windows (win32gui missing); idle.")
            return
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
        import win32gui

        instance = ClipboardMonitor._active_instance
        if instance is None:
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
        return instance._window_proc(hwnd, msg, wparam, lparam)

    def _window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> Any:
        import win32con
        import win32gui

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

        payload = self.get_clipboard_payload()  # type: ignore[call-arg]
        if payload is None:
            logger.debug("Clipboard changed but contains unsupported content")
            return

        # TODO(Phase 2.3): replace with fingerprint_payload(payload) full-hash
        # dedupe. Kept as [:100] in Phase 0 so the regression test documents it.
        content_key = str(payload.get("content", ""))[:100]
        with self._state_lock:
            if content_key == self._last_content:
                logger.debug("Ignored duplicate clipboard content")
                return
            self._last_content = content_key
            self._last_fingerprint = fingerprint_payload(payload)

        p_type = payload.get("type", "text")
        logger.info("Clipboard change detected (type=%s)", p_type)
        print(f"[CLIPBOARD] Captured {p_type} update")

        self._dispatch_change(payload)

    def _dispatch_change(self, payload: str | dict[str, Any]) -> None:
        """Schedule the async change handler on the main event loop."""
        result = self._on_change(payload)
        if asyncio.iscoroutine(result):
            asyncio.run_coroutine_threadsafe(result, self._loop)
