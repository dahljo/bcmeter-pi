"""Multi-source location cache with priority.

Port of ESP32 geoloc.h/cpp.
Priority: GPS (3) > cell_tower (2) > IP (1).
"""

import json
import logging
import threading
import urllib.request
import urllib.error

logger = logging.getLogger("bcmeter.geoloc")

PRIO_GPS = 3
PRIO_CELL_TOWER = 2
PRIO_IP = 1
PRIO_UNKNOWN = 0

# AWS Lambda endpoint (same as email_handler — not a secret, requires API key)
AWS_ENDPOINT = "https://xwqm43fafwo7w65d4lno3nspzu0ovykv.lambda-url.eu-north-1.on.aws"

_lat: float = 0.0
_lon: float = 0.0
_prio: int = 0
_source: str = ""
_has_loc: bool = False
_lock = threading.Lock()
_fetch_running: bool = False


def _source_priority(src: str) -> int:
    return {"gps": PRIO_GPS, "cell_tower": PRIO_CELL_TOWER, "ip": PRIO_IP}.get(src, PRIO_UNKNOWN)


def set_location(lat: float, lon: float, source: str = "unknown"):
    """Set location if source priority >= current."""
    global _lat, _lon, _prio, _source, _has_loc
    prio = _source_priority(source)
    with _lock:
        if prio < _prio:
            logger.debug("Ignoring %s (%.5f,%.5f) -- existing '%s' has higher priority",
                         source, lat, lon, _source)
            return
        _lat, _lon, _prio, _source = lat, lon, prio, source
        _has_loc = (lat != 0.0 or lon != 0.0)
        logger.info("Location set: %.5f,%.5f (%s)", lat, lon, source)


def get_location():
    """Returns (success: bool, lat: float, lon: float)."""
    with _lock:
        return _has_loc, _lat, _lon


def get_source() -> str:
    with _lock:
        return _source


def _fetch_from_lambda(device_name: str, api_key: str) -> bool:
    """Ask the Lambda for the best location it has stored (cell tower DDB)."""
    logger.debug("Requesting location from Lambda (cell tower DDB)...")
    try:
        payload = json.dumps({"action": "get_location"}).encode()
        req = urllib.request.Request(
            AWS_ENDPOINT,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "x-device-id": device_name,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        lat = data.get("lat", 0.0)
        lon = data.get("lon", 0.0)
        src = data.get("source", "cell_tower")
        if lat == 0.0 and lon == 0.0:
            return False
        set_location(lat, lon, src)
        return True
    except Exception as e:
        logger.debug("Lambda location fetch failed: %s", e)
        return False


def _fetch_by_ip_geo() -> bool:
    """IP geolocation via ipinfo.io -- city-level, enough for QNH."""
    logger.debug("IP geolocation lookup (ipinfo.io)...")
    try:
        req = urllib.request.Request(
            "https://ipinfo.io/json",
            headers={"User-Agent": "bcMeter/2.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        loc = data.get("loc", "")
        if "," not in loc:
            return False
        lat_s, lon_s = loc.split(",", 1)
        lat = float(lat_s)
        lon = float(lon_s)
        if lat == 0.0 and lon == 0.0:
            return False
        logger.info("IP location: %.4f,%.4f (city: %s)", lat, lon, data.get("city", "?"))
        set_location(lat, lon, "ip")
        return True
    except Exception as e:
        logger.debug("IP geolocation failed: %s", e)
        return False


def _fetch_thread(gps, device_name: str, api_key: str):
    """Background fetch: GPS first, then Lambda, then IP geo."""
    global _fetch_running
    try:
        # GPS has highest priority -- check without network
        if gps and gps.present:
            try:
                gd = gps.get_data()
                if gd.valid and (gd.lat != 0.0 or gd.lon != 0.0):
                    set_location(gd.lat, gd.lon, "gps")
            except Exception:
                pass

        # Already have GPS quality -- no need for network fetch
        with _lock:
            if _prio >= PRIO_GPS:
                return

        ok = _fetch_from_lambda(device_name, api_key)
        if not ok:
            logger.debug("Lambda failed -- trying IP geo...")
            _fetch_by_ip_geo()
    except Exception as e:
        logger.error("Geoloc fetch error: %s", e)
    finally:
        global _fetch_running
        with _lock:
            _fetch_running = False


def try_fetch(gps=None, device_name: str = "bcMeter", api_key: str = ""):
    """Initiate a background location fetch. No-op if already running."""
    global _fetch_running
    with _lock:
        if _fetch_running:
            return
        _fetch_running = True

    t = threading.Thread(
        target=_fetch_thread,
        args=(gps, device_name, api_key),
        daemon=True,
        name="geoloc_fetch",
    )
    t.start()
