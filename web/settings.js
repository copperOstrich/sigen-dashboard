// Settings page: every setting from sigen.env, editable from the collector's own network.
// The collector decides who may change things and never sends keys or private values to
// anyone else; this page only reflects that.
(function () {
  const S = window.Sigen, el = S.el, $ = (id) => document.getElementById(id);
  let data = null;
  const edits = new Map();   // key -> new value ("" clears it)
  let errors = {};
  const revealed = {};       // key -> value shown with "Show"
  const replacing = new Set();

  const ICON = {
    lock: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    ok: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  };

  const original = (e) => (e.kind === "choice" ? (e.value ?? e.default ?? "") : e.source === "default" ? "" : (e.value ?? ""));
  const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-");

  function banner(kind, html) {
    const b = $("banner"); b.innerHTML = "";
    if (!kind) return;
    const d = el("div", { class: "set-banner" + (kind === "warn" ? "" : " " + kind), role: kind === "bad" ? "alert" : "status" });
    d.innerHTML = (kind === "ok" ? ICON.ok : kind === "warn" ? ICON.lock : "") + "<div></div>";
    d.lastChild.append(...(Array.isArray(html) ? html : [html]));
    b.append(d);
  }

  function setEdit(e, value) {
    if (value === original(e)) edits.delete(e.key); else edits.set(e.key, value);
    delete errors[e.key];
    const row = document.querySelector(`.set-row[data-key="${e.key}"]`);
    if (row) { row.classList.toggle("changed", edits.has(e.key)); row.classList.remove("has-err"); row.querySelector(".set-err")?.remove(); }
    updateBar();
  }

  function updateBar() {
    const n = edits.size;
    $("bar").classList.toggle("show", n > 0 && data.editable);
    $("bar-msg").textContent = `${n} unsaved change${n === 1 ? "" : "s"}. Saving restarts the collector.`;
  }

  function control(e, disabled, id) {
    const ctl = el("div", { class: "set-ctl" });
    const cur = edits.has(e.key) ? edits.get(e.key) : original(e);
    if (e.kind === "secret") {
      if (replacing.has(e.key)) {
        const inp = el("input", { id, type: "text", autocomplete: "off", spellcheck: "false", placeholder: "Paste the new value", value: edits.get(e.key) ?? "" });
        inp.addEventListener("input", () => setEdit(e, inp.value.trim()));
        ctl.append(inp, el("button", { class: "small-btn", type: "button", text: "Cancel",
          onclick: () => { replacing.delete(e.key); edits.delete(e.key); render(); } }));
        return ctl;
      }
      const shown = revealed[e.key];
      const cleared = edits.has(e.key) && edits.get(e.key) === "";
      ctl.append(el("span", { class: "secret", id, text: cleared ? "Will be cleared" : shown != null ? shown : e.set ? (e.hint || "Set (hidden)") : "Not set" }));
      if (!disabled) {
        if (e.set && !cleared) ctl.append(el("button", { class: "small-btn", type: "button", text: shown != null ? "Hide" : "Show", onclick: async () => {
          if (shown != null) { delete revealed[e.key]; return render(); }
          try { revealed[e.key] = (await S.fetchJSON(`/api/settings?reveal=${encodeURIComponent(e.key)}`)).value; } catch (err) { /* not allowed */ }
          render();
        } }));
        ctl.append(el("button", { class: "small-btn", type: "button", text: e.set ? "Replace" : "Add", onclick: () => { replacing.add(e.key); render(); $(id)?.focus(); } }));
        if (e.set) ctl.append(el("button", { class: "small-btn", type: "button", text: cleared ? "Keep" : "Clear",
          onclick: () => { cleared ? edits.delete(e.key) : edits.set(e.key, ""); render(); } }));
      }
      return ctl;
    }
    if (e.masked) {
      ctl.append(el("input", { id, type: "text", disabled: true, value: "", placeholder: e.set ? "Hidden" : "Not set" }));
      return ctl;
    }
    let inp;
    if (e.kind === "choice") {
      inp = el("select", { id, disabled });
      const opts = [...e.options];
      if (!opts.some((o) => o[0] === cur)) opts.push([cur, cur || "(not set)"]);
      for (const [v, label] of opts) inp.append(el("option", { value: v, text: label, selected: v === cur }));
      inp.addEventListener("change", () => setEdit(e, inp.value));
    } else {
      const numeric = e.kind === "int" || e.kind === "number";
      inp = el("input", { id, type: e.kind === "date" ? "date" : "text", disabled, value: cur, spellcheck: "false",
        class: numeric || e.kind === "date" ? "short" : null, inputmode: numeric ? "decimal" : null, autocomplete: "off",
        placeholder: e.source === "default" && e.default !== "" && e.default != null ? `Default: ${e.default}` : (e.placeholder || "") });
      inp.addEventListener("input", () => setEdit(e, inp.value.trim()));
    }
    ctl.append(inp);
    if (e.unit) ctl.append(el("span", { class: "unit", text: e.unit }));
    return ctl;
  }

  function row(e) {
    const id = "f-" + e.key;
    const disabled = !data.editable || !!e.readonly;
    const r = el("div", { class: "set-row" + (edits.has(e.key) ? " changed" : "") + (errors[e.key] ? " has-err" : ""), "data-key": e.key },
      el("label", { for: id }, e.label, el("span", { class: "key", text: e.key })),
      control(e, disabled, id));
    const help = el("div", { class: "set-help" });
    if (e.readonly) help.append(el("span", { class: "tag", text: "Read-only" }));
    if (e.source === "container") help.append(el("span", { class: "tag", title: "Set in the container's environment; saving here writes it to sigen.env, which takes priority", text: "From the container" }));
    if (e.masked || (e.kind === "secret" && !data.editable)) help.append(el("span", { class: "tag", text: "Hidden away from home" }));
    help.append(e.help || "");
    if (e.kind !== "choice" && e.kind !== "secret" && e.default && e.source !== "default" && !e.readonly) help.append(` Default ${e.default}${e.unit ? " " + e.unit : ""}.`);
    if (help.childNodes.length) r.append(help);
    if (errors[e.key]) r.append(el("div", { class: "set-err", text: errors[e.key] }));
    return r;
  }

  function render() {
    const keep = document.activeElement && document.activeElement.id;
    const groups = $("groups"); groups.innerHTML = "";
    const nav = $("nav"); nav.innerHTML = "";
    const all = [...data.groups];
    if (data.other.length) all.push({ name: "Other settings in sigen.env", settings: data.other.map((o) => ({ ...o, label: o.key, kind: "text", source: "file",
      help: "Not one the dashboard knows about; kept as it is unless you change it here." })) });
    for (const g of all) {
      nav.append(el("a", { href: "#" + slug(g.name), text: g.name }));
      const sec = el("section", { class: "panel set-group", id: slug(g.name) }, el("h2", { text: g.name }));
      g.settings.forEach((e) => sec.append(row(e)));
      groups.append(sec);
    }
    if (keep && $(keep)) $(keep).focus();
    updateBar();
  }

  function renderAccess() {
    $("access-dot").className = "dot " + (data.editable ? "ok" : "warn");
    $("access-text").textContent = data.editable ? "You can change settings" : "Read-only";
    if (!data.editable) {
      banner("warn", [el("strong", { text: "Read-only. " }), `Settings can only be changed from a device on your home network, and ${data.reason || "this device isn't on it"}. ` +
        "Keys, addresses and your location are hidden."]);
      $("intro").textContent = "";
    } else {
      $("intro").textContent = `Saved to ${data.file}. Saving restarts the collector, which takes a few seconds; the dashboard reconnects by itself.`;
    }
  }

  async function load() {
    data = await S.fetchJSON("/api/settings");
    edits.clear(); errors = {}; replacing.clear();
    for (const k of Object.keys(revealed)) delete revealed[k];
    renderAccess(); render();
  }

  async function waitForRestart() {
    const t0 = Date.now();
    await new Promise((r) => setTimeout(r, 1500));
    while (Date.now() - t0 < 60000) {
      try { const h = await S.fetchJSON("/api/health"); if (h && h.version) return true; } catch (e) { /* still starting */ }
      await new Promise((r) => setTimeout(r, 1000));
    }
    return false;
  }

  async function save() {
    const changes = Object.fromEntries(edits);
    $("save").disabled = true;
    try {
      const res = await fetch(S.apiBase() + "/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ changes }) });
      const out = await res.json().catch(() => ({}));
      if (!res.ok) {
        errors = out.errors || {};
        banner("bad", out.error || `Couldn't save (${res.status})`);
        render();
        const first = Object.keys(errors)[0];
        if (first) document.querySelector(`.set-row[data-key="${first}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
        else window.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }
      if (!out.restarting) { await load(); banner("ok", "Nothing needed changing."); return; }
      $("busy").classList.add("show");
      const back = await waitForRestart();
      if (!back) {
        $("busy-title").textContent = "The collector hasn't come back yet";
        $("busy-text").textContent = "Check its log in Container Station. Your settings were saved to sigen.env (the old file is sigen.env.bak).";
        return;
      }
      await load();
      $("busy").classList.remove("show");
      const n = out.changed.length;
      banner("ok", `Saved ${n} setting${n === 1 ? "" : "s"} and restarted the collector with ${n === 1 ? "it" : "them"}.`);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      banner("bad", `Couldn't reach the collector: ${e.message}`);
    } finally {
      $("save").disabled = false;
    }
  }

  $("save").addEventListener("click", save);
  $("discard").addEventListener("click", () => { edits.clear(); errors = {}; replacing.clear(); render(); banner(null); renderAccess(); });
  window.addEventListener("beforeunload", (ev) => { if (edits.size) { ev.preventDefault(); ev.returnValue = ""; } });
  load().catch((e) => banner("bad", `Couldn't load the settings: ${e.message}`));
})();
