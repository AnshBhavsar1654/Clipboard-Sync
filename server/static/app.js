/**
 * ClipBoardSync Real-Time Frontend Logic
 * Manages WebSocket persistence, clipboard syncing, and responsive UI micro-interactions.
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
    const fileInput = document.getElementById("file-input");
    const imageInput = document.getElementById("image-input");
    const clearBtn = document.getElementById("clear-btn");
    const clearFeedBtn = document.getElementById("clear-feed-btn");
    const clipboardList = document.getElementById("clipboard-list");
    const emptyState = document.getElementById("empty-state");
    const itemCounter = document.getElementById("item-counter");
    const toastContainer = document.getElementById("toast-container");

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
    let itemsCount = 0;
    let isConnecting = false;

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
                updateConnectionStatus("connected");
                showToast("Connected to ClipBoardSync Engine", "success");
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
            statusText.textContent = "Live Bridge Connected";
        } else if (state === "connecting") {
            statusText.textContent = "Connecting to Engine...";
        } else {
            statusText.textContent = "Offline (Reconnecting)";
        }
    }

    // Message Processing
    function handleIncomingMessage(msg) {
        if (msg.type === "history" && Array.isArray(msg.items)) {
            // Render initial history batch
            clipboardList.innerHTML = "";
            itemsCount = 0;
            const sorted = msg.items.slice().reverse();
            sorted.forEach(item => renderClipCard(item, true));
            updateEmptyState();
        } else if ((msg.type === "text" || msg.type === "image" || msg.type === "file") && (msg.content || msg.file_url)) {
            // Real-time incoming clip
            renderClipCard(msg, false);
            updateEmptyState();
            
            if (msg.device_id !== deviceId) {
                const label = msg.type === "image" ? "New Image" : (msg.type === "file" ? "New File" : "New Clip");
                showToast(`${label} from ${formatDeviceName(msg.device_id)}`, "info");
            }
        }
    }

    async function uploadAndTransmitFile(file) {
        if (!file) return;

        showToast(`Uploading ${file.name}...`, "info");

        try {
            const formData = new FormData();
            formData.append("file", file);

            const response = await fetch("/api/upload", {
                method: "POST",
                body: formData
            });

            if (!response.ok) {
                throw new Error(`Upload failed with status ${response.status}`);
            }

            const data = await response.json();
            const itemType = data.type || "file";

            if (!ws || ws.readyState !== WebSocket.OPEN) {
                showToast("Uploaded, but bridge is offline!", "info");
                return;
            }

            const payload = {
                device_id: deviceId,
                timestamp: new Date().toISOString(),
                type: itemType,
                content: itemType === "image" ? data.url : `File: ${data.filename}`,
                filename: data.filename,
                filesize: data.filesize,
                file_url: data.url
            };

            ws.send(JSON.stringify(payload));
            renderClipCard(payload, false);
            updateEmptyState();
            showToast(`Transmitted ${data.filename} to Computer!`, "success");
        } catch (err) {
            console.error("Upload failed:", err);
            showToast("Failed to upload file to computer", "info");
        }
    }

    function transmitClipboard(text) {
        if (!text || !text.trim()) return;

        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showToast("Cannot send: Bridge disconnected!", "info");
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
        renderClipCard(payload, false);
        updateEmptyState();
        showToast("Transmitted to your Computer!", "success");
    }

    // UI Rendering
    function renderClipCard(item, isInitial = false) {
        itemsCount++;
        itemCounter.textContent = `${itemsCount} ${itemsCount === 1 ? 'item' : 'items'}`;
        
        const card = document.createElement("div");
        card.className = "clip-card";
        card.dataset.id = item.id || Date.now();

        const isLaptop = item.device_id && (item.device_id.toLowerCase().includes("win") || item.device_id.toLowerCase().includes("desktop") || item.device_id.toLowerCase().includes("laptop") || item.device_id.length > 25);
        const deviceTagClass = isLaptop ? "laptop" : "phone";
        const deviceIcon = isLaptop ? "💻" : "📱";
        const deviceName = formatDeviceName(item.device_id);
        
        const timeFormatted = formatTime(item.timestamp);
        const itemType = item.type || "text";

        let bodyHtml = "";
        let actionBtnHtml = "";

        if (itemType === "image") {
            const imgSrc = item.content && item.content.startsWith("data:image/") ? item.content : (item.file_url || item.content);
            bodyHtml = `
                <div class="card-media-wrap">
                    <img src="${imgSrc}" class="card-image-preview" alt="Synced Screenshot" />
                </div>
            `;
            actionBtnHtml = `
                <a href="${imgSrc}" download="${item.filename || 'synced_image.png'}" class="copy-btn link-btn" target="_blank">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                        <polyline points="7 10 12 15 17 10"></polyline>
                        <line x1="12" y1="15" x2="12" y2="3"></line>
                    </svg>
                    <span>Download Image</span>
                </a>
            `;
        } else if (itemType === "file") {
            const fname = item.filename || "File";
            const fsize = item.filesize ? (item.filesize < 1024*1024 ? `${(item.filesize/1024).toFixed(1)} KB` : `${(item.filesize/(1024*1024)).toFixed(1)} MB`) : "";
            const fileHref = item.file_url || "#";
            bodyHtml = `
                <div class="file-card-box">
                    <div class="file-icon-badge">📁</div>
                    <div class="file-details">
                        <span class="file-name-title">${escapeHtml(fname)}</span>
                        <span class="file-size-subtitle">${fsize}</span>
                    </div>
                </div>
            `;
            actionBtnHtml = `
                <a href="${fileHref}" download="${escapeHtml(fname)}" class="copy-btn link-btn" target="_blank">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                        <polyline points="7 10 12 15 17 10"></polyline>
                        <line x1="12" y1="15" x2="12" y2="3"></line>
                    </svg>
                    <span>Download File</span>
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
                    <span>Copy to Phone</span>
                </button>
            `;
        }

        card.innerHTML = `
            <div class="card-header">
                <span class="device-tag ${deviceTagClass}">
                    <span>${deviceIcon}</span>
                    <span>${deviceName}</span>
                </span>
                <span class="clip-timestamp" title="${item.timestamp || ''}">${timeFormatted}</span>
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

        if (!isInitial) {
            clipboardList.prepend(card);
        } else {
            clipboardList.appendChild(card);
        }
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
            span.textContent = "✓ Copied!";
            showToast("Copied directly to your phone clipboard!", "success");

            setTimeout(() => {
                btn.classList.remove("copied");
                span.textContent = originalText;
            }, 2500);
        } catch (err) {
            console.error("Copy failed:", err);
            showToast("Could not copy automatically. Please hold to copy.", "info");
        }
    }

    function updateEmptyState() {
        if (itemsCount === 0) {
            emptyState.style.display = "flex";
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
            ? `<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="3" fill="none"><polyline points="20 6 9 17 4 12"></polyline></svg>`
            : `<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2.5" fill="none"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`;

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

    // Event Listeners
    uploadImgBtn.addEventListener("click", () => imageInput.click());
    uploadFileBtn.addEventListener("click", () => fileInput.click());

    imageInput.addEventListener("change", (e) => {
        if (e.target.files && e.target.files[0]) {
            uploadAndTransmitFile(e.target.files[0]);
            imageInput.value = "";
        }
    });

    fileInput.addEventListener("change", (e) => {
        if (e.target.files && e.target.files[0]) {
            uploadAndTransmitFile(e.target.files[0]);
            fileInput.value = "";
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
            showToast("Please enter text or choose an image/file first", "info");
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
                    showToast("Your phone clipboard is currently empty", "info");
                }
            } else {
                showToast("Please tap inside the box above and select 'Paste'", "info");
                sendInput.focus();
            }
        } catch (err) {
            console.warn("Clipboard read permission denied or unsupported:", err);
            showToast("Please paste manually into the text box above", "info");
            sendInput.focus();
        }
    });

    clearBtn.addEventListener("click", () => {
        sendInput.value = "";
        sendInput.focus();
    });

    clearFeedBtn.addEventListener("click", () => {
        clipboardList.innerHTML = "";
        itemsCount = 0;
        itemCounter.textContent = "0 items";
        updateEmptyState();
        showToast("Cleared view on this device", "info");
    });

    // Handle Ctrl+Enter in textarea
    sendInput.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            sendBtn.click();
        }
    });

    // Helper functions
    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatDeviceName(id) {
        if (!id) return "Connected Device";
        if (id === deviceId) return "This Phone (You)";
        if (id.startsWith("Phone-") || id.startsWith("Web-")) return `Phone (${id.slice(-5)})`;
        if (id === "server") return "Sync Engine";
        // Likely Windows UUID or Desktop computer
        return `Windows Computer (${id.substring(0, 8)})`;
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
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch (e) {
            return "Just now";
        }
    }

    // Initialize connection and empty state
    updateEmptyState();
    connect();
});
