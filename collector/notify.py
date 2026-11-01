"""Phone alerts through ntfy (https://ntfy.sh): free, no account, an app for iPhone and Android.

The collector posts a short message to a topic; the ntfy app, subscribed to that topic, shows it as
a normal app notification (through Apple's or Google's push service). It isn't SMS. The topic
name is the only key, so the dashboard makes a long random one. Messages carry the system's
status only: no address, location, account numbers or links to the dashboard.

What's watched (each can be turned off with NOTIFY_ON):
  alarms      new inverter, battery or charger alarms
  strings     a solar string down or well below its usual output (the string health check)
  insulation  insulation resistance below the warning or alarm level
  inverter    the inverter not answering for 10 minutes
  backups     a nightly backup failing or overdue; the disk getting full
  feeds       prices, AEMO or weather failing for an hour
  storm       thunderstorms forecast while the battery is expected to be under half full
Each problem is sent once when it starts and once when it clears (quietly). What was sent is kept
in notify-state.json, so a restart (saving a setting) doesn't send everything again.
"""
import json
import secrets
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

KINDS = ("alarms", "strings", "insulation", "inverter", "backups", "feeds", "storm")
WAIT = {"alarms": 120, "strings": 0, "insulation": 120, "inverter": 600, "backups": 0, "feeds": 3600, "storm": 0}
RESEND_S = 3600          # a problem that clears and comes back within the hour isn't sent again
STORM_LOW = 50           # % expected when the storm arrives
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def new_topic():
    """A topic nobody will guess: 'home-energy-' and 20 random letters and digits."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "home-energy-" + "".join(secrets.choice(alphabet) for _ in range(20))


def parse_kinds(value):
    v = (value or "").strip().lower()
    if v in ("", "all"):
        return set(KINDS)
    if v in ("none", "off"):
        return set()
    return {k.strip() for k in v.split(",") if k.strip() in KINDS}


def parse_weekly(value):
    """'sun 18:00' -> (6, 18, 0); off or blank -> None."""
    v = (value or "").strip().lower()
    if v in ("", "off", "no", "0"):
        return None
    parts = v.replace(",", " ").split()
    if len(parts) != 2 or parts[0][:3] not in DAYS or ":" not in parts[1]:
        raise ValueError("A day and time, like sun 18:00, or off")
    h, m = parts[1].split(":", 1)
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("A day and time, like sun 18:00, or off")
    return DAYS.index(parts[0][:3]), h, m


def parse_quiet(value):
    """'22:00-07:00' -> (1320, 420) minutes; blank -> None."""
    v = (value or "").strip()
    if not v or v.lower() == "off":
        return None
    try:
        a, b = v.split("-")
        mins = []
        for x in (a, b):
            h, m = x.strip().split(":")
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            mins.append(h * 60 + m)
        return tuple(mins)
    except ValueError:
        raise ValueError("Two times, like 22:00-07:00, or blank") from None


def in_quiet(quiet, dt):
    if not quiet:
        return False
    a, b, m = quiet[0], quiet[1], dt.hour * 60 + dt.minute
    return a <= m < b if a < b else (m >= a or m < b)


def conditions(state, now, kinds, storm_low=STORM_LOW):
    """{key: {kind, title, message, priority, tags}} for every problem in a collector snapshot."""
    out = {}
    if not state:
        return out
    if "inverter" in kinds and state.get("status") == "error" and state.get("error_kind") == "connection":
        out["inverter"] = {"kind": "inverter", "title": "The inverter isn't answering",
                           "message": "The dashboard can't read the inverter. Check that the inverter and the network "
                                      "are on; the dashboard keeps trying.", "priority": 4, "tags": ["electric_plug"]}
    for a in state.get("alarms") or []:
        reg = str(a.get("register") or "")
        kind = "strings" if reg.startswith("string") else "insulation" if reg.startswith("insulation") else "alarms"
        if kind not in kinds:
            continue
        fault = a.get("severity") == "fault"
        key = f"{kind}:{a.get('source')}:{reg}:{a.get('bit')}:{a.get('severity')}"
        out[key] = {"kind": kind, "title": ("Fault: " if fault else "Warning: ") + (a.get("source") or "System"),
                    "message": a.get("message") or "An alarm is showing on the dashboard.", "priority": 4 if fault else 3,
                    "tags": ["rotating_light" if fault else "warning"]}
    feeds = (state.get("system") or {}).get("feeds") or []
    for f in feeds:
        name, st = f.get("name") or "", (f.get("status") or "").lower()
        if name == "Backups":
            if "backups" in kinds and st in ("error", "stale"):
                out["backups"] = {"kind": "backups", "title": "Backups need a look",
                                  "message": f"The nightly backup {'failed' if st == 'error' else 'is overdue'}"
                                             + (f": {f['error']}" if f.get("error") else "") + ".",
                                  "priority": 3, "tags": ["floppy_disk"]}
        elif name == "Disk space":
            if "backups" in kinds and st == "warn":
                out["disk"] = {"kind": "backups", "title": "The disk is nearly full",
                               "message": f"Only {f.get('what')} where the dashboard keeps its data.", "priority": 3,
                               "tags": ["floppy_disk"]}
        elif name not in ("Inverter", "Home Assistant", "Phone alerts") and "feeds" in kinds and st in ("error", "stale"):
            out["feed:" + name] = {"kind": "feeds", "title": f"{name} isn't updating",
                                   "message": f"{name} ({f.get('what') or 'data'}) has been failing"
                                              + (f": {f['error']}" if f.get("error") else "") + ".",
                                   "priority": 2, "tags": ["satellite"]}
    s = state.get("storm")
    if "storm" in kinds and s and not s.get("now") and s.get("soc") is not None and s["soc"] < storm_low:
        at = datetime.fromtimestamp(s["at"], (state.get("_tz") or None))
        out[f"storm:{s['at']}"] = {"kind": "storm", "title": "Storm coming, battery low",
                                   "message": f"{s.get('desc') or 'Thunderstorms'} forecast from {_when(at, state)}. "
                                              f"The battery should only be about {round(s['soc'])}% by then, so there's "
                                              "less in reserve if the power goes out.", "priority": 3,
                                   "tags": ["cloud_with_lightning"]}
    return out


def _when(dt, state):
    h = dt.hour % 12 or 12
    clock = f"{dt:%H:%M}" if state.get("_time_format") == "24h" else f"{h}:{dt:%M} {'am' if dt.hour < 12 else 'pm'}"
    return f"{dt:%a} {clock}"


class Notifier:
    def __init__(self, topic, server="https://ntfy.sh", token="", kinds=KINDS, weekly=None, quiet=None,
                 get_state=None, summary=None, state_file=None, tz=None, time_format="12h", log=print, post=None):
        self.topic, self.server, self.token = (topic or "").strip(), (server or "https://ntfy.sh").rstrip("/"), token or ""
        self.kinds, self.weekly, self.quiet = set(kinds), weekly, quiet
        self.get_state, self.summary, self.tz, self.time_format, self.log = get_state, summary, tz, time_format, log
        self._post = post or self._http
        self.file = Path(state_file) if state_file else None
        self.seen = {}           # key -> first time seen (this run)
        self.mem = self._load()  # {"sent": {key: {"at", "title"}}, "weekly": "YYYY-Www", "last": {...}}
        self.status = {"status": "ok" if self.enabled else "off", "sent": 0, "error": None,
                       "last": self.mem.get("last")}
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.topic)

    # ---------------------------------------------------------------- state file
    def _load(self):
        try:
            d = json.loads(self.file.read_text()) if self.file and self.file.exists() else {}
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        if not self.file:
            return
        try:
            tmp = self.file.with_name(self.file.name + ".tmp")
            tmp.write_text(json.dumps(self.mem))
            tmp.replace(self.file)
        except OSError as e:
            self.log(f"Phone alerts: couldn't save {self.file.name}: {e}")

    # ---------------------------------------------------------------- sending
    def _http(self, payload):
        req = urllib.request.Request(self.server, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "sigen-dashboard"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status

    def send(self, title, message, priority=3, tags=None, now=None):
        """Post one message. Returns (ok, error)."""
        if not self.enabled:
            return False, "No topic set"
        now = now or time.time()
        if priority <= 3 and self.tz and in_quiet(self.quiet, datetime.fromtimestamp(now, self.tz)):
            priority = 2  # quiet hours: arrives without a sound
        payload = {"topic": self.topic, "title": "Home energy: " + title, "message": message,
                   "priority": int(priority), "tags": list(tags or [])}
        try:
            self._post(payload)
        except Exception as e:  # offline, wrong server: try the next one, show it in Data feeds
            err = f"{type(e).__name__}: {e}"[:160]
            with self._lock:
                self.status.update(status="error", error=err)
            self.log(f"Phone alerts: couldn't send '{title}': {err}")
            return False, err
        with self._lock:
            self.status["sent"] += 1
            self.status.update(status="ok", error=None, last={"at": int(now), "title": title})
            self.mem["last"] = self.status["last"]
        return True, None

    def test(self):
        return self.send("test message", "Alerts from your home energy dashboard will arrive like this.",
                         priority=3, tags=["white_check_mark"])

    # ---------------------------------------------------------------- watching
    def tick(self, now=None):
        if not self.enabled:
            return
        now = now or time.time()
        state = self.get_state() if self.get_state else {}
        state = dict(state or {}, _tz=self.tz, _time_format=self.time_format)
        conds = conditions(state, now, self.kinds)
        sent = self.mem.setdefault("sent", {})
        for k in [k for k in self.seen if k not in conds]:
            del self.seen[k]
        changed = False
        for key, c in conds.items():
            first = self.seen.setdefault(key, now)
            if key in sent and sent[key].get("open"):
                continue
            if now - first < WAIT.get(c["kind"], 0):
                continue
            if key in sent and now - sent[key].get("at", 0) < RESEND_S:
                sent[key]["open"] = True  # came straight back: treat as still the same problem
                changed = True
                continue
            ok, _ = self.send(c["title"], c["message"], c["priority"], c["tags"], now)
            if ok:
                sent[key] = {"at": int(now), "title": c["title"], "open": True}
                changed = True
        for key in [k for k, v in sent.items() if v.get("open") and k not in conds]:
            # cleared (storms just expire; no message for those)
            if not key.startswith("storm:"):
                title = sent[key].get("title", "Problem")
                self.send("Cleared: " + title.split(": ", 1)[-1], f"Back to normal ({title.lower()}).", 2,
                          ["white_check_mark"], now)
            sent[key]["open"] = False
            changed = True
        for key in [k for k, v in sent.items() if not v.get("open") and now - v.get("at", 0) > 7 * 86400]:
            del sent[key]  # forget old ones
            changed = True
        if self.weekly and self.summary and self.tz:
            changed |= self._weekly(now)
        if changed:
            self._save()

    def _weekly(self, now):
        dt = datetime.fromtimestamp(now, self.tz)
        day, h, m = self.weekly
        if dt.weekday() != day or (dt.hour, dt.minute) < (h, m) or (dt.hour, dt.minute) > (h + 2, m):
            return False
        week = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
        if self.mem.get("weekly") == week:
            return False
        try:
            title, text = self.summary()
        except Exception as e:
            self.log(f"Phone alerts: weekly summary failed: {type(e).__name__}: {e}")
            title, text = "Weekly summary", "The summary couldn't be put together this week; see the dashboard."
        open_items = [v["title"] for v in self.mem.get("sent", {}).values() if v.get("open")]
        if open_items:
            text += "\nStill needs a look: " + "; ".join(open_items) + "."
        ok, _ = self.send(title, text, 2, ["sunny"], now)
        if ok:
            self.mem["weekly"] = week
        return ok

    def run(self, every=30):
        while True:
            try:
                self.tick()
            except Exception as e:  # never stop the collector for alerts
                self.log(f"Phone alerts: {type(e).__name__}: {e}")
            time.sleep(every)

    def snapshot(self):
        with self._lock:
            out = dict(self.status)
        out.update(enabled=self.enabled, server=self.server, kinds=sorted(self.kinds),
                   open=[v["title"] for v in self.mem.get("sent", {}).values() if v.get("open")])
        return out


def weekly_summary(store, tz, now=None, trading_fn=None):
    """('Week to Sunday 2 Nov', text) from the last seven full days."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
    days = [d for d in store.daily(8) if d.get("day") and d["day"] < today][-7:]
    if not days:
        return "Weekly summary", "No full days recorded this week."
    tot = lambda k: sum(d.get(k) or 0 for d in days)
    pv, load, imp, exp = tot("pv"), tot("load") + tot("ev"), tot("import"), tot("export")
    lines = [f"Solar {pv:.0f} kWh, home and car used {load:.0f} kWh, bought {imp:.0f} kWh, sold {exp:.0f} kWh."]
    if load > 0:
        lines.append(f"{max(0, min(100, round((1 - imp / load) * 100)))}% of your use came from solar and the battery.")
    costs = [d.get("cost") for d in days if d.get("cost") is not None]
    if costs:
        c = sum(costs)
        lines.append(("Energy cost" if c >= 0 else "Energy earned") + f" ${abs(c):.2f}"
                     + (f" ({len(costs)} of {len(days)} days priced)" if len(costs) < len(days) else "") + ".")
    best = max(days, key=lambda d: d.get("pv") or 0)
    lines.append(f"Best solar day: {datetime.fromisoformat(best['day']):%A} with {best.get('pv') or 0:.1f} kWh.")
    if trading_fn:
        try:
            t = trading_fn()
            if t.get("spike_count"):
                lines.append(f"Price spikes: {t['spike_count']}, caught {t['caught']}, earning ${t['spike_earned']:.2f}.")
        except Exception:
            pass
    end = datetime.fromisoformat(days[-1]["day"])
    return f"Week to {end:%A} {end.day} {end:%b}", "\n".join(lines)
