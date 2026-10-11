"""New desktop shell entry: engine + WebView2 window + tray + hotkey (plan §4).

`run_gui.py` (CustomTkinter) stays the default until this shell passes the
Phase 4 acceptance list; then it replaces it.
"""

from __future__ import annotations

import logging
import queue
import sys


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("clipboardsync-desktop")

    from server import admin
    from server.net import ensure_single_instance, find_open_port

    if not ensure_single_instance("ClipBoardSyncDesktop"):
        print("ClipBoardSync is already running. Use the open window.", file=sys.stderr)
        sys.exit(0)

    from gui.engine import BackgroundEngine
    from gui.tray import run_tray_detached, stop_tray

    port = find_open_port(8000)
    if port != 8000:
        print(f"Port 8000 is in use — sharing on port {port} instead.")

    engine = BackgroundEngine(port=port)
    log_queue: queue.Queue[str] = queue.Queue()
    admin.configure(port)
    admin.set_engine_controller(
        lambda: engine.start(log_queue), engine.stop, lambda: engine.is_running
    )
    engine.start(log_queue)

    import webview

    from gui.hotkey import last_hotkey_hwnd, start_hotkey
    from gui.shell import run_window

    token = admin.admin_token()
    holders: dict = {}
    told_tray = {"once": False}

    win, api = run_window(port, engine, token, holders=holders, block=False)
    holders["main"] = win

    def _eval(js: str) -> None:
        try:
            win.evaluate_js(js)
        except Exception as exc:
            logger.debug("evaluate_js failed: %s", exc)

    def on_closing():
        # First close minimizes to tray instead of quitting (plan §4.5).
        if not told_tray["once"]:
            told_tray["once"] = True
            _eval("toast('Still sharing from the tray — right-click the icon to quit')")
        try:
            win.hide()
        except Exception:
            pass
        return False

    try:
        win.events.closing += on_closing
    except Exception as exc:
        logger.debug("Could not hook window close: %s", exc)

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
                f"http://127.0.0.1:{port}/desktop?quick=1&t={token}",
                width=560, height=430, frameless=True, focused=True,
                js_api=api, text_select=True,
            )

            def _on_quick_closed():
                holders.pop("quick", None)

            try:
                quick.events.closed += _on_quick_closed
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
        engine.stop()
        _eval("toast('Sharing paused')")

    def tray_resume() -> None:
        if not engine.is_running:
            engine.start(log_queue)
        _eval("refreshState()")

    def tray_quit() -> None:
        try:
            engine.stop()
        finally:
            try:
                if hotkey is not None:
                    hotkey.stop()
            finally:
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
            engine.stop()


if __name__ == "__main__":
    main()
