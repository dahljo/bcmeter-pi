"""Sensirion SPS30 particulate matter sensor via I2C.

Port of ESP32 sps30.h/cpp. Direct I2C implementation replacing
sps30_i2c.py dependency.
"""

import logging
import struct
import time
import threading

logger = logging.getLogger("bcmeter.sps30")


def _crc8(data: bytes) -> int:
    """CRC-8 for Sensirion sensors (polynomial 0x31)."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = (crc << 1) ^ 0x31
            else:
                crc = crc << 1
            crc &= 0xFF
    return crc


class SPS30:
    """Sensirion SPS30 particulate matter sensor via I2C.

    Direct I2C implementation replacing sps30_i2c.py dependency.
    """

    ADDR = 0x69
    CMD_START = [0x00, 0x10]
    CMD_STOP = [0x01, 0x04]
    CMD_DATA_READY = [0x02, 0x02]
    CMD_READ_MEASURED = [0x03, 0x00]
    CMD_RESET = [0xD3, 0x04]

    def __init__(self, i2c_lock: threading.Lock = None, bus_num: int = 1):
        self._lock = i2c_lock or threading.Lock()
        self._bus_num = bus_num
        self._bus = None
        self._present = False
        self.pm1 = 0.0
        self.pm25 = 0.0
        self.pm4 = 0.0
        self.pm10 = 0.0

    @property
    def present(self) -> bool:
        return self._present

    def init(self) -> bool:
        """Detect and start SPS30."""
        try:
            from smbus2 import SMBus, i2c_msg
            self._bus = SMBus(self._bus_num)

            # Start measurement (floating point mode)
            with self._lock:
                msg_w = i2c_msg.write(self.ADDR, self.CMD_START + [0x03, 0x00, _crc8(bytes([0x03, 0x00]))])
                self._bus.i2c_rdwr(msg_w)

            time.sleep(1.0)

            # Check if data becomes ready
            for _ in range(10):
                if self._data_ready():
                    self._present = True
                    self.read()
                    logger.info("SPS30 particulate matter sensor detected")
                    return True
                time.sleep(0.5)

            logger.debug("SPS30 not responding with data")
        except Exception as e:
            logger.debug(f"SPS30 init failed: {e}")

        self._present = False
        return False

    def _data_ready(self) -> bool:
        try:
            from smbus2 import i2c_msg
            with self._lock:
                msg_w = i2c_msg.write(self.ADDR, self.CMD_DATA_READY)
                msg_r = i2c_msg.read(self.ADDR, 3)
                self._bus.i2c_rdwr(msg_w, msg_r)
                data = list(msg_r)
                return data[1] == 1
        except Exception:
            return False

    def read(self) -> dict:
        """Read PM values. Returns dict with pm1, pm25, pm4, pm10."""
        if not self._present or self._bus is None:
            return {"pm1": self.pm1, "pm25": self.pm25, "pm4": self.pm4, "pm10": self.pm10}

        try:
            from smbus2 import i2c_msg

            if not self._data_ready():
                return {"pm1": self.pm1, "pm25": self.pm25, "pm4": self.pm4, "pm10": self.pm10}

            with self._lock:
                msg_w = i2c_msg.write(self.ADDR, self.CMD_READ_MEASURED)
                msg_r = i2c_msg.read(self.ADDR, 60)
                self._bus.i2c_rdwr(msg_w, msg_r)
                data = list(msg_r)

            # Parse float values (4 bytes each, with CRC every 2 bytes)
            def parse_float(offset):
                b = bytes([data[offset], data[offset + 1], data[offset + 3], data[offset + 4]])
                return struct.unpack(">f", b)[0]

            self.pm1 = parse_float(0)
            self.pm25 = parse_float(6)
            self.pm4 = parse_float(12)
            self.pm10 = parse_float(18)

            return {"pm1": self.pm1, "pm25": self.pm25, "pm4": self.pm4, "pm10": self.pm10}

        except Exception as e:
            logger.error(f"SPS30 read error: {e}")
            return {"pm1": self.pm1, "pm25": self.pm25, "pm4": self.pm4, "pm10": self.pm10}

    def stop(self):
        """Stop measurement."""
        if self._bus:
            try:
                from smbus2 import i2c_msg
                with self._lock:
                    msg_w = i2c_msg.write(self.ADDR, self.CMD_STOP)
                    self._bus.i2c_rdwr(msg_w)
            except Exception:
                pass

    def close(self):
        self.stop()
        if self._bus:
            try:
                self._bus.close()
            except Exception:
                pass
