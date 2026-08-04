(function () {
  function initPlainEditors() {
    document.querySelectorAll("[data-plain-editor]").forEach(function (textarea) {
      if (textarea.dataset.editorAttached === "true") return;
      textarea.dataset.editorAttached = "true";
      window.attachPlainEditor({
        formId: textarea.dataset.editorForm,
        textareaId: textarea.id,
        mode: textarea.dataset.editorMode,
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPlainEditors);
  } else {
    initPlainEditors();
  }
})();
