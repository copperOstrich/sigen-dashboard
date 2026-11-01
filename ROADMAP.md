# Roadmap

Where the dashboard is heading, in phases. Each phase ships as one or more releases with a version
bump and changelog entry; the order can change as real use shows what matters.

## Done

- **Foundations (to 1.13):** live power flow, history, strings, monthly summary and report,
  trends and alarms; car charging and the fuel comparison; money (plan comparison, payback,
  battery trades); the Settings page; backups, string health, solar limits, battery wear,
  appliance timing and Home Assistant; three grid styles; always-on use and storm warnings.
- **A. Trust the numbers (1.14–1.15):** bills set their periods exactly; Amber usage reports
  imported; supply charge by date; provisional / billed / spike tags; days before the collector
  priced hour by hour from usage reports.
- **B. Reliability (1.14–1.16):** unit and smoke tests on every push; the code split into
  `config`, `store`, `loop`, `api`; data feeds status in the System panel.
- **C. Reconcile and report (1.16):** bills against the data; a fuller monthly report; year in
  review; 12/24-hour time setting.
- **D. Battery trading insight (1.17):** spikes caught and missed, export prices achieved, what a
  higher backup reserve would do.
- **E. Smarter forecasts (1.18):** weather-aware home use in the outlook; expected cost for today
  and tomorrow; car charging plan.
- **F. Less upkeep (1.19):** bills added, edited and removed on the Settings page; files imported
  from the browser; database size, free disk and compacting; a new-version notice.

## Phase G: problems that reach you (next)

Alarms, a stopped string, a failed backup or the collector going quiet are only seen when the
dashboard is open. (Amber already sends price notifications, so this is about the system's
health, not prices.)

- [ ] Choose a channel: email (SMTP), a push service (ntfy or Pushover), or Home Assistant (already
      connected over MQTT, so HA automations could notify)
- [ ] Notify on: new alarms, a string down or low for two days, insulation faults, backups failing,
      a data feed failing for an hour, the inverter not answering, a storm warning with a low battery
- [ ] A weekly summary message (the week's solar, money, any spikes and anything that needs a look)

## Phase H: deeper insight

- [ ] Big loads recognised from the minute readings (car, air conditioning, hot water, oven), with
      their share of use and cost
- [ ] Solar health over the seasons: each string's yield against the weather model, so slow
      degradation or creeping shade shows over months, not just sudden drops
- [ ] Plan what-ifs priced on your own year: Amber against a flat or time-of-use plan *with the
      battery run for that plan*, and what more panels or more battery would add
- [ ] Self-sufficiency and money targets ("90% self-sufficient this summer") with progress

## Phase I: for other households

The repo is public; making it easier for other SigenStor owners helps it stay healthy.

- [ ] A live demo (GitHub Pages) from the simulator, so people can try it before installing
- [ ] Setup wizard on first start: inverter address, retailer, location, system cost
- [ ] More than one inverter (larger SigenStor systems) and other networks' DMO presets (VIC, QLD,
      SA, and the Victorian Default Offer)

## Parked

- **Control** (writing to the inverter, for example raising the backup reserve before a storm):
  the dashboard is read-only by design. Worth revisiting only with an explicit opt-in setting,
  Sigenergy's documented writable registers, and a way to undo.
- Reading the AC charger directly (needs it wired to the inverter over RS-485 with a Modbus address)
- Signed desktop apps, an iPhone app, automatic releases (the maintainer publishes each release)
