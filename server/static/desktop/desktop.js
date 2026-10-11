/* ClipBoardSync desktop window (plan §4.2): one history list, connect dialog,
 * settings drawer, quick-paste overlay. Talks to /api/admin/* with ?t=token
 * and gets live clips over the loopback WebSocket (auto-approved). */
(() => {
"use strict";

const params = new URLSearchParams(location.search);
const TOKEN = params.get("t") || "";
const QUICK = params.get("quick") === "1";

const $ = (id) => document.getElementById(id);
const listEl = $("list"), emptyEl = $("empty"), searchEl = $("search"),
    pillEl = $("status-pill"), toastsEl = $("toasts");

let items = [];           // newest first
let filter = "all", query = "", selected = 0;
let deviceNames = {};     // device_id -> display name
let engineOn = true, phoneCount = 0, currentPin = "";

if (QUICK) document.body.classList.add("quick");

function fatal(msg) {
    document.body.innerHTML = `<p style="padding:32px">${escapeHtml(msg)}</p>`;
    throw new Error(msg);
}
if (!TOKEN) fatal("Open this page from the ClipBoardSync app.");

function api(path, opts = {}) {
    const sep = path.includes("?") ? "&" : "?";
    return fetch(path + sep + "t=" + encodeURIComponent(TOKEN), opts);
}

function escapeHtml(s) {
    return String(s ?? "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function formatSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
    return `${(n / 1073741824).toFixed(2)} GB`;
}

function ago(iso) {
    if (!iso) return "";
    const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    if (!(m >= 0)) return "";
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return new Date(iso).toLocaleDateString();
}

function sourceName(id) {
    if (!id) return "Shared clip";
    if (deviceNames[id]) return deviceNames[id];
    if (id === "server") return "ClipBoardSync";
    if (id === "This computer") return "This computer";
    if (/^(phone|web)-/i.test(id)) return "Your phone";
    if (/^(pc|desktop|computer)-/i.test(id)) return "Other computer";
    return id.length > 24 ? "This computer" : id;
}

function toast(msg) {
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    toastsEl.appendChild(el);
    setTimeout(() => el.remove(), 3500);
}

async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
}

function previewOf(item) {
    const type = item.type || "text";
    if (type === "image") {
        const src = item.thumb || item.file_url || item.content || "";
        const safe = String(src).startsWith("data:image/") || String(src).startsWith("/api/files/")
            ? escapeHtml(src) : "";
        const full = item.file_url && String(item.file_url).startsWith("/api/files/")
            ? escapeHtml(item.file_url) : safe;
        return { html: safe ? `<img class="row-thumb" src="${safe}" data-full="${full}" alt="">` : "", text: item.filename || "Photo" };
    }
    if (type === "file" || type === "folder") {
        const sub = item.type === "folder" && item.entry_count != null
            ? `${item.entry_count} items` : formatSize(item.filesize);
        return { html: "", text: `${item.filename || "File"}  ·  ${sub}` };
    }
    const content = String(item.content || "");
    const firstLine = content.split("\n")[0];
    const code = content.includes("\n") || firstLine.length > 80;
    return { html: "", text: firstLine, code };
}

function visibleItems() {
    const q = query.toLowerCase();
    return items.filter((it) => {
        if (filter === "pinned" && !it.pinned) return false;
        if (filter !== "all" && filter !== "pinned" && (it.type || "text") !== filter) return false;
        if (q && ![it.content, it.filename, it.device_id].filter(Boolean).join(" ").toLowerCase().includes(q)) return false;
        return true;
    });
}

function render() {
    const vis = QUICK ? visibleItems().slice(0, 8) : visibleItems();
    if (selected >= vis.length) selected = Math.max(0, vis.length - 1);
    listEl.innerHTML = "";
    emptyEl.hidden = vis.length !== 0;
    vis.forEach((item, i) => {
        const p = previewOf(item);
        const row = document.createElement("div");
        row.className = "row" + (i === selected ? " selected" : "") + (item.pinned ? " pinned" : "");
        row.dataset.type = item.type || "text";
        row.setAttribute("role", "option");
        row.tabIndex = -1;
        const meta = `${item.type || "text"} · ${escapeHtml(sourceName(item.device_id))} · ${escapeHtml(ago(item.timestamp))}${item.pinned ? " · pinned" : ""}`;
        const dl = item.file_url && String(item.file_url).startsWith("/api/files/")
            ? `<a class="mini-btn" href="${escapeHtml(item.file_url)}?filename=${encodeURIComponent(item.filename || "file")}" download="${escapeHtml(item.filename || "file")}">Save</a>` : "";
        row.innerHTML = `
            ${p.html}
            <div class="row-body">
                <p class="row-content${p.code ? " code" : ""}">${escapeHtml(p.text)}</p>
                <div class="row-meta">${meta}</div>
            </div>
            <div class="row-actions">
                <button class="mini-btn" data-act="copy">Copy</button>
                <button class="mini-btn" data-act="pin">${item.pinned ? "Unpin" : "Pin"}</button>
                ${dl}
                <button class="mini-btn danger" data-act="delete">Delete</button>
            </div>`;
        row.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-act]");
            if (btn) { itemAction(btn.dataset.act, item); return; }
            const img = e.target.closest(".row-thumb");
            if (img && img.dataset.full) { window.open(img.dataset.full, "_blank"); return; }
            selected = i; render();
            if (e.detail === 2) copyItem(item);
        });
        listEl.appendChild(row);
    });
}

async function copyItem(item) {
    try {
        const type = item.type || "text";
        if (type === "text") await copyText(item.content || "");
        else if (type === "image" && item.file_url) {
            const r = await fetch(item.file_url);
            if (!r.ok) throw new Error("download failed");
            const blob = await r.blob();
            await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
        } else if ((type === "file" || type === "folder") && window.pywebview && window.pywebview.api) {
            await window.pywebview.api.reveal(item.file_url);
            toast("Opened the download");
            return;
        } else return;
        toast("Copied");
        if (QUICK) closeQuick();
    } catch (e) {
        toast("Could not copy — download it instead");
    }
}

async function itemAction(act, item) {
    try {
        if (act === "copy") { copyItem(item); return; }
        if (!item.id) return;
        const id = encodeURIComponent(item.id);
        if (act === "delete") {
            const r = await api(`/api/history/${id}`, { method: "DELETE" });
            if (r.ok) { items = items.filter((i) => i !== item); render(); toast("Deleted"); }
        } else if (act === "pin") {
            const r = await api(`/api/history/${id}/pin`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ pinned: !item.pinned })
            });
            if (r.ok) { item.pinned = !item.pinned; render(); }
        }
    } catch (e) { toast("Offline — try again in a moment"); }
}

function closeQuick() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.close_window) {
        window.pywebview.api.close_window();
    } else window.close();
}

function renderPill() {
    pillEl.className = "status-pill " + (!engineOn ? "off" : phoneCount > 0 ? "on" : "starting");
    pillEl.textContent = !engineOn ? "Not sharing"
        : phoneCount === 0 ? "Sharing on · No phones"
        : `Sharing on · ${phoneCount} phone${phoneCount === 1 ? "" : "s"}`;
}

async function refreshState() {
    const r = await api("/api/admin/state");
    if (r.status === 403) fatal("Restart the ClipBoardSync app, then reopen this window.");
    const s = await r.json();
    engineOn = s.engine_running !== false;
    phoneCount = s.connection_count || 0;
    currentPin = s.pairing_pin || "";
    deviceNames = {};
    for (const d of s.devices || []) if (d.id) deviceNames[d.id] = d.name || d.id;
    for (const p of s.phones || []) deviceNames["phone:" + p.name] = p.name;
    const fresh = (s.history || []).slice().reverse();
    const optimistic = items.filter((i) => !i.id);
    items = optimistic.concat(fresh);
    renderPill();
    render();
    return s;
}

// Live clips over the loopback socket (plan §4.1).
function connectLive() {
    let ws;
    try {
        ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
    } catch (e) { return; }
    ws.onmessage = (e) => {
        try {
            const msg = JSON.parse(e.data);
            if (!msg || typeof msg !== "object") return;
            if (["text", "image", "file", "folder"].includes(msg.type) && (msg.content || msg.file_url)) {
                const i = items.findIndex((x) => x.id && x.id === msg.id);
                if (i !== -1) items[i] = msg; else items.unshift(msg);
                render();
            }
        } catch (err) { /* ignore malformed */ }
    };
    ws.onclose = () => setTimeout(connectLive, 3000);
}

// --- Connect dialog ---
let connectTimer = null;
async function openConnect() {
    $("connect-dialog").hidden = false;
    await reloadConnect();
    const before = phoneCount;
    connectTimer = setInterval(async () => {
        try {
            const s = await refreshState();
            if (s.pairing_pin && s.pairing_pin !== currentPin) { await reloadConnect(); return; }
            if ((s.connection_count || 0) > before) {
                $("connect-state").textContent = "Connected ✓";
                setTimeout(closeConnect, 1200);
            }
        } catch (e) { /* retry */ }
    }, 2000);
}

function closeConnect() {
    $("connect-dialog").hidden = true;
    if (connectTimer) { clearInterval(connectTimer); connectTimer = null; }
}

async function reloadConnect() {
    const s = await refreshState();
    $("qr-img").src = "/api/admin/qr.png?t=" + encodeURIComponent(TOKEN) + "&_=" + Date.now();
    $("connect-addr").textContent = (s.pairing_url || "").replace(/\?pair=.*$/, "");
    $("pair-code").textContent = s.pairing_pin || "off";
    $("connect-state").textContent = s.require_pin ? "Waiting for phone…" : "Open the address above — no code needed.";
}

// --- Settings drawer ---
async function openDrawer() {
    $("drawer").hidden = false;
    const s = await refreshState();
    const st = s.settings || {};
    $("set-folder").value = st.receive_folder || "";
    $("set-cap").value = Math.round((st.auto_receive_max_bytes ?? 52428800) / 1048576);
    $("set-clipboard").checked = st.put_received_on_clipboard !== false;
    $("set-private").checked = !(st.share_private_clips === true);
    $("set-pin").checked = st.require_pin !== false;
    $("engine-btn").textContent = engineOn ? "Pause sharing" : "Resume sharing";
    const box = $("phones-list");
    box.innerHTML = "";
    const phones = s.phones || [];
    const ids = s.trusted_ids || [];
    if (!phones.length && !ids.length) box.innerHTML = `<p class="muted">No phones paired yet.</p>`;
    for (const p of phones) {
        const row = document.createElement("div");
        row.className = "row";
        row.innerHTML = `<div class="row-body"><p class="row-content">${escapeHtml(p.name)}</p></div>
            <div class="row-actions" style="opacity:1"><button class="mini-btn danger">Remove</button></div>`;
        row.querySelector("button").addEventListener("click", async () => {
            await api(`/api/admin/devices/${encodeURIComponent(p.id)}`, { method: "DELETE" });
            openDrawer();
        });
        box.appendChild(row);
    }
    for (const id of ids) {
        const row = document.createElement("div");
        row.className = "row";
        row.innerHTML = `<div class="row-body"><p class="row-content">${escapeHtml(id)}</p></div>
            <div class="row-actions" style="opacity:1"><button class="mini-btn danger">Remove</button></div>`;
        row.querySelector("button").addEventListener("click", async () => {
            await api(`/api/admin/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
            openDrawer();
        });
        box.appendChild(row);
    }
    $("about-line").textContent = `ClipBoardSync ${s.version || ""}`;
    markThemeButtons();
}

function closeDrawer() { $("drawer").hidden = true; }

// --- Theme: system default, instant swap, no rebuild (plan §4.3) ---
function applyTheme(choice) {
    let theme = choice;
    if (choice === "system") {
        theme = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    }
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("clipboardsync_desktop_theme", choice);
    markThemeButtons();
}

function markThemeButtons() {
    const cur = localStorage.getItem("clipboardsync_desktop_theme") || "system";
    document.querySelectorAll("[data-theme-choice]").forEach((b) =>
        b.classList.toggle("active", b.dataset.themeChoice === cur));
}

// --- Share via shell pickers / dropzone (plan §4.4) ---
async function sharePicked(pickFn) {
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api[pickFn]) {
        toast("Open the desktop app to share files");
        return;
    }
    try {
        const paths = await window.pywebview.api[pickFn]();
        if (!paths || !paths.length) return;
        toast(`Sharing ${paths.length} item(s)…`);
        const r = await api("/api/admin/share", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ paths })
        });
        const data = await r.json();
        toast(data.shared && data.shared.length ? `Shared ${data.shared.length} item(s)` : "Nothing shared");
        refreshState();
    } catch (e) { toast("Could not share those files"); }
}

// --- Wiring ---
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    filter = c.dataset.filter;
    selected = 0;
    render();
}));

searchEl.addEventListener("input", () => { query = searchEl.value.trim(); selected = 0; render(); });
$("settings-btn").addEventListener("click", () => ($("drawer").hidden ? openDrawer() : closeDrawer()));
$("drawer-close").addEventListener("click", closeDrawer);
$("connect-btn").addEventListener("click", openConnect);
$("connect-close").addEventListener("click", closeConnect);
$("copy-link-btn").addEventListener("click", async () => {
    const s = await refreshState();
    if (s.pairing_url) { await copyText(s.pairing_url); toast("Link copied"); }
});
$("new-code-btn").addEventListener("click", async () => {
    await api("/api/admin/pairing/new", { method: "POST" });
    reloadConnect();
});
pillEl.addEventListener("click", () => { if (!engineOn) toggleEngine(); });
$("engine-btn").addEventListener("click", toggleEngine);

async function toggleEngine() {
    const r = await api("/api/admin/engine", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: engineOn ? "stop" : "start" })
    });
    const data = await r.json().catch(() => ({}));
    if (typeof data.running === "boolean") engineOn = data.running;
    else engineOn = !engineOn;
    refreshState();
    openDrawer();
}

$("save-settings").addEventListener("click", async () => {
    const body = {
        receive_folder: $("set-folder").value.trim(),
        auto_receive_max_bytes: Math.max(0, Number($("set-cap").value || 0)) * 1048576,
        put_received_on_clipboard: $("set-clipboard").checked,
        share_private_clips: !$("set-private").checked,
        require_pin: $("set-pin").checked
    };
    const r = await api("/api/admin/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    });
    toast(r.ok ? "Settings saved" : "Could not save settings");
    refreshState();
});

$("test-btn").addEventListener("click", async () => {
    const r = await api("/api/admin/diagnostics");
    const d = await r.json();
    $("diag-out").textContent = JSON.stringify(d, null, 1);
});

$("copy-diag-btn").addEventListener("click", async () => {
    await copyText($("diag-out").textContent || "no diagnostics yet");
    toast("Diagnostics copied");
});

document.querySelectorAll("[data-theme-choice]").forEach((b) =>
    b.addEventListener("click", () => applyTheme(b.dataset.themeChoice)));

const dz = $("dropzone");
["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", () => sharePicked("pick_files"));

document.addEventListener("keydown", (e) => {
    const inInput = /^(INPUT|TEXTAREA)$/.test(document.activeElement && document.activeElement.tagName || "");
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); searchEl.focus(); searchEl.select(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === ",") { e.preventDefault(); $("drawer").hidden ? openDrawer() : closeDrawer(); }
    else if (e.key === "Escape") {
        if (!$("connect-dialog").hidden) closeConnect();
        else if (!$("drawer").hidden) closeDrawer();
        else if (QUICK) closeQuick();
        else if (document.activeElement === searchEl) { searchEl.value = ""; query = ""; render(); }
    } else if (inInput) return;
    else if (e.key === "/") { e.preventDefault(); searchEl.focus(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); selected = Math.min(selected + 1, visibleItems().length - 1); render(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); selected = Math.max(selected - 1, 0); render(); }
    else if (e.key === "Enter") { const v = visibleItems()[selected]; if (v) copyItem(v); }
    else if (e.key.toLowerCase() === "p") { const v = visibleItems()[selected]; if (v) itemAction("pin", v); }
    else if (e.key === "Delete") { const v = visibleItems()[selected]; if (v) itemAction("delete", v); }
});

// Ctrl+V anywhere shares clipboard text/images via the shell (plan §4.4).
document.addEventListener("paste", async (e) => {
    if (e.target === searchEl) return;
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.share_clipboard_now) return;
    e.preventDefault();
    try {
        const done = await window.pywebview.api.share_clipboard_now();
        toast(done ? "Shared from clipboard" : "Clipboard has nothing shareable");
        refreshState();
    } catch (err) { toast("Could not share clipboard"); }
});

applyTheme(localStorage.getItem("clipboardsync_desktop_theme") || "system");
if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => {
        if ((localStorage.getItem("clipboardsync_desktop_theme") || "system") === "system") applyTheme("system");
    });
}

refreshState().then(() => { if (QUICK) searchEl.focus(); }).catch((e) => fatal(String(e && e.message || e)));
connectLive();
setInterval(() => { refreshState().catch(() => {}); }, 5000);
})();
