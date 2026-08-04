(function () {
  const output = document.getElementById("run-live-output");
  if (!output || !output.dataset.logStreamUrl) return;

  window.dkCreateLogStream(output.dataset.logStreamUrl, {
    onLine: function (line) {
      output.insertAdjacentText("beforeend", line + "\n");
      output.scrollTop = output.scrollHeight;
    },
  });
})();
