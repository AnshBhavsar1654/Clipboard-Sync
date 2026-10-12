"""Headless launcher: sharing server + clipboard client in a terminal, no window."""

from __future__ import annotations

import logging
import sys

import qrcode

from server.auth import get_store
from server.net import primary_lan_ip

logger = logging.getLogger("clipboardsync_runner")


def print_pairing_instructions(port: int) -> None:
    """Render connection details and steps for sharing with a phone."""
    ip = primary_lan_ip()
    mobile_url = f"http://{ip}:{port}"
    store = get_store()
    pin = store.pairing_pin if store.require_pin else "disabled"
    qr_url = f"{mobile_url}/?pair={pin}" if store.require_pin else mobile_url

    print("")
    print("ClipBoardSync is running")
    print("-" * 62)
    print(f"Address:      {mobile_url}")
    if store.require_pin:
        print(f"Pairing code: {pin} (single-use: scan the code below, or type it once)")
    print("")
    print("How to connect:")
    print("1. Join the same Wi-Fi on your computer and phone.")
    print("2. Open the address above on your phone, or scan this code:")
    print("-" * 62)

    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=2,
        )
        qr.add_data(qr_url)
        qr.make(fit=True)
        # Print ASCII representation suitable for standard console screens
        qr.print_ascii(invert=True)
    except Exception as exc:
        logger.debug("Could not render QR code in terminal: %s", exc)
        print(f"Open {mobile_url} directly in your phone browser.")

    print("-" * 62)
    print("Copy on either device to share it.")
    print("Press Ctrl+C to stop sharing.")
    print("")


def run_until_interrupted(port: int) -> None:
    """Share on ``port`` until Ctrl+C, using the same engine as the desktop app."""
    import queue
    import time

    from gui.engine import BackgroundEngine
    from server.net import find_open_port

    port = find_open_port(port)
    print_pairing_instructions(port)
    engine = BackgroundEngine(port=port)
    engine.start(queue.Queue())
    try:
        while engine.is_running:
            time.sleep(0.5)
    finally:
        engine.stop()


def main(argv: list[str] | None = None) -> None:
    """CLI launcher entrypoint."""
    import argparse

    parser = argparse.ArgumentParser(description="Run ClipBoardSync (server + desktop client).")
    parser.add_argument("--port", type=int, default=8000, help="port to share on (default: 8000)")
    parser.add_argument("--headless", action="store_true",
                        help="run without any window (used by the built exe smoke test)")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("ClipBoardSync works best on Windows for clipboard sharing.", file=sys.stderr)

    from server.net import ensure_single_instance

    if not ensure_single_instance():
        print("ClipBoardSync is already running. Use the open window.", file=sys.stderr)
        sys.exit(0)

    try:
        run_until_interrupted(args.port)
    except KeyboardInterrupt:
        print("\nStopping sharing. Goodbye!")


if __name__ == "__main__":
    main()
