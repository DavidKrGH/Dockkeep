(function () {
  const credentialModeMap = {
    password_env: "env",
    password_file: "file",
    password: "password",
  };

  function restorePathsField() {
    return document.getElementById("restore-snapshot-paths");
  }

  function selectedRestoreMode() {
    const selected = document.querySelector('input[name="restore_mode"]:checked');
    return selected ? selected.value : "pattern";
  }

  function syncRestoreMode() {
    const mode = selectedRestoreMode();
    const field = restorePathsField();
    const browserPanel = document.getElementById("repository-browser-panel");
    const patternSelection = document.getElementById("restore-pattern-selection");
    const browserSelection = document.getElementById("restore-browser-selection");
    if (field) field.disabled = mode !== "browser";
    if (browserPanel) browserPanel.classList.toggle("hidden", mode !== "browser");
    if (patternSelection) patternSelection.classList.toggle("hidden", mode !== "pattern");
    if (browserSelection) browserSelection.classList.toggle("hidden", mode !== "browser");
    document.querySelectorAll(".restore-mode-option").forEach(function (option) {
      const radio = option.querySelector('input[name="restore_mode"]');
      const isActive = radio && radio.checked;
      option.classList.toggle("is-active", isActive);
    });
    syncPatternFields();
  }

  function useRestoreMode(mode) {
    const selectedMode = document.querySelector('input[name="restore_mode"][value="' + mode + '"]');
    if (!selectedMode) return;
    selectedMode.checked = true;
    syncRestoreMode();
  }

  function useBrowserMode() {
    useRestoreMode("browser");
  }

  function syncPatternFields() {
    const fields = Array.from(document.querySelectorAll(".restore-pattern-field"));
    const patternMode = selectedRestoreMode() === "pattern";
    fields.forEach(function (field) {
      const otherHasValue = fields.some(function (other) {
        return other !== field && other.value.trim();
      });
      field.disabled = !patternMode || otherHasValue;
    });
    const activeLabel = document.getElementById("restore-pattern-filter-active");
    if (activeLabel) {
      activeLabel.classList.toggle(
        "hidden",
        !fields.some(function (field) {
          return field.value.trim();
        })
      );
    }
    syncRestoreScopeSummary();
  }

  function updateCredential(select) {
    const wrapper = select.closest("[data-credential-scope]") || select.closest("fieldset");
    if (!wrapper) return;
    const activeCredential = credentialModeMap[select.value] || null;
    wrapper.querySelectorAll("[data-cred]").forEach(function (container) {
      const active = container.dataset.cred === activeCredential;
      container.hidden = !active;
      container.querySelectorAll("input").forEach(function (input) {
        input.disabled = !active;
      });
    });
  }

  function initializeCredentials(scope) {
    scope.querySelectorAll("[data-credential-mode]").forEach(function (select) {
      updateCredential(select);
      select.addEventListener("change", function () {
        updateCredential(select);
      });
    });
  }

  function syncRestoreScopeSummary() {
    const summary = document.getElementById("restore-scope-summary");
    if (!summary) return;
    if (selectedRestoreMode() === "browser") {
      const field = restorePathsField();
      const paths = field ? readRestorePaths(field) : [];
      summary.textContent =
        paths.length === 1 && paths[0] === "/"
          ? "Scope: whole snapshot"
          : "Scope: " + paths.length + " selected paths";
      return;
    }
    const include = document.querySelector('textarea[name="include_patterns"]');
    const exclude = document.querySelector('textarea[name="exclude_patterns"]');
    if (include && include.value.trim()) {
      summary.textContent = "Scope: whole snapshot with include patterns";
    } else if (exclude && exclude.value.trim()) {
      summary.textContent = "Scope: whole snapshot with exclude patterns";
    } else {
      summary.textContent = "Scope: whole snapshot";
    }
  }

  function readRestorePaths(field) {
    return field.value
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
  }

  function writeRestorePaths(field, paths) {
    const unique = [];
    paths.forEach((path) => {
      if (path && !unique.includes(path)) unique.push(path);
    });
    field.value = unique.length ? unique.join("\n") : "/";
    try {
      sessionStorage.setItem(field.dataset.storageKey, field.value);
    } catch (_error) {
      // Restore selection still works when browser storage is unavailable.
    }
    renderRestorePaths();
  }

  function renderRestorePaths() {
    const field = restorePathsField();
    const container = document.getElementById("restore-selected-paths");
    const count = document.getElementById("restore-selected-path-count");
    if (!field || !container || !count) return;
    const paths = readRestorePaths(field);
    container.replaceChildren();
    paths.forEach(function (path) {
      const row = document.createElement("div");
      row.className = "flex items-center justify-between gap-2";

      const label = document.createElement("code");
      label.className = "break-all text-xs text-slate-700 dark:text-slate-300";
      label.textContent = path;
      row.appendChild(label);

      if (path !== "/") {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-ghost btn-sm shrink-0";
        remove.textContent = "Remove";
        remove.addEventListener("click", function () {
          window.dkRemoveRestorePath(remove, path);
        });
        row.appendChild(remove);
      }
      container.appendChild(row);
    });
    count.textContent =
      paths.length === 1 && paths[0] === "/"
        ? "The whole snapshot is selected."
        : paths.length + " path(s) selected. The selection is preserved while browsing.";
    syncRestoreScopeSummary();
  }

  window.dkAddRestorePath = function (_button, path) {
    const field = restorePathsField();
    if (!field || !path) return;
    useBrowserMode();
    const current = readRestorePaths(field).filter((value) => value !== "/");
    writeRestorePaths(field, current.concat([path]));
    syncBrowserSelectionState();
  };

  window.dkSetRestorePathsFromChecked = function (button) {
    const field = restorePathsField();
    const browser = button ? button.closest("#repository-browser") : null;
    if (!field || !browser) return;
    const paths = Array.from(browser.querySelectorAll(".restore-path-checkbox:checked"))
      .map((checkbox) => checkbox.value)
      .filter(Boolean);
    if (paths.length === 0) return;
    useBrowserMode();
    const current = readRestorePaths(field).filter((value) => value !== "/");
    writeRestorePaths(field, current.concat(paths));
    syncBrowserSelectionState();
  };

  window.dkRemoveRestorePath = function (_button, path) {
    const field = restorePathsField();
    if (!field || !path) return;
    useBrowserMode();
    const updated = readRestorePaths(field).filter((value) => value !== path);
    writeRestorePaths(field, updated);
    syncBrowserSelectionState();
  };

  function syncBrowserSelectionState() {
    const field = restorePathsField();
    const browser = document.getElementById("repository-browser");
    if (!field || !browser) return;
    const selected = new Set(readRestorePaths(field));
    browser.querySelectorAll(".restore-path-checkbox").forEach(function (checkbox) {
      const isSelected = selected.has(checkbox.value);
      checkbox.checked = isSelected;
      const row = checkbox.closest("tr");
      if (row) {
        row.classList.toggle("already-selected", isSelected);
        const removeBtn = row.querySelector(".only-when-selected");
        if (removeBtn) removeBtn.classList.toggle("hidden", !isSelected);
      }
    });
  }

  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.target && event.target.id === "repository-browser") {
      syncBrowserSelectionState();
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    const field = restorePathsField();
    if (!field) return;
    try {
      const stored = sessionStorage.getItem(field.dataset.storageKey);
      if (stored) field.value = stored;
    } catch (_error) {
      // Use the template default when browser storage is unavailable.
    }
    renderRestorePaths();
    syncBrowserSelectionState();
    document.querySelectorAll('input[name="restore_mode"]').forEach(function (field) {
      field.addEventListener("change", syncRestoreMode);
    });
    document.querySelectorAll(".restore-pattern-field").forEach(function (field) {
      field.addEventListener("input", syncPatternFields);
    });
    initializeCredentials(document);
    syncRestoreMode();
  });
})();
