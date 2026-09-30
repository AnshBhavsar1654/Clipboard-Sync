/**
 * ClipBoardSync Real-Time Frontend Logic
 * Manages WebSocket persistence, clipboard syncing, searchable feed, and
 * responsive UI micro-interactions. Follows DESIGN.md (vector icons, clean
 * list, keyboard-first search).
 */

document.addEventListener("DOMContentLoaded", () => {
    // UI Elements
    const connectionBadge = document.getElementById("connection-badge");
    const statusText = document.getElementById("status-text");
    const sendInput = document.getElementById("send-input");
    const sendBtn = document.getElementById("send-btn");
    const pasteAndSendBtn = document.getElementById("paste-and-send-btn");
    const uploadImgBtn = document.getElementById("upload-img-btn");
    const uploadFileBtn = document.getElementById("upload-file-btn");
    const uploadFolderBtn = document.getElementById("upload-folder-btn");
    const fileInput = document.getElementById("file-input");
    const imageInput = document.getElementById("image-input");
    const folderInput = document.getElementById("folder-input");
    const uploadProgress = document.getElementById("upload-progress");
    const uploadProgressBar = document.getElementById("upload-progress-bar");
    const uploadProgressText = document.getElementById("upload-progress-text");
    const uploadProgressPct = document.getElementById("upload-progress-pct");
    const clearBtn = document.getElementById("clear-btn");
    const clearFeedBtn = document.getElementById("clear-feed-btn");
    const clipboardList = document.getElementById("clipboard-list");
    const emptyState = document.getElementById("empty-state");
    const itemCounter = document.getElementById("item-counter");
    const toastContainer = document.getElementById("toast-container");
    const feedSearch = document.getElementById("feed-search");

    // Auth / pairing + theme elements
    const authScreen = document.getElementById("auth-screen");
    const appBody = document.getElementById("app-body");
    const pinInput = document.getElementById("pin-input");
    const pinSubmit = document.getElementById("pin-submit");
    const pinError = document.getElementById("pin-error");
    const themeToggle = document.getElementById("theme-toggle");
    const themeToggleIcon = document.getElementById("theme-toggle-icon");

    // Device Identification Setup
    let deviceId = localStorage.getItem("clipboardsync_device_id");
    if (!deviceId) {
        const platform = (navigator.userAgent.includes("Mobi") || navigator.userAgent.includes("Android") || navigator.userAgent.includes("iPhone"))
            ? "Phone" : "Web";
        deviceId = `${platform}-${Math.random().toString(36).substring(2, 7).toUpperCase()}`;
        localStorage.setItem("clipboardsync_device_id", deviceId);
    }

    // Dynamic WebSocket Connection setup
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    let ws = null;
    let reconnectDelay = 1000;
    const maxReconnectDelay = 15000;
    let isConnecting = false;

    // Feed state: canonical items (newest first) + currently visible subset
    let feedItems = [];
    let visibleItems = [];

    // Connect WebSocket
    function connect() {
        if (isConnecting || (ws && ws.readyState === WebSocket.OPEN)) return;
        isConnecting = true;
        updateConnectionStatus("connecting");

        try {
            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                isConnecting = false;
                reconnectDelay = 1000;
                updateConnectionStatus("connecting");
                showToast("Connected — ready to share", "success");
                ws.send(JSON.stringify({ type: "auth_request", device_id: deviceId }));
            };

            ws.onmessage = (event) => {
                try {
                    const message = JSON.parse(event.data);
                    handleIncomingMessage(message);
                } catch (err) {
                    console.error("Failed to parse incoming WebSocket message:", err);
                }
            };

            ws.onclose = () => {
                isConnecting = false;
                updateConnectionStatus("disconnected");
                scheduleReconnect();
            };

            ws.onerror = (err) => {
                console.warn("WebSocket encountered an error:", err);
                if (ws.readyState !== WebSocket.CLOSED) {
                    ws.close();
                }
            };
        } catch (err) {
            isConnecting = false;
            updateConnectionStatus("disconnected");
            scheduleReconnect();
        }
    }

    function scheduleReconnect() {
        updateConnectionStatus("disconnected");
        setTimeout(() => {
            connect();
        }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, maxReconnectDelay);
    }

    function updateConnectionStatus(state) {
        connectionBadge.className = `badge ${state}`;
        if (state === "connected") {
            statusText.textContent = "Sharing on";
        } else if (state === "connecting") {
            statusText.textContent = "Connecting…";
        } else {
            statusText.textContent = "Trying to reconnect…";
        }
    }

    // Message Processing
    function handleIncomingMessage(msg) {
        if (msg.type === "auth_required") {
            showAuthScreen();
            updateConnectionStatus("connecting");
            return;
        }
        if (msg.type === "auth_error") {
            updateConnectionStatus("connecting");
            showPinError(msg.message || "That code did not match. Try again.");
            return;
        }
        if (msg.type === "auth_success") {
            hideAuthScreen();
            updateConnectionStatus("connected");
            if (Array.isArray(msg.items)) {
                feedItems = msg.items.slice().reverse(); // newest first
                itemCounter.textContent = `${feedItems.length} ${feedItems.length === 1 ? "item" : "items"}`;
                applyFilter();
            }
            return;
        }
        if (msg.type === "history" && Array.isArray(msg.items)) {
            hideAuthScreen();
            updateConnectionStatus("connected");
            feedItems = msg.items.slice().reverse(); // newest first
            itemCounter.textContent = `${feedItems.length} ${feedItems.length === 1 ? "item" : "items"}`;
            applyFilter();
        } else if ((msg.type === "text" || msg.type === "image" || msg.type === "file" || msg.type === "folder") && (msg.content || msg.file_url)) {
            feedItems.unshift(msg);
            itemCounter.textContent = `${feedItems.length} ${feedItems.length === 1 ? "item" : "items"}`;
            applyFilter();

            if (msg.device_id !== deviceId) {
                const label = msg.type === "image" ? "New photo" : (msg.type === "file" ? "New file" : (msg.type === "folder" ? "New folder" : "New clip"));
                showToast(`${label} from ${formatDeviceName(msg.device_id)}`, "info");
            }
        }
    }

    function showUploadProgress(name, loaded, total) {
        if (!uploadProgress) return;
        uploadProgress.hidden = false;
        const pct = total > 0 ? Math.round((loaded / total) * 100) : 0;
        uploadProgressBar.style.width = `${pct}%`;
        uploadProgressText.textContent = `Sharing ${name}…`;
        uploadProgressPct.textContent = total > 0 ? `${pct}%` : `${Math.round(loaded / 1024)} KB`;
    }

    function hideUploadProgress() {
        if (!uploadProgress) return;
        uploadProgress.hidden = true;
        uploadProgressBar.style.width = "0%";
    }

    function postFormWithProgress(url, formData, fileName, totalBytes) {
        // XHR (not fetch) so big files show a progress bar + can be cancelled.
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", url);
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) showUploadProgress(fileName, e.loaded, e.total);
                else showUploadProgress(fileName, e.loaded, totalBytes);
            };
            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    try { resolve(JSON.parse(xhr.responseText)); }
                    catch (err) { reject(new Error("Bad server reply")); }
                } else if (xhr.status === 413) {
                    reject(new Error("Too big (over 2 GB)"));
                } else {
                    reject(new Error(`Upload failed (${xhr.status})`));
                }
            };
            xhr.onerror = () => reject(new Error("Connection lost"));
            xhr.onabort = () => reject(new Error("Cancelled"));
            xhr.send(formData);
        });
    }

    // Minimal stored-zip writer (offline-safe, no CDN). Good enough for
    // folder sharing on a LAN with no internet access.
    function crc32(bytes) {
        let table = crc32._t;
        if (!table) {
            table = new Uint32Array(256);
            for (let n = 0; n < 256; n++) {
                let c = n;
                for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
                table[n] = c >>> 0;
            }
            crc32._t = table;
        }
        let crc = 0xFFFFFFFF;
        for (let i = 0; i < bytes.length; i++) crc = table[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8);
        return (crc ^ 0xFFFFFFFF) >>> 0;
    }

    function zipStore(fileList) {
        // fileList: [{name, data: Uint8Array}]. Returns Blob (.zip, stored).
        const enc = new TextEncoder();
        const chunks = [];
        const central = [];
        let offset = 0;
        for (const f of fileList) {
            const nameBytes = enc.encode(f.name);
            const crc = crc32(f.data);
            const local = new DataView(new ArrayBuffer(30));
            local.setUint32(0, 0x04034b50, true);
            local.setUint16(4, 20, true);
            local.setUint16(6, 0x0800, true); // UTF-8 names
            local.setUint16(8, 0, true); // stored
            local.setUint16(10, 0, true); local.setUint16(12, 0, true);
            local.setUint32(14, crc, true);
            local.setUint32(18, f.data.length, true);
            local.setUint32(22, f.data.length, true);
            local.setUint16(26, nameBytes.length, true);
            local.setUint16(28, 0, true);
            chunks.push(local.buffer, nameBytes.buffer, f.data.buffer);
            central.push({ nameBytes, crc, size: f.data.length, offset });
            offset += 30 + nameBytes.length + f.data.length;
        }
        const cdStart = offset;
        let cdSize = 0;
        for (const c of central) {
            const h = new DataView(new ArrayBuffer(46));
            h.setUint32(0, 0x02014b50, true);
            h.setUint16(4, 20, true); h.setUint16(6, 20, true);
            h.setUint16(8, 0x0800, true); h.setUint16(10, 0, true);
            h.setUint16(12, 0, true); h.setUint16(14, 0, true);
            h.setUint32(16, c.crc, true);
            h.setUint32(20, c.size, true); h.setUint32(24, c.size, true);
            h.setUint16(28, c.nameBytes.length, true);
            h.setUint16(30, 0, true); h.setUint16(32, 0, true);
            h.setUint16(34, 0, true); h.setUint16(36, 0, true);
            h.setUint32(38, 0, true);
            h.setUint32(42, c.offset, true);
            chunks.push(h.buffer, c.nameBytes.buffer);
            cdSize += 46 + c.nameBytes.length;
        }
        const end = new DataView(new ArrayBuffer(22));
        end.setUint32(0, 0x06054b50, true);
        end.setUint16(8, central.length, true);
        end.setUint16(10, central.length, true);
        end.setUint32(12, cdSize, true);
        end.setUint32(16, cdStart, true);
        end.setUint16(20, 0, true);
        chunks.push(end.buffer);
        return new Blob(chunks, { type: "application/zip" });
    }

    async function readAsBytes(file) {
        const buf = await file.arrayBuffer();
        return new Uint8Array(buf);
    }

    async function uploadFolderAsZip(fileList, folderName) {
        const files = Array.from(fileList || []).filter(f => f && f.size >= 0);
        if (!files.length) {
            showToast("That folder is empty", "info");
            return;
        }
        const totalBytes = files.reduce((n, f) => n + (f.size || 0), 0);
        if (totalBytes > 1024 * 1024 * 1024) {
            showToast("That folder is too big to share from a phone (over 1 GB)", "info");
            return;
        }
        showToast(`Zipping ${files.length} files…`, "info");
        const SKIP = ["__MACOSX", ".DS_Store"];
        const entries = [];
        let skipped = 0;
        for (const f of files) {
            const rel = (f.webkitRelativePath || f.name || "").replace(/^\/+/, "");
            if (!rel || SKIP.some(s => rel.includes(s))) { skipped++; continue; }
            entries.push({ name: rel, data: await readAsBytes(f) });
        }
        if (!entries.length) {
            showToast("Nothing to share after skipping system files", "info");
            return;
        }
        const zipBlob = zipStore(entries);
        const name = `${(folderName || (entries[0].name.split("/")[0]) || "folder")}.zip`;
        const zipFile = new File([zipBlob], name, { type: "application/zip" });
        await uploadAndTransmitFile(zipFile, { itemType: "folder", entryCount: entries.length, skippedCount: skipped });
    }

    async function uploadAndTransmitFile(file, opts = {}) {
        if (!file) return;
        const itemTypeHint = opts.itemType;
        const entryCount = opts.entryCount;
        const skippedCount = opts.skippedCount;

        if ((file.size || 0) > 200 * 1024 * 1024) {
            const ok = window.confirm(`"${file.name}" is ${Math.round(file.size / 1048576)} MB. Share it anyway?`);
            if (!ok) return;
        }
        showUploadProgress(file.name, 0, file.size || 0);

        try {
            const formData = new FormData();
            formData.append("file", file, file.name || "file");
            if (itemTypeHint) formData.append("item_type", itemTypeHint);
            if (entryCount != null) formData.append("entry_count", String(entryCount));
            if (skippedCount != null) formData.append("skipped_count", String(skippedCount));

            const data = await postFormWithProgress("/api/upload", formData, file.name, file.size || 0);
            hideUploadProgress();
            const itemType = data.type || itemTypeHint || "file";

            if (!ws || ws.readyState !== WebSocket.OPEN) {
                showToast("Shared on this phone, but sharing is offline.", "info");
                return;
            }

            const payload = {
                device_id: deviceId,
                timestamp: new Date().toISOString(),
                type: itemType,
                content: itemType === "image" ? data.url : `${itemType === "folder" ? "Folder" : "File"}: ${data.filename}`,
                filename: data.filename,
                filesize: data.filesize,
                file_url: data.url
            };
            if (data.entry_count != null) payload.entry_count = data.entry_count;
            else if (entryCount != null) payload.entry_count = entryCount;
            if (data.skipped_count != null) payload.skipped_count = data.skipped_count;
            else if (skippedCount != null) payload.skipped_count = skippedCount;

            ws.send(JSON.stringify(payload));
            feedItems.unshift(payload);
            itemCounter.textContent = `${feedItems.length} ${feedItems.length === 1 ? "item" : "items"}`;
            applyFilter();
            showToast(`Shared ${data.filename} with your computer`, "success");
        } catch (err) {
            hideUploadProgress();
            console.error("Upload failed:", err);
            const msg = /over 2 GB|too big/i.test(String(err && err.message)) ? String(err.message) : "Could not share that file. Try again.";
            showToast(msg, "info");
        }
    }

    function transmitClipboard(text) {
        if (!text || !text.trim()) return;

        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showToast("Sharing is offline. Try again in a moment.", "info");
            return;
        }

        const payload = {
            device_id: deviceId,
            timestamp: new Date().toISOString(),
            type: "text",
            content: text
        };

        ws.send(JSON.stringify(payload));

        // Optimistically show in our feed
        feedItems.unshift(payload);
        itemCounter.textContent = `${feedItems.length} ${feedItems.length === 1 ? "item" : "items"}`;
        applyFilter();
        showToast("Shared with your computer", "success");
    }

    // Search / Filter
    function matchesQuery(item, q) {
        return [item.content, item.filename, item.device_id, item.type]
            .filter(Boolean)
            .join(" ")
            .toLowerCase()
            .includes(q);
    }

    function applyFilter() {
        const q = feedSearch.value.trim().toLowerCase();
        visibleItems = q ? feedItems.filter(item => matchesQuery(item, q)) : feedItems.slice();
        renderAll(visibleItems);
        updateEmptyState(q, visibleItems.length);
    }

    function renderAll(items) {
        clipboardList.innerHTML = "";
        items.forEach(item => {
            clipboardList.appendChild(buildClipCard(item));
        });
    }

    // UI Rendering
    function deviceIconSvg(isLaptop) {
        return isLaptop
            ? '<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="12" rx="2"></rect><line x1="9" y1="20" x2="15" y2="20"></line><line x1="12" y1="16" x2="12" y2="20"></line></svg>'
            : '<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="2" width="10" height="20" rx="2"></rect><line x1="11" y1="18" x2="13" y2="18"></line></svg>';
    }

    function buildClipCard(item) {
        const card = document.createElement("div");
        card.className = "clip-card";
        card.dataset.id = item.id || Date.now();

        const isLaptop = item.device_id && (item.device_id.toLowerCase().includes("win") || item.device_id.toLowerCase().includes("desktop") || item.device_id.toLowerCase().includes("laptop") || item.device_id.length > 25);
        const deviceTagClass = isLaptop ? "laptop" : "phone";
        const deviceIcon = deviceIconSvg(isLaptop);
        const deviceName = formatDeviceName(item.device_id);

        const timeFormatted = formatTime(item.timestamp);
        const itemType = item.type || "text";

        let bodyHtml = "";
        let actionBtnHtml = "";

        if (itemType === "image") {
            const imgSrc = item.content && item.content.startsWith("data:image/") ? item.content : (item.file_url || item.content);
            bodyHtml = `
                <div class="card-media-wrap">
                    <img src="${imgSrc}" class="card-image-preview" alt="Shared photo" />
                </div>
            `;
            actionBtnHtml = `
                <a href="${imgSrc}" download="${item.filename || "shared_photo.png"}" class="copy-btn link-btn" target="_blank" rel="noopener">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                        <polyline points="7 10 12 15 17 10"></polyline>
                        <line x1="12" y1="15" x2="12" y2="3"></line>
                    </svg>
                    <span>Save photo</span>
                </a>
            `;
        } else if (itemType === "file" || itemType === "folder") {
            const fname = item.filename || (itemType === "folder" ? "Folder.zip" : "File");
            const fsize = item.filesize ? formatSize(item.filesize) : "";
            let sub = fsize;
            if (itemType === "folder" && item.entry_count != null) {
                sub = `${item.entry_count} items${fsize ? ` · ${fsize}` : ""}`;
            }
            if (item.skipped_count) sub += ` · skipped ${item.skipped_count}`;
            const rawHref = item.file_url || "#";
            const fileHref = rawHref === "#" ? "#" : rawHref + (rawHref.includes("?") ? "&" : "?") + `filename=${encodeURIComponent(fname)}`;
            const icon = itemType === "folder"
                ? '<svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path></svg>'
                : '<svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>';
            bodyHtml = `
                <div class="file-card-box">
                    <div class="file-icon-badge">
                        ${icon}
                    </div>
                    <div class="file-details">
                        <span class="file-name-title">${escapeHtml(fname)}</span>
                        <span class="file-size-subtitle">${escapeHtml(sub)}</span>
                    </div>
                </div>
            `;
            actionBtnHtml = `
                <a href="${fileHref}" download="${escapeHtml(fname)}" class="copy-btn link-btn" target="_blank" rel="noopener">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                        <polyline points="7 10 12 15 17 10"></polyline>
                        <line x1="12" y1="15" x2="12" y2="3"></line>
                    </svg>
                    <span>${itemType === "folder" ? "Save folder (.zip)" : "Save file"}</span>
                </a>
            `;
        } else {
            bodyHtml = `<pre class="clip-content">${escapeHtml(item.content)}</pre>`;
            actionBtnHtml = `
                <button class="copy-btn" data-content="${encodeURIComponent(item.content)}">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none">
                        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                    </svg>
                    <span>Copy</span>
                </button>
            `;
        }

        card.innerHTML = `
            <div class="card-header">
                <span class="device-tag ${deviceTagClass}">
                    ${deviceIcon}
                    <span>${deviceName}</span>
                </span>
                <span class="clip-timestamp" title="${item.timestamp || ""}">${timeFormatted}</span>
            </div>
            ${bodyHtml}
            <div class="card-actions">
                ${actionBtnHtml}
            </div>
        `;

        if (itemType === "text") {
            const copyBtn = card.querySelector(".copy-btn");
            if (copyBtn) {
                copyBtn.addEventListener("click", () => handleCopyClick(copyBtn, item.content));
            }
        }

        return card;
    }

    async function handleCopyClick(btn, text) {
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(text);
            } else {
                // Fallback for older WebView or iOS browsers
                const tempInput = document.createElement("textarea");
                tempInput.value = text;
                document.body.appendChild(tempInput);
                tempInput.select();
                document.execCommand("copy");
                document.body.removeChild(tempInput);
            }

            // Visual feedback animation
            const span = btn.querySelector("span");
            const originalText = span.textContent;
            btn.classList.add("copied");
            span.textContent = "Copied";
            showToast("Copied to this phone", "success");

            setTimeout(() => {
                btn.classList.remove("copied");
                span.textContent = originalText;
            }, 2500);
        } catch (err) {
            console.error("Copy failed:", err);
            showToast("Could not copy. Touch and hold the text to copy it.", "info");
        }
    }

    function updateEmptyState(query, count) {
        if (count === 0 && !query) {
            emptyState.style.display = "flex";
            emptyState.querySelector(".empty-title").textContent = "Nothing shared yet";
            emptyState.querySelector(".empty-body").textContent =
                "Copy something on your computer, or share text above — it will appear here right away.";
            clipboardList.style.display = "none";
        } else if (count === 0 && query) {
            emptyState.style.display = "flex";
            emptyState.querySelector(".empty-title").textContent = "No matches";
            emptyState.querySelector(".empty-body").textContent =
                `Nothing matched for that search. Try different words.`;
            clipboardList.style.display = "none";
        } else {
            emptyState.style.display = "none";
            clipboardList.style.display = "flex";
        }
    }

    function showToast(message, type = "info") {
        const toast = document.createElement("div");
        toast.className = `toast ${type}`;

        const iconSvg = type === "success"
            ? '<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="3" fill="none"><polyline points="20 6 9 17 4 12"></polyline></svg>'
            : '<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2.5" fill="none"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>';

        toast.innerHTML = `
            <div class="toast-icon">${iconSvg}</div>
            <div class="toast-text">${escapeHtml(message)}</div>
        `;

        toastContainer.appendChild(toast);

        // Remove after animation finishes
        setTimeout(() => {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 4000);
    }

    // Pairing screen helpers
    function showAuthScreen() {
        authScreen.hidden = false;
        appBody.hidden = true;
        clearPinError();
        setTimeout(() => pinInput.focus(), 50);
    }

    function hideAuthScreen() {
        authScreen.hidden = true;
        appBody.hidden = false;
        clearPinError();
    }

    function showPinError(message) {
        pinError.textContent = message;
        pinError.hidden = false;
    }

    function clearPinError() {
        pinError.hidden = true;
        pinError.textContent = "";
    }

    function submitPin() {
        const pin = pinInput.value.replace(/\D/g, "").slice(0, 6);
        if (pin.length !== 6) {
            showPinError("Enter the 6-digit code shown on your computer.");
            return;
        }
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showPinError("Sharing is offline. Trying to reconnect…");
            return;
        }
        clearPinError();
        ws.send(JSON.stringify({ type: "auth_pin", device_id: deviceId, pin: pin }));
    }

    // Theme helpers
    function currentTheme() {
        return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        themeToggleIcon.innerHTML = theme === "dark"
            ? '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>'
            : '<circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"></path>';
    }

    // Event Listeners
    uploadImgBtn.addEventListener("click", () => imageInput.click());
    uploadFileBtn.addEventListener("click", () => fileInput.click());
    if (uploadFolderBtn) {
        const supportsFolders = (() => {
            try { return "webkitdirectory" in document.createElement("input"); }
            catch (e) { return false; }
        })();
        if (!supportsFolders) {
            uploadFolderBtn.disabled = true;
            uploadFolderBtn.title = "This browser can't pick folders — choose File and select many files instead";
        }
        uploadFolderBtn.addEventListener("click", () => {
            if (!supportsFolders) {
                showToast("This browser can't pick folders. Choose File and select many files instead.", "info");
                return;
            }
            folderInput.click();
        });
    }

    imageInput.addEventListener("change", (e) => {
        if (e.target.files && e.target.files[0]) {
            uploadAndTransmitFile(e.target.files[0]);
            imageInput.value = "";
        }
    });

    fileInput.addEventListener("change", async (e) => {
        const picked = Array.from(e.target.files || []);
        fileInput.value = "";
        if (!picked.length) return;
        if (picked.length === 1) {
            uploadAndTransmitFile(picked[0]);
            return;
        }
        // Multiple files picked (iOS fallback): zip into one folder share.
        const total = picked.reduce((n, f) => n + (f.size || 0), 0);
        if (total > 1024 * 1024 * 1024) {
            showToast("Those files are too big to share from a phone (over 1 GB)", "info");
            return;
        }
        showToast(`Zipping ${picked.length} files…`, "info");
        const entries = [];
        for (const f of picked) {
            entries.push({ name: f.name || "file", data: await readAsBytes(f) });
        }
        const zipBlob = zipStore(entries);
        const zipFile = new File([zipBlob], "shared_files.zip", { type: "application/zip" });
        uploadAndTransmitFile(zipFile, { itemType: "folder", entryCount: entries.length, skippedCount: 0 });
    });

    if (folderInput) {
        folderInput.addEventListener("change", async (e) => {
            const list = e.target.files;
            folderInput.value = "";
            if (!list || !list.length) return;
            const first = list[0].webkitRelativePath || "";
            const folderName = first.split("/")[0] || "folder";
            uploadFolderAsZip(list, folderName);
        });
    }

    feedSearch.addEventListener("input", applyFilter);

    pinSubmit.addEventListener("click", submitPin);
    pinInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            submitPin();
        }
    });
    pinInput.addEventListener("input", () => {
        pinInput.value = pinInput.value.replace(/\D/g, "").slice(0, 6);
        clearPinError();
    });

    applyTheme(currentTheme());
    themeToggle.addEventListener("click", () => {
        const next = currentTheme() === "dark" ? "light" : "dark";
        localStorage.setItem("clipboardsync_theme", next);
        applyTheme(next);
        showToast(next === "light" ? "Light theme on" : "Dark theme on", "info");
    });

    document.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
            e.preventDefault();
            feedSearch.focus();
            feedSearch.select();
        } else if (e.key === "Escape" && document.activeElement === feedSearch) {
            feedSearch.value = "";
            applyFilter();
        }
    });

    // Intercept image paste from phone/browser clipboard
    document.addEventListener("paste", (e) => {
        if (e.clipboardData && e.clipboardData.items) {
            const items = e.clipboardData.items;
            for (let i = 0; i < items.length; i++) {
                if (items[i].type.indexOf("image") !== -1) {
                    const blob = items[i].getAsFile();
                    if (blob) {
                        e.preventDefault();
                        uploadAndTransmitFile(blob);
                        return;
                    }
                }
            }
        }
    });

    sendBtn.addEventListener("click", () => {
        const text = sendInput.value;
        if (!text.trim()) {
            showToast("Type something first, or choose a photo or file", "info");
            return;
        }
        transmitClipboard(text);
        sendInput.value = "";
    });

    pasteAndSendBtn.addEventListener("click", async () => {
        try {
            if (navigator.clipboard && navigator.clipboard.read) {
                const items = await navigator.clipboard.read();
                for (const item of items) {
                    for (const type of item.types) {
                        if (type.startsWith("image/")) {
                            const blob = await item.getType(type);
                            uploadAndTransmitFile(blob);
                            return;
                        }
                    }
                }
            }
            if (navigator.clipboard && navigator.clipboard.readText) {
                const clipText = await navigator.clipboard.readText();
                if (clipText && clipText.trim()) {
                    sendInput.value = clipText;
                    transmitClipboard(clipText);
                    sendInput.value = "";
                } else {
                    showToast("Your phone clipboard is empty", "info");
                }
            } else {
                showToast("Tap the box above, then tap Paste", "info");
                sendInput.focus();
            }
        } catch (err) {
            console.warn("Clipboard read permission denied or unsupported:", err);
            showToast("Paste your text into the box above", "info");
            sendInput.focus();
        }
    });

    clearBtn.addEventListener("click", () => {
        sendInput.value = "";
        sendInput.focus();
    });

    clearFeedBtn.addEventListener("click", () => {
        feedItems = [];
        itemCounter.textContent = "0 items";
        applyFilter();
        showToast("Cleared this view. Shared clips stay on your computer.", "info");
    });

    // Handle Ctrl+Enter in textarea
    sendInput.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            sendBtn.click();
        }
    });

    // Helper functions
    function formatSize(bytes) {
        const n = Number(bytes) || 0;
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        if (n < 1024 * 1024 * 1024) return `${(n / 1048576).toFixed(1)} MB`;
        return `${(n / 1073741824).toFixed(2)} GB`;
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatDeviceName(id) {
        if (!id) return "Shared clip";
        if (id === deviceId) return "This phone";
        if (id.startsWith("Phone-") || id.startsWith("Web-")) return "Your phone";
        if (id === "server") return "ClipBoardSync";
        if (/^(PC|Desktop|Computer)-/i.test(id)) return "Other computer";
        // Likely this computer
        return "This computer";
    }

    function formatTime(isoStr) {
        if (!isoStr) return "Just now";
        try {
            const date = new Date(isoStr);
            const now = new Date();
            const diffMs = now - date;
            const diffMin = Math.floor(diffMs / 60000);

            if (diffMin < 1) return "Just now";
            if (diffMin < 60) return `${diffMin}m ago`;
            const diffHours = Math.floor(diffMin / 60);
            if (diffHours < 24) return `${diffHours}h ago`;
            return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        } catch (e) {
            return "Just now";
        }
    }

    // Initialize connection and empty state
    updateEmptyState("", 0);
    connect();
});