"""BME280 temperature/humidity/pressure sensor driver via I2C (smbus2).

Port of ESP32 bme280.h/cpp. Provides pressure readings needed for
altitude correction via QNH. Uses Bosch BST-BME280-DS002 integer
compensation formulas (section 4.2.3).
"""

import logging
import struct
import time
import threading

logger = logging.getLogger("bcmeter.bme280")

# Register addresses
_REG_CHIP_ID = 0xD0
_REG_RESET = 0xE0
_REG_CTRL_HUM = 0xF2
_REG_STATUS = 0xF3
_REG_CTRL_MEAS = 0xF4
_REG_CONFIG = 0xF5
_REG_DATA = 0xF7  # 8 bytes: press[3] temp[3] hum[2]
_REG_TRIM_TP = 0x88  # 24 bytes: T1-3, P1-9
_REG_TRIM_H1 = 0xA1  # 1 byte
_REG_TRIM_H2 = 0xE1  # 7 bytes: H2-6

_CHIP_ID_BME280 = 0x60


class BME280:
    """BME280 temperature/humidity/pressure sensor via raw I2C."""

    def __init__(self, i2c_lock: threading.Lock = None, bus_num: int = 1):
        self._lock = i2c_lock or threading.Lock()
        self._bus_num = bus_num
        self._bus = None
        self._addr = 0
        self._present = False
        self.temperature = 0.0
        self.humidity = 0.0
        self.pressure = 0.0

        # Trim coefficients
        self._dig_T1 = 0
        self._dig_T2 = 0
        self._dig_T3 = 0
        self._dig_P1 = 0
        self._dig_P2 = 0
        self._dig_P3 = 0
        self._dig_P4 = 0
        self._dig_P5 = 0
        self._dig_P6 = 0
        self._dig_P7 = 0
        self._dig_P8 = 0
        self._dig_P9 = 0
        self._dig_H1 = 0
        self._dig_H2 = 0
        self._dig_H3 = 0
        self._dig_H4 = 0
        self._dig_H5 = 0
        self._dig_H6 = 0
        self._t_fine = 0

    @property
    def present(self) -> bool:
        return self._present

    def init(self) -> bool:
        """Detect and initialize BME280 at 0x76 or 0x77. Returns True if found."""
        try:
            from smbus2 import SMBus
            self._bus = SMBus(self._bus_num)
        except Exception as e:
            logger.debug(f"BME280: failed to open I2C bus: {e}")
            return False

        for addr in (0x76, 0x77):
            self._addr = addr
            try:
                with self._lock:
                    chip_id = self._bus.read_byte_data(addr, _REG_CHIP_ID)
                if chip_id != _CHIP_ID_BME280:
                    continue

                with self._lock:
                    # Soft reset
                    self._bus.write_byte_data(addr, _REG_RESET, 0xB6)
                time.sleep(0.005)

                # Wait for NVM copy (im_update bit)
                for _ in range(10):
                    with self._lock:
                        status = self._bus.read_byte_data(addr, _REG_STATUS)
                    if not (status & 0x01):
                        break
                    time.sleep(0.002)

                # Configure
                with self._lock:
                    self._bus.write_byte_data(addr, _REG_CONFIG, 0x00)
                    self._bus.write_byte_data(addr, _REG_CTRL_HUM, 0x01)

                if not self._load_trim():
                    logger.debug(f"BME280: trim read failed at 0x{addr:02X}")
                    continue

                self._present = True
                logger.info(f"BME280 detected at 0x{addr:02X}")
                return True

            except Exception:
                continue

        logger.debug("BME280 not found")
        return False

    def _load_trim(self) -> bool:
        """Load trim/calibration coefficients from BME280 NVM."""
        try:
            with self._lock:
                tp = self._bus.read_i2c_block_data(self._addr, _REG_TRIM_TP, 24)
            b = bytes(tp)

            self._dig_T1 = struct.unpack_from("<H", b, 0)[0]
            self._dig_T2 = struct.unpack_from("<h", b, 2)[0]
            self._dig_T3 = struct.unpack_from("<h", b, 4)[0]
            self._dig_P1 = struct.unpack_from("<H", b, 6)[0]
            self._dig_P2 = struct.unpack_from("<h", b, 8)[0]
            self._dig_P3 = struct.unpack_from("<h", b, 10)[0]
            self._dig_P4 = struct.unpack_from("<h", b, 12)[0]
            self._dig_P5 = struct.unpack_from("<h", b, 14)[0]
            self._dig_P6 = struct.unpack_from("<h", b, 16)[0]
            self._dig_P7 = struct.unpack_from("<h", b, 18)[0]
            self._dig_P8 = struct.unpack_from("<h", b, 20)[0]
            self._dig_P9 = struct.unpack_from("<h", b, 22)[0]

            with self._lock:
                self._dig_H1 = self._bus.read_byte_data(self._addr, _REG_TRIM_H1)
                h = self._bus.read_i2c_block_data(self._addr, _REG_TRIM_H2, 7)

            self._dig_H2 = struct.unpack_from("<h", bytes(h), 0)[0]
            self._dig_H3 = h[2]
            # H4 and H5 share a nibble (h[3] high nibble + h[4] low nibble)
            self._dig_H4 = (self._to_signed8(h[3]) * 16) | (h[4] & 0x0F)
            self._dig_H5 = (self._to_signed8(h[5]) * 16) | (h[4] >> 4)
            self._dig_H6 = self._to_signed8(h[6])

            return True
        except Exception as e:
            logger.debug(f"BME280 trim read error: {e}")
            return False

    @staticmethod
    def _to_signed8(val):
        return val - 256 if val > 127 else val

    # -- Bosch integer compensation formulas (datasheet section 4.2.3) --

    def _compensate_temp(self, adc_T: int) -> float:
        var1 = ((((adc_T >> 3) - (self._dig_T1 << 1))) * self._dig_T2) >> 11
        var2 = (((((adc_T >> 4) - self._dig_T1) *
                   ((adc_T >> 4) - self._dig_T1)) >> 12) *
                 self._dig_T3) >> 14
        self._t_fine = var1 + var2
        return ((self._t_fine * 5 + 128) >> 8) / 100.0

    def _compensate_pressure(self, adc_P: int) -> float:
        var1 = self._t_fine - 128000
        var2 = var1 * var1 * self._dig_P6
        var2 += (var1 * self._dig_P5) << 17
        var2 += self._dig_P4 << 35
        var1 = ((var1 * var1 * self._dig_P3) >> 8) + ((var1 * self._dig_P2) << 12)
        var1 = ((1 << 47) + var1) * self._dig_P1 >> 33
        if var1 == 0:
            return 0.0
        p = 1048576 - adc_P
        p = (((p << 31) - var2) * 3125) // var1
        var1 = (self._dig_P9 * (p >> 13) * (p >> 13)) >> 25
        var2 = (self._dig_P8 * p) >> 19
        p = ((p + var1 + var2) >> 8) + (self._dig_P7 << 4)
        return p / 25600.0  # Pa -> hPa

    def _compensate_humidity(self, adc_H: int) -> float:
        v = self._t_fine - 76800
        v = (((adc_H << 14) - (self._dig_H4 << 20) - (self._dig_H5 * v)) +
             16384) >> 15
        w = (((((v * self._dig_H6) >> 10) *
               (((v * self._dig_H3) >> 11) + 32768)) >> 10) + 2097152) * \
            self._dig_H2
        v = v * ((w + 8192) >> 14)
        v -= (((((v >> 15) * (v >> 15)) >> 7) * self._dig_H1) >> 4)
        if v < 0:
            v = 0
        if v > 419430400:
            v = 419430400
        return (v >> 12) / 1024.0

    def read(self) -> tuple:
        """Trigger forced measurement and read T, H, P.

        Returns (temperature_C, humidity_pct, pressure_hPa).
        """
        if not self._present or self._bus is None:
            return self.temperature, self.humidity, self.pressure

        try:
            # Trigger forced measurement: osrs_t=x2, osrs_p=x16, osrs_h=x1, mode=forced
            with self._lock:
                self._bus.write_byte_data(
                    self._addr, _REG_CTRL_MEAS,
                    (0b010 << 5) | (0b101 << 2) | 0b01
                )

            # Wait for measurement (osrs_t*2 + osrs_p*16 + osrs_h*1 ~ 45ms)
            time.sleep(0.003)
            for _ in range(60):
                with self._lock:
                    status = self._bus.read_byte_data(self._addr, _REG_STATUS)
                if not (status & 0x08):
                    break
                time.sleep(0.001)

            # Read 8 bytes: press[3] temp[3] hum[2]
            with self._lock:
                d = self._bus.read_i2c_block_data(self._addr, _REG_DATA, 8)

            adc_P = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
            adc_T = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
            adc_H = (d[6] << 8) | d[7]

            self.temperature = self._compensate_temp(adc_T)
            self.pressure = self._compensate_pressure(adc_P)
            self.humidity = self._compensate_humidity(adc_H)

            return self.temperature, self.humidity, self.pressure

        except Exception as e:
            logger.error(f"BME280 read error: {e}")
            return self.temperature, self.humidity, self.pressure

    def close(self):
        if self._bus:
            try:
                self._bus.close()
            except Exception:
                pass
