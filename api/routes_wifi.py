"""WiFi management endpoints.

Matches the ESP32 /api/wifi/* contract.
"""

import logging
import socket
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger("bcmeter.api.wifi")

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_cfg = None
_nm = None  # NetworkManager instance


def set_dependencies(cfg, network_manager):
    global _cfg, _nm
    _cfg = cfg
    _nm = network_manager


# ---------------------------------------------------------------------------
# Scan state (background scan thread)
# ---------------------------------------------------------------------------

_scan_lock = threading.Lock()
_scan_busy = False
_scan_results: list[dict] = []


def _do_scan():
    """Background thread for WiFi scan."""
    global _scan_busy, _scan_results
    try:
        if _nm:
            results = _nm.scan_networks()
        else:
            results = []
    except Exception as exc:
        logger.error("WiFi scan failed: %s", exc)
        results = []
    with _scan_lock:
        _scan_results = results
        _scan_busy = False


# ---------------------------------------------------------------------------
# Connection state (for /api/wifi/connect flow)
# ---------------------------------------------------------------------------

_conn_lock = threading.Lock()
_conn_state = 0  # 0=idle, 1=connecting, 2=success, 3=failed
_conn_elapsed = 0.0
_conn_log: list[str] = []
_conn_start_time = 0.0


def _do_connect(ssid: str, password: str):
    """Background thread for WiFi connection attempt."""
    global _conn_state, _conn_elapsed, _conn_log, _conn_start_time

    with _conn_lock:
        _conn_state = 1
        _conn_start_time = time.time()
        _conn_log.clear()
        _conn_log.append(f"Connecting to {ssid}...\n")

    if not _nm:
        with _conn_lock:
            _conn_state = 3
            _conn_log.append("Network manager not available\n")
            _conn_elapsed = time.time() - _conn_start_time
        return

    # Save credentials first
    _nm.save_credentials(ssid, password)

    with _conn_lock:
        _conn_log.append("Credentials saved. Attempting STA connection...\n")

    ok = _nm.connect_sta(ssid, password)

    with _conn_lock:
        _conn_elapsed = time.time() - _conn_start_time
        if ok:
            _conn_state = 2
            _conn_log.append(f"Connected to {ssid}\n")
        else:
            _conn_state = 3
            _conn_log.append(f"Connection to {ssid} failed\n")
            _conn_log.append("Falling back to AP mode...\n")


# ---------------------------------------------------------------------------
# GET /api/wifi/scan
# ---------------------------------------------------------------------------

@router.get("/wifi/scan")
async def api_wifi_scan(request: Request):
    """Return scan results matching ESP32 /api/wifi/scan contract.

    Pass ?refresh=1 to trigger a new scan (replaces POST /wifi/scan/refresh).
    """
    global _scan_busy

    # ?refresh=1 triggers a new scan (matches ESP32 contract)
    if request.query_params.get("refresh") == "1":
        with _scan_lock:
            if not _scan_busy:
                _scan_busy = True
                t = threading.Thread(target=_do_scan, daemon=True, name="wifi_scan")
                t.start()

    with _scan_lock:
        scanning = _scan_busy
        results = list(_scan_results)

    networks = []
    for net in results:
        networks.append({
            "ssid": net.get("ssid", ""),
            "rssi": net.get("signal_dbm", -100),
            "secure": bool(net.get("security", "")),
        })

    return JSONResponse(content={
        "scanning": scanning,
        "networks": networks,
    })


# ---------------------------------------------------------------------------
# POST /api/wifi/scan/refresh
# ---------------------------------------------------------------------------

@router.post("/wifi/scan/refresh")
async def api_wifi_scan_refresh():
    """Trigger a new WiFi scan."""
    global _scan_busy

    with _scan_lock:
        if _scan_busy:
            return PlainTextResponse("Scan in progress", status_code=409)
        _scan_busy = True

    t = threading.Thread(target=_do_scan, daemon=True, name="wifi_scan")
    t.start()

    return PlainTextResponse("Scan started", status_code=202)


# ---------------------------------------------------------------------------
# POST /api/wifi
# ---------------------------------------------------------------------------

@router.post("/wifi")
async def api_wifi_save(request: Request):
    """Save WiFi credentials.

    Expects JSON body: ``{"ssid": "...", "pass": "..."}``
    """
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400)

    ssid = body.get("ssid", "")
    password = body.get("pass", "")

    if not ssid or len(password) < 8:
        return PlainTextResponse("Invalid credentials", status_code=400)

    if _nm:
        _nm.save_credentials(ssid, password)
    else:
        return PlainTextResponse("Network manager not available", status_code=503)

    return PlainTextResponse("Saved. Attempting connection...")


# ---------------------------------------------------------------------------
# POST /api/wifi/connect
# ---------------------------------------------------------------------------

@router.post("/wifi/connect")
async def api_wifi_connect(request: Request):
    """Save credentials and initiate WiFi connection.

    Expects JSON body: ``{"ssid": "...", "pass": "..."}``
    """
    global _conn_state

    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400)

    ssid = body.get("ssid", "")
    password = body.get("pass", "")

    if not ssid:
        return PlainTextResponse("SSID required", status_code=400)
    if not (8 <= len(password) <= 63):
        return PlainTextResponse("Password must be 8-63 chars", status_code=400)

    with _conn_lock:
        if _conn_state == 1 and _conn_start_time and (time.time() - _conn_start_time) < 30:
            return PlainTextResponse("Connection already in progress", status_code=409)

    if not _nm:
        return PlainTextResponse("Network manager not available", status_code=503)

    # Launch background connection thread
    t = threading.Thread(target=_do_connect, args=(ssid, password), daemon=True, name="wifi_connect")
    t.start()

    return PlainTextResponse("Connecting...", status_code=202)


# ---------------------------------------------------------------------------
# GET /api/wifi/connect/status
# ---------------------------------------------------------------------------

@router.get("/wifi/connect/status")
async def api_wifi_connect_status():
    """Return connection progress matching ESP32 /api/wifi/connect/status."""
    with _conn_lock:
        state = _conn_state
        elapsed = _conn_elapsed if state != 1 else (time.time() - _conn_start_time if _conn_start_time else 0.0)
        log_text = "".join(_conn_log)

    # Determine current mode/ip/ssid
    mode = "none"
    ip = ""
    ssid = ""
    if _nm:
        try:
            cur_ssid = _nm.get_current_network()
            is_connected = _nm.is_connected()
            if is_connected and cur_ssid:
                mode = "sta"
                ssid = cur_ssid
            else:
                mode = "ap"
        except Exception:
            pass

        try:
            s = __import__("socket")
            sock = s.socket(s.AF_INET, s.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            sock.close()
        except Exception:
            ip = "192.168.18.8" if mode == "ap" else ""

    # Include device name and hostname so the UI can show the .local address
    import socket as _sock
    device_name = _cfg.get_string("device_name", "bcMeter") if _cfg else "bcMeter"
    hostname = _sock.gethostname()

    return JSONResponse(content={
        "state": state,
        "elapsed": round(elapsed, 1),
        "log": log_text,
        "mode": mode,
        "ip": ip,
        "ssid": ssid,
        "device_name": device_name,
        "hostname": hostname,
    })


# ---------------------------------------------------------------------------
# GET /api/wifi/status
# ---------------------------------------------------------------------------

@router.get("/wifi/status")
async def api_wifi_status():
    """Return WiFi status matching ESP32 /api/wifi/status contract."""
    from bcmeter.state import state as global_state

    snap = global_state.snapshot()

    mode = snap.get("wifi_mode", "sta")
    ssid = snap.get("wifi_ssid", "")
    rssi = snap.get("wifi_rssi", 0)
    internet = snap.get("internet", False)

    # Derive quality tier from RSSI
    if rssi >= -55:
        quality = 4
    elif rssi >= -65:
        quality = 3
    elif rssi >= -75:
        quality = 2
    elif rssi >= -85:
        quality = 1
    else:
        quality = 0

    # Determine IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "192.168.18.8" if mode == "ap" else ""

    # Connection status string
    if internet:
        status = "connected"
    elif ssid:
        status = "no_internet"
    else:
        status = "disconnected"

    return JSONResponse(content={
        "mode": mode,
        "status": status,
        "ssid": ssid,
        "ip": ip,
        "rssi": rssi,
        "quality": quality,
        "internet": internet,
        "timeSynced": datetime.now().year > 2024,
    })


# ---------------------------------------------------------------------------
# POST /api/wifi/delete
# ---------------------------------------------------------------------------

@router.post("/wifi/delete")
async def api_wifi_delete():
    """Delete WiFi credentials and switch to AP mode."""
    if not _nm:
        return PlainTextResponse("Network manager not available", status_code=503)

    _nm.delete_credentials()

    # Switch to AP in background
    def _switch_to_ap():
        time.sleep(2)
        try:
            _nm._ensure_ap()
        except Exception as exc:
            logger.error("Failed to start AP after credential deletion: %s", exc)

    t = threading.Thread(target=_switch_to_ap, daemon=True, name="wifi_to_ap")
    t.start()

    return PlainTextResponse("OK, switching to AP...")
