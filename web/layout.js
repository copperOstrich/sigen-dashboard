// Panel layout: which panels show, their order and width. Saved per browser (localStorage).
(function () {
  const S = window.Sigen, el = S.el, $ = (id) => document.getElementById(id);
  const KEY = "sigen.layout.v1";
  const WIDTHS = [["12", "Full width"], ["8", "Two thirds"], ["6", "Half"], ["4", "One third"]];
  const sections = [...document.querySelectorAll(".grid > section[data-panel]")];
  const byKey = Object.fromEntries(sections.map((s) => [s.dataset.panel, s]));
  const spanOf = (s) => (s.className.match(/\bspan-(\d+)\b/) || [, "12"])[1];
  const DEFAULT = { order: sections.map((s) => s.dataset.panel), off: [], width: Object.fromEntries(sections.map((s) => [s.dataset.panel, spanOf(s)])) };

  function load() {
    let saved = null;
    try { saved = JSON.parse(S.store.get(KEY) || "null"); } catch (e) { saved = null; }
    if (!saved || !Array.isArray(saved.order)) return structuredClone(DEFAULT);
    // panels added in later versions go where they are by default
    const order = saved.order.filter((k) => byKey[k]);
    DEFAULT.order.forEach((k, i) => { if (!order.includes(k)) order.splice(Math.min(i, order.length), 0, k); });
    return { order, off: (saved.off || []).filter((k) => byKey[k]), width: { ...DEFAULT.width, ...(saved.width || {}) } };
  }
  let layout = load();
  const save = () => S.store.set(KEY, JSON.stringify(layout));

  function apply() {
    layout.order.forEach((k, i) => {
      const s = byKey[k];
      s.style.order = i;
      s.classList.toggle("user-off", layout.off.includes(k));
      s.classList.remove("span-12", "span-8", "span-6", "span-4");
      s.classList.add("span-" + (layout.width[k] || "12"));
    });
    clearTimeout(apply.t);
    apply.t = setTimeout(() => window.SigenRedraw && window.SigenRedraw(), 60);  // charts follow the new widths
  }

  // ------------------------------------------------------------ dialog
  const dlg = $("layout-dlg"), list = $("layout-list");
  let dragKey = null;
  function move(k, to) {
    const from = layout.order.indexOf(k);
    to = Math.max(0, Math.min(layout.order.length - 1, to));
    if (from === to) return;
    layout.order.splice(from, 1); layout.order.splice(to, 0, k);
    save(); apply(); render(k);
  }
  function render(focusKey) {
    list.innerHTML = "";
    layout.order.forEach((k, i) => {
      const s = byKey[k], on = !layout.off.includes(k);
      const auto = s.hasAttribute("hidden") ? " (shows when there's data)" : "";
      const cb = el("input", { type: "checkbox", id: "lay-" + k });
      cb.checked = on;
      cb.addEventListener("change", () => {
        layout.off = cb.checked ? layout.off.filter((x) => x !== k) : [...layout.off, k];
        save(); apply(); li.classList.toggle("off", !cb.checked);
      });
      const sel = el("select", { "aria-label": `Width of ${s.dataset.name}` },
        ...WIDTHS.map(([v, t]) => { const o = el("option", { value: v, text: t }); if ((layout.width[k] || "12") === v) o.selected = true; return o; }));
      sel.addEventListener("change", () => { layout.width[k] = sel.value; save(); apply(); });
      const up = el("button", { type: "button", class: "icon-btn", "aria-label": `Move ${s.dataset.name} up`, text: "↑", disabled: i === 0 ? "" : null });
      const dn = el("button", { type: "button", class: "icon-btn", "aria-label": `Move ${s.dataset.name} down`, text: "↓", disabled: i === layout.order.length - 1 ? "" : null });
      up.addEventListener("click", () => move(k, i - 1));
      dn.addEventListener("click", () => move(k, i + 1));
      const li = el("li", { class: "layout-item" + (on ? "" : " off"), draggable: "true", "data-key": k },
        el("span", { class: "grip", "aria-hidden": "true", text: "⋮⋮" }),
        cb, el("label", { for: "lay-" + k }, s.dataset.name, auto ? el("span", { class: "muted", text: auto }) : null),
        sel, up, dn);
      li.addEventListener("dragstart", (e) => { dragKey = k; li.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", k); });
      li.addEventListener("dragend", () => { dragKey = null; li.classList.remove("dragging"); });
      li.addEventListener("dragover", (e) => { if (dragKey && dragKey !== k) { e.preventDefault(); li.classList.add("over"); } });
      li.addEventListener("dragleave", () => li.classList.remove("over"));
      li.addEventListener("drop", (e) => { e.preventDefault(); li.classList.remove("over"); if (dragKey) move(dragKey, layout.order.indexOf(k)); });
      list.append(li);
      if (focusKey === k) (i === 0 ? dn : up).focus();
    });
  }
  $("layout-btn").addEventListener("click", () => { render(); dlg.showModal(); });
  $("layout-reset").addEventListener("click", () => { layout = structuredClone(DEFAULT); save(); apply(); render(); });

  apply();
})();
