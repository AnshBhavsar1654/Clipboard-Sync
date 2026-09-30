"""Windows Desktop GUI implementation for ClipBoardSync using CustomTkinter.

The interface follows the DESIGN.md design system: a neutral dark surface,
one indigo accent, restrained radius, clean list-based clipboard history,
first-class search (Ctrl+K) and no emoji as structural icons.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import io
import json
import logging
import queue
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import Any

import customtkinter as ctk
import win32clipboard
import win32con
from PIL import Image, ImageDraw
import qrcode
import uvicorn

from client.config import Config
from client.main import ClipBoardSyncApp
from server.auth import get_store
from server.main import app as fastapi_app, hub as sync_hub

# ---------------------------------------------------------------------------
# Design tokens (DESIGN.md — dark & light palettes, switched at runtime)
# ---------------------------------------------------------------------------
DARK_THEME = {
    "BG": "#0D0F12",
    "SURFACE": "#15181D",
    "SURFACE_RAISED": "#1C2128",
    "BORDER": "#292E36",
    "HOVER": "#262B34",
    "TEXT": "#F3F4F6",
    "TEXT_SECONDARY": "#9CA3AF",
    "TEXT_FAINT": "#6B7280",
    "PRIMARY": "#818CF8",
    "PRIMARY_STRONG": "#6366F1",
    "HOVER_PRIMARY": "#5159E0",
    "DANGER_HOVER": "#B23C3C",
    "SUCCESS": "#34D399",
    "WARNING": "#FBBF24",
    "DANGER": "#F87171",
    "ON_ACCENT": "#0D0F12",
}

LIGHT_THEME = {
    "BG": "#F7F8FA",
    "SURFACE": "#FFFFFF",
    "SURFACE_RAISED": "#EEF0F4",
    "BORDER": "#E5E7EB",
    "HOVER": "#E6E9EF",
    "TEXT": "#111827",
    "TEXT_SECONDARY": "#6B7280",
    "TEXT_FAINT": "#9CA3AF",
    "PRIMARY": "#6366F1",
    "PRIMARY_STRONG": "#4F46E5",
    "HOVER_PRIMARY": "#4F46E5",
    "DANGER_HOVER": "#B91C1C",
    "SUCCESS": "#059669",
    "WARNING": "#B45309",
    "DANGER": "#DC2626",
    "ON_ACCENT": "#FFFFFF",
}

THEMES: dict[str, dict[str, str]] = {"dark": DARK_THEME, "light": LIGHT_THEME}

# Module-level color tokens (active theme). Reassigned by _set_theme().
globals().update(DARK_THEME)
TYPE_COLORS = {"text": SUCCESS, "image": PRIMARY, "file": WARNING, "folder": PRIMARY}

RADIUS_SM = 6
RADIUS_CARD = 10
FONT = "Segoe UI"
MONO = "Consolas"


def _set_theme(name: str) -> None:
    """Apply a theme palette to all module-level design tokens."""
    tokens = THEMES[name]
    globals().update(tokens)
    globals()["TYPE_COLORS"] = {
        "text": tokens["SUCCESS"],
        "image": tokens["PRIMARY"],
        "file": tokens["WARNING"],
        "folder": tokens["PRIMARY"],
    }
    ctk.set_appearance_mode(name)


_set_theme("dark")
ctk.set_default_color_theme("blue")

logger = logging.getLogger("clipboardsync.gui")


def get_local_lan_ip() -> str:
    """Discover the primary Wi-Fi address used to connect your phone."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except Exception:
        try:
            hostname = socket.gethostname()
            return str(socket.gethostbyname(hostname))
        except Exception:
            return "127.0.0.1"


def get_icon_path() -> Path:
    """Resolve the application icon file (frozen bundle vs. source tree)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidate = Path(sys._MEIPASS) / "assets" / "clipboardsync.ico"
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent.parent / "assets" / "clipboardsync.ico"


# ---------------------------------------------------------------------------
# Line icon rendering (vector-style, no emoji) via 4x supersampled PIL draws
# ---------------------------------------------------------------------------

def _d_clipboard(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.rounded_rectangle((3 * s, 4 * s, 13 * s, 14 * s), radius=2 * s, outline=c, width=s)
    d.rectangle((5 * s, 2 * s, 11 * s, 4 * s), fill=c)
    d.line((6 * s, 8 * s, 10 * s, 8 * s), fill=c, width=s)
    d.line((6 * s, 11 * s, 10 * s, 11 * s), fill=c, width=s)


def _d_pin(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.line((8 * s, 3 * s, 8 * s, 13 * s), fill=c, width=s)
    d.line((5 * s, 6 * s, 11 * s, 6 * s), fill=c, width=s)
    d.ellipse((6 * s, 1 * s, 10 * s, 5 * s), outline=c, width=s)


def _d_folder(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.line((3 * s, 5 * s, 6 * s, 3 * s, 9 * s, 3 * s, 11 * s, 5 * s), fill=c, width=s)
    d.rounded_rectangle((3 * s, 5 * s, 13 * s, 12 * s), radius=1 * s, outline=c, width=s)
    d.line((3 * s, 8 * s, 13 * s, 8 * s), fill=c, width=s)


def _d_devices(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.rectangle((2 * s, 4 * s, 9 * s, 10 * s), outline=c, width=s)
    d.line((5 * s, 10 * s, 5 * s, 13 * s), fill=c, width=s)
    d.line((3 * s, 13 * s, 8 * s, 13 * s), fill=c, width=s)
    d.rectangle((11 * s, 6 * s, 15 * s, 14 * s), outline=c, width=s)
    d.line((12 * s, 12 * s, 14 * s, 12 * s), fill=c, width=s)


def _d_settings(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.line((3 * s, 4 * s, 13 * s, 4 * s), fill=c, width=s)
    d.rectangle((7 * s, 3 * s, 9 * s, 5 * s), fill=c)
    d.line((3 * s, 8 * s, 13 * s, 8 * s), fill=c, width=s)
    d.rectangle((5 * s, 7 * s, 7 * s, 9 * s), fill=c)
    d.line((3 * s, 12 * s, 13 * s, 12 * s), fill=c, width=s)
    d.rectangle((10 * s, 11 * s, 12 * s, 13 * s), fill=c)


def _d_search(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.ellipse((3 * s, 3 * s, 10 * s, 10 * s), outline=c, width=s)
    d.line((10 * s, 10 * s, 14 * s, 14 * s), fill=c, width=s)


def _d_power(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.ellipse((4 * s, 4 * s, 12 * s, 12 * s), outline=c, width=s)
    d.line((8 * s, 2 * s, 8 * s, 7 * s), fill=c, width=s)


def _d_copy(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.rectangle((5 * s, 3 * s, 12 * s, 10 * s), outline=c, width=s)
    d.rectangle((3 * s, 6 * s, 10 * s, 13 * s), outline=c, width=s)


def _d_download(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.line((8 * s, 3 * s, 8 * s, 11 * s), fill=c, width=s)
    d.line((5 * s, 8 * s, 8 * s, 11 * s, 11 * s, 8 * s), fill=c, width=s)
    d.line((3 * s, 13 * s, 13 * s, 13 * s), fill=c, width=s)


def _d_doc(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.rectangle((4 * s, 2 * s, 12 * s, 14 * s), outline=c, width=s)
    d.line((6 * s, 5 * s, 10 * s, 5 * s), fill=c, width=s)
    d.line((6 * s, 8 * s, 10 * s, 8 * s), fill=c, width=s)
    d.line((6 * s, 11 * s, 10 * s, 11 * s), fill=c, width=s)


def _d_image(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.rounded_rectangle((2 * s, 3 * s, 14 * s, 13 * s), radius=1 * s, outline=c, width=s)
    d.ellipse((5 * s, 5 * s, 7 * s, 7 * s), outline=c, width=s)
    d.line((2 * s, 12 * s, 7 * s, 8 * s, 10 * s, 11 * s, 12 * s, 9 * s, 14 * s, 11 * s), fill=c, width=s)


def _d_sun(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.ellipse((6 * s, 6 * s, 10 * s, 10 * s), outline=c, width=s)
    for x1, y1, x2, y2 in (
        (8, 2, 8, 4), (8, 12, 8, 14), (2, 8, 4, 8), (12, 8, 14, 8),
        (4, 4, 5, 5), (11, 11, 12, 12), (4, 12, 5, 11), (11, 5, 12, 4),
    ):
        d.line((x1 * s, y1 * s, x2 * s, y2 * s), fill=c, width=s)


def _d_moon(d: ImageDraw.ImageDraw, c: str, s: int) -> None:
    d.pieslice((2 * s, 2 * s, 14 * s, 14 * s), start=35, end=325, fill=c, outline=c, width=s)


_ICON_DRAWS = {
    "clipboard": _d_clipboard,
    "pin": _d_pin,
    "folder": _d_folder,
    "devices": _d_devices,
    "settings": _d_settings,
    "search": _d_search,
    "power": _d_power,
    "copy": _d_copy,
    "download": _d_download,
    "doc": _d_doc,
    "image": _d_image,
    "sun": _d_sun,
    "moon": _d_moon,
}


def _make_icon(name: str, color: str = TEXT_SECONDARY, size: int = 16) -> ctk.CTkImage:
    """Render a 16px line icon from the vector registry with supersampling."""
    draw_fn = _ICON_DRAWS[name]
    s = 4
    img = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(img), color, s)
    img = img.resize((size, size), Image.LANCZOS)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


# ---------------------------------------------------------------------------
# Logging plumbing
# ---------------------------------------------------------------------------

class QueueLogHandler(logging.Handler):
    """Routes Python standard log stream directly to a GUI queue for displaying in real time."""

    def __init__(self, log_queue: queue.Queue[str]) -> None:
        super().__init__(level=logging.INFO)
        self.log_queue = log_queue
        self.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            pass


class StdoutRedirector:
    """Intercepts print statements across the app to show them in the activity log."""

    def __init__(self, log_queue: queue.Queue[str], original_stdout: Any) -> None:
        self.log_queue = log_queue
        self.original_stdout = original_stdout

    def write(self, text: str) -> None:
        if text.strip():
            for line in text.rstrip().split('\n'):
                if line.strip():
                    self.log_queue.put(f"[Info] {line}")
        if self.original_stdout:
            self.original_stdout.write(text)
            self.original_stdout.flush()

    def flush(self) -> None:
        if self.original_stdout:
            self.original_stdout.flush()


class Tooltip:
    """Hover tooltip for icon-only CTk buttons.

    Shows a small floating label while the mouse hovers the widget and hides
    automatically once the pointer leaves the widget's bounds. Hiding is driven
    by polling the pointer position rather than <Leave> events, because
    CustomTkinter buttons render on an internal canvas whose event targeting
    makes <Leave> unreliable.
    """

    def __init__(self, widget: ctk.CTkBaseClass, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip_window: ctk.CTkToplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<ButtonRelease>", self._hide, add="+")
        canvas = getattr(widget, "_canvas", None)
        if canvas is not None:
            canvas.bind("<Enter>", self._show, add="+")

    def _show(self, _event: Any = None) -> None:
        if self._tip_window is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            tip = ctk.CTkToplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.wm_geometry(f"+{x}+{y}")
            label = ctk.CTkLabel(
                tip,
                text=self.text,
                font=ctk.CTkFont(family=FONT, size=11),
                fg_color=SURFACE_RAISED,
                text_color=TEXT,
                corner_radius=RADIUS_SM,
                border_width=1,
                border_color=BORDER,
                padx=8,
                pady=4,
            )
            label.pack()
            self._tip_window = tip
            self._watch()
        except Exception:
            self._tip_window = None

    def _watch(self) -> None:
        if self._tip_window is None:
            return
        inside = False
        try:
            widget = self.widget
            if widget.winfo_ismapped():
                px = widget.winfo_pointerx()
                py = widget.winfo_pointery()
                rx = widget.winfo_rootx()
                ry = widget.winfo_rooty()
                ww = widget.winfo_width()
                wh = widget.winfo_height()
                inside = rx <= px <= rx + ww and ry <= py <= ry + wh
        except Exception:
            inside = False
        if not inside:
            self._hide()
            return
        self.widget.after(80, self._watch)

    def _hide(self, _event: Any = None) -> None:
        if self._tip_window is not None:
            try:
                self._tip_window.destroy()
            except Exception:
                pass
            self._tip_window = None


# ---------------------------------------------------------------------------
# Backend engine
# ---------------------------------------------------------------------------

class BackgroundEngine:
    """Runs the sharing server and clipboard watcher together in the background."""

    def __init__(self, port: int = 8000) -> None:
        self.port = port
        self.is_running = False
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._client_app: ClipBoardSyncApp | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._peer = None
        self.peer_status: str = "disconnected"  # disconnected|connecting|connected|code_rejected|retrying
        self.peer_host: str = ""
        self._log_queue: queue.Queue[str] | None = None

    def start(self, log_queue: queue.Queue[str]) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(log_queue,),
            name="clipboardsync-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        if self._server:
            self._server.should_exit = True
        if self._client_app and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._client_app.request_shutdown)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self, log_queue: queue.Queue[str]) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_runner(log_queue))
        except Exception as exc:
            log_queue.put(f"[Error] Sharing stopped unexpectedly: {exc}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self.is_running = False

    async def _async_runner(self, log_queue: queue.Queue[str]) -> None:
        self._log_queue = log_queue
        log_queue.put(f"[Info] Starting sharing on port {self.port}...")
        uv_config = uvicorn.Config(
            app=fastapi_app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        self._server = uvicorn.Server(uv_config)
        server_task = asyncio.create_task(self._server.serve(), name="uvicorn-backend")

        await asyncio.sleep(0.4)
        log_queue.put("[Info] Watching your clipboard for changes...")
        client_config = Config()
        client_config.websocket_url = f"ws://127.0.0.1:{self.port}/ws"
        self._client_app = ClipBoardSyncApp(client_config)

        client_task = asyncio.create_task(self._client_app.run(), name="win32-client")
        log_queue.put("[Info] Sharing is on. Copy on either device to share it.")

        done, pending = await asyncio.wait(
            [server_task, client_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._peer is not None:
            try:
                await self._peer.stop()
            except Exception:
                pass
            self._peer = None
        log_queue.put("[Info] Sharing is off.")

    # -- PC-to-PC peer ------------------------------------------------------
    def connect_peer(self, host_ip: str, port: int, pin: str, device_id: str) -> bool:
        """Join another PC's server. Returns False if sharing isn't running."""
        if not self.is_running or not self._loop or not self._loop.is_running():
            return False
        if self._peer is not None:
            return False
        from client.peer_client import PeerConnection

        def _status(s: str) -> None:
            self.peer_status = s
            if self._log_queue is not None:
                if s == "connected":
                    self._log_queue.put(f"[Info] Connected to other computer at {host_ip}.")
                elif s.startswith("code_rejected"):
                    self._log_queue.put("[Error] That computer code did not match. Check it and try again.")
                elif s == "retrying":
                    self._log_queue.put("[Info] Reconnecting to the other computer…")

        async def _on_peer_message(msg: dict) -> None:
            # Inject peer clips into the local hub so they show in this feed.
            try:
                await sync_hub.handle_message(None, msg)
            except Exception:
                pass
            # Download shared files/folders in the background.
            if msg.get("type") in ("file", "folder") and msg.get("file_url"):
                await self._download_peer_file(host_ip, port, msg)

        async def _start() -> None:
            self._peer = PeerConnection(host_ip, port, device_id, pin, _on_peer_message, _status)
            self.peer_host = f"{host_ip}:{port}"
            self.peer_status = "connecting"
            self._peer.start()

        asyncio.run_coroutine_threadsafe(_start(), self._loop)
        return True

    def disconnect_peer(self) -> None:
        if self._loop and self._loop.is_running() and self._peer is not None:
            peer, self._peer = self._peer, None
            asyncio.run_coroutine_threadsafe(peer.stop(), self._loop)
        else:
            self._peer = None
        self.peer_status = "disconnected"
        self.peer_host = ""
        if self._log_queue is not None:
            self._log_queue.put("[Info] Disconnected from the other computer.")

    def send_to_peer(self, message: dict) -> None:
        """Forward our clips to the connected PC (fire-and-forget)."""
        if self._loop and self._loop.is_running() and self._peer is not None:
            asyncio.run_coroutine_threadsafe(self._peer.send(message), self._loop)

    async def _download_peer_file(self, host_ip: str, port: int, msg: dict) -> None:
        try:
            import httpx
            file_url = str(msg.get("file_url") or "")
            if not file_url.startswith("http"):
                file_url = f"http://{host_ip}:{port}{file_url if file_url.startswith('/') else '/' + file_url}"
            filename = str(msg.get("filename") or "shared_file")
            dest_dir = Path.home() / "Downloads" / "ClipBoardSync"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / filename
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                i = 1
                while (dest_dir / f"{stem} ({i}){suffix}").exists():
                    i += 1
                dest = dest_dir / f"{stem} ({i}){suffix}"
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("GET", file_url) as resp:
                    if resp.status_code not in (200, 206):
                        return
                    with dest.open("wb") as out:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            out.write(chunk)
            if self._log_queue is not None:
                kind = "Folder (.zip)" if msg.get("type") == "folder" else "File"
                self._log_queue.put(f"[Info] {kind} from other computer saved: {dest}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class ClipBoardSyncGUI(ctk.CTk):
    """Main Application graphical dashboard interface."""

    def __init__(self) -> None:
        super().__init__()
        self.title("ClipBoardSync — Share clipboard over Wi-Fi")
        self.geometry("1020x680")
        self.minsize(880, 560)

        # Window / taskbar icon (fall back gracefully when unavailable)
        icon_file = get_icon_path()
        if sys.platform == "win32" and icon_file.exists():
            try:
                self.iconbitmap(str(icon_file))
            except Exception:
                pass

        self.protocol("WM_DELETE_WINDOW", self.on_close_request)

        # Internal state & queues
        self.port = 8000
        self.local_ip = get_local_lan_ip()
        self.mobile_url = f"http://{self.local_ip}:{self.port}"
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.engine = BackgroundEngine(port=self.port)
        self.auto_scroll_logs = ctk.BooleanVar(value=True)
        self._active_tab = "clipboard"
        self.search_var = ctk.StringVar(value="")
        self._pinned: dict[str, dict[str, Any]] = self._load_pins()
        self._last_sig: tuple[tuple[str, ...], ...] = ()
        self.trust_store = get_store()
        self._last_pairing_sig: tuple[Any, ...] | None = None
        self._log_lines: list[str] = ["ClipBoardSync is ready. Sharing starts automatically."]
        # PC-to-PC: auto-discovery + manual connect state
        from server.discovery import PeerDiscovery
        self.discovery = PeerDiscovery(port=self.port)
        self.peer_ip_var = None  # created in _init_ui (needs ctk)
        self.peer_code_var = None
        self._last_peer_sig: tuple[Any, ...] | None = None
        self._peer_device_id = self._load_peer_id()

        self.theme_name = self._load_theme_pref()
        _set_theme(self.theme_name)

        # Connect log redirectors
        self.log_handler = QueueLogHandler(self.log_queue)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger("clipboardsync").setLevel(logging.INFO)
        self.original_stdout = sys.stdout
        sys.stdout = StdoutRedirector(self.log_queue, self.original_stdout)

        self._init_ui()
        self.bind("<Control-k>", self._focus_search)
        self.bind("<Control-l>", self._focus_search)
        self._start_automation()

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------
    def _init_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar ---
        self.sidebar = ctk.CTkFrame(self, width=208, corner_radius=0, fg_color=SURFACE)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(4, weight=1)

        brand = ctk.CTkLabel(
            self.sidebar,
            text="ClipBoardSync",
            font=ctk.CTkFont(family=FONT, size=18, weight="bold"),
            text_color=TEXT,
            anchor="w",
        )
        brand.grid(row=0, column=0, padx=20, pady=(24, 2), sticky="w")

        sub = ctk.CTkLabel(
            self.sidebar,
            text="Share clipboard over Wi-Fi",
            font=ctk.CTkFont(family=FONT, size=12),
            text_color=TEXT_FAINT,
            anchor="w",
        )
        sub.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="w")

        self.status_badge = ctk.CTkLabel(
            self.sidebar,
            text="● NOT SHARING",
            font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
            text_color=DANGER,
            fg_color=SURFACE_RAISED,
            corner_radius=RADIUS_CARD,
            height=30,
            anchor="w",
        )
        self.status_badge.grid(row=2, column=0, padx=16, pady=(0, 18), sticky="ew")

        nav = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        nav.grid(row=3, column=0, sticky="ew", padx=10)
        nav.grid_columnconfigure(0, weight=1)

        self.btn_clipboard = self._make_nav(nav, "Clipboard", "clipboard", 0, self._show_clipboard)
        self.btn_pinned = self._make_nav(nav, "Pinned", "pin", 1, self._show_pinned)
        self.btn_files = self._make_nav(nav, "Files", "folder", 2, self._show_files)
        self.btn_devices = self._make_nav(nav, "Devices", "devices", 3, self._show_devices)
        self.btn_settings = self._make_nav(nav, "Settings", "settings", 4, self._show_settings)

        self.conn_label = ctk.CTkLabel(
            self.sidebar,
            text="No phones connected",
            font=ctk.CTkFont(family=FONT, size=12),
            text_color=TEXT_FAINT,
            anchor="w",
        )
        self.conn_label.grid(row=5, column=0, padx=20, pady=(0, 8), sticky="w")

        self.toggle_engine_btn = ctk.CTkButton(
            self.sidebar,
            text="START SHARING",
            image=_make_icon("power", ON_ACCENT, 16),
            font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
            height=42,
            corner_radius=RADIUS_SM,
            fg_color=PRIMARY,
            hover_color=PRIMARY_STRONG,
            text_color=ON_ACCENT,
            command=self.toggle_engine,
        )
        self.toggle_engine_btn.grid(row=6, column=0, padx=16, pady=(0, 24), sticky="ew")

        self.theme_btn = ctk.CTkButton(
            self.sidebar,
            text="Light theme" if self.theme_name == "dark" else "Dark theme",
            image=_make_icon("sun" if self.theme_name == "dark" else "moon", TEXT_SECONDARY, 16),
            anchor="w",
            font=ctk.CTkFont(family=FONT, size=12),
            height=32,
            corner_radius=RADIUS_SM,
            fg_color="transparent",
            hover_color=HOVER,
            text_color=TEXT_SECONDARY,
            command=self._toggle_theme,
        )
        self.theme_btn.grid(row=7, column=0, padx=16, pady=(0, 18), sticky="ew")

        # --- Main container ---
        self.main_container = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.main_container.grid(row=0, column=1, sticky="nsew")
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(0, weight=1)

        self.tab_clipboard, self._clip_scroll = self._build_list_tab("Clipboard", with_search=True)
        self.tab_pinned, self._pinned_scroll = self._build_list_tab("Pinned", with_search=False)
        self.tab_files, self._files_scroll = self._build_list_tab("Files", with_search=False)
        self.tab_devices = self._build_tab_devices()
        self.tab_settings = self._build_tab_settings()

        self._show_clipboard()

    def _make_nav(self, parent: ctk.CTkFrame, text: str, icon: str, row: int, cmd: Any) -> ctk.CTkButton:
        btn = ctk.CTkButton(
            parent,
            text=text,
            image=_make_icon(icon),
            anchor="w",
            font=ctk.CTkFont(family=FONT, size=14),
            height=38,
            corner_radius=RADIUS_SM,
            fg_color="transparent",
            hover_color=HOVER,
            text_color=TEXT_SECONDARY,
            command=cmd,
        )
        btn.grid(row=row, column=0, pady=2, sticky="ew")
        return btn

    def _select_nav(self, selected: ctk.CTkButton, name: str, frame: ctk.CTkFrame) -> None:
        for btn in (self.btn_clipboard, self.btn_pinned, self.btn_files, self.btn_devices, self.btn_settings):
            btn.configure(
                fg_color=SURFACE_RAISED if btn is selected else "transparent",
                text_color=TEXT if btn is selected else TEXT_SECONDARY,
            )
        for tab in (self.tab_clipboard, self.tab_pinned, self.tab_files, self.tab_devices, self.tab_settings):
            tab.grid_remove()
        self._active_tab = name
        frame.grid(row=0, column=0, sticky="nsew", padx=24, pady=24)

    def _show_clipboard(self) -> None:
        self._select_nav(self.btn_clipboard, "clipboard", self.tab_clipboard)
        self._refresh_active_list()

    def _show_pinned(self) -> None:
        self._select_nav(self.btn_pinned, "pinned", self.tab_pinned)
        self._refresh_active_list()

    def _show_files(self) -> None:
        self._select_nav(self.btn_files, "files", self.tab_files)
        self._refresh_active_list()

    def _show_devices(self) -> None:
        self._select_nav(self.btn_devices, "devices", self.tab_devices)
        self._refresh_pairing_view()

    def _show_settings(self) -> None:
        self._select_nav(self.btn_settings, "settings", self.tab_settings)

    def _focus_search(self, _event: Any = None) -> None:
        if self._active_tab == "clipboard" and hasattr(self, "search_entry"):
            self.search_entry.focus_set()
            try:
                self.search_entry.select_range(0, "end")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Theme switching (rebuilds the UI so every widget uses fresh tokens)
    # ------------------------------------------------------------------
    def _load_theme_pref(self) -> str:
        try:
            p = Path.home() / ".clipboardsync" / "gui_prefs.json"
            if p.exists():
                saved = json.loads(p.read_text(encoding="utf-8")).get("theme", "dark")
                if saved in THEMES:
                    return saved
        except Exception:
            pass
        return "dark"

    def _save_theme_pref(self, name: str) -> None:
        try:
            p = Path.home() / ".clipboardsync" / "gui_prefs.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"theme": name}, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _toggle_theme(self) -> None:
        active_tab = self._active_tab
        new_name = "light" if self.theme_name == "dark" else "dark"
        self.theme_name = new_name
        self._save_theme_pref(new_name)
        _set_theme(new_name)

        # Capture the current log so it survives the widget rebuild
        try:
            if hasattr(self, "log_textbox"):
                raw = self.log_textbox.get("0.0", "end").rstrip("\n")
                self._log_lines = [ln for ln in raw.split("\n") if ln]
        except Exception:
            pass

        self.sidebar.destroy()
        self.main_container.destroy()
        self._init_ui()

        show_fn = {
            "clipboard": self._show_clipboard,
            "pinned": self._show_pinned,
            "files": self._show_files,
            "devices": self._show_devices,
            "settings": self._show_settings,
        }.get(active_tab, self._show_clipboard)
        show_fn()
        self._replay_log_lines()
        self.log_queue.put(f"[Info] Changed to {'light' if new_name == 'light' else 'dark'} theme.")

    def _replay_log_lines(self) -> None:
        try:
            self.log_textbox.configure(state="normal")
            self.log_textbox.delete("0.0", "end")
            for line in self._log_lines:
                self.log_textbox.insert("end", line + "\n")
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # List tabs (Clipboard / Pinned / Files)
    # ------------------------------------------------------------------
    def _build_list_tab(self, title: str, with_search: bool) -> tuple[ctk.CTkFrame, ctk.CTkScrollableFrame]:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(1, weight=1)

        title_lbl = ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(family=FONT, size=20, weight="bold"),
            text_color=TEXT,
            anchor="w",
        )
        title_lbl.grid(row=0, column=0, sticky="w")

        if with_search:
            self.search_entry = ctk.CTkEntry(
                header,
                placeholder_text="Search shared clips…  (Ctrl+K)",
                height=34,
                width=280,
                corner_radius=RADIUS_SM,
                fg_color=SURFACE_RAISED,
                border_color=BORDER,
                text_color=TEXT,
                placeholder_text_color=TEXT_FAINT,
                textvariable=self.search_var,
            )
            self.search_entry.grid(row=0, column=1, sticky="e", padx=(16, 0))
            self.search_entry.bind("<KeyRelease>", lambda _e: self._refresh_active_list())
            self.search_entry.bind("<Escape>", lambda _e: self.search_entry.delete(0, "end"))

        scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        return frame, scroll

    def _collect_items(self, pinned_only: bool = False, files_only: bool = False) -> list[dict[str, Any]]:
        pinned = [self._pinned[k] for k in self._pinned]
        if pinned_only:
            return pinned
        pinned_ids = set(self._pinned.keys())
        hist = [i for i in reversed(sync_hub.get_history()) if i.get("id") not in pinned_ids]
        items = pinned + hist
        if files_only:
            items = [i for i in items if i.get("type") in ("image", "file", "folder")]
        return items

    def _apply_search(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        q = self.search_var.get().strip().lower()
        if not q:
            return items
        out = []
        for i in items:
            hay = " ".join([
                str(i.get("content", "")),
                str(i.get("filename", "")),
                str(i.get("device_id", "")),
                str(i.get("type", "")),
            ]).lower()
            if q in hay:
                out.append(i)
        return out

    def _refresh_active_list(self) -> None:
        if self._active_tab == "clipboard":
            items = self._apply_search(self._collect_items())
            self._render_item_list(self._clip_scroll, items, "No clips yet",
                                   "Copy something on this computer or your phone, or send a photo or file from Devices.",
                                   action_text="Connect your phone" if not items else "",
                                   action_command=self._show_devices)
        elif self._active_tab == "pinned":
            items = self._apply_search(self._collect_items(pinned_only=True))
            self._render_item_list(self._pinned_scroll, items, "Nothing pinned yet",
                                   "Use the pin button on any clip to keep it here.")
        elif self._active_tab == "files":
            items = self._apply_search(self._collect_items(files_only=True))
            self._render_item_list(self._files_scroll, items, "No photos or files yet",
                                   "Photos and files you share appear here.")

    def _render_item_list(self, scroll: ctk.CTkScrollableFrame, items: list[dict[str, Any]],
                          empty_title: str, empty_body: str,
                          action_text: str = "", action_command: Any = None) -> None:
        for child in scroll.winfo_children():
            child.destroy()

        if not items:
            e1 = ctk.CTkLabel(scroll, text=empty_title, font=ctk.CTkFont(family=FONT, size=15, weight="bold"),
                              text_color=TEXT, anchor="w")
            e1.grid(row=0, column=0, pady=(48, 4), sticky="w", padx=(4, 0))
            e2 = ctk.CTkLabel(scroll, text=empty_body, font=ctk.CTkFont(family=FONT, size=13),
                              text_color=TEXT_SECONDARY, justify="left", anchor="w", wraplength=440)
            e2.grid(row=1, column=0, sticky="w", padx=(4, 0))
            if action_text and action_command:
                act = ctk.CTkButton(
                    scroll, text=action_text,
                    font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                    height=34, corner_radius=RADIUS_SM,
                    fg_color=PRIMARY, hover_color=HOVER_PRIMARY, text_color=ON_ACCENT,
                    command=action_command)
                act.grid(row=2, column=0, sticky="w", pady=(14, 0), padx=(4, 0))
            return

        for idx, item in enumerate(items):
            self._render_item_row(scroll, item, idx)

    def _render_item_row(self, scroll: ctk.CTkScrollableFrame, item: dict[str, Any], row: int) -> None:
        card = ctk.CTkFrame(scroll, fg_color=SURFACE_RAISED, corner_radius=RADIUS_CARD)
        card.grid(row=row, column=0, sticky="ew", pady=6)
        card.grid_columnconfigure(0, weight=1)

        item_id = str(item.get("id", ""))
        itype = item.get("type", "text")
        is_pinned = item_id in self._pinned
        content = str(item.get("content", ""))

        # Left: content column
        content_col = ctk.CTkFrame(card, fg_color="transparent")
        content_col.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=12)
        content_col.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(content_col, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)

        type_lbl = ctk.CTkLabel(
            top, text=itype.upper(),
            font=ctk.CTkFont(family=MONO, size=10, weight="bold"),
            text_color=TYPE_COLORS.get(itype, TEXT_SECONDARY), anchor="w")
        type_lbl.grid(row=0, column=0, sticky="w")

        if is_pinned:
            pin_marker = ctk.CTkLabel(top, text="PINNED",
                                      font=ctk.CTkFont(family=MONO, size=10, weight="bold"),
                                      text_color=PRIMARY, anchor="e")
            pin_marker.grid(row=0, column=1, sticky="e")

        # Body preview
        if itype == "image":
            self._render_image_body(content_col, content, item)
        elif itype in ("file", "folder"):
            fname = item.get("filename") or ("Folder" if itype == "folder" else "File")
            fsize = item.get("filesize") or 0
            if fsize < 1024:
                fsize_str = f"{fsize} B"
            elif fsize < 1024 * 1024:
                fsize_str = f"{fsize / 1024:.1f} KB"
            elif fsize < 1024 * 1024 * 1024:
                fsize_str = f"{fsize / (1024 * 1024):.1f} MB"
            else:
                fsize_str = f"{fsize / (1024 * 1024 * 1024):.2f} GB"
            detail = fname
            if itype == "folder" and item.get("entry_count"):
                detail = f"{fname}  ·  {item.get('entry_count')} items"
            if item.get("skipped_count"):
                detail = f"{detail}  ·  skipped {item.get('skipped_count')}"
            lbl = ctk.CTkLabel(content_col, text=f"{detail}  ·  {fsize_str}",
                               font=ctk.CTkFont(family=FONT, size=14), text_color=TEXT,
                               justify="left", anchor="w", wraplength=520)
            lbl.grid(row=1, column=0, pady=(8, 6), sticky="w")
        else:
            preview = _preview_text(content, 280)
            lbl = ctk.CTkLabel(content_col, text=preview,
                               font=ctk.CTkFont(family=FONT, size=14), text_color=TEXT,
                               justify="left", anchor="w", wraplength=520)
            lbl.grid(row=1, column=0, pady=(8, 6), sticky="w")

        meta = ctk.CTkLabel(
            content_col,
            text=f"{_format_device(item.get('device_id'))}  ·  {_format_time(item.get('timestamp'))}",
            font=ctk.CTkFont(family=FONT, size=12), text_color=TEXT_FAINT, anchor="w")
        meta.grid(row=2, column=0, sticky="w")

        # Right: actions
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.grid(row=0, column=1, sticky="ne", padx=(4, 12), pady=12)

        pin_btn = ctk.CTkButton(
            actions, width=30, height=30, corner_radius=RADIUS_SM, text="",
            image=_make_icon("pin", PRIMARY if is_pinned else TEXT_FAINT, 16),
            fg_color="transparent", hover_color=HOVER,
            command=lambda iid=item_id, it=item: self._toggle_pin(iid, it))
        pin_btn.grid(row=0, column=0, pady=(0, 6))
        Tooltip(pin_btn, "Remove pin" if is_pinned else "Keep at top")

        if itype in ("file", "folder"):
            file_url = str(item.get("file_url") or "")
            if file_url and not file_url.startswith("http"):
                file_url = f"{self.mobile_url}{file_url}" if file_url.startswith("/") else f"{self.mobile_url}/{file_url}"
            fname = str(item.get("filename") or ("folder.zip" if itype == "folder" else "file"))
            act_btn = ctk.CTkButton(
                actions, width=30, height=30, corner_radius=RADIUS_SM, text="",
                image=_make_icon("download", TEXT_SECONDARY, 16),
                fg_color="transparent", hover_color=HOVER,
                command=lambda u=file_url, fn=fname: self.save_shared_item(u, fn))
            act_btn.grid(row=1, column=0)
            Tooltip(act_btn, "Save folder (.zip)" if itype == "folder" else "Save file")
        elif itype == "image":
            if content.startswith("data:image/"):
                act_btn = ctk.CTkButton(
                    actions, width=30, height=30, corner_radius=RADIUS_SM, text="",
                    image=_make_icon("copy", TEXT_SECONDARY, 16),
                    fg_color="transparent", hover_color=HOVER,
                    command=lambda c=content: self.copy_image_to_local(c))
                act_btn.grid(row=1, column=0)
                Tooltip(act_btn, "Copy photo — ready to paste")
            else:
                img_url = str(item.get("file_url") or item.get("content") or "")
                if img_url and not img_url.startswith("http"):
                    img_url = f"{self.mobile_url}{img_url}" if img_url.startswith("/") else f"{self.mobile_url}/{img_url}"
                img_name = str(item.get("filename") or "photo.png")
                act_btn = ctk.CTkButton(
                    actions, width=30, height=30, corner_radius=RADIUS_SM, text="",
                    image=_make_icon("download", TEXT_SECONDARY, 16),
                    fg_color="transparent", hover_color=HOVER,
                    command=lambda u=img_url, fn=img_name: self.save_shared_item(u, fn))
                act_btn.grid(row=1, column=0)
                Tooltip(act_btn, "Save photo")
        else:
            act_btn = ctk.CTkButton(
                actions, width=30, height=30, corner_radius=RADIUS_SM, text="",
                image=_make_icon("copy", TEXT_SECONDARY, 16),
                fg_color="transparent", hover_color=HOVER,
                command=lambda c=content: self.copy_clip_to_local(c))
            act_btn.grid(row=1, column=0)
            Tooltip(act_btn, "Copy — ready to paste")

    def _render_image_body(self, content_col: ctk.CTkFrame, content: str, item: dict[str, Any]) -> None:
        """Render an image thumbnail when base64 data is embedded, else a text descriptor."""
        try:
            if content.startswith("data:image/"):
                b64_part = content.split(",", 1)[1]
                pil_img = Image.open(io.BytesIO(base64.b64decode(b64_part)))
                pil_img.thumbnail((150, 100))
                thumb = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=pil_img.size)
                img_lbl = ctk.CTkLabel(content_col, image=thumb, text="")
                img_lbl.grid(row=1, column=0, pady=(8, 6), sticky="w")
                img_lbl.image = thumb
                return
        except Exception:
            pass
        fname = item.get("filename") or "Photo"
        lbl = ctk.CTkLabel(content_col, text=f"Photo  ·  {fname}",
                           font=ctk.CTkFont(family=FONT, size=14), text_color=TEXT,
                           justify="left", anchor="w", wraplength=520)
        lbl.grid(row=1, column=0, pady=(8, 6), sticky="w")

    def _toggle_pin(self, item_id: str, item: dict[str, Any]) -> None:
        if item_id in self._pinned:
            del self._pinned[item_id]
        else:
            self._pinned[item_id] = item
        self._save_pins()
        self._refresh_active_list()

    @staticmethod
    def _load_pins() -> dict[str, dict[str, Any]]:
        try:
            p = Path.home() / ".clipboardsync" / "pins.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return {str(k): v for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_pins(self) -> None:
        try:
            p = Path.home() / ".clipboardsync" / "pins.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._pinned, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_peer_id(self) -> str:
        """Stable PC identity used when joining another computer (PC-XXXX)."""
        try:
            p = Path.home() / ".clipboardsync" / "peer_id.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                pid = str(data.get("peer_id", "")).strip()
                if pid:
                    return pid
        except Exception:
            pass
        import random
        import string
        pid = "PC-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        try:
            p = Path.home() / ".clipboardsync" / "peer_id.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"peer_id": pid}, indent=2), encoding="utf-8")
        except Exception:
            pass
        return pid

    # ------------------------------------------------------------------
    # Devices tab (QR pairing)
    # ------------------------------------------------------------------
    def _build_tab_devices(self) -> ctk.CTkFrame:
        frame = ctk.CTkScrollableFrame(self.main_container, fg_color="transparent")
        frame.grid_columnconfigure(1, weight=1)

        # QR card
        qr_card = ctk.CTkFrame(frame, fg_color=SURFACE, corner_radius=RADIUS_CARD)
        qr_card.grid(row=0, column=0, sticky="ns", padx=(0, 20))
        title_qr = ctk.CTkLabel(qr_card, text="Connect your phone",
                                font=ctk.CTkFont(family=FONT, size=17, weight="bold"), text_color=TEXT)
        title_qr.pack(pady=(22, 14), padx=24)

        self.qr_label = ctk.CTkLabel(qr_card, text="", width=224, height=224)
        self.qr_label.pack(padx=24)
        self._generate_qr_image(self.mobile_url)

        qr_note = ctk.CTkLabel(qr_card, text="Point your phone camera at the code\nto open ClipBoardSync",
                               font=ctk.CTkFont(family=FONT, size=13), text_color=TEXT_FAINT)
        qr_note.pack(pady=(12, 24), padx=24)

        # Details card
        details = ctk.CTkFrame(frame, fg_color=SURFACE, corner_radius=RADIUS_CARD)
        details.grid(row=0, column=1, sticky="nsew")
        details.grid_columnconfigure(0, weight=1)

        title_info = ctk.CTkLabel(details, text="Share between computer and phone",
                                  font=ctk.CTkFont(family=FONT, size=19, weight="bold"),
                                  text_color=TEXT, anchor="w")
        title_info.grid(row=0, column=0, padx=26, pady=(26, 10), sticky="w")

        steps = (
            "1. Same Wi-Fi\n"
            "   Keep this computer and your phone on the same Wi-Fi.\n\n"
            "2. Scan the code\n"
            "   Point your phone camera at the code on the left and open the link.\n\n"
            "3. Share instantly\n"
            "   Copy on either device. It appears on the other. Private — no accounts, no cloud."
        )
        instr = ctk.CTkLabel(details, text=steps, font=ctk.CTkFont(family=FONT, size=13),
                             text_color=TEXT_SECONDARY, justify="left", anchor="w")
        instr.grid(row=1, column=0, padx=26, pady=(0, 18), sticky="w")

        url_frame = ctk.CTkFrame(details, fg_color=SURFACE_RAISED, corner_radius=RADIUS_SM)
        url_frame.grid(row=2, column=0, padx=26, pady=(0, 18), sticky="ew")
        url_frame.grid_columnconfigure(0, weight=1)

        self.url_disp_label = ctk.CTkLabel(url_frame, text=self.mobile_url,
                                           font=ctk.CTkFont(family=MONO, size=13, weight="bold"),
                                           text_color=PRIMARY, anchor="w")
        self.url_disp_label.grid(row=0, column=0, padx=16, pady=14, sticky="w")

        btn_copy_url = ctk.CTkButton(url_frame, text="Copy link", width=100, height=32,
                                     corner_radius=RADIUS_SM, fg_color=PRIMARY_STRONG,
                                     hover_color=HOVER_PRIMARY, text_color=ON_ACCENT,
                                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                                     command=self.copy_mobile_url)
        btn_copy_url.grid(row=0, column=1, padx=16, pady=12)

        btn_open = ctk.CTkButton(details, text="Open on this computer", height=38,
                                 corner_radius=RADIUS_SM, fg_color=SURFACE_RAISED,
                                 hover_color=HOVER, text_color=TEXT,
                                 font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                                 command=lambda: webbrowser.open(self.mobile_url))
        btn_open.grid(row=3, column=0, padx=26, pady=(0, 12), sticky="w")

        btn_box = ctk.CTkFrame(details, fg_color="transparent")
        btn_box.grid(row=4, column=0, padx=26, pady=(0, 14), sticky="w")

        btn_send_img = ctk.CTkButton(btn_box, text="Send photo", image=_make_icon("image", ON_ACCENT, 15),
                                     font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                                     height=36, corner_radius=RADIUS_SM, fg_color=PRIMARY_STRONG,
                                     hover_color=HOVER_PRIMARY, text_color=ON_ACCENT,
                                     command=self.send_image_file)
        btn_send_img.grid(row=0, column=0, padx=(0, 12))

        btn_send_file = ctk.CTkButton(btn_box, text="Send file", image=_make_icon("folder", TEXT, 15),
                                      font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                                      height=36, corner_radius=RADIUS_SM, fg_color=SURFACE_RAISED,
                                      hover_color=HOVER, text_color=TEXT,
                                      command=self.send_any_file)
        btn_send_file.grid(row=0, column=1, padx=(0, 12))

        btn_send_folder = ctk.CTkButton(btn_box, text="Send folder", image=_make_icon("folder", TEXT, 15),
                                        font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                                        height=36, corner_radius=RADIUS_SM, fg_color=SURFACE_RAISED,
                                        hover_color=HOVER, text_color=TEXT,
                                        command=self.send_folder)
        btn_send_folder.grid(row=0, column=2)

        self.conn_count_label = ctk.CTkLabel(details, text="No phones connected",
                                             font=ctk.CTkFont(family=FONT, size=14, weight="bold"),
                                             text_color=SUCCESS, anchor="w")
        self.conn_count_label.grid(row=5, column=0, padx=26, pady=(8, 22), sticky="w")

        # --- Pairing PIN + trusted devices card ---
        trust_card = ctk.CTkFrame(frame, fg_color=SURFACE, corner_radius=RADIUS_CARD)
        trust_card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(20, 0))

        t_head = ctk.CTkLabel(trust_card, text="Phone access",
                              font=ctk.CTkFont(family=FONT, size=16, weight="bold"),
                              text_color=TEXT, anchor="w")
        t_head.pack(anchor="w", padx=20, pady=(16, 4))

        t_desc = ctk.CTkLabel(trust_card,
                              text="Your phone enters this code once and stays connected afterwards. "
                                   "If you scan the code again in the same phone browser, it connects without asking again.",
                              font=ctk.CTkFont(family=FONT, size=13), text_color=TEXT_SECONDARY,
                              justify="left", anchor="w", wraplength=860)
        t_desc.pack(anchor="w", padx=20, pady=(0, 12))

        pin_row = ctk.CTkFrame(trust_card, fg_color=SURFACE_RAISED, corner_radius=RADIUS_SM)
        pin_row.pack(fill="x", padx=20, pady=(0, 10))
        pin_row.grid_columnconfigure(0, weight=1)

        self.pin_policy_lbl = ctk.CTkLabel(pin_row, text="PAIRING CODE", font=ctk.CTkFont(family=MONO, size=11, weight="bold"),
                                           text_color=TEXT_FAINT, anchor="w")
        self.pin_policy_lbl.grid(row=0, column=0, padx=18, pady=(10, 0), sticky="w")

        pin_value_row = ctk.CTkFrame(pin_row, fg_color="transparent")
        pin_value_row.grid(row=1, column=0, padx=18, pady=(0, 10), sticky="w")
        self.pin_value_lbl = ctk.CTkLabel(pin_value_row, text="······",
                                          font=ctk.CTkFont(family=MONO, size=26, weight="bold"),
                                          text_color=PRIMARY, anchor="w")
        self.pin_value_lbl.grid(row=0, column=0, sticky="w")

        btn_regenerate = ctk.CTkButton(pin_row, text="New code", width=130, height=32,
                                       corner_radius=RADIUS_SM, fg_color=SURFACE_RAISED,
                                       hover_color=HOVER, text_color=TEXT,
                                       font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                                       command=self._regenerate_pin)
        btn_regenerate.grid(row=0, column=1, rowspan=2, padx=18, pady=12)

        trusted_lbl = ctk.CTkLabel(trust_card, text="Connected devices",
                                   font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                                   text_color=TEXT, anchor="w")
        trusted_lbl.pack(anchor="w", padx=20, pady=(0, 6))

        self.trusted_scroll = ctk.CTkScrollableFrame(trust_card, fg_color="transparent", height=116)
        self.trusted_scroll.pack(fill="x", padx=20, pady=(0, 18))
        self.trusted_scroll.grid_columnconfigure(0, weight=1)

        # --- Other computers on this Wi-Fi (auto-find + manual fallback) ---
        peer_card = ctk.CTkFrame(frame, fg_color=SURFACE, corner_radius=RADIUS_CARD)
        peer_card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(20, 0))
        peer_card.grid_columnconfigure(0, weight=1)

        p_head = ctk.CTkLabel(peer_card, text="Other computers nearby",
                              font=ctk.CTkFont(family=FONT, size=16, weight="bold"),
                              text_color=TEXT, anchor="w")
        p_head.grid(row=0, column=0, padx=20, pady=(16, 4), sticky="w")

        p_desc = ctk.CTkLabel(peer_card,
                              text="Computers running ClipBoardSync on this Wi-Fi appear here. "
                                   "If none appear (hotel Wi-Fi often blocks this), enter the address and code manually.",
                              font=ctk.CTkFont(family=FONT, size=13), text_color=TEXT_SECONDARY,
                              justify="left", anchor="w", wraplength=860)
        p_desc.grid(row=1, column=0, padx=20, pady=(0, 10), sticky="w")

        self.peer_status_lbl = ctk.CTkLabel(peer_card, text="Searching for computers…",
                                            font=ctk.CTkFont(family=FONT, size=12),
                                            text_color=TEXT_FAINT, anchor="w")
        self.peer_status_lbl.grid(row=2, column=0, padx=20, pady=(0, 6), sticky="w")

        self.peer_scroll = ctk.CTkScrollableFrame(peer_card, fg_color="transparent", height=84)
        self.peer_scroll.grid(row=3, column=0, padx=20, pady=(0, 6), sticky="ew")
        self.peer_scroll.grid_columnconfigure(0, weight=1)

        manual = ctk.CTkFrame(peer_card, fg_color="transparent")
        manual.grid(row=4, column=0, padx=20, pady=(0, 18), sticky="ew")
        manual.grid_columnconfigure(0, weight=1)
        manual.grid_columnconfigure(1, weight=1)
        self.peer_ip_var = ctk.StringVar(value="")
        self.peer_code_var = ctk.StringVar(value="")
        self.peer_ip_entry = ctk.CTkEntry(
            manual, placeholder_text="Address, e.g. 192.168.1.6",
            height=36, corner_radius=RADIUS_SM,
            fg_color=SURFACE_RAISED, border_color=BORDER,
            text_color=TEXT, placeholder_text_color=TEXT_FAINT,
            textvariable=self.peer_ip_var)
        self.peer_ip_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.peer_code_entry = ctk.CTkEntry(
            manual, placeholder_text="6-digit code from that computer",
            height=36, corner_radius=RADIUS_SM,
            fg_color=SURFACE_RAISED, border_color=BORDER,
            text_color=TEXT, placeholder_text_color=TEXT_FAINT,
            textvariable=self.peer_code_var)
        self.peer_code_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.peer_connect_btn = ctk.CTkButton(
            manual, text="Connect", width=120, height=36,
            corner_radius=RADIUS_SM, fg_color=PRIMARY_STRONG,
            hover_color=HOVER_PRIMARY, text_color=ON_ACCENT,
            font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
            command=self._connect_peer_manual)
        self.peer_connect_btn.grid(row=0, column=2)
        self.peer_disconnect_btn = ctk.CTkButton(
            manual, text="Disconnect", width=120, height=36,
            corner_radius=RADIUS_SM, fg_color=SURFACE_RAISED,
            hover_color=HOVER, text_color=TEXT,
            font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
            command=self._disconnect_peer)
        self.peer_disconnect_btn.grid(row=0, column=3, padx=(8, 0))

        return frame

    def _generate_qr_image(self, data: str) -> None:
        """Render the pairing QR code onto a CustomTkinter label."""
        try:
            qr = qrcode.QRCode(version=1, box_size=8, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            qr_fg = "#111827" if self.theme_name == "light" else "white"
            pil_img = qr.make_image(fill_color=qr_fg, back_color=SURFACE).convert("RGB")
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(212, 212))
            self.qr_label.configure(image=ctk_img)
            self.qr_label.image = ctk_img
        except Exception as exc:
            self.qr_label.configure(text="Could not show the code. Copy the link below instead.", image=None)

    def _refresh_pairing_view(self) -> None:
        """Refresh the pairing code label and the list of connected phones."""
        if not hasattr(self, "pin_value_lbl"):
            return
        enabled = self.trust_store.require_pin
        self.pin_value_lbl.configure(text=self.trust_store.pairing_pin if enabled else "Not needed")
        self.pin_policy_lbl.configure(text="PAIRING CODE" if enabled else "PAIRING CODE (TURNED OFF)")

        for child in self.trusted_scroll.winfo_children():
            child.destroy()

        devices = self.trust_store.trusted_devices()
        if not devices:
            lbl = ctk.CTkLabel(self.trusted_scroll, text="No phones connected yet. Scan the code and enter the pairing code once.",
                               font=ctk.CTkFont(family=FONT, size=13), text_color=TEXT_FAINT, anchor="w")
            lbl.grid(row=0, column=0, sticky="w", pady=4)
            return

        for i, dev in enumerate(devices):
            row = ctk.CTkFrame(self.trusted_scroll, fg_color=SURFACE_RAISED, corner_radius=RADIUS_SM)
            row.grid(row=i, column=0, sticky="ew", pady=3)
            row.grid_columnconfigure(0, weight=1)
            name_lbl = ctk.CTkLabel(row, text="Your phone",
                                    font=ctk.CTkFont(family=FONT, size=13), text_color=TEXT, anchor="w")
            name_lbl.grid(row=0, column=0, padx=12, pady=6, sticky="w")
            btn_rm = ctk.CTkButton(row, text="Remove", width=78, height=28, corner_radius=RADIUS_SM,
                                   fg_color="transparent", hover_color=DANGER, text_color=TEXT_SECONDARY,
                                   font=ctk.CTkFont(family=FONT, size=11, weight="bold"),
                                   command=lambda d=dev: self._remove_device(d))
            btn_rm.grid(row=0, column=1, padx=8, pady=5)

    def _regenerate_pin(self) -> None:
        pin = self.trust_store.regenerate_pin()
        self._refresh_pairing_view()
        self.log_queue.put(f"[Info] New pairing code created: {pin}")

    def _remove_device(self, device_id: str) -> None:
        self.trust_store.untrust(device_id)
        self._refresh_pairing_view()
        self.log_queue.put("[Info] Removed a connected phone. It will need the pairing code to connect again.")

    # ------------------------------------------------------------------
    # Settings tab (sharing control + activity log + help)
    # ------------------------------------------------------------------
    def _build_tab_settings(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        # Sharing control
        sec1 = ctk.CTkFrame(frame, fg_color=SURFACE, corner_radius=RADIUS_CARD)
        sec1.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        sec1.grid_columnconfigure(0, weight=1)

        t1 = ctk.CTkLabel(sec1, text="Sharing", font=ctk.CTkFont(family=FONT, size=16, weight="bold"),
                          text_color=TEXT, anchor="w")
        t1.grid(row=0, column=0, padx=20, pady=(16, 4), sticky="w")

        d1 = ctk.CTkLabel(sec1, text="Start or stop sharing with your phone. Both need to be on the "
                                     "same Wi-Fi.",
                          font=ctk.CTkFont(family=FONT, size=13), text_color=TEXT_SECONDARY,
                          justify="left", anchor="w", wraplength=680)
        d1.grid(row=1, column=0, padx=20, pady=(0, 12), sticky="w")

        row = ctk.CTkFrame(sec1, fg_color="transparent")
        row.grid(row=2, column=0, padx=20, pady=(0, 18), sticky="w")
        self.settings_toggle_btn = ctk.CTkButton(row, text="Start sharing",
                                                 font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
                                                 height=38, corner_radius=RADIUS_SM, fg_color=PRIMARY,
                                                 hover_color=PRIMARY_STRONG, text_color=ON_ACCENT,
                                                 command=self.toggle_engine)
        self.settings_toggle_btn.grid(row=0, column=0, padx=(0, 14))
        self.settings_status_lbl = ctk.CTkLabel(row, text="Status: Not sharing",
                                                font=ctk.CTkFont(family=FONT, size=12),
                                                text_color=TEXT_FAINT, anchor="w")
        self.settings_status_lbl.grid(row=0, column=1, sticky="w")

        self.require_pin_var = ctk.BooleanVar(value=self.trust_store.require_pin)
        chk_pin = ctk.CTkCheckBox(sec1, text="Ask for a pairing code on new phones (only the first time)",
                                  variable=self.require_pin_var, command=self._toggle_require_pin,
                                  text_color=TEXT_SECONDARY, fg_color=PRIMARY_STRONG,
                                  hover_color=PRIMARY_STRONG, font=ctk.CTkFont(family=FONT, size=12))
        chk_pin.grid(row=3, column=0, padx=20, pady=(0, 16), sticky="w")

        # Activity log
        sec2 = ctk.CTkFrame(frame, fg_color=SURFACE, corner_radius=RADIUS_CARD)
        sec2.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        sec2.grid_columnconfigure(0, weight=1)

        log_head = ctk.CTkFrame(sec2, fg_color="transparent")
        log_head.grid(row=0, column=0, padx=20, pady=(16, 6), sticky="ew")
        log_head.grid_columnconfigure(0, weight=1)

        t2 = ctk.CTkLabel(log_head, text="Activity log", font=ctk.CTkFont(family=FONT, size=16, weight="bold"),
                          text_color=TEXT, anchor="w")
        t2.grid(row=0, column=0, sticky="w")

        chk_scroll = ctk.CTkCheckBox(log_head, text="Auto-scroll", variable=self.auto_scroll_logs,
                                     text_color=TEXT_SECONDARY, fg_color=PRIMARY_STRONG,
                                     hover_color=PRIMARY_STRONG, font=ctk.CTkFont(family=FONT, size=12))
        chk_scroll.grid(row=0, column=1, padx=12)

        btn_clear = ctk.CTkButton(log_head, text="Clear", width=70, height=30, corner_radius=RADIUS_SM,
                                  fg_color=SURFACE_RAISED, hover_color=HOVER, text_color=TEXT_SECONDARY,
                                  font=ctk.CTkFont(family=FONT, size=12), command=self.clear_logs)
        btn_clear.grid(row=0, column=2)

        self.log_textbox = ctk.CTkTextbox(
            sec2, height=180, corner_radius=RADIUS_SM, fg_color=BG, text_color=TEXT,
            font=ctk.CTkFont(family=MONO, size=12), border_color=BORDER, border_width=1,
            wrap="word",
        )
        self.log_textbox.grid(row=1, column=0, padx=20, pady=(4, 20), sticky="ew")
        self.log_textbox.insert("0.0", "ClipBoardSync is ready. Sharing starts automatically.\n")
        self.log_textbox.configure(state="disabled")

        # Help
        sec3 = ctk.CTkFrame(frame, fg_color=SURFACE, corner_radius=RADIUS_CARD)
        sec3.grid(row=2, column=0, sticky="nsew", pady=(0, 0))
        sec3.grid_columnconfigure(0, weight=1)

        t3 = ctk.CTkLabel(sec3, text="Help & FAQ", font=ctk.CTkFont(family=FONT, size=16, weight="bold"),
                          text_color=TEXT, anchor="w")
        t3.grid(row=0, column=0, padx=20, pady=(16, 6), sticky="w")

        guide = (
            "How it works — ClipBoardSync shares your clipboard between this computer and your phone "
            "over your Wi-Fi only. No accounts, no cloud.\n\n"
            "Phone can't open the page? Make sure both are on the same Wi-Fi and allow ClipBoardSync "
            "through Windows Firewall for private networks.\n\n"
            "Does it work both ways? Yes — copy here and it appears on your phone. Send from your phone "
            "and it is ready to paste here (Ctrl+V).\n\n"
            "Do I need internet? No — it works on your home or hotspot Wi-Fi even when offline."
        )
        body = ctk.CTkLabel(sec3, text=guide, font=ctk.CTkFont(family=FONT, size=13),
                            text_color=TEXT_SECONDARY, justify="left", anchor="w", wraplength=760)
        body.grid(row=1, column=0, padx=20, pady=(0, 22), sticky="w")

        return frame

    # ------------------------------------------------------------------
    # Clipboard helpers
    # ------------------------------------------------------------------
    def copy_mobile_url(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.mobile_url)
        self.update()
        self.log_queue.put("[Info] Copied the phone link. Open it on your phone to connect.")

    def copy_clip_to_local(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.log_queue.put("[Info] Copied to this computer. Ready to paste with Ctrl+V.")

    def copy_image_to_local(self, data_uri: str) -> None:
        """Copy a shared photo to this computer so it can be pasted."""
        try:
            b64_str = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
            raw = base64.b64decode(b64_str)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "BMP")
            dib_bytes = buf.getvalue()[14:]

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_DIB, dib_bytes)
            finally:
                win32clipboard.CloseClipboard()
            self.log_queue.put("[Info] Photo copied. Ready to paste with Ctrl+V.")
        except Exception as exc:
            self.log_queue.put(f"[Error] Could not copy the photo: {exc}")

    def send_image_file(self) -> None:
        """Let the user pick a photo on this computer and share it with the phone."""
        filepath = filedialog.askopenfilename(
            title="Choose a photo to share",
            filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.gif;*.webp"), ("All Files", "*.*")]
        )
        if not filepath:
            return
        try:
            path = Path(filepath)
            raw = path.read_bytes()
            ext = path.suffix.lower().replace(".", "")
            if ext == "jpg":
                ext = "jpeg"
            b64_str = base64.b64encode(raw).decode("utf-8")
            data_uri = f"data:image/{ext};base64,{b64_str}"

            self.copy_image_to_local(data_uri)

            from server.models import ClipboardItem, get_utc_now_iso
            item = ClipboardItem(
                device_id="This computer",
                timestamp=get_utc_now_iso(),
                type="image",
                content=data_uri,
                filename=path.name,
                filesize=len(raw),
            )
            self._broadcast_item(item.to_message_dict())
            self.log_queue.put(f"[Info] Shared photo '{path.name}' with your phone.")
            self._refresh_active_list()
        except Exception as exc:
            self.log_queue.put(f"[Error] Could not share the photo: {exc}")

    def save_shared_item(self, file_url: str, filename: str) -> None:
        """Download a shared file/folder link to disk (streamed, fast)."""
        if not file_url:
            self.log_queue.put("[Error] Nothing to save yet. Try again in a moment.")
            return
        initial = Path.home() / "Downloads" / "ClipBoardSync" / filename
        dest_str = filedialog.asksaveasfilename(
            title="Save shared file",
            initialfile=filename,
            initialdir=str(initial.parent),
        )
        if not dest_str:
            return
        self.log_queue.put(f"[Info] Saving '{filename}'…")
        threading.Thread(
            target=self._save_shared_item_worker,
            args=(file_url, dest_str, filename),
            name="clipboardsync-download",
            daemon=True,
        ).start()

    def _save_shared_item_worker(self, file_url: str, dest_str: str, filename: str) -> None:
        try:
            import urllib.request
            req = urllib.request.Request(file_url, headers={"User-Agent": "ClipBoardSync"})
            with urllib.request.urlopen(req, timeout=300) as resp, open(dest_str, "wb") as out:
                shutil.copyfileobj(resp, out, length=1024 * 1024)
            self.log_queue.put(f"[Info] Saved '{filename}' to {dest_str}.")
        except Exception as exc:
            self.log_queue.put(f"[Error] Could not save '{filename}'. Check the connection and try again. ({exc})")

    def send_any_file(self) -> None:
        """Let the user pick any file and share it."""
        filepath = filedialog.askopenfilename(title="Choose a file to share")
        if not filepath:
            return
        try:
            import uuid
            from server.main import UPLOADS_DIR
            safe_name = f"{uuid.uuid4().hex[:10]}{Path(filepath).suffix}"
            dest = UPLOADS_DIR / safe_name
            # Streamed copy (no full-RAM read) so big files stay fast
            with open(filepath, "rb") as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)

            from server.models import ClipboardItem, get_utc_now_iso
            item = ClipboardItem(
                device_id="This computer",
                timestamp=get_utc_now_iso(),
                type="file",
                content=f"File: {Path(filepath).name}",
                filename=Path(filepath).name,
                filesize=Path(filepath).stat().st_size,
                file_url=f"/api/files/{safe_name}",
            )
            self._broadcast_item(item.to_message_dict())
            self.log_queue.put(f"[Info] Shared file '{Path(filepath).name}'. Others can download it directly.")
            self._refresh_active_list()
        except Exception as exc:
            self.log_queue.put(f"[Error] Could not share the file: {exc}")

    def send_folder(self) -> None:
        """Let the user pick a folder, zip it, and share one .zip."""
        folder = filedialog.askdirectory(title="Choose a folder to share")
        if not folder:
            return
        self.log_queue.put(f"[Info] Zipping '{Path(folder).name}'…")
        threading.Thread(
            target=self._send_folder_worker,
            args=(folder,),
            name="clipboardsync-zip",
            daemon=True,
        ).start()

    def _send_folder_worker(self, folder: str) -> None:
        try:
            import uuid
            from server.folder_zip import zip_directory
            from server.main import UPLOADS_DIR
            zip_path, zip_name, entries, skipped = zip_directory(folder)
            try:
                size = zip_path.stat().st_size
                if size > 1024 * 1024 * 1024:
                    self.log_queue.put(f"[Error] '{Path(folder).name}' is too big to share (over 1 GB zipped).")
                    return
                safe_name = f"{uuid.uuid4().hex[:10]}.zip"
                dest = UPLOADS_DIR / safe_name
                shutil.move(str(zip_path), str(dest))
                from server.models import ClipboardItem, get_utc_now_iso
                item = ClipboardItem(
                    device_id="This computer",
                    timestamp=get_utc_now_iso(),
                    type="folder",
                    content=f"Folder: {zip_name} ({entries} items)",
                    filename=zip_name,
                    filesize=size,
                    file_url=f"/api/files/{safe_name}",
                    entry_count=entries,
                    skipped_count=skipped,
                )
                self._broadcast_item(item.to_message_dict())
                extra = f" (skipped {skipped} system files)" if skipped else ""
                self.log_queue.put(f"[Info] Shared folder '{zip_name}' ({entries} items){extra}.")
                self._refresh_active_list()
            finally:
                try:
                    Path(zip_path).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as exc:
            self.log_queue.put(f"[Error] Could not share the folder: {exc}")

    def _broadcast_item(self, message: dict[str, Any]) -> None:
        if self.engine._loop and self.engine._loop.is_running():
            asyncio.run_coroutine_threadsafe(sync_hub.handle_message(None, message), self.engine._loop)
        # Two-way PC-to-PC: also forward our clips to the connected computer.
        try:
            self.engine.send_to_peer(message)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Automation loops
    # ------------------------------------------------------------------
    def _start_automation(self) -> None:
        self.after(100, self._process_log_queue)
        self.after(2000, self._poll_hub_status)
        try:
            self.discovery.start()
        except Exception:
            pass
        self.toggle_engine()

    # ------------------------------------------------------------------
    # PC-to-PC: auto-found computers + manual address + code
    # ------------------------------------------------------------------
    def _connect_peer_manual(self) -> None:
        ip = (self.peer_ip_var.get() if self.peer_ip_var else "").strip()
        code = (self.peer_code_var.get() if self.peer_code_var else "").strip().replace(" ", "")
        if not ip:
            self.log_queue.put("[Error] Enter the other computer's address first (e.g. 192.168.1.6).")
            return
        if len(code) != 6 or not code.isdigit():
            self.log_queue.put("[Error] Enter the 6-digit code shown on the other computer.")
            return
        self._connect_peer(ip, self.port, code)

    def _connect_peer(self, ip: str, port: int, code: str) -> None:
        if not self.engine.is_running:
            self.log_queue.put("[Error] Start sharing first, then connect to the other computer.")
            return
        if self.engine._peer is not None:
            self.log_queue.put("[Info] Already connected to another computer. Disconnect first.")
            return
        # Quick pre-flight: is there really a ClipBoardSync server there?
        # (Surfaces firewall problems in 2s instead of hanging.)
        def _check_and_connect() -> None:
            import urllib.request
            try:
                with urllib.request.urlopen(f"http://{ip}:{port}/api/history", timeout=4) as resp:
                    if resp.status != 200:
                        raise OSError(f"HTTP {resp.status}")
            except Exception as exc:
                self.log_queue.put(
                    f"[Error] Couldn't reach {ip}:{port}. Same Wi-Fi? Allow ClipBoardSync through the firewall. ({exc})"
                )
                return
            ok = self.engine.connect_peer(ip, port, code, self._peer_device_id)
            if ok:
                self.log_queue.put(f"[Info] Connecting to other computer at {ip}…")
            else:
                self.log_queue.put("[Error] Could not start the computer connection. Try again.")
        threading.Thread(target=_check_and_connect, name="peer-preflight", daemon=True).start()

    def _disconnect_peer(self) -> None:
        self.engine.disconnect_peer()
        self._refresh_peer_view(force=True)

    def _refresh_peer_view(self, force: bool = False) -> None:
        if not hasattr(self, "peer_scroll"):
            return
        try:
            found = self.discovery.peers() if hasattr(self, "discovery") else []
        except Exception:
            found = []
        status = getattr(self.engine, "peer_status", "disconnected")
        host = getattr(self.engine, "peer_host", "")
        sig = (tuple((p.get("ip"), p.get("port"), p.get("name")) for p in found), status, host)
        if not force and sig == self._last_peer_sig:
            return
        self._last_peer_sig = sig

        if status == "connected" and host:
            self.peer_status_lbl.configure(text=f"Connected to other computer at {host}.", text_color=SUCCESS)
        elif status == "connecting":
            self.peer_status_lbl.configure(text="Connecting to other computer…", text_color=WARNING)
        elif status == "retrying":
            self.peer_status_lbl.configure(text="Connection lost. Retrying…", text_color=WARNING)
        elif status.startswith("code_rejected"):
            self.peer_status_lbl.configure(text="That code did not match. Check it and try again.", text_color=DANGER)
        elif found:
            self.peer_status_lbl.configure(
                text=f"{len(found)} computer{'s' if len(found) != 1 else ''} found nearby." if found else "Searching for computers…",
                text_color=TEXT_SECONDARY)
        else:
            self.peer_status_lbl.configure(text="Searching for computers…", text_color=TEXT_FAINT)

        for child in self.peer_scroll.winfo_children():
            child.destroy()
        if not found:
            lbl = ctk.CTkLabel(self.peer_scroll,
                               text="No computers found yet. They appear automatically when on the same Wi-Fi.",
                               font=ctk.CTkFont(family=FONT, size=12), text_color=TEXT_FAINT, anchor="w",
                               wraplength=640)
            lbl.grid(row=0, column=0, sticky="w", pady=4)
            return
        for i, p in enumerate(found[:8]):
            row = ctk.CTkFrame(self.peer_scroll, fg_color=SURFACE_RAISED, corner_radius=RADIUS_SM)
            row.grid(row=i, column=0, sticky="ew", pady=3)
            row.grid_columnconfigure(0, weight=1)
            name_lbl = ctk.CTkLabel(row, text=f"{p.get('name', 'Computer')}  ·  {p.get('ip')}",
                                    font=ctk.CTkFont(family=FONT, size=13), text_color=TEXT, anchor="w")
            name_lbl.grid(row=0, column=0, padx=12, pady=6, sticky="w")
            btn = ctk.CTkButton(row, text="Connect", width=90, height=28, corner_radius=RADIUS_SM,
                                fg_color=PRIMARY_STRONG, hover_color=HOVER_PRIMARY, text_color=ON_ACCENT,
                                font=ctk.CTkFont(family=FONT, size=11, weight="bold"),
                                command=lambda ip=p.get("ip"), port=int(p.get("port", 8000)): self._connect_peer_from_list(ip, port))
            btn.grid(row=0, column=1, padx=8, pady=5)

    def _connect_peer_from_list(self, ip: str, port: int) -> None:
        code = (self.peer_code_var.get() if self.peer_code_var else "").strip().replace(" ", "")
        if len(code) != 6 or not code.isdigit():
            self.log_queue.put(f"[Info] Enter the 6-digit code from {ip} in the box below, then press Connect.")
            try:
                self.peer_ip_var.set(ip)
                self.peer_code_entry.focus_set()
            except Exception:
                pass
            return
        self._connect_peer(ip, port, code)

    def _process_log_queue(self) -> None:
        messages: list[str] = []
        try:
            while True:
                messages.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass

        if messages:
            self.log_textbox.configure(state="normal")
            for msg in messages:
                line = msg.strip()
                self.log_textbox.insert("end", line + "\n")
                self._log_lines.append(line)
            if self.auto_scroll_logs.get():
                self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")

        self.after(150, self._process_log_queue)

    def _poll_hub_status(self) -> None:
        running = self.engine.is_running
        cnt = sync_hub.connection_count if running else 0

        if cnt == 0:
            conn_text = "No phones connected"
        elif cnt == 1:
            conn_text = "1 phone connected"
        else:
            conn_text = f"{cnt} phones connected"

        if running:
            self.status_badge.configure(text="● SHARING ON", text_color=SUCCESS)
            self.settings_status_lbl.configure(text=f"Status: Sharing on · Port {self.port}", text_color=SUCCESS)
            self.toggle_engine_btn.configure(text="STOP SHARING", fg_color=DANGER,
                                             hover_color=DANGER_HOVER, text_color=ON_ACCENT)
            self.settings_toggle_btn.configure(text="Stop sharing", fg_color=DANGER,
                                               hover_color=DANGER_HOVER, text_color=ON_ACCENT)
            self.conn_count_label.configure(text=conn_text)
        else:
            self.status_badge.configure(text="● NOT SHARING", text_color=DANGER)
            self.settings_status_lbl.configure(text="Status: Not sharing", text_color=TEXT_FAINT)
            self.toggle_engine_btn.configure(text="START SHARING", fg_color=PRIMARY,
                                             hover_color=PRIMARY_STRONG, text_color=ON_ACCENT)
            self.settings_toggle_btn.configure(text="Start sharing", fg_color=PRIMARY,
                                               hover_color=PRIMARY_STRONG, text_color=ON_ACCENT)
            self.conn_count_label.configure(text="No phones connected")

        self.conn_label.configure(text=conn_text if running else "No phones connected")

        if self._active_tab == "devices":
            pair_sig = (
                tuple(self.trust_store.trusted_devices()),
                self.trust_store.pairing_pin,
                self.trust_store.require_pin,
            )
            if pair_sig != self._last_pairing_sig:
                self._last_pairing_sig = pair_sig
                self._refresh_pairing_view()
            self._refresh_peer_view()

        if self._active_tab in ("clipboard", "pinned", "files"):
            hist = sync_hub.get_history()
            sig = tuple((i.get("id"), i.get("type"), str(i.get("content", ""))[:40], i.get("filename")) for i in hist)
            if sig != self._last_sig:
                self._last_sig = sig
                self._refresh_active_list()

        self.after(2000, self._poll_hub_status)

    def toggle_engine(self) -> None:
        """Start or stop sharing with connected phones."""
        if not self.engine.is_running:
            self.engine.start(self.log_queue)
            self.status_badge.configure(text="◌ STARTING…", text_color=WARNING)
            self.settings_status_lbl.configure(text="Status: Starting sharing…", text_color=WARNING)
        else:
            self.engine.stop()
            self.status_badge.configure(text="● NOT SHARING", text_color=DANGER)

    def _toggle_require_pin(self) -> None:
        enabled = bool(self.require_pin_var.get())
        self.trust_store.require_pin = enabled
        self.log_queue.put("[Info] Pairing code " + ("turned on for new phones." if enabled else "turned off. New phones can connect directly."))
        self._refresh_pairing_view()

    def clear_logs(self) -> None:
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("0.0", "end")
        self.log_textbox.insert("0.0", "Activity log cleared.\n")
        self.log_textbox.configure(state="disabled")

    def on_close_request(self) -> None:
        """Close the window and stop sharing cleanly."""
        try:
            self.discovery.stop()
        except Exception:
            pass
        try:
            self.engine.disconnect_peer()
        except Exception:
            pass
        if self.engine.is_running:
            self.engine.stop()
        sys.stdout = self.original_stdout
        self.destroy()
        sys.exit(0)


def _preview_text(text: str, limit: int = 280) -> str:
    """Collapse whitespace and truncate long text for a clean list preview."""
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[:limit] + "…"


def _format_time(iso_str: Any) -> str:
    try:
        dt = datetime.datetime.fromisoformat(str(iso_str))
        now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
        diff = (now - dt).replace(tzinfo=None)
        mins = int(diff.total_seconds() // 60)
        if mins < 1:
            return "Just now"
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return dt.strftime("%b %d, %H:%M")
    except Exception:
        return ""


def _format_device(device_id: Any) -> str:
    """Show a friendly device name instead of a technical ID."""
    s = str(device_id or "").strip()
    if not s or s.lower() == "unknown":
        return "Unknown device"
    low = s.lower()
    if low in ("desktop-gui", "server", "this computer") or "desktop" in low or "win" in low or len(s) > 20:
        return "This computer"
    if low.startswith("pc-") or "other computer" in low:
        return "Other computer"
    if low.startswith(("phone-", "web-")) or "phone" in low:
        return "Your phone"
    if len(s) > 14:
        return f"{s[:14]}…"
    return s
