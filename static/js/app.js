(function () {
  "use strict";

  var STORAGE_KEY = "frilans-theme";

  function getStoredTheme() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
  }

  function setStoredTheme(v) {
    try {
      localStorage.setItem(STORAGE_KEY, v);
    } catch (e) {}
  }

  function resolveTheme(pref) {
    if (pref === "system" || !pref) {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    return pref === "dark" ? "dark" : "light";
  }

  function applyDocumentTheme(pref) {
    var html = document.documentElement;
    html.setAttribute("data-theme", pref || "system");
    var resolved = resolveTheme(pref || "system");
    html.setAttribute("data-resolved", resolved);
  }

  function initTheme() {
    var meta = document.querySelector('meta[name="user-theme"]');
    var serverPref = meta && meta.getAttribute("content");
    var stored = getStoredTheme();
    var pref = stored || serverPref || "system";
    applyDocumentTheme(pref);
    syncThemeButtons(pref);

    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      if (cur === "system") applyDocumentTheme("system");
    });
  }

  function syncThemeButtons(pref) {
    document.querySelectorAll(".theme-switch__btn").forEach(function (btn) {
      var t = btn.getAttribute("data-theme-set");
      btn.classList.toggle("is-active", t === pref);
    });
  }

  function postThemeQuick(value) {
    var fd = new FormData();
    fd.append("theme", value);
    fetch("/settings/theme-quick", { method: "POST", body: fd, credentials: "same-origin" }).catch(
      function () {}
    );
  }

  document.querySelectorAll(".theme-switch__btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var v = btn.getAttribute("data-theme-set");
      if (!v) return;
      setStoredTheme(v);
      applyDocumentTheme(v);
      syncThemeButtons(v);
      if (document.querySelector('meta[name="user-theme"]')) postThemeQuick(v);
    });
  });

  initTheme();

  document.querySelectorAll("dialog.action-dialog").forEach(function (dlg) {
    dlg.addEventListener("click", function (e) {
      if (e.target === dlg) dlg.close();
    });
  });

  if ("startViewTransition" in document) {
    document.addEventListener("click", function (event) {
      var anchor = event.target.closest("a");
      if (!anchor) return;
      if (anchor.target === "_blank") return;
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      var href = anchor.getAttribute("href");
      if (!href || href.startsWith("#")) return;
      if (anchor.origin !== window.location.origin) return;
      event.preventDefault();
      var nextUrl = anchor.href;
      document.startViewTransition(function () {
        window.location.href = nextUrl;
      });
    });
  }

  var timerActive = false;
  function checkTimer() {
    fetch("/api/timer", { credentials: "same-origin" })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        timerActive = !!(data && data.active);
      })
      .catch(function () {});
  }
  if (document.querySelector(".timer-active") || document.getElementById("timer-display")) {
    timerActive = true;
  } else {
    checkTimer();
    setInterval(checkTimer, 60000);
  }

  window.addEventListener("beforeunload", function (e) {
    if (timerActive) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
})();
