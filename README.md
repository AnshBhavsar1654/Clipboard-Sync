# ClipBoardSync

**Share your clipboard between a Windows PC and any phone over your Wi-Fi.** Share text, photos, and files — no cloud, no accounts, no phone apps.

```
[ This computer ]  ⇄  [ Your phone ]
  sharing app          same Wi-Fi      browser + QR code
```

---

## Features

- **Easy phone connection** — scan the code with your phone camera to open ClipBoardSync in any mobile browser.
- **Works both ways** — copy on the computer and it appears on the phone; share from the phone and it is ready to paste on the computer (`Ctrl+V`).
- **Text, photos, and files** — share any of them in either direction.
- **Searchable, pinnable history** — the desktop app keeps a clean feed with live search (`Ctrl+K`), pinning, and a dedicated Files view.
- **100% local & private** — everything stays on your Wi-Fi; works fully offline.
- **Simple desktop app** — light and dark themes, QR code connection, phone counter, and a clear activity log.
- **One-click app** — a single `ClipBoardSync.exe` anyone can double-click.

---

## Quick Start (end users)

1. Download `ClipBoardSync.exe` from the Releases page.
2. Double-click to launch — sharing starts automatically.
3. Join the **same Wi-Fi** on your computer and phone.
4. Point your phone camera at the code in the app and open the link.
5. Enter the 6-digit code once. Then copy on either device to share it.

---

## Developer Guide

### Prerequisites

- Windows 10/11 (native clipboard hooks are Win32-only; the server is platform-independent)
- Python 3.13+

### Setup & run

```bash
uv sync                  # or: pip install -r requirements.txt

python run_gui.py        # desktop GUI dashboard
python run_app.py        # headless terminal launcher
```

### Build the standalone executable

```bash
python build_exe.py
```

Output: `dist/ClipBoardSync.exe` — includes the server, clipboard monitor, GUI, web frontend, and the application icon (`assets/clipboardsync.ico`, generated from `assets/icon.png`).

---

## Architecture

| Layer | Technology |
|-------|------------|
| Sync engine | FastAPI + Uvicorn, WebSocket (`/ws`), REST history (`/api/history`) & upload (`/api/upload`) |
| Windows client | Native Win32 clipboard hooks (pywin32) wired into asyncio |
| Desktop GUI | CustomTkinter, QR via `qrcode` + Pillow |
| Mobile frontend | Vanilla HTML/CSS/JS, native WebSocket + Clipboard APIs |

### Project structure

```
ClipBoardSync/
├── gui/                  # Desktop GUI (CustomTkinter)
├── server/               # FastAPI app, sync hub, web frontend
│   └── static/           # Mobile dashboard (HTML/CSS/JS)
├── client/               # Clipboard monitor + WebSocket client
├── assets/               # Application icon
├── run_gui.py            # GUI entry point
├── run_app.py            # Headless entry point
├── build_exe.py          # PyInstaller build script
└── DESIGN.md             # Design system & UI guidelines
```

### Design

The interface follows `DESIGN.md`: a neutral palette with a single indigo accent, vector icons, restrained radius, clean list of shared clips, and keyboard-first search. Use plain `Sharing` language — never bridge, engine, or backend in user-facing text. Any UI changes must respect it.

---

## Troubleshooting

- **Phone can't open the link** — make sure the computer and phone are on the same Wi-Fi and allow ClipBoardSync through Windows Firewall for private networks.
- **Do I need internet?** No. Sharing runs on your Wi-Fi only and works offline.