# ClipboardSync — Design System

## Product

ClipboardSync is a lightweight cross-device clipboard utility for syncing text, images, and files between devices.

### Core UX

> **Open → Search → Select → Done.**

The product should feel **fast, minimal, native, private, and reliable**.

It is a utility, **not a SaaS dashboard**.

---

## Design Direction

Reference the UX philosophy of:

* Raycast — fast, keyboard-first interaction
* Maccy — lightweight clipboard management
* Modern native desktop applications — clean hierarchy and restrained visuals

Prioritize **content and usability over decoration**.

Avoid:

* Excessive gradients
* Excessive glassmorphism
* Neon colors
* Huge rounded cards
* Excessive shadows
* Unnecessary animations
* Decorative UI that doesn't serve a purpose
* Emoji as primary UI icons
* Generic SaaS-dashboard aesthetics

---

## Visual System

### Colors

Use a neutral background with **one primary accent**.

Light:

```text
Background: #F7F8FA
Surface: #FFFFFF
Border: #E5E7EB
Text: #111827
Secondary Text: #6B7280
Primary: #6366F1
```

Dark:

```text
Background: #0D0F12
Surface: #15181D
Border: #292E36
Text: #F3F4F6
Secondary Text: #9CA3AF
Primary: #818CF8
```

Use semantic colors for success, warning, error, and info.

Do not introduce arbitrary colors.

### Typography

Use:

```text
Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif
```

Keep typography clean and restrained.

### Spacing

Use a 4px-based spacing system:

```text
4 / 8 / 12 / 16 / 24 / 32 / 48
```

### Radius

Use restrained rounding:

```text
Controls: 6–8px
Cards: 10px
Overlays: 12–14px
```

---

## Layout

Desktop:

```text
┌──────────────┬─────────────────────────────┐
│              │                             │
│   Sidebar    │       Main Content          │
│              │                             │
│ Clipboard    │       Search                │
│ Pinned       │                             │
│ Files        │       Clipboard History     │
│ Devices      │                             │
│              │                             │
│ Settings     │                             │
└──────────────┴─────────────────────────────┘
```

Primary navigation:

* Clipboard
* Pinned
* Files
* Devices
* Settings

Keep the sidebar compact and collapsible.

On mobile, replace the desktop sidebar with a mobile-friendly navigation pattern.

---

## Clipboard History

The clipboard item is the **most important UI element**.

Each item should clearly communicate:

* Content preview
* Content type
* Source device
* Timestamp
* Pin state
* Relevant actions

Prefer a **clean list** over a grid of cards.

Support visually distinct previews for:

* Text
* URLs
* Images
* Files

Long content should be truncated in the history view and shown fully in a preview.

---

## Search

Search is a first-class feature.

It should be:

* Fast
* Always accessible
* Keyboard-friendly
* Able to search clipboard content, filenames, URLs, and devices

Recommended shortcut:

```text
Ctrl/Cmd + K
```

---

## Keyboard First

ClipboardSync should be highly usable without a mouse.

Support:

```text
↑ ↓       Navigate
Enter     Select
Esc       Close
Ctrl/Cmd+K Search
```

A future quick clipboard overlay should follow:

```text
Global Shortcut
      ↓
Search-focused Overlay
      ↓
Navigate
      ↓
Select
      ↓
Paste
      ↓
Close
```

---

## Devices & Sync

Cross-device sharing is the core differentiator. Use the word
**Sharing** everywhere. Never use bridge, engine, backend, WebSocket,
Win32, Uvicorn, LAN, subnet, portal, or peer in user-facing text.

Always make the following states understandable with icon + text
(never color alone):

```text
● Sharing on
◌ Connecting / Starting sharing… / Searching for computers…
○ Not sharing
! Couldn't share — Try again
```

Progress is a first-class state for folders and big files:

```text
Zipping 'Photos'…
Sharing Report.zip… 48%
Saving 'Report.zip'… 48% → Saved to Downloads/ClipBoardSync
```

Show the source as `This computer`, `Other computer`, or `Your phone`,
never as a raw device ID, UUID, Desktop-GUI, PC-XXXX, Win32, or server.
Clip content stays more prominent than device and status meta.

---

## Voice & Tone

Write for a non-technical person trying to share something quickly.

* Plain verbs, sentence case, no filler. Example: `Start sharing`,
  not `START BRIDGE`.
* Active voice, one action per control. The button says `Start sharing`,
  the toast says `Sharing is on`.
* Name things by what users understand: `This computer`, `Other computer`,
  `Your phone`, `Pairing code`, `Same Wi-Fi`. Never expose how the system is built.
* Errors explain what happened and how to fix it. They never apologize
  excessively and are never vague.
* Empty screens invite action with a next step and, where possible,
  a button that goes there.
* Folders are always shared as one `.zip` (`Photos.zip · 12 items`).
  Big files download over a direct link with a progress bar, never silently.

Preferred terms:

```text
Start sharing / Stop sharing  (never Start/Stop bridge)
Sharing on / Not sharing       (never Online/Offline bridge)
Connect your phone             (never Pair your device to the bridge)
Other computers nearby         (never peers / hosts / beacons)
Pairing code                   (6-digit code shown on the computer)
Same Wi-Fi                     (never same subnet / LAN / local routing)
Copy link / Open on this computer
Send photo / Send file / Send folder
Save file / Save folder (.zip) / Save photo
No phones connected / 1 phone connected
Searching for computers… / Connected to other computer at 192.168.1.6
```

Avoid: bridge, engine, backend, WebSocket, Win32, Uvicorn, LAN bridge,
SYSOUT, portal, peer, host, beacon, localhost, port (except `Port 8000`
in small status detail where support needs it).

---

## Terminal Output

Terminal output is plain text only: no emoji, no `[+] [*] [!]` markers,
no ASCII banners. Use short sections and numbered steps.

```text
ClipBoardSync is running
-------------------------
Address: http://192.168.1.5:8000
Pairing code: 482193 (enter once on your phone)

How to connect:
1. Join the same Wi-Fi on your computer and phone.
2. Scan the code in the app, or open the address above on your phone.
3. Copy on either device to share it.

Press Ctrl+C to stop sharing.
```

Build scripts follow the same style: `Building ClipBoardSync...`,
`Cleaning build/`, `Build complete: dist/ClipBoardSync.exe`,
plus one next step on failure.

---

## Feedback States

Every important UI component should handle:

* Loading
* Empty
* Error
* Disabled
* Success

Errors should explain what happened and provide a recovery action.

Example:

```text
Couldn't share with your phone.

The connection was interrupted.

[ Try Again ]
```

Use toasts for short-lived confirmations such as:

```text
Copied to clipboard
Phone connected — ready to share
Shared with your computer
Photo shared
File shared
```

---

## Responsive Design

The UI must work across:

```text
Mobile
Tablet
Desktop
```

Do not simply shrink the desktop interface for mobile.

Important functionality must never depend on hover.

---

## Accessibility

Always maintain:

* Keyboard navigation
* Visible focus states
* Good contrast
* Semantic controls
* Accessible labels
* Reduced-motion support

---

## AI Implementation Rules

When modifying the frontend:

1. Read this file first.
2. Preserve the existing design language.
3. Reuse existing components and design tokens.
4. Do not introduce arbitrary colors, fonts, icons, or styles.
5. Do not redesign unrelated parts of the application.
6. Preserve responsive behavior.
7. Handle loading, empty, and error states.
8. Prefer simple, functional UI over decorative UI.

---

## North Star

**Fast. Minimal. Native. Reliable.**

The complexity of synchronization should remain invisible to the user.

> **Open → Search → Select → Done.**