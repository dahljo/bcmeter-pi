"""Thread-safe shared system state.

Replaces scattered /dev/shm/bcmeter/ file-based state and calibration_data.json
with an in-memory singleton protected by a lock.
"""

import threading
from dataclasses import dataclass, field, asdict

from .errors import ErrorCode, InitStep


@dataclass
class SystemState:
    # Hardware detection
    adc_present: bool = False
    adc_type: str = ""  # "i2c" or "spi"
    sht4x_present: bool = False
    bme280_present: bool = False
    sps30_present: bool = False
    gps_present: bool = False
    modem_present: bool = False
    ota_available: bool = False
    wifi_enabled: bool = True
    debug_mode: bool = False

    # Measurement state
    sampling: bool = False
    error: ErrorCode = ErrorCode.ERR_NONE
    init_step: InitStep = InitStep.INIT_IDLE

    # Set when the wall clock is abruptly changed (manual sync from the
    # browser).  Consumed by the measure task at cycle top to emit a
    # TIME_SYNC note on the next row so a timestamp jump in the CSV is
    # traceable to a sync event.
    time_just_synced: bool = False

    # Last measured values
    last_bc: float = 0.0
    last_atn: float = 0.0
    last_flow: float = 0.0
    last_sen: float = 0.0
    last_ref: float = 0.0
    last_pm25: float = 0.0
    last_pm10: float = 0.0
    last_temp: float = 0.0
    last_humidity: float = 0.0
    last_pressure: float = 0.0

    # Pump health (0=ok, 1=recovering, 2=failed)
    flow_health: int = 0

    # Warnings (non-fatal, device keeps running)
    warning_msg: str = ""

    # Session tracking
    filter_status: int = 5
    sample_count: int = 0
    session_avg_bc: float = 0.0
    hour_avg_bc: float = 0.0

    # Calibration
    last_cal_time: str = "never"

    # Network
    wifi_mode: str = "sta"  # "sta" or "ap"
    wifi_ssid: str = ""
    wifi_rssi: int = 0
    internet: bool = False
    in_hotspot: bool = False


class StateManager:
    """Thread-safe wrapper around SystemState."""

    def __init__(self):
        self._state = SystemState()
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            return getattr(self._state, key)

    def set(self, key: str, value):
        with self._lock:
            setattr(self._state, key, value)

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._state, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            d = asdict(self._state)
            d["error"] = int(self._state.error)
            d["init_step"] = int(self._state.init_step)
            return d

    @property
    def sampling(self) -> bool:
        with self._lock:
            return self._state.sampling

    @sampling.setter
    def sampling(self, val: bool):
        with self._lock:
            self._state.sampling = val


# Global singleton
state = StateManager()
