(function () {
  "use strict";

  var root = document.getElementById("forge");
  if (!root) return;

  var UPLOAD_VALUE = "__upload__";
  var ROLES = [
    { id: "classifier", label: "Classifier" },
    { id: "evaluator", label: "Evaluator" },
    { id: "synthesizer", label: "Synthesizer" },
  ];

  var els = {
    tabs: document.getElementById("forge-tabs"),
    version: document.getElementById("forge-version"),
    save: document.getElementById("forge-save"),
    meta: document.getElementById("forge-meta"),
    prompt: document.getElementById("forge-prompt"),
    pilotControls: document.getElementById("forge-pilot-controls"),
    pilotHint: document.getElementById("forge-pilot-hint"),
    pilotSet: document.getElementById("forge-pilot-set"),
    upload: document.getElementById("forge-upload"),
    run: document.getElementById("forge-run"),
    result: document.getElementById("forge-result"),
    f1Label: document.getElementById("forge-f1-label"),
    kappaLabel: document.getElementById("forge-kappa-label"),
    f1Active: document.getElementById("forge-f1-active"),
    f1Candidate: document.getElementById("forge-f1-candidate"),
    kappaActive: document.getElementById("forge-kappa-active"),
    kappaCandidate: document.getElementById("forge-kappa-candidate"),
    detailsToggle: document.getElementById("forge-details-toggle"),
    details: document.getElementById("forge-details"),
    accActive: document.getElementById("forge-acc-active"),
    accCandidate: document.getElementById("forge-acc-candidate"),
    precActive: document.getElementById("forge-prec-active"),
    precCandidate: document.getElementById("forge-prec-candidate"),
    recActive: document.getElementById("forge-rec-active"),
    recCandidate: document.getElementById("forge-rec-candidate"),
    f1RowActive: document.getElementById("forge-f1-row-active"),
    f1RowCandidate: document.getElementById("forge-f1-row-candidate"),
    kappaRowActive: document.getElementById("forge-kappa-row-active"),
    kappaRowCandidate: document.getElementById("forge-kappa-row-candidate"),
    gateAccuracy: document.getElementById("forge-gate-accuracy"),
    gatePrecision: document.getElementById("forge-gate-precision"),
    gateRecall: document.getElementById("forge-gate-recall"),
    gateF1: document.getElementById("forge-gate-f1"),
    gateKappa: document.getElementById("forge-gate-kappa"),
    cmTp: document.getElementById("forge-cm-tp"),
    cmFn: document.getElementById("forge-cm-fn"),
    cmFp: document.getElementById("forge-cm-fp"),
    cmTn: document.getElementById("forge-cm-tn"),
    passLine: document.getElementById("forge-pass-line"),
    config: document.getElementById("forge-config"),
    gateStatus: document.getElementById("forge-gate-status"),
    reject: document.getElementById("forge-reject"),
    promote: document.getElementById("forge-promote"),
    status: document.getElementById("forge-status"),
    error: document.getElementById("forge-error"),
  };

  var state = {
    role: "classifier",
    versions: [],
    activeIds: { classifier: "default", evaluator: "default", synthesizer: "default" },
    selectedId: "",
    loadedBody: "",
    pilotSets: [],
    metrics: null,
    pilotRunId: null,
    pilotOutputDir: null,
    running: false,
    detailsOpen: false,
    decided: null,
  };

  function api(path, options) {
    return fetch(path, options || {}).then(function (response) {
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
    });
  }

  function detailMessage(body, fallback) {
    if (!body) return fallback;
    if (typeof body === "string") return body;
    if (typeof body.detail === "string") return body.detail;
    return fallback;
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

  function shortSha(sha) {
    var text = String(sha || "");
    if (text.length <= 12) return text;
    return text.slice(0, 4) + "…" + text.slice(-4);
  }

  function formatDate(value) {
    if (!value) return "";
    var text = String(value);
    if (text.length >= 10) return text.slice(0, 10);
    return text;
  }

  function formatScore(value) {
    if (typeof value !== "number" || !isFinite(value)) return "—";
    return value.toFixed(2);
  }

  function formatGateLabel(minimum) {
    if (minimum === null || minimum === undefined) return "—";
    return Number(minimum).toFixed(2);
  }

  function parseGateInput(input) {
    var raw = String(input.value || "").trim();
    if (!raw || raw === "—" || raw.toLowerCase() === "none") return null;
    var value = Number(raw);
    if (!isFinite(value)) return null;
    return value;
  }

  function readThresholds() {
    return {
      f1_min: parseGateInput(els.gateF1),
      kappa_min: parseGateInput(els.gateKappa),
      accuracy_min: parseGateInput(els.gateAccuracy),
      precision_min: parseGateInput(els.gatePrecision),
      recall_min: parseGateInput(els.gateRecall),
    };
  }

  function applyThresholdLabels(thresholds) {
    var f1 = thresholds && thresholds.f1_min != null ? thresholds.f1_min : 0.9;
    var kappa = thresholds && thresholds.kappa_min != null ? thresholds.kappa_min : 0.8;
    els.f1Label.textContent = "F1 · gate ≥ " + formatGateLabel(f1);
    els.kappaLabel.textContent = "Cohen’s κ · gate ≥ " + formatGateLabel(kappa);
  }

  function configQuery() {
    var path = els.config.value.trim();
    return path ? "?config=" + encodeURIComponent(path) : "";
  }

  function isClassifier() {
    return state.role === "classifier";
  }

  function canPromote() {
    return !!(
      state.metrics &&
      state.metrics.gates &&
      state.metrics.gates.can_promote === true &&
      state.decided !== "rejected" &&
      state.decided !== "promoted"
    );
  }

  function updatePromote() {
    els.promote.disabled = !canPromote() || !els.config.value.trim() || state.running;
  }

  function updateRoleChrome() {
    var classifier = isClassifier();
    els.pilotControls.hidden = !classifier;
    els.pilotHint.hidden = classifier;
    if (!classifier) {
      els.result.hidden = true;
    } else if (state.metrics) {
      els.result.hidden = false;
    }
    els.run.disabled = state.running || !classifier || !state.selectedId;
  }

  function updateTabs() {
    els.tabs.querySelectorAll(".tab").forEach(function (tab) {
      var on = tab.getAttribute("data-role") === state.role;
      tab.classList.toggle("on", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function renderVersionOptions() {
    var activeId = state.activeIds[state.role] || "default";
    els.version.innerHTML = "";
    state.versions.forEach(function (version) {
      var option = document.createElement("option");
      option.value = version.id;
      var tag = version.id === activeId ? "active" : "candidate";
      option.textContent = version.name + " · " + tag;
      els.version.appendChild(option);
    });
    if (state.selectedId) {
      els.version.value = state.selectedId;
    }
    if (!els.version.value && state.versions.length) {
      els.version.value = state.versions[0].id;
      state.selectedId = els.version.value;
    }
  }

  function renderMeta(version) {
    if (!version) {
      els.meta.textContent = "";
      return;
    }
    var parts = ["sha256 " + shortSha(version.sha256)];
    var date = formatDate(version.created_at);
    if (date) parts.push(date);
    if (version.notes) parts.push("“" + version.notes + "”");
    els.meta.textContent = parts.join(" · ");
  }

  function renderPilotSets() {
    var previous = els.pilotSet.value;
    els.pilotSet.innerHTML = "";
    state.pilotSets.forEach(function (set) {
      var option = document.createElement("option");
      option.value = set.id;
      var label = set.name || set.id;
      label += " · " + set.item_count + " PR" + (set.item_count === 1 ? "" : "s");
      if (set.bundled) label += " (example)";
      option.textContent = label;
      els.pilotSet.appendChild(option);
    });
    var upload = document.createElement("option");
    upload.value = UPLOAD_VALUE;
    upload.textContent = "Upload pilot set…";
    els.pilotSet.appendChild(upload);
    if (previous && previous !== UPLOAD_VALUE) {
      els.pilotSet.value = previous;
    }
    if (!els.pilotSet.value && state.pilotSets.length) {
      els.pilotSet.value = state.pilotSets[0].id;
    }
  }

  function setCandidateClass(node, failed) {
    node.classList.remove("is-pass", "is-fail");
    if (failed === true) node.classList.add("is-fail");
    else if (failed === false) node.classList.add("is-pass");
  }

  function failureSet(gates) {
    var set = {};
    if (gates && Array.isArray(gates.failures)) {
      gates.failures.forEach(function (name) {
        set[name] = true;
      });
    }
    return set;
  }

  function gateStatusText() {
    if (state.decided === "promoted") {
      return { text: "✓ Promoted. Next runs use this prompt.", cls: "is-pass" };
    }
    if (state.decided === "rejected") {
      return { text: "Rejected. The current prompt stays active.", cls: "" };
    }
    if (!state.metrics || !state.metrics.gates) {
      return { text: "", cls: "" };
    }
    var gates = state.metrics.gates;
    if (gates.can_promote) {
      return { text: "✓ Clears every gate", cls: "is-pass" };
    }
    var failures = Array.isArray(gates.failures) ? gates.failures : [];
    if (!failures.length) {
      return { text: "✗ Gates failed — cannot be promoted", cls: "is-fail" };
    }
    return {
      text: "✗ " + failures.join(", ") + " below gate — cannot be promoted",
      cls: "is-fail",
    };
  }

  function renderMetrics() {
    var metrics = state.metrics;
    if (!metrics) {
      els.result.hidden = true;
      updatePromote();
      return;
    }

    els.result.hidden = !isClassifier();
    var active = metrics.active || {};
    var candidate = metrics.candidate || {};
    var single = metrics.candidate_single_pass || {};
    var gates = metrics.gates || {};
    var thresholds = gates.thresholds || {};
    var failed = failureSet(gates);
    var confusion = metrics.confusion_candidate || (candidate.confusion || {});

    applyThresholdLabels(thresholds);

    els.f1Active.textContent = formatScore(active.f1);
    els.f1Candidate.textContent = formatScore(candidate.f1);
    setCandidateClass(els.f1Candidate, failed.f1 ? true : gates.can_promote ? false : null);

    els.kappaActive.textContent = formatScore(active.kappa);
    els.kappaCandidate.textContent = formatScore(candidate.kappa);
    setCandidateClass(
      els.kappaCandidate,
      failed.kappa ? true : gates.can_promote ? false : null
    );

    els.accActive.textContent = formatScore(active.accuracy);
    els.accCandidate.textContent = formatScore(candidate.accuracy);
    els.precActive.textContent = formatScore(active.precision);
    els.precCandidate.textContent = formatScore(candidate.precision);
    els.recActive.textContent = formatScore(active.recall);
    els.recCandidate.textContent = formatScore(candidate.recall);
    els.f1RowActive.textContent = formatScore(active.f1);
    els.f1RowCandidate.textContent = formatScore(candidate.f1);
    els.kappaRowActive.textContent = formatScore(active.kappa);
    els.kappaRowCandidate.textContent = formatScore(candidate.kappa);
    els.kappaRowCandidate.classList.toggle("is-fail", !!failed.kappa);

    if (thresholds.f1_min != null) els.gateF1.value = formatGateLabel(thresholds.f1_min);
    if (thresholds.kappa_min != null) els.gateKappa.value = formatGateLabel(thresholds.kappa_min);
    els.gateAccuracy.value =
      thresholds.accuracy_min != null ? formatGateLabel(thresholds.accuracy_min) : "—";
    els.gatePrecision.value =
      thresholds.precision_min != null ? formatGateLabel(thresholds.precision_min) : "—";
    els.gateRecall.value =
      thresholds.recall_min != null ? formatGateLabel(thresholds.recall_min) : "—";

    els.cmTp.textContent = confusion.tp != null ? String(confusion.tp) : "—";
    els.cmFn.textContent = confusion.fn != null ? String(confusion.fn) : "—";
    els.cmFp.textContent = confusion.fp != null ? String(confusion.fp) : "—";
    els.cmTn.textContent = confusion.tn != null ? String(confusion.tn) : "—";
    els.cmFn.classList.toggle("is-fail", Number(confusion.fn) > 0);
    els.cmFp.classList.toggle("is-fail", Number(confusion.fp) > 0);

    els.passLine.textContent =
      "Single pass F1 " +
      formatScore(single.f1) +
      " · with evaluator " +
      formatScore(candidate.f1);

    var status = gateStatusText();
    els.gateStatus.textContent = status.text;
    els.gateStatus.className = "forge-gate-status" + (status.cls ? " " + status.cls : "");

    updatePromote();
  }

  function setDetailsOpen(open) {
    state.detailsOpen = open;
    els.details.hidden = !open;
    els.detailsToggle.setAttribute("aria-expanded", open ? "true" : "false");
    els.detailsToggle.textContent = open ? "− Hide details" : "+ All metrics and gates";
  }

  function loadPromptBody(versionId) {
    if (!versionId) {
      els.prompt.value = "";
      state.loadedBody = "";
      renderMeta(null);
      return Promise.resolve();
    }
    return api(
      "/api/prompts/" + encodeURIComponent(state.role) + "/" + encodeURIComponent(versionId)
    ).then(function (result) {
      if (!result.ok) {
        throw new Error(detailMessage(result.body, "Could not load prompt."));
      }
      state.selectedId = versionId;
      state.loadedBody = result.body.body || "";
      els.prompt.value = state.loadedBody;
      renderMeta(result.body);
      updateRoleChrome();
    });
  }

  function loadVersions() {
    return api("/api/prompts?role=" + encodeURIComponent(state.role)).then(function (result) {
      if (!result.ok) {
        throw new Error(detailMessage(result.body, "Could not list prompts."));
      }
      state.versions = Array.isArray(result.body) ? result.body : [];
      var activeId = state.activeIds[state.role] || "default";
      var preferred = state.selectedId;
      if (!preferred || !state.versions.some(function (v) { return v.id === preferred; })) {
        preferred = activeId;
      }
      if (!state.versions.some(function (v) { return v.id === preferred; }) && state.versions.length) {
        preferred = state.versions[0].id;
      }
      state.selectedId = preferred || "";
      renderVersionOptions();
      return loadPromptBody(state.selectedId);
    });
  }

  function loadActive() {
    return api("/api/prompts/active" + configQuery()).then(function (result) {
      if (result.ok && result.body && typeof result.body === "object") {
        state.activeIds = {
          classifier: result.body.classifier || "default",
          evaluator: result.body.evaluator || "default",
          synthesizer: result.body.synthesizer || "default",
        };
      }
    });
  }

  function loadPilotSets() {
    return api("/api/pilot/sets").then(function (result) {
      if (!result.ok) {
        throw new Error(detailMessage(result.body, "Could not list pilot sets."));
      }
      state.pilotSets = Array.isArray(result.body) ? result.body : [];
      renderPilotSets();
    });
  }

  function saveVersion() {
    var name = window.prompt("Version name");
    if (name === null) return;
    name = String(name).trim();
    if (!name) {
      setError("Version name is required.");
      return;
    }
    var notes = window.prompt("Notes (optional)", "") || "";
    setError("");
    setStatus("Saving…");
    api("/api/prompts/" + encodeURIComponent(state.role), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name,
        body: els.prompt.value,
        notes: String(notes).trim(),
      }),
    })
      .then(function (result) {
        if (!result.ok) {
          throw new Error(detailMessage(result.body, "Save failed."));
        }
        state.selectedId = result.body.id;
        state.loadedBody = result.body.body || els.prompt.value;
        setStatus("Saved " + result.body.name + ".");
        return loadVersions();
      })
      .catch(function (err) {
        setStatus("");
        setError(err && err.message ? err.message : "Save failed.");
      });
  }

  function waitPilotDone(runId) {
    return api("/api/pilot/runs/" + encodeURIComponent(runId)).then(function (result) {
      if (!result.ok) {
        throw new Error(detailMessage(result.body, "Pilot status failed."));
      }
      if (result.body && (result.body.running || result.body.status === "running")) {
        return new Promise(function (resolve) {
          window.setTimeout(function () {
            resolve(waitPilotDone(runId));
          }, 400);
        });
      }
      return result;
    });
  }

  function runPilot() {
    if (!isClassifier() || state.running) return;
    if (!state.selectedId) {
      setError("Select a candidate version first.");
      return;
    }
    if (els.prompt.value !== state.loadedBody) {
      setError("Save as a new version before running the pilot.");
      return;
    }

    var pilotSet = els.pilotSet.value;
    if (!pilotSet || pilotSet === UPLOAD_VALUE) {
      setError("Choose a pilot set.");
      return;
    }

    var config = els.config.value.trim();
    if (!config) {
      setError(
        "Set a config path before running the pilot (asgard.toml or a run's config.json)."
      );
      return;
    }

    var payload = {
      candidate: state.selectedId,
      active: state.activeIds.classifier || "default",
      pilot_set: pilotSet,
      config: config,
      thresholds: readThresholds(),
    };

    state.running = true;
    state.decided = null;
    state.metrics = null;
    state.pilotOutputDir = null;
    updateRoleChrome();
    updatePromote();
    setError("");
    setStatus("Running pilot…");

    api("/api/pilot/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (result) {
        if (!result.ok) {
          throw new Error(detailMessage(result.body, "Pilot start failed."));
        }
        state.pilotRunId = result.body.id;
        state.pilotOutputDir = result.body.output_dir || null;
        return waitPilotDone(result.body.id);
      })
      .then(function (status) {
        if (status.body && status.body.error) {
          throw new Error(status.body.error);
        }
        if (status.body && status.body.status === "error") {
          throw new Error(status.body.error || "Pilot failed.");
        }
        return api("/api/pilot/runs/" + encodeURIComponent(state.pilotRunId) + "/metrics");
      })
      .then(function (metrics) {
        if (!metrics.ok) {
          throw new Error(detailMessage(metrics.body, "Metrics not ready."));
        }
        state.metrics = metrics.body;
        renderMetrics();
        setStatus("Pilot finished.");
      })
      .catch(function (err) {
        state.metrics = null;
        renderMetrics();
        setStatus("");
        setError(err && err.message ? err.message : "Pilot failed.");
      })
      .then(function () {
        state.running = false;
        updateRoleChrome();
        updatePromote();
      });
  }

  function promote() {
    if (!canPromote()) return;
    var config = els.config.value.trim();
    if (!config) {
      setError("Set a config path before promoting.");
      return;
    }
    var payload = {
      candidate: state.selectedId,
      config: config,
      role: "classifier",
    };
    if (state.pilotOutputDir) {
      payload.from_result = String(state.pilotOutputDir).replace(/\/$/, "") + "/metrics.json";
    }
    setError("");
    setStatus("Promoting…");
    api("/api/pilot/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (result) {
        if (!result.ok) {
          throw new Error(detailMessage(result.body, "Promote failed."));
        }
        state.decided = "promoted";
        state.activeIds.classifier = state.selectedId;
        renderVersionOptions();
        renderMetrics();
        setStatus("Promoted. Next runs use this prompt.");
        return loadActive().then(loadVersions);
      })
      .catch(function (err) {
        setStatus("");
        setError(err && err.message ? err.message : "Promote failed.");
      });
  }

  function reject() {
    state.decided = "rejected";
    state.metrics = null;
    state.pilotRunId = null;
    state.pilotOutputDir = null;
    els.result.hidden = true;
    renderMetrics();
    var activeId = state.activeIds.classifier || "default";
    if (state.selectedId !== activeId) {
      state.selectedId = activeId;
      renderVersionOptions();
      loadPromptBody(activeId).catch(function (err) {
        setError(err && err.message ? err.message : "Could not reload active prompt.");
      });
    }
    setStatus("Rejected. The current prompt stays active.");
    setError("");
    updatePromote();
  }

  function switchRole(role) {
    if (role === state.role) return;
    state.role = role;
    state.selectedId = "";
    state.decided = null;
    updateTabs();
    updateRoleChrome();
    setError("");
    setStatus("");
    loadVersions().catch(function (err) {
      setError(err && err.message ? err.message : "Could not load prompts.");
    });
  }

  function handlePilotSetChange() {
    if (els.pilotSet.value !== UPLOAD_VALUE) return;
    els.upload.value = "";
    els.upload.click();
    if (state.pilotSets.length) {
      els.pilotSet.value = state.pilotSets[0].id;
    } else {
      els.pilotSet.value = "";
    }
  }

  function handleUpload(event) {
    var file = event.target.files && event.target.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      var payload;
      try {
        payload = JSON.parse(String(reader.result || ""));
      } catch (_err) {
        setError("Pilot set must be valid JSON.");
        return;
      }
      setError("");
      setStatus("Uploading pilot set…");
      api("/api/pilot/sets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
        .then(function (result) {
          if (!result.ok) {
            throw new Error(detailMessage(result.body, "Upload failed."));
          }
          var uploadedId = result.body.id;
          return loadPilotSets().then(function () {
            els.pilotSet.value = uploadedId;
            setStatus("Uploaded " + uploadedId + ".");
          });
        })
        .catch(function (err) {
          setStatus("");
          setError(err && err.message ? err.message : "Upload failed.");
        });
    };
    reader.readAsText(file);
  }

  els.tabs.addEventListener("click", function (event) {
    var tab = event.target.closest(".tab");
    if (!tab) return;
    switchRole(tab.getAttribute("data-role"));
  });

  els.version.addEventListener("change", function () {
    state.selectedId = els.version.value;
    loadPromptBody(state.selectedId).catch(function (err) {
      setError(err && err.message ? err.message : "Could not load prompt.");
    });
  });

  els.save.addEventListener("click", saveVersion);
  els.run.addEventListener("click", runPilot);
  els.reject.addEventListener("click", reject);
  els.promote.addEventListener("click", promote);
  els.detailsToggle.addEventListener("click", function () {
    setDetailsOpen(!state.detailsOpen);
  });
  els.pilotSet.addEventListener("change", handlePilotSetChange);
  els.upload.addEventListener("change", handleUpload);
  els.config.addEventListener("change", function () {
    loadActive()
      .then(function () {
        renderVersionOptions();
        updatePromote();
      })
      .catch(function () {
        updatePromote();
      });
  });
  els.config.addEventListener("input", updatePromote);

  function prefillConfigFromRun() {
    var runId = (root.getAttribute("data-run") || "").trim();
    if (!runId || els.config.value.trim()) return Promise.resolve();
    return api("/api/runs/" + encodeURIComponent(runId)).then(function (result) {
      if (!result.ok || !result.body || !result.body.output_dir) return;
      els.config.value = String(result.body.output_dir).replace(/\/?$/, "") + "/config.json";
    });
  }

  applyThresholdLabels({ f1_min: 0.9, kappa_min: 0.8 });
  setDetailsOpen(false);
  updateTabs();
  updateRoleChrome();
  updatePromote();

  prefillConfigFromRun()
    .catch(function () {
      /* ignore missing run */
    })
    .then(function () {
      return Promise.all([loadActive(), loadPilotSets()]).then(loadVersions);
    })
    .catch(function (err) {
      setError(err && err.message ? err.message : "Could not load Forge.");
    });
})();
