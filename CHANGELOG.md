# Changelog

Release notes for each tagged version. The desktop app offers the latest release as an
update; the Docker image is tagged with the same version.

## 1.3.1

- **Insulation resistance check** (PV wiring to earth). Healthy systems usually read about
  0.5 to 0.8 MΩ, and the inverter protects itself below about 0.37 MΩ. Below 0.5 MΩ shows an
  amber "lower than usual" notice; below 0.37 MΩ a red alarm. Both are logged as events, with
  another when it recovers, and need three readings in a row so one odd reading doesn't
  trigger them. Limits: `INSULATION_WARN_MOHM` and `INSULATION_MIN_MOHM`.
- **Arrange panels**: choose which dashboard panels show, their order (drag, or the arrows)
  and their width. Saved in each browser, so a phone and a computer can differ.
- `SIGEN_CHARGER_UNIT=plant` skips the charger search for chargers without a Modbus address
- **Car charging without the charger on Modbus.** Most installs don't give the Sigen AC
  charger a Modbus address, so it can't be read directly. The inverter still reports it, so
  the dashboard now works out charging from the inverter's own figures: live charging power,
  charging sessions with energy, grid share and cost, and the car in the power flow, charts
  and daily totals. Plugged-in-but-not-charging and charger alarms need the charger itself.
- Version shown at the bottom of the dashboard (collector, and the desktop app when used there)
- Battery cell voltages that the inverter doesn't report are no longer shown as 65.535 V
- AC charger search waits longer for each Modbus id (3 s, or `SIGEN_CHARGER_PROBE_TIMEOUT`),
  since the inverter relays requests to a wired charger
- Accepts chargers whose firmware reports only a power or only a current rating
- When no charger is found, the log and `/api/debug` list every id that answered and what it
  returned, and `/api/debug` shows the plant's total energy sent to an AC charger
  (`total_evac_kwh`), which tells you whether the inverter knows about a charger at all

## 1.3.0

### Car charging (Sigen AC charger)
- Finds a Sigen EVAC AC charger on the system automatically (or set `SIGEN_CHARGER_UNIT`)
  and reads it over the same Modbus TCP connection, read-only
- The car in the live power flow, with its own line on the power chart and its own
  category in daily energy and today's totals; home figures now leave the car out
- Car charging panel: the charger's state in plain words, power, the current offered to
  the car, this session's energy, grid energy and cost, a 48-hour plug-in and charging
  timeline, and recent sessions
- Plug-in, charging, unplug and fault events in the event log, including a note when power
  has been offered for 10 minutes and the car hasn't started charging
- Charging sessions and the full state log (IEC 61851 states) as CSV downloads
- The desktop widget shows when the car is plugged in or charging
- Simulator includes a charger and a car so the panel can be tried without one

### Charts
- Solar strings: a **Daylight** view (now the default) that shows sunrise to sunset only.
  Today runs to the expected sunset; 3 and 7 days fold the nights away so each day's curves
  sit side by side. Sunrise and sunset come from when the strings start and stop producing,
  so they follow the seasons. **All hours** brings back the full timeline.
- Power chart: a **Today** range, from midnight to now

## 1.2.0

The first public release.

### Dashboard
- Live power flow between solar, battery, home and grid, with a plain-language summary
- Battery detail: charge, usable energy, time to full or empty, health, cell temperatures
  and voltages, reserve and charge limits
- Today's totals, self-sufficiency and net cost
- Power and battery history from 24 hours to 30 days, with a crosshair tooltip
- Daily energy for the last 30 days, including an **All** view that balances energy in
  (solar, bought, from battery) against energy out (home, sold, to battery), plus a cost view
- Solar strings chart: power, voltage and current for every PV string the inverter reports
  (1 to 36), with each string's energy, share, peak and peak time for the day
- System status, alarms decoded from Sigenergy's Modbus protocol, PV strings and an event log
- Light and dark mode, and a layout that works on a phone

### Prices and the grid
- **Amber Electric**: live buy and sell prices, 24-hour forecast, spike alerts, "cheapest
  2 hours ahead", and daily cost from Amber's metered data
- **LocalVolts**: the same, using LocalVolts' marginal import and export prices and
  metered costs
- **AEMO** (no account needed): 5-minute wholesale price and forecast for your region,
  demand, generation mix and renewable share, interconnector flows, price-cap flags

### Desktop app (Windows and macOS)
- Always-on-top compact widget and full dashboard window, with one-click switching
- Lives in the tray / menu bar; start at login; keep-on-top toggle
- Checks for new releases and offers to download and install them

### Collector
- Reads the SigenStor over Modbus TCP, strictly read-only; Python standard library only
- Built-in simulator so everything can be tried before Modbus TCP is enabled
- Minute-level history in SQLite for about two years; per-string PV logging with CSV export
- Settings from environment variables or a `sigen.env` file in the data folder
- Multi-arch Docker image (amd64, arm64) and a ready-made QNAP Container Station setup
- Stops cleanly when the container is stopped
- `/api/debug` diagnostics; unsupported alarm registers on some firmware are ignored
  instead of showing false faults
