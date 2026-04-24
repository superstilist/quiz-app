(function () {
    var key = "quiz_theme";
    var root = document.documentElement;

    function detectInitialTheme() {
        var saved = localStorage.getItem(key);
        if (saved === "light" || saved === "dark") {
            return saved;
        }
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    function applyTheme(theme) {
        root.setAttribute("data-theme", theme);
        localStorage.setItem(key, theme);
        var btn = document.getElementById("theme-toggle");
        if (btn) {
            btn.textContent = theme === "dark" ? "Light Theme" : "Dark Theme";
        }
    }

    function ensureToggleButton() {
        if (document.getElementById("theme-toggle")) {
            return;
        }
        var btn = document.createElement("button");
        btn.id = "theme-toggle";
        btn.className = "theme-toggle";
        btn.type = "button";
        btn.addEventListener("click", function () {
            var current = root.getAttribute("data-theme") || "light";
            applyTheme(current === "dark" ? "light" : "dark");
        });
        document.body.appendChild(btn);
    }

    function init() {
        applyTheme(detectInitialTheme());
        ensureToggleButton();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
