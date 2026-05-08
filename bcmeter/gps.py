"""GPS module for NMEA parsing via serial UART.

Port of ESP32's gps.cpp for Raspberry Pi.
Supports AT6668 / u-blox / generic NMEA GPS modules
connected via /dev/serial0 or /dev/ttyS0.
"""

import logging
import threading
import time

logger = logging.getLogger("bcmeter.gps")

GPS_BAUD = 9600
GPS_SERIAL_PORT = "/dev/serial0"
GPS_PROBE_BAUDS = (GPS_BAUD, 9600, 38400, 115200)
GPS_PROBE_TIMEOUT = 2.0


class GPSData:
    __slots__ = ("valid", "lat", "lon", "altitude", "speed",
                 "hdop", "satellites", "time_str", "date_str")

    def __init__(self):
        self.valid = False
        self.lat = 0.0
        self.lon = 0.0
        self.altitude = 0.0
        self.speed = 0.0
        self.hdop = 99.0
        self.satellites = 0
        self.time_str = ""
        self.date_str = ""

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "lat": self.lat,
            "lon": self.lon,
            "altitude": self.altitude,
            "speed": self.speed,
            "hdop": self.hdop,
            "satellites": self.satellites,
            "time": self.time_str,
            "date": self.date_str,
        }


def _nmea_coord(val: str, direction: str) -> float:
    """Convert NMEA coordinate to decimal degrees."""
    if not val:
        return 0.0
    try:
        raw = float(val)
    except ValueError:
        return 0.0
    deg = int(raw / 100)
    minutes = raw - deg * 100
    d = deg + minutes / 60.0
    if direction in ("S", "W"):
        d = -d
    return d


def _nmea_checksum(sentence: str) -> int:
    """XOR checksum of NMEA sentence body (between $ and *)."""
    cs = 0
    for c in sentence:
        if c == "*":
            break
        cs ^= ord(c)
    return cs


class GPS:
    """NMEA GPS reader running as a background thread."""

    def __init__(self):
        self._data = GPSData()
        self._lock = threading.Lock()
        self._serial = None
        self._detected = False
        self._assisted = False
        self._active_baud = 0

    @property
    def present(self) -> bool:
        return self._detected

    def init(self, port: str = GPS_SERIAL_PORT, baud: int = GPS_BAUD,
             probe_timeout: float = GPS_PROBE_TIMEOUT) -> bool:
        """Open serial port only if a GPS module emits NMEA data."""
        try:
            import serial
        except Exception as e:
            logger.debug(f"GPS init failed: {e}")
            self._detected = False
            return False

        self._detected = False
        self._active_baud = 0
        bauds = []
        for candidate in (baud, *GPS_PROBE_BAUDS):
            if candidate not in bauds:
                bauds.append(candidate)

        for candidate in bauds:
            ser = None
            try:
                ser = serial.Serial(port, candidate, timeout=0.1)
                logger.debug("GPS probing %s @ %d", port, candidate)
                if self._probe_nmea(ser, timeout_s=probe_timeout):
                    self._serial = ser
                    self._detected = True
                    self._active_baud = candidate
                    logger.info("GPS UART detected (%s @ %d)", port, candidate)
                    return True
            except Exception as e:
                logger.debug("GPS probe failed at %d baud: %s", candidate, e)
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass

        logger.info("GPS not detected on %s", port)
        self._serial = None
        return False

    def _probe_nmea(self, ser, timeout_s: float) -> bool:
        """Return True when the serial stream looks like NMEA output."""
        deadline = time.monotonic() + max(0.1, timeout_s)
        saw_dollar = False
        while time.monotonic() < deadline:
            try:
                raw = ser.read(128)
            except Exception:
                return False
            if not raw:
                continue
            for byte in raw:
                ch = chr(byte)
                if ch == "$":
                    saw_dollar = True
                    continue
                if saw_dollar and ch in ("G", "P"):
                    return True
                saw_dollar = False
        return False

    def get_data(self) -> GPSData:
        with self._lock:
            # Return a copy
            d = GPSData()
            d.valid = self._data.valid
            d.lat = self._data.lat
            d.lon = self._data.lon
            d.altitude = self._data.altitude
            d.speed = self._data.speed
            d.hdop = self._data.hdop
            d.satellites = self._data.satellites
            d.time_str = self._data.time_str
            d.date_str = self._data.date_str
            return d

    def _send_nmea(self, body: str):
        """Send a formatted NMEA command."""
        if not self._serial:
            return
        cs = 0
        for c in body:
            cs ^= ord(c)
        cmd = f"${body}*{cs:02X}\r\n"
        try:
            self._serial.write(cmd.encode())
        except Exception:
            pass

    def _agps_assist(self):
        """Send AGPS warm/hot start commands."""
        self._send_nmea("PCAS04,5")
        time.sleep(0.2)
        self._send_nmea("PCAS10,1")  # Warm start
        logger.info("GPS AGPS warm start sent")

    def _parse_gga(self, fields: list):
        """Parse $GNGGA / $GPGGA sentence."""
        if len(fields) < 10:
            return
        fix = int(fields[6]) if fields[6] else 0
        with self._lock:
            self._data.valid = fix > 0
            self._data.satellites = int(fields[7]) if fields[7] else 0
            self._data.hdop = float(fields[8]) if fields[8] else 99.0
            self._data.altitude = float(fields[9]) if fields[9] else 0.0
            if fix > 0:
                self._data.lat = _nmea_coord(fields[2], fields[3] if len(fields) > 3 else "")
                self._data.lon = _nmea_coord(fields[4], fields[5] if len(fields) > 5 else "")
            if fields[1] and len(fields[1]) >= 6:
                self._data.time_str = f"{fields[1][0:2]}:{fields[1][2:4]}:{fields[1][4:6]}"

    def _parse_rmc(self, fields: list):
        """Parse $GNRMC / $GPRMC sentence."""
        if len(fields) < 8:
            return
        with self._lock:
            self._data.speed = float(fields[7]) * 1.852 if fields[7] else 0.0
            if len(fields) > 9 and fields[9] and len(fields[9]) >= 6:
                self._data.date_str = f"20{fields[9][4:6]}-{fields[9][2:4]}-{fields[9][0:2]}"

    def _parse_line(self, line: str):
        """Parse a single NMEA sentence."""
        if not line.startswith("$"):
            return
        star_idx = line.find("*")
        if star_idx < 0:
            return
        try:
            given = int(line[star_idx + 1:star_idx + 3], 16)
        except (ValueError, IndexError):
            return
        body = line[1:star_idx]
        if _nmea_checksum(body) != given:
            return
        fields = body.split(",")
        tag = fields[0] if fields else ""
        if tag in ("GNGGA", "GPGGA"):
            self._parse_gga(fields)
        elif tag in ("GNRMC", "GPRMC"):
            self._parse_rmc(fields)

    def task(self, stop_event: threading.Event):
        """Background thread reading GPS serial data."""
        if not self._serial:
            return

        buf = ""
        last_log = 0
        nmea_received = False
        start_time = time.time()
        _NO_DATA_TIMEOUT = 60  # seconds before declaring no GPS module
        _last_lat = 0.0
        _last_lon = 0.0
        _search_logged = False

        while not stop_event.is_set():
            try:
                raw = self._serial.read(256)
                if raw:
                    buf += raw.decode("ascii", errors="ignore")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._parse_line(line)
                            if not nmea_received and line.startswith("$"):
                                nmea_received = True
            except Exception:
                time.sleep(0.1)
                continue

            now = time.time()

            # No NMEA data after timeout → no GPS module present
            if not nmea_received and now - start_time > _NO_DATA_TIMEOUT:
                logger.info("No GPS data received after %ds — module not present", _NO_DATA_TIMEOUT)
                self._detected = False
                try:
                    from .state import state
                    state.set("gps_present", False)
                except Exception:
                    pass
                self.close()
                return

            if now - last_log >= 30:
                last_log = now
                d = self.get_data()
                if d.valid:
                    # Only log when position changed significantly (~11m)
                    if abs(d.lat - _last_lat) > 0.0001 or abs(d.lon - _last_lon) > 0.0001:
                        logger.info(
                            "GPS Fix: %.6f,%.6f Alt:%.0fm Sats:%d",
                            d.lat, d.lon, d.altitude, d.satellites,
                        )
                        _last_lat = d.lat
                        _last_lon = d.lon
                    _search_logged = False
                else:
                    if not _search_logged:
                        logger.debug("GPS searching... Sats:%d", d.satellites)
                        _search_logged = True
                    if not self._assisted:
                        self._assisted = True
                        self._agps_assist()

            time.sleep(0.01)

    def close(self):
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
