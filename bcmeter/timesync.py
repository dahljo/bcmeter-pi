"""Time synchronization module.

On Raspberry Pi, time comes from NTP (systemd-timesyncd / timedatectl)
or a manual set via ``date`` command.
"""

import logging
import subprocess
import time
from datetime import datetime

from .state import state

logger = logging.getLogger("bcmeter.timesync")


def is_valid() -> bool:
    """Check if system time is plausible (after 2026-03-15)."""
    return datetime.now() >= datetime(2026, 3, 15)


def set_time(unix_ts: int, tz: str = None):
    """Set system clock from Unix timestamp. Optionally set timezone.

    Platform caveat: uses ``sudo date`` and ``timedatectl`` (Linux).

    When a measurement session is active, marks ``state.time_just_synced``
    so the next CSV row carries a TIME_SYNC note.
    """
    dt_str = datetime.utcfromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")
    try:
        subprocess.run(
            ["sudo", "date", "-u", "-s", dt_str],
            capture_output=True, timeout=10, check=True,
        )
        logger.info("System time set to %s UTC", dt_str)
        if state.sampling:
            state.set("time_just_synced", True)
    except Exception as e:
        logger.error("Failed to set time: %s", e)

    if tz:
        try:
            subprocess.run(
                ["sudo", "timedatectl", "set-timezone", tz],
                capture_output=True, timeout=10,
            )
            logger.info("Timezone set to %s", tz)
        except Exception:
            pass


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
