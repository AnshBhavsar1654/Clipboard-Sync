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
import time
from collections.abc import Awaitable, Callable, Collection
from concurrent.futures import ThreadPoolExecutor
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

_DEBOUNCE_SECONDS = 0.06


def _seq_is_newer(cur: int, old: int) -> bool:
    """Wrap-safe 'cur is strictly newer than old' for 32-bit seq numbers."""
    if cur == old:
        return False
    return 0 < ((cur - old) & 0xFFFFFFFF) < 0x80000000


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
        skip_private: bool = True,
        blocked_apps: Collection[str] | None = None,
    ) -> None:
        self._loop = loop
        self._on_change = on_change
        self._backend: ClipboardBackend = backend or default_backend()
        self._skip_private = skip_private
        if blocked_apps and hasattr(self._backend, "blocked_owner_exes"):
            try:
                self._backend.blocked_owner_exes.update(a.lower() for a in blocked_apps)  # type: ignore[union-attr]
            except Exception:
                pass
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._stop_event = threading.Event()
        # Reads run here (max 1 at a time) so the Win32 pump never blocks
        # and bursts of updates coalesce via the debounce below (plan §2.5).
        self._reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipboard-read")
        self._reader_shutdown = False
        self._last_notify = 0.0

        self._last_fingerprint: str | None = None
        self._suppress_next_change = False
        self._own_seq = 0
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
        if not self._reader_shutdown:
            self._reader_shutdown = True
            self._reader.shutdown(wait=True)
        logger.info("Clipboard monitor stopped")

    def _note_own_write(self, payload: dict[str, Any] | None = None) -> None:
        """Record a programmatic write for echo suppression (plan §2.4).

        The flag is set before the write (cheap, synchronous); the clipboard
        sequence number is captured after, so the two notifications raised by
        EmptyClipboard()+SetClipboardData() are both recognised as ours.
        """
        with self._state_lock:
            self._suppress_next_change = True
            if payload is not None:
                self._last_fingerprint = fingerprint_payload(payload)
        try:
            seq = int(self._backend.sequence_number() or 0)
        except Exception:
            seq = 0
        with self._state_lock:
            self._own_seq = seq

    def _is_own_update(self) -> bool:
        """True if the pending clipboard update was written by us."""
        try:
            cur = int(self._backend.sequence_number() or 0)
        except Exception:
            cur = 0
        with self._state_lock:
            if self._own_seq and cur and not _seq_is_newer(cur, self._own_seq):
                self._suppress_next_change = False
                return True
            if self._suppress_next_change:
                self._suppress_next_change = False
                return True
            return False

    def set_text(self, text: str) -> None:
        """
        Write text to the local clipboard without triggering an outbound sync.

        Echo suppression is by clipboard sequence number (plan §2.4), with the
        one-shot flag as a fallback when the backend has no sequence number.
        """
        with self._state_lock:
            self._suppress_next_change = True

        self._backend.write_text(text)
        self._note_own_write({"type": "text", "content": text})

        logger.debug("Local clipboard updated programmatically (%d chars)", len(text))

    def set_image_from_base64(self, b64_data_uri: str) -> None:
        """Write base64-encoded image data to clipboard in CF_DIB format."""
        try:
            if "," in b64_data_uri:
                b64_str = b64_data_uri.split(",", 1)[1]
            else:
                b64_str = b64_data_uri

            raw_bytes = base64.b64decode(b64_str)
            self.set_image_from_bytes(raw_bytes)
        except Exception as exc:
            logger.warning("Failed to write image to clipboard: %s", exc)

    def set_image_from_bytes(self, raw_bytes: bytes) -> None:
        """Write decoded image bytes to the clipboard (plan §2.7)."""
        with self._state_lock:
            self._suppress_next_change = True

        self._backend.write_image_bytes(bytes(raw_bytes))
        self._note_own_write({"type": "image", "content": ""})

        logger.info("Set image (%d bytes) to clipboard programmatically", len(raw_bytes))

    def set_files(self, paths: list[Path]) -> None:
        """Put files on the clipboard as CF_HDROP (Phase 2.7 helper, via backend)."""
        with self._state_lock:
            self._suppress_next_change = True
        self._backend.write_files(list(paths))
        self._note_own_write()

    def get_clipboard_payload(self=None) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
        """Inspect system clipboard for text, images, files, or folders.

        Works both as ``monitor.get_clipboard_payload()`` (uses its backend)
        and as ``ClipboardMonitor.get_clipboard_payload()`` (default backend).
        """
        try:
            if isinstance(self, ClipboardMonitor):
                return self._backend.read_payload(skip_private=self._skip_private)
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
            self._notify_clipboard_changed()
        elif msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        elif msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _notify_clipboard_changed(self) -> None:
        """Called on the Win32 pump thread: hand the read to the worker so the
        pump never blocks and bursts coalesce via debounce (plan §2.5)."""
        self._last_notify = time.monotonic()
        if self._stop_event.is_set() or self._reader_shutdown:
            return
        try:
            self._reader.submit(self._read_and_dispatch)
        except RuntimeError:
            pass  # shutting down

    def _handle_clipboard_update(self) -> None:
        """Synchronous read path (kept for tests / direct callers)."""
        self._read_and_dispatch(skip_debounce=True)

    def _read_and_dispatch(self, skip_debounce: bool = False) -> None:
        if not skip_debounce:
            while True:  # coalesce bursts: wait for 60 ms of quiet
                wait = _DEBOUNCE_SECONDS - (time.monotonic() - self._last_notify)
                if wait <= 0 or self._stop_event.is_set():
                    break
                time.sleep(wait)
        if self._stop_event.is_set():
            return

        if self._is_own_update():
            logger.debug("Ignored programmatic clipboard update")
            return

        payload = self.get_clipboard_payload()  # type: ignore[call-arg]
        if payload is None:
            logger.debug("Clipboard changed but contains unsupported content")
            return

        # Full-content hash dedupe (plan §2.3): two different long clips that
        # share a prefix must both sync.
        fingerprint = fingerprint_payload(payload)
        with self._state_lock:
            if fingerprint == self._last_fingerprint:
                logger.debug("Ignored duplicate clipboard content")
                return
            self._last_fingerprint = fingerprint

        p_type = payload.get("type", "text")
        logger.info("Clipboard change detected (type=%s)", p_type)
        print(f"[CLIPBOARD] Captured {p_type} update")

        self._dispatch_change(payload)

    def _dispatch_change(self, payload: str | dict[str, Any]) -> None:
        """Schedule the async change handler on the main event loop."""
        result = self._on_change(payload)
        if asyncio.iscoroutine(result):
            asyncio.run_coroutine_threadsafe(result, self._loop)
