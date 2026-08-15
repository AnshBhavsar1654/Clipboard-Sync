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
                print(f"[*] Generated application icon: {ico_file}")
            except Exception as exc:
                print(f"[!] Could not generate icon from icon.png: {exc}")

    return ico_file if ico_file.exists() else None


def main() -> None:
    """Run PyInstaller to compile ClipBoardSync Desktop Application."""
    print("=" * 64)
    print("   [+] BUILDING CLIPBOARDSYNC STANDALONE EXECUTABLE (.EXE) [+]")
    print("=" * 64)

    root_dir = Path(__file__).parent.resolve()
    os.chdir(root_dir)

    # Terminate running ClipBoardSync.exe instances to release file locks on Windows
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "ClipBoardSync.exe"], capture_output=True)

    # Clean old build/dist artifacts if present
    for folder in ("build", "dist", "__pycache__"):
        path = root_dir / folder
        if path.exists():
            print(f"[*] Removing legacy directory: {path}")
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

    print(f"[*] Executing PyInstaller command:\n  {' '.join(pyinstaller_args)}\n")

    result = subprocess.run(pyinstaller_args)
    if result.returncode == 0:
        exe_path = root_dir / "dist" / ("ClipBoardSync.exe" if sys.platform == "win32" else "ClipBoardSync")
        print("\n" + "=" * 64)
        print("   [SUCCESS] BUILD SUCCESSFUL! STANDALONE EXECUTABLE CREATED:")
        print(f"   [Location]: {exe_path}")
        print("=" * 64)
        print("You can now share this compiled executable publicly for anyone to run without installing Python!")
    else:
        print("\n[!] Build failed. Please verify all dependencies (pyinstaller, customtkinter) are properly installed.")
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
