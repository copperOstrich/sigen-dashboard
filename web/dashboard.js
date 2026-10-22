(function () {
  const S = window.Sigen, C = window.SigenCharts, el = S.el;
  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  let live = null, lastOk = 0, hours = 24, dailyKey = "all", historyData = null, dailyData = [];
  const hidden = new Set();

  // ------------------------------------------------------------ power flow
  const WIRES = {
    pv: { from: [320, 102], to: [320, 178], color: "--solar" },        // solar -> hub
    load: { from: [350, 200], to: [516, 200], color: "--home" },       // hub -> home
    battery: { from: [320, 222], to: [320, 298], color: "--battery" }, // hub -> battery (charging)
    grid: { from: [124, 200], to: [290, 200], color: "--grid", dash: true }, // grid -> hub (buying)
    ev: { from: [292, 180], to: [119, 83], color: "--ev", optional: true },  // hub -> car
  };
  const wireEls = {};
  (function buildWires() {
    const g = $("wires");
    for (const [k, w] of Object.entries(WIRES)) {
      const d = `M${w.from[0]},${w.from[1]}L${w.to[0]},${w.to[1]}`;
      g.append(el("path", { class: "wire-bed", d, "stroke-width": 3, "stroke-dasharray": w.dash ? "6 6" : null }));
      const wire = el("path", { class: "wire idle", d, style: `stroke:var(${w.color})`, "stroke-dasharray": w.dash ? "6 6" : null });
      const pulse = el("path", { class: "pulse idle", d, style: `stroke:var(${w.color})` });
      const bed = g.lastChild;
      g.append(wire, pulse);
      wireEls[k] = { wire, pulse, bed };
      if (w.optional) [bed, wire, pulse].forEach((n) => n.setAttribute("hidden", ""));
    }
  })();

  function setWire(key, watts, reverse) {
    const { wire, pulse } = wireEls[key];
    const a = Math.abs(watts || 0);
    const idle = a < S.IDLE_W;
    const width = 2 + 7 * Math.sqrt(Math.min(a, 12000) / 12000);
    wire.classList.toggle("idle", idle);
    pulse.classList.toggle("idle", idle);
    wire.setAttribute("stroke-width", width.toFixed(1));
    pulse.setAttribute("stroke-width", (width + 3).toFixed(1));
    pulse.style.setProperty("--dur", Math.max(0.45, 3 - 2.6 * Math.min(a, 10000) / 10000).toFixed(2) + "s");
    pulse.classList.toggle("rev", !!reverse);
  }

  function setVal(id, w) {
    const a = Math.abs(w || 0);
    $("v-" + id).textContent = a < 1000 ? Math.round(a) : (a / 1000).toFixed(a >= 10000 ? 1 : 2);
    $("u-" + id).textContent = a < 1000 ? "W" : "kW";
  }

  function caption(p) {
    const pv = p.pv || 0, b = p.battery || 0, g = p.grid || 0, ev = p.ev || 0, I = S.IDLE_W;
    const parts = [];
    if (pv > I) parts.push(`Solar is making ${S.kw(pv)}`);
    else parts.push("No solar right now");
    if (b > I) parts.push(`the battery is charging at ${S.kw(b)}`);
    else if (b < -I) parts.push(`the battery is supplying ${S.kw(b)}`);
    if (g > I) parts.push(`${S.kw(g)} is coming from the grid`);
    else if (g < -I) parts.push(`${S.kw(g)} is being sold to the grid`);
    let s = parts[0];
    if (parts.length === 2) s += " and " + parts[1];
    if (parts.length === 3) s += ", " + parts[1] + " and " + parts[2];
    return s + (ev > I ? `. The car is charging at ${S.kw(ev)}.` : ".");
  }

  // ------------------------------------------------------------ live render
  function renderLive(d) {
    live = d;
    if (d.status === "ok") lastOk = Date.now();
    renderStatus();
    if (d.version) $("foot-collector").textContent = `Collector ${d.version}`;
    $("foot-mode").textContent = d.mode === "simulator" ? "simulator" : d.target ? `reading ${d.target}` : "";
    if (d.aemo) aemoSummary = d.aemo;
    if (d.price) renderPriceNow(d.price);
    if (!d.power) return;
    const p = d.power, desc = S.describe(p);
    setVal("pv", p.pv); setVal("load", p.load); setVal("batt", p.battery); setVal("grid", p.grid);
    $("l-grid").textContent = desc.grid === "Balanced" ? "Grid" : desc.grid === "Buying" ? "Grid, buying" : "Grid, selling";
    const narrow = window.innerWidth < 560;
    $("l-batt").textContent = desc.battery === "Idle" ? "Battery" : narrow ? desc.battery : `Battery, ${desc.battery.toLowerCase()}`;
    setWire("pv", p.pv, false);
    setWire("load", p.load, false);
    setWire("battery", p.battery, (p.battery || 0) < 0);
    setWire("grid", p.grid, (p.grid || 0) < 0);
    $("caption").textContent = caption(p);
    renderEvNode(d.ev, p);
    const bt = d.battery || {};
    $("batt-icon-fill").setAttribute("height", (22 * (bt.soc || 0) / 100).toFixed(1));
    $("batt-icon-fill").setAttribute("y", (-11 + 22 * (1 - (bt.soc || 0) / 100)).toFixed(1));
    renderBattery(bt, p);
    renderToday(d.today || {});
    renderSystem(d);
    renderEv(d.ev);
    if (evOn(d.ev) && !evData) loadEv();
  }

  function hhmm(hoursF) {
    const h = Math.floor(hoursF), m = Math.round((hoursF - h) * 60);
    return h ? `${h} h ${m} min` : `${m} min`;
  }

  function renderBattery(b, p) {
    $("soc").textContent = b.soc == null ? "–" : Math.round(b.soc);
    $("cell-fill").style.height = `calc(${(b.soc || 0)}% - 10px * ${(b.soc || 0) / 100})`;
    const cut = b.cutoff_low;
    if (cut != null) { $("cell-mark").style.bottom = `${cut}%`; $("cell-mark-l").textContent = `${Math.round(cut)}%`; }
    const w = p.battery || 0;
    let state = "Idle";
    if (w > S.IDLE_W) {
      state = `Charging at ${S.kw(w)}`;
      if (b.chargeable_kwh != null && w > 200) state += `, full in about ${hhmm(b.chargeable_kwh * 1000 / w)}`;
    } else if (w < -S.IDLE_W) {
      state = `Supplying ${S.kw(w)}`;
      if (b.available_kwh != null) state += `, lasts about ${hhmm(b.available_kwh * 1000 / -w)} at this rate`;
    }
    $("batt-state").textContent = state;
    $("batt-sub").textContent = b.rated_kwh ? `${b.rated_kwh} kWh${b.packs ? `, ${b.packs} modules` : ""}` : "";
    $("f-avail").textContent = b.available_kwh == null ? "–" : `${b.available_kwh.toFixed(1)} kWh usable`;
    $("f-room").textContent = S.kwh(b.chargeable_kwh);
    $("f-soh").textContent = S.pct(b.soh, 1);
    $("f-temp").textContent = b.cell_temp_avg == null ? "–" :
      `${b.cell_temp_avg.toFixed(1)} °C` + (b.temp_min != null && b.temp_max != null ? ` (${b.temp_min.toFixed(0)}–${b.temp_max.toFixed(0)})` : "");
    $("f-volt").textContent = b.cell_v_min == null ? (b.cell_v_avg ? `${b.cell_v_avg.toFixed(3)} V` : "–") :
      `${b.cell_v_min.toFixed(3)}–${b.cell_v_max.toFixed(3)} V`;
    $("f-cut").textContent = cut == null ? "–" : `${Math.round(cut)}% to ${Math.round(b.cutoff_high ?? 100)}%`;
    $("f-max").textContent = b.max_charge_w == null ? "–" : `${S.kw(b.max_charge_w, { forceKw: true })} / ${S.kw(b.max_discharge_w, { forceKw: true })}`;
  }

  function kwhCell(id, v) {
    const n = $(id);
    n.innerHTML = "";
    n.append(v == null ? "–" : Number(v).toFixed(1));
    if (v != null) n.append(el("small", { text: "kWh" }));
  }
  function renderToday(t) {
    kwhCell("t-pv", t.pv); kwhCell("t-load", t.load); kwhCell("t-imp", t.import); kwhCell("t-exp", t.export);
    kwhCell("t-chg", t.charge); kwhCell("t-dis", t.discharge);
    const hasEv = t.ev != null;
    $("t-ev-tile").hidden = !hasEv;
    $("t-ev-tile").parentElement.classList.toggle("has-ev", hasEv);
    if (hasEv) kwhCell("t-ev", t.ev);
    const n = $("t-self"); n.innerHTML = "";
    n.append(t.self_sufficiency == null ? "–" : Math.round(t.self_sufficiency));
    if (t.self_sufficiency != null) n.append(el("small", { text: "%" }));
    const c = $("t-cost"); c.innerHTML = "";
    if (t.cost) {
      c.append(`$${Math.abs(t.cost.net).toFixed(2)}`);
      if (t.cost.net < 0) c.append(el("small", { text: "credit" }));
      c.title = `Bought $${t.cost.bought.toFixed(2)}, earned $${t.cost.earned.toFixed(2)} (estimated from live prices)`;
    } else c.append("–");
    $("today-sub").textContent = t.yesterday_pv != null ? `yesterday's solar: ${t.yesterday_pv.toFixed(1)} kWh` : "";
  }

  const icon = (kind) => {
    const c = kind === "ok" ? "var(--good)" : "var(--bad)";
    return el("svg", { width: 18, height: 18, viewBox: "0 0 20 20", "aria-hidden": "true" },
      el("circle", { cx: 10, cy: 10, r: 8.5, fill: "none", stroke: c, "stroke-width": 1.8 }),
      kind === "ok" ? el("path", { d: "M6 10.3l2.7 2.7L14 7.6", fill: "none", stroke: c, "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" })
        : el("path", { d: "M10 5.5v5.5M10 14v.4", stroke: c, "stroke-width": 2, "stroke-linecap": "round" }));
  };

  function renderSystem(d) {
    const s = d.system || {}, al = $("alarms");
    al.innerHTML = "";
    if (!d.alarms || !d.alarms.length) {
      al.append(el("div", { class: "ok-line" }, icon("ok"), el("span", { text: "No active alarms" })));
    } else {
      for (const a of d.alarms) {
        al.append(el("div", { class: "alarm" }, icon("bad"),
          el("div", {}, el("b", { text: a.message }), el("div", { class: "muted", style: "font-size:13px", text: a.source }))));
      }
    }
    const rows = [
      ["Status", s.running_state], ["Operating mode", s.ems_mode], ["Grid connection", s.grid_status],
      ["Grid meter", s.grid_meter],
      ["Grid", s.grid_voltage != null ? `${s.grid_voltage.toFixed(0)} V, ${s.grid_frequency?.toFixed(2)} Hz` : null],
      ["Inverter temperature", s.pcs_temp != null ? `${s.pcs_temp.toFixed(1)} °C` : null],
      ["Insulation", s.insulation_mohm != null ? `${s.insulation_mohm.toFixed(2)} MΩ` : null, s.insulation_low
        ? `Below ${s.insulation_min_mohm} MΩ, which Sigenergy treats as abnormal. Often moisture in a connector or a damaged cable; worth having your installer check.`
        : s.insulation_min_mohm ? `Resistance between the solar wiring and earth. Sigenergy treats below ${s.insulation_min_mohm} MΩ as abnormal.` : null],
      ["Model", s.model], ["Firmware", s.firmware],
      ["EV charger", evFact(d.ev)],
    ];
    const f = $("sys-facts"); f.innerHTML = "";
    for (const [k, v, note] of rows) {
      if (v == null || v === "") continue;
      const bad = k === "Insulation" && s.insulation_low;
      f.append(el("dt", { text: k, title: note || null }),
        el("dd", { text: bad ? `${v}, low` : v, title: note || null, class: bad ? "bad" : null, style: "font-family:var(--font);font-size:14px" }));
    }
    if (s.inverter_error) f.append(el("dt", { text: "Inverter details" }), el("dd", { text: "unavailable – check SIGEN_INVERTER_UNIT", style: "font-size:13px" }));
    $("sys-sub").textContent = d.mode === "simulator" ? "simulated" : (s.serial || "");

    const st = $("strings"); st.innerHTML = "";
    if (s.strings && s.strings.length) {
      st.append(el("tr", {}, el("th", { text: "String" }), el("th", { text: "Voltage" }), el("th", { text: "Current" }), el("th", { text: "Power" })));
      for (const r of s.strings) st.append(el("tr", {}, el("td", { text: `PV${r.n}` }), el("td", { text: `${r.v.toFixed(0)} V` }),
        el("td", { text: `${r.a.toFixed(2)} A` }), el("td", { text: S.kw(r.w) })));
    }
    const ev = $("events"); ev.innerHTML = "";
    for (const e of d.events || []) {
      ev.append(el("li", {}, el("time", { text: new Date(e.ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) }),
        el("span", { text: e.message })));
    }
    if (!(d.events || []).length) ev.append(el("li", {}, el("span", { class: "muted", text: "No events recorded" })));
  }

  // ------------------------------------------------------------ car charging (Sigen AC charger)
  const evOn = (e) => !!(e && e.enabled && (e.unit != null || e.source === "plant") && e.state_code != null);
  const evInferred = () => !!(live && live.ev && live.ev.source === "plant");
  const EV_PLUGGED_WAIT = "color-mix(in srgb, var(--ev) 30%, var(--surface))";
  const dur = (sec) => {
    const m = Math.round((sec || 0) / 60);
    return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
  };
  const dayTime = (ts) => new Date(ts * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  let evData = null;

  function evFact(e) {
    if (!e || !e.enabled) return null;
    if (e.unit == null) {
      if (e.source === "plant") return "Seen through the inverter";
      if (e.status === "scanning") return e.scan ? `Looking for one (${e.scan.checked} of ${e.scan.of})` : "Looking for one";
      if (e.status === "not found") return "None found";
      return null;
    }
    return e.status === "offline" ? `Not answering (unit ${e.unit})` : `Sigen, Modbus unit ${e.unit}`;
  }

  function renderEvNode(e, p) {
    const on = evOn(e);
    $("ev-node").toggleAttribute("hidden", !on);
    const w = wireEls.ev;
    [w.bed, w.wire, w.pulse].forEach((n) => n.toggleAttribute("hidden", !on));
    if (!on) return;
    setVal("ev", p.ev);
    setWire("ev", p.ev, false);
    const narrow = window.innerWidth < 560;
    $("l-ev").textContent = e.charging ? (narrow ? "Charging" : "Car, charging") : e.plugged ? (narrow ? "Plugged in" : "Car, plugged in") : "Car";
  }

  function renderEv(e) {
    const panel = $("ev-panel");
    panel.hidden = !evOn(e);
    if (panel.hidden) return;
    const box = $("ev-now"); box.innerHTML = "";
    const fault = e.state === "E" || e.state === "F" || (e.alarms || []).length;
    const dotCls = fault ? "fault" : e.charging ? "charging" : e.plugged ? "plugged" : "";
    box.append(el("div", { class: "ev-state" }, el("span", { class: "dot " + dotCls }), el("span", { text: e.state_label })));
    if (e.since) {
      const sameDay = new Date(e.since * 1000).toDateString() === new Date().toDateString();
      box.append(el("div", { class: "ev-since", text: `since ${sameDay ? S.time(e.since) : dayTime(e.since)}` }));
    }
    const ses = e.session;
    if (e.charging) {
      box.append(el("div", { class: "ev-big num" }, S.kwNum(e.power), el("small", { text: "kW" })));
    } else if (ses) {
      box.append(el("div", { class: "ev-big num" }, (ses.kwh || 0).toFixed(1), el("small", { text: "kWh this session" })));
    }
    for (const a of e.alarms || []) {
      box.append(el("div", { class: "alarm" }, icon("bad"), el("div", {}, el("b", { text: a.message }), el("div", { class: "muted", style: "font-size:13px", text: a.source }))));
    }
    const facts = el("dl", { class: "facts" });
    const add = (k, v) => { if (v != null && v !== "") facts.append(el("dt", { text: k }), el("dd", { text: v })); };
    const inferred = e.source === "plant";
    $("ev-key-plugged").hidden = inferred;
    if (ses && inferred) {
      add("Started", `${S.time(ses.start)}, ${dur(ses.seconds)} ago`);
      add("Energy this session", S.kwh(ses.kwh));
      add("From the grid", ses.cost != null ? `${S.kwh(ses.grid_kwh)}, $${ses.cost.toFixed(2)}` : S.kwh(ses.grid_kwh));
      facts.append(el("div", { class: "sep" }));
    } else if (ses) {
      add("Plugged in", `${S.time(ses.start)}, ${dur(ses.seconds)} ago`);
      add("Charging time", ses.charging_s ? dur(ses.charging_s) : "not yet");
      add("Energy this session", S.kwh(ses.kwh));
      add("From the grid", ses.cost != null ? `${S.kwh(ses.grid_kwh)}, $${ses.cost.toFixed(2)}` : S.kwh(ses.grid_kwh));
      facts.append(el("div", { class: "sep" }));
    }
    add("Charger", e.rated_power ? `${S.kw(e.rated_power, { forceKw: true })}, ${Math.round(e.rated_current)} A` : null);
    add("Current offered to the car", e.set_current != null ? `${e.set_current.toFixed(0)} A` : null);
    add("Supply breaker", e.breaker_current ? `${Math.round(e.breaker_current)} A` : null);
    add("Delivered in total", e.total_kwh != null ? `${Math.round(e.total_kwh).toLocaleString()} kWh` : null);
    box.append(facts);
    $("ev-sub").textContent = inferred ? "from the inverter's figures"
      : e.status === "offline" ? "charger not answering" : `Sigen AC charger, Modbus unit ${e.unit}`;
    $("ev-note").hidden = !inferred;
  }

  function drawEvTimeline() {
    const box = $("ev-timeline");
    if (!evData || $("ev-panel").hidden) return;
    const W = Math.max(280, box.clientWidth || 600), H = 46, top = 4, bh = 22;
    const now = Date.now() / 1000, t0 = now - 48 * 3600;
    const x = (t) => ((Math.max(t0, Math.min(now, t)) - t0) / (now - t0)) * W;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
      "aria-label": "Charger state over the last 48 hours: when the car was plugged in and when it was charging" });
    svg.append(el("rect", { class: "track", x: 0, y: top, width: W, height: bh, rx: 4 }));
    const log = evData.log || [];
    for (let i = 0; i < log.length; i++) {
      const a = log[i], end = i + 1 < log.length ? log[i + 1].t : now;
      if (end <= t0) continue;
      const code = a.state_code;
      const fill = code === 5 ? "var(--ev)" : code === 6 || code === 7 ? "var(--bad)" : [2, 3, 4].includes(code) ? EV_PLUGGED_WAIT : null;
      if (!fill) continue;
      const r = el("rect", { x: x(a.t), y: top, width: Math.max(1.5, x(end) - x(a.t)), height: bh, fill,
        stroke: [2, 3, 4].includes(code) ? "var(--ev)" : null, "stroke-width": 0.75 });
      r.append(el("title", { text: `${clock(Math.max(a.t, t0))} to ${clock(end)}: ${a.label || a.state}` }));
      svg.append(r);
    }
    for (let h = Math.ceil(t0 / 3600) * 3600; h <= now; h += 3600) {
      const d = new Date(h * 1000);
      if (d.getHours() % (W < 520 ? 12 : 6)) continue;
      const lbl = d.getHours() === 0 ? d.toLocaleDateString([], { weekday: "short" }) : d.toLocaleTimeString([], { hour: "numeric" });
      svg.append(el("line", { x1: x(h), x2: x(h), y1: top + bh, y2: top + bh + 4, stroke: "var(--rule-strong)" }),
        el("text", { class: "tick", x: x(h), y: H - 2, "text-anchor": "middle", text: lbl }));
    }
    box.innerHTML = "";
    box.append(svg);
  }

  function renderEvSessions() {
    const tb = $("ev-sessions"); tb.innerHTML = "";
    const list = (evData && evData.sessions || []).slice(0, 8);
    const inf = evInferred();
    tb.append(el("tr", {}, el("th", { text: inf ? "Started" : "Plugged in" }), inf ? null : el("th", { text: "For" }), el("th", { text: "Charging" }),
      el("th", { text: "Energy" }), el("th", { class: "hide-sm", text: "From grid" }), el("th", { class: "hide-sm", text: "Grid cost" })));
    if (!list.length) {
      tb.append(el("tr", {}, el("td", { colspan: 6, class: "muted", style: "font-family:var(--font)", text: "No charging sessions recorded yet." })));
    }
    for (const r of list) {
      const open = r.end == null;
      const span = (open ? Date.now() / 1000 : r.end) - r.start;
      tb.append(el("tr", {},
        el("td", { style: "font-family:var(--font)", text: dayTime(r.start) }),
        inf ? null : el("td", { text: open ? `${dur(span)} so far` : dur(span) }),
        el("td", {}, r.charging_s ? dur(r.charging_s) : el("span", { class: "tag warn", text: open ? "not yet" : "never charged" })),
        el("td", { text: S.kwh(r.kwh) }),
        el("td", { class: "hide-sm", text: S.kwh(r.grid_kwh) }),
        el("td", { class: "hide-sm", text: r.cost == null ? "–" : `$${r.cost.toFixed(2)}` })));
    }
    const links = $("ev-links"); links.innerHTML = "";
    const base = S.apiBase();
    links.append(
      el("a", { href: `${base}/api/ev-sessions?format=csv&days=800`, download: "ev-charging-sessions.csv", target: "_blank", text: "Download all sessions (CSV)" }),
      el("a", { href: `${base}/api/ev-log?format=csv&days=800`, download: "ev-charger-states.csv", target: "_blank", text: "Download the charger's state log (CSV)" }));
  }

  async function loadEv() {
    if (!evOn(live && live.ev)) return;
    try {
      evData = await S.fetchJSON("/api/ev?days=30");
      const labels = { 0: "starting up", 1: "not plugged in", 2: "plugged in, charger waiting", 3: "plugged in, car not drawing power",
        4: "car ready, charger waiting", 5: "charging", 6: "charger unavailable", 7: "pilot signal error" };
      for (const a of evData.log || []) a.label = labels[a.state_code] || a.state;
      drawEvTimeline(); renderEvSessions();
    } catch (e) { /* banner */ }
  }

  // ------------------------------------------------------------ status / banner
  function renderStatus() {
    const dot = $("status-dot"), txt = $("status-text"), b = $("banner");
    const stale = !lastOk || Date.now() - lastOk > 30000;
    b.className = "banner"; b.innerHTML = "";
    if (live && live.status === "ok" && !stale) {
      dot.className = "dot ok";
      txt.textContent = (live.mode === "simulator" ? "Simulated data, " : "Live, ") + "updated " + S.ago(live.updated);
      if (live.mode === "simulator") {
        b.className = "banner sim show";
        b.append(el("span", { text: "These numbers come from the built-in simulator. Set SIGEN_HOST to your inverter's address once Modbus TCP is enabled to see real data." }));
      }
    } else if (live && live.status === "error") {
      dot.className = "dot bad";
      txt.textContent = "Inverter not responding";
      b.className = "banner show";
      if (live.error_kind === "internal") {
        txt.textContent = "Collector error";
        b.append(el("span", { text: `The collector reached the inverter at ${live.target} but couldn't process its data: ${live.error}. ` +
          "This is a bug in the dashboard, not your inverter; the details are in the collector's log." }));
        return;
      }
      b.append(el("span", { text: `The collector can't read the inverter at ${live.target}: ${live.error}. It keeps retrying` +
        (live.updated ? `; last good reading ${S.ago(live.updated)}.` : ". Check Modbus TCP is enabled and the IP address is right.") }));
    } else if (live === null || stale) {
      dot.className = "dot warn";
      txt.textContent = live ? `No data since ${S.ago(live.updated)}` : "Connecting…";
      if (live || connectFailed) {
        b.className = "banner show";
        b.append(el("span", { text: `Can't reach the collector${S.apiBase() ? " at " + S.apiBase() : ""}. Check it's running, or change its address.` }),
          el("button", { class: "ghost-btn", type: "button", text: "Change address", onclick: openSettings }));
      }
    }
  }
  let connectFailed = false;
  setInterval(renderStatus, 5000);

  // ------------------------------------------------------------ history
  const SERIES_ALL = [
    { key: "pv", label: "Solar", color: "var(--solar)" },
    { key: "load", label: "Home", color: "var(--home)" },
    { key: "battery", label: "Battery", color: "var(--battery)" },
    { key: "grid", label: "Grid", color: "var(--grid)", dash: true },
    { key: "ev", label: "Car", color: "var(--ev)", dash: "1.5 4", dot: true },
  ];
  let SERIES = SERIES_ALL.filter((s) => s.key !== "ev");
  function buildLegend() {
    const lg = $("legend");
    lg.innerHTML = "";
    for (const s of SERIES) {
      const b = el("button", { type: "button", "aria-pressed": hidden.has(s.key) ? "false" : "true" },
        el("span", { class: "sw" + (s.dot ? " dot" : s.dash ? " dash" : ""), style: `border-color:${s.color}` }), s.label);
      b.addEventListener("click", () => {
        hidden.has(s.key) ? hidden.delete(s.key) : hidden.add(s.key);
        b.setAttribute("aria-pressed", hidden.has(s.key) ? "false" : "true");
        drawHistory();
      });
      lg.append(b);
    }
  }
  buildLegend();

  let histToday = false;
  const midnightTs = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
  function trimTo(h, since) {  // drop a bucket that started before midnight
    const i = h.t.findIndex((t) => t >= since);
    const out = {};
    for (const k of Object.keys(h)) if (Array.isArray(h[k])) out[k] = i < 0 ? [] : h[k].slice(i);
    return out;
  }
  function tipTitle(t) {
    return new Date(t * 1000).toLocaleString([], hours > 30
      ? { weekday: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
      : { hour: "numeric", minute: "2-digit" });
  }
  function drawHistory() {
    const h = historyData;
    if (!h) return;
    const want = SERIES_ALL.filter((s) => s.key !== "ev" || h.ev);
    if (want.length !== SERIES.length) { SERIES = want; buildLegend(); }
    const signed = (k, v, s) => {
      if (v == null) return "–";
      if (s.key === "battery") return Math.abs(v) < S.IDLE_W ? "idle" : `${S.kw(v)} ${v > 0 ? "in" : "out"}`;
      if (s.key === "grid") return Math.abs(v) < S.IDLE_W ? "0 W" : `${S.kw(v)} ${v > 0 ? "bought" : "sold"}`;
      return S.kw(v);
    };
    C.lineChart($("power-chart"), {
      t: h.t, series: SERIES.map((s) => ({ ...s, values: h[s.key] })), hidden, height: 280,
      gapSeconds: h.bucket_seconds * 5, yFmt: S.kwAxis,
      tipFmt: (v, s) => signed(s.key, v, s), tipTitle, ariaLabel: `Power over time for solar, home, battery and grid${h.ev ? ", and car charging" : ""}`,
    });
    C.lineChart($("soc-chart"), {
      t: h.t, series: [{ key: "soc", label: "Charge", color: "var(--battery)", values: h.soc, area: true }],
      height: 130, yMin: 0, yMax: 100, yTicks: [0, 50, 100], gapSeconds: h.bucket_seconds * 5,
      yFmt: (v) => `${Math.round(v)}%`, tipTitle, ariaLabel: "Battery charge over time",
    });
  }
  async function loadHistory() {
    // "Today" runs from midnight; the API's buckets then cover just today
    const h = histToday ? Math.max(1, (Date.now() / 1000 - midnightTs()) / 3600) : hours;
    try {
      historyData = await S.fetchJSON(`/api/history?hours=${h.toFixed(3)}`);
      if (histToday) historyData = { ...historyData, ...trimTo(historyData, midnightTs()) };
      drawHistory();
    } catch (e) { /* status banner covers it */ }
  }
  $("range").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    histToday = b.dataset.h === "today";
    hours = histToday ? 24 : +b.dataset.h;
    $("range").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    loadHistory();
  });

  // ------------------------------------------------------------ daily
  const DAILY = {
    pv: ["Solar made", "var(--solar)"], load: ["Home used", "var(--home)"], import: ["Bought", "var(--grid)"],
    export: ["Sold", "var(--grid)"], discharge: ["Battery discharged", "var(--battery)"], charge: ["Battery charged", "var(--battery)"],
    ev: ["Car charged", "var(--ev)"], cost: ["Net cost", "var(--grid)"],
  };
  const money = (v) => (v == null ? "–" : v < 0 ? `$${(-v).toFixed(2)} credit` : `$${v.toFixed(2)}`);
  const ALL_IN = [["pv", "Solar"], ["import", "Bought"], ["discharge", "From battery"]];
  const OUT_BASE = [["load", "Home"], ["export", "Sold"], ["charge", "To battery"]];
  let ALL_OUT = OUT_BASE;
  const ALL_COLOR = { pv: "var(--solar)", import: "var(--grid)", discharge: "var(--battery)", load: "var(--home)", export: "var(--grid)", charge: "var(--battery)", ev: "var(--ev)" };
  function buildDailyLegend() {
    const lg = $("daily-legend");
    lg.innerHTML = "";
    const grp = (title, items) => el("span", { class: "grp" }, el("b", { text: title }),
      ...items.map(([k, l]) => el("span", {}, el("i", { style: `background:${ALL_COLOR[k]}` }), l)));
    lg.append(grp("In", ALL_IN), grp("Out", ALL_OUT));
  }
  buildDailyLegend();

  function drawDaily() {
    const d = dailyData;
    const label = (day) => new Date(day + "T12:00:00").toLocaleDateString([], { day: "numeric", month: "short" });
    const title = (i) => new Date(d[i].day + "T12:00:00").toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
    const hasEv = d.some((r) => r.ev != null);
    $("daily-ev-btn").hidden = !hasEv;
    const out = hasEv ? [OUT_BASE[0], ["ev", "Car"], ...OUT_BASE.slice(1)] : OUT_BASE;
    if (out.length !== ALL_OUT.length) { ALL_OUT = out; buildDailyLegend(); }
    $("daily-legend").hidden = dailyKey !== "all";
    if (dailyKey === "all") {
      const series = [...ALL_IN.map(([k, l]) => ({ key: k, label: l, color: ALL_COLOR[k], values: d.map((r) => r[k] || 0) })),
        ...ALL_OUT.map(([k, l]) => ({ key: k, label: l, color: ALL_COLOR[k], values: d.map((r) => -(r[k] || 0)) }))];
      C.stackedBarChart($("daily-chart"), {
        labels: d.map((r) => label(r.day)), series, height: 280, yFmt: (v) => `${v}`, title,
        ariaLabel: `Daily energy in (solar, bought, from battery) above zero and out (${ALL_OUT.map((x) => x[1].toLowerCase()).join(", ")}) below zero, kWh`,
        tip: (i) => {
          const r = d[i], sum = (ks) => ks.reduce((a, [k]) => a + (r[k] || 0), 0);
          const rows = [["Energy in", S.kwh(sum(ALL_IN)), null], ...ALL_IN.map(([k, l]) => [l, S.kwh(r[k]), ALL_COLOR[k]]),
            ["Energy out", S.kwh(sum(ALL_OUT)), null], ...ALL_OUT.map(([k, l]) => [l, S.kwh(r[k]), ALL_COLOR[k]])];
          if (r.cost != null) rows.push([r.cost_source === "calculated" ? "Net cost (est.)" : "Net cost (retailer)", money(r.cost), null]);
          return rows;
        },
      });
      return;
    }
    C.barChart($("daily-chart"), {
      labels: d.map((r) => label(r.day)), values: d.map((r) => r[dailyKey] ?? null), color: DAILY[dailyKey][1], height: 230,
      allowNegative: dailyKey === "cost",
      yFmt: (v) => (dailyKey === "cost" ? (v < 0 ? `-$${-v}` : `$${v}`) : `${v}`), ariaLabel: `${DAILY[dailyKey][0]} per day`,
      title,
      tip: (i) => {
        const rows = ["pv", "load", "ev", "import", "export", "charge", "discharge"].filter((k) => k !== "ev" || d[i].ev != null)
          .map((k) => [DAILY[k][0], S.kwh(d[i][k]), DAILY[k][1]]);
        if (d[i].cost != null) rows.push([d[i].cost_source === "calculated" ? "Net cost (est.)" : "Net cost (retailer)", money(d[i].cost), "var(--grid)"]);
        return rows;
      },
    });
  }
  async function loadDaily() {
    try { dailyData = await S.fetchJSON("/api/daily?days=30"); drawDaily(); } catch (e) { /* banner */ }
  }
  $("daily-metric").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    dailyKey = b.dataset.k;
    $("daily-unit").textContent = dailyKey === "cost" ? "dollars per day, below zero is a credit"
      : dailyKey === "all" ? "kWh per day, energy in above zero and out below" : "kWh per day";
    $("daily-metric").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    drawDaily();
  });

  // ------------------------------------------------------------ solar strings
  // Validated categorical colours; each string also gets its own line pattern so they stay
  // distinguishable for colour-blind readers and when there are more than four strings.
  const PV_COLORS = ["var(--solar)", "var(--home)", "var(--battery)", "var(--grid)"];
  const PV_DASHES = [null, "7 4", "2 3", "8 3 2 3"];
  const pvStyle = (i) => ({ color: PV_COLORS[i % 4], dash: PV_DASHES[(i + Math.floor(i / 4)) % 4] });
  const PV_MEASURE = { w: ["Power", (v) => S.kw(v)], v: ["Voltage", (v) => `${Math.round(v)} V`], a: ["Current", (v) => `${v.toFixed(1)} A`] };
  let pvData = null, pvHours = 24, pvMeasure = "w";
  let pvDaylight = S.store.get("sigen.pvDaylight") !== "0";
  const dayKey = (ts) => new Date(ts * 1000).toDateString();
  const midnight = (ts, daysBack = 0) => { const d = new Date(ts * 1000); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - daysBack); return d.getTime() / 1000; };

  // Sunrise-to-sunset windows, one per day, taken from the strings themselves: a bucket counts
  // as daylight once any string shows voltage or power. That follows the seasons, the roof's
  // own horizon and shading, with no location setting. Today's sunset is estimated from
  // yesterday's until it actually happens.
  function daylightWindows(d) {
    const ids = Object.keys(d.strings || {}), b = d.bucket_seconds || 60, pad = 20 * 60, now = Date.now() / 1000;
    const days = new Map();
    d.t.forEach((t, i) => {
      const lit = ids.some((n) => (d.strings[n].v[i] || 0) > 20 || (d.strings[n].w[i] || 0) > 5);
      if (!lit) return;
      const k = dayKey(t), w = days.get(k);
      if (!w) days.set(k, [t, t + b]); else w[1] = t + b;
    });
    const wins = [...days.values()].map(([a, z]) => [a - pad, z + pad]);
    const last = wins[wins.length - 1];
    if (last && dayKey(last[0]) === dayKey(now) && last[1] >= now - 3 * b - pad) {
      // still daylight: stretch to the expected sunset so the rest of the day shows as to come
      const prev = wins[wins.length - 2];
      const expected = prev && dayKey(prev[0]) !== dayKey(now) ? prev[1] + Math.round((midnight(now) - midnight(prev[0])) / 86400) * 86400 : 0;
      last[1] = Math.max(last[1], expected, now + b);
    }
    return wins;
  }
  const pvHidden = new Set();
  const swatch = (st) => el("svg", { class: "swl", width: 22, height: 8, "aria-hidden": "true" },
    el("line", { x1: 1, x2: 21, y1: 4, y2: 4, stroke: st.color, "stroke-width": 2.5, "stroke-linecap": "round", "stroke-dasharray": st.dash }));

  function drawStrings() {
    const d = pvData;
    const ids = d ? Object.keys(d.strings || {}).map(Number).sort((a, b) => a - b) : [];
    $("pv-panel").hidden = !ids.length;
    if (!ids.length) return;
    const lg = $("pv-legend");
    if (lg.dataset.ids !== ids.join(",")) {
      lg.dataset.ids = ids.join(","); lg.innerHTML = "";
      ids.forEach((n, i) => {
        const b = el("button", { type: "button", "aria-pressed": pvHidden.has(n) ? "false" : "true" }, swatch(pvStyle(i)), `PV${n}`);
        b.addEventListener("click", () => {
          pvHidden.has(n) ? pvHidden.delete(n) : pvHidden.add(n);
          b.setAttribute("aria-pressed", pvHidden.has(n) ? "false" : "true");
          drawStrings();
        });
        lg.append(b);
      });
    }
    const [label, fmt] = PV_MEASURE[pvMeasure];
    const series = ids.map((n, i) => ({ key: n, label: `PV${n}`, ...pvStyle(i), values: d.strings[n][pvMeasure] }));
    const hidden = new Set([...pvHidden]);
    const ndays = Math.round(pvHours / 24);
    let windows = null;
    if (pvDaylight) {
      windows = daylightWindows(d).slice(-ndays);
      if (ndays === 1 && windows.length && dayKey(windows[0][0]) !== dayKey(Date.now() / 1000)) windows = windows.slice(-1); // before sunrise: yesterday
    }
    $("pv-range-1").textContent = pvDaylight ? "Today" : "24 h";
    C.lineChart($("pv-chart"), {
      t: d.t, series, hidden, height: 250, gapSeconds: d.bucket_seconds * 5, yMin: 0,
      windows: windows && windows.length ? windows : null, markerT: pvDaylight ? Date.now() / 1000 : null, markerLabel: "Now",
      yFmt: pvMeasure === "w" ? S.kwAxis : pvMeasure === "v" ? (v) => `${v} V` : (v) => `${v} A`,
      tipFmt: (v) => (v == null ? "–" : fmt(v)),
      tipTitle: (t) => new Date(t * 1000).toLocaleString([], pvHours > 30 || dayKey(t) !== dayKey(Date.now() / 1000)
        ? { weekday: "short", day: "numeric", hour: "numeric", minute: "2-digit" } : { hour: "numeric", minute: "2-digit" }),
      ariaLabel: `${label} of each PV string over time`,
    });
    const today = d.today || [], total = today.reduce((a, r) => a + r.kwh, 0);
    const tb = $("pv-today"); tb.innerHTML = "";
    tb.append(el("tr", {}, el("th", { text: "Today" }), el("th", { text: "Energy" }), el("th", { text: "Share" }),
      el("th", { text: "Peak" }), el("th", { text: "Peak at" }), el("th", { text: "Max voltage" })));
    ids.forEach((n, i) => {
      const r = today.find((x) => x.string === n) || {};
      tb.append(el("tr", {}, el("td", {}, swatch(pvStyle(i)), `PV${n}`), el("td", { text: S.kwh(r.kwh) }),
        el("td", { text: total > 0 ? `${Math.round((r.kwh || 0) / total * 100)}%` : "–" }),
        el("td", { text: r.peak_w ? S.kw(r.peak_w) : "–" }),
        el("td", { text: r.peak_at && r.peak_w ? clock(r.peak_at) : "–" }),
        el("td", { text: r.max_v ? `${Math.round(r.max_v)} V` : "–" })));
    });
  }
  async function loadStrings() {
    // Daylight view: whole calendar days (plus yesterday, for today's expected sunset)
    const now = Date.now() / 1000, days = Math.round(pvHours / 24);
    const hrs = pvDaylight ? Math.ceil((now - midnight(now, days)) / 3600) : pvHours;
    try { pvData = await S.fetchJSON(`/api/string-history?hours=${hrs}`); drawStrings(); } catch (e) { /* banner */ }
  }
  (function initDaylight() {
    $("pv-hours").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", (x.dataset.d === "1") === pvDaylight ? "true" : "false"));
  })();
  $("pv-hours").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    pvDaylight = b.dataset.d === "1";
    S.store.set("sigen.pvDaylight", pvDaylight ? "1" : "0");
    $("pv-hours").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    loadStrings();
  });
  $("pv-range").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    pvHours = +b.dataset.h;
    $("pv-range").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    loadStrings();
  });
  $("pv-measure").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    pvMeasure = b.dataset.m;
    $("pv-measure").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    drawStrings();
  });

  // ------------------------------------------------------------ retailer prices + AEMO wholesale
  const DESC = {
    extremelyLow: ["Extremely cheap", "cheap"], negative: ["Extremely cheap", "cheap"], veryLow: ["Very cheap", "cheap"],
    low: ["Cheap", "cheap"], neutral: ["Average", ""], high: ["Expensive", "potential"], spike: ["Price spike", "spike"],
  };
  let priceData = null, priceSummary = null, aemoData = null, aemoSummary = null;
  const cents = (v, d = 1) => (v == null ? "–" : `${v.toFixed(d)}`);
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const regionName = (r) => (r || "").replace(/1$/, "");
  const ESTIMATE_NOTE = {
    Amber: "Amber locks the price in at the end of the interval",
    LocalVolts: "LocalVolts' expected price for this 5-minute interval",
  };

  function priceAdvice(p, fc) {
    const tips = [];
    const now = Date.now() / 1000;
    const ahead = (fc || []).filter((f) => f.end > now && f.buy != null);
    const nextSpike = ahead.find((f) => f.t > now && f.spike && f.spike !== "none");
    if (p.spike === "spike") tips.push("Prices are spiking. Hold off on big appliances; the battery is best used now.");
    else if (p.buy != null && p.buy < 12) tips.push("Power is cheap right now, a good time for the dishwasher, washing or charging the car.");
    if (p.sell != null && p.sell < 0) tips.push(`Exporting currently costs ${cents(-p.sell)}c/kWh, so soaking up solar at home pays off.`);
    if (nextSpike && p.spike !== "spike") tips.push(`A price spike is forecast from ${clock(nextSpike.t)}.`);
    if (ahead.length >= 4) {
      let best = null;
      for (let i = 0; i + 4 <= ahead.length; i++) {
        const avg = ahead.slice(i, i + 4).reduce((a, f) => a + f.buy, 0) / 4;
        if (!best || avg < best.avg) best = { avg, t: ahead[i].t, end: ahead[i + 3].end };
      }
      if (best) tips.push(`Cheapest 2 hours ahead: ${clock(best.t)} to ${clock(best.end)}, about ${Math.round(best.avg)}c/kWh.`);
    }
    return tips;
  }

  function wholesaleAdvice(a, fc) {
    if (!a || a.price_c == null) return [];
    const now = Date.now() / 1000;
    const ahead = (fc || []).filter((f) => f.end > now && f.price_c != null);
    const tips = [];
    if (a.price_c < 0) tips.push("Wholesale prices are negative right now: there's more power than the grid needs.");
    const peak = ahead.reduce((m, f) => (!m || f.price_c > m.price_c ? f : m), null);
    if (peak && peak.price_c > 30) tips.push(`AEMO forecasts wholesale prices peaking around ${clock(peak.t)} at about ${Math.round(peak.price_c)}c/kWh.`);
    return tips;
  }

  function setupBox() {
    return el("div", { class: "setup" },
      el("p", { style: "margin:0", text: "Connect your retailer to see your own buy and sell prices, spike alerts and what your power is costing." }),
      el("ul", {},
        el("li", {}, el("b", { text: "Amber: " }), "add ", el("code", { text: "AMBER_API_KEY" }), " (app.amber.com.au, Developers)."),
        el("li", {}, el("b", { text: "LocalVolts: " }), "add ", el("code", { text: "LOCALVOLTS_API_KEY" }), " and ",
          el("code", { text: "LOCALVOLTS_PARTNER_ID" }), " (localvolts.com, My Profile, API Key)."),
        el("li", { text: "Other retailers: the chart shows the wholesale price from AEMO." })),
      el("p", { style: "margin:0", text: "Then restart the collector." }));
  }

  function renderPriceNow(p) {
    priceSummary = p;
    const box = $("price-now"), pb = $("price-banner");
    pb.className = "banner spike";
    pb.innerHTML = "";
    const hasAemo = aemoSummary && aemoSummary.enabled;
    if (!p.enabled) {
      box.dataset.state = "setup";
      box.innerHTML = "";
      box.append(setupBox());
      $("price-sub").textContent = hasAemo ? `wholesale price for ${regionName(aemoSummary.region)}` : "not connected";
      $("price-chart").parentElement.hidden = !hasAemo;
      $("price-note").textContent = wholesaleAdvice(aemoSummary, aemoData && aemoData.forecast).join(" ");
      return;
    }
    box.dataset.state = "on";
    $("price-chart").parentElement.hidden = false;
    const name = p.provider || "Amber";
    if (p.status === "error" && p.buy == null) {
      box.innerHTML = ""; box.append(el("p", { class: "setup", text: `Waiting for ${name}. The collector will keep trying.` }));
      return;
    }
    if (p.buy == null) return;
    const [word, cls] = DESC[p.descriptor] || [p.descriptor || "", ""];
    const ren = p.renewables != null ? p.renewables : hasAemo ? aemoSummary.renewables_pct : null;
    box.innerHTML = "";
    box.append(
      el("div", { class: "pair" },
        el("div", {}, el("div", { class: "k" }, el("i", { style: "border-color:var(--grid)" }), "Buying"),
          el("div", { class: "big" }, cents(p.buy), el("small", { text: "c/kWh" }))),
        el("div", {}, el("div", { class: "k" }, el("i", { style: "border-color:var(--solar)" }), "Selling"),
          el("div", { class: "big" }, cents(p.sell), el("small", { text: "c/kWh" })))),
      el("div", { class: "verdict" }, word ? el("span", { class: "tag " + cls, text: word }) : null,
        p.spike === "potential" ? el("span", { class: "tag potential", text: "Spike possible" }) : null,
        p.estimate ? el("span", { class: "tag", text: "Estimate", title: ESTIMATE_NOTE[name] || "" }) : null),
      el("dl", { class: "facts" },
        hasAemo && aemoSummary.price_c != null ? el("dt", { text: "Wholesale (AEMO)" }) : null,
        hasAemo && aemoSummary.price_c != null ? el("dd", { text: `${cents(aemoSummary.price_c)}c/kWh` }) : null,
        el("dt", { text: p.renewables != null ? `Renewables (${name})` : "Renewables (AEMO)" }), el("dd", { text: ren == null ? "–" : `${Math.round(ren)}%` }),
        el("dt", { text: "This price until" }), el("dd", { text: p.ends ? clock(p.ends) : "–" }),
        p.period ? el("dt", { text: "Network period" }) : null, p.period ? el("dd", { text: p.period }) : null));
    $("price-sub").textContent = p.mode === "simulator" ? `simulated ${name} prices` : `from ${name}`;
    const tips = priceAdvice(p, priceData && priceData.forecast);
    $("price-note").textContent = tips.join(" ");
    if (p.spike === "spike") {
      pb.classList.add("show");
      pb.append(el("span", { text: `Price spike: buying costs ${cents(p.buy)}c/kWh until ${clock(p.ends)}. Selling earns ${cents(p.sell)}c/kWh.` }));
    }
  }

  function drawPrices() {
    const d = priceData && priceData.enabled ? priceData : null;
    const a = aemoData && aemoData.enabled ? aemoData : null;
    if (!d && !a) return;
    const rows = new Map();
    // Key everything to its half-hour so retailer and AEMO points share rows (sources differ by a second or so)
    const row = (t0) => { const t = Math.floor((t0 + 30) / 1800) * 1800; if (!rows.has(t)) rows.set(t, { t, buy: null, sell: null, ws: null }); return rows.get(t); };
    if (d) {
      for (const h of d.history || []) Object.assign(row(h.t), { buy: h.buy, sell: h.sell });
      for (const f of d.forecast || []) Object.assign(row(f.t), { buy: f.buy, sell: f.sell, f });
    }
    if (a) {
      const since = Date.now() / 1000 - 12 * 3600, half = {};
      for (const h of a.history || []) {
        if (h.t < since - 1800) continue;
        const b = Math.floor(h.t / 1800) * 1800;
        (half[b] = half[b] || []).push(h.price_c);
      }
      for (const [b, v] of Object.entries(half)) row(+b).ws = v.reduce((x, y) => x + y, 0) / v.length;
      for (const f of a.forecast || []) Object.assign(row(f.t), { ws: f.price_c, wf: true });
    }
    const list = [...rows.values()].sort((x, y) => x.t - y.t);
    if (!list.length) return;
    const ends = [...(d && d.forecast || []), ...(a && a.forecast || [])].map((f) => f.end);
    const series = [];
    if (d) {
      series.push({ key: "buy", label: "Buying", color: "var(--grid)", values: list.map((r) => r.buy) });
      series.push({ key: "sell", label: "Selling", color: "var(--solar)", values: list.map((r) => r.sell) });
    }
    if (a && list.some((r) => r.ws != null)) series.push({ key: "ws", label: `Wholesale (${regionName(a.region)})`, color: "var(--ink-2)", dash: true, values: list.map((r) => r.ws) });
    const lg = $("price-legend"); lg.innerHTML = "";
    for (const s of series) {
      lg.append(el("span", { class: "legend-item" }, el("span", { class: "sw" + (s.dash ? " dash" : ""), style: `border-color:${s.color}` }), s.label));
    }
    lg.append(el("span", { class: "sub", text: "past 12 hours and next 24" }));
    C.lineChart($("price-chart"), {
      t: list.map((r) => r.t), stepped: true, stepEnd: ends.length ? Math.max(...ends) : null, height: 220,
      markerT: Date.now() / 1000, series,
      yFmt: (v) => `${Math.round(v)}c`, tipFmt: (v) => (v == null ? "–" : `${v.toFixed(1)}c/kWh`),
      tipTitle: (t) => {
        const r = rows.get(t), when = new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
        if (!r || (!r.f && !r.wf)) return `${when}, actual`;
        const w = r.f ? (DESC[r.f.descriptor] || [""])[0] : "";
        return `${when}, forecast${w ? ", " + w.toLowerCase() : ""}`;
      },
      ariaLabel: "Electricity prices, past 12 hours and next 24 hours",
    });
    if (priceSummary) renderPriceNow(priceSummary);
  }

  async function loadPrices() {
    try {
      const [p, a] = await Promise.all([S.fetchJSON("/api/prices?hours=12"), S.fetchJSON("/api/aemo?hours=24").catch(() => null)]);
      priceData = p; aemoData = a;
      if (a && a.enabled) { renderGrid(a); }
      $("grid-panel").hidden = !(a && a.enabled);
      if (!p.enabled) renderPriceNow({ enabled: false });
      drawPrices();
    } catch (e) { /* banner */ }
  }

  // ------------------------------------------------------------ the grid (AEMO)
  function renderGrid(a) {
    const c = a.current || {};
    $("grid-sub").textContent = `AEMO, ${regionName(a.region)} region` + (c.at ? `, ${clock(c.at)} interval` : "") +
      (a.mode === "simulator" ? ", simulated" : "");
    const now = $("grid-now"); now.innerHTML = "";
    if (a.status === "error" && c.price_c == null) {
      now.append(el("p", { class: "setup", text: `AEMO data isn't available right now (${a.error || "no data yet"}). The collector keeps retrying every 5 minutes.` }));
    } else {
      now.append(
        el("div", { class: "k", text: "Wholesale price" }),
        el("div", { class: "big" }, cents(c.price_c), el("small", { text: "c/kWh" })),
        el("div", { class: "verdict" },
          el("span", { class: "tag", text: c.rrp == null ? "–" : `$${Math.round(c.rrp).toLocaleString()}/MWh` }),
          c.price_status && c.price_status !== "FIRM" ? el("span", { class: "tag potential", text: "Not final", title: "AEMO may still revise this price" }) : null,
          c.apc ? el("span", { class: "tag spike", text: "Price capped" }) : null,
          c.suspended ? el("span", { class: "tag spike", text: "Market suspended" }) : null),
        el("dl", { class: "facts" },
          el("dt", { text: "Demand" }), el("dd", { text: c.demand_mw == null ? "–" : `${Math.round(c.demand_mw).toLocaleString()} MW` }),
          el("dt", { text: "Renewables" }), el("dd", { text: a.renewables_pct == null ? "–" : `${Math.round(a.renewables_pct)}%` })));
    }
    const mix = $("grid-mix"); mix.innerHTML = "";
    if ((a.fuel || []).length) {
      const max = Math.max(...a.fuel.map((f) => f.mw));
      for (const f of a.fuel) {
        mix.append(el("div", { class: "mix-row" + (f.renewable ? " ren" : "") },
          el("span", { class: "mix-k", text: f.fuel }),
          el("span", { class: "mix-bar" }, el("i", { style: `width:${Math.max(1, f.mw / max * 100)}%` })),
          el("span", { class: "mix-v", text: `${f.mw.toLocaleString()} MW` }),
          el("span", { class: "mix-p", text: `${Math.round(f.pct)}%` })));
      }
    } else mix.append(el("p", { class: "setup", text: "Generation mix not available yet." }));
    const ic = $("grid-ics"); ic.innerHTML = "";
    // AEMO reports each interconnector's flow as positive in its "from" to "to" direction
    const IC = { "NSW1-QLD1": ["NSW1", "QLD1"], "N-Q-MNSP1": ["NSW1", "QLD1", "Directlink"], "VIC1-NSW1": ["VIC1", "NSW1"],
      "V-SA": ["VIC1", "SA1", "Heywood"], "V-S-MNSP1": ["VIC1", "SA1", "Murraylink"], "T-V-MNSP1": ["TAS1", "VIC1", "Basslink"] };
    for (const x of a.interconnectors || []) {
      const m = IC[x.name] || ((x.name || "").split("-").length === 2 ? (x.name || "").split("-") : null);
      let label = x.name;
      if (m && x.mw != null) {
        const [from, to] = x.mw >= 0 ? [m[0], m[1]] : [m[1], m[0]];
        label = `${regionName(from)} to ${regionName(to)}` + (m[2] ? ` (${m[2]})` : "");
      }
      ic.append(el("dt", { text: label, title: x.name }), el("dd", { text: x.mw == null ? "–" : `${Math.abs(Math.round(x.mw)).toLocaleString()} MW` }));
    }
    if (!(a.interconnectors || []).length) ic.append(el("dt", { class: "muted", text: "No interconnector data" }), el("dd"));
  }

  // ------------------------------------------------------------ settings
  const dlg = $("settings");
  function openSettings() { $("api-input").value = S.apiBase() || location.origin; dlg.showModal(); }
  $("settings-btn").addEventListener("click", openSettings);
  if (!S.isDesktop && location.protocol.startsWith("http")) $("settings-btn").hidden = true;
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "save") return;
    S.setApiBase($("api-input").value.trim());
    sub.restart(); evData = null; loadHistory(); loadDaily(); loadPrices(); loadStrings();
  });

  // ------------------------------------------------------------ desktop: widget switch + updates
  if (S.isDesktop) {
    const wb = $("widget-btn");
    wb.hidden = false;
    wb.addEventListener("click", () => window.__TAURI__.core.invoke("show_widget"));
    if (window.__TAURI__.app) window.__TAURI__.app.getVersion().then((v) => {
      $("app-version").textContent = `Home Energy app version ${v}`;
      $("foot-app").textContent = `Desktop app ${v}`; $("foot-app").hidden = false;
    });

    const ub = $("update-banner");
    const showUpdate = (info, manual) => {
      ub.innerHTML = ""; ub.className = "banner update";
      if (!info) return;
      if (info.error) {
        if (!manual) return;
        ub.classList.add("show");
        ub.append(el("span", { text: `Couldn't check for updates: ${info.error}. You have version ${info.current}.` }),
          el("span", { class: "spacer" }), el("button", { class: "ghost-btn", type: "button", text: "Close", onclick: () => showUpdate(null) }));
        return;
      }
      if (!info.newer) {
        if (!manual) return;
        ub.classList.add("show");
        ub.append(el("span", { text: `You're on the latest version (${info.current}).` }),
          el("span", { class: "spacer" }), el("button", { class: "ghost-btn", type: "button", text: "Close", onclick: () => showUpdate(null) }));
        return;
      }
      if (!manual && S.store.get("sigen.skipVersion") === info.latest) return;
      ub.classList.add("show");
      const msg = el("span", { text: `Home Energy ${info.latest} is available. You have ${info.current}.` });
      const install = el("button", { class: "primary-btn", type: "button", text: "Download and install" });
      install.addEventListener("click", async () => {
        if (!info.asset) { window.open(info.page, "_blank"); return; }
        install.disabled = true; install.textContent = "Downloading…";
        try {
          await S.installUpdate(info.asset);
          msg.textContent = /Windows/i.test(navigator.userAgent)
            ? "Starting the installer…"
            : "The installer has opened. Drag Home Energy into Applications, then reopen it.";
          install.remove();
        } catch (e) {
          msg.textContent = `The update didn't install: ${e}. You can download it from the releases page instead.`;
          install.disabled = false; install.textContent = "Open download page";
          info.asset = null;
        }
      });
      ub.append(msg, el("span", { class: "spacer" }), install,
        el("button", { class: "ghost-btn", type: "button", text: "Not now", onclick: () => { S.store.set("sigen.skipVersion", info.latest); showUpdate(null); } }));
    };
    const check = (manual) => S.checkForUpdate().then((i) => showUpdate(i, manual)).catch(() => {});
    setTimeout(() => check(false), 4000);
    setInterval(() => check(false), 6 * 3600 * 1000);
    if (window.__TAURI__.event) window.__TAURI__.event.listen("check-updates", () => check(true));
  }

  // ------------------------------------------------------------ start
  if (S.isDesktop && !S.store.get("sigen.api")) setTimeout(openSettings, 300);
  const sub = S.subscribe(renderLive, () => { connectFailed = true; renderStatus(); });
  loadHistory(); loadDaily(); loadPrices(); loadStrings();
  setInterval(loadStrings, 60000);
  setInterval(loadEv, 60000);
  setInterval(loadPrices, 5 * 60000);
  setInterval(loadHistory, 60000);
  setInterval(loadDaily, 10 * 60000);
  window.SigenRedraw = () => { drawHistory(); drawPrices(); drawStrings(); drawEvTimeline(); if (dailyData.length) drawDaily(); };
  let rt;
  new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { drawHistory(); drawPrices(); drawStrings(); drawEvTimeline(); if (dailyData.length) drawDaily(); }, 120); })
    .observe(document.querySelector(".wrap"));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { drawHistory(); drawDaily(); });
})();
