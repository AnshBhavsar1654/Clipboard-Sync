"""Graphical desktop launcher entry point for ClipBoardSync."""

from __future__ import annotations

import logging
import sys
from gui.app import ClipBoardSyncGUI


def main() -> None:
    """Initialize and run the ClipBoardSync CustomTkinter application window."""
    if sys.platform != "win32":
        print("Note: Windows Desktop GUI is designed natively for win32 clipboard integration.", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    app = ClipBoardSyncGUI()
    try:
        app.mainloop()
    except KeyboardInterrupt:
        print("Application terminated by keyboard signal.")
    except Exception as exc:
        print(f"Error executing application loop: {exc}")


if __name__ == "__main__":
    main()
