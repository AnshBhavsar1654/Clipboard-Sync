"""All-in-one integrated launcher for ClipBoardSync server and Windows desktop client."""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from typing import Any

import qrcode
import uvicorn
from client.config import Config
from client.main import ClipBoardSyncApp, _install_signal_handlers
from server.main import app as fastapi_app

logger = logging.getLogger("clipboardsync_runner")


def get_local_lan_ip() -> str:
    """Determine the primary IPv4 local network LAN routing address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Does not transmit data over the Internet; discovers active networking interface IP
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except Exception:
        try:
            hostname = socket.gethostname()
            return str(socket.gethostbyname(hostname))
        except Exception:
            return "127.0.0.1"


def print_pairing_instructions(port: int) -> None:
    """Render terminal QR code and mobile connection guidance."""
    ip = get_local_lan_ip()
    mobile_url = f"http://{ip}:{port}"
    
    print("\n" + "=" * 62)
    print("    🚀 CLIPBOARDSYNC CROSS-DEVICE BRIDGE RUNNING 🚀")
    print("=" * 62)
    print(f"📱 MOBILE CONNECTION URL:  {mobile_url}")
    print("   Make sure your phone is connected to the same Wi-Fi network!")
    print("   Open your camera app and scan this QR Code to launch:")
    print("=" * 62)

    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=2,
        )
        qr.add_data(mobile_url)
        qr.make(fit=True)
        # Print ASCII representation suitable for standard console screens
        qr.print_ascii(invert=True)
    except Exception as exc:
        logger.debug("Could not render QR code in terminal: %s", exc)
        print(f" [!] Navigate directly to {mobile_url} on your mobile browser.")

    print("=" * 62 + "\n")


async def async_run_all(port: int = 8000) -> None:
    """Concurrently operate the FastAPI WebSocket engine and native Win32 client."""
    print_pairing_instructions(port)

    # Configure and start Uvicorn sync backend
    uv_config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)
    server_task = asyncio.create_task(server.serve(), name="uvicorn-backend")

    # Allow brief moment for TCP port socket binding
    await asyncio.sleep(0.4)

    # Initialize native Win32 clipboard monitoring loop
    client_config = Config()
    client_config.websocket_url = f"ws://127.0.0.1:{port}/ws"
    
    client_app = ClipBoardSyncApp(client_config)
    _install_signal_handlers(client_app)

    # Watch for server failure or client shutdown request
    client_task = asyncio.create_task(client_app.run(), name="win32-client")
    
    try:
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
    except asyncio.CancelledError:
        server.should_exit = True
        client_app.request_shutdown()


def main() -> None:
    """CLI launcher entrypoint."""
    if sys.platform != "win32":
        print("Note: Native clipboard client functions fully on Windows. Server is platform-independent.", file=sys.stderr)
    
    try:
        asyncio.run(async_run_all())
    except KeyboardInterrupt:
        print("\n[+] Shutting down ClipBoardSync seamlessly. Goodbye!")


if __name__ == "__main__":
    main()
