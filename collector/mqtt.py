"""Publish the live figures to an MQTT broker, with Home Assistant discovery.

Standard library only: a small MQTT 3.1.1 client (QoS 0, retained messages, a last will so Home
Assistant shows the sensors unavailable when the collector stops). Every MQTT_INTERVAL seconds it
publishes one JSON message to <MQTT_TOPIC>/state; with discovery on, Home Assistant creates a
"Home Energy" device with a sensor for each figure, including lifetime energy totals that suit
its Energy dashboard.

Settings: MQTT_HOST (turns it on), MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TOPIC,
MQTT_DISCOVERY_PREFIX ("homeassistant"; blank to skip discovery), MQTT_INTERVAL.
"""
import json
import socket
import struct
import threading
import time

# key, name, unit, device_class, state_class, path into the live state (or ("raw", register))
SENSORS = [
    ("pv_power", "Solar power", "W", "power", "measurement", ("power", "pv")),
    ("home_power", "Home power", "W", "power", "measurement", ("power", "load")),
    ("battery_power", "Battery power", "W", "power", "measurement", ("power", "battery")),
    ("grid_power", "Grid power", "W", "power", "measurement", ("power", "grid")),
    ("car_power", "Car charging power", "W", "power", "measurement", ("power", "ev")),
    ("battery_soc", "Battery charge", "%", "battery", "measurement", ("battery", "soc")),
    ("battery_soh", "Battery health", "%", None, "measurement", ("battery", "soh")),
    ("battery_available", "Battery energy available", "kWh", "energy_storage", "measurement", ("battery", "available_kwh")),
    ("battery_temp", "Battery temperature", "°C", "temperature", "measurement", ("battery", "cell_temp_avg")),
    ("today_solar", "Solar today", "kWh", "energy", "total_increasing", ("today", "pv")),
    ("today_home", "Home use today", "kWh", "energy", "total_increasing", ("today", "load")),
    ("today_bought", "Bought today", "kWh", "energy", "total_increasing", ("today", "import")),
    ("today_sold", "Sold today", "kWh", "energy", "total_increasing", ("today", "export")),
    ("today_car", "Car today", "kWh", "energy", "total_increasing", ("today", "ev")),
    ("self_sufficiency", "Self-sufficiency today", "%", None, "measurement", ("today", "self_sufficiency")),
    ("buy_price", "Buy price", "c/kWh", None, "measurement", ("price", "buy")),
    ("feed_in_price", "Feed-in price", "c/kWh", None, "measurement", ("price", "sell")),
    ("solar_total", "Solar lifetime", "kWh", "energy", "total_increasing", ("raw", "total_pv_kwh")),
    ("home_total", "Home use lifetime", "kWh", "energy", "total_increasing", ("raw", "total_load_kwh")),
    ("bought_total", "Bought lifetime", "kWh", "energy", "total_increasing", ("raw", "total_import_kwh")),
    ("sold_total", "Sold lifetime", "kWh", "energy", "total_increasing", ("raw", "total_export_kwh")),
    ("battery_in_total", "Battery charged lifetime", "kWh", "energy", "total_increasing", ("raw", "total_charge_kwh")),
    ("battery_out_total", "Battery discharged lifetime", "kWh", "energy", "total_increasing", ("raw", "total_discharge_kwh")),
]


def _str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _packet(kind, body):
    n, rem = len(body), b""
    while True:
        byte, n = n % 128, n // 128
        rem += bytes([byte | (0x80 if n else 0)])
        if not n:
            break
    return bytes([kind]) + rem + body


class Mqtt:
    def __init__(self, host, port=1883, username="", password="", topic="sigen-dashboard",
                 discovery="homeassistant", interval=10, get_state=None, version="", log=print):
        self.host, self.port = host, int(port or 1883)
        self.username, self.password = username or "", password or ""
        self.topic = (topic or "sigen-dashboard").strip("/")
        self.discovery = (discovery or "").strip("/")
        self.interval = max(2, float(interval or 10))
        self.get_state, self.version, self.log = get_state, version, log
        self.sock = None
        self.status, self.error, self.sent, self.connected_at = "starting", None, 0, None
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- protocol
    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=10)
        flags = 0x02 | 0x04 | 0x20  # clean session, will (retained "offline")
        payload = _str(f"sigen-dashboard-{socket.gethostname()}"[:23]) + _str(f"{self.topic}/status") + _str("offline")
        if self.username:
            flags |= 0x80
            payload += _str(self.username)
            if self.password:
                flags |= 0x40
                payload += _str(self.password)
        body = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", 60) + payload
        s.sendall(_packet(0x10, body))
        ack = self._read(s, 4)
        if ack[0] != 0x20:
            raise ConnectionError("the broker didn't answer like an MQTT broker")
        rc = ack[3]
        if rc:
            raise ConnectionError({1: "protocol version refused", 2: "client id refused", 3: "broker unavailable",
                                   4: "wrong user name or password", 5: "not authorised"}.get(rc, f"refused ({rc})"))
        s.settimeout(10)
        self.sock = s

    @staticmethod
    def _read(s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("the broker closed the connection")
            buf += chunk
        return buf

    def publish(self, topic, payload, retain=False):
        body = _str(topic) + (payload if isinstance(payload, bytes) else str(payload).encode("utf-8"))
        self.sock.sendall(_packet(0x30 | (0x01 if retain else 0), body))

    def _drain(self):
        """Throw away anything the broker sent (ping replies), so its buffer never fills."""
        self.sock.setblocking(False)
        try:
            while self.sock.recv(4096):
                pass
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            self.sock.settimeout(10)

    # ---------------------------------------------------------------- content
    def values(self):
        live, raw = self.get_state()
        out = {}
        for key, _n, _u, _d, _s, path in SENSORS:
            v = (raw or {}).get(path[1]) if path[0] == "raw" else ((live or {}).get(path[0]) or {}).get(path[1])
            if isinstance(v, (int, float)):
                out[key] = round(v, 3)
        return out

    def _discover(self):
        if not self.discovery:
            return
        node = "sigen_dashboard"
        dev = {"identifiers": [node], "name": "Home Energy", "manufacturer": "Sigenergy (via sigen-dashboard)",
               "model": "SigenStor", "sw_version": self.version}
        for key, name, unit, dclass, sclass, _p in SENSORS:
            cfg = {"name": name, "unique_id": f"{node}_{key}", "object_id": f"home_energy_{key}",
                   "state_topic": f"{self.topic}/state", "value_template": f"{{{{ value_json.{key} }}}}",
                   "availability_topic": f"{self.topic}/status", "unit_of_measurement": unit, "device": dev}
            if dclass:
                cfg["device_class"] = dclass
            if sclass:
                cfg["state_class"] = sclass
            self.publish(f"{self.discovery}/sensor/{node}/{key}/config", json.dumps(cfg), retain=True)

    # ---------------------------------------------------------------- loop
    def run(self):
        wait = 5
        while True:
            try:
                self._connect()
                self.publish(f"{self.topic}/status", "online", retain=True)
                self._discover()
                self.status, self.error, self.connected_at, wait = "connected", None, time.time(), 5
                self.log(f"MQTT: connected to {self.host}:{self.port}, publishing to {self.topic}/state")
                last_ping = time.time()
                while True:
                    self.publish(f"{self.topic}/state", json.dumps(self.values()), retain=True)
                    self.sent += 1
                    if time.time() - last_ping >= 30:
                        self.sock.sendall(b"\xc0\x00")  # PINGREQ keeps the connection alive
                        last_ping = time.time()
                    self._drain()
                    time.sleep(self.interval)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}" if not isinstance(e, ConnectionError) else str(e)
                if msg != self.error:
                    self.log(f"MQTT: {msg}; retrying")
                self.status, self.error = "error", msg
                try:
                    if self.sock:
                        self.sock.close()
                except OSError:
                    pass
                self.sock = None
                time.sleep(wait)
                wait = min(300, wait * 2)

    def summary(self):
        return {"enabled": True, "status": self.status, "error": self.error, "topic": f"{self.topic}/state",
                "sent": self.sent, "discovery": bool(self.discovery)}
