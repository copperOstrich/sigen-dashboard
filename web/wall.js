// Wide screens (a super-ultrawide monitor, or a TV on its side... well, on its back): every panel at
// once, no scrolling. When the window is very wide for its height, the panels are laid out in
// columns like a newspaper, in the Arrange panels order, scaled down only as far as needed
// (to 75% at most) for everything to fit. Narrower windows keep the normal layout.
(function () {
  const S = window.Sigen, $ = (id) => document.getElementById(id);
  const grid = document.querySelector(".wrap > .grid"), wrap = document.querySelector(".wrap"), foot = $("foot");
  if (!grid) return;
  const sections = [...grid.querySelectorAll(":scope > section[data-panel]")];
  const KEY = "sigen.wall";
  const MIN_W = 3000, MIN_RATIO = 2;         // 3440×1440 and wider (not half of a 5120 screen)
  const COL = 940, GAP = 14;                 // target column width at 100%, gap between panels
  const ZOOMS = [1, 0.95, 0.9, 0.85, 0.8, 0.75];
  let on = false, cols = [], sig = "", zoom = 1, timer = null;

  const pref = () => S.store.get(KEY) || "auto";
  const wanted = () => pref() !== "off" && innerWidth >= MIN_W && innerWidth / innerHeight >= MIN_RATIO;
  const visible = () => sections.filter((s) => !s.hidden && !s.classList.contains("user-off"))
    .sort((a, b) => (+a.style.order || 0) - (+b.style.order || 0));

  function setZoom(z) {
    zoom = z;
    for (const n of [wrap, foot]) if (n) n.style.zoom = z === 1 ? "" : String(z);
  }

  function enter() {
    on = true; sig = "";
    document.body.classList.add("wall-mode");
    wrap.classList.add("wall");
    if (foot) foot.classList.add("wall");
  }

  function leave() {
    on = false; sig = "";
    for (const s of sections) grid.append(s);  // back to their places, in page order
    cols.forEach((c) => c.remove()); cols = [];
    grid.style.gridTemplateColumns = "";
    document.body.classList.remove("wall-mode");
    wrap.classList.remove("wall");
    if (foot) foot.classList.remove("wall");
    setZoom(1);
    if (window.SigenRedraw) window.SigenRedraw();
  }

  // down the first column, then the next; null if it needs more than n columns
  function inOrder(hs, n, cap) {
    const out = Array.from({ length: n }, () => []);
    let c = 0, used = 0;
    for (let i = 0; i < hs.length; i++) {
      if (used && used + GAP + hs[i] > cap) { c++; used = 0; if (c >= n) return null; }
      used = used ? used + GAP + hs[i] : hs[i];
      if (used > cap) return null;
      out[c].push(i);
    }
    return out;
  }
  // each panel to the shortest column (keeps the order within each column)
  function balanced(hs, n) {
    const out = Array.from({ length: n }, () => []), tot = new Array(n).fill(0);
    hs.forEach((h, i) => { const c = tot.indexOf(Math.min(...tot)); out[c].push(i); tot[c] += h + (out[c].length > 1 ? GAP : 0); });
    return { out, tallest: Math.max(...tot) };
  }

  function room() {
    const top = (grid.getBoundingClientRect().top + scrollY) / zoom;   // header and banners, unscaled
    return innerHeight / zoom - top - (foot ? foot.offsetHeight : 0) - 10;
  }

  function pack(force) {
    timer = null;
    if (!wanted()) { if (on) leave(); return; }
    if (!on) { enter(); force = true; }
    // only a panel's size changed: keep things where they are while they still fit
    if (!force && cols.length && Math.max(...cols.map((c) => c.offsetHeight)) <= room()) return;
    const panels = visible();
    if (!panels.length) return;
    // heights now, in unscaled pixels (a little extra, as they change a bit with the column width)
    const hs = panels.map((s) => Math.ceil(s.offsetHeight * 1.03));
    const cs = getComputedStyle(wrap);
    const padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    const top = (grid.getBoundingClientRect().top + scrollY) / zoom;   // header and banners, unscaled
    const below = (foot ? foot.offsetHeight : 0) + 10;
    let best = null;
    for (const z of ZOOMS) {
      const w = innerWidth / z - padX - 2;
      const n = Math.max(2, Math.floor((w + GAP) / (COL + GAP)));
      const cap = innerHeight / z - top - below;
      const a = inOrder(hs, n, cap);
      if (a) { best = { z, n, a }; break; }
      const b = balanced(hs, n);
      if (b.tallest <= cap) { best = { z, n, a: b.out }; break; }
      if (z === ZOOMS[ZOOMS.length - 1]) best = { z, n, a: b.out };  // doesn't fit even small: scroll a little
    }
    const key = best.z + "|" + best.n + "|" + best.a.map((c) => c.map((i) => panels[i].dataset.panel).join(",")).join("/");
    if (key === sig) return;
    sig = key;
    setZoom(best.z);
    while (cols.length < best.n) { const c = document.createElement("div"); c.className = "wall-col"; grid.append(c); cols.push(c); }
    while (cols.length > best.n) cols.pop().remove();
    grid.style.gridTemplateColumns = `repeat(${best.n}, minmax(0, 1fr))`;
    best.a.forEach((list, c) => list.forEach((i) => cols[c].append(panels[i])));
    // panels hidden or switched off still need a home (they come back by themselves)
    for (const s of sections) if (!panels.includes(s)) cols[cols.length - 1].append(s);
    if (window.SigenRedraw) window.SigenRedraw();
    // once the charts have redrawn at the new width, check it still fits with the real heights
    setTimeout(() => schedule(0, false), 900);
  }

  let wantForce = false;
  function schedule(delay, force) {
    wantForce = wantForce || !!force;
    clearTimeout(timer);
    timer = setTimeout(() => { const f = wantForce; wantForce = false; pack(f); }, delay);
  }

  addEventListener("resize", () => schedule(250, true));
  // a panel appearing (data arrived), being switched off, reordered or changing size
  const mo = new MutationObserver(() => schedule(400, true));
  sections.forEach((s) => mo.observe(s, { attributes: true, attributeFilter: ["hidden", "class", "style"] }));
  let lastH = new Map();
  const ro = new ResizeObserver((entries) => {
    if (!on) return;
    let moved = false;
    for (const e of entries) {
      const h = Math.round(e.contentRect.height), was = lastH.get(e.target);
      lastH.set(e.target, h);
      if (was != null && Math.abs(was - h) > 24) moved = true;
    }
    if (moved) schedule(800, false);
  });
  sections.forEach((s) => ro.observe(s));

  // the choice in Arrange panels
  const sel = $("wall-pref");
  if (sel) {
    sel.value = pref();
    sel.addEventListener("change", () => { S.store.set(KEY, sel.value); schedule(0, true); });
  }
  window.SigenWall = { pack: () => schedule(0, true), get on() { return on; } };
  schedule(1200, true);  // after the first data has filled the panels
})();
