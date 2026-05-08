"""Cellular IoT connectivity via SIM7080G modem.

Provides optional cellular data upload when WiFi is unavailable.
Communicates with a SIM7080G LTE-M modem over serial AT commands,
supports HTTP POST to a Lambda endpoint, chunked CSV upload with
zlib compression, and JSON notification delivery.

The modem is optional hardware -- all public methods handle the
modem being absent gracefully and never raise on missing hardware.
"""

import base64
import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import time
import zlib
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .email_handler import normalize_recipients

logger = logging.getLogger("bcmeter.modem")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"
DEFAULT_SERIAL_PORT_ALT = "/dev/ttyAMA0"
DEFAULT_BAUD = 115200
DEFAULT_APN = "iotsim.melita.io"
DEFAULT_URL = "https://xwqm43fafwo7w65d4lno3nspzu0ovykv.lambda-url.eu-north-1.on.aws"
DEFAULT_API_KEY = ""
PWRKEY_PIN = 4

REGISTRATION_TIMEOUT = 120
HTTP_TIMEOUT = 45
HTTP_CHUNK_SIZE = 2500
HTTP_MAX_SINGLE_PAYLOAD = 3500
MAX_RETRIES = 3

# GPIO availability (Raspberry Pi only)
try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False

# pyserial availability
try:
    import serial
    _SERIAL_AVAILABLE = True
except ImportError:
    serial = None  # type: ignore[assignment]
    _SERIAL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_mac_address() -> str:
    """Read the MAC address from wlan0 (or fallback interfaces)."""
    for iface in ("wlan0", "eth0", "usb0"):
        path = f"/sys/class/net/{iface}/address"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return f.read().strip().replace(":", "")
            except Exception:
                pass
    return "unknown"


def _get_device_id() -> str:
    return f"bcMeter_{_get_mac_address()}"


def _get_telemetry() -> dict:
    """Gather lightweight system telemetry for upload metadata."""
    info: Dict = {
        "hostname": socket.gethostname(),
        "device_id": _get_device_id(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        total, used, free = shutil.disk_usage("/")
        info["disk_free_gb"] = round(free / (2 ** 30), 1)
        info["disk_used_pct"] = round((used / total) * 100, 1)
    except Exception:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["cpu_temp_c"] = round(int(f.read()) / 1000, 1)
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            info["uptime_hours"] = round(float(f.read().split()[0]) / 3600, 1)
    except Exception:
        pass
    return info


def _compress(data: bytes) -> Tuple[bytes, float]:
    """Compress *data* with zlib level 9, return (compressed, ratio)."""
    original = len(data)
    compressed = zlib.compress(data, level=9)
    ratio = len(compressed) / original if original > 0 else 1.0
    return compressed, ratio


def _is_uart_port(port: str) -> bool:
    return "serial0" in port or "ttyAMA" in port or "ttyS0" in port


# ---------------------------------------------------------------------------
# Low-level modem driver
# ---------------------------------------------------------------------------

class _SIM7080G:
    """Low-level SIM7080G modem driver over serial AT commands."""

    def __init__(self, port: str, baud: int, apn: str, pwrkey_pin: int):
        self.port = port
        self.baud = baud
        self.apn = apn
        self.pwrkey_pin = pwrkey_pin
        self._serial: Optional["serial.Serial"] = None
        self._connected = False
        self._signal: int = 0
        self._lte_configured = False

    # -- Serial I/O ---------------------------------------------------------

    def _open_serial(self) -> "serial.Serial":
        """Open the serial port with correct settings for USB vs UART."""
        if _is_uart_port(self.port):
            ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            time.sleep(0.1)
        else:
            ser = serial.Serial(self.port, self.baud, timeout=1)
        return ser

    def _at_cmd(self, cmd: str, timeout: float = 5, expect: str = "OK",
                verbose: bool = True) -> Tuple[bool, str]:
        """Send an AT command and wait for *expect* or ERROR.

        Returns ``(success, full_response_text)``.
        """
        if not self._serial or not self._serial.is_open:
            return False, ""
        if verbose:
            logger.debug("TX: %s", cmd)

        try:
            self._serial.reset_input_buffer()
            self._serial.write((cmd + "\r\n").encode())
            self._serial.flush()

            start = time.time()
            buf = b""
            while time.time() - start < timeout:
                if self._serial.in_waiting:
                    chunk = self._serial.read(self._serial.in_waiting)
                    buf += chunk
                    if b"OK\r\n" in buf or b"OK\n" in buf:
                        break
                    if b"ERROR\r\n" in buf or b"ERROR\n" in buf:
                        break
                time.sleep(0.1)

            resp = buf.decode(errors="replace").strip()
        except (OSError, IOError) as exc:
            logger.warning("Serial I/O error during AT command: %s", exc)
            self._close_serial()
            return False, ""
        except Exception as exc:
            if _SERIAL_AVAILABLE and isinstance(exc, serial.SerialException):
                logger.warning("Serial exception during AT command: %s", exc)
                self._close_serial()
                return False, ""
            raise

        if verbose and resp:
            for line in resp.split("\n"):
                line = line.strip()
                if line:
                    logger.debug("RX: %s", line)

        success = expect in resp and "ERROR" not in resp
        return success, resp

    def _at_cmd_wait_urc(self, cmd: str, urc: str,
                         timeout: float = 30) -> Tuple[bool, str]:
        """Send an AT command and wait for an unsolicited result code *urc*."""
        if not self._serial or not self._serial.is_open:
            return False, ""
        logger.debug("TX (wait %s): %s", urc, cmd)

        try:
            self._serial.reset_input_buffer()
            self._serial.write((cmd + "\r\n").encode())
            self._serial.flush()

            start = time.time()
            buf = b""
            while time.time() - start < timeout:
                if self._serial.in_waiting:
                    buf += self._serial.read(self._serial.in_waiting)
                    decoded = buf.decode(errors="replace")
                    if urc in decoded and "\n" in decoded.split(urc)[-1]:
                        break
                time.sleep(0.1)

            resp = buf.decode(errors="replace").strip()
        except (OSError, IOError) as exc:
            logger.warning("Serial I/O error waiting for URC: %s", exc)
            self._close_serial()
            return False, ""
        except Exception as exc:
            if _SERIAL_AVAILABLE and isinstance(exc, serial.SerialException):
                logger.warning("Serial exception waiting for URC: %s", exc)
                self._close_serial()
                return False, ""
            raise

        for line in resp.split("\n"):
            line = line.strip()
            if line:
                logger.debug("RX: %s", line)

        return urc in resp, resp

    def _close_serial(self):
        if self._serial:
            try:
                if self._serial.is_open:
                    self._serial.close()
            except Exception:
                pass
            self._serial = None
        self._connected = False

    # -- Power control ------------------------------------------------------

    def _pulse_pwrkey(self):
        """Toggle PWRKEY to power on/off the modem (1 s pulse)."""
        if not _GPIO_AVAILABLE:
            logger.error("GPIO not available -- cannot toggle modem power key")
            return
        logger.info("Pulsing PWRKEY (GPIO %d) for modem power toggle", self.pwrkey_pin)
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.pwrkey_pin, GPIO.OUT)

        GPIO.output(self.pwrkey_pin, GPIO.LOW)
        time.sleep(0.1)
        GPIO.output(self.pwrkey_pin, GPIO.HIGH)
        time.sleep(1)
        GPIO.output(self.pwrkey_pin, GPIO.LOW)

        logger.info("Waiting 5 s for modem boot")
        time.sleep(5)

    # -- Open / close -------------------------------------------------------

    def open(self) -> bool:
        """Open the serial port and verify the modem responds to AT."""
        try:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.write(b"AT\r\n")
                    return True
                except Exception:
                    self._serial = None

            logger.info("Opening serial %s @ %d", self.port, self.baud)

            try:
                self._serial = self._open_serial()
            except (OSError, Exception) as exc:
                if _SERIAL_AVAILABLE and isinstance(exc, serial.SerialException):
                    logger.warning("Port %s failed (%s), scanning for modem", self.port, exc)
                elif isinstance(exc, OSError):
                    logger.warning("Port %s failed (%s), scanning for modem", self.port, exc)
                else:
                    raise
                found = _scan_modem_ports()
                if not found:
                    return False
                self.port, self.baud = found
                self._serial = self._open_serial()

            time.sleep(0.3)
            self._serial.reset_input_buffer()

            # Probe with AT up to 3 times
            for _ in range(3):
                self._serial.write(b"AT\r\n")
                self._serial.flush()
                time.sleep(1)
                if self._serial.in_waiting:
                    resp = self._serial.read(self._serial.in_waiting)
                    if b"OK" in resp:
                        return True
            return True  # port open even if probe was ambiguous
        except Exception as exc:
            logger.error("Serial open error: %s", exc)
            return False

    def close(self):
        self._close_serial()
        self._lte_configured = False

    # -- Query helpers ------------------------------------------------------

    def get_signal_quality(self) -> int:
        ok, resp = self._at_cmd("AT+CSQ", timeout=1, verbose=False)
        try:
            if "+CSQ:" in resp:
                val = int(resp.split("+CSQ:")[1].split(",")[0].strip())
                self._signal = val
                return val
        except Exception:
            pass
        return 0

    def get_imsi(self) -> str:
        _, resp = self._at_cmd("AT+CIMI", timeout=1, verbose=False)
        for line in resp.split("\n"):
            line = line.strip()
            if line.isdigit() and len(line) >= 15:
                return line
        return ""

    def get_iccid(self) -> str:
        for cmd in ("AT+CICCID", "AT+CCID"):
            _, resp = self._at_cmd(cmd, timeout=1, verbose=False)
            if "ERROR" in resp:
                continue
            if "+ICCID:" in resp:
                return resp.split("+ICCID:")[1].split()[0].strip()
            for line in resp.split("\n"):
                line = line.strip()
                if len(line) >= 19 and all(c in "0123456789abcdefABCDEF" for c in line):
                    return line
        return ""

    def get_ip_address(self) -> str:
        _, resp = self._at_cmd("AT+CNACT?", timeout=1, verbose=False)
        for line in resp.split("\n"):
            if "+CNACT:" in line and ",1," in line:
                parts = line.split('"')
                if len(parts) >= 2 and parts[1] != "0.0.0.0":
                    return parts[1]
        return ""

    def get_operator(self) -> str:
        _, resp = self._at_cmd("AT+COPS?", timeout=1, verbose=False)
        if "+COPS:" in resp and '"' in resp:
            return resp.split('"')[1]
        return ""

    def get_cpsi(self) -> str:
        _, resp = self._at_cmd("AT+CPSI?", timeout=1, verbose=False)
        if "+CPSI: " in resp:
            val = resp.split("+CPSI: ", 1)[1].split("\n")[0].strip()
            self._cpsi = val
            return val
        return self._cpsi if hasattr(self, "_cpsi") else ""

    def query_sim_info(self) -> dict:
        return {
            "imsi": self.get_imsi(),
            "iccid": self.get_iccid(),
            "ip": self.get_ip_address(),
            "operator": self.get_operator(),
            "signal": self._signal,
        }

    # -- LTE configuration --------------------------------------------------

    def _configure_lte(self):
        """Configure the modem for LTE-M (CAT-M1) operation."""
        if self._lte_configured:
            logger.debug("LTE already configured, skipping")
            return

        logger.info("Configuring modem for LTE-M")
        is_usb = "USB" in self.port or "ACM" in self.port

        if not is_usb:
            self._at_cmd("AT+CFUN=0", timeout=5)
            time.sleep(1)

        self._at_cmd("AT+CNMP=38", timeout=2)        # LTE only
        self._at_cmd("AT+CMNB=1", timeout=2)          # CAT-M preferred
        self._at_cmd('AT+CBANDCFG="CAT-M",8,20', timeout=2)
        self._at_cmd(f'AT+CGDCONT=1,"IP","{self.apn}"', timeout=2)
        self._at_cmd(f'AT+CNCFG=1,1,"{self.apn}"', timeout=2)

        if not is_usb:
            self._at_cmd("AT+CFUN=1", timeout=15)

        self._lte_configured = True
        logger.info("LTE-M configuration complete, waiting for network")
        time.sleep(5)

    # -- Registration -------------------------------------------------------

    def _wait_registration(self, timeout: int = REGISTRATION_TIMEOUT) -> bool:
        """Poll +CEREG until registered (home or roaming) or timeout."""
        logger.info("Waiting for network registration (max %d s)", timeout)
        start = time.time()
        reconnect_attempts = 0

        while time.time() - start < timeout:
            if not self._serial:
                if reconnect_attempts >= 3:
                    logger.error("Too many serial reconnect attempts during registration")
                    return False
                logger.info("Serial port lost, attempting reconnect")
                time.sleep(2)
                if not self.open():
                    reconnect_attempts += 1
                    continue
                reconnect_attempts = 0

            csq = self.get_signal_quality()
            ok, resp = self._at_cmd("AT+CEREG?", timeout=1, verbose=False)

            if not self._serial:
                continue

            if "+CEREG:" in resp:
                if ",1" in resp or ",5" in resp:
                    logger.info("Registered on network (signal=%d)", csq)
                    return True
                elif ",2" in resp:
                    logger.debug("Searching for network (signal=%d)", csq)
                else:
                    logger.debug("Registration status: %s (signal=%d)", resp.strip(), csq)
            time.sleep(3)

        logger.error("Network registration timed out after %d s", timeout)
        self._at_cmd("AT+CEER", timeout=2)  # log extended error report
        return False

    # -- Connect / disconnect -----------------------------------------------

    def connect(self) -> bool:
        """Full connection sequence: open -> echo off -> LTE config -> register -> PDP."""
        if not self.open():
            return False

        self._at_cmd("ATE0", timeout=1)  # echo off
        self._configure_lte()

        if self._wait_registration():
            logger.info("Activating PDP context")
            self._at_cmd("AT+CNACT=1,1", timeout=10)
            _, ip_resp = self._at_cmd("AT+CNACT?", timeout=2)
            if "+CNACT: 1,1" in ip_resp:
                logger.info("Data connection active")
            else:
                logger.warning("PDP activation uncertain, continuing anyway")
            self._connected = True
            return True

        self._lte_configured = False
        return False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def signal(self) -> int:
        return self._signal

    # -- HTTP POST ----------------------------------------------------------

    def http_post(self, url: str, api_key: str, device_id: str,
                  payload: str) -> Tuple[bool, int, str]:
        """POST *payload* (JSON string) via AT+SH* HTTP commands.

        Returns ``(success, http_status, response_body)``.
        """
        payload_len = len(payload)
        logger.info("HTTP POST %d bytes", payload_len)

        # Disconnect any previous session
        self._at_cmd("AT+SHDISC", timeout=1)
        time.sleep(0.5)

        # Configure HTTP
        self._at_cmd('AT+SHCONF="CONTEXTID",1', timeout=1)
        self._at_cmd('AT+CSSLCFG="sslversion",1,3', timeout=1)
        self._at_cmd('AT+SHSSL=1,""', timeout=1)
        self._at_cmd(f'AT+SHCONF="URL","{url}"', timeout=2)
        self._at_cmd('AT+SHCONF="BODYLEN",4096', timeout=1)
        self._at_cmd('AT+SHCONF="HEADERLEN",350', timeout=1)

        # Connect
        ok, resp = self._at_cmd("AT+SHCONN", timeout=25)
        if not ok:
            logger.error("HTTP SHCONN failed")
            return False, 0, "Connect failed"
        time.sleep(0.5)

        # Set headers
        self._at_cmd("AT+SHCHEAD", timeout=1)
        self._at_cmd(f'AT+SHAHEAD="x-api-key","{api_key}"', timeout=1)
        self._at_cmd(f'AT+SHAHEAD="x-device-id","{device_id}"', timeout=1)
        self._at_cmd('AT+SHAHEAD="Content-Type","application/json"', timeout=1)

        # Set body
        ok_body, body_resp = self._at_cmd(f"AT+SHBOD={payload_len},10000", timeout=2)
        if "ERROR" in body_resp:
            logger.error("HTTP body setup failed: %s", body_resp)
            self._at_cmd("AT+SHDISC", timeout=1)
            return False, 0, "Body setup failed"

        time.sleep(0.3)
        self._serial.write(payload.encode())
        self._serial.flush()
        time.sleep(1)

        # Send POST request
        logger.debug("Sending HTTP POST request")
        found_urc, resp = self._at_cmd_wait_urc(
            'AT+SHREQ="/",3', "+SHREQ:", HTTP_TIMEOUT
        )

        success = False
        status_code = 0
        body = ""

        if "+SHREQ:" in resp:
            try:
                parts = resp.split("+SHREQ:")[1].split(",")
                status_code = int(parts[1])
                resp_len = int(parts[2].split()[0])
                logger.info("HTTP %d, response %d bytes", status_code, resp_len)

                if resp_len > 0:
                    time.sleep(0.5)
                    _, body_resp = self._at_cmd(
                        f"AT+SHREAD=0,{min(resp_len, 500)}", timeout=5
                    )
                    body = body_resp

                success = status_code in (200, 201)
            except Exception as exc:
                logger.error("HTTP response parse error: %s", exc)
        else:
            logger.error("No +SHREQ response received")

        self._at_cmd("AT+SHDISC", timeout=2)
        return success, status_code, body


# ---------------------------------------------------------------------------
# Port scanning
# ---------------------------------------------------------------------------

def _scan_modem_ports() -> Optional[Tuple[str, int]]:
    """Scan USB and UART ports for a SIM7080G modem responding to AT.

    Returns ``(port, baudrate)`` or ``None``.
    """
    if not _SERIAL_AVAILABLE:
        return None

    import glob as _glob

    usb_ports = sorted(_glob.glob("/dev/ttyUSB*") + _glob.glob("/dev/ttyACM*"))
    uart_ports = ["/dev/serial0", "/dev/ttyS0"]
    logger.debug("Scanning USB=%s UART=%s", usb_ports, uart_ports)

    # Try USB ports at 115200
    for port in usb_ports:
        try:
            s = serial.Serial(port, 115200, timeout=1)
            s.write(b"AT\r")
            time.sleep(0.5)
            if s.in_waiting:
                resp = s.read(s.in_waiting)
                if b"OK" in resp:
                    s.close()
                    logger.info("Modem found on %s @ 115200", port)
                    return (port, 115200)
            s.close()
        except Exception:
            continue

    # Try UART ports at 9600
    for port in uart_ports:
        if not os.path.exists(port):
            continue
        try:
            if _is_uart_port(port):
                s = serial.Serial(
                    port=port, baudrate=9600, bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                    timeout=2, xonxoff=False, rtscts=False, dsrdtr=False,
                )
                s.reset_input_buffer()
                s.reset_output_buffer()
                time.sleep(0.1)
            else:
                s = serial.Serial(port, 9600, timeout=2)
            time.sleep(0.2)
            s.reset_input_buffer()

            for _ in range(3):
                s.write(b"AT\r\n")
                s.flush()
                time.sleep(1)
                if s.in_waiting:
                    resp = s.read(s.in_waiting)
                    if b"OK" in resp:
                        s.close()
                        logger.info("Modem found on %s @ 9600", port)
                        return (port, 9600)
            s.close()
        except Exception:
            continue

    return None


# ---------------------------------------------------------------------------
# Public API: IoTManager
# ---------------------------------------------------------------------------

class IoTManager:
    """High-level cellular IoT manager for bcMeter.

    Wraps the SIM7080G modem driver and provides a clean interface for
    modem detection, connection management, and data upload.

    Parameters
    ----------
    cfg : optional
        A :class:`bcmeter.config.CfgStore` instance (or any object with a
        ``get(key, default)`` method).  When *None*, sensible defaults are
        used.
    """

    def __init__(self, cfg=None):
        self._cfg = cfg
        self._modem: Optional[_SIM7080G] = None
        self._detected: Optional[bool] = None  # None = not yet probed
        self._device_id: str = _get_device_id()
        logger.info("IoTManager initialised for %s", self._device_id)

    # -- Config helpers -----------------------------------------------------

    def _c(self, key: str, default):
        """Read a config value, supporting CfgStore or plain dict."""
        if self._cfg is None:
            return default
        if hasattr(self._cfg, "get"):
            val = self._cfg.get(key, default)
            return val if val is not None else default
        return default

    @property
    def _apn(self) -> str:
        return str(self._c("iot_apn", DEFAULT_APN))

    @property
    def _url(self) -> str:
        return str(self._c("iot_url", DEFAULT_URL))

    @property
    def _api_key(self) -> str:
        value = str(self._c("email_api_key", "") or "").strip()
        if value and value not in ("configured", "email_service_password", "your_api_key", "iot_api_key"):
            return value
        return DEFAULT_API_KEY

    @property
    def _pwrkey_pin(self) -> int:
        try:
            return int(self._c("iot_pwrkey_pin", PWRKEY_PIN))
        except (ValueError, TypeError):
            return PWRKEY_PIN

    @property
    def _chunk_size(self) -> int:
        try:
            return int(self._c("iot_chunk_size", HTTP_CHUNK_SIZE))
        except (ValueError, TypeError):
            return HTTP_CHUNK_SIZE

    @property
    def _max_retries(self) -> int:
        try:
            return int(self._c("iot_max_retries", MAX_RETRIES))
        except (ValueError, TypeError):
            return MAX_RETRIES

    @property
    def _enabled(self) -> bool:
        val = self._c("iot_enable", False)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    # -- Detection ----------------------------------------------------------

    def detect(self) -> bool:
        """Check whether a SIM7080G modem is present on any serial port.

        This does *not* connect to the network -- it only probes for an AT
        response. The result is cached; call again to re-probe.
        """
        if not _SERIAL_AVAILABLE:
            logger.debug("pyserial not installed, modem unavailable")
            self._detected = False
            return False

        result = _scan_modem_ports()
        if result:
            logger.info("Modem detected on %s @ %d", result[0], result[1])
            self._detected = True
            return True

        # Try power-cycling via PWRKEY and scan again
        logger.debug("No modem found, attempting PWRKEY power cycle")
        try:
            tmp = _SIM7080G("/dev/ttyS0", 9600, self._apn, self._pwrkey_pin)
            tmp._pulse_pwrkey()
        except Exception as exc:
            logger.debug("PWRKEY power cycle failed: %s", exc)

        time.sleep(2)
        result = _scan_modem_ports()
        if result:
            logger.info("Modem detected after power cycle on %s @ %d",
                        result[0], result[1])
            self._detected = True
            return True

        logger.debug("No modem detected")
        self._detected = False
        return False

    # -- Connection ---------------------------------------------------------

    def connect(self) -> bool:
        """Power on the modem and register on the cellular network.

        Returns ``True`` if the modem is connected and a PDP context is
        active (i.e. an IP address has been assigned).
        """
        if self._modem and self._modem.connected:
            return True

        result = _scan_modem_ports()
        if not result:
            logger.warning("No modem found, trying power cycle")
            try:
                tmp = _SIM7080G("/dev/ttyS0", 9600, self._apn, self._pwrkey_pin)
                tmp._pulse_pwrkey()
            except Exception as exc:
                logger.debug("Power cycle failed: %s", exc)
            time.sleep(2)
            result = _scan_modem_ports()

        if not result:
            logger.error("Modem not found -- cannot connect")
            self._detected = False
            return False

        port, baud = result
        self._detected = True
        self._modem = _SIM7080G(port, baud, self._apn, self._pwrkey_pin)
        if self._modem.connect():
            logger.info("Cellular connection established")
            return True

        logger.error("Cellular connection failed")
        self._modem.close()
        self._modem = None
        return False

    def disconnect(self):
        """Cleanly shut down the modem connection."""
        if self._modem:
            self._modem.close()
            self._modem = None
            logger.info("Modem disconnected")

    # -- Status queries -----------------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` if the modem has been detected (or can be detected).

        This is a lightweight check. If the modem has never been probed, a
        detection scan is performed once.
        """
        if self._detected is None:
            return self.detect()
        return self._detected

    def is_connected(self) -> bool:
        """Return ``True`` if the modem is currently registered on a network."""
        if self._modem is None:
            return False
        return self._modem.connected

    def get_signal(self) -> int:
        """Return current signal quality on the 0-31 AT+CSQ scale.

        Returns 0 if the modem is not connected.
        """
        if self._modem is None:
            return 0
        return self._modem.get_signal_quality()

    def get_cpsi(self) -> str:
        """Return the AT+CPSI? serving-cell string, or empty if unavailable."""
        if self._modem is None:
            return ""
        return self._modem.get_cpsi()

    def get_sim_info(self) -> dict:
        """Return SIM / network information (IMSI, ICCID, operator, IP, signal).

        Returns an empty dict if the modem is not connected.
        """
        if self._modem is None:
            if not self.connect():
                return {}
        return self._modem.query_sim_info()

    # -- HTTP upload (single payload) ---------------------------------------

    def _upload_single(self, payload: dict) -> bool:
        """Serialize *payload* as JSON and HTTP POST it via the modem."""
        json_str = json.dumps(payload)
        if len(json_str) > HTTP_MAX_SINGLE_PAYLOAD:
            logger.error("Payload too large for single upload (%d bytes)",
                         len(json_str))
            return False

        ok, status, body = self._modem.http_post(
            self._url, self._api_key, self._device_id, json_str,
        )
        if not ok:
            logger.warning("HTTP POST failed: status=%d body=%s",
                           status, body[:200] if body else "")
        return ok

    # -- HTTP upload (chunked) ----------------------------------------------

    def _upload_chunked(self, filename: str, compressed: bytes,
                        recipients: list, telemetry: dict,
                        shared: bool = False,
                        modem_abbreviated: bool = False) -> bool:
        """Upload large data in chunks via repeated HTTP POSTs."""
        chunk_size = self._chunk_size
        total_chunks = (len(compressed) + chunk_size - 1) // chunk_size
        file_hash = hashlib.md5(compressed).hexdigest()[:8]
        upload_id = f"{self._device_id}_{int(time.time())}_{file_hash}"

        logger.info("Chunked upload: %d bytes in %d chunks (id=%s)",
                     len(compressed), total_chunks, upload_id)

        retries = self._max_retries
        chunks_ok = 0
        chunks_failed = 0
        for i in range(total_chunks):
            start_pos = i * chunk_size
            end_pos = min(start_pos + chunk_size, len(compressed))
            chunk = compressed[start_pos:end_pos]

            payload: Dict = {
                "upload_id": upload_id,
                "chunk_index": i,
                "total_chunks": total_chunks,
                "chunk_b64": base64.b64encode(chunk).decode("ascii"),
                "filename": filename,
                "compressed": True,
                "final": i == total_chunks - 1,
            }

            if i == 0:
                payload["telemetry"] = telemetry
                payload["recipients"] = recipients
                payload["total_size"] = len(compressed)
                if shared:
                    payload["shared"] = True
                if modem_abbreviated:
                    payload["modem_abbreviated"] = True

            logger.info("Uploading chunk %d/%d (%d bytes)",
                        i + 1, total_chunks, len(chunk))

            success = False
            for attempt in range(retries):
                if self._upload_single(payload):
                    success = True
                    break
                logger.warning("Chunk %d attempt %d failed, retrying",
                               i + 1, attempt + 1)
                time.sleep(2)

            if not success:
                chunks_failed += 1
                logger.error("Failed to upload chunk %d after %d attempts",
                             i + 1, retries)
                logger.info("Chunked upload progress: ok=%d failed=%d sent=%d/%d",
                            chunks_ok, chunks_failed, i, total_chunks)
                return False

            chunks_ok += 1
            logger.info("Chunked upload progress: ok=%d failed=%d sent=%d/%d",
                        chunks_ok, chunks_failed, i + 1, total_chunks)
            time.sleep(1)

        logger.info("Chunked upload complete: ok=%d failed=%d",
                    chunks_ok, chunks_failed)
        return True

    # -- Ensure modem -------------------------------------------------------

    def _ensure_modem(self) -> bool:
        """Make sure the modem is connected, connecting if necessary."""
        if self._modem and self._modem.connected:
            return True
        return self.connect()

    # -- Public upload methods ----------------------------------------------

    def upload_data(self, data: bytes, filename: str,
                    recipients: Optional[list] = None,
                    shared: bool = False,
                    modem_abbreviated: bool = False) -> bool:
        """Upload raw data (typically CSV) via cellular.

        The data is compressed with zlib, base64-encoded, and sent as JSON.
        Large payloads are automatically split into chunks.

        Parameters
        ----------
        data : bytes
            Raw file content to upload.
        filename : str
            Filename to associate with the upload.
        recipients : list of str, optional
            Email addresses for log delivery.  Defaults to the configured
            ``mail_logs_to`` value.

        Returns
        -------
        bool
            ``True`` if the upload succeeded.
        """
        logger.info("Upload request: %s (%d bytes)", filename, len(data))

        if recipients is None:
            recipients = self._get_recipients()
        else:
            recipients = normalize_recipients(recipients)

        compressed, ratio = _compress(data)
        logger.info("Compressed: %d bytes (%.1f%% of original)",
                     len(compressed), ratio * 100)

        telemetry = _get_telemetry()
        telemetry["original_size"] = len(data)
        telemetry["compressed_size"] = len(compressed)
        telemetry["compression_ratio"] = round(ratio, 3)

        if not self._ensure_modem():
            logger.error("No modem connection for upload")
            return False

        encoded = base64.b64encode(compressed).decode("ascii")
        payload: Dict = {
            "recipients": recipients,
            "filename": filename,
            "content_b64": encoded,
            "compressed": True,
            "telemetry": telemetry,
        }
        if shared:
            payload["shared"] = True
        if modem_abbreviated:
            payload["modem_abbreviated"] = True

        payload_size = len(json.dumps(payload))
        logger.debug("Total payload size: %d bytes", payload_size)

        if payload_size <= HTTP_MAX_SINGLE_PAYLOAD:
            logger.info("Using single HTTP upload")
            return self._upload_single(payload)
        else:
            logger.info("Payload too large for single upload, using chunked")
            return self._upload_chunked(
                filename, compressed, recipients, telemetry,
                shared=shared,
                modem_abbreviated=modem_abbreviated,
            )

    def upload_file(self, filepath: str,
                    recipients: Optional[list] = None,
                    shared: bool = False) -> bool:
        """Upload a file from disk via cellular.

        Convenience wrapper around :meth:`upload_data` that reads the file
        first.
        """
        logger.info("File upload request: %s", filepath)
        if not os.path.exists(filepath):
            logger.error("File not found: %s", filepath)
            return False
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except Exception as exc:
            logger.error("File read error: %s", exc)
            return False

        return self.upload_data(content, os.path.basename(filepath), recipients,
                               shared=shared)

    def send_notification(self, notification: dict,
                          recipients: Optional[list] = None) -> bool:
        """Send a JSON notification via cellular HTTP POST.

        Parameters
        ----------
        notification : dict
            Notification payload (must be JSON-serializable).
        recipients : list of str, optional
            Email addresses for delivery.

        Returns
        -------
        bool
            ``True`` if the notification was sent successfully.
        """
        ntype = notification.get("notification_type", "unknown")
        logger.info("Notification request: %s", ntype)

        if recipients is None:
            recipients = self._get_recipients()
        else:
            recipients = normalize_recipients(recipients)

        if not self._ensure_modem():
            logger.error("No modem connection for notification")
            return False

        payload = dict(notification)
        if recipients:
            payload["recipients"] = recipients

        return self._upload_single(payload)

    # -- Recipients helper --------------------------------------------------

    def _get_recipients(self) -> List[str]:
        """Read email recipients from config."""
        raw = str(self._c("mail_logs_to", ""))
        return normalize_recipients(raw)

    # -- Status summary -----------------------------------------------------

    def get_status(self) -> dict:
        """Return a dict summarising IoT modem / connection state."""
        status: Dict = {
            "iot_enabled": self._enabled,
            "device_id": self._device_id,
            "modem_detected": self._detected or False,
            "connected": self.is_connected(),
            "signal_quality": 0,
            "recipients": self._get_recipients(),
        }

        if self._modem:
            status["connected"] = self._modem.connected
            status["signal_quality"] = self._modem.signal
            status["sim_info"] = self._modem.query_sim_info()

        return status


# ---------------------------------------------------------------------------
# WiFi / system-IP fallback functions
# ---------------------------------------------------------------------------
# These use the OS network stack (urllib) instead of AT commands, for
# situations where WiFi or Ethernet is available but we still want to
# talk to the same Lambda endpoint.

def _ip_post_json(url: str, api_key: str, device_id: str,
                  payload: dict, timeout: int = HTTP_TIMEOUT) -> Tuple[bool, int, str]:
    """HTTP POST via the system IP stack (urllib)."""
    import urllib.request
    import urllib.error

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("x-device-id", device_id)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            resp_body = resp.read().decode("utf-8", "replace")
            return 200 <= status < 300, status, resp_body
    except urllib.error.HTTPError as exc:
        try:
            resp_body = exc.read().decode("utf-8", "replace")
        except Exception:
            resp_body = str(exc)
        return False, int(getattr(exc, "code", 0) or 0), resp_body
    except Exception as exc:
        return False, 0, str(exc)


def send_notification_over_ip(notification: dict, cfg=None,
                              recipients: Optional[List[str]] = None) -> bool:
    """Send a notification via WiFi / system IP stack (not via modem AT).

    This is the fallback path used when WiFi is available.
    """
    mgr = IoTManager(cfg)
    if not mgr._enabled:
        return False
    if recipients is None:
        recipients = mgr._get_recipients()
    else:
        recipients = normalize_recipients(recipients)

    payload = dict(notification)
    payload["recipients"] = recipients

    ok, status, body = _ip_post_json(
        mgr._url, mgr._api_key, mgr._device_id, payload,
    )
    if not ok:
        logger.warning("IP notification failed: HTTP %d %s", status, body[:200])
    return ok


def upload_file_over_ip(filepath: str, cfg=None,
                        recipients: Optional[List[str]] = None,
                        content: Optional[bytes] = None,
                        shared: bool = False) -> bool:
    """Upload a file via WiFi / system IP stack (not via modem AT).

    This is the fallback path used when WiFi is available.
    """
    mgr = IoTManager(cfg)
    if not mgr._enabled:
        return False

    if content is None:
        if not os.path.exists(filepath):
            return False
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except Exception:
            return False

    if recipients is None:
        recipients = mgr._get_recipients()
    else:
        recipients = normalize_recipients(recipients)

    filename = os.path.basename(filepath)
    compressed, ratio = _compress(content)

    telemetry = _get_telemetry()
    telemetry["original_size"] = len(content)
    telemetry["compressed_size"] = len(compressed)
    telemetry["compression_ratio"] = round(ratio, 3)

    device_id = mgr._device_id
    url = mgr._url
    api_key = mgr._api_key

    encoded = base64.b64encode(compressed).decode("ascii")
    payload: Dict = {
        "recipients": recipients,
        "filename": filename,
        "content_b64": encoded,
        "compressed": True,
        "telemetry": telemetry,
    }
    if shared:
        payload["shared"] = True

    payload_size = len(json.dumps(payload))
    if payload_size <= HTTP_MAX_SINGLE_PAYLOAD:
        ok, status, body = _ip_post_json(url, api_key, device_id, payload)
        if not ok:
            logger.warning("IP upload failed: HTTP %d %s", status, body[:200])
        return ok

    # Chunked upload over IP
    chunk_size = mgr._chunk_size
    total_chunks = (len(compressed) + chunk_size - 1) // chunk_size
    file_hash = hashlib.md5(compressed).hexdigest()[:8]
    upload_id = f"{device_id}_{int(time.time())}_{file_hash}"
    retries = mgr._max_retries

    for i in range(total_chunks):
        start_pos = i * chunk_size
        end_pos = min(start_pos + chunk_size, len(compressed))
        chunk = compressed[start_pos:end_pos]

        chunk_payload: Dict = {
            "upload_id": upload_id,
            "chunk_index": i,
            "total_chunks": total_chunks,
            "chunk_b64": base64.b64encode(chunk).decode("ascii"),
            "filename": filename,
            "compressed": True,
            "final": i == total_chunks - 1,
        }

        if i == 0:
            chunk_payload["telemetry"] = telemetry
            chunk_payload["recipients"] = recipients
            chunk_payload["total_size"] = len(compressed)
            if shared:
                chunk_payload["shared"] = True

        success = False
        for attempt in range(retries):
            ok, status, body = _ip_post_json(url, api_key, device_id, chunk_payload)
            if ok:
                success = True
                break
            time.sleep(1.0)
        if not success:
            logger.error("IP chunked upload failed at chunk %d", i + 1)
            return False

    return True
