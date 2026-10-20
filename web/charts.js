// Tiny dependency-free SVG charts (line with crosshair tooltip, bars with hover).
(function () {
  const { el } = window.Sigen;

  function niceTicks(min, max, count = 5) {
    if (min === max) { max = min + 1; }
    const span = max - min;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(6));
    return ticks;
  }

  function timeTicks(t0, t1, maxLabels = 10) {
    const span = t1 - t0, H = 3600, out = [];
    let stepH = span <= 50 * H ? (span <= 12 * H ? 2 : span <= 30 * H ? 3 : 6) : null;
    if (stepH) while (span / H / stepH > maxLabels && stepH < 12) stepH = stepH === 3 ? 6 : stepH * 2;
    if (stepH) {
      const d = new Date(t0 * 1000); d.setMinutes(0, 0, 0);
      let t = d.getTime() / 1000;
      while (t <= t1) {
        const hr = new Date(t * 1000).getHours();
        if (t >= t0 && hr % stepH === 0) {
          out.push({ t, label: hr === 0
            ? new Date(t * 1000).toLocaleDateString([], { weekday: "short" })
            : new Date(t * 1000).toLocaleTimeString([], { hour: "numeric" }) });
        }
        t += H;
      }
    } else {
      const days = span / 86400;
      let every = days <= 8 ? 1 : days <= 16 ? 2 : 5;
      while (days / every > maxLabels) every += 1;
      const d = new Date(t0 * 1000); d.setHours(0, 0, 0, 0);
      let i = 0;
      for (let t = d.getTime() / 1000; t <= t1; t += 86400, i++) {
        if (t >= t0 && i % every === 0) {
          out.push({ t, label: new Date(t * 1000).toLocaleDateString([], { weekday: "short", day: "numeric" }) });
        }
      }
    }
    return out;
  }

  function tipPosition(tip, wrap, x, y) {
    tip.style.display = "block";
    const w = tip.offsetWidth, W = wrap.clientWidth;
    let left = x + 14;
    if (left + w > W) left = x - w - 14;
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = Math.max(0, y - 10) + "px";
  }

  /**
   * opts: { t:[epoch s], series:[{key,label,color,values,dash}], hidden:Set, height,
   *         yFmt(v)->string, tipFmt(v)->string, yMin, yMax, gapSeconds, tipTitle(t) }
   */
  function lineChart(wrap, opts) {
    wrap.innerHTML = "";
    const t = opts.t || [];
    if (!t.length) { wrap.append(el("div", { class: "empty", text: opts.emptyText || "No history yet. It fills in as readings arrive." })); return; }
    const W = Math.max(280, wrap.clientWidth), H = opts.height || 260;
    const m = { l: 46, r: 10, t: 10, b: 26 };
    const visible = opts.series.filter((s) => !opts.hidden || !opts.hidden.has(s.key));
    let lo = opts.yMin ?? Infinity, hi = opts.yMax ?? -Infinity;
    if (opts.yMin == null || opts.yMax == null) {
      for (const s of visible) for (const v of s.values) if (v != null) {
        if (opts.yMin == null) lo = Math.min(lo, v);
        if (opts.yMax == null) hi = Math.max(hi, v);
      }
      if (!isFinite(lo)) lo = 0; if (!isFinite(hi)) hi = 1;
      if (opts.yMin == null) lo = Math.min(lo, 0);
    }
    const ticks = opts.yTicks || niceTicks(lo, hi, opts.tickCount || 5);
    lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    const t0 = t[0], t1 = Math.max(opts.stepEnd || 0, t[t.length - 1] || t0 + 1);
    const x = (v) => m.l + ((v - t0) / Math.max(1, t1 - t0)) * (W - m.l - m.r);
    const y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (H - m.t - m.b);

    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.ariaLabel || "Chart" });
    if (opts.markerT != null && opts.markerT > t0 && opts.markerT < t1) {
      const xm = x(opts.markerT);
      svg.append(el("rect", { class: "future", x: xm, y: m.t, width: W - m.r - xm, height: H - m.t - m.b }));
    }
    for (const tv of ticks) {
      svg.append(el("line", { class: tv === 0 ? "zero" : "gridline", x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv) }));
      svg.append(el("text", { class: "axis-label", x: m.l - 8, y: y(tv) + 4, "text-anchor": "end", text: opts.yFmt ? opts.yFmt(tv) : tv }));
    }
    if (opts.markerT != null && opts.markerT > t0 && opts.markerT < t1) {
      const xm = x(opts.markerT);
      svg.append(el("line", { class: "now-line", x1: xm, x2: xm, y1: m.t, y2: H - m.b }));
      svg.append(el("text", { class: "now-label", x: xm + 5, y: m.t + 12, text: opts.markerLabel || "Now" }));
    }
    for (const tk of timeTicks(t0, t1, Math.max(3, Math.floor((W - m.l) / 62)))) {
      svg.append(el("text", { class: "axis-label", x: x(tk.t), y: H - 6, "text-anchor": "middle", text: tk.label }));
    }
    const gap = opts.gapSeconds || Infinity;
    for (const s of visible) {
      let d = "", pen = false;
      for (let i = 0; i < t.length; i++) {
        const v = s.values[i];
        if (v == null || (i > 0 && t[i] - t[i - 1] > gap)) { pen = false; if (v == null) continue; }
        if (pen && opts.stepped) d += "H" + x(t[i]).toFixed(1);
        d += (pen ? "L" : "M") + x(t[i]).toFixed(1) + "," + y(v).toFixed(1);
        pen = true;
        if (opts.stepped && i === t.length - 1 && opts.stepEnd) d += "H" + x(opts.stepEnd).toFixed(1);
      }
      if (s.area) {
        svg.append(el("path", { class: "area", fill: s.color, d: d + `L${x(t1)},${y(Math.max(lo, 0))}L${x(t0)},${y(Math.max(lo, 0))}Z` }));
      }
      svg.append(el("path", { class: "series", stroke: s.color, d, "stroke-dasharray": s.dash ? (typeof s.dash === "string" ? s.dash : "5 4") : null }));
    }

    const hover = el("g", { style: "display:none" });
    const cross = el("line", { class: "crosshair", y1: m.t, y2: H - m.b });
    hover.append(cross);
    const dots = visible.map((s) => { const c = el("circle", { class: "hover-dot", r: 4.5, fill: s.color }); hover.append(c); return c; });
    svg.append(hover);
    const tip = el("div", { class: "tip", role: "status" });
    const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent", style: "cursor:crosshair" });
    svg.append(hit);
    wrap.append(svg, tip);

    function show(i) {
      const xi = x(t[i]);
      hover.style.display = "";
      cross.setAttribute("x1", xi); cross.setAttribute("x2", xi);
      visible.forEach((s, k) => {
        const v = s.values[i];
        dots[k].style.display = v == null ? "none" : "";
        if (v != null) { dots[k].setAttribute("cx", xi); dots[k].setAttribute("cy", y(v)); }
      });
      tip.innerHTML = "";
      tip.append(el("div", { class: "t", text: opts.tipTitle ? opts.tipTitle(t[i]) : new Date(t[i] * 1000).toLocaleString() }));
      for (const s of visible) {
        tip.append(el("div", { class: "r" },
          el("span", {}, el("i", { style: `background:${s.color}` }), s.label),
          el("b", { text: (opts.tipFmt || opts.yFmt || String)(s.values[i], s) })));
      }
      tipPosition(tip, wrap, xi, 0);
    }
    function nearest(px) {
      const tv = t0 + ((px - m.l) / (W - m.l - m.r)) * (t1 - t0);
      let lo2 = 0, hi2 = t.length - 1;
      while (hi2 - lo2 > 1) { const mid = (lo2 + hi2) >> 1; (t[mid] < tv ? (lo2 = mid) : (hi2 = mid)); }
      return Math.abs(t[lo2] - tv) < Math.abs(t[hi2] - tv) ? lo2 : hi2;
    }
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect();
      show(nearest(((e.clientX - r.left) / r.width) * W));
    });
    hit.addEventListener("pointerleave", () => { hover.style.display = "none"; tip.style.display = "none"; });
  }

  /** opts: { labels:[], values:[], color, height, yFmt, tip(i)->[[label,value,color]] , title(i) } */
  function barChart(wrap, opts) {
    wrap.innerHTML = "";
    const n = opts.values.length;
    if (!n) { wrap.append(el("div", { class: "empty", text: "No daily totals yet. The first full day appears tomorrow." })); return; }
    const W = Math.max(280, wrap.clientWidth), H = opts.height || 220;
    const m = { l: 46, r: 6, t: 10, b: 26 };
    const hi0 = Math.max(...opts.values.map((v) => v || 0), opts.allowNegative ? 0 : 1);
    const lo0 = Math.min(...opts.values.map((v) => v || 0), 0);
    const ticks = niceTicks(lo0, hi0 || 1, 4), hi = ticks[ticks.length - 1], lo = ticks[0];
    const y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (H - m.t - m.b);
    const slot = (W - m.l - m.r) / n, bw = Math.max(3, Math.min(28, slot - 4));
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.ariaLabel || "Bar chart" });
    for (const tv of ticks) {
      svg.append(el("line", { class: tv === 0 ? "zero" : "gridline", x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv) }));
      svg.append(el("text", { class: "axis-label", x: m.l - 8, y: y(tv) + 4, "text-anchor": "end", text: opts.yFmt ? opts.yFmt(tv) : tv }));
    }
    const every = Math.ceil(n / Math.max(1, Math.floor((W - m.l) / 64)));
    const bars = [];
    opts.values.forEach((v, i) => {
      const cx = m.l + slot * i + slot / 2;
      const h = Math.abs(y(0) - y(v || 0));
      const r = Math.min(4, bw / 2, h);
      const x0 = cx - bw / 2;
      let d = "";
      if (h > 0 && (v || 0) >= 0) { // rounded end away from the baseline
        const y0 = y(0) - h;
        d = `M${x0},${y(0)}V${y0 + r}Q${x0},${y0} ${x0 + r},${y0}H${x0 + bw - r}Q${x0 + bw},${y0} ${x0 + bw},${y0 + r}V${y(0)}Z`;
      } else if (h > 0) {
        const y1 = y(0) + h;
        d = `M${x0},${y(0)}V${y1 - r}Q${x0},${y1} ${x0 + r},${y1}H${x0 + bw - r}Q${x0 + bw},${y1} ${x0 + bw},${y1 - r}V${y(0)}Z`;
      }
      const fill = typeof opts.color === "function" ? opts.color(v, i) : opts.color;
      const b = el("path", { class: "bar", d, fill });
      bars.push(b); svg.append(b);
      if (i % every === 0 || i === n - 1 && n < 10) {
        svg.append(el("text", { class: "axis-label", x: cx, y: H - 6, "text-anchor": "middle", text: opts.labels[i] }));
      }
    });
    const tip = el("div", { class: "tip", role: "status" });
    const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
    svg.append(hit);
    wrap.append(svg, tip);
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const i = Math.min(n - 1, Math.max(0, Math.floor((px - m.l) / slot)));
      bars.forEach((b, k) => b.classList.toggle("dim", k !== i));
      tip.innerHTML = "";
      tip.append(el("div", { class: "t", text: opts.title ? opts.title(i) : opts.labels[i] }));
      for (const [label, value, color] of opts.tip(i)) {
        tip.append(el("div", { class: "r" }, el("span", {}, el("i", { style: `background:${color}` }), label), el("b", { text: value })));
      }
      tipPosition(tip, wrap, m.l + slot * i + slot / 2, 0);
    });
    hit.addEventListener("pointerleave", () => { bars.forEach((b) => b.classList.remove("dim")); tip.style.display = "none"; });
  }

  /**
   * Diverging stacked bars: positive series stack up from zero, negative ones down.
   * opts: { labels:[], series:[{key,label,color,values:[signed]}], height, yFmt, title(i), tip(i) }
   */
  function stackedBarChart(wrap, opts) {
    wrap.innerHTML = "";
    const n = opts.labels.length;
    if (!n) { wrap.append(el("div", { class: "empty", text: "No daily totals yet. The first full day appears tomorrow." })); return; }
    const W = Math.max(280, wrap.clientWidth), H = opts.height || 260;
    const m = { l: 46, r: 6, t: 10, b: 26 };
    let hi0 = 0, lo0 = 0;
    for (let i = 0; i < n; i++) {
      let up = 0, dn = 0;
      for (const s of opts.series) { const v = s.values[i] || 0; if (v > 0) up += v; else dn += v; }
      hi0 = Math.max(hi0, up); lo0 = Math.min(lo0, dn);
    }
    const ticks = niceTicks(lo0, hi0 || 1, 6), hi = ticks[ticks.length - 1], lo = ticks[0];
    const y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (H - m.t - m.b);
    const slot = (W - m.l - m.r) / n, bw = Math.max(3, Math.min(26, slot - 4));
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.ariaLabel || "Stacked bar chart" });
    for (const tv of ticks) {
      svg.append(el("line", { class: tv === 0 ? "zero" : "gridline", x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv) }));
      svg.append(el("text", { class: "axis-label", x: m.l - 8, y: y(tv) + 4, "text-anchor": "end", text: opts.yFmt ? opts.yFmt(tv) : tv }));
    }
    const every = Math.ceil(n / Math.max(1, Math.floor((W - m.l) / 64)));
    const cols = [];
    for (let i = 0; i < n; i++) {
      const g = el("g", {}), cx = m.l + slot * i + slot / 2, x0 = cx - bw / 2;
      let up = 0, dn = 0;
      const segs = opts.series.map((s) => ({ s, v: s.values[i] || 0 })).filter((o) => Math.abs(o.v) > 0.0001);
      const lastUp = [...segs].reverse().find((o) => o.v > 0), lastDn = [...segs].reverse().find((o) => o.v < 0);
      for (const o of segs) {
        const a = o.v > 0 ? up : dn, b = a + o.v;
        if (o.v > 0) up = b; else dn = b;
        const top = y(Math.max(a, b)), bot = y(Math.min(a, b)), h = bot - top;
        if (h < 0.5) continue;
        const outer = o === lastUp || o === lastDn, r = outer ? Math.min(3, bw / 2, h) : 0;
        let d;
        if (r && o.v > 0) d = `M${x0},${bot}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x0 + bw - r}Q${x0 + bw},${top} ${x0 + bw},${top + r}V${bot}Z`;
        else if (r) d = `M${x0},${top}V${bot - r}Q${x0},${bot} ${x0 + r},${bot}H${x0 + bw - r}Q${x0 + bw},${bot} ${x0 + bw},${bot - r}V${top}Z`;
        else d = `M${x0},${top}H${x0 + bw}V${bot}H${x0}Z`;
        // 1px surface gap between stacked segments keeps adjacent colours distinct
        g.append(el("path", { class: "bar-seg", d, fill: o.s.color, stroke: "var(--surface)", "stroke-width": 1 }));
      }
      svg.append(g); cols.push(g);
      if (i % every === 0 || (i === n - 1 && n < 10)) {
        svg.append(el("text", { class: "axis-label", x: cx, y: H - 6, "text-anchor": "middle", text: opts.labels[i] }));
      }
    }
    const tip = el("div", { class: "tip", role: "status" });
    const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
    svg.append(hit);
    wrap.append(svg, tip);
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const i = Math.min(n - 1, Math.max(0, Math.floor((px - m.l) / slot)));
      cols.forEach((g, k) => g.querySelectorAll(".bar-seg").forEach((p) => p.classList.toggle("dim", k !== i)));
      tip.innerHTML = "";
      tip.append(el("div", { class: "t", text: opts.title ? opts.title(i) : opts.labels[i] }));
      for (const [label, value, color] of opts.tip(i)) {
        tip.append(el("div", { class: "r" }, el("span", {}, color ? el("i", { style: `background:${color}` }) : null, label), el("b", { text: value })));
      }
      tipPosition(tip, wrap, m.l + slot * i + slot / 2, 0);
    });
    hit.addEventListener("pointerleave", () => { cols.forEach((g) => g.querySelectorAll(".bar-seg").forEach((p) => p.classList.remove("dim"))); tip.style.display = "none"; });
  }

  window.SigenCharts = { lineChart, barChart, stackedBarChart };
})();
