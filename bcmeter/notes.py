"""Thread-safe per-cycle notes accumulator.

Port of ESP32's Notes namespace from notes.cpp.
Accumulates short diagnostic codes during a measurement cycle,
then drains them into the CSV row's 'notice' column.
"""

import threading

# Standard note codes (matching ESP32)
LED_REC = "LED_REC"
FLOW_REC = "FLOW_REC"
FLOW_BUMP = "FLOW_BUMP"
I2C_TO = "I2C_TO"
SHT_F = "SHT_F"
SPS_F = "SPS_F"
FL_0 = "FL_0"
FL_LO = "FL_LO"
ADC_LO = "ADC_LO"
ADC_HI = "ADC_HI"
OT = "OT"
ATN_HI = "ATN_HI"
PUMP_F = "PUMP_F"
HUM_HI = "HUM_HI"
TIME_SYNC = "TIME_SYNC"

_lock = threading.Lock()
_codes: list = []


def add(code: str):
    """Append a diagnostic code for the current cycle."""
    with _lock:
        _codes.append(code)


def drain() -> str:
    """Return all accumulated codes as a comma-separated string and clear."""
    with _lock:
        if not _codes:
            return ""
        out = ",".join(_codes)
        _codes.clear()
        return out


def has_notes() -> bool:
    with _lock:
        return len(_codes) > 0
