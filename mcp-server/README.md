# Home Energy MCP server

Lets Claude (or any app that speaks the Model Context Protocol) answer questions about your home
energy from your own dashboard: "what's the battery at?", "will it last the night?", "is now a good
time to run the dishwasher?", "cheapest way to put 30 kWh in the car by 7:30?".

It's a thin layer over the dashboard's own HTTP API. It never talks to the inverter, so it adds no
Modbus load, and it is **read-only**: it can't change settings, the battery or the charger.

## Tools

| Tool | Answers |
|---|---|
| `home_energy_now` | Live power flows, battery charge, today's totals and cost, price now, system state |
| `electricity_prices` | Amber buy/feed-in now, forecast for the next 1–24 hours, spikes, NSW wholesale |
| `battery_outlook` | When the battery hits reserve or fills, tonight's grid use and cost, tomorrow's solar |
| `best_time_to_use_power` | Whether now is a good time, the next good window, best start per appliance |
| `plan_ev_charging` | Cheapest windows to add a given kWh to the car by a given time |
| `ev_charging` | Charger state now and recent sessions with grid share and cost |
| `energy_by_day` / `energy_by_month` | Daily or monthly totals, self-sufficiency and money |
| `power_history` | Power flows over the last few hours, averaged into ≤36 steps |
| `weather_and_solar_forecast` | Weather now and expected solar per day |
| `battery_health` | State of health, cycles, round-trip efficiency, warranty tracking |
| `system_health` | Inverter state, alarms, insulation, feeds, solar string health |
| `money_summary` | Export/import value, spikes caught or missed, plan comparisons |
| `dashboard_api` | Any other read-only `/api/...` endpoint (settings and backups excluded) |

## Running it on the QNAP

The image is `ghcr.io/copperostrich/sigen-dashboard-mcp:latest`, or pin a version such as `:2.4.0`.
It shares the dashboard's version number, so use the same tag for both. If the dashboard has its own IP
address, use `qnap/docker-compose.own-ip-with-mcp.yml`, which runs both together on one network.

If the dashboard runs the ordinary way (port 8088 on the NAS), add this to its application instead:

```yaml
  sigen-mcp:
    image: ghcr.io/copperostrich/sigen-dashboard-mcp:latest
    container_name: sigen-mcp
    restart: unless-stopped
    ports:
      - "8765:8000"
    environment:
      - DASHBOARD_URL=http://sigen-dashboard:8080
      - MCP_TOKEN=change-me-to-a-long-random-secret
      - TZ=Australia/Sydney
```

Settings:

| Variable | Meaning |
|---|---|
| `MCP_TOKEN` | Required. A long random secret; clients send it as `Authorization: Bearer <token>` |
| `DASHBOARD_URL` | Where the dashboard is, e.g. `http://192.168.1.50` |
| `MCP_ALLOWED_HOSTS` | Optional. Addresses clients use to reach the server, e.g. `192.168.1.51:8000,homeenergy-mcp.internal:8000`; others are refused (guards against DNS rebinding) |
| `MCP_PORT` | Default `8000` |
| `TZ` | For readable times. Default `Australia/Sydney` |

Check it's up: `http://<address>:8000/health` should show `{"ok": true, "version": ...}`,
with the same version as the dashboard.
Everything else needs the token.

## Connecting Claude Desktop

Claude Desktop reaches the server from your computer, so it works at home and away (over Teleport,
Tailscale or another VPN). Don't port-forward it to the internet.

Claude's web and mobile apps and "custom connectors" connect from Anthropic's servers, which can't
see your home network, so use the Desktop app's config file with the `mcp-remote` bridge. It needs
Node.js on the computer.

In Claude Desktop: Settings → Developer → Edit Config, and add:

```json
{
  "mcpServers": {
    "home-energy": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://homeenergy-mcp.internal:8000/mcp",
               "--allow-http", "--header", "Authorization:${AUTH}"],
      "env": { "AUTH": "Bearer change-me-to-a-long-random-secret" }
    }
  }
}
```

Restart Claude Desktop and ask "what's my battery at?". (`--allow-http` is needed because the
server is plain HTTP on your own network; the token goes inside your LAN or VPN only.)

Claude Code:

```sh
claude mcp add --transport http home-energy http://homeenergy-mcp.internal:8000/mcp \
  --header "Authorization: Bearer change-me-to-a-long-random-secret"
```

## Tests

`tests/test_mcp.py` (run in CI with the rest) checks each tool against a fake dashboard, the token
gate, a real MCP session over HTTP, and every tool against the collector on its simulator:

```sh
pip install "mcp==2.3.*"
python -m unittest tests.test_mcp -v
```

## Trying it without a NAS

```sh
pip install "mcp==2.3.*"
DASHBOARD_URL=http://homeenergy.internal MCP_TOKEN=a-long-test-token-123 python energy_mcp.py
```

Or run it over stdio next to Claude Desktop with `python energy_mcp.py stdio`
(no token needed, since nothing listens on the network).
