"""Circular buffer of timestamped diagnostic incidents.

Port of ESP32 incident_log.h/cpp. Stores up to MAX_INCIDENTS entries
with timestamp, severity level, and message. Thread-safe.
"""

import json
import threading
import time
from datetime import datetime

MAX_INCIDENTS = 64


class _Entry:
    __slots__ = ("timestamp", "level", "msg")

    def __init__(self, timestamp: int, level: str, msg: str):
        self.timestamp = timestamp
        self.level = level
        self.msg = msg


_ring: list = [None] * MAX_INCIDENTS
_head: int = 0
_count: int = 0
_lock = threading.Lock()


def add(level: str, msg: str, *args):
    """Append a diagnostic incident.

    *level*: ``"info"``, ``"warn"``, ``"error"``, or ``"ok"``.
    *msg*: format string (printf-style with ``%`` placeholders).
    *args*: optional format arguments.
    """
    global _head, _count

    if args:
        try:
            msg = msg % args
        except Exception:
            pass

    now = datetime.now()
    ts = int(now.timestamp()) if now.year > 2024 else int(time.monotonic())

    with _lock:
        _ring[_head] = _Entry(ts, level[:5], msg[:95])
        _head = (_head + 1) % MAX_INCIDENTS
        if _count < MAX_INCIDENTS:
            _count += 1


def count() -> int:
    with _lock:
        return _count


def get(idx: int):
    """Get entry by index (0 = oldest). Returns _Entry or None."""
    with _lock:
        if idx >= _count:
            return None
        pos = idx if _count < MAX_INCIDENTS else (_head + idx) % MAX_INCIDENTS
        return _ring[pos]


def to_json() -> str:
    """Serialize all entries as JSON array of {ts, s, v}."""
    with _lock:
        entries = []
        for i in range(_count):
            pos = i if _count < MAX_INCIDENTS else (_head + i) % MAX_INCIDENTS
            e = _ring[pos]
            if e:
                entries.append({"ts": e.timestamp, "s": e.level, "v": e.msg})
    return json.dumps(entries)
