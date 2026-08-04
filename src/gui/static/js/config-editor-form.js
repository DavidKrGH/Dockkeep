(function () {
  "use strict";

  var root = document.documentElement;
  if (root.dataset.configEditorFormInitialized === "true") {
    return;
  }
  root.dataset.configEditorFormInitialized = "true";

  var credentialModeMap = {
    password_env: "env",
    password_file: "file",
    password: "password",
  };

  var cronFields = [
    { min: 0, max: 59, name: "Minute" },
    { min: 0, max: 23, name: "Hour" },
    { min: 1, max: 31, name: "Day" },
    { min: 1, max: 12, name: "Month" },
    { min: 0, max: 7, name: "Weekday" },
  ];

  function updateCredential(select, changed) {
    var wrapper = select.closest("[data-credential-scope]") || select.closest("fieldset");
    if (!wrapper) {
      return;
    }

    var activeCredential = credentialModeMap[select.value] || null;
    wrapper.querySelectorAll("[data-cred]").forEach(function (container) {
      var active = container.dataset.cred === activeCredential;
      container.hidden = !active;
      container.querySelectorAll("input").forEach(function (input) {
        input.disabled = !active;
        if (
          active &&
          changed &&
          (select.dataset.previousMode === "inherit" ||
            select.dataset.previousMode === "unset")
        ) {
          input.value = "";
        }
      });
    });
    select.dataset.previousMode = select.value;
  }

  function updateProvider(select) {
    var wrapper = select.closest("[data-provider]");
    if (!wrapper) {
      return;
    }

    var enabled = select.value === "true";
    wrapper
      .querySelectorAll(
        "[data-provider-fields] input, [data-provider-fields] select, [data-provider-fields] textarea",
      )
      .forEach(function (field) {
        field.disabled = !enabled;
      });
    wrapper.querySelectorAll("[data-provider-test]").forEach(function (button) {
      button.disabled = !enabled;
    });

    if (enabled) {
      wrapper
        .querySelectorAll("[data-provider-fields] details")
        .forEach(function (details) {
          details.open = true;
        });
    }
  }

  function parseCronPart(part, field) {
    var values = new Set();
    var items = part.split(",");
    var unrestricted = part === "*";
    if (!part || items.some(function (item) { return item === ""; })) {
      throw new Error(field.name + " is empty.");
    }

    items.forEach(function (item) {
      var step = 1;
      var base = item;
      if (item.indexOf("/") !== -1) {
        var stepParts = item.split("/");
        if (stepParts.length !== 2 || !/^\d+$/.test(stepParts[1])) {
          throw new Error(field.name + " has an invalid step value.");
        }
        base = stepParts[0];
        step = Number(stepParts[1]);
        if (step < 1) {
          throw new Error(field.name + " needs a step value greater than 0.");
        }
      }

      var start;
      var end;
      if (base === "*") {
        start = field.min;
        end = field.max;
      } else if (base.indexOf("-") !== -1) {
        var range = base.split("-");
        if (range.length !== 2 || !/^\d+$/.test(range[0]) || !/^\d+$/.test(range[1])) {
          throw new Error(field.name + " has an invalid range.");
        }
        start = Number(range[0]);
        end = Number(range[1]);
      } else if (/^\d+$/.test(base)) {
        start = Number(base);
        end = Number(base);
      } else {
        throw new Error(field.name + " may contain only numbers, *, lists, ranges, or steps.");
      }

      if (start < field.min || end > field.max || start > end) {
        throw new Error(field.name + " is outside " + field.min + "-" + field.max + ".");
      }

      for (var value = start; value <= end; value += step) {
        values.add(field.name === "Weekday" && value === 7 ? 0 : value);
      }
    });

    return { values: values, unrestricted: unrestricted };
  }

  function parseCronExpression(expression) {
    var parts = expression.trim().split(/\s+/);
    if (parts.length !== 5) {
      throw new Error("Cron needs exactly 5 fields: minute hour day month weekday.");
    }
    return parts.map(function (part, index) {
      return parseCronPart(part, cronFields[index]);
    });
  }

  function cronMatches(date, parsed) {
    var dayOfMonth = parsed[2].values.has(date.getDate());
    var dayOfWeek = parsed[4].values.has(date.getDay());
    var dayMatches = parsed[2].unrestricted || parsed[4].unrestricted
      ? dayOfMonth && dayOfWeek
      : dayOfMonth || dayOfWeek;

    return (
      parsed[0].values.has(date.getMinutes()) &&
      parsed[1].values.has(date.getHours()) &&
      dayMatches &&
      parsed[3].values.has(date.getMonth() + 1)
    );
  }

  function nextCronRuns(parsed, limit) {
    var runs = [];
    var cursor = new Date();
    cursor.setSeconds(0, 0);
    cursor.setMinutes(cursor.getMinutes() + 1);
    var maxChecks = 60 * 24 * 366 * 2;

    for (var checked = 0; checked < maxChecks && runs.length < limit; checked += 1) {
      if (cronMatches(cursor, parsed)) {
        runs.push(new Date(cursor));
      }
      cursor.setMinutes(cursor.getMinutes() + 1);
    }

    return runs;
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function formatRun(date) {
    var weekday = new Intl.DateTimeFormat("en-US", { weekday: "short" }).format(date);
    return (
      weekday +
      ", " +
      date.getFullYear() +
      "-" +
      pad(date.getMonth() + 1) +
      "-" +
      pad(date.getDate()) +
      " " +
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes())
    );
  }

  function updateCronPreview(input) {
    var wrapper = input.closest("[data-cron-field]");
    if (!wrapper) {
      return;
    }
    var preview = wrapper.querySelector("[data-cron-preview]");
    var status = wrapper.querySelector("[data-cron-status]");
    var runsList = wrapper.querySelector("[data-cron-runs]");
    if (!preview || !status || !runsList) {
      return;
    }

    runsList.innerHTML = "";
    var expression = input.value.trim();
    if (!expression) {
      preview.dataset.state = "empty";
      status.textContent = "No schedule: manual runs only.";
      input.removeAttribute("aria-invalid");
      return;
    }

    try {
      var parsed = parseCronExpression(expression);
      var runs = nextCronRuns(parsed, 3);
      if (!runs.length) {
        throw new Error("No run was found for this expression in the next 2 years.");
      }
      preview.dataset.state = "valid";
      status.textContent = "Automatic execution, upcoming runs:";
      runs.forEach(function (run) {
        var item = document.createElement("li");
        item.textContent = formatRun(run);
        runsList.appendChild(item);
      });
      input.removeAttribute("aria-invalid");
    } catch (error) {
      preview.dataset.state = "invalid";
      status.textContent = error.message;
      input.setAttribute("aria-invalid", "true");
    }
  }

  function selectedBackend(scope) {
    var form = scope.closest("form");
    var select = form ? form.querySelector('select[name="backend"]') : null;
    return select instanceof HTMLSelectElement ? (select.value || "restic") : "";
  }

  function updateFieldInfo(scope) {
    var button = scope.querySelector("[data-field-info-toggle]");
    var panels = Array.prototype.slice.call(scope.querySelectorAll("[data-field-info-panel]"));
    if (!button || !panels.length) {
      return;
    }

    var backend = selectedBackend(scope);
    var expanded = button.getAttribute("aria-expanded") === "true";
    var visiblePanels = panels.filter(function (panel) {
      return !backend || !panel.dataset.backend || panel.dataset.backend === backend;
    });

    button.hidden = visiblePanels.length === 0;
    if (!visiblePanels.length) {
      button.setAttribute("aria-expanded", "false");
      expanded = false;
    }

    panels.forEach(function (panel) {
      panel.hidden = !expanded || visiblePanels.indexOf(panel) === -1;
    });
  }

  function syncListOverride(container) {
    var valueField = container.querySelector("[data-list-value]");
    var emptyField = container.querySelector("[data-list-empty]");
    if (!valueField || !emptyField) {
      return;
    }
    var hasValue = Boolean(valueField.value.trim());
    var editorDisabled = valueField.dataset.editorDisabled === "true";
    if (hasValue) {
      emptyField.checked = false;
    }
    valueField.disabled = editorDisabled || emptyField.checked;
    emptyField.disabled = editorDisabled || hasValue;
  }

  function initializeListOverrides(scope) {
    scope.querySelectorAll("[data-list-empty]").forEach(function (emptyField) {
      var container = emptyField.closest(".form-field-full");
      if (!container || container.dataset.listOverrideInitialized === "true") {
        return;
      }
      var valueField = container.querySelector("[data-list-value]");
      if (!valueField) {
        return;
      }
      container.dataset.listOverrideInitialized = "true";
      valueField.addEventListener("input", function () {
        syncListOverride(container);
      });
      emptyField.addEventListener("change", function () {
        if (emptyField.checked) {
          valueField.value = "";
        }
        syncListOverride(container);
      });
      syncListOverride(container);
    });
  }

  function initializeConfigEditorForms(scope) {
    initializeListOverrides(scope);
    scope.querySelectorAll("[data-credential-mode]").forEach(function (select) {
      updateCredential(select, false);
    });
    scope.querySelectorAll("[data-provider-toggle]").forEach(function (select) {
      updateProvider(select);
    });
    scope.querySelectorAll("[data-cron-schedule]").forEach(function (input) {
      updateCronPreview(input);
    });
    scope.querySelectorAll("[data-field-info-scope]").forEach(function (container) {
      updateFieldInfo(container);
    });
  }

  document.addEventListener("input", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLInputElement) || !target.matches("[data-cron-schedule]")) {
      return;
    }

    updateCronPreview(target);
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLElement) || !target.matches("[data-cron-help-toggle]")) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();

    var wrapper = target.closest("[data-cron-field]");
    var help = wrapper ? wrapper.querySelector("[data-cron-help]") : null;
    if (!help) {
      return;
    }
    var expanded = target.getAttribute("aria-expanded") === "true";
    target.setAttribute("aria-expanded", expanded ? "false" : "true");
    help.hidden = expanded;
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLElement) || !target.matches("[data-field-info-toggle]")) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();

    var wrapper = target.closest("[data-field-info-scope]");
    if (!wrapper) {
      return;
    }
    var expanded = target.getAttribute("aria-expanded") === "true";
    target.setAttribute("aria-expanded", expanded ? "false" : "true");
    updateFieldInfo(wrapper);
  });

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLSelectElement)) {
      return;
    }

    if (target.matches("[data-credential-mode]")) {
      updateCredential(target, true);
      return;
    }

    if (target.matches("[data-provider-toggle]")) {
      updateProvider(target);
      return;
    }

    if (target.matches('select[name="backend"]')) {
      var form = target.closest("form") || document;
      form.querySelectorAll("[data-field-info-scope]").forEach(function (container) {
        updateFieldInfo(container);
      });
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    initializeConfigEditorForms(event.target);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initializeConfigEditorForms(document);
    });
  } else {
    initializeConfigEditorForms(document);
  }
}());
