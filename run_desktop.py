"""ClipBoardSync desktop app: sharing engine + window + tray + quick-paste hotkey.

This is the entry point the installer/exe launches (``run_gui.py`` forwards
here). The window is the web UI under ``server/static/desktop`` shown in an
Edge WebView2 frame.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ClipBoardSync desktop app")
    parser.add_argument("--port", type=int, default=8000, help="port to share on (default: 8000)")
    parser.add_argument("--minimized", action="store_true",
                        help="start in the tray without opening the window")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("clipboardsync-desktop")

    from server import admin
    from server.discovery import PeerDiscovery
    from server.net import ensure_single_instance, find_open_port

    if not ensure_single_instance("ClipBoardSyncDesktop"):
        print("ClipBoardSync is already running. Use the open window.", file=sys.stderr)
        sys.exit(0)

    from gui.engine import BackgroundEngine
    from gui.tray import run_tray_detached, stop_tray
    from server.main import hub

    port = find_open_port(args.port)
    if port != args.port:
        print(f"Port {args.port} is in use — sharing on port {port} instead.")

    engine = BackgroundEngine(port=port)
    log_queue: queue.Queue[str] = queue.Queue()
    admin.configure(port)
    admin.set_identity(engine.device_id)
    admin.set_settings_listener(engine.apply_settings)

    # "Pause sharing" only stops clips flowing. The server keeps running
    # because it also serves this window.
    def _set_paused(paused: bool) -> None:
        hub.paused = paused

    discovery = PeerDiscovery(port=port)
    discovery.start()
    admin.set_peer_sources(lambda: engine.peer, discovery.peers)
    admin.set_engine_controller(
        lambda: _set_paused(False), lambda: _set_paused(True), lambda: not hub.paused
    )
    engine.start(log_queue)

    import webview
    from webview.dom import DOMEventHandler

    from gui.hotkey import last_hotkey_hwnd, start_hotkey
    from gui.shell import run_window

    token = admin.admin_token()
    holders: dict = {}
    state = {"quitting": False, "told_tray": False}

    win, api = run_window(port, engine, token, holders=holders, block=False, hidden=args.minimized)
    holders["main"] = win

    def _eval(js: str) -> None:
        try:
            win.evaluate_js(js)
        except Exception as exc:
            logger.debug("evaluate_js failed: %s", exc)

    def _hide_to_tray() -> None:
        try:
            win.hide()
        except Exception:
            pass
        if not state["told_tray"]:
            state["told_tray"] = True
            _eval("toast('Still sharing from the tray. Right-click the tray icon to quit.')")

    def on_closing():
        # Closing the window keeps sharing alive in the tray; Quit really exits.
        # This handler runs while the UI thread is blocked on us, so anything
        # that talks to the window must happen on another thread, after we return.
        if state["quitting"]:
            return True
        threading.Timer(0.05, _hide_to_tray).start()
        return False

    try:
        win.events.closing += on_closing
    except Exception as exc:
        logger.debug("Could not hook window close: %s", exc)

    def on_loaded() -> None:
        """Real file paths for drag-and-drop (browsers hide them)."""
        def _on_drop(event: dict) -> None:
            try:
                files = (event.get("dataTransfer") or {}).get("files") or []
                paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
                if paths:
                    _eval(f"sharePaths({json.dumps(paths)})")
            except Exception as exc:
                logger.debug("Drop handler failed: %s", exc)

        try:
            win.dom.document.events.drop += DOMEventHandler(_on_drop, True, True)
        except Exception as exc:
            logger.debug("Drag-and-drop paths unavailable: %s", exc)

    try:
        win.events.loaded += on_loaded
    except Exception as exc:
        logger.debug("Could not hook window load: %s", exc)

    def open_quick_paste() -> None:
        holders["last_hwnd"] = last_hotkey_hwnd()
        existing = holders.get("quick")
        try:
            if existing is not None:
                existing.show()
                existing.focus()
                return
        except Exception:
            holders.pop("quick", None)
        try:
            quick = webview.create_window(
                "ClipBoardSync quick paste",
                f"http://127.0.0.1:{port}/desktop/?quick=1&t={token}",
                width=560, height=430, frameless=True, focused=True,
                js_api=api, text_select=True,
            )
            try:
                quick.events.closed += lambda: holders.pop("quick", None)
            except Exception:
                pass
            holders["quick"] = quick
        except Exception as exc:
            logger.warning("Quick paste window failed: %s", exc)

    hotkey = start_hotkey(open_quick_paste)

    def tray_open() -> None:
        try:
            win.show()
        except Exception as exc:
            logger.debug("Window show failed: %s", exc)

    def tray_connect() -> None:
        tray_open()
        _eval("openConnect()")

    def tray_pause() -> None:
        _set_paused(True)
        _eval("refreshState()")

    def tray_resume() -> None:
        _set_paused(False)
        _eval("refreshState()")

    def tray_quit() -> None:
        state["quitting"] = True
        try:
            win.destroy()
        except Exception:
            pass

    tray = run_tray_detached(
        {"open": tray_open, "connect": tray_connect, "pause": tray_pause,
         "resume": tray_resume, "quit": tray_quit}
    )

    try:
        webview.start(gui="edgechromium")
    finally:
        try:
            if hotkey is not None:
                hotkey.stop()
        finally:
            stop_tray(tray)
            discovery.stop()
            engine.stop()


if __name__ == "__main__":
    main()
