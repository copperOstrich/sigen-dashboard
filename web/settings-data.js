// Settings page, part two: bills, importing files, the database and new versions.
// Changes go to the collector straight away (no restart); only allowed from the home network.
(function () {
  const S = window.Sigen, el = S.el, $ = (id) => document.getElementById(id);
  const root = $("data-panels");
  let info = null, bills = [], editing = null, busy = false;

  const money = (v) => (v == null ? "" : "$" + Number(v).toFixed(2));
  const mb = (b) => (b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1e3)) + " kB");
  const dt = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });

  async function post(path, body) {
    const res = await fetch(S.apiBase() + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await res.json().catch(() => ({}));
    if (!res.ok && !out.error && !out.messages) out.error = `${res.status} ${res.statusText}`;
    return { ok: res.ok, status: res.status, out };
  }

  function section(id, title, intro) {
    const sec = el("section", { class: "panel set-group", id, "data-nav": title }, el("h2", { text: title }));
    if (intro) sec.append(el("p", { class: "set-intro", style: "margin:2px 0 10px", text: intro }));
    return sec;
  }
  const note = (kind, text) => el("div", { class: "set-note " + (kind || ""), role: kind === "bad" ? "alert" : "status", text });

  // ---------------------------------------------------------------- bills
  function billsPanel() {
    const sec = section("bills", "Bills",
      "Each bill sets what its period cost, exactly: the dashboard scales each day's bought and earned amounts so the " +
      "period adds up to the bill. Amounts in dollars including GST: energy is usage and demand charges (not the " +
      "supply charge or membership fee); credit is what the bill paid for exports.");
    const tbl = el("table", { class: "strings bill-edit" });
    tbl.append(el("tr", {}, ...["Period", "Energy", "Credit", "Avg c/kWh", "Data had", ""].map((t) => el("th", { text: t }))));
    if (!bills.length) tbl.append(el("tr", {}, el("td", { colspan: 6, class: "muted", text: "No bills yet." })));
    for (const b of bills) {
      if (editing && editing.start === b.start && editing.end === b.end) { tbl.append(billForm(b)); continue; }
      const diff = b.difference;
      tbl.append(el("tr", {},
        el("td", {}, `${b.start} to ${b.end}`, el("div", { class: "muted small", text: `${b.days} days${b.file ? " · " + b.file : ""}` })),
        el("td", { class: "num", "data-label": "Energy", text: money(b.bill.bought) }), el("td", { class: "num", "data-label": "Credit", text: money(b.bill.earned) }),
        el("td", { class: "num", "data-label": "Avg c/kWh", text: b.avg_buy_c != null ? Number(b.avg_buy_c).toFixed(2) : "" }),
        el("td", { class: "num", "data-label": "Data had", title: "What the dashboard's own data said for this period's net cost before the bill took over",
          text: b.days_with_data ? `${money(b.data.net).replace("$-", "−$")}${Math.abs(diff) >= 0.01 ? ` · bill $${Math.abs(diff).toFixed(2)} ${diff > 0 ? "more" : "less"}` : " · matches"}` : "no data" }),
        el("td", { class: "acts" }, ...(info.editable ? [
          el("button", { class: "small-btn", type: "button", text: "Edit", disabled: busy, onclick: () => { editing = b; render(); } }),
          el("button", { class: "small-btn", type: "button", text: "Remove", disabled: busy, onclick: () => removeBill(b) })] : []))));
    }
    if (editing === "new") tbl.append(billForm(null));
    sec.append(el("div", { class: "table-scroll" }, tbl));
    if (info.editable && !editing) sec.append(el("button", { class: "ghost-btn set-other-add", type: "button", text: "Add a bill", onclick: () => { editing = "new"; render(); } }));
    if (!info.editable) sec.append(note("", "Bills can be added and changed from your home network."));
    if (billsPanel.msg) sec.append(note(billsPanel.msg[0], billsPanel.msg[1]));
    return sec;
  }

  function billForm(b) {
    const f = {};
    const inp = (k, v, attrs) => (f[k] = el("input", { type: "text", inputmode: "decimal", value: v ?? "", autocomplete: "off", ...attrs }));
    const errs = billForm.errors || {};
    const LBL = { energy_cost: "Energy $", export_credit: "Credit $", avg_buy_c: "Avg c/kWh" };
    const cell = (k, input) => el("td", { "data-label": LBL[k] }, input, errs[k] ? el("div", { class: "set-err", text: errs[k] }) : null);
    const tr = el("tr", { class: "editing" },
      el("td", { class: "dates" }, inp("start", b && b.start, { type: "date", "aria-label": "Start" }), " to ",
        inp("end", b && b.end, { type: "date", "aria-label": "End" }),
        errs.start ? el("div", { class: "set-err", text: errs.start }) : null, errs.end ? el("div", { class: "set-err", text: errs.end }) : null),
      cell("energy_cost", inp("energy_cost", b && b.bill.bought, { "aria-label": "Energy charges, dollars", placeholder: "0.00" })),
      cell("export_credit", inp("export_credit", b && b.bill.earned, { "aria-label": "Export credit, dollars", placeholder: "0.00" })),
      cell("avg_buy_c", inp("avg_buy_c", b && b.avg_buy_c, { "aria-label": "Average usage rate, c/kWh", placeholder: "optional" })),
      el("td", { class: "muted small", text: "" }),
      el("td", { class: "acts" },
        el("button", { class: "primary-btn", type: "button", text: "Save", disabled: busy, onclick: async () => {
          busy = true; render();
          const bill = Object.fromEntries(Object.entries(f).map(([k, i]) => [k, i.value.trim()]));
          const r = await post("/api/bills", { action: "save", bill, replace: b ? { start: b.start, end: b.end } : null }).catch((e) => ({ out: { error: e.message } }));
          busy = false;
          if (r.ok) { bills = r.out.bills; editing = null; billForm.errors = null; billsPanel.msg = ["ok", "Bill saved. Money figures use it from now on."]; }
          else { billForm.errors = r.out.errors || {}; billsPanel.msg = ["bad", r.out.error || "Couldn't save the bill"]; }
          render();
        } }),
        el("button", { class: "small-btn", type: "button", text: "Cancel", onclick: () => { editing = null; billForm.errors = null; billsPanel.msg = null; render(); } })));
    return tr;
  }

  async function removeBill(b) {
    if (!confirm(`Remove the bill for ${b.start} to ${b.end}? Those days go back to Amber's figures or the dashboard's own pricing.`)) return;
    busy = true; render();
    const r = await post("/api/bills", { action: "delete", start: b.start, end: b.end }).catch((e) => ({ out: { error: e.message } }));
    busy = false;
    if (r.ok) { bills = r.out.bills; billsPanel.msg = ["ok", `Removed the bill for ${b.start} to ${b.end}.`]; }
    else billsPanel.msg = ["bad", r.out.error || "Couldn't remove it"];
    render();
  }

  // ---------------------------------------------------------------- import
  function importPanel() {
    const sec = section("import", "Import files",
      "Amber's usage report (the monthly email's CSV, or Usage → download in the Amber app), a mySigen daily Energy " +
      "export (.xlsx), or a bills CSV. Imported straight away; the same as dropping the file in the data folder's import folder.");
    if (!info.editable) { sec.append(note("", "Files can be imported from your home network.")); return sec; }
    const file = el("input", { type: "file", accept: ".csv,.xlsx", id: "imp-file", class: "visually-hidden" });
    const drop = el("label", { class: "drop", for: "imp-file" }, el("strong", { text: "Choose a file" }), " or drop it here");
    const out = el("div", { class: "imp-out", "aria-live": "polite" });
    const go = async (f) => {
      if (!f) return;
      if (f.size > 40 * 1024 * 1024) { out.replaceChildren(note("bad", "That file is larger than 40 MB.")); return; }
      out.replaceChildren(note("", `Importing ${f.name}…`));
      try {
        const buf = new Uint8Array(await f.arrayBuffer());
        let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        const r = await post("/api/import", { name: f.name, data: btoa(bin) });
        const msgs = (r.out.messages || []).map((m) => m.replace(/^Import: /, ""));
        out.replaceChildren(note(r.ok ? "ok" : "bad", r.ok ? "Imported." : (r.out.error || "Not imported.")),
          ...(msgs.length ? [el("ul", { class: "imp-log" }, ...msgs.map((m) => el("li", { text: m })))] : []));
        if (r.ok) { bills = (await S.fetchJSON("/api/bills")).bills || bills; info = await S.fetchJSON("/api/maintenance"); render(out); }
      } catch (e) { out.replaceChildren(note("bad", `Couldn't send the file: ${e.message}`)); }
      file.value = "";
    };
    file.addEventListener("change", () => go(file.files[0]));
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); go(e.dataTransfer.files[0]); });
    sec.append(file, drop, importPanel.out || out);
    importPanel.out = importPanel.out || out;
    if (info.imports && info.imports.length) {
      const d = el("details", { class: "imp-hist" }, el("summary", { text: `Earlier files (${info.imports.length})` }));
      const t = el("table", { class: "strings" }, el("tr", {}, el("th", { text: "File" }), el("th", { text: "" }), el("th", { text: "When" })));
      for (const f of info.imports) t.append(el("tr", {}, el("td", { text: f.name }),
        el("td", { class: f.state === "skipped" ? "bad-text" : "muted", text: f.state === "imported" ? "Imported" : f.state === "skipped" ? "Skipped (see the log)" : "Waiting for a restart" }),
        el("td", { class: "muted", text: when(f.at) })));
      d.append(el("div", { class: "table-scroll" }, t));
      sec.append(d);
    }
    return sec;
  }

  // ---------------------------------------------------------------- database
  function dbPanel() {
    const d = info.database, sec = section("database", "Database");
    const total = d.bytes + d.wal_bytes;
    const rows = d.rows || {};
    const kv = el("dl", { class: "kv" });
    const add = (k, v) => kv.append(el("dt", { text: k }), el("dd", { text: v }));
    add("Size", mb(total) + (d.free_bytes > 1e6 ? ` (${mb(d.free_bytes)} unused, which compacting gives back)` : ""));
    if (d.first) add("History", `${dt(d.first)} to ${dt(d.last)}, kept for ${d.retention_days} days`);
    add("Minute readings", (rows.samples || 0).toLocaleString());
    if (rows.strings != null) add("String readings", rows.strings.toLocaleString());
    if (d.disk) add("Free space on that disk", `${mb(d.disk.free)} of ${mb(d.disk.total)}`);
    if (d.file) add("File", d.file);
    sec.append(kv);
    if (d.disk && d.disk.free < Math.max(2 * total, 500e6)) sec.append(note("bad", "The disk is getting full. Shorten Keep history for, or free some space."));
    if (info.editable) {
      const b = el("button", { class: "ghost-btn", type: "button", text: "Compact the database", disabled: busy });
      b.addEventListener("click", async () => {
        busy = true; b.disabled = true; b.textContent = "Compacting…";
        const r = await post("/api/maintenance", { action: "vacuum" }).catch((e) => ({ out: { error: e.message } }));
        busy = false;
        dbPanel.msg = r.ok ? ["ok", `Done in ${r.out.seconds} s: ${mb(r.out.before)} to ${mb(r.out.after)}.`] : ["bad", r.out.error || "Couldn't compact it"];
        info = await S.fetchJSON("/api/maintenance"); render();
      });
      sec.append(el("div", { class: "row-btns" }, b, el("span", { class: "muted small", text: "Readings pause for a few seconds while it runs. Only worth it after shortening the history." })));
    }
    if (dbPanel.msg) sec.append(note(dbPanel.msg[0], dbPanel.msg[1]));
    return sec;
  }

  // ---------------------------------------------------------------- versions
  function versionPanel() {
    const u = info.update || {}, sec = section("version", "Version");
    const p = el("p", { class: "ver" }, `This collector is version ${info.version}. `);
    if (!u.enabled) p.append("Checking for new versions is off (Tell me about new versions, above).");
    else if (u.newer) p.append(el("strong", { text: `Version ${u.latest} is out` }), u.published ? ` (${u.published}). ` : ". ");
    else if (u.latest) p.append(`That's the latest${u.published ? ` (released ${u.published})` : ""}.`);
    else if (u.error) p.append(`Couldn't check GitHub: ${u.error}.`);
    else p.append("Checking GitHub…");
    sec.append(p);
    if (u.newer) {
      sec.append(el("ol", { class: "steps" },
        el("li", {}, "In Container Station, open the collector's container and choose to edit or recreate it."),
        el("li", {}, "Change the image tag to ", el("code", { text: `${info.upgrade.image}:${u.latest}` }), "."),
        el("li", {}, "Start it. Your data, settings and backups stay in the data folder.")));
      if (u.url) sec.append(el("a", { href: u.url, target: "_blank", rel: "noopener", text: "What's new in " + u.latest }));
    }
    if (u.enabled && info.editable) sec.append(el("div", { class: "row-btns" }, el("button", { class: "small-btn", type: "button", text: "Check now", onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = "Checking…";
      const r = await post("/api/maintenance", { action: "check-update" }).catch(() => null);
      if (r && r.ok) info.update = r.out;
      render();
    } })));
    return sec;
  }

  function render(keepOut) {
    if (!info) return;
    if (keepOut) importPanel.out = keepOut;
    root.replaceChildren(billsPanel(), importPanel(), dbPanel(), versionPanel());
    if (window.SigenSettingsNav) window.SigenSettingsNav();
  }

  async function load() {
    try {
      [info, bills] = await Promise.all([S.fetchJSON("/api/maintenance"), S.fetchJSON("/api/bills").then((b) => b.bills || [])]);
      render();
    } catch (e) { root.replaceChildren(note("bad", `Couldn't load bills and data: ${e.message}`)); }
  }
  load();
})();
