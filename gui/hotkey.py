"""Global Ctrl+Shift+V quick-paste hotkey (plan §4.2/§4.6, Windows only).

Records the foreground window before firing so paste-through can refocus it.
No-op on other platforms (import-safe everywhere).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

HOTKEY_ID = 1
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
VK_V = 0x56
WM_HOTKEY = 0x0312

_last_hwnd: int = 0


def last_hotkey_hwnd() -> int:
    return _last_hwnd


class HotkeyThread(threading.Thread):
    """Registers the hotkey and pumps a message loop until stopped."""

    def __init__(self, on_hotkey: Callable[[], None]) -> None:
        super().__init__(name="clipboardsync-hotkey", daemon=True)
        self._on_hotkey = on_hotkey
        self._stop_event = threading.Event()
        self._tid: int = 0

    def run(self) -> None:
        import ctypes
        from ctypes import wintypes

        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
        except Exception as exc:
            logger.warning("Global hotkey needs Windows: %s", exc)
            return
        self._tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT, VK_V):
            logger.warning("Could not register Ctrl+Shift+V (maybe taken). Quick paste disabled.")
            return
        try:
            msg = wintypes.MSG()
            while not self._stop_event.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    global _last_hwnd
                    try:
                        _last_hwnd = user32.GetForegroundWindow() or 0
                    except Exception:
                        _last_hwnd = 0
                    try:
                        self._on_hotkey()
                    except Exception as exc:
                        logger.warning("Hotkey handler failed: %s", exc)
                try:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                except Exception:
                    pass
        finally:
            try:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_event.set()
        try:
            import ctypes

            if self._tid:
                ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
        except Exception:
            pass
        self.join(timeout=3.0)


def start_hotkey(on_hotkey: Callable[[], None]) -> HotkeyThread | None:
    """Start the listener; None where unsupported."""
    import sys

    if sys.platform != "win32":
        return None
    thread = HotkeyThread(on_hotkey)
    thread.start()
    return thread
