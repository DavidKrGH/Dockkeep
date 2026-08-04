(function () {
  function setThemeIcons(isDark) {
    const moon = document.getElementById("icon-moon");
    const sun = document.getElementById("icon-sun");
    if (!moon || !sun) return;
    moon.classList.toggle("hidden", isDark);
    sun.classList.toggle("hidden", !isDark);
  }

  window.toggleDark = function () {
    const isDark = document.documentElement.classList.toggle("dark");
    localStorage.setItem("theme", isDark ? "dark" : "light");
    setThemeIcons(isDark);
  };

  window.dkFormatBytes = function (value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
    let size = Number(value);
    const units = ["B", "KB", "MB", "GB", "TB"];
    for (let index = 0; index < units.length; index += 1) {
      if (Math.abs(size) < 1024) return size.toFixed(1) + " " + units[index];
      size /= 1024;
    }
    return size.toFixed(1) + " PB";
  };

  window.dkCreateLogStream = function (url, { onLine, onOpen, onError } = {}) {
    const source = new EventSource(url);
    if (onOpen) source.onopen = onOpen;
    if (onLine) {
      source.onmessage = function (event) {
        onLine(event.data);
      };
    }
    if (onError) source.onerror = onError;
    window.addEventListener("beforeunload", function () {
      source.close();
    });
    return source;
  };

  document.addEventListener("DOMContentLoaded", function () {
    setThemeIcons(document.documentElement.classList.contains("dark"));
    document.querySelectorAll("[data-auto-submit]").forEach(function (field) {
      field.addEventListener("change", function () {
        if (field.form) field.form.submit();
      });
    });
    document.querySelectorAll("[data-navigate-select]").forEach(function (field) {
      field.addEventListener("change", function () {
        if (field.value) window.location.href = field.value;
      });
    });
    document.querySelectorAll("dialog.modal-dialog[open]").forEach(function (dialog) {
      if (typeof dialog.showModal !== "function") return;
      dialog.removeAttribute("open");
      dialog.showModal();
      var autofocus = dialog.querySelector("[autofocus]");
      if (autofocus) autofocus.focus();
    });
    document.querySelectorAll("[data-open-dialog]").forEach(function (button) {
      button.addEventListener("click", function () {
        var dialog = document.getElementById(button.dataset.openDialog);
        if (!dialog) return;
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
        var autofocus = dialog.querySelector("[autofocus]");
        if (autofocus) autofocus.focus();
      });
    });
    document.querySelectorAll("[data-close-dialog]").forEach(function (button) {
      button.addEventListener("click", function () {
        var dialog = document.getElementById(button.dataset.closeDialog);
        if (!dialog) return;
        if (typeof dialog.close === "function") {
          dialog.close();
        } else {
          dialog.removeAttribute("open");
        }
      });
    });
    document.querySelectorAll("[data-snapshot-path-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        var row = document.getElementById(button.getAttribute("aria-controls"));
        if (!row) return;
        var expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", expanded ? "false" : "true");
        row.hidden = expanded;
      });
    });
  });
})();
