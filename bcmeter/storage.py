"""CSV session management for measurement data.

Handles log file creation, data appending, and session lifecycle.
"""

import csv
import logging
import os
import time
from datetime import datetime
from dataclasses import dataclass, field, fields
from typing import Optional

logger = logging.getLogger("bcmeter.storage")


@dataclass
class MeasureRow:
    """Single measurement row matching the CSV format."""
    date: str = ""
    time_str: str = ""
    # Per-channel data (up to 3 wavelengths)
    ref_880nm: float = 0.0
    sen_880nm: float = 0.0
    atn_880nm: float = 0.0
    bc_unfiltered_880nm: float = 0.0
    bc_880nm: float = 0.0
    ref_520nm: float = 0.0
    sen_520nm: float = 0.0
    atn_520nm: float = 0.0
    bc_unfiltered_520nm: float = 0.0
    bc_520nm: float = 0.0
    ref_370nm: float = 0.0
    sen_370nm: float = 0.0
    atn_370nm: float = 0.0
    bc_unfiltered_370nm: float = 0.0
    bc_370nm: float = 0.0
    # Derived
    relative_load: float = 0.0
    aae: float = 0.0
    # Environment
    temperature: float = 0.0
    humidity: float = 0.0
    airflow: float = 0.0
    pressure: float = 0.0
    # Particulates
    pm25: float = 0.0
    pm10: float = 0.0
    # Meta
    sample_duration: float = 0.0
    notice: str = ""
    # Location
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    # Pump diagnostics
    pump_duty: int = 0


# CSV header matching existing Python format (semicolon-delimited)
def _build_header(num_channels: int = 1, has_sps30: bool = False,
                   log_pump_duty: bool = False) -> list:
    """Build CSV header columns based on hardware config."""
    wls = ["880nm", "520nm", "370nm"][:num_channels]
    cols = ["bcmDate", "bcmTime"]
    for wl in wls:
        cols.extend([
            f"bcmRef_{wl}", f"bcmSen_{wl}", f"bcmATN_{wl}",
            f"BCngm3_unfiltered_{wl}", f"BCngm3_{wl}",
        ])
    cols.extend(["relativeLoad", "AAE", "Temperature", "notice", "sampleDuration", "humidity", "airflow", "hPa"])
    if has_sps30:
        cols.extend(["PM2.5", "PM10"])
    if log_pump_duty:
        cols.append("pumpDuty")
    cols.append("notes")
    return cols


_base_dir = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
_SESSION_FLAG_FILE = os.path.join(_base_dir, ".bcmeter_session_running")


def was_session_running() -> bool:
    """Check if a session was active before power loss."""
    return os.path.exists(_SESSION_FLAG_FILE)


def _set_session_flag(running: bool):
    """Persist session-running state across power cycles."""
    if running:
        try:
            with open(_SESSION_FLAG_FILE, "w") as f:
                f.write("1")
        except Exception:
            pass
    else:
        try:
            os.remove(_SESSION_FLAG_FILE)
        except FileNotFoundError:
            pass


class Storage:
    """Manages CSV log sessions."""

    def __init__(self, log_dir: str, num_channels: int = 1, has_sps30: bool = False,
                 log_pump_duty: bool = False):
        self._log_dir = log_dir
        self._num_channels = num_channels
        self._has_sps30 = has_sps30
        self._log_pump_duty = log_pump_duty
        self._session_file: Optional[str] = None
        self._session_active = False
        self._header = _build_header(num_channels, has_sps30, log_pump_duty)
        self._row_count = 0
        self._current_link = os.path.join(log_dir, "log_current.csv")

        os.makedirs(log_dir, exist_ok=True)

    @property
    def log_dir(self) -> str:
        """Base directory for log files."""
        return self._log_dir

    def start_session(self) -> str:
        """Create a new log session file. Returns the filename."""
        ts = datetime.now().strftime("%d-%m-%y_%H%M%S")
        filename = f"{ts}.csv"
        filepath = os.path.join(self._log_dir, filename)

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(self._header)

        self._session_file = filepath
        self._session_active = True
        self._row_count = 0
        _set_session_flag(True)

        # Create/update symlink for log_current.csv
        try:
            if os.path.islink(self._current_link) or os.path.exists(self._current_link):
                os.remove(self._current_link)
            os.symlink(filepath, self._current_link)
        except Exception as e:
            logger.warning(f"Could not create current log link: {e}")

        logger.info(f"Session started: {filename}")
        return filename

    def end_session(self):
        """Close current session."""
        self._session_active = False
        _set_session_flag(False)
        logger.info(f"Session ended ({self._row_count} rows)")

    def append_row(self, row: MeasureRow):
        """Append a measurement row to the current session."""
        if not self._session_active or not self._session_file:
            return

        wls = ["880nm", "520nm", "370nm"][:self._num_channels]
        values = [row.date, row.time_str]

        for wl in wls:
            values.extend([
                f"{getattr(row, f'ref_{wl}', 0):.6f}",
                f"{getattr(row, f'sen_{wl}', 0):.6f}",
                f"{getattr(row, f'atn_{wl}', 0):.5f}",
                f"{getattr(row, f'bc_unfiltered_{wl}', 0):.0f}",
                f"{getattr(row, f'bc_{wl}', 0):.0f}",
            ])

        values.extend([
            f"{row.relative_load:.6f}",
            f"{row.aae:.3f}",
            f"{row.temperature:.1f}",
            "",  # notice column (ESP32 parity: always empty, annotations go to notes)
            f"{row.sample_duration:.1f}",
            f"{row.humidity:.0f}",
            f"{row.airflow:.3f}",
            f"{row.pressure:.1f}" if row.pressure > 0 else "",
        ])

        if self._has_sps30:
            values.extend([
                f"{row.pm25:.2f}",
                f"{row.pm10:.2f}",
            ])

        if self._log_pump_duty:
            values.append(str(row.pump_duty))

        # Notes column (always last, ESP32 parity)
        values.append(row.notice)

        try:
            with open(self._session_file, "a", newline="") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(values)
            self._row_count += 1
        except Exception as e:
            logger.error(f"Failed to write row: {e}")

    @property
    def session_active(self) -> bool:
        return self._session_active

    @property
    def session_filename(self) -> Optional[str]:
        if self._session_file:
            return os.path.basename(self._session_file)
        return None

    @property
    def session_filepath(self) -> Optional[str]:
        return self._session_file

    @property
    def row_count(self) -> int:
        return self._row_count

    def list_logs(self) -> list:
        """List all CSV log files sorted by modification time (newest first).

        Skips header-only files (< 200 bytes) as they contain no useful data.
        Includes a ``lines`` count for each file.
        """
        try:
            files = []
            for f in os.listdir(self._log_dir):
                if f.endswith(".csv") and f != "log_current.csv":
                    path = os.path.join(self._log_dir, f)
                    size = os.path.getsize(path)
                    if size < 200:
                        continue
                    mtime = os.path.getmtime(path)
                    try:
                        lines = 0
                        with open(path, "r") as fh:
                            for _ in fh:
                                lines += 1
                                if lines >= 3:
                                    break
                    except Exception:
                        lines = 0
                    files.append({"name": f, "size": size, "mtime": mtime, "lines": lines})
            files.sort(key=lambda x: x["mtime"], reverse=True)
            return files
        except Exception as e:
            logger.error(f"Failed to list logs: {e}")
            return []

    def read_log(self, filename: str) -> Optional[str]:
        """Read entire content of a log file."""
        filepath = os.path.join(self._log_dir, filename)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Failed to read log {filename}: {e}")
            return None

    def delete_old_logs(self, keep_count: int = 50):
        """Delete oldest logs keeping only `keep_count` files."""
        logs = self.list_logs()
        if len(logs) <= keep_count:
            return
        for entry in logs[keep_count:]:
            path = os.path.join(self._log_dir, entry["name"])
            try:
                os.remove(path)
                logger.info(f"Deleted old log: {entry['name']}")
            except Exception:
                pass
