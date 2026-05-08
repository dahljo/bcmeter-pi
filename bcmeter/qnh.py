"""Sea-level pressure (QNH) fetching from Open-Meteo API.

Port of ESP32 qnh.h/cpp. 2-hour cache. Uses geoloc or GPS for
coordinate auto-discovery.
"""

import json
import logging
import threading
import time
import urllib.request
import urllib.error

from . import geoloc

logger = logging.getLogger("bcmeter.qnh")

_cached_qnh: float = 1013.25
_valid: bool = False
_last_fetch: float = 0.0
_fetch_running: bool = False
_lock = threading.Lock()

REFRESH_SECONDS = 2 * 3600  # 2 hours


def get() -> float:
    """Return cached QNH in hPa. Falls back to ISA 1013.25 until fetched."""
    with _lock:
        return _cached_qnh


def is_valid() -> bool:
    """True once a successful Open-Meteo response has been parsed."""
    with _lock:
        return _valid


def _do_fetch(lat: float, lon: float):
    """Background fetch from Open-Meteo."""
    global _cached_qnh, _valid, _last_fetch, _fetch_running
    try:
        # Resolve coordinates if not provided
        if lat == 0.0 and lon == 0.0:
            ok, lat, lon = geoloc.get_location()
            if not ok:
                logger.debug("QNH: no location available -- cannot fetch")
                return
            logger.debug("QNH: using cached location (%.4f,%.4f src:%s)",
                         lat, lon, geoloc.get_source())

        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat:.6f}&longitude={lon:.6f}&current=pressure_msl")
        req = urllib.request.Request(url, headers={"User-Agent": "bcMeter/2.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        qnh = data.get("current", {}).get("pressure_msl", 0.0)
        if 800.0 < qnh < 1100.0:
            with _lock:
                _cached_qnh = qnh
                _valid = True
                _last_fetch = time.time()
            logger.info("QNH cache updated: %.2f hPa", qnh)
        else:
            logger.warning("QNH: implausible value %.2f hPa -- rejected", qnh)
    except Exception as e:
        logger.error("QNH fetch failed: %s", e)
    finally:
        with _lock:
            _fetch_running = False


def fetch_if_needed(lat: float = 0.0, lon: float = 0.0):
    """Fetch QNH if cache is stale (>2h) or missing. Runs in background."""
    global _fetch_running
    with _lock:
        if _fetch_running:
            return
        if _valid and (time.time() - _last_fetch) < REFRESH_SECONDS:
            return
        _fetch_running = True

    t = threading.Thread(
        target=_do_fetch, args=(lat, lon),
        daemon=True, name="qnh_fetch",
    )
    t.start()
