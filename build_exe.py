"""Package ClipBoardSync into a standalone Windows executable (plan §5.1).

Default: --onedir GUI build wrapped by installer.iss (fast start, fewer AV
false positives). Variants:
  python build_exe.py --mode onefile --target gui       # portable single exe
  python build_exe.py --target headless                 # console server+client
                                                        # (used by the smoke test)
The generated .spec is kept for review; dist/, build/ and *.spec are
git-ignored — releases ship via GitHub Release assets, not git.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


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


def build(mode: str, target: str) -> int:
    print("Building ClipBoardSync...")
    print("Creating the standalone app file you can double-click to start sharing.")
    print("-" * 64)

    root_dir = Path(__file__).parent.resolve()
    os.chdir(root_dir)

    # Terminate running instances to release file locks on Windows
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "ClipBoardSync.exe"], capture_output=True)

    # Clean old build/dist artifacts if present
    for folder in ("build", "dist", "__pycache__"):
        path = root_dir / folder
        if path.exists():
            print(f"Cleaning: {path}")
            shutil.rmtree(path, ignore_errors=True)

    # Determine OS path separator for PyInstaller --add-data (semicolon on Windows, colon on Linux/macOS)
    sep = ";" if sys.platform == "win32" else ":"

    # Application icon (regenerated from icon.png, embedded in the exe and
    # bundled for the runtime window/taskbar icon)
    icon_file = _ensure_icon(root_dir)
    icon_arg = f"--icon={icon_file}" if icon_file else "--icon="
    asset_arg = f"--add-data=assets{sep}assets"

    entry = "run_gui.py" if target == "gui" else "run_app.py"
    name = "ClipBoardSync" if target == "gui" else "ClipBoardSyncHeadless"

    # Build command arguments
    pyinstaller_args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name", name,
        "--onedir" if mode == "onedir" else "--onefile",
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
        "--hidden-import=websockets.asyncio.client",
        "--hidden-import=win32clipboard",
        "--hidden-import=win32con",
        "--hidden-import=win32timezone",
        "--hidden-import=multipart",
        "--clean",
        "--noconfirm",
        entry,
    ]
    if target == "gui":
        pyinstaller_args[4:4] = ["--windowed", "--noconsole"]

    print(f"Running: {' '.join(pyinstaller_args)}\n")

    result = subprocess.run(pyinstaller_args)
    if result.returncode == 0:
        from server import __version__

        if mode == "onedir":
            exe_path = root_dir / "dist" / name / (f"{name}.exe" if sys.platform == "win32" else name)
        else:
            exe_path = root_dir / "dist" / (f"{name}.exe" if sys.platform == "win32" else name)
        print("")
        print(f"Build complete (version {__version__}).")
        print(f"Your app is ready: {exe_path}")
        if target == "gui" and mode == "onedir":
            print("Next: build the installer with Inno Setup using installer.iss.")
        else:
            print("Double-click it to start sharing. No Python setup needed.")
    else:
        print("")
        print("Build did not complete. Check Python, PyInstaller, and dependencies,")
        print("then try again.")
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the ClipBoardSync executable.")
    parser.add_argument("--mode", choices=("onedir", "onefile"), default="onedir",
                        help="onedir: fast start for the installer (default); onefile: portable exe")
    parser.add_argument("--target", choices=("gui", "headless"), default="gui",
                        help="gui: desktop window; headless: console server+client for smoke tests")
    args = parser.parse_args()
    sys.exit(build(args.mode, args.target))


if __name__ == "__main__":
    main()
