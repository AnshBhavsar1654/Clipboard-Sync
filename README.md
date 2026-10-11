# ClipBoardSync

**Share your clipboard between a Windows PC and any phone over your Wi-Fi.** Share text, photos, and files — no cloud, no accounts, no phone apps.

```
[ This computer ]  ⇄  [ Your phone ]
  sharing app          same Wi-Fi      browser + QR code
```

---

## Features

- **Easy phone connection** — scan the code with your phone camera: no typing, the code pairs automatically. Typing the 6-digit code works too. Codes are single-use.
- **Works both ways** — copy on the computer and it appears on the phone; share from the phone and it lands on the computer ready to paste (`Ctrl+V`).
- **Text, photos, and files** — what lands where:
  | Direction | Text | Photo | File / folder |
  |---|---|---|---|
  | PC → phone | appears in feed, tap **Copy** | thumbnail + Save | name, size + Save |
  | Phone → PC | on clipboard (`Ctrl+V`) | on clipboard **and** saved | on clipboard as file (`Ctrl+V` in Explorer) **and** saved |
  > Browsers can't silently write your phone's clipboard over plain `http`, so PC → phone needs one tap on **Copy**. Incoming phone files over 50 MB aren't auto-downloaded (configurable in `prefs.json`).
- **Searchable, pinnable history** — feed with live search (`Ctrl+K`), pins, and a Files view. History and pins survive restarts (kept 200 items / 30 days). **Clear** on the phone hides that phone's view; deleting a clip removes it everywhere including its file.
- **Private by pairing** — new phones enter the code once, then stay paired via a server-issued token. History, uploads, and downloads all require pairing; wrong codes lock out after 5 tries. Password-manager clips marked do-not-record are never shared. Works fully offline (no internet requests at all).
- **Simple desktop app** — light and dark themes, QR code connection, a phone counter that counts phones (not the app itself).
- **One-click app** — install with `ClipBoardSync-Setup-*.exe`, or run the portable `ClipBoardSync.exe` from Releases.

---

## Quick Start (end users)

1. Install `ClipBoardSync-Setup-*.exe` from the Releases page (or run the portable exe).
2. Double-click to launch — sharing starts automatically.
3. Join the **same Wi-Fi** on your computer and phone.
4. Point your phone camera at the code in the app and open the link — no typing needed.
5. Copy on either device to share it.

---

## Developer Guide

### Prerequisites

- Windows 10/11 (native clipboard hooks are Win32-only; the server is platform-independent)
- Python 3.13+

### Setup & run

```bash
uv sync                  # or: pip install -r requirements.txt

python run_gui.py        # desktop GUI dashboard
python run_app.py        # headless terminal launcher (see --help for --port/--headless)
```

### Checks

```bash
pytest -q                                 # unit + integration suite
pytest tests/smoke_exe.py -q --exe=...    # needs a built headless exe (see below)
pytest tests/test_phone_web.py -v         # needs: pip install playwright && playwright install chromium
ruff check client server/auth.py server/filenames.py server/media.py server/net.py server/store.py tests/
```

### Build the standalone executable

```bash
python build_exe.py                       # GUI, fast-start folder build (dist/ClipBoardSync/)
python build_exe.py --mode onefile        # portable single exe
python build_exe.py --target headless     # console build for the smoke test
```

`dist/`, `build/` and `*.spec` are git-ignored — publish them as **GitHub Release assets**, never commit them. Wrap the folder build in an installer with Inno Setup 6 using `installer.iss`.

---

## Architecture

| Layer | Technology |
|-------|------------|
| Sync engine | FastAPI + Uvicorn, WebSocket (`/ws`), REST history (`/api/history`), upload (`/api/upload`), pairing (`/api/pair`) |
| Windows client | Native Win32 clipboard hooks (pywin32) wired into asyncio |
| Desktop GUI | CustomTkinter, QR via `qrcode` + Pillow |
| Mobile frontend | Vanilla HTML/CSS/JS (no CDN, no frameworks), native WebSocket + Clipboard APIs |

### Project structure

```
ClipBoardSync/
├── gui/                  # Desktop GUI (CustomTkinter)
├── server/               # FastAPI app, sync hub, pairing, storage
│   ├── auth.py           # PIN + device tokens + brute-force throttle
│   ├── hub.py            # WebSocket fan-out, history, device list
│   ├── store.py          # SQLite history, pins, upload janitor
│   ├── media.py          # thumbnail helper
│   ├── net.py            # LAN IP, free-port probe, single instance
│   ├── filenames.py      # safe download names
│   └── static/           # Mobile dashboard (HTML/CSS/JS)
├── client/               # Clipboard monitor + WebSocket client
│   ├── clipboard_backend.py  # Win32/Fake backends, content hash
│   └── config.py         # device id + receive prefs (prefs.json)
├── assets/               # Application icon
├── run_gui.py            # GUI entry point
├── run_app.py            # Headless entry point (--port/--headless)
├── build_exe.py          # PyInstaller build script
├── installer.iss         # Inno Setup installer script
├── tests/                # pytest suite + built-exe smoke test
└── DESIGN.md             # Design system & UI guidelines
```

Receive preferences live in `~/.clipboardsync/prefs.json`: receive folder, auto-download cap, put-received-on-clipboard, private-clip sharing. App data (history DB, shared files, trust store) lives in `%LOCALAPPDATA%\ClipBoardSync` on Windows.

### Other computers nearby (experimental)

The Devices tab can join another PC's server with its address + code. It works for basic sharing; PC-to-PC is still being hardened — same-Wi-Fi phone sharing is the supported path.

### Design

The interface follows `DESIGN.md`: a neutral palette with a single indigo accent, vector icons, restrained radius, clean list of shared clips, and keyboard-first search. Use plain `Sharing` language — never bridge, engine, or backend in user-facing text. Any UI changes must respect it.

---

## Troubleshooting

- **Phone can't open the link** — same Wi-Fi on both? Allow ClipBoardSync through Windows Firewall for private networks. The app rolls to the next free port (8000–8010) if 8000 is busy — re-scan the code after a restart.
- **QR shows the wrong address** — the address follows the active network; switching Wi-Fi updates it. Laptops with VPNs/virtual adapters: prefer the 192.168.x.x address.
- **Code rejected twice** — codes are single-use and lock out after 5 wrong tries; open the Devices tab for a fresh code and wait a few minutes after a lockout.
- **Second launch does nothing** — by design: one copy runs at a time; use the open window.
- **Do I need internet?** No. Sharing runs on your Wi-Fi only and works offline.
- **Paste button missing on the phone** — one-tap paste needs HTTPS; over plain `http` tap the box and use the system Paste.
