"""Automated build script to package ClipBoardSync into a standalone Windows Executable (.exe)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def _ensure_icon(root_dir: Path) -> Path | None:
    """Regenerate the Windows .ico from assets/icon.png so every build uses the latest logo."""
    assets = root_dir / "assets"
    png_file = assets / "icon.png"
    ico_file = assets / "clipboardsync.ico"

    if png_file.exists():
        stale = not ico_file.exists() or png_file.stat().st_mtime > ico_file.stat().st_mtime
        if stale:
            try:
                from PIL import Image
                icon = Image.open(png_file).convert("RGBA")
                icon.save(
                    ico_file,
                    format="ICO",
                    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
                )
                print(f"Created app icon: {ico_file}")
            except Exception as exc:
                print(f"Could not create app icon: {exc}")

    return ico_file if ico_file.exists() else None


def main() -> None:
    """Run PyInstaller to compile ClipBoardSync Desktop Application."""
    print("Building ClipBoardSync...")
    print("Creating the standalone app file you can double-click to start sharing.")
    print("-" * 64)

    root_dir = Path(__file__).parent.resolve()
    os.chdir(root_dir)

    # Terminate running ClipBoardSync.exe instances to release file locks on Windows
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "ClipBoardSync.exe"], capture_output=True)

    # Clean old build/dist artifacts if present
    for folder in ("build", "dist", "__pycache__"):
        path = root_dir / folder
        if path.exists():
            print(f"Cleaning: {path}")
            shutil.rmtree(path, ignore_errors=True)

    spec_file = root_dir / "ClipBoardSync.spec"
    if spec_file.exists():
        spec_file.unlink()

    # Determine OS path separator for PyInstaller --add-data (semicolon on Windows, colon on Linux/macOS)
    sep = ";" if sys.platform == "win32" else ":"

    # Application icon (regenerated from icon.png, embedded in the exe and
    # bundled for the runtime window/taskbar icon)
    icon_file = _ensure_icon(root_dir)
    icon_arg = f"--icon={icon_file}" if icon_file else "--icon="
    asset_arg = f"--add-data=assets{sep}assets"

    # Build command arguments
    pyinstaller_args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name", "ClipBoardSync",
        "--onefile",
        "--windowed",
        "--noconsole",
        f"--add-data=server/static{sep}server/static",
        asset_arg,
        icon_arg,
        "--collect-data", "customtkinter",
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.protocols.websockets.auto",
        "--hidden-import=uvicorn.lifespan.on",
        "--hidden-import=websockets",
        "--hidden-import=win32clipboard",
        "--hidden-import=win32con",
        "--clean",
        "run_gui.py"
    ]

    print(f"Running: {' '.join(pyinstaller_args)}\n")

    result = subprocess.run(pyinstaller_args)
    if result.returncode == 0:
        exe_path = root_dir / "dist" / ("ClipBoardSync.exe" if sys.platform == "win32" else "ClipBoardSync")
        print("")
        print("Build complete.")
        print(f"Your app is ready: {exe_path}")
        print("Double-click it to start sharing. No Python setup needed.")
    else:
        print("")
        print("Build did not complete. Check that Python, PyInstaller, and project dependencies are installed, then try again.")
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
