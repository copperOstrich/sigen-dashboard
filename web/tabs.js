// Panels with tabs (Battery, Solar, System, Prices and the grid): one tab showing at a time, the
// last one chosen remembered in this browser. A tab with nothing to show yet (its page is hidden,
// or every part on it is) is left out, and the panel hides when none has anything.
// Panels marked data-together (Battery, System) show all their tabs side by side instead, when
// they're wide enough (Full width in Arrange panels).
(function () {
  const S = window.Sigen, el = S.el;
  const groups = [...document.querySelectorAll("section.group[data-panel]")];
  const KEY = (g) => "sigen.tab." + g.dataset.panel;

  const pages = (g) => [...g.querySelectorAll(":scope > .tabpage")];
  const ready = (p) => !p.hidden && (!p.querySelector(":scope > .part") || [...p.querySelectorAll(":scope > .part")].some((x) => !x.hidden));

  const TOGETHER_W = 1100;
  function render(g) {
    const ps = pages(g), avail = ps.filter(ready);
    const hide = !avail.length;
    if (g.hidden !== hide) g.hidden = hide;
    if (hide) return;
    const together = g.hasAttribute("data-together") && avail.length > 1 && g.clientWidth >= TOGETHER_W;
    if (g.classList.contains("together") !== together) { g.classList.toggle("together", together); redraw(); }
    let cur = g.dataset.tab;
    const want = S.store.get(KEY(g));
    // the remembered tab, as soon as it has something to show (its data can arrive after the page loads)
    if (!g._chosen && want && avail.some((p) => p.dataset.tab === want)) cur = want;
    else if (!g._chosen && !want) cur = avail[0].dataset.tab;  // nothing chosen yet: the first tab, once it has data
    if (!avail.some((p) => p.dataset.tab === cur)) cur = avail[0].dataset.tab;
    const changed = g.dataset.tab !== cur;
    g.dataset.tab = cur;
    for (const p of ps) p.classList.toggle("inactive", p.dataset.tab !== cur);
    const bar = g.querySelector(".tabs");
    const sig = avail.map((p) => p.dataset.tab).join("|") + "#" + cur;
    if (bar.dataset.sig !== sig) {
      bar.dataset.sig = sig;
      bar.replaceChildren(...(avail.length < 2 ? [] : avail.map((p) => {
        const on = p.dataset.tab === cur;
        const b = el("button", { type: "button", role: "tab", "aria-selected": on ? "true" : "false", tabindex: on ? "0" : "-1",
          id: `tab-${g.dataset.panel}-${p.dataset.tab.replace(/\W+/g, "-").toLowerCase()}`, text: p.dataset.tab });
        b.addEventListener("click", () => choose(g, p.dataset.tab));
        b.addEventListener("keydown", (e) => {
          const i = avail.indexOf(p);
          const to = e.key === "ArrowRight" ? avail[(i + 1) % avail.length] : e.key === "ArrowLeft" ? avail[(i - 1 + avail.length) % avail.length] : null;
          if (to) { e.preventDefault(); choose(g, to.dataset.tab); bar.querySelector(`[aria-selected="true"]`)?.focus(); }
        });
        return b;
      })));
    }
    if (changed) redraw();
  }

  function choose(g, tab) {
    g._chosen = true;
    S.store.set(KEY(g), tab);
    g.dataset.tab = tab;
    render(g);
    redraw();
  }

  let rt;
  function redraw() {  // charts drawn while their tab was hidden had no width to work with
    clearTimeout(rt);
    rt = setTimeout(() => window.SigenRedraw && window.SigenRedraw(), 30);
  }

  for (const g of groups) {
    render(g);
    new MutationObserver(() => render(g)).observe(g, { subtree: true, attributes: true, attributeFilter: ["hidden"] });
    if (g.hasAttribute("data-together")) {
      let w = 0;
      new ResizeObserver(() => { if (Math.abs(g.clientWidth - w) > 20) { w = g.clientWidth; render(g); } }).observe(g);
    }
  }
  window.SigenTabs = { render: () => groups.forEach(render) };
})();
