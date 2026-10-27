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
    const c = kind === "ok" ? "var(--good)" : kind === "warn" ? "var(--warn)" : "var(--bad)";
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
  async function loadMoney() {
    let d;
    try { d = await S.fetchJSON("/api/money"); } catch (e) { return; }
    const panel = $("money-panel");
    panel.hidden = !d || !d.enabled;
    if (panel.hidden) return;
    const me = d.retailer || "Your retailer";
    const cmp = d.compare, pb = d.payback, tr = d.trades || {}, pl = d.plans;
    $("money-sub").textContent = pl ? `compared with ${pl.list.length === 1 ? `the ${pl.list[0].name}` : "other plans"}${pl.title ? `, ${pl.title}` : ""}` : "";

    // ---- is your retailer worth it?
    const c1 = $("money-compare"); c1.innerHTML = "";
    c1.append(el("h3", { text: `Is ${me} worth it?` }));
    if (!pl) {
      c1.append(el("div", { class: "setup" },
        el("p", { text: "Compare what you pay with the Default Market Offer (the standard-plan price cap) or your own quotes, using your own usage hour by hour." }),
        el("p", {}, "Set ", el("code", { text: "COMPARE_PLANS" }), " to ", el("code", { text: "nsw-ausgrid" }), ", ",
          el("code", { text: "nsw-endeavour" }), " or ", el("code", { text: "nsw-essential" }),
          ", or put your own plans in ", el("code", { text: "plans.json" }), " in the data folder.")));
    } else if (!cmp || !cmp.total || !cmp.total.days) {
      c1.append(el("p", { class: "setup", text: d.reason ? `Not yet: ${d.reason}.` : "Not enough history yet; check back after a full day." }));
    } else {
      const t = cmp.total, best = t.cheapest, diff = t.saving_vs_cheapest;
      const span = t.days >= 360 ? "the last 12 months" : `the last ${t.days} day${t.days === 1 ? "" : "s"}`;
      c1.append(el("div", { class: "big" }, money$(Math.abs(diff)), el("small", { text: diff >= 0 ? "saved" : "extra" })),
        el("p", { class: "lead", text: `${me} cost ${money$(Math.abs(diff))} ${diff >= 0 ? "less" : "more"} than ${pl.list.length === 1 ? `the ${best}` : `the cheapest alternative (${best})`} over ${span}${cmp.energy_only ? ", energy only" : ""}.` }));
      const rows = [[me, t.actual, true], ...Object.entries(t.plans).map(([n, v]) => [n, v, false])].sort((a, b) => a[1] - b[1]);
      const dshort = (x) => new Date(x + "T12:00:00").toLocaleDateString([], { day: "numeric", month: "short" });
      const max = Math.max(...rows.map((r) => Math.abs(r[1])), 1);
      for (const [n, v, mine] of rows) {
        c1.append(el("div", { class: "mix-row cmp-row" + (mine ? " mine" : ""), title: `${n}: ${money$(v, 2)} over ${span}` },
          el("span", { class: "mix-k", text: n }),
          el("span", { class: "mix-bar" }, el("i", { style: `width:${Math.max(1, Math.abs(v) / max * 100)}%` })),
          el("span", { class: "mix-v", text: money$(v) })));
      }
      // the same split as the retailer's app (energy) and bill (plus fixed charges), to check against
      c1.append(el("p", { class: "chart-note", text: `${me}, ${dshort(t.first)} to ${dshort(t.last)}: ${money$(t.actual_energy, 2)} for energy` +
        (t.actual_fixed ? ` plus ${money$(t.actual_fixed, 2)} supply charge and fee` : "") + "." }));
    }
    if (d.waiting_days) c1.append(el("p", { class: "chart-note", text: `${d.waiting_days} imported day${d.waiting_days === 1 ? " is" : "s are"} waiting for ${me}'s costs, which the collector fetches in the background; they're left out until then.` }));

    // ---- payback
    const c2 = $("money-payback"); c2.innerHTML = "";
    c2.append(el("h3", { text: "Solar and battery payback" }));
    if (!pb) {
      c2.append(el("p", { class: "setup" }, d.reason && !cmp ? `Not yet: ${d.reason}.` : "",
        ...(d.reason && !cmp ? [] : ["Set ", el("code", { text: "SYSTEM_COST" }), " (what the system cost, in dollars) and, optionally, ",
          el("code", { text: "SYSTEM_INSTALLED" }), " (the date it was switched on) to track when it pays for itself."])));
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

    // ---- battery trades
    const c3 = $("money-trades"); c3.innerHTML = "";
    c3.append(el("h3", { text: "Battery trades with the grid" }));
    const tm = (tr.months || {})[tr.this_month], lm = (tr.months || {})[tr.last_month];
    const sumLine = (label, m) => {
      if (!m) return el("p", { class: "lead", text: `${label}: none` });
      const parts = [];
      if (m.exported_kwh) parts.push(`sent ${m.exported_kwh.toFixed(1)} kWh to the grid for ${m.priced ? money$(m.export_value, 2) : "–"}`);
      if (m.imported_kwh) parts.push(`charged ${m.imported_kwh.toFixed(1)} kWh from the grid for ${m.priced ? money$(m.import_cost, 2) : "–"}`);
      return el("p", { class: "lead", text: `${label}: ${parts.join("; ")}.` });
    };
    if (!(tr.events || []).length) {
      c3.append(el("p", { class: "lead", text: "None in the last 60 days. When the battery sends power to the grid, or charges from it, beyond what the house needs (usually your retailer's battery control, such as Amber SmartShift), it shows here." }));
    } else {
      const net = tm ? (tm.export_value || 0) - (tm.import_cost || 0) : 0;
      c3.append(el("div", { class: "big" }, money$(net, 2), el("small", { text: "this month" })),
        sumLine("This month", tm), sumLine("Last month", lm));
      const ul = el("ul", { class: "trade-list" });
      for (const e of tr.events.slice(0, 6)) {
        const st = new Date(e.start * 1000), en = new Date(e.end * 1000);
        const when = `${st.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}, ${st.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} to ${en.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
        ul.append(el("li", {}, el("span", { class: "when", text: when }),
          el("span", { text: `${e.kind === "export" ? "Sent" : "Charged"} ${e.kwh.toFixed(1)} kWh${e.avg_c != null ? ` at ${Math.round(e.avg_c)}c` : ""}` }),
          el("span", { class: "amt", text: e.value == null ? "–" : `${e.kind === "export" ? "+" : "−"}${money$(e.value, 2)}` })));
      }
      c3.append(ul);
    }

    // ---- month by month table
    const tb = $("money-table"); tb.innerHTML = "";
    if (pl && cmp && cmp.months && cmp.months.length) {
      const names = pl.list.map((p) => p.name);
      tb.append(el("tr", {}, el("th", { text: "Month" }), el("th", { class: "mine", text: me }), ...names.map((n) => el("th", { text: n }))));
      for (const m of cmp.months.slice().reverse()) {
        const vals = [m.actual, ...names.map((n) => m.plans[n])];
        const min = Math.min(...vals);
        tb.append(el("tr", {}, el("td", { text: `${mName(m.month)}${m.days < 28 ? ` (${m.days} d)` : ""}` }),
          ...vals.map((v, i) => el("td", { class: (i === 0 ? "mine " : "") + (v === min ? "best" : ""), text: money$(v, 2) }))));
      }
    }
    const notes = [];
    if (pl) {
      notes.push(`Each plan priced on your own grid use and exports, hour by hour${cmp && cmp.energy_only ? "" : ", with its daily supply charge"}. The same energy flows are used for every plan; on another plan the battery might be run differently, so treat this as a guide.`);
      if (cmp && cmp.energy_only) notes.push(`Energy only: set RETAILER_SUPPLY_C_DAY (and RETAILER_FEE_MONTHLY for a membership fee) from your ${me} bill to include supply charges.`);
      else if (d.fixed_known) notes.push(`${me} includes your ${d.supply_c_day}c/day supply charge${d.fee_monthly ? ` and ${money$(d.fee_monthly, 2)}/month fee` : ""}.`);
      const fromTxt = (d) => new Date(d + "T12:00:00").toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
      notes.push(`Current prices (${pl.source}): ` + pl.list.map((p) => `${p.name} ${p.tou ? "time of use" : `${(+p.usage_c_kwh).toFixed(2)}c/kWh`}, ${(+p.supply_c_day).toFixed(2)}c/day, feed-in ${p.feed_in_c_kwh == null ? "–" : `${p.feed_in_c_kwh}c`}${p.from ? ` from ${fromTxt(p.from)}` : ""}`).join("; ") + ".");
      const early = pl.list.find((p) => p.first_from && cmp && cmp.months && cmp.months.length && cmp.months[0].month + "-31" < p.first_from);
      if (early) notes.push(`Months before ${fromTxt(early.first_from)} use the earliest prices in the plans file.`);
      if (cmp && cmp.imported_days) notes.push(`${cmp.imported_days} of these days come from a mySigen export (daily totals${pl.list.some((p) => p.tou) ? ", so time-of-use plans are approximate for them" : ""}).`);
    }
    $("money-note").textContent = notes.join(" ");
  }

  // ------------------------------------------------------------ monthly summary
  let moData = [], moIdx = -1;
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
    try { moData = await S.fetchJSON("/api/monthly?months=13"); renderMonthly(); } catch (e) { /* banner */ }
  }
  $("mo-prev").addEventListener("click", () => { if (moIdx > 0) { moIdx--; renderMonthly(); } });
  $("mo-next").addEventListener("click", () => { if (moIdx < moData.length - 1) { moIdx++; renderMonthly(); } });

  // ------------------------------------------------------------ trends over months
  let trData = null, trMetric = "strings", trDays = 365;
  const dayT = (d) => new Date(d + "T12:00:00").getTime() / 1000;
  const rolling = (vals, n = 7) => vals.map((_, i) => {
    const w = vals.slice(Math.max(0, i - n + 1), i + 1).filter((v) => v != null);
    return w.length >= Math.min(3, n) ? w.reduce((a, b) => a + b, 0) / w.length : null;
  });
  function drawTrends() {
    const d = trData, panel = $("tr-panel");
    panel.hidden = !(d && d.days && d.days.length >= 2);
    if (panel.hidden) return;
    const t = d.days.map(dayT), lg = $("tr-legend"); lg.innerHTML = "";
    let series = [], opts = {}, note = "";
    const dayTitle = (x) => new Date(x * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
    if (trMetric === "strings") {
      const ids = Object.keys(d.strings).map(Number).sort((a, b) => a - b);
      const total = d.days.map((_, i) => ids.reduce((a, n) => a + (d.strings[n][i] || 0), 0));
      series = ids.map((n, k) => {
        // 7-day share of the total, from summed energy so dull days count less
        const share = d.days.map((_, i) => {
          let a = 0, b = 0;
          for (let j = Math.max(0, i - 6); j <= i; j++) { a += d.strings[n][j] || 0; b += total[j] || 0; }
          return b > 3 ? a / b * 100 : null;
        });
        return { key: n, label: `PV${n}`, ...pvStyle(k), values: share };
      });
      opts = { yFmt: (v) => `${Math.round(v)}%`, tipFmt: (v) => (v == null ? "–" : `${v.toFixed(1)}%`), yMin: 0 };
      note = "Each string's share of the solar, averaged over a week. Shares move a little with the seasons as the sun's path changes, but a string that drifts down against the others is losing output: new shade from a growing tree, dirt, or a failing panel.";
    } else if (trMetric === "insulation") {
      const L = d.insulation_limits || { warn: 0.5, min: 0.37 };
      series = [
        { key: "min", label: "Lowest each day", color: "var(--home)", values: d.stats.insulation.min },
        { key: "avg", label: "Daily average", color: "var(--home)", dash: "2 3", values: d.stats.insulation.avg },
        { key: "warn", label: `Lower than usual (${L.warn} MΩ)`, color: "var(--warn)", dash: "6 4", values: t.map(() => L.warn) },
        { key: "alarm", label: `Alarm (${L.min} MΩ)`, color: "var(--bad)", dash: "6 4", values: t.map(() => L.min) }];
      opts = { yMin: 0, yFmt: (v) => `${v} MΩ`, tipFmt: (v) => (v == null ? "–" : `${v.toFixed(3)} MΩ`) };
      note = "Insulation resistance between the solar wiring and earth. Dips on wet mornings are normal; a lowest-each-day line creeping down over weeks is worth an installer's look before it reaches the alarm level.";
    } else if (trMetric === "battery") {
      const v = d.stats.soh.avg.filter((x) => x != null);
      const lo = v.length ? Math.floor(Math.min(...v) - 1) : 90;
      series = [{ key: "soh", label: "Battery health", color: "var(--battery)", values: d.stats.soh.avg }];
      opts = { yMin: Math.max(0, lo), yMax: 100, yFmt: (x) => `${x}%`, tipFmt: (x) => (x == null ? "–" : `${x.toFixed(1)}%`) };
      note = "Battery state of health as the battery reports it: how much of its original capacity is left. Expect a slow decline of around a percent or two a year.";
    } else {
      series = [{ key: "bt", label: "Battery, hottest each day", color: "var(--battery)", values: d.stats.battery_temp.max },
        { key: "it", label: "Inverter, hottest each day", color: "var(--grid)", dash: "5 4", values: d.stats.inverter_temp.max }];
      opts = { yFmt: (x) => `${x}°`, tipFmt: (x) => (x == null ? "–" : `${x.toFixed(1)} °C`) };
      note = "Hottest battery cell and inverter temperature each day. Summer peaks are expected; a steady rise against similar weather can mean poor airflow around the unit.";
    }
    for (const s of series) lg.append(el("span", { class: "legend-item" }, el("span", { class: "sw" + (s.dash ? " dash" : ""), style: `border-color:${s.color}` }), s.label));
    const hasData = series.some((s) => s.values.some((v) => v != null && s.key !== "warn" && s.key !== "alarm"));
    C.lineChart($("tr-chart"), { t, series, height: 240, gapSeconds: 86400 * 3, tipTitle: dayTitle,
      emptyText: "Not enough readings yet.", ariaLabel: `Trend over ${trDays} days`, ...opts });
    if (!hasData) { $("tr-chart").innerHTML = ""; $("tr-chart").append(el("div", { class: "empty", text: "This builds up from the first day of readings; check back in a few days." })); }
    $("tr-note").textContent = note;
  }
  async function loadTrends() {
    try { trData = await S.fetchJSON(`/api/trends?days=${trDays}`); drawTrends(); } catch (e) { /* banner */ }
  }
  $("tr-metric").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    trMetric = b.dataset.m;
    $("tr-metric").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    drawTrends();
  });
  $("tr-range").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    trDays = +b.dataset.d;
    $("tr-range").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    loadTrends();
  });

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
    const soc = live && live.battery ? live.battery.soc : null;
    add("Battery now", soc != null ? `${Math.round(soc)}%` : null);
    add("Lowest point", o.lowest ? `${Math.round(o.lowest.soc)}% at ${olWhen(o.lowest.t)}` : null);
    add(`Reaches ${o.reserve}% reserve`, o.empty_at ? olWhen(o.empty_at) : "not tonight");
    if (o.night_grid_kwh > 0.05) add("From the grid until sunrise", `${o.night_grid_kwh.toFixed(1)} kWh` + (o.night_cost != null ? `, $${o.night_cost.toFixed(2)}` : ""));
    add("Solar takes over", o.solar_covers_at ? olWhen(o.solar_covers_at) : null);
    const t = o.tomorrow;
    if (t) {
      f.append(el("div", { class: "sep" }));
      add(`${t.label}: solar`, `${t.pv_kwh.toFixed(0)} kWh`);
      add(`${t.label}: home use`, `${t.home_kwh.toFixed(0)} kWh`);
      add(t.full_at ? "Full again" : "Highest charge", t.full_at ? olWhen(t.full_at) : t.max_soc != null ? `${Math.round(t.max_soc)}%` : null);
      if (t.spare_kwh >= 0.5) add("Spare solar after that", `${t.spare_kwh.toFixed(0)} kWh`);
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
    const row = (t) => { if (!rows.has(t)) rows.set(t, { t, act: null, fc: null, pv: null, load: null }); return rows.get(t); };
    if (olPast) olPast.t.forEach((t, i) => { if (olPast.soc[i] != null && t < nowT) row(t).act = olPast.soc[i]; });
    for (const p of proj) Object.assign(row(p.t), { fc: p.soc, pv: p.pv, load: p.load });
    // join the actual line to the projection at "now"
    if (proj.length && olPast) { const r0 = row(proj[0].t); if (r0.act == null && live && live.battery) r0.act = live.battery.soc; }
    const lst = [...rows.values()].sort((a, b) => a.t - b.t);
    const lg = $("ol-legend"); lg.innerHTML = "";
    const items = [["Charge so far", "var(--battery)", false], ["Expected charge", "var(--battery)", true], ["Expected solar", "var(--solar)", true], ["Usual home use", "var(--home)", true]];
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
      series: [{ key: "pv", label: "Expected solar", color: "var(--solar)", dash: "5 4", values: lst.map((r) => r.pv) },
        { key: "load", label: "Usual home use", color: "var(--home)", dash: "2 3", values: lst.map((r) => r.load) }],
      yFmt: S.kwAxis, tipFmt: (v) => (v == null ? "–" : S.kw(v)), tipTitle: title,
      ariaLabel: "Expected solar and usual home use for the same period",
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
  let wxData = null, wxActual = null;
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
    const c = w.current, today = w.daily[0], tom = w.daily[1];
    $("wx-sub").textContent = w.mode === "simulator" ? "simulated"
      : (w.location && w.location.name ? w.location.name.split(",").slice(0, 2).join(",") + ", " : "") + "from Open-Meteo";
    const icon = c.is_day ? c.icon : (c.icon === "sun" ? "cloud" : c.icon);
    now.append(el("div", { class: "wx-now-top" }, wxIcon(c.is_day || c.icon !== "sun" ? icon : "cloud", 44),
      el("div", { class: "wx-temp" }, c.temp == null ? "–" : Math.round(c.temp), el("small", { text: "°C" }))));
    now.append(el("div", { class: "wx-desc", text: c.desc + (c.feels != null ? `, feels like ${Math.round(c.feels)}°` : "") }));
    const facts = el("dl", { class: "facts" });
    const add = (k, v) => { if (v != null) facts.append(el("dt", { text: k }), el("dd", { text: v })); };
    add("Cloud", c.cloud != null ? `${Math.round(c.cloud)}%` : null);
    add("Wind", c.wind != null ? `${Math.round(c.wind)} km/h` : null);
    add("Humidity", c.humidity != null ? `${Math.round(c.humidity)}%` : null);
    if (today) add("Sunrise, sunset", `${clock(today.sunrise)}, ${clock(today.sunset)}`);
    facts.append(el("div", { class: "sep" }));
    const learned = w.factor && w.factor.value != null;
    if (today) {
      add("Solar today, forecast", today.pv_kwh != null ? `${kwh1(today.pv_kwh)} kWh` : "learning");
      const made = live && live.today ? live.today.pv : null;
      add("Made so far", made != null ? `${kwh1(made)} kWh` : null);
      const rem = live && live.weather ? live.weather.remaining_kwh : null;
      add("Still to come", rem != null && learned ? `${kwh1(rem)} kWh` : null);
    }
    if (tom) add("Tomorrow", tom.pv_kwh != null ? `${kwh1(tom.pv_kwh)} kWh, ${tom.desc.toLowerCase()}` : tom.desc);
    now.append(facts);

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

  function drawWeather() {
    const w = wxData;
    if (!w || w.status !== "ok" || $("wx-panel").hidden) return;
    const days = w.daily.slice(0, 2);
    const windows = days.map((d) => [d.sunrise - 1800, d.sunset + 1800]);
    const byHour = new Map();
    for (const h of w.hourly) byHour.set(h.t, { t: h.t + 1800, fc: h.pv_w, cloud: h.cloud, act: null });
    for (const [t, v] of Object.entries(wxActual || {})) { const r = byHour.get(+t); if (r) r.act = v; }
    const rows = [...byHour.values()].sort((a, b) => a.t - b.t);
    const nowT = Date.now() / 1000;
    const series = [
      { key: "fc", label: "Forecast", color: "var(--solar)", dash: "5 4", values: rows.map((r) => r.fc) },
      { key: "act", label: "Actual", color: "var(--solar)", values: rows.map((r) => (r.t - 1800 < nowT ? r.act : null)) },
    ];
    const lg = $("wx-legend"); lg.innerHTML = "";
    for (const s of series) lg.append(el("span", { class: "legend-item" }, el("span", { class: "sw" + (s.dash ? " dash" : ""), style: `border-color:${s.color}` }), s.label));
    C.lineChart($("wx-chart"), {
      t: rows.map((r) => r.t), series, height: 200, yMin: 0, windows, markerT: nowT, gapSeconds: 7200,
      yFmt: S.kwAxis, tipFmt: (v) => (v == null ? "–" : S.kw(v)),
      tipTitle: (t) => { const r = byHour.get(t - 1800); return `${new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "numeric" })}` + (r && r.cloud != null ? `, ${Math.round(r.cloud)}% cloud` : ""); },
      ariaLabel: "Solar forecast for today and tomorrow, hour by hour, with what the panels actually made today",
    });
  }

  async function loadWeather() {
    try {
      wxData = await S.fetchJSON("/api/weather");
      if (wxData && wxData.enabled && wxData.status === "ok") {
        const since = Math.min(...wxData.hourly.map((h) => h.t));
        const hrs = Math.max(1, (Date.now() / 1000 - since) / 3600);
        const h = await S.fetchJSON(`/api/history?hours=${hrs.toFixed(2)}`);
        const acc = {};
        h.t.forEach((t, i) => { const k = Math.floor(t / 3600) * 3600; if (h.pv[i] != null) (acc[k] = acc[k] || []).push(h.pv[i]); });
        wxActual = Object.fromEntries(Object.entries(acc).map(([k, v]) => [k, v.reduce((a, b) => a + b, 0) / v.length]));
      }
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
    const fu = evData && evData.fuel && evData.fuel.enabled ? evData.fuel : null;
    renderEvFuel(fu);
    tb.append(el("tr", {}, el("th", { text: inf ? "Started" : "Plugged in" }), inf ? null : el("th", { text: "For" }), el("th", { text: "Charging" }),
      el("th", { text: "Energy" }), el("th", { class: "hide-sm", text: "From grid" }), el("th", { class: "hide-sm", text: "Grid cost" }),
      fu ? el("th", { class: "hide-sm", text: "Fuel equiv." }) : null, fu ? el("th", { text: "Saved" }) : null));
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
        el("td", { class: "hide-sm", text: r.cost == null ? "–" : `$${r.cost.toFixed(2)}` }),
        fu ? el("td", { class: "hide-sm", title: r.fuel && r.fuel.fuel_cost != null ? `${r.fuel.km} km: ${r.fuel.litres} L at ${r.fuel.price_c}c/L` +
          (r.fuel.price_from === "recorded" ? "" : r.fuel.price_from === "fixed" ? " (your fixed price)" : " (nearest recorded price)") +
          `; charging cost $${(r.fuel.charge_cost ?? 0).toFixed(2)} (grid $${(r.cost ?? 0).toFixed(2)} + solar $${(r.fuel.solar_cost ?? 0).toFixed(2)} at the export price)` : null,
          text: r.fuel && r.fuel.fuel_cost != null ? `$${r.fuel.fuel_cost.toFixed(2)}` : "–" }) : null,
        fu ? el("td", { text: r.fuel && r.fuel.saved != null ? `${r.fuel.saved < 0 ? "−" : ""}$${Math.abs(r.fuel.saved).toFixed(2)}` : "–" }) : null));
    }
    const links = $("ev-links"); links.innerHTML = "";
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
    renderNem(a, IC);
  }

  // all NEM regions: a rough map with prices, demand, renewables and the flows between them
  const REGION_FULL = { NSW1: "New South Wales", QLD1: "Queensland", VIC1: "Victoria", SA1: "South Australia", TAS1: "Tasmania" };
  function renderNem(a, IC) {
    const regions = a.regions || [], nem = a.nem || {};
    $("grid-nem").hidden = regions.length < 2;
    if (regions.length < 2) return;
    const mw = (v) => (v == null ? "–" : `${Math.round(v).toLocaleString()} MW`);
    const pct = (v) => (v == null ? "–" : `${Math.round(v)}%`);
    const flowText = (r) => (r.net_interchange_mw == null || Math.abs(r.net_interchange_mw) < 5 ? "about balanced"
      : `${r.net_interchange_mw > 0 ? "exporting" : "importing"} ${mw(Math.abs(r.net_interchange_mw))}`);
    const priceCls = (r) => (r.rrp == null ? "" : r.rrp < 0 ? "low" : r.rrp >= 300 ? "high" : "");
    // net flow between each pair of regions (QLD-NSW and VIC-SA each have two links)
    const pairs = {};
    for (const x of a.all_interconnectors || []) {
      const m = IC[x.name];
      if (!m || x.mw == null) continue;
      const key = [m[0], m[1]].sort().join("|");
      const sign = m[0] < m[1] ? 1 : -1;  // positive = from the alphabetically first region
      const pr = pairs[key] || (pairs[key] = { mw: 0, parts: [] });
      pr.mw += sign * x.mw;
      pr.parts.push(`${m[2] || x.name}: ${Math.abs(Math.round(x.mw)).toLocaleString()} MW from ${regionName(x.mw >= 0 ? m[0] : m[1])} to ${regionName(x.mw >= 0 ? m[1] : m[0])}`);
    }

    // ---- map (wide screens)
    const W = 270, H = 118;
    const pos = { QLD1: [640, 10], NSW1: [640, 180], SA1: [0, 220], VIC1: [340, 330], TAS1: [340, 510] };
    // link ends: [regionA, regionB, end at A, end at B, label offset and anchor]  (A alphabetically first)
    const links = [["NSW1", "QLD1", [775, 180], [775, 128], [14, 5, "start"]], ["NSW1", "VIC1", [640, 250], [540, 330], [-14, -8, "end"]],
      ["SA1", "VIC1", [270, 279], [340, 389], [-10, 26, "end"]], ["TAS1", "VIC1", [475, 510], [475, 448], [14, 5, "start"]]];
    const svg = el("svg", { viewBox: "0 0 910 630", role: "img", "aria-label": "Prices, demand and flows across the NEM regions" });
    for (const [ra, rb, pa, pb, lab] of links) {
      const f = pairs[[ra, rb].join("|")];
      if (!f) continue;
      svg.append(el("line", { class: "ic", x1: pa[0], y1: pa[1], x2: pb[0], y2: pb[1] }));
      const [from, to] = f.mw >= 0 ? [pa, pb] : [pb, pa];
      const mx = (pa[0] + pb[0]) / 2, my = (pa[1] + pb[1]) / 2;
      const ang = Math.atan2(to[1] - from[1], to[0] - from[0]) * 180 / Math.PI;
      if (Math.abs(f.mw) >= 5) svg.append(el("path", { class: "arrow", d: "M 9 0 L -7 -7 L -7 7 Z", transform: `translate(${mx} ${my}) rotate(${ang})` }));
      svg.append(el("text", { class: "icl", x: mx + lab[0], y: my + lab[1], "text-anchor": lab[2] },
        `${Math.abs(Math.round(f.mw)).toLocaleString()} MW`, el("title", { text: f.parts.join("\n") })));
    }
    for (const r of regions) {
      const p = pos[r.region];
      if (!p) continue;
      const [x, y] = p, home = r.region === a.region;
      const g = el("g", {},
        el("title", { text: `${REGION_FULL[r.region] || r.region}: ${cents(r.price_c)}c/kWh ($${r.rrp == null ? "–" : Math.round(r.rrp).toLocaleString()}/MWh)` +
          `${r.price_status && r.price_status !== "FIRM" ? ", not final" : ""}; demand ${mw(r.demand_mw)}; ${flowText(r)}` +
          `${r.renewables_pct != null ? `; ${pct(r.renewables_pct)} renewable` : ""}${r.top_fuel ? `; most from ${r.top_fuel.toLowerCase()}` : ""}` }),
        el("rect", { class: "rg" + (home ? " home" : ""), x, y, width: W, height: H, rx: 12 }),
        el("text", { class: "rn", x: x + 14, y: y + 25, text: REGION_FULL[r.region] || r.region }),
        home ? el("text", { class: "yours", x: x + W - 12, y: y + 25, text: "yours" }) : null,
        el("text", { class: "rp " + priceCls(r), x: x + 14, y: y + 61 }, cents(r.price_c), el("tspan", { class: "ru", dx: 4, text: "c/kWh" })),
        el("text", { class: "rs", x: x + 14, y: y + 85, text: `${mw(r.demand_mw)} demand` + (r.renewables_pct != null ? ` · ${pct(r.renewables_pct)} renewable` : "") }),
        el("text", { class: "rs", x: x + 14, y: y + 106, text: flowText(r) }));
      svg.append(g);
    }
    // whole-market totals in the open space at the top left
    const tot = [["Whole market demand", mw(nem.demand_mw)], ["Renewable share", pct(nem.renewables_pct)],
      ["Average price", nem.price_c == null ? "–" : `${cents(nem.price_c)}c/kWh`]];
    tot.forEach(([k, v], i) => {
      svg.append(el("text", { class: "tot-k", x: 0, y: 22 + i * 66, text: k }), el("text", { class: "tot-v", x: 0, y: 54 + i * 66, text: v }));
    });
    const map = $("grid-nem-map"); map.innerHTML = ""; map.append(svg);

    // ---- table (phones)
    const t = $("grid-nem-table"); t.innerHTML = "";
    t.append(el("tr", {}, ...["Region", "c/kWh", "MW", "Renew.", "Flow MW"].map((h) => el("th", { text: h }))));
    for (const r of regions) {
      const ni = r.net_interchange_mw;
      t.append(el("tr", { class: r.region === a.region ? "home" : "" },
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
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "save") return;
    S.setApiBase($("api-input").value.trim());
    sub.restart(); evData = null; loadWeather(); loadOutlook(); loadMonthly(); loadTrends(); loadMoney(); loadHistory(); loadDaily(); loadPrices(); loadStrings();
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
  loadWeather();
  setInterval(loadWeather, 10 * 60000);
  loadOutlook();
  setInterval(loadOutlook, 5 * 60000);
  loadMonthly(); loadTrends(); loadMoney();
  setInterval(() => { loadMonthly(); loadTrends(); loadMoney(); }, 30 * 60000);
  setInterval(loadPrices, 5 * 60000);
  setInterval(loadHistory, 60000);
  setInterval(loadDaily, 10 * 60000);
  window.SigenRedraw = () => { drawHistory(); drawPrices(); drawStrings(); drawEvTimeline(); drawWeather(); drawOutlook(); drawMonthly(); drawTrends(); if (dailyData.length) drawDaily(); };
  let rt;
  new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { drawHistory(); drawPrices(); drawStrings(); drawEvTimeline(); drawWeather(); drawOutlook(); drawMonthly(); drawTrends(); if (dailyData.length) drawDaily(); }, 120); })
    .observe(document.querySelector(".wrap"));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { drawHistory(); drawDaily(); });
})();
