(function () {
  const dataEl = document.getElementById("log-viewer-data");
  if (!dataEl) return;

  let config = {};
  try {
    config = JSON.parse(dataEl.textContent || "{}");
  } catch (_error) {
    config = {};
  }

  // The static view renders exactly what the page load delivered and never
  // refetches: following a growing log is what the live view (SSE) is for.
  const job = config.job || "";
  const date = config.date || "";
  let evtSource = null;
  let isLive = false;
  let staticSearchQuery = "";
  let taskFilterQuery = "";

  const levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
  const levelActive = { DEBUG: true, INFO: true, WARNING: true, ERROR: true, CRITICAL: true };
  const levelRe = /\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]/;
  const timestampRe = /^\d{2}:\d{2}:\d{2}/;
  const taskRe = /^\d{2}:\d{2}:\d{2} \[\w+\] \[[^\]]+\] \[([^\]]+)\]/;
  const lastTaskByPre = {};

  function getLevelFromLine(line) {
    const match = levelRe.exec(line);
    return match ? match[1] : null;
  }

  // Timestamped lines carry their own task tag (or none); continuation lines
  // (tracebacks, multi-line messages) inherit the task of the line above.
  function taskForLine(preId, line) {
    if (timestampRe.test(line)) {
      const match = taskRe.exec(line);
      lastTaskByPre[preId] = match ? match[1] : "";
    }
    return lastTaskByPre[preId] || "";
  }

  function resetTaskTracking(preId) {
    lastTaskByPre[preId] = "";
  }

  function isLineVisible(line, task) {
    const level = getLevelFromLine(line);
    if (level !== null && levelActive[level] !== true) return false;
    if (taskFilterQuery === "") return true;
    return taskMatchesFilter(task, taskFilterQuery);
  }

  function taskMatchesFilter(task, query) {
    const normalizedTask = String(task || "").trim().toLowerCase();
    const normalizedQuery = String(query || "").trim().toLowerCase();
    if (normalizedTask === "" || normalizedQuery === "") return normalizedQuery === "";

    const taskParts = normalizedTask.split(".");
    const queryParts = normalizedQuery.split(".");
    if (queryParts.length > taskParts.length) return false;

    for (let index = 0; index <= taskParts.length - queryParts.length; index += 1) {
      if (queryParts.every(function (part, offset) { return taskParts[index + offset] === part; })) {
        return true;
      }
    }
    return false;
  }

  window.dkLogTaskMatches = taskMatchesFilter;

  function isStaticLineVisible(line, task) {
    if (!isLineVisible(line, task)) return false;
    return staticSearchQuery === "" || line.toLowerCase().includes(staticSearchQuery);
  }

  function syncButton(level) {
    const btn = document.getElementById("lvl-btn-" + level);
    if (!btn) return;
    btn.classList.toggle("is-active", levelActive[level]);
    btn.setAttribute("aria-pressed", levelActive[level] ? "true" : "false");
  }

  function applyFilter() {
    const livePre = document.getElementById("live-output");
    if (livePre) {
      livePre.querySelectorAll("span[data-task]").forEach(function (span) {
        span.style.display = isLineVisible(lineOfSpan(span), span.dataset.task || "")
          ? ""
          : "none";
      });
    }

    const staticPre = document.getElementById("static-pre");
    if (staticPre) {
      staticPre.querySelectorAll("span[data-task]").forEach(function (span) {
        span.style.display = isStaticLineVisible(lineOfSpan(span), span.dataset.task || "")
          ? ""
          : "none";
      });
    }
  }

  function updateStaticSearch(value) {
    staticSearchQuery = value.trim().toLowerCase();
    const searchInput = document.getElementById("log-search");
    if (searchInput) {
      searchInput.setAttribute("aria-label", staticSearchQuery === "" ? "Search log text" : "Log text filtered");
    }
    applyFilter();
  }

  function clearStaticSearch() {
    const searchInput = document.getElementById("log-search");
    if (searchInput) {
      searchInput.value = "";
    }
    updateStaticSearch("");
  }

  function updateTaskFilter(value) {
    taskFilterQuery = value.trim().toLowerCase();
    const taskInput = document.getElementById("log-task-filter");
    if (taskInput) {
      taskInput.setAttribute("aria-label", taskFilterQuery === "" ? "Filter by task" : "Task filtered");
    }
    applyFilter();
  }

  function clearTaskFilter() {
    const taskInput = document.getElementById("log-task-filter");
    if (taskInput) {
      taskInput.value = "";
    }
    updateTaskFilter("");
  }

  function lineVisibleForPre(pre, line, task) {
    if (pre.id === "static-pre") {
      return isStaticLineVisible(line, task);
    }
    return isLineVisible(line, task);
  }

  // The line text lives only in the span's own text content; duplicating it into
  // a data attribute would double the memory of a large log view for nothing.
  function makeLineSpan(line, task, visible) {
    const span = document.createElement("span");
    span.dataset.task = task;
    span.textContent = line + "\n";
    if (!visible) span.style.display = "none";
    return span;
  }

  function lineOfSpan(span) {
    const text = span.textContent || "";
    return text.endsWith("\n") ? text.slice(0, -1) : text;
  }

  function appendLineSpan(pre, line) {
    const task = taskForLine(pre.id, line);
    pre.appendChild(makeLineSpan(line, task, lineVisibleForPre(pre, line, task)));
  }

  function appendTextFragment(pre, lines) {
    const frag = document.createDocumentFragment();
    lines.forEach(function (line) {
      const task = taskForLine(pre.id, line);
      frag.appendChild(makeLineSpan(line, task, lineVisibleForPre(pre, line, task)));
    });
    pre.appendChild(frag);
  }

  function toggleLevel(level) {
    levelActive[level] = !levelActive[level];
    syncButton(level);
    applyFilter();
  }

  function renderTextInPre(pre, text) {
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = "";
    resetTaskTracking(pre.id);
    const lines = text.split("\n");
    if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
    appendTextFragment(pre, lines);
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  function renderInitialContent() {
    const pre = document.getElementById("static-pre");
    if (!pre || config.content === null || config.content === undefined) return;
    renderTextInPre(pre, config.content);
    requestAnimationFrame(function () {
      pre.scrollTop = pre.scrollHeight;
    });
    window.addEventListener("load", function () {
      pre.scrollTop = pre.scrollHeight;
      requestAnimationFrame(function () {
        pre.scrollTop = pre.scrollHeight;
      });
    });
  }

  function startLive() {
    if (!job) return;
    isLive = true;

    const dateSelect = document.querySelector('select[name="date"]');
    if (dateSelect && dateSelect.options.length > 0) {
      dateSelect.value = dateSelect.options[0].value;
    }

    const liveContainer = document.getElementById("live-container");
    const staticContainer = document.getElementById("static-container");
    const liveStatus = document.getElementById("live-status");
    const output = document.getElementById("live-output");
    if (!liveContainer || !staticContainer || !liveStatus || !output) return;

    liveContainer.classList.remove("hidden");
    staticContainer.classList.add("hidden");
    liveStatus.textContent = "Connecting...";
    output.textContent = "";
    resetTaskTracking(output.id);

    if (evtSource) evtSource.close();
    evtSource = window.dkCreateLogStream("/diagnostics/logs/" + encodeURIComponent(job) + "/stream", {
      onOpen: function () {
        liveStatus.textContent = "Connected";
      },
      onLine: function (line) {
        const atBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 20;
        appendLineSpan(output, line);
        if (atBottom) output.scrollTop = output.scrollHeight;
      },
      onError: function () {
        liveStatus.textContent = "Connection lost";
      },
    });
  }

  function stopLive() {
    isLive = false;
    if (evtSource) {
      evtSource.close();
      evtSource = null;
    }

    const dateSelect = document.querySelector('select[name="date"]');
    if (dateSelect) dateSelect.value = date;

    const liveContainer = document.getElementById("live-container");
    const staticContainer = document.getElementById("static-container");
    if (liveContainer) liveContainer.classList.add("hidden");
    if (staticContainer) staticContainer.classList.remove("hidden");
  }

  function bindControls() {
    document.querySelectorAll("[data-log-level]").forEach(function (button) {
      button.addEventListener("click", function () {
        toggleLevel(button.dataset.logLevel);
      });
    });
    document.querySelectorAll("[data-log-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (button.dataset.logAction === "start-live") startLive();
        else if (button.dataset.logAction === "stop-live") stopLive();
      });
    });
    document.querySelectorAll("[data-log-search]").forEach(function (field) {
      field.addEventListener("input", function () {
        updateStaticSearch(field.value || "");
      });
    });
    document.querySelectorAll("[data-log-search-clear]").forEach(function (button) {
      button.addEventListener("click", clearStaticSearch);
    });
    document.querySelectorAll("[data-log-task-filter]").forEach(function (field) {
      field.addEventListener("input", function () {
        updateTaskFilter(field.value || "");
      });
    });
    document.querySelectorAll("[data-log-task-filter-clear]").forEach(function (button) {
      button.addEventListener("click", clearTaskFilter);
    });
  }

  bindControls();
  renderInitialContent();
  levels.forEach(syncButton);
  window.addEventListener("beforeunload", function () {
    if (evtSource) evtSource.close();
  });
})();
