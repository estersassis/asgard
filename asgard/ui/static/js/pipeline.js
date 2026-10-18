(function () {
  "use strict";

  var STAGES = [
    "mine",
    "normalize",
    "prefilter",
    "classify",
    "consolidate",
    "synthesize",
    "security_md",
    "bundle",
  ];
  var LOG_TAIL = 40;
  var EVENT_TYPES = [
    "StageStarted",
    "StageFinished",
    "PRMined",
    "RateLimited",
    "PRFiltered",
    "ClassifyProgress",
    "Error",
    "ClassificationAttempt",
    "EvaluatorVerdict",
    "LoopFinished",
    "SynthesisChunk",
    "CitationWarning",
  ];

  var params = new URLSearchParams(window.location.search);
  var runId = params.get("run");
  if (!runId) {
    var missing = document.getElementById("pipeline-status");
    if (missing) missing.textContent = "No run selected";
    var runsBox = document.getElementById("pipeline-runs");
    var runList = document.getElementById("pipeline-run-list");
    var stages = document.getElementById("pipeline-stages");
    var controls = document.querySelector(".pipeline-controls");
    if (stages) stages.hidden = true;
    if (controls) controls.hidden = true;
    if (runsBox && runList) {
      fetch("/api/runs")
        .then(function (response) {
          return response.ok ? response.json() : [];
        })
        .then(function (runs) {
          runList.innerHTML = "";
          if (!Array.isArray(runs) || !runs.length) {
            runsBox.hidden = true;
            return;
          }
          runs.forEach(function (run) {
            var li = document.createElement("li");
            var link = document.createElement("a");
            link.href = "/pipeline?run=" + encodeURIComponent(run.id);
            var title = document.createElement("span");
            title.textContent = run.repo || run.id;
            var meta = document.createElement("span");
            meta.className = "pipeline-run-meta";
            var stamp =
              typeof run.stamp === "string" && run.stamp
                ? run.stamp
                : typeof run.id === "string" && run.id.indexOf("--") >= 0
                  ? run.id.split("--").pop()
                  : "";
            var status = run.running
              ? "running"
              : run.error
                ? "error"
                : run.complete
                  ? "complete"
                  : "partial";
            meta.textContent = (stamp ? stamp + " · " : "") + status;
            link.appendChild(title);
            link.appendChild(meta);
            li.appendChild(link);
            runList.appendChild(li);
          });
          runsBox.hidden = false;
        })
        .catch(function () {
          runsBox.hidden = true;
        });
    }
    return;
  }

  var els = {
    status: document.getElementById("pipeline-status"),
    owner: document.getElementById("pipeline-owner"),
    name: document.getElementById("pipeline-name"),
    stages: document.getElementById("pipeline-stages"),
    errors: document.getElementById("pipeline-errors"),
    stop: document.getElementById("pipeline-stop"),
    logToggle: document.getElementById("pipeline-log-toggle"),
    primary: document.getElementById("pipeline-primary"),
    log: document.getElementById("pipeline-log"),
  };

  var stageEls = {};
  STAGES.forEach(function (id) {
    var node = els.stages.querySelector('[data-stage="' + id + '"]');
    if (!node) return;
    stageEls[id] = {
      root: node,
      detail: node.querySelector("[data-detail]"),
      note: node.querySelector("[data-note]"),
      bar: node.querySelector("[data-bar]"),
      fill: node.querySelector("[data-bar-fill]"),
    };
  });

  function emptyStage() {
    return {
      state: "pending",
      total: null,
      count: 0,
      dropped: 0,
      index: null,
      progressTotal: null,
      note: "",
      removed: null,
      startedAt: null,
      endedAt: null,
    };
  }

  var state = {
    running: false,
    stopped: false,
    completed: false,
    sessionError: null,
    firstAt: null,
    prefilterOff: false,
    seen: {},
    stages: {},
    errors: [],
    logLines: [],
    logOpen: false,
    source: null,
    timer: null,
  };

  STAGES.forEach(function (id) {
    state.stages[id] = emptyStage();
  });

  function api(path, options) {
    return fetch("/api/runs/" + encodeURIComponent(runId) + path, options || {}).then(
      function (response) {
        return response.text().then(function (text) {
          var body = text;
          if (text && response.headers.get("content-type") &&
              response.headers.get("content-type").indexOf("json") >= 0) {
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

  function eventKey(event) {
    return JSON.stringify(event);
  }

  function remember(event) {
    var key = eventKey(event);
    if (state.seen[key]) return false;
    state.seen[key] = true;
    return true;
  }

  function parseLog(text) {
    var events = [];
    String(text || "")
      .split("\n")
      .forEach(function (line) {
        line = line.trim();
        if (!line) return;
        try {
          var event = JSON.parse(line);
          if (event && typeof event === "object" && event.type) events.push(event);
        } catch (_err) {
          /* skip bad lines */
        }
      });
    return events;
  }

  function reduce(event, options) {
    if (!event || !event.type) return;
    if (!remember(event)) return;
    var silent = options && options.silent;

    if (event.at && !state.firstAt) state.firstAt = event.at;

    var stage;
    switch (event.type) {
      case "StageStarted":
        stage = state.stages[event.stage];
        if (!stage) break;
        // Drop stale errors from earlier failed attempts of this stage.
        state.errors = state.errors.filter(function (err) {
          return err.stage !== event.stage;
        });
        stage.state = "running";
        stage.total = typeof event.total === "number" ? event.total : null;
        stage.count = 0;
        stage.dropped = 0;
        stage.index = null;
        stage.progressTotal = null;
        stage.note = "";
        stage.removed = null;
        stage.startedAt = event.at || stage.startedAt || null;
        stage.endedAt = null;
        state.stopped = false;
        state.completed = false;
        break;
      case "StageFinished":
        stage = state.stages[event.stage];
        if (!stage) break;
        stage.state = event.skipped ? "skipped" : "done";
        if (event.at) stage.endedAt = event.at;
        if (!stage.startedAt && event.at) stage.startedAt = event.at;
        // A finished stage supersedes its prior Error events in the log.
        state.errors = state.errors.filter(function (err) {
          return err.stage !== event.stage;
        });
        if (event.stage === "prefilter" && !event.skipped && !state.prefilterOff) {
          fetchRemoved();
        }
        break;
      case "PRMined":
        stage = state.stages.mine;
        if (stage) stage.count += 1;
        break;
      case "RateLimited":
        stage = state.stages.mine;
        if (stage) {
          var waited = Number(event.wait_seconds);
          var secs = Number.isFinite(waited)
            ? waited % 1 === 0
              ? String(waited)
              : waited.toFixed(1)
            : "?";
          stage.note = "Waited " + secs + " s for the GitHub rate limit";
        }
        break;
      case "PRFiltered":
        stage = state.stages.prefilter;
        if (stage) stage.dropped += 1;
        break;
      case "ClassifyProgress":
        stage = state.stages.classify;
        if (!stage) break;
        if (typeof event.index === "number") stage.index = event.index;
        if (typeof event.total === "number") stage.progressTotal = event.total;
        break;
      case "Error":
        state.errors.push({
          message: String(event.message || "error"),
          stage: event.stage || null,
        });
        break;
      default:
        break;
    }

    state.logLines.push(event);
    if (!silent) render();
  }

  function interruptedStage() {
    for (var i = 0; i < STAGES.length; i++) {
      var id = STAGES[i];
      var stage = state.stages[id];
      if (stage.state === "running" || stage.state === "stopped") return id;
    }
    for (var j = 0; j < STAGES.length; j++) {
      var sid = STAGES[j];
      var st = state.stages[sid];
      if (st.state !== "done" && st.state !== "skipped") return sid;
    }
    return null;
  }

  function allFinished() {
    return STAGES.every(function (id) {
      var st = state.stages[id];
      return st.state === "done" || st.state === "skipped";
    });
  }

  function formatElapsed(fromIso, toIso) {
    if (!fromIso) return "0:00:00";
    var start = Date.parse(fromIso);
    if (!Number.isFinite(start)) return "0:00:00";
    var end = toIso ? Date.parse(toIso) : Date.now();
    if (!Number.isFinite(end)) end = Date.now();
    var seconds = Math.max(0, Math.floor((end - start) / 1000));
    var hh = Math.floor(seconds / 3600);
    var mm = Math.floor((seconds % 3600) / 60);
    var ss = seconds % 60;
    return (
      hh +
      ":" +
      String(mm).padStart(2, "0") +
      ":" +
      String(ss).padStart(2, "0")
    );
  }

  function stageElapsed(stage) {
    if (!stage || !stage.startedAt) return "";
    if (stage.state === "skipped") return "";
    return formatElapsed(stage.startedAt, stage.endedAt || null);
  }

  function withElapsed(text, stage) {
    var elapsed = stageElapsed(stage);
    if (!elapsed) return text;
    if (!text) return elapsed;
    return text + " · " + elapsed;
  }

  function clock(at) {
    if (!at) return "--:--:--";
    var date = new Date(at);
    if (Number.isNaN(date.getTime())) return "--:--:--";
    return (
      String(date.getUTCHours()).padStart(2, "0") +
      ":" +
      String(date.getUTCMinutes()).padStart(2, "0") +
      ":" +
      String(date.getUTCSeconds()).padStart(2, "0")
    );
  }

  function formatLogEvent(event) {
    var parts = [clock(event.at), event.type];
    if (event.stage) parts.push(event.stage);
    if (event.pr_id) parts.push("pr " + event.pr_id);
    if (event.type === "ClassifyProgress" && event.index != null && event.total != null) {
      parts.push(event.index + " / " + event.total);
    } else if (event.type === "RateLimited" && event.wait_seconds != null) {
      parts.push("waited " + event.wait_seconds + " s");
    } else if (event.type === "EvaluatorVerdict") {
      parts.push(event.approved ? "approved" : "revise");
    } else if (event.type === "Error" && event.message) {
      parts.push(event.message);
    } else if (event.type === "PRFiltered" && event.reason) {
      parts.push(event.reason);
    } else if (event.type === "StageStarted" && event.total != null) {
      parts.push(String(event.total));
    } else if (event.type === "StageFinished") {
      parts.push(event.skipped ? "skipped" : "finished");
    } else if (event.type === "SynthesisChunk" && typeof event.text === "string") {
      parts.push(event.text.length + " chars");
    } else if (event.message) {
      parts.push(event.message);
    }
    return parts.join(" · ");
  }

  function stageDetail(id) {
    var stage = state.stages[id];
    var stoppedMid = !state.running && (stage.state === "running" || stage.state === "stopped");

    if (id === "mine") {
      if (stage.state === "done") return withElapsed("✓ " + stage.count + " PRs", stage);
      if (stage.state === "skipped") return "skipped";
      if (stage.count > 0) return withElapsed(String(stage.count), stage);
      return withElapsed("", stage);
    }

    if (id === "normalize") {
      if (stage.state === "done") {
        if (stage.total != null) return withElapsed("✓ " + stage.total, stage);
        return withElapsed("✓", stage);
      }
      if (stage.state === "skipped") return "skipped";
      return withElapsed("", stage);
    }

    if (id === "prefilter") {
      if (state.prefilterOff) return "off";
      if (stage.removed) {
        var before = stage.removed.before;
        var after = stage.removed.after;
        var dropped = before - after;
        var text = after + " kept · " + dropped + " dropped";
        if (stage.state === "done") return withElapsed("✓ " + text, stage);
        return withElapsed(text, stage);
      }
      if (stage.state === "done" || stage.state === "running" || stage.state === "stopped") {
        if (stage.total != null) {
          var kept = Math.max(0, stage.total - stage.dropped);
          var live = kept + " kept · " + stage.dropped + " dropped";
          return withElapsed(stage.state === "done" ? "✓ " + live : live, stage);
        }
        if (stage.dropped > 0) return withElapsed(stage.dropped + " dropped", stage);
      }
      if (stage.state === "skipped") return "skipped";
      return withElapsed("", stage);
    }

    if (id === "classify") {
      if (stage.index != null && stage.progressTotal != null) {
        var progress = stage.index + " / " + stage.progressTotal;
        if (stoppedMid) return withElapsed("stopped · " + progress, stage);
        if (stage.state === "done") return withElapsed("✓ " + progress, stage);
        return withElapsed(progress, stage);
      }
      if (stage.state === "done") return withElapsed("✓", stage);
      if (stage.state === "skipped") return "skipped";
      if (stoppedMid) return withElapsed("stopped", stage);
      return withElapsed("", stage);
    }

    if (stage.state === "done") return withElapsed("✓", stage);
    if (stage.state === "skipped") return "skipped";
    return withElapsed("", stage);
  }

  function stageUiState(id) {
    var stage = state.stages[id];
    if (!state.running && (stage.state === "running" || stage.state === "stopped")) {
      return "stopped";
    }
    return stage.state;
  }

  function progressFraction(id) {
    var stage = state.stages[id];
    if (id === "classify" && stage.index != null && stage.progressTotal > 0) {
      return Math.min(1, stage.index / stage.progressTotal);
    }
    if (id === "mine" && stage.total != null && stage.total > 0) {
      return Math.min(1, stage.count / stage.total);
    }
    return null;
  }

  function renderStages() {
    STAGES.forEach(function (id) {
      var ui = stageEls[id];
      if (!ui) return;
      var stage = state.stages[id];
      var uiState = stageUiState(id);
      ui.root.setAttribute("data-state", uiState);
      ui.detail.textContent = stageDetail(id);

      if (stage.note) {
        ui.note.hidden = false;
        ui.note.textContent = stage.note;
        ui.note.classList.toggle("is-rate-limit", id === "mine");
      } else {
        ui.note.hidden = true;
        ui.note.textContent = "";
        ui.note.classList.remove("is-rate-limit");
      }

      var showBar = uiState === "running";
      if (!showBar) {
        ui.bar.hidden = true;
        ui.bar.classList.remove("is-indeterminate");
        ui.fill.style.width = "0";
        return;
      }
      ui.bar.hidden = false;
      var fraction = progressFraction(id);
      if (fraction == null) {
        ui.bar.classList.add("is-indeterminate");
        ui.fill.style.width = "";
      } else {
        ui.bar.classList.remove("is-indeterminate");
        ui.fill.style.width = Math.round(fraction * 100) + "%";
      }
    });
  }

  function renderErrors() {
    els.errors.innerHTML = "";
    if (!state.errors.length) {
      els.errors.hidden = true;
      return;
    }
    els.errors.hidden = false;
    state.errors.forEach(function (err, index) {
      var row = document.createElement("div");
      row.className = "pipeline-error";

      var msg = document.createElement("span");
      msg.className = "pipeline-error-msg";
      var prefix =
        state.errors.length === 1
          ? "1 error"
          : index + 1 + "/" + state.errors.length + " errors";
      msg.textContent = prefix + " · " + err.message;

      var retry = document.createElement("button");
      retry.type = "button";
      retry.className = "link";
      retry.textContent = "Retry PR";
      retry.addEventListener("click", function () {
        var from = err.stage || interruptedStage() || "mine";
        resumeFrom(from);
      });

      row.appendChild(msg);
      row.appendChild(retry);
      els.errors.appendChild(row);
    });
  }

  function renderStatus() {
    els.status.classList.remove("is-running", "is-stopped", "live");
    if (state.running) {
      els.status.textContent = "Running · " + formatElapsed(state.firstAt);
      els.status.classList.add("is-running", "live");
      return;
    }
    if (state.stopped || (state.sessionError && !allFinished())) {
      els.status.textContent = "Stopped · finished PRs are saved";
      els.status.classList.add("is-stopped");
      return;
    }
    if (allFinished()) {
      els.status.textContent = "Done · " + formatElapsed(state.firstAt);
      return;
    }
    els.status.textContent = state.sessionError || "—";
  }

  function renderControls() {
    var interrupted = interruptedStage();
    var showStop = state.running;
    els.stop.hidden = !showStop;

    els.primary.hidden = false;
    els.primary.classList.add("btn", "pri");
    els.primary.onclick = null;

    if (state.running) {
      els.primary.textContent = "Watch the council";
      els.primary.setAttribute("href", "/council?run=" + encodeURIComponent(runId));
      return;
    }

    if (interrupted && (state.stopped || state.sessionError || !allFinished())) {
      els.primary.textContent = "Resume from " + interrupted;
      els.primary.setAttribute("href", "#");
      els.primary.onclick = function (event) {
        event.preventDefault();
        resumeFrom(interrupted);
      };
      return;
    }

    els.primary.textContent = "Watch the council";
    els.primary.setAttribute("href", "/council?run=" + encodeURIComponent(runId));
  }

  function renderLog() {
    els.logToggle.setAttribute("aria-expanded", state.logOpen ? "true" : "false");
    els.logToggle.textContent = state.logOpen ? "Hide log" : "Show log";
    els.log.hidden = !state.logOpen;
    if (!state.logOpen) return;

    var tail = state.logLines.slice(-LOG_TAIL).reverse();
    els.log.innerHTML = "";
    tail.forEach(function (event) {
      var li = document.createElement("li");
      li.textContent = formatLogEvent(event);
      els.log.appendChild(li);
    });
  }

  function render() {
    if (!state.running && interruptedStage() && !allFinished()) {
      state.stopped = true;
    }
    if (!state.running && allFinished()) {
      state.stopped = false;
      state.completed = true;
    }
    renderStatus();
    renderStages();
    renderErrors();
    renderControls();
    renderLog();
  }

  function applyStatus(status) {
    state.running = !!status.running;
    state.sessionError = status.error || null;
    if (!status.running && status.error === "run stopped") {
      state.stopped = true;
    }
    if (!status.running && allFinished()) {
      state.stopped = false;
      state.completed = true;
    }
    render();
  }

  function fetchRemoved() {
    return api("/prefilter/removed").then(function (result) {
      if (!result.ok || !result.body || typeof result.body !== "object") return;
      var body = result.body;
      if (typeof body.before !== "number" || typeof body.after !== "number") return;
      state.stages.prefilter.removed = {
        before: body.before,
        after: body.after,
      };
      render();
    });
  }

  function applyConfig(config) {
    if (!config || typeof config !== "object") return;
    if (typeof config.prefilter === "boolean") {
      state.prefilterOff = config.prefilter === false;
    }
    var repo = typeof config.repo === "string" ? config.repo : "";
    var parts = repo.split("/");
    if (parts.length >= 2) {
      els.owner.textContent = parts[0];
      els.name.textContent = parts.slice(1).join("/");
    } else if (repo) {
      els.owner.textContent = repo;
      els.name.textContent = "";
    }
  }

  function hydrateFromLog(text) {
    parseLog(text).forEach(function (event) {
      reduce(event, { silent: true });
    });
  }

  function closeSource() {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
  }

  function refreshStatus() {
    return api("").then(function (result) {
      if (!result.ok) return null;
      applyStatus(result.body);
      if (!result.body.running) closeSource();
      return result.body;
    });
  }

  function openSource() {
    closeSource();
    var source = new EventSource(
      "/api/runs/" + encodeURIComponent(runId) + "/events"
    );
    state.source = source;

    function onEvent(message) {
      if (!message.data) return;
      var event;
      try {
        event = JSON.parse(message.data);
      } catch (_err) {
        return;
      }
      reduce(event);
      if (allFinished()) refreshStatus();
    }

    EVENT_TYPES.forEach(function (type) {
      source.addEventListener(type, onEvent);
    });
    source.addEventListener("ping", function () {});
    source.addEventListener("error", function () {
      refreshStatus();
    });
  }

  function waitUntilRunning() {
    var attempts = 0;
    return new Promise(function (resolve, reject) {
      function tick() {
        refreshStatus()
          .then(function (status) {
            if (!status) {
              reject(new Error("status failed"));
              return;
            }
            if (status.running) {
              resolve(status);
              return;
            }
            attempts += 1;
            if (attempts > 60) {
              reject(new Error("run did not start"));
              return;
            }
            setTimeout(tick, 250);
          })
          .catch(reject);
      }
      tick();
    });
  }

  function resumeFrom(stage) {
    closeSource();
    els.primary.setAttribute("aria-busy", "true");
    api("/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from: stage }),
    })
      .then(function (result) {
        if (!result.ok) {
          throw new Error(
            (result.body && result.body.detail) || "Could not resume run"
          );
        }
        return waitUntilRunning();
      })
      .then(function () {
        state.stopped = false;
        state.sessionError = null;
        state.errors = [];
        var target = state.stages[stage];
        if (target && (target.state === "stopped" || target.state === "running")) {
          target.state = "running";
        }
        openSource();
        render();
      })
      .catch(function (err) {
        state.sessionError = err.message || String(err);
        state.stopped = true;
        state.running = false;
        render();
      })
      .then(function () {
        els.primary.removeAttribute("aria-busy");
      });
  }

  function stopRun() {
    api("/stop", { method: "POST" })
      .then(function (result) {
        if (!result.ok) {
          throw new Error(
            (result.body && result.body.detail) || "Could not stop run"
          );
        }
        state.running = false;
        state.stopped = true;
        state.sessionError = "run stopped";
        var interrupted = interruptedStage();
        if (interrupted) state.stages[interrupted].state = "stopped";
        render();
      })
      .catch(function (err) {
        state.sessionError = err.message || String(err);
        render();
      });
  }

  function refreshLog() {
    return api("/log").then(function (result) {
      if (!result.ok) return;
      var events = parseLog(result.text);
      state.logLines = events;
      events.forEach(function (event) {
        remember(event);
      });
      renderLog();
    });
  }

  els.stop.addEventListener("click", function () {
    stopRun();
  });

  els.logToggle.addEventListener("click", function () {
    state.logOpen = !state.logOpen;
    if (state.logOpen) {
      refreshLog().then(render);
    } else {
      render();
    }
  });

  function startTimer() {
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(function () {
      if (state.running) {
        renderStatus();
        renderStages();
      }
    }, 1000);
  }

  Promise.all([
    api(""),
    api("/config").catch(function () {
      return { ok: false };
    }),
    api("/log").catch(function () {
      return { ok: false, text: "" };
    }),
  ])
    .then(function (results) {
      var status = results[0];
      var config = results[1];
      var log = results[2];

      if (!status.ok) {
        els.status.textContent =
          (status.body && status.body.detail) || "Run not found";
        els.status.classList.add("is-stopped");
        return;
      }

      if (config.ok) applyConfig(config.body);
      if (log.ok) hydrateFromLog(log.text);
      applyStatus(status.body);

      if (state.stages.prefilter.state === "done" && !state.prefilterOff) {
        fetchRemoved();
      }

      if (status.body.running) openSource();
      else if (!allFinished()) openSource();

      startTimer();
      render();
    })
    .catch(function (err) {
      els.status.textContent = err.message || String(err);
      els.status.classList.add("is-stopped");
    });
})();
