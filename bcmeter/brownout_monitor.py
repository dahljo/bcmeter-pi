"""Monitor kernel logs for under-voltage (brownout) events.

On Raspberry Pi 3A+, the kernel logs under-voltage warnings to dmesg when
the supply voltage drops below ~4.63V.  These appear as:

    Under-voltage detected! (0x00050005)
    Voltage normalised (0x00000000)

Occasional events are harmless, but frequent brownouts (6+ per hour) indicate
an inadequate power supply or cable.  When the threshold is reached an email
alert is sent with PSU recommendations.
"""

import logging
import re
import subprocess
import threading
import time
from collections import deque

from bcmeter import incident_log

logger = logging.getLogger("bcmeter.brownout")

# Pattern matching kernel under-voltage messages
_UV_PATTERN = re.compile(r"Under-voltage detected", re.IGNORECASE)

# Alert thresholds
_THRESHOLD_COUNT = 6       # events required to trigger alert
_THRESHOLD_WINDOW = 3600   # seconds (1 hour)
_ALERT_COOLDOWN = 3600     # don't re-alert within 1 hour

_events: deque = deque()   # timestamps of recent under-voltage events
_last_alert_time = 0.0


def _check_dmesg() -> int:
    """Parse dmesg for new under-voltage events. Returns count of new events."""
    try:
        result = subprocess.run(
            ["dmesg", "--time-format=raw", "--level=warn,err"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0

    count = 0
    now = time.time()
    for line in result.stdout.splitlines():
        if not _UV_PATTERN.search(line):
            continue
        # Extract kernel timestamp (seconds since boot)
        # Format: "   123.456789] Under-voltage detected..."
        match = re.match(r"\s*([\d.]+)\]", line)
        if not match:
            continue
        # Convert boot-relative timestamp to absolute
        boot_ts = float(match.group(1))
        try:
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
        except (OSError, ValueError):
            uptime = 0
        abs_ts = now - uptime + boot_ts
        # Only count events not already tracked
        if not _events or abs_ts > _events[-1]:
            _events.append(abs_ts)
            count += 1

    return count


def _prune_old_events():
    """Remove events older than the threshold window."""
    cutoff = time.time() - _THRESHOLD_WINDOW
    while _events and _events[0] < cutoff:
        _events.popleft()


def task(stop_event: threading.Event):
    """Background thread: poll dmesg every 60s for under-voltage events."""
    global _last_alert_time

    logger.info("Brownout monitor started")
    # Lazy import to avoid circular dependency
    from bcmeter import email_handler

    while not stop_event.is_set():
        stop_event.wait(60)
        if stop_event.is_set():
            break

        new_count = _check_dmesg()
        _prune_old_events()

        if new_count > 0:
            logger.warning("Under-voltage detected (%d new, %d in window)",
                           new_count, len(_events))
            incident_log.add("warn", "Under-voltage: %d events in last hour",
                             len(_events))

        if (len(_events) >= _THRESHOLD_COUNT
                and time.time() - _last_alert_time > _ALERT_COOLDOWN):
            logger.error("Brownout threshold reached: %d events in 1 hour",
                         len(_events))
            incident_log.add("error", "Brownout alert: %d events/hour",
                             len(_events))
            try:
                email_handler.send_brownout_alert(
                    count=len(_events),
                    minutes=_THRESHOLD_WINDOW // 60,
                )
            except Exception:
                logger.exception("Failed to send brownout alert email")
            _last_alert_time = time.time()

    logger.info("Brownout monitor stopped")
