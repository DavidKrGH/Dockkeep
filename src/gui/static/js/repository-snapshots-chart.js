(function () {
  const fmtBytes = window.dkFormatBytes || function (value) { return String(value); };

  function dateLabel(value) {
    return String(value || "").slice(0, 10);
  }

  function shouldShowDateTick(labels, index) {
    const label = dateLabel(labels[index]);
    if (!label) return false;
    return index === 0 || label !== dateLabel(labels[index - 1]);
  }

  document.addEventListener("DOMContentLoaded", function () {
    const growthEl = document.getElementById("chart-backup-growth");
    const dataEl = document.getElementById("repository-growth-data");
    if (!growthEl || !dataEl || typeof Chart === "undefined") return;

    let growthData = {};
    try {
      growthData = JSON.parse(dataEl.textContent || "{}");
    } catch (_error) {
      return;
    }

    const isDark = document.documentElement.classList.contains("dark");
    const gridColor = isDark ? "rgba(148,163,184,0.12)" : "rgba(100,116,139,0.12)";
    const tickColor = isDark ? "#94a3b8" : "#64748b";

    new Chart(growthEl, {
      type: "line",
      data: {
        labels: growthData.labels,
        datasets: [{
          label: "Repo size",
          data: growthData.data,
          borderColor: "rgba(59,130,246,0.9)",
          backgroundColor: "rgba(59,130,246,0.08)",
          fill: true,
          tension: 0.3,
          pointRadius: 3,
          pointHoverRadius: 5,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: function (items) {
                return items.length > 0 ? items[0].label : "";
              },
              label: function (ctx) { return growthData.formatted[ctx.dataIndex]; },
            },
          },
        },
        scales: {
          x: {
            grid: { color: gridColor },
            ticks: {
              color: tickColor,
              autoSkip: true,
              maxTicksLimit: 6,
              maxRotation: 0,
              minRotation: 0,
              font: { size: 10 },
              callback: function (_value, index) {
                return shouldShowDateTick(growthData.labels, index) ? dateLabel(growthData.labels[index]) : "";
              },
            },
          },
          y: {
            grid: { color: gridColor },
            ticks: { color: tickColor, font: { size: 10 }, callback: fmtBytes },
          },
        },
      },
    });
  });
})();
