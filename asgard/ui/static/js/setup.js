(function () {
  "use strict";

  var MODEL = {
    ollama: "llama3.1",
    gemini: "gemini-2.5-flash",
    openai: "gpt-4.1-mini",
    anthropic: "claude-sonnet-5-5",
  };
  var KEY = {
    gemini: { label: "Gemini API key", env: "GEMINI_API_KEY" },
    openai: { label: "OpenAI API key", env: "OPENAI_API_KEY" },
    anthropic: { label: "Anthropic API key", env: "ANTHROPIC_API_KEY" },
  };
  var ROLES = ["classifier", "evaluator", "synthesizer"];
  var PROVIDERS = ["ollama", "gemini", "openai", "anthropic"];

  var form = document.getElementById("setup-form");
  if (!form) return;

  var els = {
    repo: document.getElementById("repo"),
    token: document.getElementById("github_token"),
    provider: document.getElementById("provider"),
    model: document.getElementById("model"),
    credentials: document.getElementById("credentials"),
    moreToggle: document.getElementById("more-toggle"),
    moreOptions: document.getElementById("more-options"),
    maxPrs: document.getElementById("max_prs"),
    state: document.getElementById("state"),
    since: document.getElementById("since"),
    until: document.getElementById("until"),
    maxIterations: document.getElementById("max_iterations"),
    batchSize: document.getElementById("batch_size"),
    prefilter: document.getElementById("prefilter"),
    start: document.getElementById("start-run"),
    load: document.getElementById("load-toml"),
    save: document.getElementById("save-toml"),
    file: document.getElementById("toml-file"),
    formError: document.getElementById("form-error"),
    hintGithub: document.getElementById("hint-github"),
    hintRepo: document.getElementById("hint-repo"),
  };

  var envPrefill = {};
  var credValues = {};
  var validateTimer = null;
  var modelsTimer = null;
  var lastModels = Object.assign({}, MODEL);
  var modelLists = {};
  var modelsRequest = 0;

  function roleProvider(role) {
    var select = form.querySelector('.role-provider[data-role="' + role + '"]');
    return select ? select.value : "default";
  }

  function roleModel(role) {
    var select = form.querySelector('.role-model[data-role="' + role + '"]');
    return select ? select.value.trim() : "";
  }

  function fillSelect(select, models, current, allowEmpty) {
    if (!select) return;
    var value = current || "";
    var options = models.slice();
    if (value && options.indexOf(value) < 0) options.unshift(value);
    select.innerHTML = "";
    if (allowEmpty) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "default";
      select.appendChild(empty);
    }
    var seen = {};
    options.forEach(function (id) {
      if (!id || seen[id]) return;
      seen[id] = true;
      var option = document.createElement("option");
      option.value = id;
      option.textContent = id;
      select.appendChild(option);
    });
    if (allowEmpty && !value) select.value = "";
    else if (value) select.value = value;
    else if (options.length) select.value = options[0];
  }

  function modelsFor(provider) {
    var listed = modelLists[provider];
    if (listed && listed.length) return listed.slice();
    var fallback = lastModels[provider] || MODEL[provider];
    return fallback ? [fallback] : [];
  }

  function applyModelLists() {
    fillSelect(
      els.model,
      modelsFor(els.provider.value),
      els.model.value.trim() || lastModels[els.provider.value] || MODEL[els.provider.value],
      false
    );
    lastModels[els.provider.value] = els.model.value.trim();
    ROLES.forEach(function (role) {
      var provider = roleProvider(role);
      var select = form.querySelector('.role-model[data-role="' + role + '"]');
      if (!select) return;
      if (provider === "default") {
        fillSelect(select, [], select.value.trim(), true);
        return;
      }
      fillSelect(select, modelsFor(provider), select.value.trim(), true);
    });
  }

  function scheduleModels() {
    if (modelsTimer) clearTimeout(modelsTimer);
    modelsTimer = setTimeout(fetchModels, 300);
  }

  function fetchModels() {
    var providers = usedProviders();
    var secrets = collectSecrets();
    var requestId = ++modelsRequest;
    providers.forEach(function (provider) {
      fetch("/api/setup/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: provider, secrets: secrets }),
      })
        .then(function (response) {
          return response.json().then(function (body) {
            return { ok: response.ok, body: body };
          });
        })
        .then(function (result) {
          if (requestId !== modelsRequest) return;
          if (result.ok && result.body && Array.isArray(result.body.models)) {
            modelLists[provider] = result.body.models;
          }
          applyModelLists();
        })
        .catch(function () {
          if (requestId !== modelsRequest) return;
          applyModelLists();
        });
    });
  }

  function usedProviders() {
    var used = [els.provider.value];
    ROLES.forEach(function (role) {
      var p = roleProvider(role);
      used.push(p === "default" ? els.provider.value : p);
    });
    return PROVIDERS.filter(function (p) {
      return used.indexOf(p) >= 0;
    });
  }

  function setHint(el, text, kind) {
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      el.classList.remove("ok", "err");
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.classList.remove("ok", "err");
    if (kind) el.classList.add(kind);
  }

  function showFormError(message) {
    setHint(els.formError, message || "", message ? "err" : null);
  }

  function renderCredentials() {
    var used = usedProviders();
    var previous = {};
    els.credentials.querySelectorAll("[data-cred]").forEach(function (input) {
      previous[input.getAttribute("data-cred")] = input.value;
    });

    els.credentials.innerHTML = "";
    used.forEach(function (provider) {
      var label = document.createElement("label");
      label.className = "f";

      var caption = document.createElement("span");
      caption.className = "lbl";

      var input = document.createElement("input");
      input.className = "inp";
      input.setAttribute("data-cred", provider);

      var hint = document.createElement("span");
      hint.className = "hint";
      hint.id = "hint-" + provider;

      if (provider === "ollama") {
        caption.textContent = "Ollama host";
        input.type = "text";
        input.placeholder = "http://localhost:11434";
        input.value =
          previous.ollama ||
          credValues.ollama ||
          envPrefill.OLLAMA_HOST ||
          "http://localhost:11434";
      } else {
        var meta = KEY[provider];
        caption.textContent = meta.label;
        input.type = "password";
        input.placeholder = meta.env + " from .env, or paste it here";
        input.autocomplete = "off";
        input.value =
          previous[provider] ||
          credValues[provider] ||
          envPrefill[meta.env] ||
          "";
      }

      input.addEventListener("input", function () {
        scheduleValidate();
        scheduleModels();
      });
      label.appendChild(caption);
      label.appendChild(input);
      label.appendChild(hint);
      els.credentials.appendChild(label);
    });
  }

  function collectSecrets() {
    var secrets = {};
    var token = els.token.value.trim();
    if (token) secrets.GITHUB_TOKEN = token;
    els.credentials.querySelectorAll("[data-cred]").forEach(function (input) {
      var provider = input.getAttribute("data-cred");
      var value = input.value.trim();
      if (!value) return;
      if (provider === "ollama") secrets.OLLAMA_HOST = value;
      else if (KEY[provider]) secrets[KEY[provider].env] = value;
    });
    return secrets;
  }

  function collectOverrides() {
    var overrides = {
      repo: els.repo.value.trim(),
      llm: {
        default: {
          provider: els.provider.value,
          model: els.model.value.trim() || MODEL[els.provider.value],
        },
      },
    };

    var maxIterations = parseInt(els.maxIterations.value, 10);
    if (!isNaN(maxIterations) && maxIterations >= 1) {
      overrides.max_iterations = maxIterations;
    }
    var batchSize = parseInt(els.batchSize.value, 10);
    if (!isNaN(batchSize) && batchSize >= 1) {
      overrides.batch_size = batchSize;
    }
    overrides.prefilter = els.prefilter.value === "on";

    var filters = { state: els.state.value };
    var maxPrs = parseInt(els.maxPrs.value, 10);
    if (!isNaN(maxPrs) && maxPrs >= 1) filters.max_prs = maxPrs;
    if (els.since.value) filters.since = els.since.value;
    if (els.until.value) filters.until = els.until.value;
    overrides.filters = filters;

    ROLES.forEach(function (role) {
      var provider = roleProvider(role);
      var model = roleModel(role);
      if (provider === "default" && !model) return;
      var spec = {};
      if (provider !== "default") spec.provider = provider;
      if (model && model !== "default") spec.model = model;
      if (Object.keys(spec).length) overrides.llm[role] = spec;
    });

    return overrides;
  }

  function applyFields(data) {
    if (!data || typeof data !== "object") return;
    if (data.repo) els.repo.value = data.repo;

    if (typeof data.max_iterations === "number") {
      els.maxIterations.value = String(data.max_iterations);
    }
    if (typeof data.batch_size === "number") {
      els.batchSize.value = String(data.batch_size);
    }
    if (typeof data.prefilter === "boolean") {
      els.prefilter.value = data.prefilter ? "on" : "off";
    }

    var filters = data.filters || {};
    if (filters.state) els.state.value = filters.state;
    if (filters.max_prs != null) els.maxPrs.value = String(filters.max_prs);
    else els.maxPrs.value = "";
    if (filters.since) els.since.value = String(filters.since).slice(0, 10);
    else els.since.value = "";
    if (filters.until) els.until.value = String(filters.until).slice(0, 10);
    else els.until.value = "";

    var llm = data.llm || {};
    var def = llm.default || {};
    if (def.provider) {
      els.provider.value = def.provider;
      lastModels[def.provider] = def.model || MODEL[def.provider] || "";
    }
    if (def.model) {
      lastModels[def.provider || els.provider.value] = def.model;
      fillSelect(els.model, modelsFor(els.provider.value), def.model, false);
    } else if (def.provider) {
      fillSelect(els.model, modelsFor(def.provider), MODEL[def.provider] || "", false);
    }

    ROLES.forEach(function (role) {
      var spec = llm[role] || {};
      var providerSelect = form.querySelector(
        '.role-provider[data-role="' + role + '"]'
      );
      var modelSelect = form.querySelector('.role-model[data-role="' + role + '"]');
      if (providerSelect) {
        providerSelect.value = spec.provider || "default";
      }
      if (modelSelect) {
        var provider = providerSelect ? providerSelect.value : "default";
        if (provider === "default") fillSelect(modelSelect, [], spec.model || "", true);
        else fillSelect(modelSelect, modelsFor(provider), spec.model || "", true);
      }
    });

    renderCredentials();
    scheduleValidate();
    scheduleModels();
  }

  function applyProbe(results) {
    if (!results || typeof results !== "object") return;
    var github = results.github;
    if (github) {
      if (github.ok) {
        var repo = els.repo.value.trim() || "repository";
        setHint(
          els.hintGithub,
          "✓ Can read " + repo + " · never saved to config",
          "ok"
        );
        setHint(els.hintRepo, "", null);
      } else {
        setHint(els.hintGithub, github.message || "GitHub check failed", "err");
      }
    }

    PROVIDERS.forEach(function (provider) {
      var hint = document.getElementById("hint-" + provider);
      if (!hint) return;
      var result = results[provider];
      if (!result) {
        setHint(hint, "", null);
        return;
      }
      if (result.ok) {
        if (provider === "ollama") {
          setHint(
            hint,
            "✓ Daemon answers · " + (els.model.value.trim() || "model") + " is pulled",
            "ok"
          );
        } else {
          setHint(hint, "✓ Key works · never saved to config", "ok");
        }
      } else {
        setHint(hint, result.message || "Check failed", "err");
      }
    });
  }

  function scheduleValidate() {
    if (validateTimer) clearTimeout(validateTimer);
    validateTimer = setTimeout(runValidate, 400);
  }

  function runValidate() {
    var overrides = collectOverrides();
    if (!overrides.repo) {
      setHint(els.hintGithub, "Token from .env when set · never saved to config", null);
      return;
    }
    fetch("/api/setup/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overrides: overrides, secrets: collectSecrets() }),
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, status: response.status, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          var detail =
            (result.body && result.body.detail) || "Validation failed";
          setHint(els.hintGithub, String(detail), "err");
          return;
        }
        applyProbe(result.body);
      })
      .catch(function () {
        setHint(els.hintGithub, "Could not reach validation endpoint", "err");
      });
  }

  function downloadToml(text) {
    var blob = new Blob([text], { type: "text/toml" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "asgard.toml";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  els.moreToggle.addEventListener("click", function () {
    var open = els.moreOptions.hidden;
    els.moreOptions.hidden = !open;
    els.moreToggle.setAttribute("aria-expanded", open ? "true" : "false");
    els.moreToggle.textContent = open ? "− Fewer options" : "+ More options";
  });

  els.provider.addEventListener("change", function () {
    var next = els.provider.value;
    var current = els.model.value.trim();
    var prevDefault = false;
    PROVIDERS.forEach(function (p) {
      if (current === MODEL[p] || current === lastModels[p]) prevDefault = true;
    });
    var nextValue = current;
    if (!current || prevDefault) {
      nextValue = lastModels[next] || MODEL[next] || "";
    }
    fillSelect(els.model, modelsFor(next), nextValue, false);
    lastModels[next] = els.model.value.trim();
    renderCredentials();
    scheduleValidate();
    scheduleModels();
  });

  els.model.addEventListener("change", function () {
    lastModels[els.provider.value] = els.model.value.trim();
    scheduleValidate();
  });

  form.querySelectorAll(".role-provider").forEach(function (select) {
    select.addEventListener("change", function () {
      var role = select.getAttribute("data-role");
      var modelSelect = form.querySelector('.role-model[data-role="' + role + '"]');
      var provider = select.value;
      if (modelSelect) {
        if (provider === "default") fillSelect(modelSelect, [], "", true);
        else fillSelect(modelSelect, modelsFor(provider), "", true);
      }
      renderCredentials();
      scheduleValidate();
      scheduleModels();
    });
  });
  form.querySelectorAll(".role-model").forEach(function (select) {
    select.addEventListener("change", scheduleValidate);
  });

  ["repo", "token", "maxPrs", "state", "since", "until", "maxIterations", "batchSize", "prefilter"].forEach(
    function (key) {
      var el = els[key];
      if (!el) return;
      el.addEventListener("input", scheduleValidate);
      el.addEventListener("change", scheduleValidate);
    }
  );

  els.save.addEventListener("click", function () {
    showFormError("");
    fetch("/api/setup/toml", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overrides: collectOverrides() }),
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (body) {
            throw new Error((body && body.detail) || "Could not save TOML");
          });
        }
        return response.text();
      })
      .then(downloadToml)
      .catch(function (err) {
        showFormError(err.message || String(err));
      });
  });

  els.load.addEventListener("click", function () {
    els.file.value = "";
    els.file.click();
  });

  els.file.addEventListener("change", function () {
    var file = els.file.files && els.file.files[0];
    if (!file) return;
    showFormError("");
    file
      .text()
      .then(function (text) {
        return fetch("/api/setup/parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text }),
        });
      })
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) {
            throw new Error((body && body.detail) || "Could not parse file");
          }
          return body;
        });
      })
      .then(function (fields) {
        applyFields(fields);
        if (!els.moreOptions.hidden) return;
        els.moreToggle.click();
      })
      .catch(function (err) {
        showFormError(err.message || String(err));
      });
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    showFormError("");
    els.start.disabled = true;
    fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        overrides: collectOverrides(),
        secrets: collectSecrets(),
      }),
    })
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) {
            throw new Error((body && body.detail) || "Could not start run");
          }
          return body;
        });
      })
      .then(function (body) {
        window.location.href = "/pipeline?run=" + encodeURIComponent(body.id);
      })
      .catch(function (err) {
        showFormError(err.message || String(err));
        els.start.disabled = false;
      });
  });

  applyModelLists();

  fetch("/api/setup/env")
    .then(function (response) {
      return response.ok ? response.json() : {};
    })
    .then(function (values) {
      envPrefill = values || {};
      if (envPrefill.GITHUB_TOKEN && !els.token.value) {
        els.token.value = envPrefill.GITHUB_TOKEN;
      }
      renderCredentials();
      setHint(
        els.hintGithub,
        envPrefill.GITHUB_TOKEN
          ? "Read from .env · never saved to config"
          : "Never saved to config",
        null
      );
      scheduleModels();
      if (els.repo.value.trim()) scheduleValidate();
    })
    .catch(function () {
      renderCredentials();
      scheduleModels();
    });
})();
