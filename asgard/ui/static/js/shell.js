(function () {
  "use strict";

  var STORAGE_KEY = "asgard-theme";
  var root = document.getElementById("ag");
  var toggle = document.getElementById("theme-toggle");
  var footer = document.getElementById("runs-footer");
  var list = document.getElementById("runs-menu-list");
  var empty = document.getElementById("runs-menu-empty");

  function currentTheme() {
    if (!root) return "dark";
    return root.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  function syncToggle() {
    if (!toggle) return;
    var light = currentTheme() === "light";
    toggle.setAttribute(
      "aria-label",
      light ? "Switch to dark theme" : "Switch to light theme"
    );
    toggle.title = light ? "Dark theme" : "Light theme";
  }

  function setTheme(theme) {
    if (!root) return;
    if (theme === "light") root.setAttribute("data-theme", "light");
    else root.removeAttribute("data-theme");
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {}
    var params = new URLSearchParams(window.location.search);
    if (theme === "light") params.set("theme", "light");
    else params.delete("theme");
    var query = params.toString();
    var next = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
    window.history.replaceState(null, "", next);
    syncToggle();
  }

  function withTheme(href) {
    try {
      var url = new URL(href, window.location.origin);
      if (currentTheme() === "light") url.searchParams.set("theme", "light");
      else url.searchParams.delete("theme");
      return url.pathname + url.search + url.hash;
    } catch (e) {
      return href;
    }
  }

  function syncNavLinks() {
    document.querySelectorAll(".site-header a[href]").forEach(function (link) {
      var href = link.getAttribute("href");
      if (!href || href.charAt(0) === "#") return;
      link.setAttribute("href", withTheme(href));
    });
  }

  if (toggle) {
    syncToggle();
    syncNavLinks();
    toggle.addEventListener("click", function () {
      setTheme(currentTheme() === "light" ? "dark" : "light");
      syncNavLinks();
    });
  }

  if (!footer || !list) return;

  var params = new URLSearchParams(window.location.search);
  var currentRun = params.get("run") || "";

  function statusLabel(run) {
    if (run.running) return "running";
    if (run.error) return "error";
    if (run.complete) return "complete";
    return "partial";
  }

  function stampOf(run) {
    if (typeof run.stamp === "string" && run.stamp) return run.stamp;
    if (typeof run.id !== "string") return "";
    var parts = run.id.split("--");
    return parts.length > 1 ? parts[parts.length - 1] : "";
  }

  function hrefFor(runId) {
    var path = window.location.pathname || "/";
    var next = new URLSearchParams();
    next.set("run", runId);
    if (currentTheme() === "light") next.set("theme", "light");
    if (path === "/" || path === "/setup") {
      return "/pipeline?" + next.toString();
    }
    return path + "?" + next.toString();
  }

  function render(runs) {
    list.innerHTML = "";
    if (!Array.isArray(runs) || !runs.length) {
      footer.hidden = true;
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    runs.forEach(function (run) {
      if (!run || typeof run.id !== "string") return;
      var li = document.createElement("li");
      var link = document.createElement("a");
      link.href = hrefFor(run.id);
      if (run.id === currentRun) link.className = "on";
      var title = document.createElement("span");
      title.textContent = run.repo || run.id;
      var meta = document.createElement("span");
      meta.className = "runs-footer-meta";
      var stamp = stampOf(run);
      meta.textContent = (stamp ? stamp + " · " : "") + statusLabel(run);
      link.appendChild(title);
      link.appendChild(meta);
      li.appendChild(link);
      list.appendChild(li);
    });
    footer.hidden = false;
  }

  fetch("/api/runs")
    .then(function (response) {
      return response.ok ? response.json() : [];
    })
    .then(render)
    .catch(function () {
      render([]);
    });
})();
