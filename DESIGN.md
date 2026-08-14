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

Cross-device synchronization is the core differentiator.

Always make the following states understandable:

```text
● Connected
◌ Syncing
○ Offline
! Sync Error
```

Never communicate important state through color alone.

Show the source device and sync status without making them more prominent than the clipboard content.

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
Couldn't synchronize with your phone.

The connection was interrupted.

[ Try Again ]
```

Use toasts for short-lived confirmations such as:

```text
✓ Copied
✓ Device connected
✓ Sent to Phone
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