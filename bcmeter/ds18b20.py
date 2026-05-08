"""DS18B20 1-Wire temperature sensor (Pi-only, no ESP32 equivalent)."""

import logging
import re

logger = logging.getLogger("bcmeter.ds18b20")


class DS18B20:
    """DS18B20 1-Wire temperature sensor (fallback if no SHT4x)."""

    def __init__(self):
        self._present = False
        self._device_file = None
        self.temperature = 0.0

    @property
    def present(self) -> bool:
        return self._present

    def init(self) -> bool:
        """Detect DS18B20 via 1-Wire filesystem."""
        import glob as glob_mod
        try:
            devices = glob_mod.glob("/sys/bus/w1/devices/28*/w1_slave")
            if devices:
                self._device_file = devices[0]
                self._present = True
                self.read()
                logger.info("DS18B20 temperature sensor detected")
                return True
        except Exception as e:
            logger.debug(f"DS18B20 not found: {e}")
        self._present = False
        return False

    def read(self) -> float:
        """Read temperature in C."""
        if not self._present or not self._device_file:
            return self.temperature
        try:
            with open(self._device_file, "r") as f:
                lines = [line.strip() for line in f.readlines()]
            if len(lines) >= 2 and lines[0].endswith("YES"):
                match = re.search(r"t=(\d+)", lines[1])
                if match:
                    self.temperature = int(match.group(1)) / 1000.0
            return self.temperature
        except Exception as e:
            logger.error(f"DS18B20 read error: {e}")
            return self.temperature
