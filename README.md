# Home Energy: SigenStor + Amber dashboard

A local dashboard and desktop widget for a SigenStor system that reads the inverter
directly over **Modbus TCP**, so it keeps working when Sigenergy's cloud is down.
Optional **Amber Electric** prices add buy/sell prices, spike warnings, forecasts and
what your power is costing.

```
SigenStor ──Modbus TCP (read-only)──▶ Collector (Docker / Pi / NAS) ◀── Amber API
                                         │  SQLite history, web dashboard, JSON API
                     ┌───────────────────┼─────────────────────┐
              Browser dashboard   Windows widget app      Mac widget app
```

* **Collector** (`collector/`): Python standard library only, no packages to install.
  Polls every 5 s, keeps minute history for ~2 years, serves the dashboard on port 8080.
* **Dashboard** (`web/`): live power flow, battery detail, today's totals and cost,
  power and battery history, daily energy and cost, Amber prices and forecast,
  system status and alarms. Light and dark mode, works on a phone.
* **Desktop app** (`desktop/`): one app for Windows and macOS. A small always-on-top
  widget plus the full dashboard window, living in the tray / menu bar, with
  "Start at login".

It never writes to the inverter. The Modbus client has no write functions, so it can't
interfere with Amber SmartShift or VPP control.

## 1. Run the collector

On any always-on machine on your home network (NAS, Raspberry Pi, mini PC) with Docker:

```sh
cp .env.example .env        # edit it
docker compose up -d --build
```

Open `http://<that-machine>:8080`.

No Docker? Python 3.9+ is all it needs:

```sh
SIGEN_SIMULATE=1 python3 collector/sigen_collector.py          # simulated data
SIGEN_HOST=192.168.1.60 python3 collector/sigen_collector.py   # real system
```

### Until Modbus TCP is available

Leave `SIGEN_SIMULATE=1`. A built-in simulator of an example home system (10 kWp solar,
16 kWh battery, 10 kW inverter, 5 kW export limit) serves the exact same Modbus
registers, so everything is exercised end to end. To make it resemble your own system,
set `SIM_PV_KWP`, `SIM_BATTERY_KWH`, `SIM_INVERTER_KW` and `SIM_EXPORT_LIMIT_KW`. It pre-fills 14 days of history and
the dashboard shows a "simulated data" banner. Simulated history is kept in a separate
database (`data/simulator.db`) and never mixes with real readings.

You can also run the simulator on its own as a fake inverter:
`python3 collector/simulator.py --port 5020`, then `SIGEN_HOST=127.0.0.1 SIGEN_PORT=5020`.
`SIM_ALARMS="alarm1:9"` sets alarm bits so you can see how alarms look.

### Switching to the real system

1. **Enable Modbus TCP server** on the SigenStor. This is in the mySigen app's
   communication settings and usually needs the **installer account**, so you may
   need to ask your installer. Make sure they tap Save.
2. Give the inverter a **fixed IP** (DHCP reservation in your router).
3. In `.env`: `SIGEN_SIMULATE=0`, `SIGEN_HOST=<inverter IP>`, then
   `docker compose up -d`.

Defaults match Sigenergy's protocol: port 502, plant unit **247** (whole-system values),
inverter unit **1** (battery cells, PV strings, temperatures). If you also have a
Sigen AC charger, the inverter is often unit 2; set `SIGEN_INVERTER_UNIT=2`.

## 2. Add Amber (optional)

1. Sign in at app.amber.com.au, open **Developers**, generate a token.
2. Put it in `.env` as `AMBER_API_KEY=...` and restart.

The key stays on the collector; the desktop apps never see it. You get:

* current buy and sell price, price descriptor, spike status, grid renewables
* past 12 h and next 24 h price chart, "cheapest 2 hours ahead" and forecast spike notes
* a spike banner on the dashboard and price line on the widget
* **today's net cost**, calculated minute by minute from your actual grid flow × the
  price in force at the time
* **daily cost** history; once Amber's meter data arrives (about a day later) it
  replaces the estimate with Amber's own figures

Amber reports feed-in prices as negative when you're paid; the dashboard shows them as
a positive "selling" price and counts them as credit.

In simulator mode without a key, prices are simulated too so you can see the panel.

## 3. Desktop widget app (Windows and Mac)

Built with Tauri (small, native webview; ~10 MB). The installers are built by GitHub
Actions, so you don't need a Mac and a PC set up for development:

1. Push this folder to a GitHub repository.
2. **Actions → Build desktop widget apps → Run workflow.**
3. Download `home-energy-windows-latest` (.msi / .exe) and `home-energy-macos-latest`
   (.dmg, Intel and Apple Silicon) from the run's artifacts.
   Pushing a tag such as `v1.0.0` also creates a draft release with them attached.

On first launch the dashboard window asks for the collector address, e.g.
`http://192.168.1.50:8080`. The widget then appears; drag it by its top bar.
Tray / menu bar icon: show widget, open dashboard, keep on top, start at login, quit.
Closing a window hides it.

The apps aren't code-signed, so the first time:
* **Windows**: SmartScreen → More info → Run anyway.
* **macOS**: right-click the app → Open → Open (or System Settings → Privacy & Security → Open Anyway).

To build locally instead: install Rust and Node, then `cd desktop && npm install && npm run build`.

## API

| Endpoint | What it returns |
|---|---|
| `GET /api/live` | Latest snapshot: power, battery, today, system, alarms, events, price |
| `GET /api/stream` | Same as live, pushed as Server-Sent Events on every poll |
| `GET /api/history?hours=24` | Bucketed power and SoC series (up to 31 days) |
| `GET /api/daily?days=30` | Daily kWh totals and net cost |
| `GET /api/prices?hours=12` | Amber site, current prices, 24 h forecast, recent price history |
| `GET /api/health` | `{ok, status, updated}` for monitoring |

Signs: `grid` positive = buying, negative = selling. `battery` positive = charging.

## Troubleshooting

* **"Inverter not responding"**: check Modbus TCP is enabled and saved, the IP is right,
  and nothing else is holding the inverter's Modbus connection (some systems only allow
  one client; Home Assistant's Sigenergy integration counts).
* **Plant values fine but no battery cell / PV string detail**: wrong inverter unit id,
  see `SIGEN_INVERTER_UNIT`.
* `exception code 2` in logs means a register your firmware doesn't expose; it's
  skipped automatically.
* **Widget says "Can't reach collector"**: click its status text to open the dashboard and
  fix the address. The collector must be reachable from that computer on port 8080.

## Licence

MIT, see [LICENSE](LICENSE).

## Credits

Register map from Sigenergy's Modbus Protocol (V2.7–V2.9) as implemented by the
[Sigenergy Local Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus) Home Assistant
integration; Amber endpoints from Amber's public API spec.
