"""SHT4x temperature/humidity sensor driver via raw Linux I2C."""

import fcntl
import logging
import os
import statistics
import time
import threading

logger = logging.getLogger("bcmeter.sht4x")

I2C_SLAVE = 0x0703


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


class SHT4x:
    """SHT40/SHT41/SHT45 temperature/humidity sensor via raw I2C."""

    ADDR = 0x44
    CMD_MEASURE_LOW = 0xE0  # Low precision, no heater. Fast and sufficient for env compensation.
    CMD_RESET = 0x94
    READ_LEN = 6
    MEASURE_DELAY_S = 0.003
    STARTUP_TIMEOUT_S = 0.25
    READ_TIMEOUT_S = 0.15
    STARTUP_ATTEMPTS = 100
    READ_ATTEMPTS = 50
    SAMPLE_INTERVAL_S = 5.0

    def __init__(self, i2c_lock: threading.Lock = None, bus_num: int = 1):
        self._lock = i2c_lock or threading.Lock()
        self._bus_num = bus_num
        self._fd = None
        self._present = False
        self._stop_event = threading.Event()
        self._thread = None
        self._last_sample_s = 0.0
        self.temperature = 0.0
        self.humidity = 0.0

    @property
    def present(self) -> bool:
        return self._present

    def init(self) -> bool:
        """Detect and initialize SHT4x sensor."""
        try:
            with self._lock:
                self._open_locked()
                self._reset_locked()
                sample = self._sample_locked(
                    max_attempts=self.STARTUP_ATTEMPTS,
                    timeout_s=self.STARTUP_TIMEOUT_S,
                    target_valid=1,
                )
            if sample is None:
                logger.debug("SHT4x not found: no valid CRC-checked measurement")
                self.close()
                return False

            self._set_sample(sample)
            self._present = True
            self._start_sampler()
            logger.info("SHT4x temperature/humidity sensor detected")
            return True
        except Exception as e:
            logger.debug(f"SHT4x not found: {e}")
            self._present = False
            self.close()
            return False

    def read(self) -> tuple:
        """Return the latest temperature (°C) and humidity (%RH)."""
        return self.temperature, self.humidity

    def _open_locked(self):
        if self._fd is not None:
            return
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        self._fd = os.open(f"/dev/i2c-{self._bus_num}", flags)
        fcntl.ioctl(self._fd, I2C_SLAVE, self.ADDR)

    def _reset_locked(self):
        try:
            os.write(self._fd, bytes([self.CMD_RESET]))
            time.sleep(0.002)
        except OSError:
            pass

    def _measure_once_locked(self):
        os.write(self._fd, bytes([self.CMD_MEASURE_LOW]))
        time.sleep(self.MEASURE_DELAY_S)
        data = os.read(self._fd, self.READ_LEN)
        if len(data) != self.READ_LEN:
            return None
        return self._decode_measurement(data)

    @staticmethod
    def _decode_measurement(data: bytes):
        if _crc8(data[0:2]) != data[2] or _crc8(data[3:5]) != data[5]:
            return None

        raw_temp = (data[0] << 8) | data[1]
        raw_hum = (data[3] << 8) | data[4]

        temperature = -45.0 + 175.0 * (raw_temp / 65535.0)
        humidity = -6.0 + 125.0 * (raw_hum / 65535.0)
        return temperature, max(0.0, min(100.0, humidity))

    def _sample_locked(self, max_attempts: int, timeout_s: float, target_valid: int):
        deadline = time.monotonic() + timeout_s
        attempts = 0
        samples = []

        while attempts < max_attempts and time.monotonic() < deadline and len(samples) < target_valid:
            attempts += 1
            try:
                sample = self._measure_once_locked()
            except OSError:
                continue
            if sample is None:
                continue
            samples.append(sample)

        if not samples:
            return None

        temps = [sample[0] for sample in samples]
        hums = [sample[1] for sample in samples]
        return statistics.median(temps), statistics.median(hums)

    def _set_sample(self, sample: tuple):
        self.temperature, self.humidity = sample
        self._last_sample_s = time.monotonic()

    def _start_sampler(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._sample_loop,
            name="sht4x-sampler",
            daemon=True,
        )
        self._thread.start()

    def _sample_loop(self):
        while not self._stop_event.wait(self.SAMPLE_INTERVAL_S):
            try:
                with self._lock:
                    if self._fd is None:
                        self._open_locked()
                    sample = self._sample_locked(
                        max_attempts=self.READ_ATTEMPTS,
                        timeout_s=self.READ_TIMEOUT_S,
                        target_valid=1,
                    )
                if sample is not None:
                    self._set_sample(sample)
            except Exception as e:
                logger.debug(f"SHT4x background sample error: {e}")

    def close(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None
        self._present = False
