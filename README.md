# ⚡ ClipBoardSync

<p align="center">
  <b>Universal Real-Time Cross-Device Local Wi-Fi Clipboard Bridge</b><br>
  <i>Seamlessly share clipboard text between Windows laptops and mobile smartphones without cloud servers or installing apps on your phone!</i>
</p>

---

## 🌟 Overview

**ClipBoardSync** solves the universal frustration of transferring copied text, links, notes, and messages between your computer and mobile phone. 

Instead of emailing text to yourself, sending messages via third-party chat apps, or relying on internet-reliant cloud storage, ClipBoardSync creates a high-speed, secure local WebSocket bridge right over your home Wi-Fi or personal mobile hotspot.

```
       [ Windows Desktop / PC ]                        [ Mobile Phone / Tablet ]
  (Native Win32 Clipboard + Web Hub)  <== Wi-Fi ==>    (Standard Browser Dashboard)
          ⚡ Port 8000 WebSockets                            📷 Scan QR Code
```

---

## ✨ Features

- **🚀 Zero-Configuration Mobile Pairing:** Simply point your iPhone or Android camera at the QR code displayed on your PC to launch the live mobile dashboard instantly in Safari, Chrome, or Firefox!
- **💻 Polished Desktop Application:** Designed with **CustomTkinter** for an aesthetic dark-mode desktop GUI featuring live connection counters, instant QR code rendering, synchronized history logs, and a built-in diagnostic feed.
- **📱 No Phone Apps to Install:** Everything operates via standard mobile browser technologies with real-time bidirectional synchronization.
- **🛡️ 100% Local & Privacy-First:** Your copied data never leaves your Wi-Fi router. It functions entirely offline without internet access or external cloud accounts.
- **📦 One-Click Executable (.exe) for Non-Coders:** Designed for everyday users! Run the pre-compiled standalone desktop application without touching a terminal or installing Python.

---

## 👥 Guide for Everyday Users (Quick Start)

Not a programmer? No problem! You do not need Python or any terminal commands to use ClipBoardSync.

1. **Download the App:**
   - Go to the **Releases** tab on GitHub and download the pre-compiled standalone executable file: **`ClipBoardSync.exe`**.
2. **Launch on Your PC:**
   - Double-click `ClipBoardSync.exe` to open the desktop dashboard.
   - Click **`▶ START BRIDGE`** (if not already started automatically) to launch your local synchronization server.
3. **Connect Your Phone:**
   - Make sure your PC and mobile device are connected to the exact **same Wi-Fi network** or portable hotspot.
   - Open your smartphone camera app and point it at the **QR Code** displayed on the screen.
   - Tap the pop-up browser banner to open your personal live clipboard dashboard!
4. **Start Syncing!**
   - Copy any text on your laptop, and watch it appear instantly in your phone's browser feed.
   - Paste or write text on your mobile phone browser dashboard, and it immediately injects into your Windows PC clipboard ready to paste (`Ctrl+V`)!

---

## 🛠️ Guide for Developers (Source Code & Build)

If you are a developer looking to extend or compile ClipBoardSync from source, follow these instructions:

### 1. Prerequisites
- **OS:** Windows 10/11 (Required for native Win32 clipboard hooks; server architecture is platform-independent).
- **Python:** Version `>= 3.13` (Recommended).

### 2. Installation
Clone the repository and install dependencies via `uv` or standard Python `pip`:

```bash
# Using uv (Recommended)
uv sync

# OR using traditional pip
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Running Locally from Source

- **Run Desktop GUI Dashboard:**
  ```bash
  python run_gui.py
  ```
- **Run Headless Terminal Launcher:**
  ```bash
  python run_app.py
  ```

### 4. Building Standalone Windows Executable (.exe)

ClipBoardSync includes an automated one-click build script powered by **PyInstaller** that packages the entire server, clipboard monitor, GUI window, and static web HTML/CSS/JS frontend into a single self-contained executable file:

```bash
python build_exe.py
```
Once complete, your portable executable will be generated at **`dist/ClipBoardSync.exe`** ready for seamless public sharing!

---

## 📐 Architecture & Tech Stack

- **Backend Sync Engine:** [FastAPI](https://fastapi.tiangolo.com/) & [Uvicorn](https://www.uvicorn.org/) providing asynchronous real-time WebSocket communication (`/ws`) and RESTful history endpoints (`/api/history`).
- **Windows Client:** Native Win32 user32 clipboard monitoring hooks (`pywin32`) integrated directly into asyncio event loops.
- **Desktop Interface:** Modern dark-mode GUI framework via [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) + QR rendering via [Pillow](https://python-pillow.org/) & `qrcode`.
- **Mobile Frontend:** Responsive Vanilla HTML5/CSS3/JavaScript single-page app utilizing native browser WebSockets and Clipboard APIs.

---

## ❓ Troubleshooting & FAQ

- **Why can't my phone open the QR code link?**
  - Verify that both devices are on the identical local Wi-Fi subnet.
  - Check your **Windows Defender Firewall**. If Windows prompts for network access when launching ClipBoardSync, make sure to check **Allow access for Private networks** so incoming mobile requests on port `8000` are not blocked.
- **Does this require an active internet connection?**
  - No! The system relies purely on local LAN routing. It functions smoothly even on offline routers or isolated Wi-Fi access points.
