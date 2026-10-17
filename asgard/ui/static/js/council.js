(function () {
  "use strict";

  var EVENT_TYPES = [
    "ClassificationAttempt",
    "EvaluatorVerdict",
    "LoopFinished",
    "ClassifyProgress",
    "StageStarted",
    "StageFinished",
    "Error",
  ];

  var FINDING_ORDER = ["decision", "evidence", "completeness", "rationale"];
  var STATUS_FILTERS = [
    "all",
    "classifying",
    "review",
    "security",
    "not security",
    "unresolved",
    "error",
  ];
  var ROMAN = [
    "I",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "VII",
    "VIII",
    "IX",
    "X",
    "XI",
    "XII",
  ];

  var root = document.getElementById("council");
  if (!root) return;

  var params = new URLSearchParams(window.location.search);
  var runId = params.get("run") || root.getAttribute("data-run") || "";
  if (!runId) return;

  var els = {
    security: document.getElementById("council-security"),
    notSecurity: document.getElementById("council-not-security"),
    unresolved: document.getElementById("council-unresolved"),
    errorTotal: document.getElementById("council-error-total"),
    loop: document.getElementById("council-loop"),
    rateI: document.getElementById("council-rate-i"),
    rateFinal: document.getElementById("council-rate-final"),
    mean: document.getElementById("council-mean"),
    filters: document.getElementById("council-filters"),
    prs: document.getElementById("council-pr-list"),
    status: document.getElementById("council-status"),
    title: document.getElementById("council-pr-title"),
    waiting: document.getElementById("council-waiting"),
    error: document.getElementById("council-error"),
    retry: document.getElementById("council-retry"),
    retryAll: document.getElementById("council-retry-all"),
    rounds: document.getElementById("council-rounds"),
    prTemplate: document.getElementById("council-pr-template"),
    roundTemplate: document.getElementById("council-round-template"),
  };

  var state = {
    order: [],
    prs: {},
    selected: null,
    statusFilter: "all",
    why: null,
    totals: null,
    metrics: null,
    classifyDone: false,
    source: null,
    seen: {},
    retrying: false,
  };

  function emptyPr(prId) {
    return {
      prId: prId,
      title: "",
      display: prId,
      phase: "pending",
      attempts: 0,
      status: null,
      decision: null,
      iterations: 0,
      error: "",
      rounds: [],
      liveRound: null,
    };
  }

  function ensurePr(prId) {
    if (!state.prs[prId]) {
      state.prs[prId] = emptyPr(prId);
      if (state.order.indexOf(prId) < 0) state.order.push(prId);
    }
    return state.prs[prId];
  }

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

  function resetPrForRetry(prId) {
    var pr = ensurePr(prId);
    pr.phase = "classifying";
    pr.status = null;
    pr.error = "";
    pr.rounds = [];
    pr.attempts = 0;
    pr.decision = null;
    pr.iterations = 0;
    pr.liveRound = null;
  }

  function retryClassify(options) {
    if (state.retrying) return Promise.resolve();
    var body = options || {};
    state.retrying = true;
    render();
    return api("/classify/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (result) {
        if (!result.ok) {
          var detail =
            result.body && result.body.detail
              ? result.body.detail
              : "Could not retry classify";
          throw new Error(typeof detail === "string" ? detail : "Could not retry classify");
        }
        var ids = Array.isArray(result.body.pr_ids) ? result.body.pr_ids : [];
        ids.forEach(resetPrForRetry);
        state.classifyDone = false;
        state.metrics = null;
        recountTotalsFromPrs();
        openSource();
        render();
      })
      .catch(function (err) {
        var message = err.message || String(err);
        return loadItems()
          .catch(function () {
            /* ignore */
          })
          .then(function () {
            var pr = state.selected ? state.prs[state.selected] : null;
            if (pr) pr.error = message;
            render();
          });
      })
      .then(function () {
        state.retrying = false;
        render();
      });
  }

  function parseJsonl(text) {
    var rows = [];
    String(text || "")
      .split("\n")
      .forEach(function (line) {
        line = line.trim();
        if (!line) return;
        try {
          rows.push(JSON.parse(line));
        } catch (_err) {
          /* skip */
        }
      });
    return rows;
  }

  function roman(n) {
    if (n >= 1 && n <= ROMAN.length) return ROMAN[n - 1];
    return String(n);
  }

  function percent(rate) {
    if (typeof rate !== "number" || isNaN(rate)) return "—";
    return Math.round(rate * 100) + "%";
  }

  function oneDecimal(value) {
    if (typeof value !== "number" || isNaN(value)) return "—";
    return value.toFixed(1);
  }

  function decisionLabel(decision) {
    if (decision === true) return "security";
    if (decision === false) return "not security";
    return null;
  }

  function bucketOf(status, decision) {
    if (status === "approved") {
      if (decision === true) return "security";
      if (decision === false) return "not security";
      return null;
    }
    if (status === "error") return "error";
    if (typeof status === "string" && status.indexOf("unresolved_") === 0) {
      return "unresolved";
    }
    return null;
  }

  function shortOf(pr) {
    if (pr.phase === "classifying") return "classifying";
    if (pr.phase === "review") return "review";
    if (pr.status === "error") return "error";
    if (pr.status === "approved") return decisionLabel(pr.decision) || "security";
    if (typeof pr.status === "string" && pr.status.indexOf("unresolved_") === 0) {
      return "unresolved";
    }
    return "";
  }

  function statusClass(pr) {
    var short = shortOf(pr);
    if (short === "classifying") return "is-classifying";
    if (short === "review") return "is-review";
    if (short === "security") return "is-security";
    if (short === "not security") return "is-not-security";
    if (short === "unresolved") return "is-unresolved";
    if (short === "error") return "is-error";
    return "";
  }

  function statusLabel(pr) {
    if (pr.phase === "classifying") return "classifying";
    if (pr.phase === "review") {
      var n = pr.attempts || pr.liveRound || 1;
      return "under review · round " + roman(n);
    }
    if (pr.status === "error") return "error";
    if (pr.status === "approved") {
      var label = decisionLabel(pr.decision);
      if (!label) return "approved";
      if (label === "security" && pr.iterations > 0) {
        return (
          "approved · security · " +
          pr.iterations +
          " round" +
          (pr.iterations === 1 ? "" : "s")
        );
      }
      return "approved · " + label;
    }
    if (pr.status === "unresolved_max_iter") return "unresolved · iteration cap";
    if (pr.status === "unresolved_no_progress") return "unresolved · no progress";
    if (typeof pr.status === "string" && pr.status.indexOf("unresolved_") === 0) {
      return "unresolved";
    }
    return "";
  }

  function isLive(pr) {
    return pr.phase === "classifying" || pr.phase === "review";
  }

  function belongsTo(line, prId) {
    if (!line || typeof line !== "object") return false;
    if (line.pr_id === prId) return true;
    return Array.isArray(line.pr_ids) && line.pr_ids.indexOf(prId) >= 0;
  }

  function parsedFor(line, prId) {
    var parsed = line.parsed;
    if (!parsed || typeof parsed !== "object") return null;
    if (Array.isArray(line.pr_ids)) {
      var item = parsed[prId];
      return item && typeof item === "object" ? item : null;
    }
    return parsed;
  }

  function firstQuote(parsed) {
    if (!parsed || !Array.isArray(parsed.evidence) || !parsed.evidence.length) {
      return "";
    }
    var first = parsed.evidence[0];
    if (!first || typeof first !== "object") return "";
    return typeof first.quote === "string" ? first.quote : "";
  }

  function findingsOf(line, parsed) {
    var raw = null;
    if (Array.isArray(line.findings)) raw = line.findings;
    else if (parsed && Array.isArray(parsed.findings)) raw = parsed.findings;
    if (!raw) return [];
    var byName = {};
    raw.forEach(function (finding) {
      if (!finding || typeof finding !== "object") return;
      if (typeof finding.criterion === "string") byName[finding.criterion] = finding;
    });
    return FINDING_ORDER.map(function (name) {
      var finding = byName[name];
      if (!finding) {
        return { c: name, passed: false, text: "" };
      }
      return {
        c: name,
        passed: !!finding.passed,
        text: typeof finding.explanation === "string" ? finding.explanation : "",
      };
    });
  }

  function verdictOf(line, parsed) {
    if (typeof line.verdict === "string") return line.verdict;
    if (parsed && typeof parsed.verdict === "string") return parsed.verdict;
    return null;
  }

  function feedbackOf(parsed) {
    if (parsed && typeof parsed.feedback === "string") return parsed.feedback;
    return "";
  }

  function errorFromTraces(lines) {
    for (var i = 0; i < lines.length; i += 1) {
      var err = lines[i] && lines[i].error;
      if (typeof err === "string" && err) return err;
    }
    return "";
  }

  function buildRounds(prId, lines) {
    var byIter = {};
    var order = [];
    lines.forEach(function (line) {
      if (!line || typeof line !== "object") return;
      var iter = line.iteration;
      if (typeof iter !== "number") return;
      if (!byIter[iter]) {
        byIter[iter] = { classifier: null, evaluator: null };
        order.push(iter);
      }
      if (line.role === "classifier") byIter[iter].classifier = line;
      if (line.role === "evaluator") byIter[iter].evaluator = line;
    });
    order.sort(function (a, b) {
      return a - b;
    });
    return order.map(function (iter) {
      var pair = byIter[iter];
      var cParsed = pair.classifier ? parsedFor(pair.classifier, prId) : null;
      var eParsed = pair.evaluator ? parsedFor(pair.evaluator, prId) : null;
      var decision = null;
      if (cParsed && typeof cParsed.has_security_discussion === "boolean") {
        decision = cParsed.has_security_discussion;
      }
      var verdict = pair.evaluator ? verdictOf(pair.evaluator, eParsed) : null;
      var findings = pair.evaluator ? findingsOf(pair.evaluator, eParsed) : [];
      return {
        n: roman(iter),
        iteration: iter,
        decision: decisionLabel(decision),
        quote: firstQuote(cParsed),
        verdict: verdict ? String(verdict).toLowerCase() : null,
        findings: findings,
        feedback: feedbackOf(eParsed),
        // Only "reviewing" while live; a finished PR with no evaluator ended early.
        awaitingEvaluator: !pair.evaluator,
      };
    });
  }

  function applyTerminal(pr, payload) {
    if (!payload || typeof payload !== "object") return;
    pr.status = typeof payload.status === "string" ? payload.status : pr.status;
    pr.iterations =
      typeof payload.iterations === "number" ? payload.iterations : pr.iterations;
    var classification = payload.classification;
    if (classification && typeof classification === "object") {
      if (typeof classification.has_security_discussion === "boolean") {
        pr.decision = classification.has_security_discussion;
      }
    }
    pr.phase = "done";
    pr.liveRound = null;
    if (Array.isArray(payload.traces)) {
      pr.rounds = buildRounds(pr.prId, payload.traces);
      if (pr.status === "error") {
        pr.error = errorFromTraces(payload.traces) || pr.error ||
          "Classification failed for this pull request.";
      }
    } else if (pr.status === "error" && !pr.error) {
      pr.error = "Classification failed for this pull request.";
    }
  }

  function applyTracesForPr(prId, lines) {
    var pr = ensurePr(prId);
    var mine = lines.filter(function (line) {
      return belongsTo(line, prId);
    });
    pr.rounds = buildRounds(prId, mine);
    if (pr.status === "error") {
      pr.error =
        errorFromTraces(mine) ||
        pr.error ||
        "Classification failed for this pull request.";
    }
  }

  function recountTotalsFromPayloads(payloads) {
    var security = 0;
    var notSecurity = 0;
    var unresolved = 0;
    var error = 0;
    payloads.forEach(function (payload) {
      if (!payload || typeof payload !== "object") return;
      var decision = null;
      var classification = payload.classification;
      if (classification && typeof classification === "object") {
        if (typeof classification.has_security_discussion === "boolean") {
          decision = classification.has_security_discussion;
        }
      }
      var bucket = bucketOf(payload.status, decision);
      if (bucket === "security") security += 1;
      else if (bucket === "not security") notSecurity += 1;
      else if (bucket === "unresolved") unresolved += 1;
      else if (bucket === "error") error += 1;
    });
    state.totals = {
      security: security,
      notSecurity: notSecurity,
      unresolved: unresolved,
      error: error,
    };
  }

  function recountTotalsFromPrs() {
    var security = 0;
    var notSecurity = 0;
    var unresolved = 0;
    var error = 0;
    state.order.forEach(function (prId) {
      var pr = state.prs[prId];
      if (!pr) return;
      var bucket = shortOf(pr);
      if (bucket === "security") security += 1;
      else if (bucket === "not security") notSecurity += 1;
      else if (bucket === "unresolved") unresolved += 1;
      else if (bucket === "error") error += 1;
    });
    state.totals = {
      security: security,
      notSecurity: notSecurity,
      unresolved: unresolved,
      error: error,
    };
  }

  function seedFromPrefilter(pulls) {
    if (!Array.isArray(pulls)) return;
    pulls.forEach(function (pull) {
      if (!pull || typeof pull !== "object") return;
      var context = pull.context;
      if (!context || typeof context !== "object") return;
      var prId = context.pr_id;
      if (typeof prId !== "string" || !prId) return;
      var pr = ensurePr(prId);
      if (typeof context.title === "string") pr.title = context.title;
    });
  }

  function resolveDisplayNumbers(numbers, nodeIds) {
    var byKey = {};
    numbers.forEach(function (number) {
      byKey[String(number)] = String(number);
    });
    Object.keys(nodeIds).forEach(function (number) {
      var nodeId = nodeIds[number];
      if (nodeId) byKey[nodeId] = String(number);
    });
    state.order.forEach(function (prId) {
      var pr = state.prs[prId];
      if (!pr) return;
      if (byKey[prId]) pr.display = byKey[prId];
    });
  }

  function loadMineNumbers() {
    return api("/mine/index").then(function (result) {
      if (!result.ok || !result.body || typeof result.body !== "object") return;
      var numbers = Array.isArray(result.body.numbers) ? result.body.numbers : [];
      var nodeIds =
        result.body.node_ids && typeof result.body.node_ids === "object"
          ? result.body.node_ids
          : {};
      numbers = numbers.filter(function (item) {
        return typeof item === "number" && !isNaN(item);
      });
      if (!numbers.length) return;
      resolveDisplayNumbers(numbers, nodeIds);
    });
  }

  function loadItems() {
    return api("/classify/items").then(function (result) {
      if (!result.ok) return;
      var rows = parseJsonl(result.text);
      var payloads = [];
      var seen = {};
      rows.forEach(function (row) {
        if (!row || typeof row !== "object") return;
        var payload = row.payload;
        if (!payload || typeof payload !== "object") return;
        var prId =
          typeof payload.pr_id === "string"
            ? payload.pr_id
            : typeof row.id === "string"
              ? row.id
              : null;
        if (!prId) return;
        var pr = ensurePr(prId);
        applyTerminal(pr, payload);
        payloads.push(payload);
        seen[prId] = true;
      });
      // Dropped error checkpoints (retry) must not stay painted as error from the log.
      state.order.forEach(function (prId) {
        var pr = state.prs[prId];
        if (!pr || seen[prId]) return;
        if (pr.status !== "error") return;
        resetPrForRetry(prId);
      });
      if (payloads.length && !state.classifyDone) {
        recountTotalsFromPayloads(payloads);
      } else {
        recountTotalsFromPrs();
      }
    });
  }

  function loadOutput() {
    return api("/classify/output").then(function (result) {
      if (!result.ok || !Array.isArray(result.body)) return false;
      result.body.forEach(function (payload) {
        if (!payload || typeof payload !== "object") return;
        var prId = payload.pr_id;
        if (typeof prId !== "string") return;
        var pr = ensurePr(prId);
        applyTerminal(pr, payload);
      });
      recountTotalsFromPayloads(result.body);
      return true;
    });
  }

  function loadTrace() {
    return api("/classify/trace").then(function (result) {
      if (!result.ok) return;
      var lines = parseJsonl(result.text);
      state.order.forEach(function (prId) {
        applyTracesForPr(prId, lines);
      });
      Object.keys(state.prs).forEach(function (prId) {
        if (state.order.indexOf(prId) < 0) applyTracesForPr(prId, lines);
      });
    });
  }

  function loadMetrics() {
    return api("/classify/metrics").then(function (result) {
      if (!result.ok || !result.body || typeof result.body !== "object") return;
      state.metrics = {
        rateI: result.body.approval_rate_iteration_1,
        rateFinal: result.body.approval_rate_final,
        mean: result.body.mean_iterations,
      };
    });
  }

  function loadFinishedArtifacts() {
    return loadOutput().then(function (loaded) {
      if (!loaded) return false;
      state.classifyDone = true;
      return loadTrace()
        .then(loadMetrics)
        .then(function () {
          return true;
        });
    });
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

  function findingsFromEvent(raw) {
    if (!Array.isArray(raw) || !raw.length) return [];
    var byName = {};
    raw.forEach(function (finding) {
      if (!finding || typeof finding !== "object") return;
      if (typeof finding.criterion === "string") byName[finding.criterion] = finding;
    });
    return FINDING_ORDER.map(function (name) {
      var finding = byName[name];
      if (!finding) {
        return { c: name, passed: false, text: "" };
      }
      return {
        c: name,
        passed: !!finding.passed,
        text: typeof finding.explanation === "string" ? finding.explanation : "",
      };
    });
  }

  function upsertLiveRound(pr, patch) {
    var iteration = pr.liveRound || pr.attempts || 1;
    var existing = null;
    for (var i = 0; i < pr.rounds.length; i += 1) {
      if (pr.rounds[i].iteration === iteration) {
        existing = pr.rounds[i];
        break;
      }
    }
    if (!existing) {
      existing = {
        n: roman(iteration),
        iteration: iteration,
        decision: null,
        quote: "",
        verdict: null,
        findings: [],
        feedback: "",
        awaitingEvaluator: true,
      };
      pr.rounds.push(existing);
    }
    Object.keys(patch).forEach(function (key) {
      existing[key] = patch[key];
    });
    return existing;
  }

  function onClassificationAttempt(event) {
    var prId = event.pr_id;
    if (typeof prId !== "string") return;
    var pr = ensurePr(prId);
    if (pr.phase === "done") return;
    pr.attempts += 1;
    pr.phase = "review";
    pr.liveRound = pr.attempts;
    var decision = null;
    if (typeof event.has_security_discussion === "boolean") {
      decision = event.has_security_discussion;
    }
    upsertLiveRound(pr, {
      decision: decisionLabel(decision),
      quote: typeof event.quote === "string" ? event.quote : "",
      verdict: null,
      findings: [],
      feedback: "",
      awaitingEvaluator: true,
    });
  }

  function onEvaluatorVerdict(event) {
    var prId = event.pr_id;
    if (typeof prId !== "string") return;
    var pr = ensurePr(prId);
    if (pr.phase === "done") return;
    var findings = findingsFromEvent(event.findings);
    var feedback = typeof event.feedback === "string" ? event.feedback : "";
    upsertLiveRound(pr, {
      verdict: event.approved === true ? "approve" : "revise",
      findings: findings,
      feedback: feedback,
      awaitingEvaluator: false,
    });
    if (event.approved === true) return;
    pr.phase = "classifying";
  }

  function onLoopFinished(event, live) {
    var prId = event.pr_id;
    if (typeof prId !== "string") return null;
    ensurePr(prId);
    if (!live) return "render";
    return loadItems().then(render);
  }

  function onClassifyProgress(event) {
    if (event.kind !== "pull") return;
    var index = event.index;
    if (typeof index !== "number" || index < 1) return;
    var prId = state.order[index - 1];
    if (!prId) return;
    var pr = ensurePr(prId);
    if (pr.phase === "done" || pr.phase === "review") return;
    pr.phase = "classifying";
  }

  function onStageFinished(event, live) {
    if (event.stage !== "classify") return null;
    state.classifyDone = true;
    if (!live) return "render";
    return loadFinishedArtifacts().then(render);
  }

  function onError(event) {
    if (event.stage && event.stage !== "classify") return;
    if (typeof event.message !== "string" || !event.message) return;
    if (!state.selected) return;
    var pr = state.prs[state.selected];
    if (pr && pr.phase !== "done" && pr.status !== "error") {
      /* stage-level errors surface on the selected live PR when still open */
      pr.error = event.message;
    }
  }

  function reduce(event, live) {
    if (!event || !event.type) return null;
    if (!remember(event)) return null;
    switch (event.type) {
      case "ClassificationAttempt":
        onClassificationAttempt(event);
        return "render";
      case "EvaluatorVerdict":
        onEvaluatorVerdict(event);
        return "render";
      case "LoopFinished":
        return onLoopFinished(event, live);
      case "ClassifyProgress":
        onClassifyProgress(event);
        return "render";
      case "StageFinished":
        return onStageFinished(event, live);
      case "Error":
        onError(event);
        return "render";
      default:
        return null;
    }
  }

  function select(prId) {
    if (!state.prs[prId]) return;
    state.selected = prId;
    state.why = null;
    render();
  }

  function setStatusFilter(filter) {
    if (STATUS_FILTERS.indexOf(filter) < 0) return;
    state.statusFilter = filter;
    state.why = null;
    var visible = visibleOrder();
    if (visible.indexOf(state.selected) < 0) {
      state.selected = visible.length ? visible[0] : null;
    }
    render();
  }

  function matchesFilter(pr) {
    if (state.statusFilter === "all") return true;
    return shortOf(pr) === state.statusFilter;
  }

  function visibleOrder() {
    return state.order.filter(function (prId) {
      var pr = state.prs[prId];
      return pr && matchesFilter(pr);
    });
  }

  function toggleWhy(key) {
    state.why = state.why === key ? null : key;
    render();
  }

  function clearStatusClasses(node) {
    [
      "is-classifying",
      "is-review",
      "is-security",
      "is-not-security",
      "is-unresolved",
      "is-error",
    ].forEach(function (name) {
      node.classList.remove(name);
    });
  }

  function renderTotals() {
    if (!state.totals) {
      els.security.textContent = "—";
      els.notSecurity.textContent = "—";
      els.unresolved.textContent = "—";
      if (els.errorTotal) els.errorTotal.textContent = "—";
      if (els.retryAll) els.retryAll.hidden = true;
      return;
    }
    els.security.textContent = String(state.totals.security);
    els.notSecurity.textContent = String(state.totals.notSecurity);
    els.unresolved.textContent = String(state.totals.unresolved);
    if (els.errorTotal) els.errorTotal.textContent = String(state.totals.error || 0);
    if (els.retryAll) {
      var n = state.totals.error || 0;
      els.retryAll.hidden = n < 1 || state.retrying;
      els.retryAll.disabled = state.retrying;
      els.retryAll.textContent =
        n === 1 ? "Retry 1 error" : "Retry all " + n + " errors";
    }
  }

  function renderMetrics() {
    if (!state.metrics) {
      els.loop.hidden = true;
      return;
    }
    els.rateI.textContent = percent(state.metrics.rateI);
    els.rateFinal.textContent = percent(state.metrics.rateFinal);
    els.mean.textContent = oneDecimal(state.metrics.mean);
    els.loop.hidden = false;
  }

  function renderFilters() {
    if (!els.filters) return;
    els.filters.innerHTML = "";
    STATUS_FILTERS.forEach(function (filter) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "council-filter";
      button.setAttribute("role", "tab");
      button.setAttribute(
        "aria-selected",
        filter === state.statusFilter ? "true" : "false"
      );
      if (filter === state.statusFilter) button.classList.add("on");
      button.textContent = filter;
      button.addEventListener("click", function () {
        setStatusFilter(filter);
      });
      els.filters.appendChild(button);
    });
  }

  function renderList() {
    if (!els.prs) return;
    els.prs.innerHTML = "";
    if (!els.prTemplate) return;
    visibleOrder().forEach(function (prId) {
      var pr = state.prs[prId];
      if (!pr) return;
      var node = els.prTemplate.content.firstElementChild.cloneNode(true);
      var dot = node.querySelector("[data-dot]");
      var id = node.querySelector("[data-id]");
      var title = node.querySelector("[data-title]");
      var short = node.querySelector("[data-short]");
      var klass = statusClass(pr);
      var shortLabel = shortOf(pr);

      if (prId === state.selected) node.classList.add("sel");
      node.setAttribute("aria-pressed", prId === state.selected ? "true" : "false");

      clearStatusClasses(dot);
      clearStatusClasses(short);
      dot.classList.toggle("live", isLive(pr));
      if (klass) {
        dot.classList.add(klass);
        short.classList.add(klass);
      }

      id.textContent = "#" + pr.display;
      title.textContent = pr.title || pr.prId;
      short.textContent = shortLabel;

      node.addEventListener("click", function () {
        select(prId);
      });
      els.prs.appendChild(node);
    });
  }

  function renderFindings(marks, whyList, findings, open) {
    marks.innerHTML = "";
    whyList.innerHTML = "";
    findings.forEach(function (finding) {
      var mark = document.createElement("span");
      mark.className = finding.passed ? "is-yes" : "is-no";
      mark.appendChild(
        document.createTextNode((finding.passed ? "✓" : "✗") + " ")
      );
      var name = document.createElement("span");
      name.className = "finding-name";
      name.textContent = finding.c;
      mark.appendChild(name);
      marks.appendChild(mark);

      if (open) {
        var item = document.createElement("li");
        var strong = document.createElement("span");
        strong.className = "finding-name";
        strong.textContent = finding.c;
        item.appendChild(strong);
        item.appendChild(document.createTextNode(" — " + (finding.text || "")));
        whyList.appendChild(item);
      }
    });
  }

  function renderRounds(pr) {
    els.rounds.innerHTML = "";
    if (!els.roundTemplate) return;

    var rounds = pr.rounds.slice();
    if (pr.phase === "review" && !rounds.length) {
      rounds.push({
        n: roman(pr.attempts || 1),
        decision: null,
        quote: "",
        verdict: null,
        findings: [],
        feedback: "",
        awaitingEvaluator: true,
      });
    }

    rounds.forEach(function (round) {
      var node = els.roundTemplate.content.firstElementChild.cloneNode(true);
      var n = node.querySelector("[data-n]");
      var decision = node.querySelector("[data-decision]");
      var quote = node.querySelector("[data-quote]");
      var verdict = node.querySelector("[data-verdict]");
      var findingsBox = node.querySelector("[data-findings]");
      var marks = node.querySelector("[data-marks]");
      var whyBtn = node.querySelector("[data-why]");
      var whyList = node.querySelector("[data-why-list]");
      var feedback = node.querySelector("[data-feedback]");

      n.textContent = round.n;
      decision.textContent =
        "Classifier · " + (round.decision || (pr.phase === "review" ? "…" : "—"));

      if (round.quote) {
        quote.hidden = false;
        quote.textContent = "“" + round.quote + "”";
      } else {
        quote.hidden = true;
        quote.textContent = "";
      }

      var liveReview =
        !!(round.awaitingEvaluator || round.reviewing) &&
        (pr.phase === "review" || pr.phase === "classifying");
      var verdictText = round.verdict;
      var stopNote = "";
      if (!verdictText) {
        if (liveReview) {
          verdictText = "reviewing…";
        } else if (pr.status === "unresolved_no_progress") {
          verdictText = "no progress";
          stopNote = "Classifier repeated the same answer; evaluation did not run again.";
        } else if (pr.status === "unresolved_max_iter") {
          verdictText = "unresolved";
        } else if (pr.status === "error") {
          verdictText = "error";
        } else {
          verdictText = "—";
        }
      }
      verdict.textContent = "Evaluator · " + verdictText;
      verdict.classList.remove("is-approve", "is-revise", "is-reviewing");
      if (verdictText === "approve") verdict.classList.add("is-approve");
      else if (verdictText === "revise") verdict.classList.add("is-revise");
      else if (verdictText === "reviewing…") verdict.classList.add("is-reviewing");
      else if (
        verdictText === "no progress" ||
        verdictText === "unresolved" ||
        verdictText === "error"
      ) {
        verdict.classList.add("is-revise");
      }

      var key = pr.prId + ":" + round.n;
      var open = state.why === key;
      var hasFindings = Array.isArray(round.findings) && round.findings.length > 0;

      if (hasFindings) {
        findingsBox.hidden = false;
        whyBtn.setAttribute("aria-expanded", open ? "true" : "false");
        whyBtn.textContent = open ? "hide" : "why";
        whyBtn.onclick = function () {
          toggleWhy(key);
        };
        renderFindings(marks, whyList, round.findings, open);
        whyList.hidden = !open;
      } else {
        findingsBox.hidden = true;
        whyList.hidden = true;
        whyBtn.onclick = null;
      }

      var feedbackText = round.feedback || stopNote;
      if (feedbackText) {
        feedback.hidden = false;
        feedback.textContent = feedbackText;
      } else {
        feedback.hidden = true;
        feedback.textContent = "";
      }

      els.rounds.appendChild(node);
    });
  }

  function renderDeliberation() {
    var pr = state.selected ? state.prs[state.selected] : null;
    if (!pr) {
      els.status.textContent = "";
      clearStatusClasses(els.status);
      els.title.textContent = "";
      els.waiting.hidden = true;
      els.error.hidden = true;
      if (els.retry) els.retry.hidden = true;
      els.rounds.innerHTML = "";
      return;
    }

    var label = statusLabel(pr);
    var klass = statusClass(pr);
    els.status.textContent = label;
    clearStatusClasses(els.status);
    if (klass) els.status.classList.add(klass);
    els.title.textContent = pr.title || pr.prId;

    var waiting = pr.phase === "classifying" && !pr.rounds.length;
    els.waiting.hidden = !(pr.phase === "classifying");

    var showError = pr.status === "error" && pr.error;
    els.error.hidden = !showError;
    els.error.textContent = showError ? pr.error : "";
    if (els.retry) {
      els.retry.hidden = pr.status !== "error" || state.retrying;
      els.retry.disabled = state.retrying;
    }

    if (waiting) {
      els.rounds.innerHTML = "";
      return;
    }
    renderRounds(pr);
  }

  function render() {
    var visible = visibleOrder();
    if (!state.selected || visible.indexOf(state.selected) < 0) {
      state.selected = visible.length ? visible[0] : null;
    }
    renderTotals();
    renderMetrics();
    renderFilters();
    renderList();
    renderDeliberation();
  }

  if (els.retry) {
    els.retry.addEventListener("click", function () {
      if (!state.selected) return;
      retryClassify({ pr_ids: [state.selected] });
    });
  }
  if (els.retryAll) {
    els.retryAll.addEventListener("click", function () {
      retryClassify({ all_errors: true });
    });
  }

  function closeSource() {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
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
      var result = reduce(event, true);
      if (result === "render") render();
      else if (result && typeof result.then === "function") {
        result.catch(function () {
          /* ignore fetch races */
        });
      }
    }

    EVENT_TYPES.forEach(function (type) {
      source.addEventListener(type, onEvent);
    });
    source.addEventListener("ping", function () {});
    source.addEventListener("error", function () {
      api("").then(function (result) {
        if (result.ok && result.body && !result.body.running) closeSource();
      });
    });
  }

  function hydrateFromLog(text) {
    parseJsonl(text).forEach(function (event) {
      if (!event || !event.type) return;
      if (EVENT_TYPES.indexOf(event.type) < 0) return;
      reduce(event, false);
    });
  }

  Promise.all([
    api(""),
    api("/prefilter/output").catch(function () {
      return { ok: false };
    }),
    api("/log").catch(function () {
      return { ok: false, text: "" };
    }),
  ])
    .then(function (results) {
      var status = results[0];
      var prefilter = results[1];
      var log = results[2];

      if (!status.ok) return;

      if (prefilter.ok) seedFromPrefilter(prefilter.body);

      return loadMineNumbers()
        .catch(function () {
          /* display ids stay as stored pr_id */
        })
        .then(function () {
          if (log.ok) hydrateFromLog(log.text);
        })
        .then(function () {
          // items.jsonl is the source of truth over historical log events.
          return loadItems().catch(function () {
            /* no items yet */
          });
        })
        .then(function () {
          return loadFinishedArtifacts().catch(function () {
            return false;
          });
        })
        .then(function () {
          openSource();
          render();
        });
    })
    .catch(function () {
      render();
    });
})();


