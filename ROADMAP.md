# Roadmap

Where the dashboard is heading, in phases. Each phase ships as one or more releases with a version
bump and changelog entry; the order can change as real use shows what matters.

## Done so far

Live power flow, history, strings, monthly summary and report, trends and alarms (to 1.8);
car charging and the fuel comparison; money (plan comparison, payback, battery trades); the
Settings page (1.10); backups, string health, solar limits, battery wear, appliance timing and
Home Assistant (1.11); three grid styles (1.12); always-on use and storm warnings (1.13).

## Phase A: trust the numbers (1.14)

The money figures should match the retailer's bills to the cent and say when they might still move.

- [x] Bills set their periods' costs exactly (scaled per day so each period adds up to the bill)
- [x] Amber usage reports (the 5-minute CSV from the app or monthly email) imported as exact
      daily costs, and not replaced by Amber's API figures
- [x] Supply charge by date, so months before a price change use the rate then
- [x] Days before the collector ran priced hour by hour from Amber usage reports (1.15)
- [x] "provisional", "billed" and "spike" tags in the Money panel's months, and price-spike days
      listed with what they earned

## Phase B: reliability (1.14 to 1.16)

Catch regressions before they reach the NAS, and keep the code easy to change.

- [x] Unit tests for the parts with rules in them: settings checks, importers (mySigen, bills,
      Amber usage), bill scaling, dated rates, export-limit learning, appliance timing, MQTT
      packets (`python -m unittest discover -s tests`)
- [x] A smoke test that starts the collector on the simulator and checks every page and API
      endpoint answers without errors
- [x] Both run on every push (GitHub Actions, "Tests")
- [x] Database code split out of `sigen_collector.py` into `store.py` (1.15.1)
- [x] HTTP API, collector loop and settings split out too: `api.py`, `loop.py`, `config.py` (1.16)
- [x] Data feeds in the System panel: each feed's status, last update and errors, backup age, disk space (1.15)

## Phase C: reconcile and report (1.16)

- [x] "Your bills against the data": each bill beside what the data had for its period (1.16)
- [x] The printable monthly report gains solar held back, price spikes, always-on use and battery cycles (1.16)
- [x] Year in review page: production, self-sufficiency, money, payback and highlights (1.16)

## Phase D: battery trading insight (1.17)

How well the battery is being traded (Amber SmartShift or a VPP), not just what it earned.

- [x] Spikes caught against spikes missed, and why (1.17)
- [x] Battery export price achieved against each day's best hour (1.17)
- [x] What a higher backup reserve would have cost, earned and added in outage cover (1.17)

## Phase E: smarter forecasts (1.18)

- [x] Home use in the battery outlook learned against temperature (1.18)
- [x] Expected cost for the rest of today and tomorrow (1.18)
- [x] Car charging plan: kWh needed and leaving time, cheapest times before then (1.18)

## Parked

Notifications (Amber already sends them), signed desktop apps, an iPhone app, reading the AC
charger directly (needs it wired to the inverter over RS-485 with a Modbus address), automatic
releases (the maintainer publishes each release).
