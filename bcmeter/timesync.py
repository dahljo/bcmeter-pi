"""Time synchronization module.

On Raspberry Pi, time comes from NTP (systemd-timesyncd / timedatectl)
or a manual set via ``date`` command.
"""

import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from .state import state

logger = logging.getLogger("bcmeter.timesync")

_manual_time_valid = False
_manual_set_generation = 0
_status_lock = threading.Lock()
_log_boundary_provider = None
_log_sync_event_handler = None
_log_sync_lost_handler = None
_MIN_MANUAL_YEAR = 2025
_MAX_MANUAL_YEAR = 2100


def _local_now() -> datetime:
    """Local naive wall time, matching the representation stored in CSV."""
    return datetime.now()


def set_log_boundary_provider(provider):
    """Register a lock-safe callback returning the active log row boundary."""
    global _log_boundary_provider
    _log_boundary_provider = provider


def set_log_sync_event_handler(handler):
    """Register the storage-owned, session-generation-aware event handler."""
    global _log_sync_event_handler
    _log_sync_event_handler = handler


def set_log_sync_lost_handler(handler):
    """Register the storage hook that freezes delivery on NTP sync loss."""
    global _log_sync_lost_handler
    _log_sync_lost_handler = handler


def _capture_log_boundary() -> dict:
    provider = _log_boundary_provider
    if provider is None:
        return {}
    try:
        return provider() or {}
    except Exception:
        logger.exception("Could not capture time-sync log boundary")
        return {}


def _sync_event_metadata(
        old_local_cutoff: datetime, boundary=None,
        old_timeline_local: datetime = None,
        old_timeline_monotonic: float = None) -> dict:
    metadata = {"old_local_cutoff": old_local_cutoff}
    if old_timeline_local is not None:
        metadata["old_timeline_local"] = old_timeline_local
    if old_timeline_monotonic is not None:
        metadata["old_timeline_monotonic"] = old_timeline_monotonic
    metadata.update(_capture_log_boundary() if boundary is None else boundary)
    return metadata


def _publish_sync_event(offset_seconds: float, source: str, metadata: dict) -> bool:
    """Publish a clock step through Storage when available.

    Storage serializes publication with session end.  That permits correction
    of a just-closed generation and lets NTP distinguish a genuine later
    re-sync from a late observation of the initial boot sync.
    """
    handler = _log_sync_event_handler
    if handler is not None:
        try:
            return bool(handler(offset_seconds, source=source, **metadata))
        except Exception:
            logger.exception("Storage time-sync event handler failed")

    # Fallback for isolated use/tests without an attached Storage instance.
    if source == "ntp" and state.get("session_time_synced") \
            and not metadata.get("session_generation"):
        return False
    return state.mark_time_synced_if_logging(offset_seconds, **metadata)


def _publish_sync_lost(metadata: dict) -> bool:
    handler = _log_sync_lost_handler
    if handler is not None:
        try:
            return bool(handler(**metadata))
        except Exception:
            logger.exception("Storage time-sync-loss handler failed")
    if state.get("logging_session_active"):
        state.set("session_time_synced", False)
        return True
    return False


def _property_is_synchronized(property_name: str):
    """Return a timedatectl synchronization property, or None if unavailable."""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property", property_name, "--value"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip().lower()
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


def system_clock_synchronized() -> bool:
    """Return the systemd-reported NTP/system-clock synchronization state."""
    statuses = [
        _property_is_synchronized("NTPSynchronized"),
        _property_is_synchronized("SystemClockSynchronized"),
    ]
    return any(status is True for status in statuses)


def is_valid() -> bool:
    """Return whether the clock was manually set or systemd reports it synced."""
    with _status_lock:
        manual_valid = _manual_time_valid
    return manual_valid or system_clock_synchronized()


def set_time(unix_ts: int, tz: str = None) -> bool:
    """Set system clock from Unix timestamp. Optionally set timezone.

    Platform caveat: uses ``sudo date`` and ``timedatectl`` (Linux).

    When a measurement session is active, marks ``state.time_just_synced``
    so the next CSV row carries a TIME_SYNC note.
    """
    global _manual_time_valid, _manual_set_generation

    try:
        requested_utc = datetime.fromtimestamp(unix_ts, timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        logger.error("Rejected invalid manual timestamp: %r", unix_ts)
        return False
    if not (_MIN_MANUAL_YEAR <= requested_utc.year <= _MAX_MANUAL_YEAR):
        logger.error("Rejected implausible manual timestamp year: %d", requested_utc.year)
        return False
    dt_str = requested_utc.strftime("%Y-%m-%d %H:%M:%S")
    local_before = _local_now()
    mono_before = time.monotonic()
    log_boundary = _capture_log_boundary()
    try:
        subprocess.run(
            ["sudo", "date", "-u", "-s", dt_str],
            capture_output=True, timeout=10, check=True,
        )
        logger.info("System time set to %s UTC", dt_str)
    except Exception as e:
        logger.error("Failed to set time: %s", e)
        return False

    if tz:
        try:
            subprocess.run(
                ["sudo", "timedatectl", "set-timezone", tz],
                capture_output=True, timeout=10,
            )
            logger.info("Timezone set to %s", tz)
        except Exception:
            pass

    elapsed = time.monotonic() - mono_before
    expected_without_step = local_before + timedelta(seconds=elapsed)
    offset_seconds = (_local_now() - expected_without_step).total_seconds()
    with _status_lock:
        _manual_time_valid = True
        _manual_set_generation += 1
    _publish_sync_event(
        offset_seconds,
        "manual",
        _sync_event_metadata(
            expected_without_step,
            log_boundary,
            old_timeline_local=local_before,
            old_timeline_monotonic=mono_before,
        ),
    )
    return True


def sync_ntp():
    """Trigger NTP sync via timedatectl."""
    try:
        subprocess.run(
            ["sudo", "timedatectl", "set-ntp", "true"],
            capture_output=True, timeout=10,
        )
        logger.debug("NTP sync triggered")
    except Exception as e:
        logger.debug("NTP sync trigger failed: %s", e)


def wait_for_valid(timeout_s: float = 30.0) -> bool:
    """Block until time is valid or timeout. Returns True if synced."""
    if is_valid():
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_valid():
            return True
        time.sleep(0.5)
    return is_valid()


def monitor_sync(stop_event, poll_interval_s: float = 5.0):
    """Monitor the first unsynced->synced transition and publish its clock step.

    A monotonic baseline preserves the old wall-clock timeline while NTP is
    pending.  When systemd reports synchronization, the difference between
    that projected timeline and the new wall clock is the correction required
    for timestamps already written to an active session.
    """
    with _status_lock:
        generation = _manual_set_generation
    synchronized = system_clock_synchronized()
    baseline_local = _local_now()
    baseline_mono = time.monotonic()
    last_unsynced_boundary = _capture_log_boundary()
    unsynced_start_boundary = dict(last_unsynced_boundary)
    unsynced_start_monotonic = baseline_mono

    while not stop_event.wait(max(0.1, poll_interval_s)):
        with _status_lock:
            current_generation = _manual_set_generation
        if current_generation != generation:
            # set_time() already published its own precisely measured offset.
            # Rebase so a later NTP status transition cannot publish it twice.
            generation = current_generation
            baseline_local = _local_now()
            baseline_mono = time.monotonic()
            synchronized = system_clock_synchronized()
            last_unsynced_boundary = _capture_log_boundary()
            unsynced_start_boundary = dict(last_unsynced_boundary)
            unsynced_start_monotonic = baseline_mono
            continue

        now_synchronized = system_clock_synchronized()
        now_mono = time.monotonic()
        now_local = _local_now()
        expected_without_step = baseline_local + timedelta(
            seconds=now_mono - baseline_mono,
        )
        wall_deviation = (now_local - expected_without_step).total_seconds()
        if not now_synchronized and abs(wall_deviation) < 1.0:
            # Still demonstrably on the old wall-clock timeline.  Once a step
            # is visible, freeze this boundary even if systemd reports synced
            # a poll later.
            last_unsynced_boundary = _capture_log_boundary()
        if not synchronized and now_synchronized:
            offset_seconds = wall_deviation
            logger.info("System clock synchronized (wall-clock step %.3fs)", offset_seconds)
            metadata = _sync_event_metadata(
                expected_without_step,
                last_unsynced_boundary,
                old_timeline_local=baseline_local,
                old_timeline_monotonic=baseline_mono,
            )
            metadata.update({
                "row_start_exclusive": unsynced_start_boundary.get("row_limit"),
                "row_start_session_generation": unsynced_start_boundary.get(
                    "session_generation"
                ),
                "unsynced_start_monotonic": unsynced_start_monotonic,
            })
            _publish_sync_event(
                offset_seconds,
                "ntp",
                metadata,
            )
        elif synchronized and not now_synchronized:
            # Start a fresh old-clock baseline if synchronization is lost.
            baseline_local = _local_now()
            baseline_mono = time.monotonic()
            last_unsynced_boundary = _capture_log_boundary()
            unsynced_start_boundary = dict(last_unsynced_boundary)
            unsynced_start_monotonic = baseline_mono
            _publish_sync_lost(unsynced_start_boundary)
        synchronized = now_synchronized
