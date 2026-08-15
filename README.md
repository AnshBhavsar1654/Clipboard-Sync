# ClipBoardSync

**Real-time cross-device clipboard bridge over your local Wi-Fi.** Share text, images, and files between a Windows PC and any phone — no cloud, no accounts, no phone apps.

```
[ Windows PC ]  ⇄  [ Phone / Tablet ]
  clipboard +      WebSocket over     standard browser
  web hub (8000)   your Wi-Fi         dashboard + QR
```

---

## Features

- **Zero-setup phone pairing** — scan the QR code with your camera to open the live dashboard in any mobile browser.
- **Bidirectional sync** — copy on the PC and it appears on the phone; copy on the phone and it lands in your Windows clipboard (`Ctrl+V`).
- **Text, images, and files** — send any of them in either direction across the LAN.
- **Searchable, pinnable history** — the desktop app keeps a clean clipboard feed with live search (`Ctrl+K`), pinning, and a dedicated Files view.
- **100% local & private** — everything stays on your Wi-Fi router; works fully offline.
- **Polished desktop GUI** — dark CustomTkinter interface with QR pairing, connected-device counter, and a live activity log.
- **One-click executable** — a single self-contained `.exe` for non-technical users.

---

## Quick Start (end users)

1. Download `ClipBoardSync.exe` from the Releases page.
2. Double-click to launch — the bridge starts automatically.
3. Connect your phone and PC to the **same Wi-Fi network**.
4. Point your phone camera at the QR code and open the link.
5. Copy on either device and watch it sync.

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

The interface follows `DESIGN.md`: a neutral dark palette with a single indigo accent, vector icons (no emoji), restrained radius, clean list-based clipboard history, and keyboard-first search. Any UI changes must respect it.

---

## Troubleshooting

- **Phone can't open the QR link** — confirm both devices are on the same Wi-Fi subnet and allow ClipBoardSync through Windows Firewall for private networks (port 8000).
- **Requires internet?** No. The bridge runs entirely on local network routing.