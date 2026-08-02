"""Windows Desktop GUI implementation for ClipBoardSync using CustomTkinter."""

from __future__ import annotations

import asyncio
import base64
import datetime
import io
import logging
import queue
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
from PIL import Image
import qrcode
import uvicorn

from client.config import Config
from client.main import ClipBoardSyncApp
from server.main import app as fastapi_app, hub as sync_hub


# Configure default visual styling
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

logger = logging.getLogger("clipboardsync.gui")


def get_local_lan_ip() -> str:
    """Discover the primary IPv4 network LAN address for Wi-Fi pairing."""
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


class QueueLogHandler(logging.Handler):
    """Routes Python standard log stream directly to a GUI queue for displaying in real time."""
    def __init__(self, log_queue: queue.Queue[str]) -> None:
        super().__init__(level=logging.INFO)
        self.log_queue = log_queue
        self.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.log_queue.put(msg)
        except Exception:
            pass


class StdoutRedirector:
    """Intercepts print statements across the app to stream seamlessly into the GUI console."""
    def __init__(self, log_queue: queue.Queue[str], original_stdout: Any) -> None:
        self.log_queue = log_queue
        self.original_stdout = original_stdout

    def write(self, text: str) -> None:
        if text.strip():
            for line in text.rstrip().split('\n'):
                if line.strip():
                    self.log_queue.put(f"[SYSOUT] {line}")
        if self.original_stdout:
            self.original_stdout.write(text)
            self.original_stdout.flush()

    def flush(self) -> None:
        if self.original_stdout:
            self.original_stdout.flush()


class BackgroundEngine:
    """Manages concurrent execution of Uvicorn WebSocket server and Win32 Clipboard sync client."""

    def __init__(self, port: int = 8000) -> None:
        self.port = port
        self.is_running = False
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._client_app: ClipBoardSyncApp | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, log_queue: queue.Queue[str]) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(log_queue,),
            name="clipboardsync-worker",
            daemon=True
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
            log_queue.put(f"[ERROR] Engine halted unexpectedly: {exc}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self.is_running = False

    async def _async_runner(self, log_queue: queue.Queue[str]) -> None:
        log_queue.put(f"[ENGINE] Launching ClipBoardSync Server on port {self.port}...")
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
        log_queue.put("[ENGINE] Connecting Win32 Desktop Clipboard Monitor...")
        client_config = Config()
        client_config.websocket_url = f"ws://127.0.0.1:{self.port}/ws"
        self._client_app = ClipBoardSyncApp(client_config)

        client_task = asyncio.create_task(self._client_app.run(), name="win32-client")
        log_queue.put("[ENGINE] Cross-device synchronization active and ready!")

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
        log_queue.put("[ENGINE] Synchronization bridge cleanly shut down.")


class ClipBoardSyncGUI(ctk.CTk):
    """Main Application graphical dashboard interface."""

    def __init__(self) -> None:
        super().__init__()
        self.title("ClipBoardSync - Universal Local Wi-Fi Clipboard Bridge")
        self.geometry("980x640")
        self.minsize(840, 560)
        
        # Windows taskbar & close protocols
        self.protocol("WM_DELETE_WINDOW", self.on_close_request)

        # Internal state & queues
        self.port = 8000
        self.local_ip = get_local_lan_ip()
        self.mobile_url = f"http://{self.local_ip}:{self.port}"
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.engine = BackgroundEngine(port=self.port)
        self.auto_scroll_logs = ctk.BooleanVar(value=True)

        # Connect log redirectors
        self.log_handler = QueueLogHandler(self.log_queue)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger("clipboardsync").setLevel(logging.INFO)
        self.original_stdout = sys.stdout
        sys.stdout = StdoutRedirector(self.log_queue, self.original_stdout)

        self._init_ui()
        self._start_automation()

    def _init_ui(self) -> None:
        """Construct visual components, sidebar navigation, and content view frames."""
        # Grid structure: 1 sidebar column, 1 main expandable column
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- LEFT SIDEBAR ---
        self.sidebar_frame = ctk.CTkFrame(self, width=230, corner_radius=0, fg_color="#181824")
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(5, weight=1)

        brand_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="⚡ ClipBoardSync",
            font=ctk.CTkFont(size=21, weight="bold"),
            text_color="#00E5FF"
        )
        brand_label.grid(row=0, column=0, padx=20, pady=(24, 4))
        
        sub_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="Cross-Device Wi-Fi Bridge",
            font=ctk.CTkFont(size=12),
            text_color="#9FA8DA"
        )
        sub_label.grid(row=1, column=0, padx=20, pady=(0, 16))

        # Status badge indicator
        self.status_badge = ctk.CTkLabel(
            self.sidebar_frame,
            text="  🔴 OFFLINE  ",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#B71C1C",
            text_color="#FFFFFF",
            corner_radius=12,
            height=26
        )
        self.status_badge.grid(row=2, column=0, padx=20, pady=(0, 20), sticky="ew")

        # Navigation menu buttons
        self.btn_tab_pairing = self._create_nav_button("📱 Mobile Pairing", 0, self._show_tab_pairing)
        self.btn_tab_pairing.grid(row=3, column=0, padx=12, pady=6, sticky="ew")

        self.btn_tab_logs = self._create_nav_button("📝 Live Activity Logs", 1, self._show_tab_logs)
        self.btn_tab_logs.grid(row=4, column=0, padx=12, pady=6, sticky="ew")

        self.btn_tab_history = self._create_nav_button("🕒 Synced Clip History", 2, self._show_tab_history)
        self.btn_tab_history.grid(row=5, column=0, padx=12, pady=6, sticky="nw")

        self.btn_tab_guide = self._create_nav_button("ℹ️ Help & Instructions", 3, self._show_tab_guide)
        self.btn_tab_guide.grid(row=6, column=0, padx=12, pady=(6, 16), sticky="ew")

        # Bottom engine toggle button
        self.toggle_engine_btn = ctk.CTkButton(
            self.sidebar_frame,
            text="▶ START BRIDGE",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=42,
            fg_color="#00C853",
            hover_color="#00B248",
            command=self.toggle_engine
        )
        self.toggle_engine_btn.grid(row=7, column=0, padx=16, pady=(0, 24), sticky="ew")

        # --- MAIN CONTAINER AREA ---
        self.main_container = ctk.CTkFrame(self, fg_color="#101018", corner_radius=0)
        self.main_container.grid(row=0, column=1, sticky="nsew")
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(0, weight=1)

        # Build modular tab frames
        self.tab_pairing = self._build_tab_pairing()
        self.tab_logs = self._build_tab_logs()
        self.tab_history = self._build_tab_history()
        self.tab_guide = self._build_tab_guide()

        # Activate initial view
        self._show_tab_pairing()

    def _create_nav_button(self, text: str, idx: int, cmd: Any) -> ctk.CTkButton:
        return ctk.CTkButton(
            self.sidebar_frame,
            text=text,
            anchor="w",
            font=ctk.CTkFont(size=14),
            height=38,
            fg_color="transparent",
            hover_color="#252538",
            text_color="#E0E0E0",
            command=cmd
        )

    def _select_nav(self, selected_btn: ctk.CTkButton, active_frame: ctk.CTkFrame) -> None:
        """Switch highlighted menu item and visible display view."""
        for btn in (self.btn_tab_pairing, self.btn_tab_logs, self.btn_tab_history, self.btn_tab_guide):
            btn.configure(fg_color="transparent" if btn != selected_btn else "#2A2A40", text_color="#FFFFFF" if btn == selected_btn else "#B0B0C0")
        for frame in (self.tab_pairing, self.tab_logs, self.tab_history, self.tab_guide):
            frame.grid_remove()
        active_frame.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)

    def _show_tab_pairing(self) -> None:
        self._select_nav(self.btn_tab_pairing, self.tab_pairing)

    def _show_tab_logs(self) -> None:
        self._select_nav(self.btn_tab_logs, self.tab_logs)

    def _show_tab_history(self) -> None:
        self._select_nav(self.btn_tab_history, self.tab_history)
        self._refresh_history_view()

    def _show_tab_guide(self) -> None:
        self._select_nav(self.btn_tab_guide, self.tab_guide)

    # --- TAB 1: PAIRING & QR CODE ---
    def _build_tab_pairing(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        # Left Column: QR Card
        qr_card = ctk.CTkFrame(frame, fg_color="#1E1E2C", corner_radius=16)
        qr_card.grid(row=0, column=0, sticky="ns", padx=(0, 20))
        
        title_qr = ctk.CTkLabel(qr_card, text="Smartphone QR Pairing", font=ctk.CTkFont(size=18, weight="bold"))
        title_qr.pack(pady=(24, 12), padx=24)

        self.qr_label = ctk.CTkLabel(qr_card, text="", width=260, height=260)
        self.qr_label.pack(padx=24, pady=12)
        self._generate_qr_image(self.mobile_url)

        qr_note = ctk.CTkLabel(
            qr_card,
            text="Scan with standard Android\nor iPhone Camera app",
            font=ctk.CTkFont(size=13),
            text_color="#9FA8DA"
        )
        qr_note.pack(pady=(4, 24), padx=24)

        # Right Column: Details & Controls
        details_card = ctk.CTkFrame(frame, fg_color="#1E1E2C", corner_radius=16)
        details_card.grid(row=0, column=1, sticky="nsew")
        details_card.grid_columnconfigure(0, weight=1)

        title_info = ctk.CTkLabel(
            details_card,
            text="How to Connect Your Devices",
            font=ctk.CTkFont(size=20, weight="bold"),
            anchor="w"
        )
        title_info.grid(row=0, column=0, padx=28, pady=(26, 10), sticky="w")

        instr_text = (
            "1. Check Wi-Fi Connection:\n"
            "   Ensure both this computer and your mobile device are on the exact\n"
            "   same Wi-Fi network or personal hotspot.\n\n"
            "2. Instant Camera Scanning:\n"
            "   Open your phone camera app and point it at the QR Code on the left.\n"
            "   Tap the browser pop-up link that appears to launch the sync portal.\n\n"
            "3. No App Installation Required:\n"
            "   Everything operates in real time over local network WebSockets!"
        )
        instr_label = ctk.CTkLabel(
            details_card,
            text=instr_text,
            font=ctk.CTkFont(size=14),
            text_color="#CCCCCC",
            justify="left",
            anchor="w"
        )
        instr_label.grid(row=1, column=0, padx=28, pady=(0, 20), sticky="w")

        # LAN URL Copy section
        url_frame = ctk.CTkFrame(details_card, fg_color="#14141D", corner_radius=12)
        url_frame.grid(row=2, column=0, padx=28, pady=(0, 24), sticky="ew")
        url_frame.grid_columnconfigure(0, weight=1)

        self.url_disp_label = ctk.CTkLabel(
            url_frame,
            text=f"LAN Portal Address:  {self.mobile_url}",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#00E5FF",
            anchor="w"
        )
        self.url_disp_label.grid(row=0, column=0, padx=16, pady=16, sticky="w")

        btn_copy_url = ctk.CTkButton(
            url_frame,
            text="📋 Copy Link",
            width=110,
            fg_color="#3949AB",
            hover_color="#303F9F",
            command=self.copy_mobile_url
        )
        btn_copy_url.grid(row=0, column=1, padx=16, pady=16)

        btn_open_browser = ctk.CTkButton(
            details_card,
            text="🌐 Open Web Portal on PC",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=40,
            fg_color="#26A69A",
            hover_color="#00897B",
            command=lambda: webbrowser.open(self.mobile_url)
        )
        btn_open_browser.grid(row=3, column=0, padx=28, pady=(0, 16), sticky="w")

        # Action toolbar for sending images or files from desktop
        btn_box = ctk.CTkFrame(details_card, fg_color="transparent")
        btn_box.grid(row=4, column=0, padx=28, pady=(0, 16), sticky="w")

        btn_send_img = ctk.CTkButton(
            btn_box,
            text="📷 Send Image",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=34,
            fg_color="#7B1FA2",
            hover_color="#6A1B9A",
            command=self.send_image_file
        )
        btn_send_img.grid(row=0, column=0, padx=(0, 12))

        btn_send_file = ctk.CTkButton(
            btn_box,
            text="📁 Send File",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=34,
            fg_color="#1565C0",
            hover_color="#0D47A1",
            command=self.send_any_file
        )
        btn_send_file.grid(row=0, column=1)

        # Live connected peer indicator
        self.conn_count_label = ctk.CTkLabel(
            details_card,
            text="📡 Active Connected Devices: 0",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#A5D6A7",
            anchor="w"
        )
        self.conn_count_label.grid(row=5, column=0, padx=28, pady=(12, 24), sticky="w")

        return frame

    def _generate_qr_image(self, data: str) -> None:
        """Render high-contrast QR Code directly onto a CustomTkinter canvas."""
        try:
            qr = qrcode.QRCode(version=1, box_size=8, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            pil_img = qr.make_image(fill_color="white", back_color="#1E1E2C").convert("RGB")
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(240, 240))
            self.qr_label.configure(image=ctk_img)
            self.qr_label.image = ctk_img  # Prevent GC garbage disposal
        except Exception as exc:
            self.qr_label.configure(text=f"[QR Error: {exc}]", image=None)

    # --- TAB 2: LOG CONSOLE ---
    def _build_tab_logs(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        # Header Control bar
        ctrl_bar = ctk.CTkFrame(frame, fg_color="transparent")
        ctrl_bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ctrl_bar.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            ctrl_bar,
            text="Live Synchronization Diagnostic Feed",
            font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w"
        )
        title.grid(row=0, column=0, sticky="w")

        chk_scroll = ctk.CTkCheckBox(
            ctrl_bar,
            text="Auto-Scroll Feed",
            variable=self.auto_scroll_logs,
            text_color="#CCCCCC"
        )
        chk_scroll.grid(row=0, column=1, padx=16)

        btn_clear = ctk.CTkButton(
            ctrl_bar,
            text="🗑 Clear Console",
            width=130,
            fg_color="#37474F",
            hover_color="#263238",
            command=self.clear_logs
        )
        btn_clear.grid(row=0, column=2)

        # Log Text Box
        self.log_textbox = ctk.CTkTextbox(
            frame,
            font=ctk.CTkFont(family="Consolas", size=13),
            fg_color="#0B0B11",
            text_color="#00FF66",
            corner_radius=12,
            wrap="word"
        )
        self.log_textbox.grid(row=1, column=0, sticky="nsew")
        self.log_textbox.insert("0.0", "=== ClipBoardSync Application Engine Initialized ===\n")
        self.log_textbox.configure(state="disabled")
        return frame

    # --- TAB 3: RECENT SYNC HISTORY ---
    def _build_tab_history(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        # Header bar
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            header,
            text="Recent Synced Clipboard Items",
            font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w"
        )
        title.grid(row=0, column=0, sticky="w")

        self.btn_refresh_hist = ctk.CTkButton(
            header,
            text="🔄 Refresh List",
            width=120,
            fg_color="#3949AB",
            hover_color="#303F9F",
            command=self._refresh_history_view
        )
        self.btn_refresh_hist.grid(row=0, column=1)

        # Scrollable History Feed
        self.history_scroll = ctk.CTkScrollableFrame(frame, fg_color="#181824", corner_radius=12)
        self.history_scroll.grid(row=1, column=0, sticky="nsew")
        self.history_scroll.grid_columnconfigure(0, weight=1)

        self.history_empty_label = ctk.CTkLabel(
            self.history_scroll,
            text="No clipboard items synchronized yet.\nCopy some text on any connected device to see it appear here instantly!",
            font=ctk.CTkFont(size=15),
            text_color="#7B7E98"
        )
        self.history_empty_label.grid(row=0, column=0, pady=60)
        return frame

    def _refresh_history_view(self) -> None:
        """Render recent clipboard items as aesthetic cards."""
        for child in self.history_scroll.winfo_children():
            child.destroy()

        items = sync_hub.get_history()
        if not items:
            self.history_empty_label = ctk.CTkLabel(
                self.history_scroll,
                text="No clipboard items synchronized yet.\nCopy text, screenshots, or files on any connected device!",
                font=ctk.CTkFont(size=15),
                text_color="#7B7E98"
            )
            self.history_empty_label.grid(row=0, column=0, pady=60)
            return

        for idx, item in enumerate(reversed(items)):
            card = ctk.CTkFrame(self.history_scroll, fg_color="#232334", corner_radius=10)
            card.grid(row=idx, column=0, sticky="ew", padx=12, pady=8)
            card.grid_columnconfigure(0, weight=1)

            dev_id = item.get("device_id", "Unknown Device")
            t_str = item.get("timestamp", "")
            if t_str and "T" in t_str:
                t_str = t_str.split("T")[1][:8]

            item_type = item.get("type", "text")
            type_icon = "📷 Image" if item_type == "image" else ("📁 File" if item_type == "file" else "📝 Text")
            title_str = f" From: {dev_id}  |  [{type_icon}]  |  ⏰ {t_str}"
            top_lbl = ctk.CTkLabel(card, text=title_str, font=ctk.CTkFont(size=12, weight="bold"), text_color="#00E5FF", anchor="w")
            top_lbl.grid(row=0, column=0, padx=16, pady=(12, 4), sticky="w")

            content_text = str(item.get("content", ""))
            file_url = item.get("file_url")

            if item_type == "image":
                # Render Image thumbnail if base64 URI
                try:
                    if content_text.startswith("data:image/"):
                        b64_part = content_text.split(",", 1)[1]
                        raw_img = base64.b64decode(b64_part)
                        pil_img = Image.open(io.BytesIO(raw_img))
                        pil_img.thumbnail((200, 130))
                        ctk_thumb = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=pil_img.size)
                        img_lbl = ctk.CTkLabel(card, image=ctk_thumb, text="")
                        img_lbl.grid(row=1, column=0, padx=16, pady=(4, 12), sticky="w")
                    elif file_url:
                        lbl_file = ctk.CTkLabel(card, text=f"📷 Image File: {item.get('filename', 'Image')}", font=ctk.CTkFont(size=14, weight="bold"), text_color="#EEEEEE")
                        lbl_file.grid(row=1, column=0, padx=16, pady=(4, 12), sticky="w")
                except Exception:
                    lbl_file = ctk.CTkLabel(card, text="📷 [Image Data]", font=ctk.CTkFont(size=14), text_color="#EEEEEE")
                    lbl_file.grid(row=1, column=0, padx=16, pady=(4, 12), sticky="w")

                btn_copy = ctk.CTkButton(
                    card,
                    text="📋 Copy Image",
                    width=130,
                    height=32,
                    fg_color="#7B1FA2",
                    hover_color="#6A1B9A",
                    command=lambda c=content_text: self.copy_image_to_local(c)
                )
                btn_copy.grid(row=0, column=1, rowspan=2, padx=16, pady=12)

            elif item_type == "file":
                fname = item.get("filename", "File")
                fsize = item.get("filesize", 0)
                fsize_str = f"{fsize / 1024:.1f} KB" if fsize < 1024 * 1024 else f"{fsize / (1024*1024):.1f} MB"
                file_info = f"📁 {fname} ({fsize_str})"

                lbl_file = ctk.CTkLabel(card, text=file_info, font=ctk.CTkFont(size=14, weight="bold"), text_color="#81D4FA", anchor="w")
                lbl_file.grid(row=1, column=0, padx=16, pady=(4, 12), sticky="w")

                if file_url:
                    full_file_url = f"{self.mobile_url}{file_url}" if not file_url.startswith("http") else file_url
                    btn_open = ctk.CTkButton(
                        card,
                        text="🌐 Download File",
                        width=130,
                        height=32,
                        fg_color="#1565C0",
                        hover_color="#0D47A1",
                        command=lambda u=full_file_url: webbrowser.open(u)
                    )
                    btn_open.grid(row=0, column=1, rowspan=2, padx=16, pady=12)

            else:
                preview = content_text if len(content_text) <= 150 else content_text[:150] + "..."
                txt_lbl = ctk.CTkLabel(card, text=preview, font=ctk.CTkFont(size=14), text_color="#EEEEEE", justify="left", anchor="w", wraplength=560)
                txt_lbl.grid(row=1, column=0, padx=16, pady=(0, 12), sticky="w")

                btn_copy = ctk.CTkButton(
                    card,
                    text="📋 Copy Text",
                    width=130,
                    height=32,
                    fg_color="#00897B",
                    hover_color="#00695C",
                    command=lambda c=content_text: self.copy_clip_to_local(c)
                )
                btn_copy.grid(row=0, column=1, rowspan=2, padx=16, pady=12)

    def send_image_file(self) -> None:
        """Pick an image file from disk and broadcast across local Wi-Fi bridge."""
        filepath = filedialog.askopenfilename(
            title="Select Image to Send",
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

            # Write to local Windows clipboard
            self.copy_image_to_local(data_uri)

            # Broadcast via sync_hub
            from server.models import ClipboardItem, get_utc_now_iso
            item = ClipboardItem(
                device_id="Desktop-GUI",
                timestamp=get_utc_now_iso(),
                type="image",
                content=data_uri,
                filename=path.name,
                filesize=len(raw)
            )
            if self.engine._loop and self.engine._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    sync_hub.handle_message(None, item.to_message_dict()),
                    self.engine._loop
                )
            self.log_queue.put(f"[ACTION] Sent image '{path.name}' ({len(raw)} bytes) across LAN bridge.")
            self._refresh_history_view()
        except Exception as exc:
            self.log_queue.put(f"[ERROR] Failed to send image file: {exc}")

    def send_any_file(self) -> None:
        """Pick any file from disk, upload to server, and broadcast across local Wi-Fi bridge."""
        filepath = filedialog.askopenfilename(title="Select File to Send")
        if not filepath:
            return
        try:
            path = Path(filepath)
            import shutil
            import uuid
            from server.main import UPLOADS_DIR
            safe_name = f"{uuid.uuid4().hex[:10]}{path.suffix}"
            dest = UPLOADS_DIR / safe_name
            shutil.copy2(path, dest)

            file_url = f"/uploads/{safe_name}"
            from server.models import ClipboardItem, get_utc_now_iso
            item = ClipboardItem(
                device_id="Desktop-GUI",
                timestamp=get_utc_now_iso(),
                type="file",
                content=f"File: {path.name}",
                filename=path.name,
                filesize=path.stat().st_size,
                file_url=file_url
            )
            if self.engine._loop and self.engine._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    sync_hub.handle_message(None, item.to_message_dict()),
                    self.engine._loop
                )
            self.log_queue.put(f"[ACTION] Sent file '{path.name}' ({path.stat().st_size} bytes) across LAN bridge.")
            self._refresh_history_view()
        except Exception as exc:
            self.log_queue.put(f"[ERROR] Failed to send file: {exc}")

    def copy_image_to_local(self, data_uri: str) -> None:
        """Write base64 image data to Windows clipboard."""
        try:
            if "," in data_uri:
                b64_str = data_uri.split(",", 1)[1]
            else:
                b64_str = data_uri
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
            self.log_queue.put("[ACTION] Image copied directly to Windows clipboard (ready for Ctrl+V).")
        except Exception as exc:
            self.log_queue.put(f"[ERROR] Failed to set image to Windows clipboard: {exc}")

    def copy_clip_to_local(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.log_queue.put(f"[ACTION] Copied history item ({len(text)} chars) to Windows Desktop clipboard.")

    # --- TAB 4: USER GUIDE ---
    def _build_tab_guide(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.main_container, fg_color="#181824", corner_radius=16)
        frame.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            frame,
            text="ClipBoardSync User Guide & FAQ",
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w"
        )
        title.grid(row=0, column=0, padx=32, pady=(28, 16), sticky="w")

        guide_text = (
            "Welcome to ClipBoardSync! Here is everything you need to know:\n\n"
            "1. How Does This Work?\n"
            "   ClipBoardSync establishes a secure local WebSocket server on your Windows PC.\n"
            "   When you open the web portal on your smartphone (via QR code), both devices communicate\n"
            "   directly across your home Wi-Fi network. No external cloud servers are involved!\n\n"
            "2. Why Won't My Smartphone Load the Web Page?\n"
            "   - Check that your PC and smartphone are connected to the EXACT same Wi-Fi router.\n"
            "   - If your PC has Windows Firewall enabled, it might block inbound port 8000 connections.\n"
            "     Allow Python or ClipBoardSync when Windows prompts for local network access.\n\n"
            "3. Does My Clipboard Sync Both Ways?\n"
            "   Yes! Copying text on your Windows laptop will immediately populate on your phone dashboard,\n"
            "   and copying text from your phone dashboard will instantly load into your Windows clipboard!\n\n"
            "4. Public & Offline Use:\n"
            "   Because ClipBoardSync runs entirely locally, it works smoothly even on offline wireless routers\n"
            "   and portable mobile hotspots!"
        )
        body = ctk.CTkLabel(
            frame,
            text=guide_text,
            font=ctk.CTkFont(size=14),
            text_color="#CCCCCC",
            justify="left",
            anchor="w"
        )
        body.grid(row=1, column=0, padx=32, pady=(0, 24), sticky="w")
        return frame

    # --- ENGINE & AUTOMATION LOOPS ---
    def _start_automation(self) -> None:
        """Start non-blocking UI background checks and auto-boot synchronization engine."""
        self.after(100, self._process_log_queue)
        self.after(2000, self._poll_hub_status)
        # Proactively start synchronization bridge upon launch
        self.toggle_engine()

    def _process_log_queue(self) -> None:
        """Flush accumulated thread logs into the console display safely."""
        messages: list[str] = []
        try:
            while True:
                messages.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass

        if messages:
            self.log_textbox.configure(state="normal")
            for msg in messages:
                self.log_textbox.insert("end", msg.strip() + "\n")
            if self.auto_scroll_logs.get():
                self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")

        self.after(150, self._process_log_queue)

    def _poll_hub_status(self) -> None:
        """Update connection counts and status indicators automatically."""
        if self.engine.is_running:
            cnt = sync_hub.connection_count
            self.conn_count_label.configure(text=f"📡 Active Connected Devices: {cnt}")
            self.status_badge.configure(text="  🟢 ONLINE (PORT 8000)  ", fg_color="#2E7D32")
            self.toggle_engine_btn.configure(text="⏹ STOP BRIDGE", fg_color="#C62828", hover_color="#B71C1C")
        else:
            self.conn_count_label.configure(text="📡 Active Connected Devices: 0")
            self.status_badge.configure(text="  🔴 OFFLINE  ", fg_color="#B71C1C")
            self.toggle_engine_btn.configure(text="▶ START BRIDGE", fg_color="#00C853", hover_color="#00B248")

        self.after(2000, self._poll_hub_status)

    def toggle_engine(self) -> None:
        """Start or stop the backend synchronization bridge."""
        if not self.engine.is_running:
            self.engine.start(self.log_queue)
            self.status_badge.configure(text="  🟡 STARTING...  ", fg_color="#F57F17")
        else:
            self.engine.stop()
            self.status_badge.configure(text="  🔴 OFFLINE  ", fg_color="#B71C1C")

    def copy_mobile_url(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.mobile_url)
        self.update()
        self.log_queue.put(f"[ACTION] Copied mobile pairing URL to desktop clipboard: {self.mobile_url}")

    def clear_logs(self) -> None:
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("0.0", "end")
        self.log_textbox.insert("0.0", "=== Log Console Cleared ===\n")
        self.log_textbox.configure(state="disabled")

    def on_close_request(self) -> None:
        """Handle window termination cleanly."""
        if self.engine.is_running:
            self.engine.stop()
        sys.stdout = self.original_stdout
        self.destroy()
        sys.exit(0)
