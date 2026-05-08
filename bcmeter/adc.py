"""ADC abstraction layer for MCP342X (I2C) and ADS8344 (SPI).

Provides a unified interface for reading sensor and reference voltages
regardless of underlying hardware.
"""

import logging
import threading
import time

logger = logging.getLogger("bcmeter.adc")

# MCP342X I2C register constants
MCP342X_DEFAULT_ADDRESS = 0x68
MCP342X_CONFIG_READY = 0x80
MCP342X_CONFIG_MODE_ONESHOT = 0x00
MCP342X_CONFIG_MODE_CONTINUOUS = 0x10
MCP342X_CONFIG_CH1 = 0x00
MCP342X_CONFIG_CH2 = 0x20
MCP342X_CONFIG_CH3 = 0x40
MCP342X_CONFIG_CH4 = 0x60
MCP342X_CONFIG_SPS_240_12BIT = 0x00
MCP342X_CONFIG_SPS_60_14BIT = 0x04
MCP342X_CONFIG_SPS_15_16BIT = 0x08
MCP342X_CONFIG_GAIN_1X = 0x00
VOLTAGE_REFERENCE = 2.048

# Sample rate -> (max_conversion_time_s, bit_depth, read_bytes)
_RATE_INFO = {
    MCP342X_CONFIG_SPS_240_12BIT: (1.0 / 240, 12, 3),
    MCP342X_CONFIG_SPS_60_14BIT:  (1.0 / 60,  14, 3),
    MCP342X_CONFIG_SPS_15_16BIT:  (1.0 / 15,  16, 3),
}


class ADS8344:
    """SPI ADC driver for ADS8344 (18-bit)."""

    START_BIT = 0x80
    SINGLE_END = 0x04
    CLOCK_INTERNAL = 0x02
    CHANNELS = {
        0: 0x00, 1: 0x04, 2: 0x01, 3: 0x05,
        4: 0x02, 5: 0x06, 6: 0x03, 7: 0x07,
    }

    def __init__(self, bus=0, device=0, vref=4.096, busy_pin=None):
        self.vref = vref
        self.busy_pin = busy_pin
        self.initialized = False
        self._lock = threading.Lock()

        try:
            import spidev
            self.spi = spidev.SpiDev()
            self.spi.open(bus, device)
            self.spi.max_speed_hz = 1000000
            self.spi.mode = 0
            if self.busy_pin is not None:
                import RPi.GPIO as GPIO
                GPIO.setup(self.busy_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.initialized = True
        except Exception as e:
            logger.error(f"Failed to initialize SPI ADC: {e}")

    def close(self):
        if hasattr(self, "spi") and self.spi is not None:
            try:
                self.spi.close()
            except Exception:
                pass

    def read_channel(self, channel):
        """Read a single channel. Returns (voltage, raw_bytes) or (-1, None)."""
        if not self.initialized:
            return -1, None
        try:
            channel_bits = (self.CHANNELS[channel] & 0x7) << 4
            cmd = self.START_BIT | self.SINGLE_END | channel_bits | self.CLOCK_INTERNAL

            with self._lock:
                self.spi.writebytes([cmd])

                if self.busy_pin is not None:
                    import RPi.GPIO as GPIO
                    timeout_start = time.time()
                    while GPIO.input(self.busy_pin) == GPIO.LOW:
                        if time.time() - timeout_start > 0.002:
                            break
                else:
                    time.sleep(0.00005)

                result = self.spi.readbytes(3)
            value = (result[0] << 9) | (result[1] << 1) | (result[2] >> 7)
            voltage = (value / 65536.0) * self.vref
            return voltage, result
        except Exception as e:
            logger.error(f"SPI read error: {e}")
            return -1, None


class ADC:
    """Unified ADC interface abstracting I2C (MCP342X) and SPI (ADS8344).

    Usage:
        adc = ADC(i2c_lock)
        if adc.detect():
            sen, ref = adc.read_interleaved()
    """

    def __init__(self, i2c_lock: threading.Lock = None):
        self._lock = i2c_lock or threading.Lock()
        self._type = None  # "i2c" or "spi"
        self._i2c_bus = None
        self._i2c_addr = MCP342X_DEFAULT_ADDRESS
        self._spi_adc = None
        self._swap_channels = False
        self._adc_rate = MCP342X_CONFIG_SPS_60_14BIT
        self._adc_gain = MCP342X_CONFIG_GAIN_1X
        self._spi_trim_count = 10

    @property
    def type(self) -> str:
        return self._type or ""

    @property
    def present(self) -> bool:
        return self._type is not None

    @property
    def vref(self) -> float:
        """Voltage reference of the active ADC."""
        if self._type == "spi" and self._spi_adc:
            return self._spi_adc.vref
        if self._type == "i2c":
            return VOLTAGE_REFERENCE
        return 0.0

    @property
    def high_limit(self) -> float:
        """Maximum usable ADC voltage (~93% of Vref)."""
        return self.vref * 0.93

    def detect(self, swap_channels=False, adc_rate=None, adc_gain=None,
               spi_vref=4.096) -> bool:
        """Scan for ADC hardware. Try I2C first, then SPI.

        Args:
            swap_channels: Swap sensor/reference channel assignments.
            adc_rate: MCP342X sample rate constant (default 14-bit/60 SPS).
            adc_gain: MCP342X gain constant (default 1x).
            spi_vref: ADS8344 voltage reference (default 4.096V).
        """
        self._swap_channels = swap_channels
        if adc_rate is not None:
            self._adc_rate = adc_rate
        if adc_gain is not None:
            self._adc_gain = adc_gain

        # Try I2C MCP342X
        if self._detect_i2c():
            self._type = "i2c"
            logger.info("I2C ADC (MCP342X) found at 0x%02x", self._i2c_addr)
            return True

        # Try SPI ADS8344
        if self._detect_spi(spi_vref):
            self._type = "spi"
            logger.info("SPI ADC (ADS8344) detected")
            return True

        logger.error("No ADC found")
        return False

    def _detect_i2c(self) -> bool:
        try:
            import smbus
            bus = smbus.SMBus(1)
            for addr in [0x68, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F]:
                try:
                    bus.read_byte(addr)
                    self._i2c_bus = bus
                    self._i2c_addr = addr
                    return True
                except Exception:
                    continue
            # No MCP342X found — close the bus
            try:
                bus.close()
            except Exception:
                pass
        except Exception as e:
            logger.debug("I2C ADC detection failed: %s", e)
        return False

    def _detect_spi(self, vref=4.096) -> bool:
        try:
            adc = ADS8344(bus=0, device=0, vref=vref, busy_pin=None)
            voltage, result = adc.read_channel(0)
            if result is not None:
                # Verify with a few more reads
                for _ in range(3):
                    v, r = adc.read_channel(0)
                    if r is None:
                        adc.close()
                        return False
                self._spi_adc = adc
                return True
            adc.close()
        except Exception as e:
            logger.debug("SPI ADC detection failed: %s", e)
        return False

    def read_i2c_channel(self, channel_config):
        """Read a single I2C ADC channel (one-shot mode with ready-bit polling).

        Returns voltage as float, or 0.0 on error.
        """
        if self._i2c_bus is None:
            return 0.0

        rate = self._adc_rate
        gain = self._adc_gain
        max_conv_time, n_bits, read_bytes = _RATE_INFO.get(
            rate, (1.0 / 60, 14, 3)
        )

        config_byte = (
            MCP342X_CONFIG_READY
            | channel_config
            | MCP342X_CONFIG_MODE_ONESHOT
            | rate
            | gain
        )

        with self._lock:
            try:
                self._i2c_bus.write_byte(self._i2c_addr, config_byte)
            except OSError as e:
                logger.error("I2C write error on channel 0x%02x: %s",
                             channel_config, e)
                return 0.0

            # Wait for conversion
            time.sleep(max_conv_time * 1.1)

            # Poll for ready bit (cleared = conversion done)
            timeout = max_conv_time * 3
            start = time.time()
            data = None
            while (time.time() - start) < timeout:
                try:
                    data = self._i2c_bus.read_i2c_block_data(
                        self._i2c_addr, 0x00, read_bytes
                    )
                    if not (data[-1] & MCP342X_CONFIG_READY):
                        break
                    time.sleep(0.002)
                except OSError:
                    time.sleep(0.005)
            else:
                # Timed out waiting for conversion
                return 0.0

            if data is None:
                return 0.0

            try:
                raw_value = (data[0] << 8) | data[1]
                if raw_value >= (1 << (n_bits - 1)):
                    raw_value -= (1 << n_bits)
                voltage = (2 * VOLTAGE_REFERENCE * raw_value) / (1 << n_bits)
                return max(0.0, voltage)
            except Exception as e:
                logger.error("I2C conversion error: %s", e)
                return 0.0

    def read_sensor(self) -> float:
        """Read the sensor (main photodiode) channel."""
        if self._type == "spi":
            ch = 1 if self._swap_channels else 0
            return self._read_spi_trimmed(ch)
        elif self._type == "i2c":
            ch = MCP342X_CONFIG_CH2 if self._swap_channels else MCP342X_CONFIG_CH1
            return self.read_i2c_channel(ch)
        return 0.0

    def read_reference(self) -> float:
        """Read the reference channel."""
        if self._type == "spi":
            ch = 0 if self._swap_channels else 1
            return self._read_spi_trimmed(ch)
        elif self._type == "i2c":
            ch = MCP342X_CONFIG_CH1 if self._swap_channels else MCP342X_CONFIG_CH2
            return self.read_i2c_channel(ch)
        return 0.0

    def _read_spi_trimmed(self, channel: int, count: int = None) -> float:
        """Read an ADS8344 channel using the legacy settle + trimmed mean path."""
        if self._spi_adc is None:
            return 0.0

        vals = []
        read_count = max(1, count or self._spi_trim_count)
        self._spi_adc.read_channel(channel)  # dummy after channel switch
        time.sleep(0.002)

        for _ in range(read_count):
            v, _ = self._spi_adc.read_channel(channel)
            if v >= 0:
                vals.append(v)
            time.sleep(0.001)

        if not vals:
            return 0.0

        vals.sort()
        trim = int(len(vals) * 0.2)
        if trim > 0 and len(vals) > trim * 2:
            vals = vals[trim:-trim]
        return sum(vals) / len(vals)

    def read_interleaved(self, samples=1, duration_s=None) -> tuple:
        """Read sensor and reference as a pair, optionally averaged over time.

        Returns (sensor_avg, reference_avg).
        """
        if self._type == "spi" and self._spi_adc is not None:
            sensor_ch = 1 if self._swap_channels else 0
            ref_ch = 0 if self._swap_channels else 1

            if duration_s is not None:
                sen_vals = []
                ref_vals = []
                end_time = time.time() + duration_s
                while time.time() < end_time:
                    sen_vals.append(self._read_spi_trimmed(sensor_ch))
                    ref_vals.append(self._read_spi_trimmed(ref_ch))
                if not sen_vals or not ref_vals:
                    return 0.0, 0.0
                return sum(sen_vals) / len(sen_vals), sum(ref_vals) / len(ref_vals)

            count = max(1, samples)
            return self._read_spi_trimmed(sensor_ch, count), self._read_spi_trimmed(ref_ch, count)

        sen_sum = 0.0
        ref_sum = 0.0
        count = 0

        if duration_s is not None:
            end_time = time.time() + duration_s
            while time.time() < end_time:
                sen_sum += self.read_sensor()
                ref_sum += self.read_reference()
                count += 1
        else:
            for _ in range(samples):
                sen_sum += self.read_sensor()
                ref_sum += self.read_reference()
                count += 1

        if count == 0:
            return 0.0, 0.0
        return sen_sum / count, ref_sum / count

    def read_airflow_voltage(self) -> float:
        """Read the airflow sensor channel (channel 3 on I2C, channel 2 on SPI)."""
        if self._type == "spi":
            v, _ = self._spi_adc.read_channel(2)
            return max(0.0, v) if v >= 0 else 0.0
        elif self._type == "i2c":
            return self.read_i2c_channel(MCP342X_CONFIG_CH3)
        return 0.0

    def close(self):
        """Release hardware resources."""
        if self._spi_adc:
            self._spi_adc.close()
            self._spi_adc = None
        if self._i2c_bus:
            try:
                self._i2c_bus.close()
            except Exception:
                pass
            self._i2c_bus = None
        self._type = None
