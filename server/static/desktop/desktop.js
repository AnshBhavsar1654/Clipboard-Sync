/* ClipBoardSync desktop window.
 * Talks to /api/admin/* with the per-launch token (?t=) and gets live clips
 * over the loopback WebSocket. Every dynamic value goes in through
 * textContent / DOM nodes, never innerHTML (CSP is script-src 'self'). */
(() => {
"use strict";

const params = new URLSearchParams(location.search);
const TOKEN = params.get("t") || "";
const QUICK = params.get("quick") === "1";
const THEME_KEY = "clipboardsync_theme";   // shared with /theme.js (pre-paint)

const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";

if (QUICK) document.body.classList.add("quick");
if (!TOKEN) {
    document.body.textContent = "Open this page from the ClipBoardSync app.";
    return;
}

// ---------- state ----------
let items = [];                 // newest first
let S = null;                   // last /api/admin/state
let filter = "all", query = "";
let selectedId = null, openId = null;
let paused = false;
const fresh = new Set();
const rowById = new Map();
let lastSig = "";

// ---------- tiny helpers ----------
function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
        if (v == null || v === false) continue;
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) n.append(kid);
    return n;
}

function icon(name) {
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "ic");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(NS, "use");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
}

function api(path, opts = {}) {
    const sep = path.includes("?") ? "&" : "?";
    return fetch(path + sep + "t=" + encodeURIComponent(TOKEN), opts);
}
const postJson = (path, body) => api(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
});

function shell() {
    return (window.pywebview && window.pywebview.api) || null;
}

function toast(msg, action) {
    const n = el("div", { class: "toast" }, el("span", { text: msg }));
    if (action) n.append(el("button", { text: action.label, onclick: () => { n.remove(); action.run(); } }));
    $("toasts").append(n);
    setTimeout(() => n.remove(), action ? 6000 : 3200);
}

function formatSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
    return `${(n / 1073741824).toFixed(2)} GB`;
}

function ago(iso) {
    const t = new Date(iso).getTime();
    if (!t) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function dayLabel(iso) {
    const d = new Date(iso), now = new Date();
    const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((start(now) - start(d)) / 86400000);
    if (isNaN(diff) || diff <= 0) return "Today";
    if (diff === 1) return "Yesterday";
    return "Earlier";
}

function sourceName(id) {
    if (!id) return "Unknown device";
    if (S && (id === S.this_device_id)) return "This computer";
    if (id === "This computer") return "This computer";
    if (/^phone-/i.test(id)) return "Phone";
    if (/^web-/i.test(id)) return "Browser";
    return "Another device";
}

function kindOf(it) {
    const type = it.type || "text";
    if (type !== "text") return type;
    const c = String(it.content || "").trim();
    if (/^https?:\/\/\S+$/i.test(c)) return "link";
    if (c.includes("\n") || /[{};]\s*$|=>|<\/?\w+>|^\s*(def|function|const|import|class)\s/m.test(c)) return "code";
    return "text";
}
const KIND_LABEL = { text: "Text", link: "Link", code: "Code", image: "Image", file: "File", folder: "Folder" };
const KIND_ICON = { text: "text", link: "link", code: "code", image: "image", file: "file", folder: "folder" };

// ---------- list ----------
function matchesFilter(it) {
    const type = it.type || "text";
    if (filter === "pinned") return !!it.pinned;
    if (filter === "text") return type === "text";
    if (filter === "image") return type === "image";
    if (filter === "file") return type === "file" || type === "folder";
    return true;
}

function visibleItems() {
    const q = query.toLowerCase();
    let list = items.filter((it) => matchesFilter(it) && (!q ||
        [it.content, it.filename, sourceName(it.device_id)].filter(Boolean).join(" ").toLowerCase().includes(q)));
    if (QUICK) list = list.slice(0, 8);
    return list;
}

function titleNode(it, kind) {
    if (kind === "link") {
        const c = String(it.content).trim();
        try {
            const u = new URL(c);
            const rest = (u.pathname === "/" ? "" : u.pathname) + u.search;
            return el("p", { class: "clip-title" }, el("span", { class: "host", text: u.host }), el("span", { class: "path", text: rest }));
        } catch (e) { /* fall through */ }
    }
    if (kind === "image") return el("p", { class: "clip-title", text: it.filename || "Image" });
    if (kind === "file") return el("p", { class: "clip-title", text: it.filename || "File" });
    if (kind === "folder") return el("p", { class: "clip-title", text: it.filename || "Folder" });
    const text = String(it.content || "").slice(0, 1200);
    return el("p", { class: "clip-title" + (kind === "code" ? " code" : ""), text });
}

function tileNode(it, kind) {
    const tile = el("div", { class: `tile t-${kind}` });
    const src = it.thumb || (kind === "image" ? it.file_url : "");
    if (kind === "image" && src && (src.startsWith("data:image/") || src.startsWith("/api/files/"))) {
        tile.append(el("img", { src, alt: "", loading: "lazy" }));
    } else {
        tile.append(icon(KIND_ICON[kind]));
    }
    return tile;
}

function metaNode(it, kind) {
    const parts = [KIND_LABEL[kind], sourceName(it.device_id), ago(it.timestamp)];
    if (kind === "image" || kind === "file") parts.push(it.filesize ? formatSize(it.filesize) : "");
    if (kind === "folder") parts.push(it.entry_count != null ? `${it.entry_count} items` : "");
    else if (kind === "text" || kind === "code") {
        const len = String(it.content || "").length;
        if (len > 200) parts.push(`${len.toLocaleString()} characters`);
    }
    const meta = el("div", { class: "clip-meta" });
    if (it.pinned) meta.append(el("span", { class: "pin-flag", title: "Pinned" }, icon("pin")));
    parts.filter(Boolean).forEach((p, i) => meta.append(el("span", { class: i || it.pinned ? "sep" : "", text: p })));
    return meta;
}

function actionBtn(label, ic, run, extra) {
    return el("button", { class: "icon-btn " + (extra || ""), title: label, "aria-label": label, tabindex: "-1",
        onclick: (e) => { e.stopPropagation(); run(); } }, icon(ic));
}

function actionsNode(it, kind) {
    const box = el("div", { class: "clip-actions" });
    const isFile = kind === "file" || kind === "folder";
    if (kind === "link") box.append(actionBtn("Open in browser", "external", () => openLink(it)));
    box.append(el("button", { class: "btn small primary", tabindex: "-1", onclick: (e) => { e.stopPropagation(); primary(it); } },
        icon(isFile ? "download" : "copy"), el("span", { text: isFile ? "Save" : "Copy" })));
    box.append(actionBtn(it.pinned ? "Unpin" : "Pin", "pin", () => togglePin(it), it.pinned ? "on" : ""));
    box.append(actionBtn("Delete", "trash", () => remove(it), "danger"));
    return box;
}

function render() {
    const list = $("list");
    const vis = visibleItems();
    rowById.clear();
    list.replaceChildren();

    if (!vis.some((i) => i.id === selectedId)) selectedId = QUICK && vis.length ? vis[0].id : null;

    let group = null;
    for (const it of vis) {
        const g = dayLabel(it.timestamp);
        if (!QUICK && g !== group) { group = g; list.append(el("h3", { class: "group-title", text: g })); }
        const kind = kindOf(it);
        const row = el("div", {
            class: "clip" + (it.id === selectedId ? " selected" : "") + (it.id === openId ? " open" : "") + (fresh.has(it.id) ? " fresh" : ""),
            role: "option", tabindex: "-1", "aria-selected": it.id === selectedId ? "true" : "false",
            onclick: () => onRowClick(it),
            ondblclick: () => primary(it),
        }, tileNode(it, kind), el("div", { class: "clip-body" }, titleNode(it, kind), metaNode(it, kind)), actionsNode(it, kind));
        rowById.set(it.id, row);
        list.append(row);
    }
    updateEmpty(vis.length);
    updateCounts();
}

function onRowClick(it) {
    if (selectedId === it.id) { openId = openId === it.id ? null : it.id; rowById.get(it.id)?.classList.toggle("open", openId === it.id); }
    else select(it.id, false);
}

function select(id, scroll = true) {
    if (selectedId && rowById.has(selectedId)) {
        const o = rowById.get(selectedId); o.classList.remove("selected"); o.setAttribute("aria-selected", "false");
    }
    selectedId = id;
    const row = rowById.get(id);
    if (row) {
        row.classList.add("selected"); row.setAttribute("aria-selected", "true");
        if (scroll) row.scrollIntoView({ block: "nearest" });
    }
}

function updateEmpty(count) {
    const empty = $("empty");
    empty.hidden = count !== 0;
    if (count !== 0) return;
    const action = $("empty-action");
    action.hidden = true;
    const searching = query || filter !== "all";
    if (searching && items.length) {
        $("empty-icon").setAttribute("href", "#i-search");
        $("empty-title").textContent = "No matches";
        $("empty-body").textContent = query ? `Nothing found for "${query}".` : "Nothing in this view yet.";
    } else {
        $("empty-icon").setAttribute("href", "#i-clipboard");
        $("empty-title").textContent = "Nothing shared yet";
        const noPhone = S && !(S.phones || []).length && !(S.connection_count);
        $("empty-body").textContent = noPhone
            ? "Connect your phone, then copy something on either device. It shows up here and on the other one."
            : "Copy something on this computer or send it from your phone. It will show up here.";
        if (noPhone) { action.textContent = "Connect a device"; action.hidden = false; action.onclick = openConnect; }
    }
}

function updateCounts() {
    const c = { all: items.length, text: 0, image: 0, file: 0, pinned: 0 };
    for (const it of items) {
        const t = it.type || "text";
        if (t === "text") c.text++; else if (t === "image") c.image++; else c.file++;
        if (it.pinned) c.pinned++;
    }
    document.querySelectorAll("[data-count]").forEach((n) => { const v = c[n.dataset.count]; n.textContent = v ? v : ""; });
}

// ---------- item actions ----------
async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return; } catch (e) { /* fall back */ }
    const ta = el("textarea", { class: "sprite" });
    ta.value = text; document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
}

async function toPng(blob) {
    if (blob.type === "image/png") return blob;
    const bmp = await createImageBitmap(blob);
    const cv = document.createElement("canvas");
    cv.width = bmp.width; cv.height = bmp.height;
    cv.getContext("2d").drawImage(bmp, 0, 0);
    return await new Promise((res, rej) => cv.toBlob((b) => (b ? res(b) : rej(new Error("png"))), "image/png"));
}

async function primary(it) {
    const kind = kindOf(it);
    try {
        if (kind === "image" && it.file_url) {
            const r = await fetch(it.file_url);
            if (!r.ok) throw new Error("gone");
            const png = await toPng(await r.blob());
            await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
            toast("Image copied");
        } else if (kind === "file" || kind === "folder") {
            await save(it);
            return;
        } else {
            if (QUICK && shell() && shell().paste_to_last_app) {
                await shell().paste_to_last_app(String(it.content || ""));
                closeQuick();
                return;
            }
            await copyText(String(it.content || ""));
            toast("Copied");
        }
        if (QUICK) closeQuick();
    } catch (e) {
        toast(kind === "image" ? "Could not copy the image. Try Save." : "Could not copy that.");
    }
}

async function save(it) {
    const sh = shell();
    if (!it.file_url) return;
    if (!sh || !sh.save_item) {
        const a = el("a", { href: it.file_url, download: it.filename || "file" });
        document.body.append(a); a.click(); a.remove();
        return;
    }
    toast("Saving…");
    const res = await sh.save_item(it.file_url, it.filename || "file");
    if (res && res.ok) {
        toast("Saved " + res.path.split("\\").pop(), { label: "Show", run: () => sh.show_in_folder(res.path) });
    } else {
        toast((res && res.error) || "Could not save the file.");
    }
}

function openLink(it) {
    const sh = shell();
    const url = String(it.content || "").trim();
    if (sh && sh.open_url) sh.open_url(url); else window.open(url, "_blank", "noopener");
}

async function togglePin(it) {
    if (!it.id) return;
    const r = await postJson(`/api/history/${encodeURIComponent(it.id)}/pin`, { pinned: !it.pinned });
    if (r.ok) { it.pinned = !it.pinned; render(); toast(it.pinned ? "Pinned" : "Unpinned"); }
}

async function remove(it) {
    if (!it.id) return;
    const r = await api(`/api/history/${encodeURIComponent(it.id)}`, { method: "DELETE" });
    if (r.ok) {
        const vis = visibleItems(), i = vis.findIndex((x) => x.id === it.id);
        items = items.filter((x) => x.id !== it.id);
        const next = vis[i + 1] || vis[i - 1];
        selectedId = next ? next.id : null;
        render();
    }
}

function closeQuick() {
    const sh = shell();
    if (sh && sh.close_window) sh.close_window(); else window.close();
}

// ---------- status ----------
function renderStatus() {
    const box = $("status");
    const phones = S ? S.connection_count || 0 : 0;
    let state = "idle", text = "Ready", sub = "No device connected yet.";
    if (!S) { state = "starting"; text = "Starting…"; sub = ""; }
    else if (paused) { state = "paused"; text = "Sharing paused"; sub = "Nothing is sent until you resume."; }
    else if (phones > 0) {
        state = "on"; text = "Sharing on";
        const names = (S.devices || []).map((d) => friendly(d)).filter(Boolean);
        sub = phones === 1 ? `${names[0] || "1 device"} is connected.` : `${phones} devices are connected.`;
    }
    const peer = S && S.peer;
    if (peer && peer.status !== "disconnected" && !paused) {
        const linked = peer.status === "connected" ? `Linked to ${peer.name || peer.host}.` : `Linking to ${peer.name || peer.host}…`;
        sub = state === "on" ? `${sub} ${linked}` : linked;
        if (state === "idle" && peer.status === "connected") state = "on";
    }
    box.dataset.state = state;
    $("status-text").textContent = text;
    $("status-sub").textContent = sub;
    const pb = $("pause-btn");
    pb.replaceChildren(icon(paused ? "play" : "pause"));
    pb.title = pb.ariaLabel = paused ? "Resume sharing" : "Pause sharing";
}

function friendly(d) {
    const raw = d.name || d.id || "";
    if (d.kind === "computer" && raw) return raw;
    if (!raw || /^(phone|web)-/i.test(raw)) return /^web-/i.test(raw) ? "A browser" : "Your phone";
    return raw;
}

async function refreshState() {
    const r = await api("/api/admin/state");
    if (r.status === 403) {
        document.body.textContent = "Restart ClipBoardSync, then reopen this window.";
        throw new Error("forbidden");
    }
    S = await r.json();
    paused = S.engine_running === false;
    const fresher = (S.history || []).slice().reverse();
    const sig = fresher.map((i) => `${i.id}:${i.pinned ? 1 : 0}`).join();
    if (sig !== lastSig) {
        lastSig = sig;
        items = fresher;
        render();
    } else updateEmpty(visibleItems().length);
    renderStatus();
    if (!$("connect-dialog").hidden && !$("tab-computer").hidden) renderPeer();
    return S;
}
window.refreshState = () => refreshState().catch(() => {});

// ---------- live clips ----------
function connectLive() {
    let ws;
    try { ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`); }
    catch (e) { return; }
    ws.onmessage = (e) => {
        let msg;
        try { msg = JSON.parse(e.data); } catch (err) { return; }
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "history" && Array.isArray(msg.items)) {
            items = msg.items.slice().reverse();
            lastSig = items.map((i) => `${i.id}:${i.pinned ? 1 : 0}`).join();
            render();
        } else if (["text", "image", "file", "folder"].includes(msg.type) && (msg.content || msg.file_url)) {
            const idx = items.findIndex((x) => x.id && x.id === msg.id);
            if (idx !== -1) items[idx] = { ...items[idx], ...msg };
            else {
                items.unshift(msg);
                fresh.add(msg.id);
                setTimeout(() => fresh.delete(msg.id), 1200);
            }
            lastSig = items.map((i) => `${i.id}:${i.pinned ? 1 : 0}`).join();
            render();
        }
    };
    ws.onclose = () => setTimeout(connectLive, 2500);
}

// ---------- connect dialog ----------
let connectTimer = null, connectPin = "", connectBase = 0;

async function openConnect(tab = "phone") {
    $("connect-dialog").hidden = false;
    showTab(tab === "computer" ? "computer" : "phone");
    $("connect-state").className = "connect-state";
    await reloadConnect();
    connectBase = S ? S.connection_count || 0 : 0;
    $("connect-close").focus();
    clearInterval(connectTimer);
    connectTimer = setInterval(async () => {
        try {
            const s = await refreshState();
            if ((s.pairing_pin || "") !== connectPin) { await reloadConnect(); }
            if ((s.connection_count || 0) > connectBase) {
                const st = $("connect-state");
                st.className = "connect-state ok";
                st.textContent = "Connected. You're all set.";
                clearInterval(connectTimer);
                setTimeout(closeConnect, 1600);
            }
        } catch (e) { /* retry on next tick */ }
    }, 1500);
}
window.openConnect = openConnect;

function closeConnect() {
    $("connect-dialog").hidden = true;
    clearInterval(connectTimer); connectTimer = null;
}

async function reloadConnect() {
    const s = await refreshState();
    connectPin = s.pairing_pin || "";
    $("qr-img").src = "/api/admin/qr.png?t=" + encodeURIComponent(TOKEN) + "&_=" + Date.now();
    $("connect-addr").textContent = (s.pairing_url || "").replace(/\/?\?pair=.*$/, "").replace(/^http:\/\//, "");
    $("pair-code").textContent = connectPin || "";
    $("step-code").hidden = !s.require_pin;
    $("connect-state").textContent = "Waiting for your phone…";
}

function showTab(name) {
    document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $("tab-phone").hidden = name !== "phone";
    $("tab-computer").hidden = name !== "computer";
    if (name === "computer") renderPeer();
}

const PEER_TEXT = {
    connected: "Connected. Clips sync both ways.",
    connecting: "Connecting…",
    retrying: "Lost the connection. Trying again…",
    code_rejected: "The other computer no longer trusts this one. Disconnect and pair again.",
};

function renderPeer() {
    if (!S) return;
    const peer = S.peer;
    const linked = !!(peer && peer.status !== "disconnected");
    $("peer-linked").hidden = !linked;
    $("peer-form").hidden = linked;
    if (linked) {
        $("peer-name").textContent = peer.name || peer.host;
        $("peer-status").textContent = PEER_TEXT[peer.status] || "";
        return;
    }
    const nearby = S.nearby || [];
    $("nearby").hidden = !nearby.length;
    const list = $("nearby-list");
    list.replaceChildren(...nearby.map((n) => el("button", { class: "nearby-row", onclick: () => {
        $("peer-host").value = n.ip; $("peer-port").value = n.port; $("peer-code").focus();
    } }, el("span", {}, el("strong", { text: n.name }), " ", el("small", { text: `${n.ip}:${n.port}` })), icon("link"))));
}

async function connectPeer() {
    const err = $("peer-error"), btn = $("peer-connect");
    err.hidden = true; btn.disabled = true; btn.textContent = "Connecting…";
    try {
        const r = await postJson("/api/admin/peer", {
            host: $("peer-host").value, port: Number($("peer-port").value) || 8000, code: $("peer-code").value,
        });
        if (!r.ok) {
            const body = await r.json().catch(() => ({}));
            err.textContent = body.detail || "Could not connect."; err.hidden = false;
        } else { $("peer-code").value = ""; toast("Computer linked"); }
    } catch (e) { err.textContent = "Could not connect."; err.hidden = false; }
    btn.disabled = false; btn.textContent = "Connect";
    await refreshState().catch(() => {}); renderPeer();
}

// ---------- settings ----------
function openDrawer() {
    $("drawer").hidden = false; $("scrim").hidden = false;
    loadSettings();
    $("drawer-close").focus();
}
function closeDrawer() { $("drawer").hidden = true; $("scrim").hidden = true; }

async function loadSettings() {
    const s = await refreshState();
    const st = s.settings || {};
    $("set-folder").value = st.receive_folder || "";
    $("set-cap").value = Math.round((st.auto_receive_max_bytes ?? 52428800) / 1048576);
    $("set-clipboard").checked = st.put_received_on_clipboard !== false;
    $("set-private").checked = st.share_private_clips !== true;
    $("set-pin").checked = st.require_pin !== false;
    $("about-line").textContent = `ClipBoardSync ${s.version || ""} · ${s.computer_name || ""} · port ${s.port}`;
    renderPhones(s);
    markThemeButtons();
}

function renderPhones(s) {
    const box = $("phones-list");
    box.replaceChildren();
    const rows = [];
    for (const p of s.phones || []) {
        const seen = p.last_seen ? `Last seen ${ago(new Date(p.last_seen * 1000).toISOString())}` : "";
        const label = /^(phone|web)-/i.test(p.name || "") ? `Phone ${String(p.name).split("-")[1] || ""}`.trim() : p.name || "Phone";
        rows.push({ id: p.id, label, seen });
    }
    if (!rows.length) { box.append(el("p", { class: "muted", text: "Nothing paired yet." })); return; }
    for (const r of rows) {
        box.append(el("div", { class: "phone-row" }, icon("phone"),
            el("div", {}, el("span", { text: r.label }), el("small", { text: r.seen })),
            el("button", { class: "btn small danger", text: "Remove", onclick: async () => {
                await api(`/api/admin/devices/${encodeURIComponent(r.id)}`, { method: "DELETE" });
                loadSettings();
            } })));
    }
}

let saveTimer = null;
function queueSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
        const r = await postJson("/api/admin/settings", {
            receive_folder: $("set-folder").value.trim(),
            auto_receive_max_bytes: Math.max(0, Number($("set-cap").value || 0)) * 1048576,
            put_received_on_clipboard: $("set-clipboard").checked,
            share_private_clips: !$("set-private").checked,
            require_pin: $("set-pin").checked,
        });
        if (!r.ok) toast("Could not save settings.");
    }, 350);
}

function applyTheme(choice) {
    try {
        if (choice === "system") localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, choice);
    } catch (e) { /* private mode */ }
    const dark = choice === "dark" || (choice === "system" && !matchMedia("(prefers-color-scheme: light)").matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    markThemeButtons();
}
function markThemeButtons() {
    let cur = "system";
    try { const t = localStorage.getItem(THEME_KEY); if (t === "dark" || t === "light") cur = t; } catch (e) { /* ignore */ }
    document.querySelectorAll("[data-theme-choice]").forEach((b) => b.classList.toggle("active", b.dataset.themeChoice === cur));
}

async function runDiagnostics() {
    const out = $("diag-out");
    out.replaceChildren(el("li", { text: "Checking…" }));
    const d = await (await api("/api/admin/diagnostics")).json();
    const rows = [];
    const ips = d.lan_ips || [];
    rows.push(ips.length ? ["", `Your phone should open http://${ips[0]}:${d.port}`] : ["bad", "No Wi-Fi or network address found. Connect this computer to your network."]);
    if (ips.length > 1) rows.push(["warn", `Other addresses: ${ips.slice(1).join(", ")}. Use the one on your phone's Wi-Fi.`]);
    rows.push([d.connection_count ? "" : "warn", d.connection_count ? `${d.connection_count} device(s) connected right now.` : "No device is connected right now."]);
    rows.push(["", `${d.history_items} clips and ${d.shared_files} files stored (${formatSize(d.shared_bytes)}).`]);
    rows.push(["warn", "If the phone can't connect, allow ClipBoardSync through Windows Firewall on private networks."]);
    out.replaceChildren(...rows.map(([cls, text]) => el("li", { class: cls, text })));
    out.dataset.text = rows.map((r) => r[1]).join("\n");
}

// ---------- sharing ----------
async function sharePaths(paths) {
    if (!paths || !paths.length) return;
    toast(`Sharing ${paths.length} item${paths.length === 1 ? "" : "s"}…`);
    try {
        const data = await (await postJson("/api/admin/share", { paths })).json();
        const n = (data.shared || []).length;
        toast(n ? `Shared ${n} item${n === 1 ? "" : "s"}` : "Nothing was shared. Files over 2 GB are skipped.");
    } catch (e) { toast("Could not share those files."); }
    refreshState().catch(() => {});
}
window.sharePaths = sharePaths;
window.toast = toast;

async function pickAndShare() {
    const sh = shell();
    if (!sh || !sh.pick_files) { toast("Open the desktop app to share files."); return; }
    sharePaths(await sh.pick_files());
}

async function shareClipboard() {
    const sh = shell();
    if (!sh || !sh.share_clipboard_now) { toast("Open the desktop app to share the clipboard."); return; }
    toast((await sh.share_clipboard_now()) ? "Clipboard shared" : "The clipboard has nothing to share.");
}

async function togglePause() {
    const r = await postJson("/api/admin/engine", { action: paused ? "start" : "stop" });
    const data = await r.json().catch(() => ({}));
    paused = typeof data.running === "boolean" ? !data.running : !paused;
    toast(paused ? "Sharing paused" : "Sharing resumed");
    refreshState().catch(() => {});
}

// ---------- wiring ----------
document.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("[data-filter]").forEach((x) => x.classList.toggle("active", x === b));
    filter = b.dataset.filter; render();
}));
$("search").addEventListener("input", (e) => { query = e.target.value.trim(); selectedId = null; render(); if (QUICK || query) { const v = visibleItems()[0]; if (v) select(v.id, false); } });
$("settings-btn").addEventListener("click", () => ($("drawer").hidden ? openDrawer() : closeDrawer()));
$("drawer-close").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);
$("connect-btn").addEventListener("click", () => openConnect());
document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("peer-connect").addEventListener("click", connectPeer);
$("peer-code").addEventListener("keydown", (e) => { if (e.key === "Enter") connectPeer(); });
$("peer-disconnect").addEventListener("click", async () => { await api("/api/admin/peer", { method: "DELETE" }); await refreshState(); renderPeer(); });
$("connect-close").addEventListener("click", closeConnect);
$("connect-x").addEventListener("click", closeConnect);
$("connect-dialog").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeConnect(); });
$("copy-link-btn").addEventListener("click", async () => {
    const s = await refreshState();
    if (s.pairing_url) { await copyText(s.pairing_url); toast("Link copied"); }
});
$("new-code-btn").addEventListener("click", async () => { await postJson("/api/admin/pairing/new"); reloadConnect(); });
$("pause-btn").addEventListener("click", togglePause);
$("share-btn").addEventListener("click", pickAndShare);
$("clip-btn").addEventListener("click", shareClipboard);
$("folder-btn").addEventListener("click", async () => {
    const sh = shell();
    if (!sh || !sh.pick_folder) return;
    const dir = await sh.pick_folder();
    if (dir) { $("set-folder").value = dir; queueSave(); }
});
["set-folder", "set-cap", "set-clipboard", "set-private", "set-pin"].forEach((id) => $(id).addEventListener("change", queueSave));
$("test-btn").addEventListener("click", () => runDiagnostics().catch(() => toast("Could not run the check.")));
$("copy-diag-btn").addEventListener("click", async () => {
    if (!$("diag-out").dataset.text) await runDiagnostics();
    await copyText($("diag-out").dataset.text || ""); toast("Details copied");
});
document.querySelectorAll("[data-theme-choice]").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.themeChoice)));

let clearArmed = null;
$("clear-btn").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    if (!clearArmed) {
        b.textContent = "Click again to delete everything"; b.classList.add("confirm");
        clearArmed = setTimeout(() => { clearArmed = null; b.textContent = "Clear history"; b.classList.remove("confirm"); }, 4000);
        return;
    }
    clearTimeout(clearArmed); clearArmed = null;
    b.textContent = "Clear history"; b.classList.remove("confirm");
    await api("/api/history", { method: "DELETE" });
    items = []; lastSig = ""; render(); toast("History cleared");
});

// Drag files anywhere onto the window. Real paths arrive through the shell
// (Python drop handler -> sharePaths); here we only paint the overlay and
// stop the browser from navigating to the dropped file.
let dragDepth = 0;
const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { e.preventDefault(); dragDepth++; $("drop").hidden = false; } });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("dragleave", (e) => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; $("drop").hidden = true; } });
window.addEventListener("drop", (e) => {
    e.preventDefault(); dragDepth = 0; $("drop").hidden = true;
    if (!shell()) toast("Drag and drop works in the desktop app.");
});

document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    const modal = !$("connect-dialog").hidden || !$("drawer").hidden;
    if (e.key === "Escape") {
        if (!$("connect-dialog").hidden) closeConnect();
        else if (!$("drawer").hidden) closeDrawer();
        else if (QUICK) closeQuick();
        else if ($("search").value) { $("search").value = ""; query = ""; render(); }
        return;
    }
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); $("search").focus(); $("search").select(); return; }
    if (mod && e.key === ",") { e.preventDefault(); $("drawer").hidden ? openDrawer() : closeDrawer(); return; }
    if (modal) return;
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA";
    const vis = visibleItems();
    const idx = vis.findIndex((x) => x.id === selectedId);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const next = e.key === "ArrowDown" ? Math.min(idx + 1, vis.length - 1) : Math.max(idx - 1, 0);
        if (vis[next < 0 ? 0 : next]) select(vis[next < 0 ? 0 : next].id);
        return;
    }
    if (e.key === "Enter") {
        const it = vis[idx] || (typing ? vis[0] : null);
        if (it && (!(e.target instanceof HTMLButtonElement))) { e.preventDefault(); primary(it); }
        return;
    }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); $("search").focus(); }
    else if (e.key === " " && vis[idx] && !(e.target instanceof HTMLButtonElement)) { e.preventDefault(); onRowClick(vis[idx]); }
    else if (e.key.toLowerCase() === "p" && !mod && vis[idx]) togglePin(vis[idx]);
    else if (e.key === "Delete" && vis[idx]) remove(vis[idx]);
    else if (mod && e.key.toLowerCase() === "v") { e.preventDefault(); shareClipboard(); }
});

try { markThemeButtons(); } catch (e) { /* ignore */ }
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => {
    let t = null; try { t = localStorage.getItem(THEME_KEY); } catch (e) { /* ignore */ }
    if (t !== "dark" && t !== "light") applyTheme("system");
});

refreshState().then(() => {
    if (QUICK) $("search").focus();
    else if (location.hash === "#connect") openConnect();
    else if (location.hash === "#computer") openConnect("computer");
    else if (location.hash === "#settings") openDrawer();
}).catch(() => {});
connectLive();
setInterval(() => { refreshState().catch(() => {}); }, 4000);
})();
