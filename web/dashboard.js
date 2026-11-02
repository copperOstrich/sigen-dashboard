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
    renderStorm(d.storm, bt);
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
      // the power itself is on Right now
      state = b.chargeable_kwh != null && w > 200 ? `Charging, full in about ${hhmm(b.chargeable_kwh * 1000 / w)} at this rate` : "Charging";
    } else if (w < -S.IDLE_W) {
      state = b.available_kwh != null ? `Discharging, lasts about ${hhmm(b.available_kwh * 1000 / -w)} at this rate` : "Discharging";
    }
    $("batt-state").textContent = state;
    $("batt-sub").textContent = b.rated_kwh ? `${b.rated_kwh} kWh${b.packs ? `, ${b.packs} modules` : ""}` : "";
    $("f-avail").textContent = b.available_kwh == null ? "–" : `${b.available_kwh.toFixed(1)} kWh usable`;
    $("f-room").textContent = S.kwh(b.chargeable_kwh);
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
    const c = kind === "ok" ? "var(--good)" : kind === "warn" ? "var(--warn)" : "var(--bad)";
    return el("svg", { width: 18, height: 18, viewBox: "0 0 20 20", "aria-hidden": "true" },
      el("circle", { cx: 10, cy: 10, r: 8.5, fill: "none", stroke: c, "stroke-width": 1.8 }),
      kind === "ok" ? el("path", { d: "M6 10.3l2.7 2.7L14 7.6", fill: "none", stroke: c, "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" })
        : el("path", { d: "M10 5.5v5.5M10 14v.4", stroke: c, "stroke-width": 2, "stroke-linecap": "round" }));
  };

  let canEdit = false;
  const agoDay = (ts) => {
    const d = new Date(ts * 1000), now = new Date(), tm = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return `today ${tm}`;
    const y = new Date(now); y.setDate(now.getDate() - 1);
    return d.toDateString() === y.toDateString() ? `yesterday ${tm}` : d.toLocaleDateString([], { day: "numeric", month: "short" });
  };
  async function backupNow(e) {
    const b = e.target; b.disabled = true; b.textContent = "backing up…";
    try {
      const res = await fetch(S.apiBase() + "/api/backups", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const out = await res.json().catch(() => ({}));
      b.textContent = res.ok ? "done" : "failed";
      if (!res.ok) b.title = out.error || "";
    } catch (err) { b.textContent = "failed"; }
  }

  function renderSystem(d) {
    const s = d.system || {}, al = $("alarms");
    al.innerHTML = "";
    if (!d.alarms || !d.alarms.length) {
      al.append(el("div", { class: "ok-line" }, icon("ok"), el("span", { text: "No active alarms" })));
    } else {
      for (const a of d.alarms) {
        al.append(el("div", { class: "alarm" + (a.severity === "warning" ? " warn" : "") }, icon(a.severity === "warning" ? "warn" : "bad"),
          el("div", {}, el("b", { text: a.message }), el("div", { class: "muted", style: "font-size:13px", text: a.source }))));
      }
    }
    const rows = [
      ["Status", s.running_state], ["Operating mode", s.ems_mode], ["Grid connection", s.grid_status],
      ["Grid meter", s.grid_meter],
      ["Grid", s.grid_voltage != null ? `${s.grid_voltage.toFixed(0)} V, ${s.grid_frequency?.toFixed(2)} Hz` : null],
      ["Inverter temperature", s.pcs_temp != null ? `${s.pcs_temp.toFixed(1)} °C` : null],
      ["Insulation", s.insulation_mohm != null ? `${s.insulation_mohm.toFixed(2)} MΩ` : null,
        `Resistance between the solar wiring and earth. Healthy systems usually read about 0.5 to 0.8 MΩ, ` +
        `and the inverter protects itself below about ${s.insulation_min_mohm ?? 0.37} MΩ. Low readings are often moisture ` +
        `in a connector or a damaged cable, especially on wet mornings; if it stays low, have your installer check.`],
      ["Model", s.model], ["Firmware", s.firmware],
      ["EV charger", evFact(d.ev)],
    ];
    const f = $("sys-facts"); f.innerHTML = "";
    for (const [k, v, note] of rows) {
      if (v == null || v === "") continue;
      const lvl = k === "Insulation" ? s.insulation_level || 0 : 0;
      f.append(el("dt", { text: k, title: note || null }),
        el("dd", { text: lvl === 2 ? `${v}, too low` : lvl === 1 ? `${v}, lower than usual` : v, title: note || null,
          class: lvl === 2 ? "bad" : lvl === 1 ? "warn" : null, style: "font-family:var(--font);font-size:14px" }));
    }
    if (s.inverter_error) f.append(el("dt", { text: "Inverter details" }), el("dd", { text: "unavailable – check SIGEN_INVERTER_UNIT", style: "font-size:13px" }));
    const bk = s.backup || {};
    if (bk.enabled) {
      const last = bk.last;
      const txt = bk.busy ? "backing up…" : bk.error ? "failed" : last ? `${agoDay(last.ts)}, ${(last.bytes / 1e6).toFixed(last.bytes < 1e7 ? 1 : 0)} MB` : "first one soon";
      const dd = el("dd", { class: bk.error ? "warn" : null, style: "font-family:var(--font);font-size:14px",
        title: bk.error ? `Last attempt failed: ${bk.error}` : `${bk.count} kept in the data folder's "${bk.folder}" folder (the last ${bk.keep_daily} days and ${bk.keep_weekly} weeks). Next: ${new Date(bk.next * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}.`,
        text: txt });
      if (canEdit && !bk.busy) dd.append(el("button", { class: "small-link", type: "button", text: "Back up now", onclick: backupNow }));
      f.append(el("dt", { text: "Last backup" }), dd);
    }
    const fd = $("feeds");
    if (fd) {
      fd.innerHTML = "";
      const ago = (ts) => { if (!ts) return ""; const m = Math.round((Date.now() / 1000 - ts) / 60);
        return m < 2 ? "just now" : m < 90 ? `${m} min ago` : m < 48 * 60 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`; };
      // backups and Home Assistant have their own lines above
      for (const x of (s.feeds || []).filter((f) => f.name !== "Backups" && f.name !== "Home Assistant")) {
        const st = x.status === "ok" ? "ok" : x.status === "off" ? "off" : ["error", "stale", "warn"].includes(x.status) ? "bad" : "wait";
        const label = st === "ok" ? (x.mode === "simulator" ? "simulated" : "ok") : st === "off" ? "off" : x.status === "stale" ? "overdue" : st === "bad" ? "problem" : x.status;
        fd.append(el("li", { class: "feed " + st, title: x.error || "" },
          el("span", { class: "dot " + (st === "ok" ? "ok" : st === "bad" ? "bad" : st === "off" ? "" : "warn") }),
          el("span", { class: "fn", text: x.name }), el("span", { class: "fw", text: x.what || "" }),
          el("span", { class: "fs", text: [label, st === "ok" || st === "bad" ? ago(x.updated) : ""].filter(Boolean).join(", ") })));
      }
    }
    const mq = s.mqtt || {};
    if (mq.enabled) f.append(el("dt", { text: "Home Assistant" }), el("dd", { class: mq.status === "error" ? "warn" : null,
      style: "font-family:var(--font);font-size:14px", title: mq.error || `Publishing to ${mq.topic}${mq.discovery ? ", with discovery" : ""}`,
      text: mq.status === "connected" ? "connected" : mq.status === "error" ? "can't connect" : "connecting…" }));
    $("sys-sub").textContent = d.mode === "simulator" ? "simulated" : (s.serial || "");

    const ev = $("events"); ev.innerHTML = "";
    for (const e of d.events || []) {
      ev.append(el("li", {}, el("time", { text: new Date(e.ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) }),
        el("span", { text: e.message })));
    }
    if (!(d.events || []).length) ev.append(el("li", {}, el("span", { class: "muted", text: "No events recorded" })));
  }




  // ------------------------------------------------------------ money
  const money$ = (v, d = 0) => (v == null ? "–" : `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`);
  const mName = (m) => new Date(m + "-15T12:00:00").toLocaleDateString([], { month: "short", year: "2-digit" });
  // ------------------------------------------------------------ what if
  async function loadWhatIf(params) {
    const panel = $("wi-panel");
    const qs = params ? `?add_kwh=${encodeURIComponent(params.kwh)}&add_kw=${encodeURIComponent(params.kw)}` : "";
    let d;
    try { d = await S.fetchJSON("/api/whatif" + qs); } catch (e) { return; }
    panel.hidden = !(d && d.enabled && d.plans && d.plans.length);
    if (panel.hidden) return;
    const a = d.assumptions;
    if (!params) { $("wi-kwh").value = a.add_kwh; $("wi-kw").value = a.add_kw; }
    const fmtDay = (x) => new Date(x + "T12:00:00").toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
    $("wi-sub").textContent = `a year's cost, from ${d.days} days of your readings (${fmtDay(d.from)} to ${fmtDay(d.to)})`;
    const money0 = (v) => (v < 0 ? `−$${Math.abs(v).toLocaleString()}` : `$${v.toLocaleString()}`);
    const t = $("wi-table"); t.innerHTML = "";
    t.append(el("tr", {}, el("th", { text: "Plan" }), ...d.scenarios.map((x) => el("th", { text: x.label }))));
    for (const r of d.plans) {
      t.append(el("tr", {}, el("td", { style: "font-family:var(--font)" }, r.name,
        el("div", { class: "muted", style: "font-size:12px", text: r.kind === "amber" ? "battery as it ran; changes simulated" : r.kind === "tou" ? "battery charged for the peak" : "battery soaks up solar" })),
        ...d.scenarios.map((x) => el("td", { class: d.best[x.key] === r.name ? "wi-best" : null, text: money0(r.costs[x.key]) }))));
    }
    // the lead: best plan now, and what each change is worth on the best plan for it
    const best = (k) => d.plans.find((r) => r.name === d.best[k]);
    const now = best("now"), parts = [];
    const cur = d.plans[0] && d.plans[0].kind === "amber" ? d.plans[0] : null;
    if (cur && now !== cur) parts.push(`${now.name} would have cost ${money0(cur.costs.now - now.costs.now)} a year less than ${cur.name.replace(/ \(.*/, "")}.`);
    else if (cur) parts.push(`${cur.name.replace(/ \(.*/, "")} is the cheapest of these for you, by ${money0(Math.min(...d.plans.filter((r) => r !== cur).map((r) => r.costs.now)) - cur.costs.now)} a year.`);
    const gain = (k) => now.costs.now - best(k).costs[k];
    parts.push(`On ${now.name.replace(/ \(.*/, "")}, the battery is worth about ${money0(now.costs.no_battery - now.costs.now)} a year.`);
    if (a.add_kwh > 0) parts.push(`Another ${a.add_kwh} kWh of battery would save about ${money0(gain("more_battery"))} a year more.`);
    if (a.add_kw > 0) parts.push(`Another ${a.kwp ? a.add_kw + " kW of panels" : "quarter more panels"} would save about ${money0(gain("more_panels"))} a year.`);
    $("wi-lead").textContent = parts.join(" ");
    const c = d.check;
    const how = `Your recorded hours are played back through a battery of ${a.usable_kwh} kWh usable (${a.charge_kw} kW in, ${a.discharge_kw} kW out, ${a.round_trip}% round trip),` +
      " run the way each plan rewards: on time of use it also charges from the grid in the cheap hours for the dear ones; on wholesale prices it charges when power is about free and sells when feed-in pays $1/kWh or more." +
      " It knows the next day's use and solar, so it does a little better than a real battery would." +
      (a.export_limit_kw ? ` Exports are capped at ${a.export_limit_kw} kW.` : "") + (a.inverter_kw ? ` More panels are capped at the inverter's ${a.inverter_kw} kW.` : "") +
      (c ? ` Check: simulated this way, your system as it is would have cost ${money0(c.simulated)} a year on wholesale prices; as it actually ran, ${money0(c.actual)}` +
        " (the gap is partly your battery's real control, partly the model knowing the day ahead)." : "") +
      " Costs include each plan's supply charge. The other plans are your comparison plans (Settings > Money); add a real quote to plans.json in the data folder to compare it too.";
    const note = $("wi-note"); note.innerHTML = "";
    note.append(d.full_year ? "A year's cost on each plan. " : `Scaled to a year from ${d.days} days, so seasons not recorded yet aren't in it. `,
      el("details", { class: "how" }, el("summary", { text: "How this is worked out" }), el("p", { text: how })));
  }
  $("wi-form").addEventListener("submit", (e) => {
    e.preventDefault();
    loadWhatIf({ kwh: $("wi-kwh").value || 0, kw: $("wi-kw").value || 0 });
  });

  async function loadMoney() {
    let d;
    try { d = await S.fetchJSON("/api/money"); } catch (e) { return; }
    const panel = $("money-panel");
    panel.hidden = !d || !d.enabled;
    if (panel.hidden) return;
    const me = d.retailer || "Your retailer";
    const cmp = d.compare, pb = d.payback, tr = d.trades || {}, pl = d.plans;
    $("money-sub").textContent = "";

    // ---- payback
    const c2 = $("money-payback"); c2.innerHTML = "";
    if (!pb) {
      c2.append(el("p", { class: "setup" }, d.reason && !cmp ? `Not yet: ${d.reason}.` : "",
        ...(d.reason && !cmp ? [] : ["Add what the system cost (and when it was switched on) under ",
          el("a", { href: "settings.html#money", text: "Settings > Money" }), " to track when it pays for itself."])));
    } else {
      const sd = pb.standard, wf = pb.with_fuel;
      // date on one line, years under it (so the cell never wraps mid-phrase)
      const when = (x) => (x.paid_back ? "already" : x.payback_date
        ? el("span", {}, new Date(x.payback_date + "T12:00:00").toLocaleDateString([], { month: "short", year: "numeric" }),
          el("small", { class: "yrs", text: `${x.years_left} yrs` })) : "–");
      const meter = (x, label) => el("div", {},
        el("div", { class: "meter-k" }, el("span", { text: label }), el("b", { text: `${Math.round(x.pct)}%` })),
        el("div", { class: "meter", role: "img", "aria-label": `${label}: ${Math.round(x.pct)}% of ${money$(pb.cost)} paid back` },
          x.estimated_before > 0 ? el("i", { class: "est", style: `width:${Math.min(100, x.estimated_before / pb.cost * 100)}%`, title: `Estimated before tracking: ${money$(x.estimated_before)}` }) : null,
          el("i", { style: `width:${Math.max(0, Math.min(100, x.pct) - Math.min(100, (x.estimated_before || 0) / pb.cost * 100))}%` })));
      const sf = pb.standard_with_fuel;
      const dmo = sd ? (sd.plan === "Default Market Offer" ? "DMO" : sd.plan) : null;
      // columns: retailer, retailer + fuel, standard plan, standard plan + fuel
      const cols = [[me, "", pb], wf ? [me, "+ fuel", wf] : null, sd ? [dmo, "", sd] : null, sf ? [dmo, "+ fuel", sf] : null].filter(Boolean);
      const pcts = cols.map(([, , x]) => x.pct);
      const lo = Math.round(Math.min(...pcts)), hi = Math.round(Math.max(...pcts));
      c2.append(el("div", { class: "big" }, lo === hi ? `${lo}%` : `${lo}–${hi}%`, el("small", { text: "paid back" })));
      const bars = el("div", { class: "pb-bars" });
      for (const [g, f, x] of cols) {
        const label = `${g}${f ? " " + f : ""}`;
        bars.append(el("span", { class: "pb-k", text: label }),
          el("span", { class: "meter", role: "img", "aria-label": `${label}: ${Math.round(x.pct)}% of ${money$(pb.cost)} paid back` },
            x.estimated_before > 0 ? el("i", { class: "est", style: `width:${Math.min(100, x.estimated_before / pb.cost * 100)}%` }) : null,
            el("i", { style: `width:${Math.max(0, Math.min(100, x.pct) - Math.min(100, (x.estimated_before || 0) / pb.cost * 100))}%` })),
          el("b", { class: "pb-v", text: `${Math.round(x.pct)}%` }));
      }
      c2.append(bars);
      // two header rows: the price basis, then with or without fuel
      const groups = [];
      for (const [g] of cols) { const last = groups[groups.length - 1]; if (last && last[0] === g) last[1]++; else groups.push([g, 1]); }
      const t = el("table", { class: "pb-table" + (cols.length > 3 ? " four" : "") },
        el("tr", {}, el("th", { text: "" }), ...groups.map(([g, n]) => el("th", { colspan: n, class: "grp", text: g }))),
        el("tr", {}, el("th", { text: "" }), ...cols.map(([, f]) => el("th", { class: "sub", text: f || "energy" }))),
        el("tr", {}, el("td", { text: "Saved so far" }), ...cols.map(([, , x]) => el("td", { text: money$(x.saved) }))),
        el("tr", {}, el("td", { text: "Saving a day" }), ...cols.map(([, , x]) => el("td", { text: money$(x.per_day, 2) }))),
        el("tr", {}, el("td", { text: "Paid back" }), ...cols.map(([, , x]) => el("td", {}, when(x)))));
      c2.append(el("div", { class: "pb-wrap" }, t));
      if (sd) {
        const diff = sd.per_day - pb.per_day;
        const yrs = pb.years_left != null && sd.years_left != null ? pb.years_left - sd.years_left : null;
        c2.append(el("p", { class: "lead", style: "margin-top:10px", text: Math.abs(diff) < 0.05
          ? `Both views agree: being with ${me} makes little difference to the saving.`
          : `${diff > 0 ? `Against a standard plan the system saves ${money$(diff, 2)} a day more` : `Against a standard plan the system saves ${money$(-diff, 2)} a day less`}` +
            ` (about ${money$(Math.abs(diff) * 365)} a year). That's the value of being with ${me}, which only makes sense with a battery` +
            (yrs != null && Math.abs(yrs) >= 0.1 ? `. Paid back about ${Math.abs(yrs).toFixed(1)} years ${yrs > 0 ? "sooner" : "later"} on that view.` : ".") }));
      }
      c2.append(el("p", { class: "chart-note", text: `"${me} prices": what your home and car would have cost without solar and battery ` +
        (pb.method === "plan" ? `at ${pb.baseline_plan}'s rates, ` : `at ${me}'s price at the time, `) +
        "minus what you paid for energy (supply charges cancel out). " +
        (sd ? `"${sd.plan}": the same use on the ${sd.plan}` + (sd.with_fixed ? ", with its supply charge, minus everything you paid " + me + " including supply and fee" : ", energy only") + ", since without solar and battery you'd likely be on a standard plan. " : "") +
        `Daily savings are the average of ${pb.average_days} day${pb.average_days === 1 ? "" : "s"}` +
        (pb.average_days < 330 ? " and will swing with the seasons until there's a year of history." : ".") +
        (pb.estimated_days > 0 ? " Striped parts are estimated at that average for the days before the collector started." : "") +
        (wf ? ` "+ fuel": the same, but the car's ${wf.car_kwh} kWh counted against the ${wf.fuel === "DIESEL" ? "diesel" : "petrol"} ${wf.car} would have burnt instead of grid power` +
          ` (${money$(wf.fuel_extra)} more against ${me}'s prices` + (sf ? `, ${money$(sf.fuel_extra)} against the ${sd.plan}` : "") + " so far)." : "") +
        (pb.imported_days ? ` ${pb.imported_days} days come from a mySigen export, priced from ${me}'s bills or daily prices` +
          (pb.previous_days ? ` (the ${pb.previous_days} days before you joined ${me} are on the ${pb.previous_plan} in both views: an estimate)` : "") + "." : "") }));
    }

  }

  // ------------------------------------------------------------ monthly summary
  let moData = [], moIdx = -1, tgData = null;

  // targets: this month's progress and projection, or whether a past month made it
  function renderTargets(m, cur) {
    const box = $("mo-targets"); box.innerHTML = "";
    const t = tgData;
    box.hidden = !(t && t.enabled);
    if (box.hidden) return;
    const pct = (v) => `${Math.round(v)}%`;
    const money = (v) => (v < 0 ? `${dollars(-v)} credit` : dollars(v));
    const row = (label, target, value, projected, met, fmt, higherBetter, live) => {
      if (target == null) return null;
      const scale = higherBetter ? 100 : Math.max(Math.abs(target), Math.abs(value ?? 0), Math.abs(projected ?? 0)) * 1.25 || 1;
      const pos = (v) => higherBetter ? Math.max(0, Math.min(100, v)) : Math.max(0, Math.min(100, 50 + (v / scale) * 50));
      const bar = el("div", { class: "tg-bar" + (higherBetter ? "" : " centre") },
        value != null ? el("span", { class: "tg-fill", style: higherBetter ? `width:${pos(value)}%` : `left:${Math.min(50, pos(value))}%;width:${Math.abs(pos(value) - 50)}%` }) : null,
        projected != null && live ? el("span", { class: "tg-proj", style: `left:${pos(projected)}%`, title: `Heading for ${fmt(projected)}` }) : null,
        el("span", { class: "tg-mark", style: `left:${pos(target)}%`, title: `Target ${fmt(target)}` }));
      const status = met == null ? "" : live ? (met ? "on track" : "behind") : (met ? "made it" : "missed");
      const text = live
        ? `${value == null ? "–" : fmt(value)} so far${projected != null ? `, heading for ${fmt(projected)}` : ""}. Target ${fmt(target)}.`
        : `${value == null ? "–" : fmt(value)} against a target of ${fmt(target)}.`;
      return el("div", { class: "tg-row" }, el("div", { class: "tg-k" }, label, status ? el("span", { class: "tg-s " + (met ? "ok" : "bad"), text: status }) : null),
        bar, el("div", { class: "tg-t", text: text }));
    };
    let rows = [];
    if (m.month === cur) {
      if (t.self) rows.push(row("Self-sufficiency", t.self.target, t.self.so_far, t.self.projected, t.self.on_track, pct, true, true));
      if (t.cost) rows.push(row("Net cost", t.cost.target, t.cost.so_far, t.cost.projected, t.cost.on_track, money, false, true));
    } else {
      const h = (t.months || []).find((x) => x.month === m.month);
      if (h && h.self) rows.push(row("Self-sufficiency", h.self.target, h.self.value, null, h.self.met, pct, true, false));
      if (h && h.cost) rows.push(row("Net cost", h.cost.target, h.cost.value, null, h.cost.met, money, false, false));
    }
    rows = rows.filter(Boolean);
    const made = (t.months || []).filter((x) => x.full);
    const tally = (k) => { const a = made.filter((x) => x[k]); return a.length ? `${a.filter((x) => x[k].met).length} of ${a.length}` : null; };
    const parts = [["self", "self-sufficiency"], ["cost", "cost"]].map(([k, n]) => tally(k) && `${n} target made in ${tally(k)} full months`).filter(Boolean);
    if (!rows.length && !parts.length) { box.hidden = true; return; }
    box.append(el("div", { class: "tg-head", text: "Targets" }), ...rows);
    if (parts.length) box.append(el("div", { class: "tg-note", text: parts.join("; ").replace(/^./, (c) => c.toUpperCase()) + "." }));
  }
  const monthName = (m, long = true) => new Date(m + "-15T12:00:00").toLocaleDateString([], { month: long ? "long" : "short", year: "numeric" });
  const dollars = (v) => (v == null ? "–" : `${v < 0 ? "−" : ""}$${Math.abs(v).toFixed(2)}`);

  function renderMonthly() {
    const panel = $("mo-panel");
    panel.hidden = !moData.length;
    if (panel.hidden) return;
    if (moIdx < 0 || moIdx >= moData.length) moIdx = moData.length - 1;
    const m = moData[moIdx];
    const cmp = moData.find((x) => x.month === `${+m.month.slice(0, 4) - 1}${m.month.slice(4)}`) || moData[moIdx - 1];
    $("mo-label").textContent = monthName(m.month);
    $("mo-prev").disabled = moIdx === 0; $("mo-next").disabled = moIdx === moData.length - 1;
    const now = new Date(), cur = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    $("mo-sub").textContent = m.month === cur ? `so far, ${m.days} days` : `${m.days} days of readings`;
    const tile = (label, color, value, unit, key, better) => {
      const v = el("div", { class: "v num" }, value, unit ? el("small", { text: unit }) : null);
      let delta = null;
      if (cmp && key && cmp[key] != null && m[key] != null && m.month !== cur) {
        const a = cmp[key], b = m[key];
        if (Math.abs(a) > 0.01) {
          const pct = Math.round((b - a) / Math.abs(a) * 100);
          const good = better === "up" ? pct > 0 : better === "down" ? pct < 0 : null;
          delta = el("div", { class: "d" + (good == null || pct === 0 ? "" : good ? " up" : " down"),
            text: `${pct > 0 ? "+" : ""}${pct}% on ${monthName(cmp.month, false)}` });
        }
      }
      return el("div", {}, el("div", { class: "k" }, color ? el("i", { style: `background:${color}` }) : null, label), v, delta);
    };
    const tiles = $("mo-tiles"); tiles.innerHTML = "";
    tiles.classList.toggle("has-ev", m.ev > 0.05);
    tiles.append(
      tile("Solar made", "var(--solar)", m.pv.toFixed(0), "kWh", "pv", "up"),
      tile("Home used", "var(--home)", m.load.toFixed(0), "kWh", "load", "down"),
      ...(m.ev > 0.05 ? [tile("Car", "var(--ev)", m.ev.toFixed(0), "kWh", "ev", null)] : []),
      tile("Bought", "var(--grid)", m.import.toFixed(0), "kWh", "import", "down"),
      tile("Sold", "var(--grid)", m.export.toFixed(0), "kWh", "export", "up"),
      tile("Self-sufficient", null, m.self_sufficiency == null ? "–" : Math.round(m.self_sufficiency), m.self_sufficiency == null ? "" : "%", "self_sufficiency", "up"),
      tile("Net cost", null, dollars(m.net != null && m.net < 0 ? -m.net : m.net), m.net != null && m.net < 0 ? "credit" : "", "net", "down"),
      tile("Solar and battery value", null, dollars(m.saved), "", "saved", "up"));
    renderTargets(m, cur);
    drawMonthly();
    const tb = $("mo-table"); tb.innerHTML = "";
    tb.append(el("tr", {}, ...["Month", "Solar", "Home", "Bought", "Sold", "Self-suff.", "Net cost"].map((h) => el("th", { text: h }))));
    [...moData].reverse().forEach((x) => {
      const i = moData.indexOf(x);
      const tr = el("tr", { class: i === moIdx ? "sel" : null, title: "Show this month" },
        el("td", { style: "font-family:var(--font)", text: monthName(x.month, false) }),
        el("td", { text: `${x.pv.toFixed(0)} kWh` }), el("td", { text: `${x.load.toFixed(0)} kWh` }),
        el("td", { text: `${x.import.toFixed(0)} kWh` }), el("td", { text: `${x.export.toFixed(0)} kWh` }),
        el("td", { text: x.self_sufficiency == null ? "–" : `${Math.round(x.self_sufficiency)}%` }), el("td", { text: dollars(x.net) }));
      tr.addEventListener("click", () => { moIdx = i; renderMonthly(); });
      tb.append(tr);
    });
    const base = S.apiBase();
    $("mo-links").innerHTML = "";
    $("mo-links").append(el("a", { href: `${base}/report.html?month=${m.month}${base ? `&api=${encodeURIComponent(base)}` : ""}`, target: "_blank", text: "Printable report" }),
      el("a", { href: `${base}/year.html${base ? `?api=${encodeURIComponent(base)}` : ""}`, target: "_blank", text: "Year in review" }),
      el("a", { href: `${base}/api/monthly?months=36&format=csv`, download: "home-energy-monthly.csv", target: "_blank", text: "CSV" }));
    const src = m.cost_source === "retailer" ? "Costs from your retailer's meter data" : m.cost_source ? "Costs worked out from live prices" : "Add retailer prices to see costs";
    $("mo-note").textContent = `${src}. "Solar and battery value" is what the energy you made and stored would have cost at your average buy price (${m.avg_buy_c != null ? `${m.avg_buy_c}c/kWh` : "–"}) plus what you earned selling, before supply charges.` +
      (m.car ? ` Car: ${m.car.sessions} charging session${m.car.sessions === 1 ? "" : "s"}, ${m.car.kwh} kWh${m.car.cost != null ? `, ${dollars(m.car.cost)} from the grid` : ""}.` : "") +
      (m.best_day ? ` Best solar day: ${new Date(m.best_day.day + "T12:00:00").toLocaleDateString([], { day: "numeric", month: "short" })}, ${m.best_day.pv.toFixed(0)} kWh.` : "");
  }
  function drawMonthly() {
    if (!moData.length || $("mo-panel").hidden) return;
    C.stackedBarChart($("mo-chart"), {
      labels: moData.map((x) => monthName(x.month, false).split(" ")[0]), height: 220, yFmt: (v) => `${v}`,
      series: [{ key: "pv", label: "Solar made", color: "var(--solar)", values: moData.map((x) => x.pv) },
        { key: "used", label: "Used", color: "var(--home)", values: moData.map((x) => -(x.load + x.ev)) }],
      title: (i) => monthName(moData[i].month),
      tip: (i) => { const x = moData[i]; return [["Solar made", S.kwh(x.pv, 0), "var(--solar)"], ["Home and car", S.kwh(x.load + x.ev, 0), "var(--home)"],
        ["Bought", S.kwh(x.import, 0), "var(--grid)"], ["Sold", S.kwh(x.export, 0), "var(--grid)"], ["Net cost", dollars(x.net), null]]; },
      ariaLabel: "Solar made each month above zero, and energy used below zero, kWh",
    });
  }
  async function loadMonthly() {
    try {
      [moData, tgData] = await Promise.all([S.fetchJSON("/api/monthly?months=13"), S.fetchJSON("/api/targets").catch(() => null)]);
      renderMonthly();
    } catch (e) { /* banner */ }
  }
  $("mo-prev").addEventListener("click", () => { if (moIdx > 0) { moIdx--; renderMonthly(); } });
  $("mo-next").addEventListener("click", () => { if (moIdx < moData.length - 1) { moIdx++; renderMonthly(); } });

  // ------------------------------------------------------------ trends over months
  const dayT = (d) => new Date(d + "T12:00:00").getTime() / 1000;
  const rolling = (vals, n = 7) => vals.map((_, i) => {
    const w = vals.slice(Math.max(0, i - n + 1), i + 1).filter((v) => v != null);
    return w.length >= Math.min(3, n) ? w.reduce((a, b) => a + b, 0) / w.length : null;
  });
  // each trend lives in its subject's panel: Battery > Health, Solar > Long term, System > Health
  const TRENDS = [
    { id: "tb", metric: "battery", days: 365 },
    { id: "ts", metric: "pvidx", days: 365 },
    { id: "ty", metric: "insulation", days: 365 },
  ];
  const trCache = {};
  function trendSeries(m, d, t) {
    let series = [], opts = {}, note = "", empty = null;
    if (m === "pvidx") {
      const pi = d.pv_index || {};
      if (!pi.enabled) return { empty: `Solar vs weather ${pi.reason || "isn't available yet"}.`,
        note: "Compares each day's solar with what the weather model says the panels could have made, so slow changes over the seasons show up." };
      const ids = Object.keys(pi.strings_series || {}).map(Number).sort((a, b) => a - b);
      series = [{ key: "sys", label: "Whole array vs the weather", color: "var(--ink)", values: pi.system_series },
        ...ids.map((n, k) => ({ key: n, label: `PV${n} vs the others`, ...pvStyle(k), values: pi.strings_series[n] }))];
      const vals = series.flatMap((x) => x.values).filter((v) => v != null);
      opts = { yMin: Math.min(85, Math.floor(Math.min(...vals, 100) / 5) * 5), yMax: Math.max(110, Math.ceil(Math.max(...vals, 100) / 5) * 5),
        yFmt: (v) => `${Math.round(v)}%`, tipFmt: (v) => (v == null ? "–" : `${v.toFixed(1)}%`) };
      const since = new Date(pi.since + "T00:00").toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
      note = `100% is each line's first month (from ${since}). The whole array line is the solar made in clear, unrestricted hours against what the weather model says the roof could make: ` +
        "a slow, steady fall is dirt or panel ageing (around half a percent a year is normal), while a wobble of a few percent with the seasons is the model. " +
        "Each string's line is its output against the other strings, so the weather cancels out; strings on different roof faces trade share with the seasons, " +
        "so after a year each is also compared with the same weeks last year, where one that has dropped means new shade, dirt on that face, or a failing panel. " +
        "Hours with a full battery, exports at the limit or a negative feed-in price are left out. Smoothed over 15 days." +
        (pi.ready ? (pi.flags && pi.flags.length ? ` Worth a look: ${pi.flags.join("; ")}.` : " Nothing needs a look.") : ` Still learning: ${pi.days} of 45 days so far.`) +
        (pi.span_days < 380 ? " String comparisons with last year start once there's a year of readings." : "");
    } else if (m === "insulation") {
      const L = d.insulation_limits || { warn: 0.5, min: 0.37 };
      series = [
        { key: "min", label: "Lowest each day", color: "var(--home)", values: d.stats.insulation.min },
        { key: "avg", label: "Daily average", color: "var(--home)", dash: "2 3", values: d.stats.insulation.avg },
        { key: "warn", label: `Lower than usual (${L.warn} MΩ)`, color: "var(--warn)", dash: "6 4", values: t.map(() => L.warn) },
        { key: "alarm", label: `Alarm (${L.min} MΩ)`, color: "var(--bad)", dash: "6 4", values: t.map(() => L.min) }];
      opts = { yMin: 0, yFmt: (v) => `${v} MΩ`, tipFmt: (v) => (v == null ? "–" : `${v.toFixed(3)} MΩ`) };
      note = "Insulation resistance between the solar wiring and earth. Dips on wet mornings are normal; a lowest-each-day line creeping down over weeks is worth an installer's look before it reaches the alarm level.";
    } else if (m === "battery") {
      const v = d.stats.soh.avg.filter((x) => x != null);
      const lo = v.length ? Math.floor(Math.min(...v) - 1) : 90;
      series = [{ key: "soh", label: "Health", color: "var(--battery)", values: d.stats.soh.avg }];
      opts = { yMin: Math.max(0, lo), yMax: 100, yFmt: (x) => `${x}%`, tipFmt: (x) => (x == null ? "–" : `${x.toFixed(1)}%`) };
      note = "State of health as the battery reports it: how much of its original capacity is left. Expect a slow decline of around a percent or two a year.";
    } else if (m === "batt_temp") {
      series = [{ key: "max", label: "Hottest cell each day", color: "var(--battery)", values: d.stats.battery_temp.max },
        { key: "avg", label: "Daily average", color: "var(--battery)", dash: "2 3", values: d.stats.battery_temp.avg }];
      opts = { yFmt: (x) => `${x}°`, tipFmt: (x) => (x == null ? "–" : `${x.toFixed(1)} °C`) };
      note = "Battery cell temperature each day. Heat ages a battery faster than use does: summer peaks are expected, but a steady rise against similar weather can mean poor airflow around it.";
    } else {
      series = [{ key: "max", label: "Hottest each day", color: "var(--grid)", values: d.stats.inverter_temp.max },
        { key: "avg", label: "Daily average", color: "var(--grid)", dash: "2 3", values: d.stats.inverter_temp.avg }];
      opts = { yFmt: (x) => `${x}°`, tipFmt: (x) => (x == null ? "–" : `${x.toFixed(1)} °C`) };
      note = "Inverter temperature each day. Summer peaks are expected; a steady rise against similar weather can mean poor airflow around it, which makes it hold back power on hot days.";
    }
    return { series, opts, note, empty };
  }
  function drawTrend(tr) {
    const d = trCache[tr.days], box = $(tr.id);
    if (!box) return;
    const has = !!(d && d.days && d.days.length >= 2);
    if (box.hidden === has) box.hidden = !has;
    if (!has || (box.closest(".tabpage.inactive") && !box.closest(".group.together"))) return;
    const t = d.days.map(dayT), lg = $(tr.id + "-legend"), chart = $(tr.id + "-chart");
    const dayTitle = (x) => new Date(x * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
    const r = trendSeries(tr.metric, d, t);
    lg.innerHTML = ""; chart.innerHTML = "";
    $(tr.id + "-note").textContent = r.note;
    if (r.empty) { chart.append(el("div", { class: "empty", text: r.empty })); return; }
    for (const s of r.series) lg.append(el("span", { class: "legend-item" }, el("span", { class: "sw" + (s.dash ? " dash" : ""), style: `border-color:${s.color}` }), s.label));
    const hasData = r.series.some((s) => (s.values || []).some((v) => v != null && s.key !== "warn" && s.key !== "alarm"));
    C.lineChart(chart, { t, series: r.series, height: 240, gapSeconds: 86400 * 3, tipTitle: dayTitle,
      emptyText: "Not enough readings yet.", ariaLabel: `Trend over ${tr.days} days`, ...r.opts });
    if (!hasData) { chart.innerHTML = ""; chart.append(el("div", { class: "empty", text: "This builds up from the first day of readings; check back in a few days." })); }
  }
  function drawTrends() { TRENDS.forEach(drawTrend); }
  async function loadTrends() {
    const need = [...new Set(TRENDS.map((x) => x.days))];
    await Promise.all(need.map(async (days) => {
      try { trCache[days] = await S.fetchJSON(`/api/trends?days=${days}`); } catch (e) { /* banner */ }
    }));
    drawTrends();
  }
  for (const tr of TRENDS) {
    const seg = (id, f) => { const n = $(id); if (n) n.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      n.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      f(b);
    }); };
    seg(tr.id + "-metric", (b) => { tr.metric = b.dataset.m; drawTrend(tr); });
    seg(tr.id + "-range", async (b) => {
      tr.days = +b.dataset.d;
      if (!trCache[tr.days]) { try { trCache[tr.days] = await S.fetchJSON(`/api/trends?days=${tr.days}`); } catch (e) { return; } }
      drawTrend(tr);
    });
  }

  // ------------------------------------------------------------ battery outlook
  let olData = null, olPast = null;
  const olWhen = (ts) => {
    if (!ts) return "–";
    const d = new Date(ts * 1000), today = new Date();
    const tm = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === today.toDateString()) return tm;
    const tmr = new Date(today); tmr.setDate(today.getDate() + 1);
    return d.toDateString() === tmr.toDateString() ? `${tm} tomorrow` : `${tm} ${d.toLocaleDateString([], { weekday: "short" })}`;
  };

  function renderOutlook() {
    const o = olData, panel = $("ol-panel");
    panel.hidden = !(o && o.enabled);
    if (panel.hidden) return;
    const box = $("ol-now"); box.innerHTML = "";
    box.append(el("p", { class: "ol-head", text: o.headline }));
    const f = el("dl", { class: "facts" });
    const add = (k, v) => { if (v != null && v !== "") f.append(el("dt", { text: k }), el("dd", { text: v })); };
    add("Lowest point", o.lowest ? `${Math.round(o.lowest.soc)}% at ${olWhen(o.lowest.t)}` : null);
    add(`Reaches ${o.reserve}% reserve`, o.empty_at ? olWhen(o.empty_at) : "not tonight");
    if (o.night_grid_kwh > 0.05) add("From the grid until sunrise", `${o.night_grid_kwh.toFixed(1)} kWh` + (o.night_cost != null ? `, $${o.night_cost.toFixed(2)}` : ""));
    add("Solar takes over", o.solar_covers_at ? olWhen(o.solar_covers_at) : null);
    const t = o.tomorrow;
    if (t) {
      f.append(el("div", { class: "sep" }));
      add(`${t.label}: home use`, `${t.home_kwh.toFixed(0)} kWh`);
      add(t.full_at ? "Full again" : "Highest charge", t.full_at ? olWhen(t.full_at) : t.max_soc != null ? `${Math.round(t.max_soc)}%` : null);
      if (t.spare_kwh >= 0.5) add("Spare solar after that", `${t.spare_kwh.toFixed(0)} kWh`);
    }
    const oc = o.cost || {};
    const costTxt = (c) => `${c.net < 0 ? "$" + (-c.net).toFixed(2) + " back" : "$" + c.net.toFixed(2)}`;
    if (oc.today || oc.tomorrow) f.append(el("div", { class: "sep" }));
    if (oc.today) add("Rest of today, expected", costTxt(oc.today));
    if (oc.tomorrow) add("Tomorrow, expected", costTxt(oc.tomorrow));
    if (o.temperature && o.temperature.tomorrow_factor && Math.abs(o.temperature.tomorrow_factor - 1) >= 0.05) {
      add("Tomorrow's use, for the weather", `${o.temperature.tomorrow_factor > 1 ? "+" : "−"}${Math.abs(Math.round((o.temperature.tomorrow_factor - 1) * 100))}%`);
    }
    box.append(f);
    const adv = $("ol-advice"); adv.innerHTML = "";
    for (const a of o.advice || []) adv.append(el("p", { text: a }));
    $("ol-sub").textContent = `${o.solar_source === "forecast" ? "solar from the weather forecast" : "solar from recent days"}, home use from the last ${o.history_days} days`;
    $("ol-note").textContent = "Assumes the battery runs the house and stores spare solar, at your usual use for each hour (car charging left out)." +
      (o.managed ? ` Your system is in ${o.ems_mode} mode, so your retailer or VPP may export or charge the battery differently from this.` : "");
    drawOutlook();
  }

  function drawOutlook() {
    const o = olData;
    if (!o || !o.enabled || $("ol-panel").hidden) return;
    const proj = o.projection || [], nowT = Date.now() / 1000;
    const rows = new Map();
    const row = (t) => { if (!rows.has(t)) rows.set(t, { t, act: null, fc: null, pv: null, load: null, pvAct: null }); return rows.get(t); };
    if (olPast) olPast.t.forEach((t, i) => { if (t < nowT) { const r = row(t); if (olPast.soc[i] != null) r.act = olPast.soc[i]; if (olPast.pv && olPast.pv[i] != null) r.pvAct = olPast.pv[i]; } });
    for (const p of proj) Object.assign(row(p.t), { fc: p.soc, pv: p.pv, load: p.load });
    // join the actual line to the projection at "now"
    if (proj.length && olPast) { const r0 = row(proj[0].t); if (r0.act == null && live && live.battery) r0.act = live.battery.soc; }
    const lst = [...rows.values()].sort((a, b) => a.t - b.t);
    const lg = $("ol-legend"); lg.innerHTML = "";
    const items = [["Charge so far", "var(--battery)", false], ["Expected charge", "var(--battery)", true], ["Solar so far", "var(--solar)", false], ["Expected solar", "var(--solar)", true], ["Usual home use", "var(--home)", true]];
    for (const [l, c, d] of items) lg.append(el("span", { class: "legend-item" }, el("span", { class: "sw" + (d ? " dash" : ""), style: `border-color:${c}` }), l));
    const title = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
    C.lineChart($("ol-chart"), {
      t: lst.map((r) => r.t), height: 190, yMin: 0, yMax: 100, yTicks: [0, 25, 50, 75, 100], markerT: nowT,
      series: [{ key: "act", label: "Charge", color: "var(--battery)", values: lst.map((r) => r.act), area: false },
        { key: "fc", label: "Expected", color: "var(--battery)", dash: "5 4", values: lst.map((r) => r.fc) }],
      yFmt: (v) => `${Math.round(v)}%`, tipFmt: (v) => (v == null ? "–" : `${Math.round(v)}%`), tipTitle: title,
      ariaLabel: "Battery charge over the last 12 hours and expected until the end of tomorrow",
    });
    // same time axis as the charge chart above, so the two line up
    C.lineChart($("ol-flow"), {
      t: lst.map((r) => r.t), height: 120, yMin: 0, markerT: nowT, markerLabel: "",
      series: [{ key: "pva", label: "Solar so far", color: "var(--solar)", values: lst.map((r) => r.pvAct) },
        { key: "pv", label: "Expected solar", color: "var(--solar)", dash: "5 4", values: lst.map((r) => r.pv) },
        { key: "load", label: "Usual home use", color: "var(--home)", dash: "2 3", values: lst.map((r) => r.load) }],
      yFmt: S.kwAxis, tipFmt: (v) => (v == null ? "–" : S.kw(v)), tipTitle: title,
      ariaLabel: "Solar so far, expected solar and usual home use for the same period",
    });
  }

  async function loadOutlook() {
    try {
      const [o, h] = await Promise.all([S.fetchJSON("/api/outlook"), S.fetchJSON("/api/history?hours=12")]);
      olData = o; olPast = h;
      renderOutlook();
    } catch (e) { /* banner */ }
  }

  // ------------------------------------------------------------ weather and solar forecast
  let wxData = null;
  const WXI = {
    sun: '<circle class="sun" cx="12" cy="12" r="4.5" fill="none" stroke="currentColor"/><path class="sun" stroke="currentColor" d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6"/>',
    part: '<circle class="sun" cx="9" cy="9" r="3.6" fill="none" stroke="currentColor"/><path class="sun" stroke="currentColor" d="M9 2v1.6M2 9h1.6M4 4l1.1 1.1M14 4l-1.1 1.1"/><path fill="var(--surface)" stroke="currentColor" d="M8 19.5h9.5a3.5 3.5 0 0 0 .2-7 5 5 0 0 0-9.6 1.3A2.9 2.9 0 0 0 8 19.5z"/>',
    cloud: '<path fill="none" stroke="currentColor" d="M7 18.5h10.5a4 4 0 0 0 .3-8 5.6 5.6 0 0 0-10.8 1.4A3.3 3.3 0 0 0 7 18.5z"/>',
    rain: '<path fill="none" stroke="currentColor" d="M7 14.5h10.5a4 4 0 0 0 .3-8 5.6 5.6 0 0 0-10.8 1.4A3.3 3.3 0 0 0 7 14.5z"/><path stroke="var(--home)" d="M9 17l-1 3M13 17l-1 3M17 17l-1 3"/>',
    storm: '<path fill="none" stroke="currentColor" d="M7 14.5h10.5a4 4 0 0 0 .3-8 5.6 5.6 0 0 0-10.8 1.4A3.3 3.3 0 0 0 7 14.5z"/><path stroke="var(--solar)" fill="none" d="M12.5 15.5l-2 3.5h3l-2 3.5"/>',
    fog: '<path fill="none" stroke="currentColor" d="M4 9h16M6 13h12M4 17h16"/>',
    snow: '<path fill="none" stroke="currentColor" d="M7 14.5h10.5a4 4 0 0 0 .3-8 5.6 5.6 0 0 0-10.8 1.4A3.3 3.3 0 0 0 7 14.5z"/><path stroke="currentColor" d="M9 18.5h.01M13 18.5h.01M17 18.5h.01M11 21h.01M15 21h.01"/>',
  };
  const wxIcon = (kind, size = 22) => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", size); s.setAttribute("height", size);
    s.setAttribute("class", "wx-icon"); s.setAttribute("aria-hidden", "true");
    s.setAttribute("stroke-width", "1.6"); s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
    s.innerHTML = WXI[kind] || WXI.cloud;
    return s;
  };
  const wxDay = (t) => new Date(t * 1000).toLocaleDateString([], { weekday: "short" });
  const kwh1 = (v) => (v == null ? "–" : v.toFixed(v >= 100 ? 0 : 1));

  function renderWeather() {
    const w = wxData, panel = $("wx-panel");
    panel.hidden = !(w && w.enabled);
    if (panel.hidden) return;
    const now = $("wx-now"); now.innerHTML = "";
    if (w.status !== "ok" || !w.current) {
      now.append(el("p", { class: "setup", text: w.status === "error" ? `Weather isn't available right now (${w.error}). It retries every few minutes.` : "Getting the forecast…" }));
      return;
    }
    const c = w.current, today = w.daily[0];
    $("wx-sub").textContent = w.mode === "simulator" ? "simulated"
      : (w.location && w.location.name ? w.location.name.split(",").slice(0, 2).join(",") + ", " : "") + "from Open-Meteo";
    const icon = c.is_day ? c.icon : (c.icon === "sun" ? "cloud" : c.icon);
    const bits = [c.desc + (c.feels != null && Math.abs(c.feels - c.temp) >= 2 ? `, feels like ${Math.round(c.feels)}°` : ""),
      c.wind != null ? `wind ${Math.round(c.wind)} km/h` : null, c.humidity != null ? `humidity ${Math.round(c.humidity)}%` : null,
      today ? `sunrise ${clock(today.sunrise)}, sunset ${clock(today.sunset)}` : null].filter(Boolean);
    now.append(wxIcon(icon, 30), el("span", { class: "wx-temp" }, c.temp == null ? "–" : Math.round(c.temp), el("small", { text: "°C" })),
      el("span", { class: "wx-line", text: bits.join(" · ") }));
    const learned = w.factor && w.factor.value != null;
    const days = $("wx-days"); days.innerHTML = "";
    w.daily.forEach((d, i) => {
      days.append(el("div", { class: "wx-day" },
        el("div", { class: "d" }, el("span", { text: i === 0 ? "Today" : i === 1 ? "Tomorrow" : wxDay(d.t) }), wxIcon(d.icon, 22)),
        el("div", { class: "k" }, d.pv_kwh == null ? "–" : kwh1(d.pv_kwh), el("small", { text: "kWh" })),
        el("div", { class: "m", text: `${d.desc}, ${Math.round(d.tmin)}–${Math.round(d.tmax)}°` }),
        el("div", { class: "m", text: `${d.rain_prob != null ? `${d.rain_prob}% rain` : ""}${d.uv != null ? `, UV ${Math.round(d.uv)}` : ""}` })));
    });
    $("wx-note").textContent = !learned
      ? "The solar forecast needs a few sunny days of readings to learn your system (or set SOLAR_KWP for a first estimate)."
      : w.factor.learned
        ? `Forecast from Open-Meteo's sunshine forecast for your roof, scaled by how your system has actually performed over the last two weeks (${Math.round(w.factor.hours)} sunny hours compared).`
        : "Forecast from Open-Meteo's sunshine forecast for your roof and SOLAR_KWP. It sharpens once a few sunny days have been compared with what your panels made.";
    drawWeather();
  }

  function drawWeather() { /* the solar forecast is drawn in the battery outlook chart */ }
  async function loadWeather() {
    try {
      wxData = await S.fetchJSON("/api/weather");
      renderWeather();
    } catch (e) { /* banner */ }
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
  // compact forms for the sessions table, so rows stay on one line on a phone
  const dayShort = (ts) => { const d = new Date(ts * 1000);
    return `${d.toLocaleDateString([], { day: "numeric", month: "short" })}, ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`; };
  const durShort = (sec) => { const m = Math.round(sec / 60); return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`; };
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
      add("From the grid", ses.cost != null ? `${S.kwh(ses.grid_kwh)}, $${ses.cost.toFixed(2)}` : S.kwh(ses.grid_kwh));
      facts.append(el("div", { class: "sep" }));
    } else if (ses) {
      add("Plugged in", `${S.time(ses.start)}, ${dur(ses.seconds)} ago`);
      add("Charging time", ses.charging_s ? dur(ses.charging_s) : "not yet");
      add("From the grid", ses.cost != null ? `${S.kwh(ses.grid_kwh)}, $${ses.cost.toFixed(2)}` : S.kwh(ses.grid_kwh));
      facts.append(el("div", { class: "sep" }));
    }
    add("Charger", e.rated_power ? `${S.kw(e.rated_power, { forceKw: true })}, ${Math.round(e.rated_current)} A` : null);
    add("Current offered to the car", e.set_current != null ? `${e.set_current.toFixed(0)} A` : null);
    add("Supply breaker", e.breaker_current ? `${Math.round(e.breaker_current)} A` : null);
    add("Delivered in total", e.total_kwh != null ? `${Math.round(e.total_kwh).toLocaleString()} kWh` : null);
    box.append(facts);
    $("ev-sub").textContent = inferred ? "from the inverter's figures"
      : e.status === "offline" ? "charger not answering" : "Sigen AC charger";
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
    const fu = evData && evData.fuel && evData.fuel.enabled ? evData.fuel : null;
    renderEvFuel(fu);
    tb.append(el("tr", {}, el("th", { text: inf ? "Started" : "Plugged in" }), inf ? null : el("th", { class: "hide-sm", text: "For" }), el("th", { class: "hide-sm", text: "Charging" }),
      el("th", { text: "kWh" }), el("th", { class: "hide-sm", text: "Grid kWh" }), el("th", { class: "hide-sm", text: "Grid cost" }),
      fu ? el("th", { title: "Charging cost per kWh: grid energy at what you paid plus solar energy at the export price (free when negative)", text: "c/kWh" }) : null,
      fu ? el("th", { title: `${fu.fuel_label} price used for the session, cents per litre`, text: "Fuel c/L" }) : null,
      fu ? el("th", { class: "hide-sm", text: "Fuel equiv." }) : null, fu ? el("th", { text: "Saved" }) : null));
    if (!list.length) {
      tb.append(el("tr", {}, el("td", { colspan: 6, class: "muted", style: "font-family:var(--font)", text: "No charging sessions recorded yet." })));
    }
    for (const r of list) {
      const open = r.end == null;
      const span = (open ? Date.now() / 1000 : r.end) - r.start;
      tb.append(el("tr", {},
        el("td", { style: "font-family:var(--font)", title: dayTime(r.start), text: dayShort(r.start) }),
        inf ? null : el("td", { class: "hide-sm", text: open ? `${durShort(span)} so far` : durShort(span) }),
        el("td", { class: "hide-sm" }, r.charging_s ? durShort(r.charging_s) : el("span", { class: "tag warn", text: open ? "not yet" : "never charged" })),
        el("td", { text: (r.kwh || 0).toFixed(1) }),
        el("td", { class: "hide-sm", text: (r.grid_kwh || 0).toFixed(1) }),
        el("td", { class: "hide-sm", text: r.cost == null ? "–" : `$${r.cost.toFixed(2)}` }),
        fu ? el("td", { class: open ? "muted" : null, title: r.fuel && r.fuel.charge_cost != null ? (open ? "So far: " : "") +
            `$${r.fuel.charge_cost.toFixed(2)} for ${(r.kwh || 0).toFixed(1)} kWh` +
            (r.fuel.solar_kwh > 0.05 ? `, including ${r.fuel.solar_kwh.toFixed(1)} kWh not from the grid at ${r.fuel.solar_c}c/kWh ` +
              (r.fuel.solar_price_from === "interval" ? "(export price while charging)" : "(average export price over the plug-in time)") : "") : null,
          text: r.fuel && r.fuel.charge_c_per_kwh != null ? r.fuel.charge_c_per_kwh.toFixed(1) : "–" },
          open && r.fuel && r.fuel.charge_c_per_kwh != null ? el("span", { class: "hide-sm", text: " so far" }) : null) : null,
        fu ? el("td", { title: r.fuel && r.fuel.price_c != null ? (r.fuel.price_from === "recorded" ? "Average price recorded that day"
            : r.fuel.price_from === "fixed" ? "Your fixed price (no price recorded that day)" : "Nearest recorded price (none recorded that day)") : null,
          text: r.fuel && r.fuel.price_c != null ? `${r.fuel.price_c.toFixed(1)}${r.fuel.price_from === "recorded" ? "" : "*"}` : "–" }) : null,
        fu ? el("td", { class: "hide-sm", title: r.fuel && r.fuel.fuel_cost != null ? `${r.fuel.km} km: ${r.fuel.litres} L at ${r.fuel.price_c}c/L` +
          (r.fuel.price_from === "recorded" ? "" : r.fuel.price_from === "fixed" ? " (your fixed price)" : " (nearest recorded price)") +
          `; charging cost $${(r.fuel.charge_cost ?? 0).toFixed(2)} (grid $${(r.cost ?? 0).toFixed(2)} + solar $${(r.fuel.solar_cost ?? 0).toFixed(2)} at the export price)` : null,
          text: r.fuel && r.fuel.fuel_cost != null ? `$${r.fuel.fuel_cost.toFixed(2)}` : "–" }) : null,
        fu ? el("td", { text: r.fuel && r.fuel.saved != null ? `${r.fuel.saved < 0 ? "−" : ""}$${Math.abs(r.fuel.saved).toFixed(2)}` : "–" }) : null));
    }
    const links = $("ev-links"); links.innerHTML = "";
    if (fu && list.some((r) => r.fuel && r.fuel.price_c != null && r.fuel.price_from !== "recorded")) {
      links.append(el("span", { class: "muted", style: "flex-basis:100%;font-size:12.5px",
        text: "* No fuel price was recorded that day, so the nearest recorded price (or your fixed price) is used." }));
    }
    const base = S.apiBase();
    links.append(
      el("a", { href: `${base}/api/ev-sessions?format=csv&days=800`, download: "ev-charging-sessions.csv", target: "_blank", text: "Download all sessions (CSV)" }),
      el("a", { href: `${base}/api/ev-log?format=csv&days=800`, download: "ev-charger-states.csv", target: "_blank", text: "Download the charger's state log (CSV)" }));
  }

  function renderEvFuel(fu) {
    const box = $("ev-fuel");
    box.hidden = !fu; box.innerHTML = "";
    if (!fu) return;
    const t = fu.totals || {};
    const where = fu.source === "fixed" ? "your fixed price" : fu.source === "simulator" ? "simulated prices"
      : `the average of ${(fu.stations || []).length} stations within ${fu.radius_km} km (${fu.source})`;
    box.append(
      el("div", { class: "ev-fuel-head" },
        el("span", { class: "big" }, t.saved != null ? `$${Math.abs(t.saved).toFixed(2)}` : "–",
          el("small", { text: t.saved != null && t.saved < 0 ? "more than fuel" : "saved on fuel" })),
        el("span", { class: "muted", text: t.sessions ? `${t.sessions} session${t.sessions === 1 ? "" : "s"}, ${t.km.toLocaleString()} km, ${t.litres} L of ${fu.fuel_label}` +
          (t.this_month ? `; $${t.this_month.toFixed(2)} this month` : "") : "No charging sessions to compare yet" })),
      el("p", { class: "chart-note", text: `${fu.ev_model} at ${fu.ev_kwh_per_100km} kWh/100 km against ${fu.car} at ${fu.car_l_per_100km} L/100 km` +
        (fu.price_c ? `, ${fu.fuel_label} at ${fu.price_c}c/L today (${where})` : "") +
        `. Each session uses that day's price. Charging costs the grid energy at what you paid, plus solar energy at what it would have earned exported (free when the export price is negative).` +
        (fu.status === "error" ? ` Fuel prices: ${fu.error}.` : "") }));
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

  let pvHealth = null;
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
      el("th", { text: "Peak" }), el("th", { text: "Peak at" }), el("th", { text: "Max voltage" }),
      el("th", { title: "The last few days against each string's usual share of the solar at each hour, sunny hours only", text: "Vs usual" })));
    const hs = pvHealth && pvHealth.enabled ? Object.fromEntries(pvHealth.strings.map((x) => [x.n, x])) : {};
    ids.forEach((n, i) => {
      const r = today.find((x) => x.string === n) || {};
      tb.append(el("tr", {}, el("td", {}, swatch(pvStyle(i)), `PV${n}`), el("td", { text: S.kwh(r.kwh) }),
        el("td", { text: total > 0 ? `${Math.round((r.kwh || 0) / total * 100)}%` : "–" }),
        el("td", { text: r.peak_w ? S.kw(r.peak_w) : "–" }),
        el("td", { text: r.peak_at && r.peak_w ? clock(r.peak_at) : "–" }),
        el("td", { text: r.max_v ? `${Math.round(r.max_v)} V` : "–" }),
        healthCell(hs[n])));
    });
    if (pvHealth && !pvHealth.enabled && pvHealth.learning_days != null) {
      tb.append(el("tr", {}, el("td", { colspan: 7, class: "muted", style: "font-family:var(--font);font-size:12.5px",
        text: `String health check: ${pvHealth.reason}.` })));
    }
  }
  function healthCell(h) {
    if (!h || h.ratio == null) return el("td", { text: "–" });
    const pct = Math.round(h.ratio * 100);
    const label = h.status === "down" ? `${pct}%, almost nothing` : h.status === "low" ? `${pct}%, low` : `${pct}%`;
    return el("td", { class: h.status === "ok" ? null : "st-" + h.status, text: label,
      title: h.days.map((x) => `${new Date(x.day + "T12:00:00").toLocaleDateString([], { weekday: "short", day: "numeric" })}: ${x.kwh} of ${x.expected_kwh} kWh expected (${Math.round(x.ratio * 100)}%)`).join("\n") });
  }
  async function loadStrings() {
    // Daylight view: whole calendar days (plus yesterday, for today's expected sunset)
    const now = Date.now() / 1000, days = Math.round(pvHours / 24);
    const hrs = pvDaylight ? Math.ceil((now - midnight(now, days)) / 3600) : pvHours;
    try {
      [pvData, pvHealth] = await Promise.all([S.fetchJSON(`/api/string-history?hours=${hrs}`), S.fetchJSON("/api/strings-health").catch(() => null)]);
      drawStrings();
    } catch (e) { /* banner */ }
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
    if (a && !d && list.some((r) => r.ws != null)) series.push({ key: "ws", label: `Wholesale (${regionName(a.region)})`, color: "var(--ink-2)", dash: true, values: list.map((r) => r.ws) });
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
    renderNem(a, IC);
  }

  // all NEM regions: a rough map with prices, demand, renewables and the flows between them
  const REGION_FULL = { NSW1: "New South Wales", QLD1: "Queensland", VIC1: "Victoria", SA1: "South Australia", TAS1: "Tasmania" };
  function renderNem(a, IC) {
    const regions = a.regions || [], nem = a.nem || {}, box = $("grid-nem");
    box.hidden = regions.length < 2;
    const view = ["tiles", "map", "chain"].includes(a.view) ? a.view : "tiles";
    $("grid-panel").classList.toggle("nem-view-tiles", view === "tiles" && !box.hidden);
    if (box.hidden) return;
    box.dataset.view = view;
    const R = Object.fromEntries(regions.map((r) => [r.region, r])), home = a.region;
    const mw = (v) => (v == null ? "–" : `${Math.round(Math.abs(v)).toLocaleString()} MW`);
    const pct = (v) => (v == null ? "–" : `${Math.round(v)}%`);
    const full = (k) => REGION_FULL[k] || k;
    const flowText = (r) => (r.net_interchange_mw == null || Math.abs(r.net_interchange_mw) < 5 ? "about balanced"
      : `${r.net_interchange_mw > 0 ? "exporting" : "importing"} ${mw(r.net_interchange_mw)}`);
    const tipFor = (r) => `${full(r.region)}: ${cents(r.price_c)}c/kWh ($${r.rrp == null ? "–" : Math.round(r.rrp).toLocaleString()}/MWh)` +
      `${r.price_status && r.price_status !== "FIRM" ? ", not final" : ""}; demand ${mw(r.demand_mw)}; ${flowText(r)}` +
      `${r.renewables_pct != null ? `; ${pct(r.renewables_pct)} renewable` : ""}${r.top_fuel ? `; most from ${r.top_fuel.toLowerCase()}` : ""}`;
    // net flow between each pair of regions (QLD-NSW and VIC-SA each have two links); + = from the first to the second
    const pairs = {};
    for (const x of a.all_interconnectors || []) {
      const m = IC[x.name];
      if (!m || x.mw == null) continue;
      const k = m[0] + "|" + m[1], pr = pairs[k] || (pairs[k] = { mw: 0, parts: [] });
      pr.mw += x.mw;
      pr.parts.push(`${m[2] || x.name}: ${mw(x.mw)} from ${regionName(x.mw >= 0 ? m[0] : m[1])} to ${regionName(x.mw >= 0 ? m[1] : m[0])}`);
    }
    const pairOf = (x, y) => pairs[x + "|" + y] ? { mw: pairs[x + "|" + y].mw, parts: pairs[x + "|" + y].parts }
      : pairs[y + "|" + x] ? { mw: -pairs[y + "|" + x].mw, parts: pairs[y + "|" + x].parts } : null;
    // price against the whole market: cheaper, about average, dearer (negative and $300+ always count)
    const level = (r) => (r.rrp == null || nem.price_c == null ? "avg" : r.rrp < 0 ? "cheap" : r.rrp >= 300 ? "dear"
      : r.price_c < nem.price_c - Math.max(0.1 * Math.abs(nem.price_c), 0.3) ? "cheap"
      : r.price_c > nem.price_c + Math.max(0.1 * Math.abs(nem.price_c), 0.3) ? "dear" : "avg");
    const LEVEL = { cheap: ["var(--battery)", "cheaper than the market average"], avg: ["var(--solar)", "about average"], dear: ["var(--grid)", "dearer"] };

    // whole-market tiles (nodes and wires, chain)
    const marketTiles = () => {
      const hr = R[home] || {}, rank = [...regions].sort((x, y) => (x.price_c ?? 0) - (y.price_c ?? 0)).findIndex((r) => r.region === home) + 1;
      const ord = ["", "cheapest", "2nd cheapest", "3rd cheapest", "4th cheapest", "dearest"][rank] || "";
      const ni = hr.net_interchange_mw, from = Object.keys(R).filter((k) => k !== home && (pairOf(k, home) || {}).mw > 5).map(regionName);
      const tile = (k, v, u, d) => el("div", {}, el("div", { class: "k", text: k }), el("div", { class: "v" }, v, u ? el("small", { text: u }) : null),
        d ? el("div", { class: "d", text: d }) : null);
      return el("div", { class: "today nem-tiles" },
        tile("Average price", cents(nem.price_c), "c/kWh", hr.price_c != null ? `${regionName(home)} ${cents(hr.price_c)}c, ${ord}` : null),
        tile("Market demand", nem.demand_mw == null ? "–" : Math.round(nem.demand_mw).toLocaleString(), "MW", "all five regions"),
        tile("Renewable share", nem.renewables_pct == null ? "–" : Math.round(nem.renewables_pct), "%", hr.renewables_pct != null ? `${regionName(home)} ${pct(hr.renewables_pct)}` : null),
        tile(ni != null && ni > 5 ? `Out of ${regionName(home)}` : `Into ${regionName(home)}`, ni == null ? "–" : Math.round(Math.abs(ni)).toLocaleString(), "MW",
          ni != null && ni < -5 && from.length ? `from ${from.join(" and ")}` : ni != null && Math.abs(ni) <= 5 ? "about balanced" : null));
    };

    const out = $("grid-nem-map"); out.innerHTML = "";
    $("grid-nem-intro").textContent = view === "tiles" ? "All regions right now"
      : "All regions right now: wholesale price, demand, renewable share and the flows between them";

    if (view === "tiles") {
      const t = el("div", { class: "nem-rtiles" });
      for (const k of ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]) {
        const r = R[k]; if (!r) continue;
        const ni = r.net_interchange_mw, mine = k === home;
        t.append(el("div", { class: mine ? "home" : null, title: tipFor(r) },
          el("div", { class: "k" }, full(k), mine ? el("span", { class: "pill-y", text: "yours" }) : null),
          el("div", { class: "v" }, cents(r.price_c), el("small", { text: "c/kWh" })),
          el("div", { class: "d", text: `${mw(r.demand_mw)} demand` }),
          el("div", { class: "ren-bar" }, el("span", { style: `width:${Math.max(0, Math.min(100, r.renewables_pct || 0))}%` })),
          el("div", { class: "d", text: `${pct(r.renewables_pct)} renewable` }),
          el("div", { class: "flow", text: ni == null || Math.abs(ni) < 5 ? "about balanced" : `${ni > 0 ? "↑ exporting" : "↓ importing"} ${mw(ni)}` })));
      }
      const head = el("div", { class: "nem-ichead" }, el("span", { text: "Interconnectors: how full each one is" }),
        el("span", { text: `NEM: ${cents(nem.price_c)}c/kWh average, ${mw(nem.demand_mw)}, ${pct(nem.renewables_pct)} renewable` }));
      const g = el("div", { class: "nem-icgrid" });
      for (const x of a.all_interconnectors || []) {
        const m = IC[x.name]; if (!m || x.mw == null) continue;
        const [from, to] = x.mw >= 0 ? [m[0], m[1]] : [m[1], m[0]];
        const lim = x.mw >= 0 ? x.export_limit : (x.import_limit != null ? -x.import_limit : null);
        const frac = lim ? Math.min(1, Math.abs(x.mw) / lim) : null;
        g.append(el("div", { class: "lbl" }, el("b", { text: `${regionName(from)} → ${regionName(to)}` }), m[2] ? ` ${m[2]}` : ""),
          el("div", { class: "val" }, mw(x.mw), frac != null ? el("span", { class: "pct", text: `${Math.round(frac * 100)}%` }) : null),
          el("div", { class: "track", title: frac != null ? `${Math.round(frac * 100)}% of its ${Math.round(lim).toLocaleString()} MW limit in this direction` : "limit not reported" },
            el("span", { style: `width:${frac != null ? Math.max(1, frac * 100) : 0}%` })));
      }
      out.append(t, head, g);
    } else if (view === "map") {
      const pos = { QLD1: [700, 70], NSW1: [700, 230], VIC1: [470, 330], SA1: [240, 250], TAS1: [470, 470] };
      const svg = el("svg", { viewBox: "0 0 940 540", role: "img", "aria-label": "Prices, demand and flows across the NEM regions" });
      for (const [x, y] of [["QLD1", "NSW1"], ["VIC1", "NSW1"], ["VIC1", "SA1"], ["TAS1", "VIC1"]]) {
        const f = pairOf(x, y); if (!f || !R[x] || !R[y]) continue;
        const [p, q] = f.mw >= 0 ? [pos[x], pos[y]] : [pos[y], pos[x]];
        const d = `M${p[0]},${p[1]}L${q[0]},${q[1]}`, w = 3 + Math.min(6, Math.abs(f.mw) / 150);
        svg.append(el("path", { class: "bed", d, "stroke-width": 3 }));
        if (Math.abs(f.mw) >= 5) svg.append(el("path", { class: "wire", d, "stroke-width": w }));
        const mx = (p[0] + q[0]) / 2, my = (p[1] + q[1]) / 2, dx = q[0] - p[0], dy = q[1] - p[1], L = Math.hypot(dx, dy);
        const nx = -dy / L, ny = dx / L, sg = ny > 0 ? -1 : 1;
        svg.append(el("text", { class: "mw", x: mx + nx * 18 * sg, y: my + ny * 18 * sg + 5,
          "text-anchor": Math.abs(nx) < 0.3 ? "middle" : (nx * sg > 0 ? "start" : "end") }, mw(f.mw), el("title", { text: f.parts.join("\n") })));
      }
      for (const [k, [x, y]] of Object.entries(pos)) {
        const r = R[k]; if (!r) continue;
        const mine = k === home, left = k === "SA1", anchor = left ? "end" : "start", lx = left ? x - 62 : x + 62;
        svg.append(el("g", {}, el("title", { text: tipFor(r) }),
          el("circle", { class: "ring" + (mine ? " home" : ""), cx: x, cy: y, r: 44, style: `stroke:${LEVEL[level(r)][0]}` }),
          el("text", { class: "pr", x, y: y + 4, text: cents(r.price_c) }), el("text", { class: "pu", x, y: y + 20, text: "c/kWh" }),
          el("text", { class: "nm", x: lx, y: y - 6, "text-anchor": anchor, text: full(k) + (mine ? " · yours" : "") }),
          el("text", { class: "st", x: lx, y: y + 14, "text-anchor": anchor, text: `${mw(r.demand_mw)} · ${pct(r.renewables_pct)} renewable` })));
      }
      const scale = el("div", { class: "nem-scale" }, ...["cheap", "avg", "dear"].map((l) => el("span", {}, el("i", { style: `border-color:${LEVEL[l][0]}` }), LEVEL[l][1])),
        el("span", { text: "wires: thicker = more power, moving towards where it flows" }));
      out.append(marketTiles(), el("div", { class: "nem-flowmap" }, svg), scale);
    } else {
      const reg = (k) => {
        const r = R[k]; if (!r) return el("div");
        const mine = k === home;
        return el("div", { class: "reg" + (mine ? " home" : ""), title: tipFor(r) },
          el("div", { class: "k", text: full(k) + (mine ? " · yours" : "") }),
          el("div", { class: "v" }, cents(r.price_c), el("small", { text: "c/kWh" })),
          el("div", { class: "d", text: `${mw(r.demand_mw)} · ${pct(r.renewables_pct)} renewable` }));
      };
      const link = (x, y) => {
        const f = pairOf(x, y), right = !f || f.mw >= 0, s = el("svg", { width: 80, height: 14, viewBox: "0 0 80 14", "aria-hidden": "true" });
        s.append(el("path", { d: "M4 7H76", class: "ln" }), f && Math.abs(f.mw) >= 5 ? el("path", { d: right ? "M68 2L76 7L68 12" : "M12 2L4 7L12 12", class: "hd" }) : null);
        return el("div", { class: "link", title: f ? f.parts.join("\n") : null }, el("span", { text: f ? mw(f.mw) : "–" }), s);
      };
      const chain = el("div", { class: "nem-chain" }, reg("QLD1"), link("QLD1", "NSW1"), reg("NSW1"), link("NSW1", "VIC1"), reg("VIC1"), link("VIC1", "SA1"), reg("SA1"));
      const f = pairOf("TAS1", "VIC1");
      const tas = el("div", { class: "nem-chain-tas" }, el("div", { class: "col" },
        el("div", { class: "vlink", title: f ? f.parts.join("\n") : null, text: f ? `${f.mw >= 0 ? "↑" : "↓"} ${mw(f.mw)} Basslink` : "" }), reg("TAS1")));
      out.append(marketTiles(), chain, tas);
    }

    // ---- table (phones, for the map and the chain)
    const t = $("grid-nem-table"); t.innerHTML = "";
    t.append(el("tr", {}, ...["Region", "c/kWh", "MW", "Renew.", "Flow MW"].map((h) => el("th", { text: h }))));
    for (const r of regions) {
      const ni = r.net_interchange_mw;
      t.append(el("tr", { class: r.region === home ? "home" : "" },
        el("td", { text: regionName(r.region) }), el("td", { text: cents(r.price_c) }),
        el("td", { text: r.demand_mw == null ? "–" : Math.round(r.demand_mw).toLocaleString() }),
        el("td", { text: pct(r.renewables_pct) }),
        el("td", { text: ni == null ? "–" : Math.abs(ni) < 5 ? "–" : `${ni > 0 ? "out" : "in"} ${Math.abs(Math.round(ni)).toLocaleString()}` })));
    }
    t.append(el("tr", { class: "total" }, el("td", { text: "NEM" }), el("td", { text: cents(nem.price_c) }),
      el("td", { text: nem.demand_mw == null ? "–" : nem.demand_mw.toLocaleString() }), el("td", { text: pct(nem.renewables_pct) }), el("td", { text: "" })));
  }


  // ------------------------------------------------------------ settings
  const dlg = $("settings");
  function openSettings() { $("api-input").value = S.apiBase() || location.origin; dlg.showModal(); }
  $("settings-btn").addEventListener("click", openSettings);
  if (!S.isDesktop && location.protocol.startsWith("http")) $("settings-btn").hidden = true;
  // Settings can only be changed from the collector's own network: grey the gear elsewhere
  // (the page still opens, read-only, with private values hidden).
  S.fetchJSON("/api/settings").then((st) => {
    const g = $("config-btn");
    g.classList.toggle("locked", !st.editable);
    canEdit = !!st.editable;
    g.title = st.editable ? "Settings" : "Settings (read-only: you're not on your home network)";
  }).catch(() => {});
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "save") return;
    S.setApiBase($("api-input").value.trim());
    sub.restart(); evData = null; loadWeather(); loadOutlook(); loadMonthly(); loadTrends(); loadMoney(); loadHistory(); loadDaily(); loadPrices(); loadStrings();
    loadAppliances(); loadLimits(); loadWear(); loadBaseload(); loadTrading();
  });


  // ------------------------------------------------------------ best times to run appliances
  let apData = null;
  const apWhen = (ts) => {
    const d = new Date(ts * 1000), now = new Date(), tm = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return tm;
    const t = new Date(now); t.setDate(now.getDate() + 1);
    return d.toDateString() === t.toDateString() ? `${tm} tomorrow` : `${tm} ${d.toLocaleDateString([], { weekday: "short" })}`;
  };
  const apCost = (v) => (v == null ? "–" : Math.abs(v) < 0.005 ? "$0.00" : v < 0 ? `$${(-v).toFixed(2)} back` : `$${v.toFixed(2)}`);
  function renderAppliances() {
    const a = apData, panel = $("ap-panel");
    panel.hidden = !(a && a.enabled && a.appliances.length);
    if (panel.hidden) return;
    const tb = $("ap-table"); tb.innerHTML = "";
    tb.append(el("tr", {}, el("th", { text: "Appliance" }), el("th", { text: "Best start" }), el("th", { text: a.priced ? "Cost then" : "From the grid" }),
      el("th", { class: "hide-sm", text: a.priced ? "If started now" : "If started now" }), el("th", { class: "hide-sm", text: "Why" })));
    for (const x of a.appliances) {
      const val = (c) => (a.priced ? apCost(c.cost) : `${c.grid_kwh.toFixed(1)} kWh`);
      tb.append(el("tr", {},
        el("td", { title: `${x.kw} kW for ${x.hours} h, about ${x.kwh} kWh` }, x.name),
        el("td", {}, el("span", { class: "when", text: x.run_now ? "Now" : apWhen(x.best.start) }),
          x.run_now ? el("span", { class: "now-tag", text: "good time" }) : null),
        el("td", { text: val(x.best), title: `${x.best.solar_kwh} kWh spare solar, ${x.best.battery_kwh} kWh battery, ${x.best.grid_kwh} kWh grid` }),
        el("td", { class: "hide-sm", text: val(x.now) }),
        el("td", { class: "hide-sm why", text: x.best.why })));
    }
    $("ap-sub").textContent = `next ${Math.round((a.until - Date.now() / 1000) / 3600)} hours`;
    $("ap-note").textContent = (a.priced
      ? `Spare solar is costed at what exporting it would have earned, battery energy at what you'll pay for grid power later (about ${a.battery_c}c/kWh), and the rest at the forecast buy price. `
      : "No retailer prices, so this picks the time that needs the least energy from the grid. ") +
      "Each appliance is worked out on its own, so two run together share the same spare solar. Change the list in Settings (Appliances).";
  }
  async function loadAppliances() {
    try { apData = await S.fetchJSON("/api/appliances"); renderAppliances(); } catch (e) { /* banner */ }
  }

  // ------------------------------------------------------------ solar limits
  let limData = null;
  const LIM = [["export", "Export limit", "var(--grid)"], ["negative", "Negative feed-in price", "var(--home)"],
    ["inverter", "Inverter at maximum", "var(--solar)"], ["full", "Battery full, not exporting", "var(--battery)"]];
  function drawLimits() {
    const d = limData ? { ...limData } : null, panel = $("lim-panel");
    if (d && d.modelled && d.days) {  // days before the weather model's reach have no kWh: start the chart after them
      const i = d.days.findIndex((x) => x.modelled);
      if (i > 0) d.days = d.days.slice(i);
    }
    const any = d && d.days && d.days.some((x) => LIM.some(([k]) => (x[k] || {}).minutes));
    panel.hidden = !(d && d.days && d.days.length >= 2);
    if (panel.hidden) return;
    const lg = $("lim-legend"); lg.innerHTML = "";
    const used = LIM.filter(([k]) => d.days.some((x) => (x[k] || {}).minutes));
    for (const [, l, c] of used) lg.append(el("span", { class: "legend-item" }, el("span", { class: "sw", style: `background:${c};border-color:${c}` }), l));
    const sm = $("lim-summary"); sm.innerHTML = "";
    const m = d.this_month, p = d.period;
    const tot = (o) => LIM.reduce((a, [k]) => a + (o[k].kwh || 0), 0);
    const mins = (o) => LIM.reduce((a, [k]) => a + (o[k].minutes || 0), 0);
    if (d.modelled) {
      sm.append(el("span", {}, el("b", { text: `${tot(m).toFixed(1)} kWh` }), "held back this month"),
        el("span", {}, el("b", { text: `${tot(p).toFixed(1)} kWh` }), "in the last 30 days"));
      const lost = p.export.value + p.inverter.value + p.full.value, saved = -p.negative.value;
      if (lost >= 0.01) sm.append(el("span", {}, el("b", { text: `$${lost.toFixed(2)}` }), "of exports missed"));
      if (saved >= 0.01) sm.append(el("span", {}, el("b", { text: `$${saved.toFixed(2)}` }), "not paid to export at negative prices"));
    } else {
      sm.append(el("span", {}, el("b", { text: `${Math.round(mins(p) / 60)} h` }), "of limited solar in the last 30 days"));
    }
    const label = (day) => new Date(day + "T12:00:00").toLocaleDateString([], { day: "numeric", month: "short" });
    const title = (i) => new Date(d.days[i].day + "T12:00:00").toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
    if (!any) {
      $("lim-chart").innerHTML = ""; $("lim-chart").append(el("div", { class: "empty", text: "Nothing held back in the last 30 days." }));
    } else {
      const series = (used.length ? used : LIM).map(([k, l, c]) => ({ key: k, label: l, color: c,
        values: d.days.map((x) => (d.modelled ? (x[k] || {}).kwh || 0 : ((x[k] || {}).minutes || 0) / 60)) }));
      C.stackedBarChart($("lim-chart"), { labels: d.days.map((x) => label(x.day)), series, height: 200, title,
        yFmt: (v) => `${v}`, ariaLabel: d.modelled ? "Solar held back each day, kWh, by reason" : "Hours of limited solar each day, by reason",
        tip: (i) => LIM.filter(([k]) => (d.days[i][k] || {}).minutes).map(([k, l, c]) => {
          const x = d.days[i][k];
          return [l, `${x.kwh != null ? `${x.kwh.toFixed(1)} kWh, ` : ""}${x.minutes} min${x.value ? `, ${x.value < 0 ? "saved" : "worth"} $${Math.abs(x.value).toFixed(2)}` : ""}`, c];
        }) });
    }
    const caps = d.caps || {};
    $("lim-note").textContent = "Solar the panels could have made but didn't, against the weather model's expected output for each hour: when exports sat at the limit" +
      (caps.export_w ? ` (${(caps.export_w / 1000).toFixed(1)} kW, ${caps.export_source === "setting" ? "from Settings" : "learned"})` : "") +
      ", the feed-in price was negative and nothing was exported (turned down on purpose), the inverter was at its maximum, or the battery was full with nowhere to send it. Estimates; passing cloud isn't counted." +
      (d.modelled ? "" : " Turn on the weather forecast (Settings, latitude and longitude) to estimate kWh.");
  }
  async function loadLimits() {
    try { limData = await S.fetchJSON("/api/limits?days=30"); drawLimits(); } catch (e) { /* banner */ }
  }

  // ------------------------------------------------------------ battery wear
  let wearData = null;
  function wearEnergyTile(w, wa) {
    const mon = (d) => new Date(d + "T12:00:00").toLocaleDateString([], { month: "short", year: "numeric" });
    const div = el("div", {}, el("div", { class: "k", text: "Energy through it" }),
      el("div", { class: "v" }, w.discharged_mwh.toFixed(w.discharged_mwh < 10 ? 2 : 1), el("small", { text: "MWh" })),
      el("div", { class: "d", text: wa.mwh ? `${wa.mwh_used_pct}% of ${wa.mwh} MWh warranty` : "discharged" }));
    if (wa.mwh && wa.mwh_projected) {
      const early = wa.ends && wa.mwh_projected < wa.ends;
      div.append(el("div", { class: "d" + (early ? " warn" : ""),
        title: `At ${wa.mwh_per_year} MWh a year (${wa.mwh_basis === "since" ? "the average since " + new Date(w.since + "T12:00:00").toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) : "the last 30 days"}). ` +
          (early ? `That's before the ${wa.years}-year warranty ends (${mon(wa.ends)}), so the energy limit would end it first.` : `The ${wa.years}-year limit (${mon(wa.ends)}) comes first.`),
        text: `limit about ${mon(wa.mwh_projected)}` }));
    }
    return div;
  }
  function renderWear() {
    const w = wearData, panel = $("wear-panel");
    panel.hidden = !(w && w.enabled);
    if (panel.hidden) return;
    const tiles = $("wear-tiles"); tiles.innerHTML = "";
    const tile = (k, v, unit, d, cls) => el("div", {}, el("div", { class: "k", text: k }),
      el("div", { class: "v" }, v, unit ? el("small", { text: unit }) : null), d ? el("div", { class: "d" + (cls ? " " + cls : ""), text: d }) : null);
    const h = w.health, wa = w.warranty || {};
    tiles.append(
      tile("Cycles", w.cycles.toLocaleString(undefined, { maximumFractionDigits: w.cycles < 100 ? 1 : 0 }), "",
        w.recent.cycles_per_day != null ? `${w.recent.cycles_per_day} a day lately` : `${w.cycles_per_day} a day`),
      wearEnergyTile(w, wa),
      tile("Health", h.now != null ? h.now.toFixed(1) : "–", "%",
        h.per_year != null ? `${h.per_year > 0 ? "+" : ""}${h.per_year}% a year` : h.days >= 1 ? "no clear trend yet" : null),
      tile("At warranty end", h.at_warranty_end != null ? h.at_warranty_end.toFixed(0) : "–", h.at_warranty_end != null ? "%" : "",
        wa.ends ? `${new Date(wa.ends + "T12:00:00").getFullYear()}${wa.soh_floor ? `, warranty ${wa.soh_floor}%` : ""}` : null,
        wa.soh_floor && h.at_warranty_end != null && h.at_warranty_end < wa.soh_floor ? "warn" : null),
      tile("Round trip", w.round_trip != null ? w.round_trip.toFixed(0) : "–", w.round_trip != null ? "%" : "", "energy out vs in"),
      tile("Battery temperature", w.temps.avg != null ? w.temps.avg.toFixed(0) : "–", w.temps.avg != null ? "°C" : "",
        w.temps.max != null ? `hottest ${w.temps.max.toFixed(0)}°C, 30 days` : null));
    $("wear-sub").textContent = `${w.lifetime ? "since it was switched on" : "since recording began"} ${new Date(w.since + "T12:00:00").toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })}`;
    $("wear-note").textContent = `One cycle is the battery's full ${w.rated_kwh} kWh out once. ` +
      (w.recent.deep_days ? `${w.recent.deep_days} of the last ${w.recent.days} days used most of it. ` : "") +
      (wa.years ? `At the rate so far it will reach about ${wa.cycles_at_end.toLocaleString()} cycles by the end of its ${wa.years}-year warranty` +
        (wa.mwh_at_end ? ` and ${wa.mwh_at_end} MWh` + (wa.mwh_reached ? `, passing the ${wa.mwh} MWh limit around ${new Date(wa.mwh_reached + "T12:00:00").toLocaleDateString([], { month: "short", year: "numeric" })}` : "") : "") + ". " : "") +
      "Health is the battery's own reading; a trend is shown once it has moved a point over four months. Warranty terms come from Settings (Battery).";
  }
  async function loadWear() {
    try { wearData = await S.fetchJSON("/api/battery-wear"); renderWear(); } catch (e) { /* banner */ }
  }


  // ------------------------------------------------------------ storm warning
  function renderStorm(st, bt) {
    const b = $("storm-banner");
    b.classList.toggle("show", !!st);
    if (!st) { b.innerHTML = ""; return; }
    const at = new Date(st.at * 1000), until = new Date(st.until * 1000), now = new Date();
    const tm = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: d.getMinutes() ? "2-digit" : undefined });
    const day = (d) => (d.toDateString() === now.toDateString() ? "" : (() => { const t = new Date(now); t.setDate(now.getDate() + 1);
      return d.toDateString() === t.toDateString() ? " tomorrow" : ` ${d.toLocaleDateString([], { weekday: "long" })}`; })());
    const when = st.now ? `now, until about ${tm(until)}` : `from ${tm(at)}${day(at)} until about ${tm(until)}`;
    const soc = st.soc != null ? Math.round(st.soc) : null;
    const batt = soc == null ? "" : st.now ? ` The battery is at ${soc}%.` : soc >= 90 ? ` The battery should be nearly full by then (about ${soc}%).`
      : ` The battery outlook has it at about ${soc}% by then.`;
    const tip = soc != null && soc < 90 && !st.now ? " To keep more in reserve for a power cut, raise the backup reserve in the mySigen app beforehand, and lower it again afterwards." : "";
    b.innerHTML = "";
    const ic = el("svg", { width: 22, height: 22, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
    ic.append(el("path", { d: "M17.5 15.5a4 4 0 0 0-.9-7.9A6 6 0 0 0 5 9.5a3.5 3.5 0 0 0 .5 7" }), el("path", { d: "M12 12l-2.5 4.5h4L11 21" }));
    b.append(ic, el("div", { class: "txt" }, el("b", { text: `${st.desc} forecast ${when}.` }), ` Power cuts are more likely in storms.${batt}${tip}`));
  }

  // ------------------------------------------------------------ always-on use
  let blData = null;
  function drawBaseload() {
    const d = blData, panel = $("bl-panel");
    panel.hidden = !(d && d.enabled);
    if (panel.hidden) return;
    const tiles = $("bl-tiles"); tiles.innerHTML = "";
    const tile = (k, v, u, dtext, cls) => el("div", {}, el("div", { class: "k", text: k }), el("div", { class: "v" }, v, u ? el("small", { text: u }) : null),
      dtext ? el("div", { class: "d" + (cls ? " " + cls : ""), text: dtext }) : null);
    const ch = d.change_w;
    tiles.append(
      tile("Always on now", d.now_w, "W", "middle of the last 7 nights"),
      tile("Four weeks ago", d.before_w ?? "–", d.before_w != null ? "W" : "",
        ch == null ? "not enough history yet" : Math.abs(ch) < 10 ? "about the same" : `${ch > 0 ? "up" : "down"} ${Math.abs(ch)} W since`, ch == null || Math.abs(ch) < 10 ? null : ch > 0 ? "up" : "down"),
      tile("A year of it", d.kwh_year.toLocaleString(), "kWh", `${(d.now_w * 24 / 1000).toFixed(1)} kWh a day`),
      tile("Costs about", d.cost_year != null ? `$${d.cost_year.toLocaleString()}` : "–", d.cost_year != null ? "a year" : "",
        d.price_c != null ? `at ${d.price_c}c/kWh` : "needs retailer prices"),
      tile("Share of home use", d.share != null ? d.share : "–", d.share != null ? "%" : "", "the last 30 days"));
    const t = d.nights.map((x) => new Date(x.day + "T03:00:00").getTime() / 1000);
    C.lineChart($("bl-chart"), { t, series: [{ key: "w", label: "Always on", color: "var(--home)", values: d.nights.map((x) => x.w) }],
      height: 170, yMin: 0, gapSeconds: 86400 * 3, yFmt: (v) => `${v} W`, tipFmt: (v) => (v == null ? "–" : `${Math.round(v)} W`),
      tipTitle: (x) => `Night of ${new Date((x - 86400) * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}`,
      ariaLabel: "Always-on use each night" });
    $("bl-note").textContent = "The level the house doesn't drop below between midnight and 5 am (the quietest tenth of the night): fridge and freezer, network gear, standby power, pumps and anything left on. " +
      "Every 10 W is about 88 kWh a year. A step up that stays is worth hunting down: switch things off at the wall one at a time and watch Home in Right now." +
      (d.price_c != null ? " Priced at your average buy price, though overnight it mostly comes from the battery." : "");
  }
  async function loadBaseload() {
    try { blData = await S.fetchJSON("/api/baseload?days=90"); drawBaseload(); } catch (e) { /* banner */ }
  }


  // ------------------------------------------------------------ battery trading
  let btData = null;
  function renderTrading() {
    const t = btData, panel = $("bt-panel");
    panel.hidden = !(t && t.enabled);
    if (panel.hidden) return;
    const money2 = (v) => (v == null ? "–" : `${v < 0 ? "−" : ""}$${Math.abs(v).toFixed(2)}`);
    const tiles = $("bt-tiles"); tiles.innerHTML = "";
    const tile = (k, v, u, d) => el("div", {}, el("div", { class: "k", text: k }), el("div", { class: "v" }, v, u ? el("small", { text: u }) : null),
      d ? el("div", { class: "d", text: d }) : null);
    const pr = t.prices;
    tiles.append(
      tile("Battery sold to the grid", pr.battery_kwh.toFixed(0), "kWh", `earning ${money2(pr.battery_value)}`),
      tile("Battery export price", pr.battery_avg_c != null ? pr.battery_avg_c.toFixed(0) : "–", pr.battery_avg_c != null ? "c/kWh" : "",
        pr.best_hour_avg_c != null ? `best hour each day averaged ${pr.best_hour_avg_c.toFixed(0)}c` : null),
      tile("Spikes caught", `${t.caught} of ${t.spike_count}`, "", t.spike_count ? `earned ${money2(t.spike_earned)}` : `none at $${(t.spike_c / 100).toFixed(0)}/kWh or more`),
      tile("Missed in spikes", money2(t.spike_missed_value), "", t.spike_count ? "what full exports could have added" : null));
    const st = $("bt-spikes"); st.innerHTML = "";
    if (!t.spikes.length) st.append(el("tr", {}, el("td", { class: "muted", text: `No price spikes in the last ${t.days} days.` })));
    else {
      st.append(el("tr", {}, ...["When", "Peak", "Sold", "Earned", "Battery", ""].map((h) => el("th", { text: h }))));
      for (const s of t.spikes.slice().reverse()) {
        const d = new Date(s.start * 1000);
        st.append(el("tr", { title: `${s.minutes} min at an average of $${(s.avg_c / 100).toFixed(2)}/kWh. ${s.battery_kwh} kWh from the battery. ` +
            `It could have sold about ${s.could_kwh} kWh (${money2(s.could_earn)}).` },
          el("td", { text: `${d.toLocaleDateString([], { day: "numeric", month: "short" })}, ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` }),
          el("td", { text: `$${(s.peak_c / 100).toFixed(2)}` }), el("td", { text: `${s.kwh.toFixed(1)} kWh` }), el("td", { text: money2(s.earned) }),
          el("td", { text: s.soc_start != null ? `${Math.round(s.soc_start)}%` : "–" }),
          s.caught ? el("td", { class: "ok", text: "caught" }) : el("td", { class: "miss", title: s.why, text: s.why === "battery near its reserve" ? "missed: battery low" : "missed: not sold" })));
      }
    }
    const rt = $("bt-reserve"); rt.innerHTML = "";
    rt.append(el("tr", {}, ...["Reserve", "Extra grid", "Spike gain", "Net", "Outage cover"].map((h) => el("th", { text: h }))));
    rt.append(el("tr", {}, el("td", { text: `${Math.round(t.reserve)}% (now)` }), el("td", { text: "–" }), el("td", { text: "–" }), el("td", { text: "–" }),
      el("td", { text: "–" })));
    for (const w of t.what_if) {
      rt.append(el("tr", { title: `Holds back ${w.held_kwh} kWh; about ${w.extra_grid_kwh} kWh more bought from the grid over ${t.days} days` },
        el("td", { text: `${w.reserve}%` }), el("td", { text: money2(w.extra_cost) }), el("td", { text: money2(w.spike_gain) }),
        el("td", { class: w.net >= 0 ? "ok" : null, text: money2(w.net) }),
        el("td", { text: w.outage_hours != null ? `+${w.outage_hours} h` : `+${w.held_kwh} kWh` })));
    }
    $("bt-reserve-note").textContent = `Over the last ${t.days} days: what the house would have bought instead while the battery stopped at the higher level, against what that held-back energy could have earned in spikes missed for lack of charge. Outage cover is at your always-on use. Change the reserve in the mySigen app.`;
    $("bt-sub").textContent = `last ${t.days} days, from the collector's readings`;
    $("bt-note").textContent = `A spike is when the feed-in price reaches $${(t.spike_c / 100).toFixed(0)}/kWh. Caught means at least half of what the battery could have sold (at the fastest export seen, ${(t.max_export_w / 1000).toFixed(1)} kW, from its charge above the ${Math.round(t.reserve)}% reserve). Estimates from minute readings: your retailer's own control isn't visible, only what it did.`;
  }
  async function loadTrading() {
    try { btData = await S.fetchJSON("/api/trading?days=30"); renderTrading(); } catch (e) { /* banner */ }
  }


  // ------------------------------------------------------------ car charging plan
  (function evPlan() {
    const kwh = $("ev-plan-kwh"), by = $("ev-plan-by"), out = $("ev-plan-out");
    if (!kwh) return;
    kwh.value = S.store.get("sigen.evplan.kwh") || kwh.value;
    by.value = S.store.get("sigen.evplan.by") || by.value;
    const tm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const day = (ts) => { const d = new Date(ts * 1000), n = new Date(); if (d.toDateString() === n.toDateString()) return "";
      const t = new Date(n); t.setDate(n.getDate() + 1); return d.toDateString() === t.toDateString() ? " tomorrow" : ` ${d.toLocaleDateString([], { weekday: "short" })}`; };
    $("ev-plan-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      S.store.set("sigen.evplan.kwh", kwh.value); S.store.set("sigen.evplan.by", by.value);
      out.textContent = "Working it out…";
      try {
        const p = await S.fetchJSON(`/api/ev-plan?kwh=${encodeURIComponent(kwh.value)}&by=${encodeURIComponent(by.value)}`);
        out.innerHTML = "";
        if (!p.enabled) { out.textContent = `Can't plan yet: ${p.reason}.`; return; }
        out.append(el("div", {}, el("b", { text: `$${p.cost.toFixed(2)}` }), ` for ${p.kwh} kWh, about ${p.avg_c}c/kWh at ${p.kw} kW` +
          (p.solar_kwh >= 0.5 ? `, ${p.solar_kwh} kWh of it spare solar` : "") +
          (p.saving != null && p.saving >= 0.05 ? `. Starting now instead: $${p.now_cost.toFixed(2)}.` : ".") +
          (p.short_kwh > 0 ? ` The price forecast only reaches ${tm(p.until)}${day(p.until)}, so ${p.short_kwh} kWh is left for after that.` : "")));
        const ul = el("ul");
        for (const w of p.windows) ul.append(el("li", { text: `${tm(w.start)}${day(w.start)} to ${tm(w.end)}: ${w.kwh} kWh` }));
        out.append(ul);
      } catch (err) { out.textContent = `Couldn't plan: ${err.message}`; }
    });
  })();

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
  loadWeather();
  setInterval(loadWeather, 10 * 60000);
  loadOutlook();
  setInterval(loadOutlook, 5 * 60000);
  loadMonthly(); loadTrends(); loadMoney(); loadAppliances(); loadLimits(); loadWear(); loadBaseload(); loadTrading(); loadWhatIf();
  setInterval(() => { loadMonthly(); loadTrends(); loadMoney(); loadLimits(); loadWear(); loadBaseload(); loadTrading(); loadWhatIf(); }, 60 * 60000);
  setInterval(loadAppliances, 5 * 60000);
  setInterval(loadPrices, 5 * 60000);
  setInterval(loadHistory, 60000);
  setInterval(loadDaily, 10 * 60000);
  window.SigenRedraw = () => { drawBaseload(); drawLimits(); drawHistory(); drawPrices(); drawStrings(); drawEvTimeline(); drawWeather(); drawOutlook(); drawMonthly(); drawTrends(); if (dailyData.length) drawDaily(); };
  let rt;
  new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { drawBaseload(); drawLimits(); drawHistory(); drawPrices(); drawStrings(); drawEvTimeline(); drawWeather(); drawOutlook(); drawMonthly(); drawTrends(); if (dailyData.length) drawDaily(); }, 120); })
    .observe(document.querySelector(".wrap"));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { drawHistory(); drawDaily(); });
})();
