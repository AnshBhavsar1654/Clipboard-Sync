/* Theme bootstrap: runs before first paint to avoid a flash.
 * Kept in its own file (no inline <script>) so the Content-Security-Policy
 * can stay `script-src 'self'`. */
(function () {
    try {
        var t = localStorage.getItem("clipboardsync_theme");
        if (t !== "dark" && t !== "light") {
            t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";
        }
        document.documentElement.setAttribute("data-theme", t);
    } catch (e) {
        document.documentElement.setAttribute("data-theme", "dark");
    }
})();
