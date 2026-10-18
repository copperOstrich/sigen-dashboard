# Changelog

Release notes for each tagged version. The desktop app offers the latest release as an
update; the Docker image is tagged with the same version.

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
