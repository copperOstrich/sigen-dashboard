# Changelog

Release notes for each tagged version. The desktop app offers the latest release as an
update; the Docker image is tagged with the same version.

## 1.6.3

- **The grid: all regions at a glance.** A map of every NEM region (NSW, Queensland,
  Victoria, South Australia, Tasmania) with its wholesale price, demand, renewable share and
  whether it's importing or exporting, arrows for the flows between regions, and
  whole-market demand, renewable share and average price. A table on phones. Uses the same
  AEMO requests as before; `/api/aemo` adds `regions` and `nem`.
- reMarkable sleep screen away from home: after 3 hours without reaching the collector
  (`AWAY_HOURS`), the tablet shows an "Away from home" screen instead of old figures; the
  live figures come back with the next refresh at home (`/eink.png?away=1` draws it)
- The tablet only uses a real image, so a Wi-Fi login page on a hotel or cafe network, or
  another device at the same address, can't replace the sleep screen
- reMarkable uninstaller: fixed a harmless "head: invalid option" message on the tablet
  (BusyBox's `head` needs `-n 1`)

## 1.6.2

- reMarkable sleep screen: the tablet adds the owner name and number itself, on top of the
  image, so the layout now leaves that strip clear instead of drawing them again
  (`/eink.png?rm=1`); run the installer again to switch
- Fixed the heading not showing on the tablet: the installer's text encoding didn't work with
  the tablet's built-in tools

## 1.6.1

- reMarkable sleep screen: keeps the "Paper tablet is sleeping" heading and can show the
  owner's name and phone number at the bottom, like reMarkable's own sleep screen
  (`OWNER_NAME`, `OWNER_PHONE` and `SLEEP_TITLE` when installing, remembered on the tablet;
  or `?title=&owner=&phone=` on `/eink.png`)

## 1.6.0

### Monthly summary
- New **Monthly summary** panel: solar made, home and car use, bought and sold, self-sufficiency,
  net cost and the value of your solar and battery for any month, with the change on the same
  month last year (or the month before), a 12-month chart and table
- **Printable report** for any month (`report.html`): totals, a comparison with the previous
  month and the same month last year, and every day of the month; print it or save it as a PDF
- Monthly figures as CSV (`/api/monthly?format=csv`)

### Trends
- New **Trends** panel over 3 months, a year or everything recorded:
  - **Strings**: each string's share of the solar, averaged over a week, to spot a string
    slowly losing output (growing shade, dirt, a failing panel)
  - **Insulation**: lowest and average reading each day against the warning and alarm levels
  - **Battery health**: state of health as the battery reports it
  - **Temperatures**: hottest battery and inverter temperature each day
- The collector now keeps daily figures for insulation, battery health, temperatures and grid
  voltage; these trends build up from the day you install 1.6.0. String trends use the
  history already recorded.
- Long charts label months instead of days

### E-ink display (reMarkable 2)
- The collector draws an e-ink version of the dashboard at `/eink.png` (1404 x 1872 for the
  reMarkable 2 by default; `?w=&h=` for other screens): the four flows, battery, outlook,
  a chart of the buy, sell and AEMO wholesale prices for the next two hours, today's totals
  and the solar forecast in high-contrast greyscale
- A one-command installer makes it a reMarkable 2's sleep screen, refreshed every 5 minutes
  while the tablet is awake, and an uninstaller puts everything back
- The Docker image now includes Pillow, used only to draw this image

### Desktop app
- The macOS app is withdrawn until it can be signed with an Apple developer certificate;
  unsigned apps won't open on current macOS. Use the dashboard in a browser on a Mac.

## 1.5.0

### Battery outlook
- New **Battery outlook** panel: where the battery's charge is heading until the end of
  tomorrow, drawn on from the last 12 hours, with expected solar and usual home use below
- Plain answers: whether the battery lasts the night (or when it reaches its reserve), how much
  the house will need from the grid until the sun takes over and what that costs at the
  forecast prices, when solar fills it again and how much is left over
- Advice on grid charging: when the next day's solar will refill the battery anyway, it says
  charging overnight isn't needed; when it won't, it points to the cheapest window
- Home use is your typical use for each hour over the last two weeks, weekdays and weekends
  separately (car charging left out); solar comes from the weather forecast when it's set up,
  otherwise from recent days. It notes when VPP or retailer control may do things differently.
- `/api/outlook` returns the projection and the figures behind it

## 1.4.1

- macOS: the app is now ad-hoc signed, so macOS offers **Open Anyway** (System Settings →
  Privacy & Security) instead of saying the app "is damaged and can't be opened"

## 1.4.0

### Weather and solar forecast
- New **Weather and solar forecast** panel: current conditions, sunrise and sunset, and four
  days of forecast from Open-Meteo (free, no account)
- Solar production forecast in kWh for today, tomorrow and the next two days, plus "still to
  come" today, and an hour-by-hour chart of forecast against what the panels actually made
- The forecast uses the sunshine expected on your own roof faces (`SOLAR_PLANES`: tilt,
  direction and size of each) and then learns from your system, comparing the last two weeks
  of forecasts with real production, so it allows for losses, shading and clipping
- Location from `SITE_LAT`/`SITE_LON`, or `SITE_ADDRESS` looked up once and cached

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
