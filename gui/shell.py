"""pywebview desktop shell: window + JS bridge (plan §4.1/§4.6)."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ShellApi:
    """Python functions callable from the desktop page via window.pywebview.api."""

    def __init__(self, engine, get_window, get_last_hwnd):
        self._engine = engine
        self._get_window = get_window
        self._get_last_hwnd = get_last_hwnd

    # -- file pickers (native dialogs; DataTransfer paths are faked) ---------
    def pick_files(self) -> list[str]:
        return self._pick("files")

    def pick_folder(self) -> str:
        picked = self._pick("folder")
        return picked[0] if picked else ""

    def _pick(self, kind: str) -> list[str]:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                if kind == "folder":
                    chosen = filedialog.askdirectory(title="Choose a folder to share")
                    return [chosen] if chosen else []
                chosen = filedialog.askopenfilenames(title="Choose files to share")
                return list(chosen) if chosen else []
            finally:
                root.destroy()
        except Exception as exc:
            logger.warning("File picker failed: %s", exc)
            return []

    # -- downloads ------------------------------------------------------------
    def reveal(self, file_url: str) -> bool:
        """Download a shared file, then open it (desktop Save/Open action)."""
        try:
            import httpx

            from client.config import load_prefs
            from server.filenames import unique_path

            app = self._engine._client_app
            if app is None or not file_url:
                return False
            url = app._resolve_file_url(str(file_url))
            if not url:
                return False
            prefs = load_prefs(app._config.config_dir)
            if prefs.receive_folder:
                dest_dir = Path(prefs.receive_folder).expanduser()
            else:
                dest_dir = Path.home() / "Downloads" / "ClipBoardSync"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = unique_path(dest_dir, "shared_file")
            with httpx.Client(timeout=300.0) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code not in (200, 206):
                        return False
                    with dest.open("wb") as out:
                        for chunk in resp.iter_bytes(1024 * 1024):
                            out.write(chunk)
            os.startfile(str(dest))  # noqa: S606 - local user-picked download
            return True
        except Exception as exc:
            logger.warning("Reveal failed: %s", exc)
            return False

    # -- clipboard sharing ----------------------------------------------------
    def share_clipboard_now(self) -> bool:
        """Share whatever is on the clipboard right now (Ctrl+V in window)."""
        try:
            app = self._engine._client_app
            loop = self._engine._loop
            if app is None or loop is None or not loop.is_running():
                return False
            payload = app._clipboard.get_clipboard_payload()
            if not payload:
                return False
            asyncio.run_coroutine_threadsafe(app._on_local_clipboard_change(payload), loop)
            return True
        except Exception as exc:
            logger.warning("Clipboard share failed: %s", exc)
            return False

    # -- quick paste ----------------------------------------------------------
    def paste_to_last_app(self, text: str) -> bool:
        """Copy text, refocus the pre-overlay window, send Ctrl+V (best effort)."""
        try:
            app = self._engine._client_app
            if app is None:
                return False
            app._clipboard.set_text(text or "")
            hwnd = self._get_last_hwnd()
            _send_ctrl_v(hwnd)
            return True
        except Exception as exc:
            logger.warning("Paste-through failed: %s", exc)
            return False

    def close_window(self) -> None:
        win = self._get_window("quick")
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def quit(self) -> None:
        try:
            self._engine.stop()
        except Exception:
            pass
        win = self._get_window("main")
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass


def _send_ctrl_v(hwnd: int | None) -> None:
    """Focus hwnd (if known) and synthesize Ctrl+V. Never raises."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        if hwnd:
            try:
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time_sleep(0.15)
        VK_CONTROL, VK_V = 0x11, 0x56
        KEYEVENTF_KEYUP = 0x0002
        for vk, up in ((VK_CONTROL, False), (VK_V, False), (VK_V, True), (VK_CONTROL, True)):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)
            time_sleep(0.03)
    except Exception:
        pass


def time_sleep(seconds: float) -> None:
    import time as _time

    _time.sleep(seconds)


def run_window(port: int, engine, admin_token: str, holders: dict[str, Any] | None = None,
               block: bool = True):
    """Create the main desktop window. Returns (window, api).

    ``holders`` is caller-owned shared state (main/quick windows, last_hwnd).
    """
    import webview

    from server import admin

    admin.configure(port)
    if holders is None:
        holders = {}

    def _get_window(which: str):
        return holders.get(which)

    api = ShellApi(engine, _get_window, lambda: holders.get("last_hwnd", 0))
    url = f"http://127.0.0.1:{port}/desktop?t={admin_token}"
    win = webview.create_window(
        "ClipBoardSync", url, width=980, height=680, min_size=(720, 520),
        js_api=api, text_select=True,
    )
    holders["main"] = win
    if block:
        webview.start(gui="edgechromium")
    return win, api
