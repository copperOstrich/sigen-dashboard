// Shared helpers for the dashboard and the desktop widget.
// Works both when served by the collector (same origin) and inside the desktop app,
// where the collector address is stored locally and passed as ?api=http://host:8080
(function () {
  const S = {};
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
  };

  S.isDesktop = !!window.__TAURI__;
  const qp = new URLSearchParams(location.search).get("api");
  if (qp) store.set("sigen.api", qp.replace(/\/+$/, ""));
  S.apiBase = function () {
    if (location.protocol.startsWith("http") && !S.isDesktop && !qp) return "";
    return (store.get("sigen.api") || "http://sigen-dashboard.local:8080").replace(/\/+$/, "");
  };
  S.setApiBase = function (url) { store.set("sigen.api", url.replace(/\/+$/, "")); };
  S.store = store;

  S.fetchJSON = async function (path) {
    const res = await fetch(S.apiBase() + path, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  };

  // Live updates: Server-Sent Events, falling back to polling.
  S.subscribe = function (onData, onError) {
    let es = null, timer = null, closed = false;
    const poll = async () => {
      try { onData(await S.fetchJSON("/api/live")); } catch (e) { onError && onError(e); }
      if (!closed) timer = setTimeout(poll, 5000);
    };
    const start = () => {
      if (!window.EventSource) return poll();
      es = new EventSource(S.apiBase() + "/api/stream");
      let gotOne = false;
      es.onmessage = (ev) => { gotOne = true; try { onData(JSON.parse(ev.data)); } catch (e) { /* ignore */ } };
      es.onerror = (e) => {
        onError && onError(new Error("Can't reach the collector"));
        if (!gotOne) { es.close(); es = null; poll(); }
      };
    };
    start();
    return { close() { closed = true; es && es.close(); clearTimeout(timer); },
             restart() { this.close(); closed = false; start(); } };
  };

  S.kw = function (w, opts = {}) {
    if (w == null || isNaN(w)) return "–";
    const a = Math.abs(w);
    if (a < 1000 && !opts.forceKw) return `${Math.round(a)} W`;
    return `${(a / 1000).toFixed(a >= 10000 ? 1 : opts.digits ?? 1)} kW`;
  };
  // Axis labels: whole kW where the tick is whole, one decimal otherwise (2.5 kW, not "3 kW")
  S.kwAxis = (w) => { const k = Math.round(w / 100) / 10; return `${Number.isInteger(k) ? k : k.toFixed(1)} kW`; };
  S.kwNum = (w) => (w == null ? "–" : (Math.abs(w) / 1000).toFixed(Math.abs(w) >= 10000 ? 1 : 2));
  S.kwh = (v, d = 1) => (v == null ? "–" : `${Number(v).toFixed(d)} kWh`);
  S.pct = (v, d = 0) => (v == null ? "–" : `${Number(v).toFixed(d)}%`);
  S.time = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  S.ago = function (ts) {
    if (!ts) return "never";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 10) return "just now";
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    return `at ${S.time(ts)}`;
  };
  S.IDLE_W = 40; // below this a flow counts as idle

  // Plain-language descriptions of what each flow is doing
  S.describe = function (p) {
    const b = p.battery ?? 0, g = p.grid ?? 0;
    return {
      battery: Math.abs(b) < S.IDLE_W ? "Idle" : b > 0 ? "Charging" : "Discharging",
      grid: Math.abs(g) < S.IDLE_W ? "Balanced" : g > 0 ? "Buying" : "Selling",
    };
  };

  S.el = function (tag, attrs = {}, ...children) {
    const svg = ["svg", "g", "path", "line", "rect", "circle", "text", "polyline", "defs", "clipPath", "tspan", "title"].includes(tag);
    const node = svg ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c != null) node.append(c);
    return node;
  };

  // ---------------------------------------------------------------- app updates (desktop only)
  S.UPDATE_REPO = "copperOstrich/sigen-dashboard";
  const verParts = (v) => String(v || "0").replace(/^v/, "").split(/[.+-]/).map((x) => parseInt(x, 10) || 0);
  S.compareVersions = function (a, b) {
    const x = verParts(a), y = verParts(b);
    for (let i = 0; i < Math.max(x.length, y.length); i++) {
      if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0) ? 1 : -1;
    }
    return 0;
  };
  S.checkForUpdate = async function () {
    if (!S.isDesktop || !window.__TAURI__.app) return null;
    const current = await window.__TAURI__.app.getVersion();
    let res;
    try {
      res = await fetch(`https://api.github.com/repos/${S.UPDATE_REPO}/releases/latest`,
        { headers: { Accept: "application/vnd.github+json" }, cache: "no-store" });
    } catch (e) { return { current, error: "Can't reach GitHub" }; }
    if (!res.ok) return { current, error: res.status === 404 ? "No public release found yet" : `GitHub returned ${res.status}` };
    const rel = await res.json();
    const latest = String(rel.tag_name || "").replace(/^v/, "");
    const isWin = /Windows/i.test(navigator.userAgent);
    const asset = (rel.assets || []).find((a) => (isWin ? /\.msi$/i : /\.dmg$/i).test(a.name));
    // A release only counts once this platform's installer has been attached (builds take a few minutes)
    return { current, latest, newer: !!latest && !!asset && S.compareVersions(latest, current) > 0, asset, page: rel.html_url };
  };
  S.installUpdate = function (asset) {
    return window.__TAURI__.core.invoke("install_update", { url: asset.browser_download_url, name: asset.name });
  };

  window.Sigen = S;
})();
