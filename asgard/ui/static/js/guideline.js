(function () {
  "use strict";

  var root = document.getElementById("guideline");
  if (!root) return;

  var params = new URLSearchParams(window.location.search);
  var runId = params.get("run") || root.getAttribute("data-run") || "";

  var els = {
    label: document.getElementById("guideline-label"),
    edit: document.getElementById("guideline-edit"),
    bundle: document.getElementById("guideline-bundle"),
    download: document.getElementById("guideline-download"),
    promptPanel: document.getElementById("guideline-prompt-panel"),
    prompt: document.getElementById("guideline-prompt"),
    reuse: document.getElementById("guideline-reuse"),
    regenerate: document.getElementById("guideline-regenerate"),
    empty: document.getElementById("guideline-empty"),
    status: document.getElementById("guideline-status"),
    error: document.getElementById("guideline-error"),
    preview: document.getElementById("guideline-preview"),
  };

  var state = {
    editing: false,
    open: null,
    markdown: "",
    consolidate: [],
    unknown: {},
    repo: "",
    prompt: "",
    regenerating: false,
  };

  function api(path, options) {
    return fetch("/api/runs/" + encodeURIComponent(runId) + path, options || {}).then(
      function (response) {
        return response.text().then(function (text) {
          var body = text;
          var type = response.headers.get("content-type") || "";
          if (text && type.indexOf("json") >= 0) {
            try {
              body = JSON.parse(text);
            } catch (_err) {
              body = text;
            }
          }
          return { ok: response.ok, status: response.status, body: body, text: text };
        });
      }
    );
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function inlineFormat(text) {
    var escaped = escapeHtml(text);
    escaped = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    escaped = escaped.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (_m, label, href) {
      if (!/^https?:\/\//i.test(href)) return escapeHtml(label);
      var safe = href.replace(/"/g, "");
      return '<a href="' + safe + '">' + label + "</a>";
    });
    return escaped;
  }

  function renderMarkdown(md) {
    var lines = String(md || "").split(/\r?\n/);
    var html = [];
    var para = [];

    function flush() {
      if (!para.length) return;
      var text = para.join(" ").trim();
      if (text) html.push("<p>" + inlineFormat(text) + "</p>");
      para = [];
    }

    lines.forEach(function (line) {
      if (/^###\s+/.test(line)) {
        flush();
        html.push("<h3>" + inlineFormat(line.replace(/^###\s+/, "")) + "</h3>");
      } else if (/^##\s+/.test(line)) {
        flush();
        html.push("<h2>" + inlineFormat(line.replace(/^##\s+/, "")) + "</h2>");
      } else if (/^#\s+/.test(line)) {
        flush();
        html.push("<h1>" + inlineFormat(line.replace(/^#\s+/, "")) + "</h1>");
      } else if (/^---+\s*$/.test(line)) {
        flush();
        html.push("<hr>");
      } else if (line.trim() === "") {
        flush();
      } else {
        para.push(line);
      }
    });
    flush();
    return html.join("");
  }

  function splitDoc(md) {
    var text = String(md || "");
    var marker = "\n---\n";
    var index = text.lastIndexOf(marker);
    if (index < 0) return { body: text, footer: "" };
    return {
      body: text.slice(0, index),
      footer: text.slice(index + marker.length).replace(/^\n+/, "").trimEnd(),
    };
  }

  function citeHtml(href, id) {
    var warn = state.unknown[id] ? " warn" : "";
    var open = state.open === id;
    return (
      '<button type="button" class="cite' +
      warn +
      '" data-pr="' +
      escapeHtml(id) +
      '" data-href="' +
      escapeHtml(href) +
      '" aria-expanded="' +
      (open ? "true" : "false") +
      '">#' +
      escapeHtml(id) +
      "</button>"
    );
  }

  function replaceCitations(html) {
    return html.replace(
      /<a href="(https:\/\/github\.com\/[^"]+\/pull\/(\d+))">#\2<\/a>/g,
      function (_m, href, id) {
        return citeHtml(href, id);
      }
    );
  }

  function evidenceFor(prId) {
    for (var i = 0; i < state.consolidate.length; i += 1) {
      var row = state.consolidate[i];
      if (String(row.pr_id) === String(prId)) return row;
    }
    return null;
  }

  function evidenceBlock(prId, href) {
    var row = evidenceFor(prId);
    var parts = ['<div class="cite-block" data-for="' + escapeHtml(prId) + '">'];
    if (row && Array.isArray(row.evidence) && row.evidence.length) {
      row.evidence.forEach(function (item) {
        var quote = item && item.quote ? item.quote : "";
        var location = item && item.location ? item.location : "";
        parts.push(
          "<span>“" +
            escapeHtml(quote) +
            "”" +
            (location ? " — " + escapeHtml(location) : "") +
            "</span>"
        );
      });
    } else if (state.unknown[prId]) {
      parts.push(
        "<span>Not in the approved security set. Kept as a link and reported as a citation warning.</span>"
      );
    } else {
      parts.push("<span>No evidence quotes for #" + escapeHtml(prId) + ".</span>");
    }
    var link = href || (row && row.link) || "";
    if (link) {
      parts.push(
        '<a href="' +
          escapeHtml(link) +
          '" target="_blank" rel="noopener noreferrer">Open #' +
          escapeHtml(prId) +
          " on GitHub ↗</a>"
      );
    }
    parts.push("</div>");
    return parts.join("");
  }

  function insertOpenEvidence() {
    if (!state.open) return;
    var button = els.preview.querySelector('.cite[data-pr="' + state.open + '"]');
    if (!button) return;
    var host = button.closest("p") || button.parentNode;
    if (!host || !host.parentNode) return;
    var block = document.createElement("div");
    block.innerHTML = evidenceBlock(state.open, button.getAttribute("data-href"));
    var node = block.firstChild;
    if (node) host.parentNode.insertBefore(node, host.nextSibling);
  }

  function renderPreview() {
    var doc = splitDoc(state.markdown);
    var html = replaceCitations(renderMarkdown(doc.body));
    if (doc.footer) {
      html += '<div class="ms-footer">' + escapeHtml(doc.footer) + "</div>";
    }
    els.preview.innerHTML = html;
    insertOpenEvidence();
    els.preview.querySelectorAll(".cite").forEach(function (button) {
      button.addEventListener("click", function () {
        var id = button.getAttribute("data-pr");
        state.open = state.open === id ? null : id;
        renderPreview();
      });
    });
  }

  function setError(message) {
    if (!message) {
      els.error.hidden = true;
      els.error.textContent = "";
      return;
    }
    els.error.hidden = false;
    els.error.textContent = message;
  }

  function setStatus(message) {
    if (!message) {
      els.status.hidden = true;
      els.status.textContent = "";
      return;
    }
    els.status.hidden = false;
    els.status.textContent = message;
  }

  function updateChrome() {
    var label = "SECURITY.md";
    if (state.repo) label += " · " + state.repo;
    els.label.textContent = label;

    var n = state.consolidate.length;
    els.reuse.textContent =
      "Reuses the " + n + " approved PR" + (n === 1 ? "" : "s") + ". No classification is repeated.";

    els.edit.textContent = state.editing ? "Close prompt" : "Edit prompt";
    els.edit.setAttribute("aria-expanded", state.editing ? "true" : "false");
    els.promptPanel.hidden = !state.editing;

    var base = "/api/runs/" + encodeURIComponent(runId);
    if (state.markdown) {
      els.download.hidden = false;
      els.download.href = base + "/SECURITY.md";
      els.download.setAttribute("download", "SECURITY.md");
      els.bundle.hidden = false;
      els.bundle.href = base + "/bundle.zip";
      els.bundle.setAttribute("download", "bundle.zip");
      els.preview.hidden = false;
      els.empty.hidden = true;
    } else {
      els.download.hidden = true;
      els.bundle.hidden = true;
      els.preview.hidden = true;
    }

    els.regenerate.disabled = state.regenerating;
  }

  function load() {
    if (!runId) {
      els.empty.hidden = false;
      return;
    }

    return Promise.all([
      api("/SECURITY.md"),
      api("/consolidate/output"),
      api("/security_md/output"),
      api("/config"),
      api("/synthesize/prompt"),
    ]).then(function (results) {
      var md = results[0];
      var consolidate = results[1];
      var securityMd = results[2];
      var config = results[3];
      var prompt = results[4];

      if (!md.ok) {
        els.empty.hidden = false;
        els.empty.textContent =
          md.status === 404
            ? "SECURITY.md is not ready for this run yet."
            : "Could not load SECURITY.md.";
        return;
      }

      state.markdown = md.text || "";
      state.consolidate = consolidate.ok && Array.isArray(consolidate.body) ? consolidate.body : [];
      state.unknown = {};
      if (securityMd.ok && securityMd.body && Array.isArray(securityMd.body.unknown_citations)) {
        securityMd.body.unknown_citations.forEach(function (id) {
          state.unknown[String(id)] = true;
        });
      }
      state.repo =
        config.ok && config.body && typeof config.body.repo === "string" ? config.body.repo : "";
      state.prompt = prompt.ok ? prompt.text || "" : "";
      els.prompt.value = state.prompt;

      updateChrome();
      renderPreview();
      setError("");
    });
  }

  function waitDone() {
    return api("").then(function (result) {
      if (result.ok && result.body && result.body.running) {
        return new Promise(function (resolve) {
          window.setTimeout(function () {
            resolve(waitDone());
          }, 250);
        });
      }
      return result;
    });
  }

  function regenerate() {
    if (!runId || state.regenerating) return;
    state.regenerating = true;
    setError("");
    setStatus("Regenerating from synthesize…");
    updateChrome();

    var text = els.prompt.value;
    api("/regenerate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from: "synthesize", prompt: text }),
    })
      .then(function (result) {
        if (!result.ok) {
          var detail =
            result.body && result.body.detail ? result.body.detail : "Regenerate failed.";
          throw new Error(typeof detail === "string" ? detail : "Regenerate failed.");
        }
        return waitDone();
      })
      .then(function (status) {
        if (status.body && status.body.error) {
          throw new Error(status.body.error);
        }
        state.prompt = text;
        state.open = null;
        return load();
      })
      .then(function () {
        setStatus("Guideline updated.");
      })
      .catch(function (err) {
        setError(err && err.message ? err.message : "Regenerate failed.");
        setStatus("");
      })
      .then(function () {
        state.regenerating = false;
        updateChrome();
      });
  }

  els.edit.addEventListener("click", function () {
    if (!runId) return;
    state.editing = !state.editing;
    if (state.editing && !els.prompt.value && state.prompt) {
      els.prompt.value = state.prompt;
    }
    updateChrome();
  });

  els.regenerate.addEventListener("click", regenerate);

  updateChrome();
  load();
})();
