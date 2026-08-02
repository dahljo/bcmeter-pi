"""Read-only Modbus TCP server for SCADA/BMS integration (e.g. fire+).

Serves a 32-register snapshot of the live measurement state on port 502
(fallback 1502 when binding 502 needs privileges), function codes 3 and 4,
big-endian word order.  Disabled by default; the thread runs permanently
and opens/closes the listening socket to follow the `modbus_tcp` config
key, so the setting applies live.  Parity with esp32 modbus_tcp.cpp.

Modbus TCP framing: MBAP header = transaction id (2), protocol id (2),
length (2, counts unit id + PDU), unit id (1).  Only FC 3 (read holding
registers) and FC 4 (read input registers) are served, both from the same
table; every other function code gets an ILLEGAL FUNCTION exception, so
the server is strictly read-only.  The unit id is ignored and echoed.
"""

import logging
import select
import socket
import struct
import time

from .errors import InitStep

logger = logging.getLogger("bcmeter.modbus")

PORT = 502
FALLBACK_PORT = 1502
MAX_CLIENTS = 4
IDLE_TIMEOUT_S = 120
MAP_VERSION = 1
NUM_REGS = 32
MBAP_LEN = 7
MAX_FRAME = 260  # spec limit: 6 + max length field 254

# Bits of the data-valid register (address 3).  A cleared bit means the
# corresponding values read 0 because the sensor is not fitted.
VALID_OPTICS = 1 << 0    # bc/atn/sen/ref (ADC present)
VALID_TEMP_HUM = 1 << 1  # temperature/humidity (BME280 or SHT4x)
VALID_PRESSURE = 1 << 2  # pressure (BME280)
VALID_PM = 1 << 3        # pm25/pm10 (SPS30)

# Register map: one contiguous block of 32 registers so a master can poll
# everything in a single read.  Multi-register values are big-endian (word
# order ABCD, the Modbus default).  Measurement values update once per
# sample cycle while sampling, matching /api/status and the CSV.
# (address, words, name, type, unit, description)
REG_MAP = [
    (0, 1, "map_version", "uint16", "", "Register map version"),
    (1, 1, "status", "uint16", "", "0=idle 1=warmup 2=sampling 3=error"),
    (2, 1, "error", "uint16", "", "Error code, 0=none"),
    (3, 1, "valid", "uint16", "", "Data-valid bits: 0=optics 1=temp/hum 2=pressure 3=pm"),
    (4, 1, "filter", "uint16", "", "Filter loading, 5=fresh to 1=replace"),
    (5, 1, "duty", "uint16", "", "Pump duty cycle, 0-255"),
    (6, 1, "session", "uint16", "", "1=logging session active"),
    (7, 1, "reserved", "uint16", "", "Always 0"),
    (8, 2, "samples", "uint32", "", "Samples this session"),
    (10, 2, "uptime", "uint32", "s", "Seconds since device start"),
    (12, 2, "bc", "float32", "ng/m3", "Black carbon, 880nm, Kalman-filtered"),
    (14, 2, "atn", "float32", "", "Attenuation, 880nm"),
    (16, 2, "flow", "float32", "L/min", "Airflow"),
    (18, 2, "sen", "float32", "V", "Sensor photodiode voltage"),
    (20, 2, "ref", "float32", "V", "Reference photodiode voltage"),
    (22, 2, "temperature", "float32", "degC", "Ambient temperature"),
    (24, 2, "humidity", "float32", "%RH", "Relative humidity"),
    (26, 2, "pressure", "float32", "hPa", "Barometric pressure"),
    (28, 2, "pm25", "float32", "ug/m3", "Particulate matter PM2.5"),
    (30, 2, "pm10", "float32", "ug/m3", "Particulate matter PM10"),
]


def _put_u32(regs, addr, value):
    value = int(value) & 0xFFFFFFFF
    regs[addr] = value >> 16
    regs[addr + 1] = value & 0xFFFF


def _put_f32(regs, addr, value):
    regs[addr], regs[addr + 1] = struct.unpack(">HH", struct.pack(">f", float(value)))


class _Client:
    __slots__ = ("sock", "buf", "last_rx")

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.last_rx = time.monotonic()


class ModbusServer:
    """select()-driven single-thread server; run task() in its own thread."""

    def __init__(self, cfg, state_mgr, storage, pump):
        self._cfg = cfg
        self._state = state_mgr
        self._storage = storage
        self._pump = pump
        self._sock = None
        self._port = PORT
        self._clients = []
        self._t0 = time.monotonic()

    def _uptime_s(self):
        """Seconds since device start (ESP32 parity), not service start."""
        try:
            with open("/proc/uptime") as f:
                return float(f.read().split()[0])
        except (OSError, ValueError):
            return time.monotonic() - self._t0

    # ── Register snapshot ───────────────────────────────────────────────────

    def _build_registers(self):
        snap = self._state.snapshot()
        regs = [0] * NUM_REGS

        err_code = snap.get("error", 0)
        init_step = snap.get("init_step", 0)
        sampling = snap.get("sampling", False)
        # Same derivation as /api/status: 0=idle 1=initializing 2=sampling 3=error
        if err_code != 0:
            status = 3
        elif sampling:
            status = 2 if init_step >= int(InitStep.INIT_DONE) else 1
        else:
            status = 0

        temp_hum = snap.get("bme280_present", False) or snap.get("sht4x_present", False)
        valid = 0
        if snap.get("adc_present", False):
            valid |= VALID_OPTICS
        if temp_hum:
            valid |= VALID_TEMP_HUM
        if snap.get("bme280_present", False):
            valid |= VALID_PRESSURE
        if snap.get("sps30_present", False):
            valid |= VALID_PM

        regs[0] = MAP_VERSION
        regs[1] = status
        regs[2] = err_code
        regs[3] = valid
        regs[4] = snap.get("filter_status", 5)
        regs[5] = self._pump.get_duty()
        regs[6] = 1 if self._storage.session_active else 0
        _put_u32(regs, 8, snap.get("sample_count", 0))
        _put_u32(regs, 10, self._uptime_s())
        _put_f32(regs, 12, snap.get("last_bc", 0.0))
        _put_f32(regs, 14, snap.get("last_atn", 0.0))
        _put_f32(regs, 16, snap.get("last_flow", 0.0))
        _put_f32(regs, 18, snap.get("last_sen", 0.0))
        _put_f32(regs, 20, snap.get("last_ref", 0.0))
        _put_f32(regs, 22, snap.get("last_temp", 0.0))
        _put_f32(regs, 24, snap.get("last_humidity", 0.0))
        _put_f32(regs, 26, snap.get("last_pressure", 0.0))
        _put_f32(regs, 28, snap.get("last_pm25", 0.0))
        _put_f32(regs, 30, snap.get("last_pm10", 0.0))
        return regs

    # ── Protocol ────────────────────────────────────────────────────────────

    @staticmethod
    def _exception(frame, code):
        return frame[:4] + struct.pack(">HBB", 3, frame[6], frame[7] | 0x80) + bytes([code])

    def _handle_request(self, frame):
        """Handle one complete request frame; return the response bytes.

        Returns b"" for frames that must be dropped silently (non-Modbus
        protocol id).
        """
        if frame[2:4] != b"\x00\x00":
            return b""
        fc = frame[7]
        if fc not in (3, 4):
            return self._exception(frame, 0x01)
        if len(frame) != MBAP_LEN + 5:  # read PDU is fixed-size
            return self._exception(frame, 0x03)
        addr, qty = struct.unpack(">HH", frame[8:12])
        if not 1 <= qty <= 125:
            return self._exception(frame, 0x03)
        if addr + qty > NUM_REGS:
            return self._exception(frame, 0x02)

        regs = self._build_registers()
        data = struct.pack(">%dH" % qty, *regs[addr:addr + qty])
        header = frame[:4] + struct.pack(">HBBB", 3 + 2 * qty, frame[6], fc, 2 * qty)
        return header + data

    def _process_buffer(self, client):
        """Consume complete frames from the client's accumulation buffer.

        Returns False when the connection must be dropped (malformed stream
        or failed send).
        """
        while len(client.buf) >= MBAP_LEN:
            len_field = struct.unpack(">H", client.buf[4:6])[0]
            if not 2 <= len_field <= 254:
                return False
            frame_len = 6 + len_field
            if len(client.buf) < frame_len:
                break
            response = self._handle_request(client.buf[:frame_len])
            client.buf = client.buf[frame_len:]
            if response:
                try:
                    client.sock.sendall(response)
                except OSError:
                    return False
        return True

    # ── Server ──────────────────────────────────────────────────────────────

    def _start_server(self):
        for port in (PORT, FALLBACK_PORT):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", port))
                sock.listen(MAX_CLIENTS)
            except OSError as exc:  # privileges, port in use, ...
                sock.close()
                if port == FALLBACK_PORT:
                    logger.error("Modbus bind failed on ports %d and %d: %s",
                                 PORT, FALLBACK_PORT, exc)
                    return False
                logger.warning("Modbus bind on port %d failed (%s), trying %d",
                               port, exc, FALLBACK_PORT)
                continue
            self._sock = sock
            self._port = port
            logger.info("Modbus TCP listening on port %d", port)
            return True
        return False

    def _stop_server(self):
        for client in self._clients:
            client.sock.close()
        self._clients = []
        if self._sock:
            self._sock.close()
            self._sock = None
            logger.info("Modbus TCP server stopped")

    def _accept_client(self):
        try:
            sock, peer = self._sock.accept()
        except OSError:
            return
        if len(self._clients) >= MAX_CLIENTS:
            sock.close()
            return
        sock.settimeout(5)  # bounds sendall(); recv only runs after select()
        self._clients.append(_Client(sock))
        logger.debug("Modbus client connected: %s", peer)

    def _serve_once(self):
        """One select() round with 1 s timeout: accept, read, respond, idle-sweep."""
        readable, _, _ = select.select(
            [self._sock] + [c.sock for c in self._clients], [], [], 1.0)

        if self._sock in readable:
            self._accept_client()

        now = time.monotonic()
        for client in list(self._clients):
            if client.sock in readable:
                try:
                    data = client.sock.recv(MAX_FRAME)
                except OSError:
                    data = b""
                client.buf += data
                client.last_rx = now
                # Consume complete frames BEFORE applying the size cap: a
                # leftover partial frame plus a full recv() may legitimately
                # exceed MAX_FRAME on pipelined streams.  After processing,
                # a valid stream never leaves more than one partial frame.
                if not data or not self._process_buffer(client) or len(client.buf) > MAX_FRAME:
                    client.sock.close()
                    self._clients.remove(client)
            elif now - client.last_rx > IDLE_TIMEOUT_S:
                client.sock.close()
                self._clients.remove(client)

    def task(self, stop_event):
        """Thread entry point; follows the `modbus_tcp` config key."""
        try:
            while not stop_event.is_set():
                if not self._cfg.get_bool("modbus_tcp", False):
                    if self._sock:
                        self._stop_server()
                    stop_event.wait(2)
                    continue
                if self._sock is None and not self._start_server():
                    stop_event.wait(5)
                    continue
                self._serve_once()
        finally:
            self._stop_server()

    # ── Introspection (GET /api/modbus) ─────────────────────────────────────

    def info(self):
        return {
            "enabled": self._cfg.get_bool("modbus_tcp", False),
            "listening": self._sock is not None,
            "port": self._port,
            "clients": len(self._clients),
            "max_clients": MAX_CLIENTS,
            "access": "read-only",
            "unit_id": "any",
            "word_order": "big-endian",
            "map_version": MAP_VERSION,
            "function_codes": [3, 4],
            "registers": self._registers_info(),
        }

    def _registers_info(self):
        registers = []
        for reg in REG_MAP:
            entry = dict(zip(("address", "words", "name", "type", "unit", "description"), reg))
            registers.append(entry)
        return registers
