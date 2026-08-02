"""CSV session management for measurement data.

Handles log file creation, data appending, and session lifecycle.
"""

import csv
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
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
    notes: str = ""
    # Location
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    # Pump diagnostics
    pump_duty: int = 0


# CSV header matching existing Python format (semicolon-delimited)
def _build_header(num_channels: int = 1, has_sps30: bool = False,
                   log_pump_duty: bool = False, has_gps: bool = False) -> list:
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
    if has_gps:
        cols.extend(["lat", "lon", "altitude"])
    if log_pump_duty:
        cols.append("pumpDuty")
    cols.append("notes")
    return cols


_base_dir = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
_SESSION_FLAG_FILE = os.path.join(_base_dir, ".bcmeter_session_running")
_MAX_SESSION_RECORDS = 8


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
        self._has_gps = False
        self._session_file: Optional[str] = None
        self._session_active = False
        self._session_filename_time_synced = False
        self._header = _build_header(num_channels, has_sps30, log_pump_duty)
        self._row_count = 0
        self._current_link = os.path.join(log_dir, "log_current.csv")
        self._file_lock = threading.RLock()
        self._session_generation = 0
        # In-memory provenance for each CSV timestamp.  A measurement row can
        # be created before a wall-clock step and appended afterwards; a plain
        # row-count boundary cannot classify that in-flight row correctly.
        # The monotonic capture time lets the sync event identify which wall
        # timeline produced every row without adding private columns to CSV.
        self._row_timestamp_monotonic = {}
        self._session_records = {}

        os.makedirs(log_dir, exist_ok=True)

    @property
    def log_dir(self) -> str:
        """Base directory for log files."""
        return self._log_dir

    def start_session(self, has_sps30: Optional[bool] = None,
                      has_gps: bool = False, time_synced: bool = False) -> str:
        """Create a new log session file. Returns the filename.

        Sensor-dependent header flags are re-evaluated per session (ESP32
        startSession parity); has_sps30=None keeps the constructor value."""
        with self._file_lock:
            self._current_session_record()
            if has_sps30 is not None:
                self._has_sps30 = has_sps30
            self._has_gps = has_gps
            self._header = _build_header(self._num_channels, self._has_sps30,
                                         self._log_pump_duty, self._has_gps)
            ts = datetime.now().strftime("%d-%m-%y_%H%M%S")
            suffix = 0
            while True:
                filename = f"{ts}.csv" if suffix == 0 else f"{ts}_session{suffix}.csv"
                filepath = os.path.join(self._log_dir, filename)
                try:
                    with open(filepath, "x", newline="") as f:
                        writer = csv.writer(f, delimiter=";", lineterminator="\n")
                        writer.writerow(self._header)
                    break
                except FileExistsError:
                    suffix += 1

            self._session_file = filepath
            try:
                if not time_synced:
                    with open(self._time_pending_path(filepath), "x") as marker:
                        marker.write("pending\n")
                self._ensure_current_link()
            except Exception:
                self._session_file = None
                try:
                    os.unlink(filepath)
                except FileNotFoundError:
                    pass
                try:
                    os.unlink(self._time_pending_path(filepath))
                except FileNotFoundError:
                    pass
                logger.exception("Could not create current log link")
                raise

            self._session_active = True
            self._session_filename_time_synced = bool(time_synced)
            self._session_generation += 1
            self._row_count = 0
            self._row_timestamp_monotonic = {}
            self._session_records[self._session_generation] = {
                "path": self._session_file,
                "row_count": 0,
                "row_timestamp_monotonic": self._row_timestamp_monotonic,
                "filename_time_synced": self._session_filename_time_synced,
            }
            self._prune_session_records()
            from .state import state
            state.begin_logging_session(time_synced)
            _set_session_flag(True)

        logger.info(f"Session started: {filename}")
        return filename

    def end_session(self):
        """Close current session."""
        with self._file_lock:
            if not self._session_active:
                return
            had_event, corrected = self.apply_pending_time_sync()
            if had_event and corrected is None:
                raise RuntimeError("Pending time synchronization could not be applied")

            from .state import state
            late_events = state.close_logging_session()
            for event_index, event in enumerate(late_events):
                corrected = self._apply_time_sync_event(
                    event,
                    final_event=event_index == len(late_events) - 1,
                )
                if corrected is None:
                    state.begin_logging_session(False)
                    for retry_event in late_events[event_index:]:
                        retry_event = dict(retry_event)
                        offset = retry_event.pop("offset_seconds")
                        state.mark_time_synced(offset, **retry_event)
                    raise RuntimeError("Late time synchronization could not be applied")

            if late_events:
                state.set("session_time_synced", True)

            self._session_active = False
            _set_session_flag(False)
            logger.info(f"Session ended ({self._row_count} rows)")

    def append_row(self, row: MeasureRow):
        """Append a measurement row to the current session."""
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
            row.notice,  # session markers (first row) / skip reason (ESP32 parity)
            f"{row.sample_duration:.1f}",
            f"{row.humidity:.0f}",
            f"{row.airflow:.3f}",
            f"{row.pressure:.1f}",
        ])

        if self._has_sps30:
            values.extend([
                f"{row.pm25:.2f}",
                f"{row.pm10:.2f}",
            ])

        if self._has_gps:
            values.extend([
                f"{row.latitude:.6f}",
                f"{row.longitude:.6f}",
                f"{row.altitude:.1f}",
            ])

        if self._log_pump_duty:
            values.append(str(row.pump_duty))

        # Notes column: per-cycle diagnostic codes, always last
        # (ESP32 appendRow parity, incl. its 99-char truncation)
        values.append(row.notes[:99])

        try:
            with self._file_lock:
                if not self._session_active or not self._session_file:
                    return False
                with open(self._session_file, "a", newline="") as f:
                    writer = csv.writer(f, delimiter=";", lineterminator="\n")
                    writer.writerow(values)
                self._row_count += 1
                timestamp_monotonic = getattr(row, "_timestamp_monotonic", None)
                if timestamp_monotonic is not None:
                    self._row_timestamp_monotonic[self._row_count] = float(
                        timestamp_monotonic,
                    )
                record = self._current_session_record()
                if record is not None:
                    record["row_count"] = self._row_count
            return True
        except Exception as e:
            logger.error(f"Failed to write row: {e}")
            return False

    def _ensure_current_link(self):
        """Atomically point log_current.csv at the active session file."""
        if not self._session_file:
            return
        tmp_link = os.path.join(
            self._log_dir,
            f".log_current.{os.getpid()}.{threading.get_ident()}.tmp",
        )
        try:
            try:
                os.unlink(tmp_link)
            except FileNotFoundError:
                pass
            os.symlink(self._session_file, tmp_link)
            os.replace(tmp_link, self._current_link)
        finally:
            try:
                os.unlink(tmp_link)
            except FileNotFoundError:
                pass

    @staticmethod
    def _time_pending_path(session_path: str) -> str:
        return session_path + ".time_pending"

    def _clear_time_pending_markers(self, *paths: str):
        for path in paths:
            if not path:
                continue
            try:
                os.unlink(self._time_pending_path(path))
            except FileNotFoundError:
                pass

    def _ensure_time_pending_marker(self, session_path: Optional[str] = None):
        """Persist that a log must not be delivered yet."""
        target_path = session_path or self._session_file
        if not target_path:
            return
        marker_path = self._time_pending_path(target_path)
        try:
            with open(marker_path, "x") as marker:
                marker.write("pending\n")
        except FileExistsError:
            pass

    def _current_session_record(self):
        """Return and refresh the record for the current generation."""
        if self._session_generation <= 0 or not self._session_file:
            return None
        record = self._session_records.setdefault(self._session_generation, {})
        record.update({
            "path": self._session_file,
            "row_count": self._row_count,
            "row_timestamp_monotonic": self._row_timestamp_monotonic,
            "filename_time_synced": self._session_filename_time_synced,
        })
        return record

    def _prune_session_records(self):
        """Bound retained row provenance while covering delayed sync events.

        NTP publication is delayed by at most one poll and manual ``date`` by
        its short subprocess timeout. Eight complete session generations are
        therefore a deliberately generous race window without an unbounded
        per-row memory cost on long-running devices.
        """
        generations = sorted(self._session_records)
        for generation in generations[:-_MAX_SESSION_RECORDS]:
            del self._session_records[generation]

    def _corrected_session_path(
            self, offset_seconds: int,
            session_path: Optional[str] = None) -> Optional[str]:
        """Return a collision-free, timestamp-corrected session path."""
        target_path = session_path or self._session_file
        if not target_path:
            return None
        stem, extension = os.path.splitext(os.path.basename(target_path))
        try:
            # A prior correction may already have added a collision suffix
            # such as ``_timesync1``.  The leading timestamp remains the
            # authoritative session start for any later clock adjustment.
            old_start = datetime.strptime(stem[:15], "%d-%m-%y_%H%M%S")
        except ValueError:
            return None
        corrected_stem = (old_start + timedelta(seconds=offset_seconds)).strftime(
            "%d-%m-%y_%H%M%S",
        )
        candidate = os.path.join(self._log_dir, corrected_stem + extension)
        if candidate == target_path:
            return candidate
        suffix = 1
        while os.path.lexists(candidate):
            candidate = os.path.join(
                self._log_dir,
                f"{corrected_stem}_timesync{suffix}{extension}",
            )
            suffix += 1
        return candidate

    def _rename_session_for_offset(
            self, session_path: str, offset_seconds: int,
            update_current_link: bool = False) -> str:
        """Correct one timestamped filename without overwriting another log."""
        old_path = session_path
        new_path = self._corrected_session_path(offset_seconds, old_path)
        if not old_path or not new_path or new_path == old_path:
            if update_current_link:
                self._ensure_current_link()
            return old_path

        # link(2) fails rather than overwriting if an external writer wins the
        # collision race.  Switch log_current atomically before removing the
        # old name, and roll back cleanly if the switch fails.
        os.link(old_path, new_path)
        if update_current_link:
            self._session_file = new_path
            try:
                self._ensure_current_link()
            except Exception:
                self._session_file = old_path
                os.unlink(new_path)
                raise
        try:
            os.unlink(old_path)
        except OSError:
            logger.warning("Corrected session retained duplicate old name: %s", old_path)
        return new_path

    def _rename_active_session_for_offset(self, offset_seconds: int):
        """Correct the active/current filename and update its stable record."""
        if not self._session_file:
            return
        new_path = self._rename_session_for_offset(
            self._session_file,
            offset_seconds,
            update_current_link=True,
        )
        record = self._current_session_record()
        if record is not None:
            record["path"] = new_path

    def capture_time_sync_boundary(self) -> dict:
        """Capture the last fully appended row for an atomic clock-step event."""
        with self._file_lock:
            if not self._session_active or not self._session_file:
                return {}
            return {
                "session_file": self._session_file,
                "session_generation": self._session_generation,
                "row_limit": self._row_count,
            }

    def handle_time_sync_event(
            self, offset_seconds: float, source: str = "manual", **metadata) -> bool:
        """Bind a clock step atomically to the matching log generation.

        The event can arrive after ``end_session`` because NTP status is
        polled.  Holding the storage lock here makes event publication and
        session close a transaction: an active match is queued for the
        measurement thread, while a just-closed match is corrected directly.
        """
        from .state import state

        with self._file_lock:
            current_record = self._current_session_record()
            event = dict(metadata)
            event_generation = event.get("session_generation")
            generation_matches = (
                event_generation is not None
                and int(event_generation) == self._session_generation
            )

            # The captured generation may have closed and been replaced before
            # NTP publishes the step. Correct that retained file first; never
            # redirect log_current away from a newer active generation.
            if event_generation is not None and not generation_matches:
                target_record = self._session_records.get(int(event_generation))
                if target_record is not None:
                    retained_event = dict(event)
                    retained_event["offset_seconds"] = float(offset_seconds)
                    retained_event["correct_session_filename"] = (
                        source != "ntp"
                        or not target_record.get("filename_time_synced", False)
                    )
                    corrected = self._apply_time_sync_event_to_record(
                        retained_event,
                        target_record,
                        final_event=True,
                        update_current_session=False,
                    )
                    if corrected is None:
                        logger.error(
                            "Could not apply time sync to retained session generation %s",
                            event_generation,
                        )
                        return False

            if self._session_active and self._session_file:
                provisional_session = not self._session_filename_time_synced
                if not generation_matches and not provisional_session:
                    # The clock stepped before this already-valid generation
                    # started; publication merely arrived afterwards.
                    logger.info("Ignoring late clock-step observation")
                    return True

                if event_generation is None or not generation_matches:
                    # The session opened after the monitor's last unsynced
                    # boundary.  A provisional session still belongs to this
                    # clock epoch; classify every row by monotonic provenance.
                    event.update({
                        "session_file": self._session_file,
                        "session_generation": self._session_generation,
                        "row_limit": 0,
                    })

                start_generation = event.get("row_start_session_generation")
                if start_generation is None \
                        or int(start_generation) != self._session_generation:
                    event["row_start_exclusive"] = 0

                # Initial provisional/manual corrections also fix the
                # timestamped filename.  A later NTP loss/re-sync only changes
                # rows in that unsynced epoch; the session start was correct.
                event["correct_session_filename"] = (
                    source != "ntp" or provisional_session
                )
                self._ensure_time_pending_marker()
                state.mark_time_synced(offset_seconds, **event)
                return True

            # A boundary captured for this generation remains authoritative
            # even if the session closed before the clock step was published.
            if self._session_file and generation_matches:
                event["offset_seconds"] = float(offset_seconds)
                start_generation = event.get("row_start_session_generation")
                if start_generation is None \
                        or int(start_generation) != self._session_generation:
                    event["row_start_exclusive"] = 0
                event["correct_session_filename"] = (
                    source != "ntp" or not self._session_filename_time_synced
                )
                corrected = self._apply_time_sync_event_to_record(
                    event,
                    current_record,
                    final_event=True,
                    update_current_session=True,
                )
                if corrected is None:
                    logger.error("Could not apply time sync to just-closed session")
                    return False
                state.set("session_time_synced", True)
                return True

            return True

    def handle_time_sync_lost(self, **metadata) -> bool:
        """Freeze delivery for a session that entered an unsynced NTP epoch."""
        from .state import state

        with self._file_lock:
            event_generation = metadata.get("session_generation")
            if event_generation is not None \
                    and int(event_generation) != self._session_generation:
                return True
            if not self._session_file:
                return True
            if self._session_active or event_generation is not None:
                self._ensure_time_pending_marker()
                if self._session_active:
                    state.set("session_time_synced", False)
            return True

    def _apply_time_sync_event(self, event: dict, final_event: bool = True):
        """Apply one clock event and invalidate delivery on success."""
        event_generation = event.get("session_generation")
        if event_generation is not None:
            if int(event_generation) != self._session_generation:
                logger.info("Ignoring time-sync event for a superseded session")
                return 0
        else:
            event_file = event.get("session_file")
            if event_file and self._session_file:
                if os.path.realpath(event_file) != os.path.realpath(self._session_file):
                    logger.info("Ignoring time-sync event for a superseded session")
                    return 0
        record = self._current_session_record()
        if record is None:
            return 0
        return self._apply_time_sync_event_to_record(
            event, record, final_event=final_event, update_current_session=True,
        )

    def _apply_time_sync_event_to_record(
            self, event: dict, record: dict, final_event: bool,
            update_current_session: bool) -> Optional[int]:
        """Apply an event to either the current or a retained closed record."""
        session_path = record.get("path")
        if not session_path:
            return 0
        self._ensure_time_pending_marker(session_path)
        corrected = self.correct_active_session_timestamps(
            event.get("offset_seconds", 0.0),
            row_limit=event.get("row_limit"),
            row_start_exclusive=event.get("row_start_exclusive"),
            old_local_cutoff=event.get("old_local_cutoff"),
            old_timeline_local=event.get("old_timeline_local"),
            old_timeline_monotonic=event.get("old_timeline_monotonic"),
            unsynced_start_monotonic=event.get("unsynced_start_monotonic"),
            correct_session_filename=event.get("correct_session_filename", True),
            clear_time_pending=final_event,
            _target_session_path=session_path,
            _row_timestamp_monotonic=record.get("row_timestamp_monotonic", {}),
            _update_current_session=update_current_session,
            _session_record=record,
        )
        if corrected is None:
            return None
        if event.get("correct_session_filename", True):
            record["filename_time_synced"] = True
            if update_current_session:
                self._session_filename_time_synced = True
        if corrected:
            from . import email_handler
            email_handler.reset_team_offset()
            email_handler.reset_log_mail_offset()
        return corrected

    def apply_pending_time_sync(self):
        """Consume and apply a pending clock step, retrying after I/O failure.

        Returns ``(False, 0)`` when there is no event, ``(True, count)`` on
        success (including a header-only count of zero), and ``(True, None)``
        when the event was requeued for a later retry.
        """
        from .state import state
        event = state.consume_time_sync_event()
        if event is None:
            return False, 0
        final_event = not state.get("time_just_synced")
        corrected = self._apply_time_sync_event(event, final_event=final_event)
        if corrected is None:
            retry_event = dict(event)
            offset = retry_event.pop("offset_seconds")
            state.mark_time_synced(offset, **retry_event)
        elif final_event:
            state.set("session_time_synced", True)
        return True, corrected

    def correct_active_session_timestamps(
            self, offset_seconds: float, row_limit: Optional[int] = None,
            row_start_exclusive: Optional[int] = None,
            old_local_cutoff: Optional[datetime] = None,
            old_timeline_local: Optional[datetime] = None,
            old_timeline_monotonic: Optional[float] = None,
            unsynced_start_monotonic: Optional[float] = None,
            correct_session_filename: bool = True,
            clear_time_pending: bool = True,
            _target_session_path: Optional[str] = None,
            _row_timestamp_monotonic: Optional[dict] = None,
            _update_current_session: bool = True,
            _session_record: Optional[dict] = None) -> Optional[int]:
        """Shift existing bcmDate/bcmTime values in the active CSV atomically.

        Returns the number of corrected data rows.  Unknown, zero/sub-second
        offsets and CSVs without the expected named timestamp columns are left
        untouched.
        """
        try:
            offset_whole_seconds = int(round(float(offset_seconds)))
        except (TypeError, ValueError, OverflowError):
            return 0
        if offset_whole_seconds == 0:
            with self._file_lock:
                target_path = _target_session_path or self._session_file
                if target_path and clear_time_pending:
                    self._clear_time_pending_markers(target_path)
            return 0

        with self._file_lock:
            target_path = _target_session_path or self._session_file
            if not target_path:
                return 0
            session_file = target_path
            row_timestamp_monotonic = (
                self._row_timestamp_monotonic
                if _row_timestamp_monotonic is None
                else _row_timestamp_monotonic
            )
            tmp_path = None
            try:
                with open(session_file, "r", newline="") as source:
                    rows = list(csv.reader(source, delimiter=";"))
                if not rows:
                    return 0
                header = rows[0]
                try:
                    date_index = header.index("bcmDate")
                    time_index = header.index("bcmTime")
                except ValueError:
                    logger.error("Cannot correct timestamps: CSV header has no bcmDate/bcmTime")
                    return None

                corrected = 0
                required_columns = max(date_index, time_index) + 1
                shift = timedelta(seconds=offset_whole_seconds)
                for row_number, row in enumerate(rows[1:], start=1):
                    if len(row) < required_columns:
                        continue
                    try:
                        old_timestamp = datetime.strptime(
                            f"{row[date_index]} {row[time_index]}",
                            "%d-%m-%y %H:%M:%S",
                        )
                    except (TypeError, ValueError):
                        continue
                    has_epoch_boundary = (
                        row_limit is not None or row_start_exclusive is not None
                    )
                    row_monotonic = row_timestamp_monotonic.get(row_number)
                    before_epoch = (
                        unsynced_start_monotonic is not None
                        and row_monotonic is not None
                        and row_monotonic <= float(unsynced_start_monotonic)
                    )
                    before_epoch_boundary = (
                        row_start_exclusive is not None
                        and row_number <= int(row_start_exclusive)
                    )
                    outside_epoch_start = before_epoch or before_epoch_boundary
                    if not has_epoch_boundary:
                        should_correct = True
                    elif outside_epoch_start:
                        should_correct = False
                    else:
                        should_correct = (
                            row_limit is not None and row_number <= int(row_limit)
                        )
                    if not should_correct and old_timeline_local is not None \
                            and old_timeline_monotonic is not None \
                            and not outside_epoch_start:
                        if row_monotonic is not None:
                            expected_old = old_timeline_local + timedelta(
                                seconds=row_monotonic - float(old_timeline_monotonic),
                            )
                            old_distance = abs((old_timestamp - expected_old).total_seconds())
                            new_distance = abs(
                                (old_timestamp - (expected_old + shift)).total_seconds()
                            )
                            should_correct = old_distance <= new_distance
                    elif not should_correct and old_local_cutoff is not None \
                            and not outside_epoch_start:
                        # Compatibility fallback for rows without provenance
                        # (for example a legacy/injected row). Runtime rows use
                        # the monotonic classification above.
                        old_distance = abs(
                            (old_timestamp - old_local_cutoff).total_seconds()
                        )
                        new_distance = abs(
                            (old_timestamp - (old_local_cutoff + shift)).total_seconds()
                        )
                        should_correct = old_distance <= new_distance
                    if not should_correct:
                        continue
                    corrected_timestamp = old_timestamp + shift
                    row[date_index] = corrected_timestamp.strftime("%d-%m-%y")
                    row[time_index] = corrected_timestamp.strftime("%H:%M:%S")
                    corrected += 1
                if corrected == 0:
                    # A session can still be header-only (for example during
                    # warmup) when sync arrives.  Its timestamped filename is
                    # wrong even though there are no data rows to rewrite yet.
                    try:
                        old_session_file = self._session_file
                        if correct_session_filename:
                            if _update_current_session:
                                self._rename_active_session_for_offset(
                                    offset_whole_seconds,
                                )
                                corrected_session_file = self._session_file
                            else:
                                corrected_session_file = self._rename_session_for_offset(
                                    session_file,
                                    offset_whole_seconds,
                                    update_current_link=False,
                                )
                        else:
                            corrected_session_file = session_file
                        if _session_record is not None:
                            _session_record["path"] = corrected_session_file
                    except Exception:
                        logger.exception("Could not correct header-only session filename")
                        return None
                    self._clear_time_pending_markers(session_file)
                    if clear_time_pending:
                        self._clear_time_pending_markers(corrected_session_file)
                    else:
                        self._ensure_time_pending_marker(corrected_session_file)
                    return 0

                old_mode = os.stat(session_file).st_mode & 0o777
                fd, tmp_path = tempfile.mkstemp(
                    prefix=".timesync-", suffix=".csv", dir=self._log_dir,
                )
                with os.fdopen(fd, "w", newline="") as target:
                    writer = csv.writer(target, delimiter=";", lineterminator="\n")
                    writer.writerows(rows)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(tmp_path, old_mode)
                os.replace(tmp_path, session_file)
                tmp_path = None
                corrected_session_file = session_file
                try:
                    if correct_session_filename and _update_current_session:
                        self._rename_active_session_for_offset(offset_whole_seconds)
                        corrected_session_file = self._session_file
                    elif correct_session_filename:
                        corrected_session_file = self._rename_session_for_offset(
                            session_file,
                            offset_whole_seconds,
                            update_current_link=False,
                        )
                    if _session_record is not None:
                        _session_record["path"] = corrected_session_file
                except Exception:
                    # The data correction is already durable at the old path.
                    # Do not hide that success (delivery offsets must reset)
                    # just because a collision/race prevented filename cleanup.
                    logger.exception("Could not correct active-session filename")
                    if _update_current_session:
                        self._session_file = session_file
                        try:
                            self._ensure_current_link()
                        except Exception:
                            logger.exception(
                                "Could not restore log_current after filename failure"
                            )
                self._clear_time_pending_markers(session_file)
                if clear_time_pending:
                    self._clear_time_pending_markers(corrected_session_file)
                else:
                    self._ensure_time_pending_marker(corrected_session_file)
                logger.info(
                    "Corrected %d active-session timestamp(s) by %+ds in %s",
                    corrected, offset_whole_seconds,
                    os.path.basename(corrected_session_file),
                )
                return corrected
            except Exception:
                logger.exception("Failed to correct active-session timestamps")
                return None
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except FileNotFoundError:
                        pass

    @property
    def session_active(self) -> bool:
        with self._file_lock:
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
    def csv_header_line(self) -> str:
        """Current CSV header, without a trailing newline."""
        return ";".join(self._header)

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
                    active = (
                        self._session_active
                        and self._session_file is not None
                        and os.path.abspath(path) == os.path.abspath(self._session_file)
                    )
                    files.append({
                        "name": f,
                        "size": size,
                        "mtime": mtime,
                        "lines": lines,
                        "active": active,
                    })
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

    def delete_log(self, filename: str) -> bool:
        """Delete one inactive CSV log and its pending-time marker."""
        fname = os.path.basename(str(filename or "").lstrip("/"))
        if not fname.endswith(".csv") or fname != str(filename or "").lstrip("/"):
            return False
        path = os.path.join(self._log_dir, fname)
        with self._file_lock:
            if (
                self._session_active
                and self._session_file is not None
                and os.path.abspath(path) == os.path.abspath(self._session_file)
            ):
                return False
            try:
                os.remove(path)
                self._clear_time_pending_markers(path)
                logger.info("Deleted log: %s", fname)
                return True
            except FileNotFoundError:
                return False
            except OSError as exc:
                logger.error("Failed to delete log %s: %s", fname, exc)
                return False

    def delete_old_logs(self, keep_count: int = 50):
        """Delete oldest logs keeping only `keep_count` files."""
        logs = self.list_logs()
        if len(logs) <= keep_count:
            return
        for entry in logs[keep_count:]:
            path = os.path.join(self._log_dir, entry["name"])
            try:
                os.remove(path)
                self._clear_time_pending_markers(path)
                logger.info(f"Deleted old log: {entry['name']}")
            except Exception:
                pass
