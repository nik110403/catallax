/*
 * Catallax weekly report pack — hand-written, dependency-free chart renderer
 * (Phase 17). Deliberately NOT a vendored copy of Chart.js/d3/etc: the
 * brief prefers a small self-contained renderer over a large library ("zero
 * network at render time" — Playwright's `page.set_content()` never fetches
 * anything, and there is nothing here to fetch). Canvas-based; print-media
 * aware (no `@media screen` gating — Chromium's print emulation must see the
 * exact same canvas the screen would).
 *
 * Contract with the Jinja template (report_pack.html):
 *   - A `<script type="application/json" id="report-chart-data">` element
 *     holds one JSON object keyed by chart id, each value `{type, ...}`.
 *   - Canvas elements carry `data-chart="<key>"` to be matched against it.
 *   - When done (successfully or not — a malformed/missing data blob must
 *     never hang the render), this script sets
 *     `document.documentElement.setAttribute('data-render-done', 'true')`.
 *     Drawing is synchronous (canvas 2D, no image loads), so this fires
 *     immediately after the draw pass — including the zero-charts case.
 *
 * design review fix round (P1.5/P2.12): `drawLines` now takes an optional
 * third `xLabels` arg (a date axis + min/max annotation, for the
 * regime-history chart), `drawBars` paints a date-tick x-axis from the
 * `label` field the data already carried, and `drawDonut` prefers each
 * slice's own `d.color` (assigned in Python) over the old index-cycling
 * PALETTE lookup, so "Other" can be pinned to a fixed neutral grey instead
 * of inheriting whatever ramp color its index happened to land on.
 */
(function () {
  "use strict";

  function readChartData() {
    var el = document.getElementById("report-chart-data");
    if (!el) return {};
    try {
      return JSON.parse(el.textContent || "{}");
    } catch (e) {
      return {};
    }
  }

  function ctx2d(canvas) {
    var w = canvas.width || 600;
    var h = canvas.height || 300;
    var c = canvas.getContext("2d");
    c.clearRect(0, 0, w, h);
    return c;
  }

  var PALETTE = ["#1b3a5c", "#3d6d9e", "#7ba7cc", "#b8d0e6", "#c98a3a", "#8a3a3a", "#5c8a5c"];
  // design review fix round: axis/caption text needs a font stack that
  // actually resolves in the render container (only Liberation Serif is
  // installed there — see report_pack.html's CSS comment) — canvas text
  // follows the same system-font substitution CSS does, so the stack
  // matches what report_pdf.py now declares first everywhere else.
  var LABEL_FONT = "8px 'Liberation Serif', Georgia, serif";
  var LABEL_COLOR = "#666666";

  function drawDonut(canvas, data) {
    data = Array.isArray(data) ? data : [];
    var c = ctx2d(canvas);
    var w = canvas.width, h = canvas.height;
    var cx = w / 2, cy = h / 2;
    var rOuter = Math.min(w, h) / 2 - 6;
    var rInner = rOuter * 0.55;
    var total = data.reduce(function (s, d) { return s + (Number(d.value) || 0); }, 0);
    if (total <= 0) return;
    var start = -Math.PI / 2;
    data.forEach(function (d, i) {
      var frac = (Number(d.value) || 0) / total;
      var end = start + frac * Math.PI * 2;
      c.beginPath();
      c.moveTo(cx, cy);
      c.arc(cx, cy, rOuter, start, end);
      c.closePath();
      // Design review P2.12: each slice now carries its own `color` (Python
      // assigns it — `report_pdf.py`'s `_allocation_breakdown` — "Other"
      // always the fixed neutral grey, real positions a de-duplicated
      // 8-color ramp). Falls back to the old index-cycling PALETTE for any
      // OTHER donut caller that doesn't supply per-slice colors.
      c.fillStyle = d.color || PALETTE[i % PALETTE.length];
      c.fill();
      start = end;
    });
    c.beginPath();
    c.arc(cx, cy, rInner, 0, Math.PI * 2);
    c.fillStyle = "#ffffff";
    c.fill();
  }

  // Picks a small, readable subset of indices to label along an axis —
  // first, last, and evenly-spaced interior ticks — never every single
  // point (a 60-bar factor-IC series would be unreadable text soup).
  // Deliberately just 3 ticks (first/middle/last) rather than more — a
  // date label is wide ("2026-08-10") and a chart this small has no room
  // to guarantee non-overlap for anything denser once real (uneven) date
  // strings are involved; 3 fixed positions are ALWAYS well-spaced by
  // construction, which "a few interior ticks" only promises loosely.
  function pickLabelIndices(n) {
    if (n <= 0) return [];
    if (n === 1) return [0];
    if (n === 2) return [0, 1];
    var mid = Math.floor((n - 1) / 2);
    var idxs = [0, mid, n - 1];
    return idxs.filter(function (v, i, a) { return a.indexOf(v) === i; });
  }

  // The first/last tick sits at the very edge of the plot area — centering
  // text there overflows the canvas (and gets silently clipped) on one
  // side. Anchor edge ticks outward instead; only an interior tick centers.
  function edgeAlign(i, n) {
    if (i === 0) return "left";
    if (i === n - 1) return "right";
    return "center";
  }

  function drawLines(canvas, series, xLabels) {
    series = Array.isArray(series) ? series : [];
    var c = ctx2d(canvas);
    var w = canvas.width, h = canvas.height;
    var padL = 36, padR = 8, padT = 10, padB = 22;
    var allPoints = [];
    series.forEach(function (s) { (s.points || []).forEach(function (p) { allPoints.push(p); }); });
    if (allPoints.length === 0) return;
    var xs = allPoints.map(function (p) { return p.x; });
    var ys = allPoints.map(function (p) { return p.y; });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    if (minY === maxY) { minY -= 1; maxY += 1; }
    if (minX === maxX) { minX -= 1; maxX += 1; }
    var plotW = w - padL - padR, plotH = h - padT - padB;
    function px(x) { return padL + ((x - minX) / (maxX - minX)) * plotW; }
    function py(y) { return padT + plotH - ((y - minY) / (maxY - minY)) * plotH; }

    // axes
    c.strokeStyle = "#c7c7c7";
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(padL, padT);
    c.lineTo(padL, padT + plotH);
    c.lineTo(padL + plotW, padT + plotH);
    c.stroke();

    series.forEach(function (s, i) {
      var pts = s.points || [];
      if (pts.length === 0) return;
      c.strokeStyle = PALETTE[i % PALETTE.length];
      c.lineWidth = 2;
      c.beginPath();
      pts.forEach(function (p, j) {
        var x = px(p.x), y = py(p.y);
        if (j === 0) c.moveTo(x, y); else c.lineTo(x, y);
      });
      c.stroke();
    });

    // Design review P1.5: a date x-axis (first/last + interior ticks) and a
    // min/max value annotation — only when the caller supplies `xLabels`
    // (currently just the regime-history line; the two-point actual-vs-
    // do-nothing scorecard chart has no dates to label and stays as-is).
    if (Array.isArray(xLabels) && xLabels.length) {
      c.save();
      c.fillStyle = LABEL_COLOR;
      c.font = LABEL_FONT;
      pickLabelIndices(xLabels.length).forEach(function (i) {
        if (i >= xLabels.length) return;
        c.textAlign = edgeAlign(i, xLabels.length);
        c.fillText(String(xLabels[i]), px(i), padT + plotH + 12);
      });
      c.textAlign = "left";
      c.fillText("max " + maxY.toFixed(2), padL + 2, padT + 8);
      c.fillText("min " + minY.toFixed(2), padL + 2, padT + plotH - 3);
      c.restore();
    }
  }

  function drawBars(canvas, data) {
    data = Array.isArray(data) ? data : [];
    var c = ctx2d(canvas);
    var w = canvas.width, h = canvas.height;
    var padL = 40, padR = 10, padT = 10, padB = 30;
    var plotW = w - padL - padR, plotH = h - padT - padB;
    if (data.length === 0) return;
    var values = data.map(function (d) { return Number(d.value) || 0; });
    var maxAbs = Math.max.apply(null, values.map(Math.abs)) || 1;
    var zeroY = padT + plotH / 2;
    var barW = plotW / data.length;
    // Zero baseline — the reference line every bar's sign/height reads
    // against (a factor-IC series straddles zero; without this a reader
    // has no fixed reference for "how positive/negative is this bar").
    c.strokeStyle = "#c7c7c7";
    c.beginPath();
    c.moveTo(padL, zeroY);
    c.lineTo(padL + plotW, zeroY);
    c.stroke();
    data.forEach(function (d, i) {
      var v = Number(d.value) || 0;
      var barH = (Math.abs(v) / maxAbs) * (plotH / 2 - 4);
      var x = padL + i * barW + barW * 0.2;
      var bw = barW * 0.6;
      c.fillStyle = v >= 0 ? "#3d6d9e" : "#8a3a3a";
      if (v >= 0) c.fillRect(x, zeroY - barH, bw, barH);
      else c.fillRect(x, zeroY, bw, barH);
    });

    // Design review P1.5: the chart-data JSON already carries `d.label`
    // (the date) per bar — first/last + a few interior ticks along the
    // bottom, same subset-picking as the line chart's date axis.
    c.save();
    c.fillStyle = LABEL_COLOR;
    c.font = LABEL_FONT;
    pickLabelIndices(data.length).forEach(function (i) {
      var d = data[i];
      if (!d || !d.label) return;
      c.textAlign = edgeAlign(i, data.length);
      var x = padL + i * barW + barW * 0.5;
      c.fillText(String(d.label), x, padT + plotH + 12);
    });
    // Fix round 7 (design review verify): the regime-history line chart
    // already annotates its min/max value in the corners (`drawLines`
    // above) — factor IC bars had a zero baseline but no min/max reading,
    // the one bit of "how big is the biggest bar" a reader can't get from
    // the bars alone. IC values are small (~0.01-0.1) — 4dp mirrors the
    // scoreboard table's `| num(4)`, not the line chart's 2dp.
    var maxV = Math.max.apply(null, values), minV = Math.min.apply(null, values);
    c.textAlign = "left";
    c.fillText("max " + maxV.toFixed(4), padL + 2, padT + 8);
    c.fillText("min " + minV.toFixed(4), padL + 2, padT + plotH - 3);
    c.restore();
  }

  function drawSparkline(canvas, points) {
    points = Array.isArray(points) ? points.map(Number).filter(function (v) { return !isNaN(v); }) : [];
    var c = ctx2d(canvas);
    var w = canvas.width, h = canvas.height;
    if (points.length < 2) return;
    var minV = Math.min.apply(null, points), maxV = Math.max.apply(null, points);
    if (minV === maxV) { minV -= 1; maxV += 1; }
    var pad = 3;
    var plotW = w - pad * 2, plotH = h - pad * 2;
    c.strokeStyle = points[points.length - 1] >= points[0] ? "#5c8a5c" : "#8a3a3a";
    c.lineWidth = 1.5;
    c.beginPath();
    points.forEach(function (v, i) {
      var x = pad + (i / (points.length - 1)) * plotW;
      var y = pad + plotH - ((v - minV) / (maxV - minV)) * plotH;
      if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
    });
    c.stroke();
  }

  function renderAll() {
    var data = readChartData();
    var canvases = document.querySelectorAll("canvas[data-chart]");
    for (var i = 0; i < canvases.length; i++) {
      var canvas = canvases[i];
      var key = canvas.getAttribute("data-chart");
      var spec = data[key];
      if (!spec || !canvas.getContext) continue;
      try {
        if (spec.type === "donut") drawDonut(canvas, spec.data);
        else if (spec.type === "lines") drawLines(canvas, spec.series, spec.x_labels);
        else if (spec.type === "bars") drawBars(canvas, spec.data);
        else if (spec.type === "sparkline") drawSparkline(canvas, spec.points);
      } catch (e) {
        // A malformed chart spec must never block the render-done flag.
      }
    }
  }

  try {
    renderAll();
  } finally {
    document.documentElement.setAttribute("data-render-done", "true");
  }
})();
