"""System tray icon + menu. Runs on its own thread."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _load_icon():
    from pathlib import Path

    from PIL import Image

    candidates = [
        Path(__file__).parent.parent / "assets" / "icon.png",
        Path(__file__).parent.parent / "assets" / "clipboardsync.ico",
    ]
    for path in candidates:
        if path.is_file():
            try:
                img = Image.open(path).convert("RGBA")
                img.thumbnail((64, 64))
                return img
            except Exception:
                continue
    return Image.new("RGBA", (64, 64), (99, 102, 241, 255))


def build_menu(callbacks: dict[str, Callable[[], None]]):
    import pystray

    def _item(label: str, key: str):
        return pystray.MenuItem(label, lambda icon, item: callbacks[key]())

    return pystray.Menu(
        _item("Open ClipBoardSync", "open"),
        _item("Connect phone…", "connect"),
        pystray.Menu.SEPARATOR,
        _item("Pause sharing", "pause"),
        _item("Resume sharing", "resume"),
        pystray.Menu.SEPARATOR,
        _item("Quit", "quit"),
    )


def run_tray(callbacks: dict[str, Callable[[], None]]):
    """Create the icon; the caller runs it (blocking) or detached."""
    import pystray

    icon = pystray.Icon("ClipBoardSync", _load_icon(), "ClipBoardSync", build_menu(callbacks))
    return icon


def run_tray_detached(callbacks: dict[str, Callable[[], None]]):
    icon = run_tray(callbacks)
    icon.run_detached()
    return icon


def stop_tray(icon: Any) -> None:
    try:
        icon.stop()
    except Exception as exc:
        logger.debug("Tray stop failed: %s", exc)
