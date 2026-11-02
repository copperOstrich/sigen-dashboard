// README screenshots, light and dark, from a collector running the built-in simulator:
//
//   DATA_DIR=/tmp/sim HTTP_PORT=8098 AEMO_SIMULATE=1 DEMO_HISTORY_DAYS=60 python collector/sigen_collector.py &
//   node tools/screenshots.js http://localhost:8098            (or add names: ... battery,arrange)
//
// Needs Playwright (npm i playwright). Writes docs/screenshots/<name>-light.png and -dark.png.
const { chromium } = require("playwright");
const path = require("path");

const base = process.argv[2] || "http://localhost:8098";
const only = process.argv[3] ? process.argv[3].split(",") : null;  // e.g. battery,arrange
const out = path.join(__dirname, "..", "docs", "screenshots");

// [name, page, what to capture, tab to pick first, viewport width, layout override]
const SHOTS = [
  ["dashboard", "/", null, null, 1400],
  ["power-flow", "/", 'section[data-panel="flow"]', null, 1400],
  ["battery", "/", 'section[data-panel="battery"]', null, 1400, { battery: "12" }],
  ["forecast", "/", 'section[data-panel="forecast"]', "Next two days", 1400],
  ["ev-charging", "/", 'section[data-panel="ev"]', null, 1400],
  ["prices-grid", "/", 'section[data-panel="price"]', "Your price", 1400],
  ["history", "/", 'section[data-panel="history"]', "Power", 1400],
  ["monthly", "/", 'section[data-panel="history"]', "Months", 1400],
  ["strings", "/", 'section[data-panel="solar"]', "Strings", 1400],
  ["money", "/", 'section[data-panel="money"]', "Plans", 1400],
  ["trading", "/", 'section[data-panel="money"]', "Trading", 1400],
  ["system", "/", 'section[data-panel="system"]', null, 1400],
  ["arrange", "/", "#layout-dlg", null, 1100],
  ["phone", "/", null, null, 390],
  ["widget", "/widget.html", null, null, 420],
  ["settings", "/settings.html", null, null, 1100],
];

(async () => {
  const browser = await chromium.launch();
  for (const scheme of ["light", "dark"]) {
    for (const [name, page, sel, tab, width, widths] of SHOTS) {
      if (only && !only.includes(name)) continue;
      const p = await browser.newPage({ viewport: { width, height: name === "phone" ? 844 : name === "widget" ? 520 : 900 }, colorScheme: scheme });
      await p.goto(base + page);
      await p.evaluate((w) => {
        localStorage.clear();
        if (w) localStorage.setItem("sigen.layout.v1", JSON.stringify({ order: [], off: [], width: w }));
      }, widths || null);
      await p.reload();
      await p.waitForTimeout(8000);
      if (name === "arrange") { await p.click("#layout-btn"); await p.waitForTimeout(500); }
      if (tab) {
        const b = p.locator(`${sel} .tabs button`, { hasText: tab });
        if (await b.count()) { await b.first().click(); await p.waitForTimeout(900); }
      }
      const file = path.join(out, `${name}-${scheme}.png`);
      if (sel) await p.locator(sel).screenshot({ path: file });
      else await p.screenshot({ path: file, fullPage: false });
      console.log(file);
      await p.close();
    }
  }
  await browser.close();
})();
