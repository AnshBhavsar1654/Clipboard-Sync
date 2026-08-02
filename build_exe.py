"""Automated build script to package ClipBoardSync into a standalone Windows Executable (.exe)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


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
