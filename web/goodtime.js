// The "good time to use power?" indicator in the header: a level from free to spike, from what an
// extra kWh costs you now (spare solar, battery or grid), with a drop-down saying why, when the
// next good time is, the scale, and the best times to run appliances.
(function () {
  const S = window.Sigen, el = S.el, $ = (id) => document.getElementById(id);
  const box = $("gt"), btn = $("gt-btn"), pop = $("gt-pop"), body = $("gt-body");
  if (!box) return;
  const ICON = {
    free: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4"/>',
    green: '<path d="M5 19c0-8 5-13 14-14-1 9-6 14-14 14z"/><path d="M5 19l7-7"/>',
    normal: '<path d="M9 3v5M15 3v5"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v4"/>',
    dear: '<circle cx="12" cy="12" r="8.5"/><path d="M14.5 9.3c-.5-.8-1.4-1.3-2.5-1.3-1.5 0-2.6.8-2.6 2s1.1 1.7 2.6 2 2.6.8 2.6 2-1.1 2-2.6 2c-1.1 0-2.1-.5-2.6-1.3M12 6.5v1.5M12 16v1.5"/>',
    spike: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.3v.2"/>',
  };
  const icon = (lv, size = 18) => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", size); s.setAttribute("height", size);
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "1.9");
    s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round"); s.setAttribute("aria-hidden", "true");
    s.innerHTML = ICON[lv] || ICON.normal;
    return s;
  };
  const NAMES = { free: "Free", green: "Good", normal: "Normal", dear: "Dear", spike: "Spike" };
  const c = (v) => (v == null ? "–" : `${Math.abs(v) >= 100 ? `$${(v / 100).toFixed(2)}` : `${Math.round(v * 10) / 10}c`}`);
  const tm = (t) => {
    const d = new Date(t * 1000), now = new Date();
    const s = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return s;
    const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
    return d.toDateString() === tmr.toDateString() ? `${s} tomorrow` : `${s} ${d.toLocaleDateString([], { weekday: "short" })}`;
  };
  // "11:45 am to 12:30 pm tomorrow": the day only once
  const range = (a, b) => {
    const da = new Date(a * 1000), db = new Date(b * 1000);
    const t = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return da.toDateString() === db.toDateString() ? `${t(da)} to ${tm(b)}` : `${tm(a)} to ${tm(b)}`;
  };
  let data = null;

  function render() {
    const d = data;
    box.hidden = !(d && d.enabled);
    if (box.hidden) return;
    box.dataset.level = d.level;
    const priceTxt = d.source === "spare solar" ? "spare solar" : `${c(d.c_kwh)}/kWh`;
    btn.replaceChildren(icon(d.level), el("span", { class: "gt-l", text: d.label }), el("span", { class: "gt-c", text: priceTxt }));
    btn.title = `${d.label}: an extra kWh now costs about ${c(d.c_kwh)} (${d.source}). Click for when it's best to use power.`;
    btn.setAttribute("aria-label", `${d.label}. An extra kilowatt hour now costs about ${c(d.c_kwh)}. Show details.`);

    body.replaceChildren();
    const lead = d.level === "free" ? `Using power now is about free: ${d.why}.`
      : d.level === "green" ? `A good time to use power: ${d.why}, and an extra kWh costs about ${c(d.c_kwh)}.`
        : d.level === "spike" ? `Price spike: ${d.why}, at ${c(d.c_kwh)}/kWh. Put off what you can.`
          : d.level === "dear" ? `Power is dear now: ${d.why}, at about ${c(d.c_kwh)}/kWh.`
            : `An ordinary time: ${d.why}, at about ${c(d.c_kwh)}/kWh.`;
    body.append(el("p", { class: "gt-lead", text: lead }));
    const facts = el("dl", { class: "facts" });
    const add = (k, v) => { if (v) facts.append(el("dt", { text: k }), el("dd", { text: v })); };
    if (d.good_until) add("Good until", tm(d.good_until));
    if (d.next_good) add("Next good time",
      `${range(d.next_good.start, d.next_good.end)}, ${d.next_good.level === "free" ? "about free" : `from about ${c(d.next_good.c_kwh)}`}`);
    else if (!d.good_until) add("Next good time", "none in the forecast");
    if (d.next_spike) add("Spike forecast", `${tm(d.next_spike.start)}, ${c(d.next_spike.c_kwh)}/kWh`);
    if (d.renewables != null) add("Grid renewables", `${d.renewables}%`);
    body.append(facts);

    // the scale, with where now sits
    const steps = [["free", `3c or less`], ["green", `under ${c(d.green_below)}`], ["normal", ""], ["dear", `${c(d.dear_from)} or more`], ["spike", "$1 or more"]];
    body.append(el("div", { class: "gt-scale", role: "img", "aria-label": `Scale from free to spike; now: ${NAMES[d.level]}` },
      ...steps.map(([lv, txt]) => el("div", { class: "gt-step" + (lv === d.level ? " on" : ""), "data-level": lv },
        el("span", { class: "bar" }), el("b", { text: NAMES[lv] }), el("small", { text: txt })))));
    const how = (d.scale === "fixed" ? "Fixed prices" : "From your own prices over the last fortnight") +
      (d.renewables_min != null ? `; good also needs ${d.renewables_min}% renewables or more` : "") +
      `. Spare solar is costed at the feed-in it would have earned, the battery at what the house will pay later (about ${c(d.battery_c)}/kWh), the grid at its price now.`;
    body.append(el("p", { class: "chart-note" }, how + " ", el("a", { href: "settings.html#dashboard", text: "Change the scale" })));
  }

  async function load() {
    try { data = await S.fetchJSON("/api/goodtime"); render(); } catch (e) { /* the status pill says if the collector is down */ }
  }
  function open(on) {
    pop.hidden = !on;
    btn.setAttribute("aria-expanded", on ? "true" : "false");
    if (on && window.SigenLoadAppliances) window.SigenLoadAppliances();
  }
  btn.addEventListener("click", (e) => { e.stopPropagation(); open(pop.hidden); });
  document.addEventListener("click", (e) => { if (!pop.hidden && !box.contains(e.target)) open(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pop.hidden) { open(false); btn.focus(); } });
  load();
  setInterval(load, 60000);
})();
