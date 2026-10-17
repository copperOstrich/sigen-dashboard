"""Minimal, dependency-free Modbus TCP client (function 0x04, read input registers).

Read-only by design: this module has no write functions at all, so the dashboard
can never change inverter settings or interfere with Amber / VPP control.
"""
import socket
import struct
import threading

from registers import reg_count


class ModbusError(Exception):
    pass


class ModbusException(ModbusError):
    """The device answered with a Modbus exception code (e.g. 2 = illegal address)."""

    def __init__(self, code):
        super().__init__(f"Modbus exception code {code}")
        self.code = code


class ModbusTCPClient:
    def __init__(self, host, port=502, timeout=5.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock = None
        self._tid = 0
        self._lock = threading.Lock()

    def connect(self):
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("connection closed by device")
            buf += chunk
        return buf

    def read_input_registers(self, unit, address, count):
        if count < 1 or count > 125:
            raise ValueError("count must be 1..125")
        with self._lock:
            if self._sock is None:
                self.connect()
            self._tid = (self._tid + 1) & 0xFFFF
            pdu = struct.pack(">BHH", 0x04, address, count)
            frame = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, unit) + pdu
            try:
                self._sock.sendall(frame)
                header = self._recv_exact(7)
                tid, proto, length, _unit = struct.unpack(">HHHB", header)
                body = self._recv_exact(length - 1)
            except (OSError, ModbusError):
                self.close()
                raise
            if tid != self._tid:
                self.close()
                raise ModbusError("transaction id mismatch")
            fc = body[0]
            if fc & 0x80:
                raise ModbusException(body[1])
            nbytes = body[1]
            data = body[2:2 + nbytes]
            if len(data) != count * 2:
                raise ModbusError("short response")
            return list(struct.unpack(f">{count}H", data))


def decode(dtype, regs):
    if dtype == "u16":
        return regs[0]
    if dtype == "s16":
        return regs[0] - 0x10000 if regs[0] & 0x8000 else regs[0]
    if dtype in ("u32", "s32"):
        v = (regs[0] << 16) | regs[1]
        if dtype == "s32" and v & 0x80000000:
            v -= 0x100000000
        return v
    if dtype == "u64":
        v = 0
        for r in regs:
            v = (v << 16) | r
        return v
    if dtype.startswith("str"):
        raw = b"".join(struct.pack(">H", r) for r in regs)
        return raw.split(b"\x00")[0].decode("ascii", "ignore").strip()
    raise ValueError(dtype)


def encode(dtype, value):
    """Inverse of decode - used by the simulator."""
    if dtype.startswith("str"):
        n = reg_count(dtype)
        raw = str(value).encode("ascii")[: n * 2].ljust(n * 2, b"\x00")
        return list(struct.unpack(f">{n}H", raw))
    v = int(round(value))
    n = reg_count(dtype)
    bits = 16 * n
    v &= (1 << bits) - 1
    return [(v >> (16 * (n - 1 - i))) & 0xFFFF for i in range(n)]


def build_blocks(defs, max_regs=120):
    """Group register definitions into contiguous read blocks.

    Only *exactly contiguous* definitions are merged: reading across gaps can hit
    unsupported addresses, which Sigenergy answers with exception 2 for the whole block.
    """
    items = sorted(defs, key=lambda d: d[1])
    blocks, cur = [], []
    for d in items:
        if cur:
            last = cur[-1]
            end = last[1] + reg_count(last[2])
            size = d[1] + reg_count(d[2]) - cur[0][1]
            if d[1] == end and size <= max_regs:
                cur.append(d)
                continue
            blocks.append(cur)
        cur = [d]
    if cur:
        blocks.append(cur)
    return blocks


class DeviceReader:
    """Reads a register set from one Modbus unit, remembering unsupported registers."""

    def __init__(self, client, unit, defs):
        self.client, self.unit, self.defs = client, unit, defs
        self.unsupported = set()
        self.blocks = build_blocks(defs)

    def read(self):
        values = {}
        for block in self.blocks:
            block = [d for d in block if d[0] not in self.unsupported]
            if not block:
                continue
            start = block[0][1]
            count = block[-1][1] + reg_count(block[-1][2]) - start
            try:
                regs = self.client.read_input_registers(self.unit, start, count)
                for name, addr, dtype, gain, _u in block:
                    off = addr - start
                    raw = decode(dtype, regs[off:off + reg_count(dtype)])
                    values[name] = raw if dtype.startswith("str") else raw / gain
            except ModbusException:
                # Fall back to one-by-one so a single unsupported register doesn't
                # hide the rest of the block.
                for name, addr, dtype, gain, _u in block:
                    try:
                        regs = self.client.read_input_registers(self.unit, addr, reg_count(dtype))
                        raw = decode(dtype, regs)
                        values[name] = raw if dtype.startswith("str") else raw / gain
                    except ModbusException:
                        self.unsupported.add(name)
        return values
