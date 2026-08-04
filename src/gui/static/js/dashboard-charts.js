(function () {
  const palette = [
    "rgba(59,130,246,0.8)", "rgba(16,185,129,0.8)", "rgba(245,158,11,0.8)",
    "rgba(139,92,246,0.8)", "rgba(239,68,68,0.8)", "rgba(20,184,166,0.8)",
    "rgba(249,115,22,0.8)", "rgba(99,102,241,0.8)",
  ];

  const fmtBytes = window.dkFormatBytes || function (value) { return String(value); };

  function dateLabel(value) {
    return String(value || "").slice(0, 10);
  }

  function shouldShowDateTick(labels, index) {
    const label = dateLabel(labels[index]);
    if (!label) return false;
    return index === 0 || label !== dateLabel(labels[index - 1]);
  }

  function parseData() {
    const dataEl = document.getElementById("dashboard-chart-data");
    if (!dataEl) return {};
    try {
      return JSON.parse(dataEl.textContent || "{}");
    } catch (_error) {
      return {};
    }
  }

  function renderGrowth(data, tickColor, gridColor) {
    const el = document.getElementById("chart-repo-growth");
    if (!el || !data) return;

    const datasets = data.datasets.map(function (dataset, index) {
      return {
        label: dataset.label,
        data: dataset.data,
        borderColor: palette[index % palette.length],
        backgroundColor: palette[index % palette.length].replace("0.8", "0.15"),
        fill: true,
        tension: 0.3,
        pointRadius: 3,
        pointHoverRadius: 5,
        spanGaps: true,
      };
    });

    new Chart(el, {
      type: "line",
      data: {
        labels: data.all_labels,
        datasets: datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: "bottom",
            labels: { color: tickColor, font: { size: 11 }, padding: 10 },
          },
          tooltip: {
            callbacks: {
              title: function (items) {
                return items.length > 0 ? items[0].label : "";
              },
              label: function (ctx) { return " " + ctx.dataset.label + ": " + fmtBytes(ctx.parsed.y); },
            },
          },
        },
        scales: {
          x: {
            ticks: {
              color: tickColor,
              autoSkip: true,
              maxTicksLimit: 6,
              maxRotation: 0,
              minRotation: 0,
              callback: function (_value, index) {
                return shouldShowDateTick(data.all_labels, index) ? dateLabel(data.all_labels[index]) : "";
              },
            },
            grid: { color: gridColor },
          },
          y: {
            ticks: {
              color: tickColor,
              callback: fmtBytes,
            },
            grid: { color: gridColor },
            beginAtZero: true,
          },
        },
      },
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (typeof Chart === "undefined") return;
    const data = parseData();
    const isDark = document.documentElement.classList.contains("dark");
    const tickColor = isDark ? "#94a3b8" : "#64748b";
    const gridColor = isDark ? "#334155" : "#e2e8f0";

    renderGrowth(data.growth, tickColor, gridColor);
  });
})();
