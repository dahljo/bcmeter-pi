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
    warmup_started_monotonic: float = 0.0
    warmup_end_monotonic: float = 0.0
    warmup_progress: int = 0

    # Set when the wall clock is abruptly changed (manual sync from the
    # browser or the first successful NTP synchronization).  The offset is
    # the amount added to the old wall clock.  Both values are consumed
    # atomically by the measure task at cycle top.
    time_just_synced: bool = False
    time_sync_offset_seconds: float = 0.0
    logging_session_active: bool = False
    session_time_synced: bool = False

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
    calibration_running: bool = False

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
        self._time_sync_events = []

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

    def mark_time_synced(self, offset_seconds: float, **metadata):
        """Publish one wall-clock synchronization event atomically."""
        with self._lock:
            self._mark_time_synced_locked(offset_seconds, metadata)

    def mark_time_synced_if_logging(self, offset_seconds: float, **metadata) -> bool:
        """Publish a sync event only for an already-open log session."""
        with self._lock:
            if not self._state.logging_session_active:
                return False
            self._mark_time_synced_locked(offset_seconds, metadata)
            return True

    def mark_ntp_synced_if_needed(self, offset_seconds: float, **metadata) -> bool:
        """Publish first-NTP correction only for a provisional-time session.

        The NTP monitor may observe the status transition after a new session
        has already started on the corrected clock.  ``session_time_synced``
        distinguishes that case from a log that was actually opened while
        the clock was still provisional.
        """
        with self._lock:
            if (not self._state.logging_session_active
                    or self._state.session_time_synced):
                return False
            self._mark_time_synced_locked(offset_seconds, metadata)
            return True

    def _mark_time_synced_locked(self, offset_seconds: float, metadata=None):
        event = {"offset_seconds": float(offset_seconds)}
        event.update(metadata or {})
        self._time_sync_events.append(event)
        self._state.time_sync_offset_seconds = self._time_sync_events[0]["offset_seconds"]
        self._state.time_just_synced = True
        if self._state.logging_session_active:
            # Freeze delivery immediately; corrected timestamps must replace
            # the pending local view before another payload is created.
            self._state.session_time_synced = False

    def begin_logging_session(self, time_synced: bool):
        with self._lock:
            self._state.logging_session_active = True
            self._state.session_time_synced = bool(time_synced)

    def close_logging_session(self):
        """Atomically close the sync-publication gate and drain late events."""
        with self._lock:
            self._state.logging_session_active = False
            events = list(self._time_sync_events)
            self._time_sync_events.clear()
            self._state.time_just_synced = False
            self._state.time_sync_offset_seconds = 0.0
            return events

    def consume_time_sync_event(self):
        with self._lock:
            if not self._time_sync_events:
                return None
            event = self._time_sync_events.pop(0)
            if self._time_sync_events:
                self._state.time_sync_offset_seconds = self._time_sync_events[0][
                    "offset_seconds"
                ]
                self._state.time_just_synced = True
            else:
                self._state.time_sync_offset_seconds = 0.0
                self._state.time_just_synced = False
            return event

    def consume_time_sync(self):
        """Consume a pending time-sync event, returning its offset or None."""
        event = self.consume_time_sync_event()
        return None if event is None else event["offset_seconds"]

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
