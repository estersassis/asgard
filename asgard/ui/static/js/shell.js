(function () {
  "use strict";

  var footer = document.getElementById("runs-footer");
  var list = document.getElementById("runs-menu-list");
  var empty = document.getElementById("runs-menu-empty");
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
    if (path === "/" || path === "/setup") {
      return "/pipeline?run=" + encodeURIComponent(runId);
    }
    return path + "?run=" + encodeURIComponent(runId);
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
