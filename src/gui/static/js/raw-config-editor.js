(function () {
  function escapeHtml(value) {
    return value
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function token(className, value) {
    return '<span class="code-token-' + className + '">' + escapeHtml(value) + "</span>";
  }

  function commentIndex(line, marker) {
    let quote = "";
    for (let index = 0; index < line.length; index += 1) {
      const character = line[index];
      if (quote && character === quote && line[index - 1] !== "\\") quote = "";
      else if (!quote && (character === '"' || character === "'")) quote = character;
      else if (!quote && character === marker) return index;
    }
    return -1;
  }

  function highlightValues(value) {
    const pattern =
      /("(?:\\.|[^"\\])*"|'[^']*'|\b(?:true|false)\b|\b\d(?:[\d_]*\.?[\d_]*)?(?:[eE][+-]?\d+)?\b)/g;
    let result = "";
    let lastIndex = 0;
    value.replace(pattern, function (match, _capture, offset) {
      result += escapeHtml(value.slice(lastIndex, offset));
      const className = /^(?:"|')/.test(match)
        ? "string"
        : /^(?:true|false)$/.test(match)
          ? "boolean"
          : "number";
      result += token(className, match);
      lastIndex = offset + match.length;
      return match;
    });
    return result + escapeHtml(value.slice(lastIndex));
  }

  function highlightTomlLine(line) {
    const commentAt = commentIndex(line, "#");
    const content = commentAt === -1 ? line : line.slice(0, commentAt);
    const comment = commentAt === -1 ? "" : token("comment", line.slice(commentAt));
    if (/^\s*\[\[?.*\]?\]\s*$/.test(content)) return token("section", content) + comment;

    const assignment = content.match(/^(\s*)([A-Za-z0-9_.-]+)(\s*=)(.*)$/);
    if (!assignment) return highlightValues(content) + comment;
    return (
      escapeHtml(assignment[1]) +
      token("key", assignment[2]) +
      escapeHtml(assignment[3]) +
      highlightValues(assignment[4]) +
      comment
    );
  }

  function highlightIniLine(line) {
    const commentAt = Math.min(
      ...["#", ";"]
        .map(function (marker) {
          const index = commentIndex(line, marker);
          return index === -1 ? line.length : index;
        })
    );
    const content = line.slice(0, commentAt);
    const comment = commentAt === line.length ? "" : token("comment", line.slice(commentAt));
    if (/^\s*\[.*\]\s*$/.test(content)) return token("section", content) + comment;

    const assignment = content.match(/^(\s*)([^=\s][^=]*?)(\s*=)(.*)$/);
    if (!assignment) return escapeHtml(content) + comment;
    return (
      escapeHtml(assignment[1]) +
      token("key", assignment[2]) +
      escapeHtml(assignment[3]) +
      highlightValues(assignment[4]) +
      comment
    );
  }

  function insertText(textarea, text, selectionStart, selectionEnd) {
    textarea.setRangeText(text, selectionStart, selectionEnd, "end");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function changeLineIndent(textarea, removeIndent) {
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const lineStart = textarea.value.lastIndexOf("\n", start - 1) + 1;
    let lineEnd = textarea.value.indexOf("\n", end);
    if (lineEnd === -1) lineEnd = textarea.value.length;
    const lines = textarea.value.slice(lineStart, lineEnd);
    const changed = removeIndent ? lines.replace(/^ {1,2}/gm, "") : lines.replace(/^/gm, "  ");
    const collapsed = start === end;
    textarea.setRangeText(changed, lineStart, lineEnd, collapsed ? "end" : "select");
    if (collapsed) {
      const caret = Math.max(lineStart, start + changed.length - lines.length);
      textarea.setSelectionRange(caret, caret);
    }
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  window.attachPlainEditor = function (options) {
    const textarea = document.getElementById(options.textareaId);
    const form = document.getElementById(options.formId);
    if (!textarea || !form) return;
    const initialValue = textarea.value.replace(/\r\n/g, "\n");
    const mode = options.mode === "ini" ? "ini" : "toml";

    textarea.classList.add("admin-code-editor", "font-mono", "text-sm");
    textarea.setAttribute("spellcheck", "false");
    textarea.setAttribute("autocomplete", "off");
    textarea.setAttribute("autocorrect", "off");
    textarea.setAttribute("autocapitalize", "off");
    textarea.setAttribute("wrap", "off");

    const toolbar = document.createElement("div");
    toolbar.className = "code-editor-toolbar";
    toolbar.innerHTML =
      '<span class="code-editor-status" aria-live="polite">Saved</span>' +
      '<span class="code-editor-shortcuts">Tab: indent · Ctrl/Cmd+F: search · Ctrl/Cmd+S: save</span>';

    const search = document.createElement("div");
    search.className = "code-editor-search hidden";
    search.innerHTML =
      '<label>Search <input type="search" class="code-editor-search-input" autocomplete="off"></label>' +
      '<span class="code-editor-search-result" aria-live="polite"></span>' +
      '<button type="button" data-action="previous" title="Previous match">↑</button>' +
      '<button type="button" data-action="next" title="Next match">↓</button>' +
      '<button type="button" data-action="close" title="Close search">×</button>';

    const body = document.createElement("div");
    body.className = "code-editor-body";
    const gutterViewport = document.createElement("div");
    gutterViewport.className = "code-editor-gutter-viewport";
    gutterViewport.setAttribute("aria-hidden", "true");
    const gutter = document.createElement("pre");
    gutter.className = "code-editor-gutter";
    const stack = document.createElement("div");
    stack.className = "code-editor-stack";
    const overlay = document.createElement("div");
    overlay.className = "code-editor-overlay";
    overlay.setAttribute("aria-hidden", "true");
    const highlighting = document.createElement("pre");
    highlighting.className = "code-editor-highlighting";
    overlay.appendChild(highlighting);

    textarea.parentNode.insertBefore(toolbar, textarea);
    textarea.parentNode.insertBefore(search, textarea);
    textarea.parentNode.insertBefore(body, textarea);
    body.appendChild(gutterViewport);
    gutterViewport.appendChild(gutter);
    body.appendChild(stack);
    stack.appendChild(overlay);
    stack.appendChild(textarea);
    textarea.classList.add("code-editor-input");

    const status = toolbar.querySelector(".code-editor-status");
    const searchInput = search.querySelector(".code-editor-search-input");
    const searchResult = search.querySelector(".code-editor-search-result");

    function render() {
      const lines = textarea.value.split("\n");
      const highlighter = mode === "ini" ? highlightIniLine : highlightTomlLine;
      highlighting.innerHTML = lines.map(highlighter).join("\n") + "\n";
      gutter.textContent = lines.map(function (_line, index) {
        return index + 1;
      }).join("\n");
      status.textContent = textarea.value.replace(/\r\n/g, "\n") === initialValue
        ? "Saved"
        : "Unsaved changes";
      status.classList.toggle("is-dirty", status.textContent !== "Saved");
    }

    function syncScroll() {
      highlighting.style.transform =
        "translate(" + -textarea.scrollLeft + "px, " + -textarea.scrollTop + "px)";
      gutter.style.transform = "translateY(" + -textarea.scrollTop + "px)";
    }

    function find(direction) {
      const query = searchInput.value;
      if (!query) {
        searchResult.textContent = "";
        return;
      }
      const value = textarea.value.toLocaleLowerCase();
      const needle = query.toLocaleLowerCase();
      const from = direction < 0 ? textarea.selectionStart - 1 : textarea.selectionEnd;
      let index = direction < 0 ? value.lastIndexOf(needle, from) : value.indexOf(needle, from);
      if (index === -1) {
        index = direction < 0 ? value.lastIndexOf(needle) : value.indexOf(needle);
      }
      if (index === -1) {
        searchResult.textContent = "No match";
        return;
      }
      textarea.focus();
      textarea.setSelectionRange(index, index + query.length);
      searchResult.textContent = "Match";
    }

    function openSearch() {
      search.classList.remove("hidden");
      searchInput.focus();
      searchInput.select();
    }

    textarea.addEventListener("input", render);
    textarea.addEventListener("scroll", syncScroll);
    textarea.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        form.requestSubmit();
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
        event.preventDefault();
        openSearch();
      } else if (event.key === "Tab") {
        event.preventDefault();
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        if (!event.shiftKey && start === end) {
          insertText(textarea, "  ", start, end);
        } else {
          changeLineIndent(textarea, event.shiftKey);
        }
      }
    });

    search.addEventListener("click", function (event) {
      const action = event.target.getAttribute("data-action");
      if (action === "close") search.classList.add("hidden");
      else if (action === "previous") find(-1);
      else if (action === "next") find(1);
    });
    searchInput.addEventListener("input", function () { find(1); });
    searchInput.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        search.classList.add("hidden");
        textarea.focus();
      } else if (event.key === "Enter") {
        event.preventDefault();
        find(event.shiftKey ? -1 : 1);
      }
    });

    form.addEventListener("submit", function () {
      textarea.value = textarea.value.replace(/\r\n/g, "\n");
    });

    render();
  };
})();
