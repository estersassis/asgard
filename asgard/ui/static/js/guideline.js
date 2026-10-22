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
    mapPrompt: document.getElementById("guideline-map-prompt"),
    reducePrompt: document.getElementById("guideline-reduce-prompt"),
    modeFull: document.getElementById("guideline-mode-full"),
    modeBatch: document.getElementById("guideline-mode-batch"),
    batchSize: document.getElementById("guideline-batch-size"),
    reuse: document.getElementById("guideline-reuse"),
    regenerate: document.getElementById("guideline-regenerate"),
    empty: document.getElementById("guideline-empty"),
    status: document.getElementById("guideline-status"),
    error: document.getElementById("guideline-error"),
    preview: document.getElementById("guideline-preview"),
  };

  var MORE_PRS = 5;

  var state = {
    editing: false,
    open: null,
    openLists: {},
    markdown: "",
    consolidate: [],
    unknown: {},
    repo: "",
    mapPrompt: "",
    reducePrompt: "",
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

  function titleKey(title) {
    return String(title || "")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .replace(/\.+$/, "")
      .trim();
  }

  function pullRequestsIn(text) {
    var found = [];
    var seen = {};
    var pattern = /\[#(\d+)\]\((https:\/\/github\.com\/[^)\s]+\/pull\/\d+)\)|#(\d+)/g;
    var match;
    while ((match = pattern.exec(String(text || "")))) {
      var id = match[1] || match[3];
      if (seen[id]) continue;
      seen[id] = true;
      found.push({ id: id, md: match[1] ? match[0] : "#" + id });
    }
    return found;
  }

  function parseRecommendation(block) {
    var lines = String(block || "").replace(/\s+$/, "").split(/\r?\n/);
    var header = lines[0] || "";
    var match = header.match(/^\*\*REC-\d+\s+[—–-]\s+(.+)\*\*$/);
    if (!match) return null;
    var title = match[1].replace(/\.\s*$/, "").trim();
    var body = [];
    var prs = [];
    var seen = {};
    for (var i = 1; i < lines.length; i += 1) {
      var trimmed = lines[i].trim();
      if (/^Related PRs:/i.test(trimmed)) {
        pullRequestsIn(trimmed).forEach(function (pr) {
          if (seen[pr.id]) return;
          seen[pr.id] = true;
          prs.push(pr);
        });
      } else {
        body.push(lines[i]);
      }
    }
    while (body.length && body[body.length - 1].trim() === "") body.pop();
    return { title: title, body: body.join("\n"), prs: prs, seen: seen };
  }

  function mergeDuplicateRecs(markdown) {
    var number = 0;
    return String(markdown || "")
      .split(/^(?=##\s)/m)
      .map(function (section) {
        var pieces = section.split(/^(?=\*\*REC-\d+)/m);
        if (pieces.length < 2) return section;
        var merged = [];
        var byTitle = {};
        pieces.slice(1).forEach(function (piece) {
          var rec = parseRecommendation(piece);
          if (!rec) {
            merged.push({ raw: piece });
            return;
          }
          var key = titleKey(rec.title);
          var prior = byTitle[key];
          if (!prior) {
            byTitle[key] = rec;
            merged.push(rec);
            return;
          }
          rec.prs.forEach(function (pr) {
            if (prior.seen[pr.id]) return;
            prior.seen[pr.id] = true;
            prior.prs.push(pr);
          });
        });
        var text = pieces[0].replace(/\s*$/, "");
        merged.forEach(function (rec) {
          if (rec.raw) {
            text += "\n\n" + rec.raw.replace(/^\n+|\s+$/g, "");
            return;
          }
          number += 1;
          var label = number < 10 ? "0" + number : String(number);
          var cites = rec.prs
            .map(function (pr) {
              return pr.md;
            })
            .join(", ");
          text += "\n\n**REC-" + label + " — " + rec.title + ".**";
          if (rec.body) text += "\n" + rec.body;
          if (cites) text += "\nRelated PRs: (" + cites + ")";
        });
        return text + "\n";
      })
      .join("");
  }

  function renderMarkdown(md) {
    var lines = String(md || "").split(/\r?\n/);
    var html = [];
    var para = [];
    var list = [];
    var listSeq = 0;

    function flushPara() {
      if (!para.length) return;
      var text = para.join(" ").trim();
      if (text) html.push("<p>" + inlineFormat(text) + "</p>");
      para = [];
    }

    var related = "";

    function relatedHtml(body) {
      var text = String(body || "").trim();
      if (text.charAt(0) === "(" && text.charAt(text.length - 1) === ")") {
        text = text.slice(1, -1).trim();
      }
      if (!text) return "";
      var formatted = inlineFormat(text);
      var count = (text.match(/#\d+/g) || []).length;
      if (count <= MORE_PRS) {
        return (
          '<div class="related-prs"><span class="related-label">Related PRs:</span> ' +
          formatted +
          "</div>"
        );
      }
      listSeq += 1;
      var id = String(listSeq);
      var open = state.openLists[id] ? " open" : "";
      return (
        '<div class="related-prs"><span class="related-label">Related PRs:</span> ' +
        '<details class="more-prs" data-list="' +
        id +
        '"' +
        open +
        "><summary>(mais prs...)</summary>" +
        '<div class="more-prs-box">' +
        formatted +
        "</div></details></div>"
      );
    }

    function takeCites(text) {
      var linked = text.match(
        /\s*\((\[#\d+\]\([^)]+\)(?:,\s*\[#\d+\]\([^)]+\))*)\)\s*$/
      );
      if (linked) {
        return { text: text.slice(0, linked.index).trim(), cites: linked[1] };
      }
      var raw = text.match(/\s*\((#\d+(?:,\s*#\d+)*)\)\s*$/);
      if (raw) {
        return { text: text.slice(0, raw.index).trim(), cites: raw[1] };
      }
      return { text: text, cites: "" };
    }

    function flushList() {
      if (!list.length) return;
      html.push(
        "<ul>" +
          list
            .map(function (item) {
              return "<li>" + inlineFormat(item) + "</li>";
            })
            .join("") +
          "</ul>"
      );
      if (related) {
        html.push(relatedHtml(related));
        related = "";
      }
      list = [];
    }

    function flush() {
      flushPara();
      flushList();
    }

    lines.forEach(function (line) {
      var trimmed = line.trim();
      if (/^###\s+/.test(line)) {
        flush();
        html.push("<h3>" + inlineFormat(line.replace(/^###\s+/, "")) + "</h3>");
      } else if (/^##\s+/.test(line)) {
        flush();
        html.push("<h2>" + inlineFormat(line.replace(/^##\s+/, "")) + "</h2>");
      } else if (/^#\s+/.test(line)) {
        flush();
        html.push("<h1>" + inlineFormat(line.replace(/^#\s+/, "")) + "</h1>");
      } else if (/^---+\s*$/.test(trimmed)) {
        flush();
        html.push("<hr>");
      } else if (/^[-*]\s+/.test(trimmed)) {
        flushPara();
        var split = takeCites(trimmed.replace(/^[-*]\s+/, ""));
        if (split.text) list.push(split.text);
        if (split.cites) related = split.cites;
      } else if (/^Related PRs:/i.test(trimmed)) {
        flushList();
        html.push(relatedHtml(trimmed.replace(/^Related PRs:\s*/i, "")));
      } else if (/^\*\*REC-/.test(trimmed)) {
        flush();
        html.push("<p>" + inlineFormat(trimmed) + "</p>");
      } else if (trimmed === "") {
        flush();
      } else {
        flushList();
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
    var host =
      button.closest(".related-prs") ||
      button.closest("li") ||
      button.closest("p") ||
      button.parentNode;
    if (!host || !host.parentNode) return;
    var anchor = host.tagName === "LI" ? host.parentNode : host;
    if (!anchor || !anchor.parentNode) return;
    var block = document.createElement("div");
    block.innerHTML = evidenceBlock(state.open, button.getAttribute("data-href"));
    var node = block.firstChild;
    if (node) anchor.parentNode.insertBefore(node, anchor.nextSibling);
  }

  function renderPreview() {
    var doc = splitDoc(state.markdown);
    var html = replaceCitations(renderMarkdown(mergeDuplicateRecs(doc.body)));
    if (doc.footer) {
      html += '<div class="ms-footer">' + escapeHtml(doc.footer) + "</div>";
    }
    els.preview.innerHTML = html;
    els.preview.querySelectorAll("details.more-prs").forEach(function (details) {
      var id = details.getAttribute("data-list");
      details.addEventListener("toggle", function () {
        if (!id) return;
        state.openLists[id] = details.open;
      });
    });
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

    els.edit.textContent = state.editing ? "Close prompts" : "Edit prompts";
    els.edit.setAttribute("aria-expanded", state.editing ? "true" : "false");
    els.promptPanel.hidden = !state.editing;

    var base = "/api/runs/" + encodeURIComponent(runId);
    if (state.markdown) {
      els.download.hidden = false;
      els.download.href = "#";
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
    return api("").then(function (status) {
      if (status.ok && status.body && status.body.running) {
        state.regenerating = true;
        setError("");
        setStatus("Run in progress…");
        updateChrome();
        return waitDone().then(function (done) {
          if (done.body && done.body.error) {
            setError(done.body.error);
            setStatus("");
          } else {
            setStatus("");
          }
          state.regenerating = false;
          updateChrome();
          return loadFiles();
        });
      }
      return loadFiles();
    });
  }

  function loadFiles() {
    if (!runId) {
      els.empty.hidden = false;
      return;
    }

    return Promise.all([
      api("/SECURITY.md"),
      api("/consolidate/output"),
      api("/security_md/output"),
      api("/config"),
      api("/synthesize/prompts"),
    ]).then(function (results) {
      var md = results[0];
      var consolidate = results[1];
      var securityMd = results[2];
      var config = results[3];
      var prompts = results[4];

      state.consolidate =
        consolidate.ok && Array.isArray(consolidate.body) ? consolidate.body : [];
      state.unknown = {};
      if (securityMd.ok && securityMd.body && Array.isArray(securityMd.body.unknown_citations)) {
        securityMd.body.unknown_citations.forEach(function (id) {
          state.unknown[String(id)] = true;
        });
      }
      state.repo =
        config.ok && config.body && typeof config.body.repo === "string" ? config.body.repo : "";
      var batchSize =
        config.ok && config.body ? config.body.synthesize_batch_size : null;
      if (typeof batchSize === "number" && batchSize >= 1) {
        els.modeBatch.checked = true;
        els.batchSize.value = String(batchSize);
      } else if (els.modeFull) {
        els.modeFull.checked = true;
      }
      syncBatchSize();
      if (prompts.ok && prompts.body && typeof prompts.body === "object") {
        state.mapPrompt =
          typeof prompts.body.map === "string" ? prompts.body.map : "";
        state.reducePrompt =
          typeof prompts.body.reduce === "string" ? prompts.body.reduce : "";
      } else {
        state.mapPrompt = "";
        state.reducePrompt = "";
      }
      if (state.editing || !els.mapPrompt.value) {
        els.mapPrompt.value = state.mapPrompt;
      }
      if (state.editing || !els.reducePrompt.value) {
        els.reducePrompt.value = state.reducePrompt;
      }

      if (!md.ok) {
        state.markdown = "";
        els.empty.hidden = false;
        els.empty.textContent =
          md.status === 404
            ? "SECURITY.md is not ready for this run yet."
            : "Could not load SECURITY.md.";
        updateChrome();
        if (els.error.hidden) setError("");
        return;
      }

      state.markdown = md.text || "";
      els.empty.hidden = true;
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

    var mapText = els.mapPrompt.value;
    var reduceText = els.reducePrompt.value;
    var strategy;
    try {
      strategy = readStrategy();
    } catch (err) {
      state.regenerating = false;
      setStatus("");
      setError(err && err.message ? err.message : "Batch size must be at least 1.");
      updateChrome();
      return;
    }
    api("/regenerate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        from: "synthesize",
        map_prompt: mapText,
        reduce_prompt: reduceText,
        map_mode: strategy.map_mode,
        map_batch_size: strategy.map_batch_size,
      }),
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
        state.mapPrompt = mapText;
        state.reducePrompt = reduceText;
        state.open = null;
        return loadFiles();
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

  function shownMarkdown() {
    var doc = splitDoc(state.markdown);
    var body = foldRelated(mergeDuplicateRecs(doc.body));
    if (body.charAt(body.length - 1) !== "\n") body += "\n";
    if (!doc.footer) return body;
    return body.replace(/\s*$/, "") + "\n\n---\n\n" + doc.footer.trim() + "\n";
  }

  function foldRelated(markdown) {
    return String(markdown).replace(/^Related PRs:\s*(.+)\s*$/gm, function (_line, rest) {
      var text = rest.trim();
      if (text.charAt(0) === "(" && text.charAt(text.length - 1) === ")") {
        text = text.slice(1, -1).trim();
      }
      var count = (text.match(/#\d+/g) || []).length;
      if (count <= MORE_PRS) return "Related PRs: (" + text + ")";
      return (
        "Related PRs:\n\n<details>\n<summary>(mais prs...)</summary>\n\n(" +
        text +
        ")\n\n</details>"
      );
    });
  }

  function downloadShown(event) {
    if (event) event.preventDefault();
    if (!state.markdown) return;
    var blob = new Blob([shownMarkdown()], { type: "text/markdown;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "SECURITY.md";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function syncBatchSize() {
    if (!els.batchSize || !els.modeBatch) return;
    els.batchSize.disabled = !els.modeBatch.checked;
  }

  function readStrategy() {
    var batch = els.modeBatch && els.modeBatch.checked;
    if (!batch) return { map_mode: "full", map_batch_size: null };
    var size = parseInt(String(els.batchSize.value || ""), 10);
    if (!Number.isFinite(size) || size < 1) {
      throw new Error("Batch size must be at least 1.");
    }
    return { map_mode: "batch", map_batch_size: size };
  }

  if (els.modeFull) els.modeFull.addEventListener("change", syncBatchSize);
  if (els.modeBatch) els.modeBatch.addEventListener("change", syncBatchSize);

  els.edit.addEventListener("click", function () {
    if (!runId) return;
    state.editing = !state.editing;
    if (state.editing) {
      if (!els.mapPrompt.value && state.mapPrompt) {
        els.mapPrompt.value = state.mapPrompt;
      }
      if (!els.reducePrompt.value && state.reducePrompt) {
        els.reducePrompt.value = state.reducePrompt;
      }
    }
    updateChrome();
  });

  els.regenerate.addEventListener("click", regenerate);
  if (els.download) els.download.addEventListener("click", downloadShown);

  updateChrome();
  load();
})();
