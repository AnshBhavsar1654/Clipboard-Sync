"""pywebview desktop shell: window + JS bridge."""

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

    # -- native pickers --------------------------------------------------------
    def pick_files(self) -> list[str]:
        return self._pick("files")

    def pick_folder(self) -> str:
        picked = self._pick("folder")
        return picked[0] if picked else ""

    def _pick(self, kind: str) -> list[str]:
        try:
            import webview

            win = self._get_window("main")
            if win is None:
                return []
            if kind == "folder":
                chosen = win.create_file_dialog(webview.FileDialog.FOLDER)
            else:
                chosen = win.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True)
            return [str(p) for p in chosen] if chosen else []
        except Exception as exc:
            logger.warning("File picker failed: %s", exc)
            return []

    # -- downloads ------------------------------------------------------------
    def _receive_dir(self) -> Path:
        from client.config import Config, load_prefs

        prefs = load_prefs(Config().config_dir)
        if prefs.receive_folder:
            dest_dir = Path(prefs.receive_folder).expanduser()
        else:
            dest_dir = Path.home() / "Downloads" / "ClipBoardSync"
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir

    def save_item(self, file_url: str, filename: str) -> dict[str, Any]:
        """Download a shared file into the receive folder under its real name."""
        try:
            import httpx

            from server.filenames import unique_path
            from server.hub import is_allowed_file_url

            if not file_url or not str(file_url).startswith("/") or not is_allowed_file_url(str(file_url)):
                return {"ok": False, "error": "Not a shared file."}
            dest = unique_path(self._receive_dir(), filename or "shared_file")
            with httpx.Client(timeout=300.0) as client:
                with client.stream("GET", f"http://127.0.0.1:{self._engine.port}{file_url}") as resp:
                    if resp.status_code not in (200, 206):
                        return {"ok": False, "error": "The file is no longer available."}
                    with dest.open("wb") as out:
                        for chunk in resp.iter_bytes(1024 * 1024):
                            out.write(chunk)
            return {"ok": True, "path": str(dest)}
        except Exception as exc:
            logger.warning("Save failed: %s", exc)
            return {"ok": False, "error": "Could not save the file."}

    def open_path(self, path: str) -> bool:
        try:
            os.startfile(str(path))  # noqa: S606 - a file the user just saved
            return True
        except Exception as exc:
            logger.warning("Open failed: %s", exc)
            return False

    def show_in_folder(self, path: str) -> bool:
        try:
            import subprocess

            subprocess.Popen(["explorer", f"/select,{path}"])  # noqa: S603,S607
            return True
        except Exception as exc:
            logger.warning("Show in folder failed: %s", exc)
            return False

    def open_url(self, url: str) -> bool:
        """Open a shared link in the default browser (http/https only)."""
        import webbrowser

        if not str(url).lower().startswith(("http://", "https://")):
            return False
        return webbrowser.open(str(url))

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
               block: bool = True, hidden: bool = False):
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
    url = f"http://127.0.0.1:{port}/desktop/?t={admin_token}"
    win = webview.create_window(
        "ClipBoardSync", url, width=1040, height=700, min_size=(760, 520),
        js_api=api, text_select=True, hidden=hidden,
    )
    holders["main"] = win
    if block:
        webview.start(gui="edgechromium")
    return win, api
