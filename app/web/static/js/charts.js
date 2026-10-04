/* FAMILYFIN charts: renders every <canvas data-chart='{...}'> with Chart.js,
   themed from the CSS tokens, and keeps label colours consistent per page
   (the same category gets the same colour in the donut, bars and lists). */
(function () {
  "use strict";

  // Same palette as the desktop app (app/gui/dashboard.py _CHART_PALETTE).
  var PALETTE = ["#2563EB", "#16A34A", "#DC2626", "#D97706", "#7C3AED",
                 "#0891B2", "#DB2777", "#65A30D", "#EA580C", "#6366F1"];
  var GREY = "#94A3B8";
  var colorMap = {};
  var nextColor = 0;
  var charts = [];

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // Stable per name across every screen and visit: the same category is
  // always the same colour. Collisions inside one chart are resolved by
  // taking the next free palette colour.
  function hash(text) {
    var h = 2166136261;
    for (var i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function colorFor(label) {
    if (!label) return GREY;
    if (label.indexOf("Sin categor") === 0) return GREY;
    if (!(label in colorMap)) {
      var used = Object.keys(colorMap).map(function (k) { return colorMap[k]; });
      var start = hash(label) % PALETTE.length, pick = PALETTE[start];
      for (var j = 0; j < PALETTE.length && used.indexOf(pick) >= 0 && used.length < PALETTE.length; j++) {
        pick = PALETTE[(start + j) % PALETTE.length];
      }
      colorMap[label] = pick;
    }
    return colorMap[label];
  }

  function token(name) {
    return { income: css("--income"), expense: css("--expense"), investment: css("--investment"),
             accent: css("--accent"), muted: css("--muted"), warning: css("--warning") }[name] || name;
  }

  var eur = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", maximumFractionDigits: 2 });
  var eurShort = new Intl.NumberFormat("es-ES", { notation: "compact", maximumFractionDigits: 1 });
  function money(v) { return v == null ? "—" : eur.format(v); }
  function short(v) { return eurShort.format(v) + " €"; }

  function alpha(hex, a) {
    var n = parseInt(hex.replace("#", ""), 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }

  function baseOptions(data, isMoney) {
    var text2 = css("--text-2"), border = css("--border");
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 450 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: css("--surface"), titleColor: css("--text"), bodyColor: css("--text-2"),
          borderColor: css("--border-strong"), borderWidth: 1,
          titleFont: { weight: "600", size: 12 }, bodyFont: { size: 12 },
          padding: 12, cornerRadius: 10, boxPadding: 5, usePointStyle: true, caretSize: 0,
          callbacks: {
            label: function (ctx) {
              var v = ctx.parsed.y !== undefined ? ctx.parsed.y : ctx.parsed;
              var name = ctx.dataset.label || ctx.label || "";
              return " " + name + ": " + (isMoney ? money(v) : v);
            }
          }
        }
      },
      scales: {
        x: { grid: { display: false }, border: { display: false },
             ticks: { color: text2, maxRotation: 0, autoSkip: true, autoSkipPadding: 14, maxTicksLimit: 8, font: { size: 11 } } },
        y: { grid: { color: border, drawTicks: false }, border: { display: false, dash: [3, 4] },
             ticks: { color: text2, padding: 8, maxTicksLimit: 5, font: { size: 11 },
                      callback: function (v) { return isMoney ? short(v) : v; } } }
      }
    };
  }

  function build(canvas) {
    var data;
    try { data = JSON.parse(canvas.getAttribute("data-chart")); } catch (e) { return; }
    var isMoney = canvas.getAttribute("data-money") !== "0";
    var opts = baseOptions(data, isMoney);
    var cfg;

    if (data.type === "doughnut") {
      var colors = data.labels.map(colorFor);
      cfg = {
        type: "doughnut",
        data: { labels: data.labels, datasets: [{ data: data.values, backgroundColor: colors,
                borderColor: css("--surface"), borderWidth: 3, borderRadius: 4, hoverOffset: 6 }] },
        options: Object.assign(opts, { cutout: "72%", scales: {}, interaction: { mode: "nearest", intersect: true } })
      };
      var total = data.values.reduce(function (a, b) { return a + b; }, 0);
      cfg.plugins = [{
        id: "center",
        afterDraw: function (chart) {
          var area = chart.chartArea, ctx = chart.ctx;
          ctx.save();
          ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillStyle = css("--text-3"); ctx.font = "500 12px " + css("--font");
          ctx.fillText("Total", (area.left + area.right) / 2, (area.top + area.bottom) / 2 - 12);
          ctx.fillStyle = css("--text"); ctx.font = "700 18px " + css("--font");
          ctx.fillText(money(total), (area.left + area.right) / 2, (area.top + area.bottom) / 2 + 9);
          ctx.restore();
        }
      }];
    } else if (data.type === "bar" || data.type === "stacked") {
      var stacked = data.type === "stacked";
      cfg = {
        type: "bar",
        data: { labels: data.labels, datasets: data.series.map(function (s) {
          var c = s.color ? token(s.color) : colorFor(s.name);
          return { label: s.name, data: s.values, backgroundColor: c, borderRadius: stacked ? 3 : 6,
                   borderSkipped: false, maxBarThickness: 22, categoryPercentage: .7, barPercentage: .85 };
        }) },
        options: opts
      };
      if (stacked) { opts.scales.x.stacked = true; opts.scales.y.stacked = true; }
    } else {
      var series = data.series || [{ name: "Importe", color: data.color || "accent", values: data.values }];
      cfg = {
        type: "line",
        data: { labels: data.labels, datasets: series.map(function (s, i) {
          var c = token(s.color || "accent");
          return { label: s.name, data: s.values, borderColor: c, borderWidth: s.dashed ? 1.6 : 2.2,
                   borderDash: s.dashed ? [5, 4] : [], pointRadius: 0, pointHoverRadius: 4, tension: .25,
                   spanGaps: true, fill: i === 0 && !s.dashed,
                   backgroundColor: function (ctx) {
                     var area = ctx.chart.chartArea; if (!area) return "transparent";
                     var g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
                     g.addColorStop(0, alpha(c, .22)); g.addColorStop(1, alpha(c, 0));
                     return g;
                   } };
        }) },
        options: opts
      };
    }

    var chart = new Chart(canvas, cfg);
    charts.push(chart);
    renderLegend(canvas, chart, data);
  }

  function renderLegend(canvas, chart, data) {
    var box = document.querySelector('[data-legend-for="' + canvas.id + '"]');
    if (!box) return;
    box.textContent = "";
    var items = data.type === "doughnut"
      ? data.labels.map(function (l, i) { return { name: l, color: chart.data.datasets[0].backgroundColor[i] }; })
      : chart.data.datasets.map(function (d, i) {
          var s = (data.series || [])[i] || {};
          return { name: d.label, color: d.borderColor && data.type !== "bar" && data.type !== "stacked" ? d.borderColor : d.backgroundColor, dashed: s.dashed };
        });
    items.slice(0, 12).forEach(function (it) {
      var span = document.createElement("span");
      var dot = document.createElement("i");
      if (it.dashed) dot.className = "dash"; else dot.style.background = it.color;
      span.appendChild(dot);
      span.appendChild(document.createTextNode(it.name));
      box.appendChild(span);
    });
  }

  function paintSwatches(root) {
    root.querySelectorAll("[data-swatch]").forEach(function (el) {
      el.style.background = colorFor(el.getAttribute("data-swatch"));
    });
    root.querySelectorAll("[data-swatch-bg]").forEach(function (el) {
      el.style.background = colorFor(el.getAttribute("data-swatch-bg"));
    });
    root.querySelectorAll("[data-pct]").forEach(function (el) {
      el.style.width = Math.max(1.5, Math.min(100, parseFloat(el.getAttribute("data-pct")) || 0)) + "%";
    });
  }

  function init(root, fresh) {
    if (fresh) {
      charts.forEach(function (c) { c.destroy(); });
      charts = []; colorMap = {}; nextColor = 0;
    }
    if (window.Chart) {
      Chart.defaults.font.family = css("--font");
      root.querySelectorAll("canvas[data-chart]").forEach(function (c) {
        if (!c.__ffChart) { c.__ffChart = true; build(c); }
      });
    }
    paintSwatches(root);
  }

  function rethemeAll() {
    var canvases = document.querySelectorAll("canvas[data-chart]");
    charts.forEach(function (c) { c.destroy(); });
    charts = [];
    canvases.forEach(function (c) { c.__ffChart = false; });
    colorMap = {}; nextColor = 0;
    init(document, false);
  }

  window.FFCharts = { init: init, retheme: rethemeAll };
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rethemeAll);
})();
