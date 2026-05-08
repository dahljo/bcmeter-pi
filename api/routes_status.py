"""Status & monitoring endpoints.

Matches the ESP32 /api/status, /api/system, and /api/logs contracts.
"""

import glob
import io
import json
import logging
import math
import os
import shutil
import socket
import subprocess
import time
import zipfile
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("bcmeter.api.status")

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_cfg = None
_state = None
_storage = None
_gps = None
_pump = None


def set_dependencies(cfg, state_mgr, storage, gps=None, pump=None):
    global _cfg, _state, _storage, _gps, _pump
    _cfg = cfg
    _state = state_mgr
    _storage = storage
    _gps = gps
    _pump = pump


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_version() -> str:
    """Return the package version string."""
    try:
        from bcmeter import __version__
        return __version__
    except Exception:
        return "2.0.0"


def _get_memory_info() -> dict:
    """Return memory usage dict.  Prefers psutil, falls back to /proc/meminfo."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {
            "mem_total": vm.total,
            "mem_available": vm.available,
            "mem_used": vm.used,
            "mem_percent": vm.percent,
        }
    except ImportError:
        pass

    # Fallback: parse /proc/meminfo (Linux only)
    info = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    val_kb = int(parts[1])
                    if key == "MemTotal":
                        info["mem_total"] = val_kb * 1024
                    elif key == "MemAvailable":
                        info["mem_available"] = val_kb * 1024
        if "mem_total" in info and "mem_available" in info:
            info["mem_used"] = info["mem_total"] - info["mem_available"]
            info["mem_percent"] = round(
                100.0 * info["mem_used"] / info["mem_total"], 1
            ) if info["mem_total"] > 0 else 0.0
    except Exception:
        pass

    return info


def _get_ip_address() -> str:
    """Best-effort local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_mac_address() -> str:
    """Return wlan0 MAC address, or empty string."""
    try:
        with open("/sys/class/net/wlan0/address", "r") as f:
            return f.read().strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def api_status():
    """Return device status JSON matching ESP32 /api/status contract."""
    from bcmeter.errors import ErrorCode, InitStep, error_string, init_step_string

    snap = _state.snapshot() if _state else {}

    err_code = snap.get("error", 0)
    init_step = snap.get("init_step", 0)
    sampling = snap.get("sampling", False)

    # Derive top-level status: 0=idle, 1=initializing, 2=sampling, 3=error
    if err_code != 0:
        status = 3
    elif sampling:
        status = 2 if init_step >= int(InitStep.INIT_DONE) else 1
    else:
        status = 0

    data = {
        "status": status,
        "adc": snap.get("adc_present", False),
        "adc_type": snap.get("adc_type", ""),
        "gps": snap.get("gps_present", False),
        "sht4x": snap.get("sht4x_present", False),
        "sps30": snap.get("sps30_present", False),
        "error": int(err_code),
        "error_msg": error_string(ErrorCode(err_code)),
        "warning_msg": snap.get("warning_msg", ""),
        "init_step": int(init_step),
        "init_msg": init_step_string(InitStep(init_step)),
        "name": _cfg.get_string("device_name", "bcMeter") if _cfg else "bcMeter",
        "bc": round(snap.get("last_bc", 0.0), 1),
        "atn": round(snap.get("last_atn", 0.0), 2),
        "sen": round(snap.get("last_sen", 1.0), 4),
        "ref": round(snap.get("last_ref", 1.0), 4),
        "flow": round(snap.get("last_flow", 0.0), 4),
        "filter": snap.get("filter_status", 5),
        "samples": snap.get("sample_count", 0),
        "session": _storage.session_active if _storage else False,
        "duty": _pump.get_duty() if _pump else 0,
        "wifi_mode": snap.get("wifi_mode", "sta"),
        "wifi_ssid": snap.get("wifi_ssid", ""),
        "wifi_rssi": snap.get("wifi_rssi", 0),
        "internet": snap.get("internet", False),
        "bme280": snap.get("bme280_present", False),
        "time_synced": datetime.now().year > 2024,
        "last_cal": _cfg.get_string("last_cal_time", "never") if _cfg else "never",
        "version": _get_version(),
        "env": "pi",
    }

    # OTA update status
    try:
        from bcmeter import ota_check
        ota = ota_check.get_info()
        data["ota_available"] = ota.get("available", False)
        data["ota_version"] = ota.get("version", "")
    except Exception:
        data["ota_available"] = False
        data["ota_version"] = ""

    return JSONResponse(content=data)


# ---------------------------------------------------------------------------
# GET /api/system
# ---------------------------------------------------------------------------

@router.get("/system")
async def api_system():
    """Return system info JSON matching ESP32 /api/system contract."""
    snap = _state.snapshot() if _state else {}

    # Disk usage
    try:
        du = shutil.disk_usage("/")
        disk_used = du.used
        disk_total = du.total
    except Exception:
        disk_used = 0
        disk_total = 0

    mem = _get_memory_info()

    device_name = _cfg.get_string("device_name", "bcMeter") if _cfg else "bcMeter"
    hostname = device_name.lower().replace(" ", "-")

    data = {
        "ip": _get_ip_address(),
        "mac": _get_mac_address(),
        "hostname": hostname,
        "device_name": device_name,
        "adc": snap.get("adc_present", False),
        "adc_type": snap.get("adc_type", ""),
        "sps30": snap.get("sps30_present", False),
        "modem": snap.get("modem_present", False),
        "modem_active": False,
        "modem_signal": snap.get("modem_signal", ""),
        "modem_operator": snap.get("modem_operator", ""),
        "disk_used": disk_used,
        "disk_total": disk_total,
        "flash_used": disk_used,
        "flash_total": disk_total,
        "heap": mem.get("mem_available", 0),
        "heap_max_block": mem.get("mem_available", 0),
        "heap_fragmentation": 0,
        "mem_total": mem.get("mem_total", 0),
        "mem_available": mem.get("mem_available", 0),
        "mem_percent": mem.get("mem_percent", 0),
        "session": _storage.session_filename if _storage else "",
        "last_cal": _cfg.get_string("last_cal_time", "never") if _cfg else "never",
        "wifi_mode": snap.get("wifi_mode", "sta"),
        "wifi_status": "connected" if snap.get("internet", False) else "no_internet",
        "time_synced": datetime.now().year > 2024,
        "gps": snap.get("gps_present", False),
    }

    # BME280 / barometric data
    data["bme280"] = snap.get("bme280_present", False)
    pressure = snap.get("last_pressure", 0.0)
    if pressure > 0:
        data["baro_pressure"] = round(pressure, 2)
        try:
            from bcmeter import qnh as _qnh
            qnh_val = _qnh.get()
            data["baro_qnh"] = round(qnh_val, 2)
            # Barometric altitude: h = 44330 * (1 - (P/P0)^(1/5.255))
            if qnh_val > 0:
                data["baro_alt"] = round(
                    44330.0 * (1.0 - math.pow(pressure / qnh_val, 1.0 / 5.255)), 1
                )
        except Exception:
            pass

    # Geolocation
    try:
        from bcmeter import geoloc as _geoloc
        geo_ok, geo_lat, geo_lon = _geoloc.get_location()
        if geo_ok:
            lat = round(geo_lat, 6)
            lon = round(geo_lon, 6)
            src = _geoloc.get_source()
            data["geoloc_lat"] = lat
            data["geoloc_lon"] = lon
            data["geoloc_source"] = src
            data["geo_lat"] = lat
            data["geo_lon"] = lon
            data["geo_source"] = src
    except Exception:
        pass

    # GPS details
    if _gps and _gps.present:
        try:
            gd = _gps.get_data()
            data["gps_valid"] = gd.valid
            data["gps_sats"] = gd.satellites
            data["gps_hdop"] = gd.hdop
            if gd.valid:
                data["gps_lat"] = gd.lat
                data["gps_lon"] = gd.lon
                data["gps_alt"] = gd.altitude
                data["gps_speed"] = gd.speed
        except Exception:
            pass

    # System time
    now = datetime.now()
    if now.year > 2024:
        data["time"] = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        data["time"] = ""

    return JSONResponse(content=data)


# ---------------------------------------------------------------------------
# GET /api/logs
# ---------------------------------------------------------------------------

@router.get("/debug_mobile/status")
async def api_debug_mobile_status():
    """Return debug mobile progress."""
    from bcmeter import email_handler
    return email_handler.get_debug_mobile_status()


@router.get("/logs")
async def api_logs():
    """Return structured diagnostic JSON matching ESP32 /api/logs contract.

    Each section is an array of {k, v, s} objects (key, value, status).
    Status values: "ok", "warn", "error", "info".
    """
    from bcmeter.errors import ErrorCode, error_string

    snap = _state.snapshot() if _state else {}
    err_code = snap.get("error", 0)
    sampling = snap.get("sampling", False)

    now = datetime.now()
    if now.year > 2024:
        ts = now.strftime("%H:%M:%S")
    else:
        ts = time.strftime("%H:%M:%S", time.gmtime(time.monotonic()))

    # --- Hardware ---
    hw = []
    adc_ok = snap.get("adc_present", False)
    adc_type = snap.get("adc_type", "")
    adc_name = {
        "i2c": "MCP342X (I2C)",
        "spi": "ADS8344 (SPI)",
    }.get(adc_type, "")
    hw.append({
        "k": "Optical ADC",
        "v": f"OK -- {adc_name}" if adc_ok and adc_name else (
            "OK" if adc_ok else
            "Not detected -- expected MCP342X (I2C) or ADS8344 (SPI)"
        ),
        "s": "ok" if adc_ok else "error",
    })
    sps_ok = snap.get("sps30_present", False)
    sht_ok = snap.get("sht4x_present", False)
    hw.append({
        "k": "SHT4x",
        "v": "OK" if sht_ok else "Not found",
        "s": "ok" if sht_ok else "error",
    })
    hw.append({
        "k": "SPS30",
        "v": "OK" if sps_ok else "Not found",
        "s": "ok" if sps_ok else "warn",
    })
    modem_ok = snap.get("modem_present", False)
    hw.append({
        "k": "Modem",
        "v": "OK" if modem_ok else "Not found",
        "s": "ok" if modem_ok else "info",
    })

    # GPS
    if snap.get("gps_present", False) and _gps:
        try:
            gd = _gps.get_data()
            if gd.valid:
                gps_v = f"Fix: {gd.lat:.6f}, {gd.lon:.6f} ({gd.satellites} sats)"
                gps_s = "ok"
            else:
                gps_v = f"Searching... ({gd.satellites} sats visible)"
                gps_s = "warn"
        except Exception:
            gps_v = "Error"
            gps_s = "error"
        hw.append({"k": "GPS", "v": gps_v, "s": gps_s})

    # --- Measurement ---
    meas = []
    if err_code:
        status_v = error_string(ErrorCode(err_code))
        status_s = "error"
    elif sampling:
        status_v = "Sampling"
        status_s = "ok"
    else:
        status_v = "Idle"
        status_s = "info"
    meas.append({"k": "Status", "v": status_v, "s": status_s})
    meas.append({"k": "Samples", "v": str(snap.get("sample_count", 0)), "s": "info"})
    meas.append({
        "k": "Last BC",
        "v": f"{snap.get('last_bc', 0.0):.1f} ng/m\u00b3",
        "s": "info",
    })
    last_atn = snap.get("last_atn", 0.0)
    meas.append({
        "k": "Last ATN",
        "v": f"{last_atn:.2f}",
        "s": "warn" if last_atn > 100 else "info",
    })
    last_flow = snap.get("last_flow", 0.0)
    meas.append({
        "k": "Airflow",
        "v": f"{last_flow * 1000:.1f} ml/min",
        "s": "warn" if last_flow < 0.05 else "info",
    })
    meas.append({
        "k": "Session",
        "v": _storage.session_filename or "none" if _storage else "none",
        "s": "info",
    })

    # --- System ---
    mem = _get_memory_info()
    sys_arr = []
    mem_avail = mem.get("mem_available", 0)
    sys_arr.append({
        "k": "Memory",
        "v": f"{mem_avail // (1024 * 1024)} MB available ({mem.get('mem_percent', 0):.0f}% used)",
        "s": "warn" if mem.get("mem_percent", 0) > 85 else "info",
    })
    try:
        du = shutil.disk_usage("/")
        disk_pct = round(100.0 * du.used / du.total, 1)
        sys_arr.append({
            "k": "Disk",
            "v": f"{du.used // (1024 * 1024)} / {du.total // (1024 * 1024)} MB ({disk_pct}%)",
            "s": "warn" if disk_pct > 90 else "info",
        })
    except Exception:
        sys_arr.append({"k": "Disk", "v": "Unknown", "s": "warn"})
    sys_arr.append({
        "k": "Hostname",
        "v": socket.gethostname(),
        "s": "info",
    })

    # --- Network ---
    net = []
    wifi_mode = snap.get("wifi_mode", "sta")
    net.append({"k": "WiFi Mode", "v": wifi_mode.upper(), "s": "info"})
    net.append({"k": "SSID", "v": snap.get("wifi_ssid", ""), "s": "info"})
    net.append({"k": "IP", "v": _get_ip_address(), "s": "info"})
    rssi = snap.get("wifi_rssi", 0)
    net.append({
        "k": "Signal",
        "v": f"{rssi} dBm",
        "s": "warn" if rssi < -80 else "info",
    })
    internet = snap.get("internet", False)
    net.append({
        "k": "Internet",
        "v": "Yes" if internet else "No",
        "s": "ok" if internet else "warn",
    })
    time_synced = datetime.now().year > 2024
    net.append({
        "k": "Time Sync",
        "v": "Synced" if time_synced else "Not synced",
        "s": "ok" if time_synced else "warn",
    })

    # --- Incidents ---
    try:
        from bcmeter import incident_log
        incidents_json = incident_log.to_json()
    except Exception:
        incidents_json = "[]"

    return JSONResponse(content={
        "timestamp": ts,
        "hardware": hw,
        "measurement": meas,
        "system": sys_arr,
        "network": net,
        "incidents": json.loads(incidents_json),
    })


# ---------------------------------------------------------------------------
# GET /api/maintenance-logs  — comprehensive debug bundle as zip
# ---------------------------------------------------------------------------

BASE_DIR = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
_MAINT_LOG_DIR = os.path.join(BASE_DIR, "maintenance_logs")
_SESSION_LOG_DIR = os.path.join(BASE_DIR, "logs")
_SECRET_CONFIG_KEYS = {
    "ap_password",
    "email_api_key",
    "email_service_password",
    "iot_api_key",
    "wifi_pwd",
    "wifi_password",
}


def _cmd_output(cmd: list, max_lines: int = 500) -> str:
    """Run a command and return its stdout, silently returning '' on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        lines = r.stdout.splitlines()[:max_lines]
        return "\n".join(lines)
    except Exception:
        return ""


def _zip_file(zf: zipfile.ZipFile, path: str, arcname: str, errors: list[str]) -> bool:
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            zf.write(path, arcname)
            return True
    except Exception as exc:
        errors.append(f"{arcname}: {exc}")
    return False


def _redact_config_value(key: str, value):
    key_l = key.lower()
    if key_l in _SECRET_CONFIG_KEYS or "password" in key_l or key_l.endswith("_key"):
        return "configured" if str(value or "").strip() else ""
    return value


def _redacted_json_file(path: str, errors: list[str]) -> str:
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key, value in list(data.items()):
                if isinstance(value, dict) and "value" in value:
                    value["value"] = _redact_config_value(key, value.get("value"))
                else:
                    data[key] = _redact_config_value(key, value)
        return json.dumps(data, indent=2, sort_keys=True)
    except Exception as exc:
        errors.append(f"{os.path.basename(path)}: {exc}")
        return ""


@router.get("/maintenance-logs")
async def api_maintenance_logs():
    """Bundle syslog and maintenance logs into a zip for debugging."""
    from bcmeter import incident_log

    buf = io.BytesIO()
    included: list[str] = []
    errors: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def add_file(path: str, arcname: str):
            if _zip_file(zf, path, arcname, errors):
                included.append(arcname)

        # 1. All maintenance logs, including QC reports and generated subfolders.
        if os.path.isdir(_MAINT_LOG_DIR):
            for root, _, files in os.walk(_MAINT_LOG_DIR):
                files.sort()
                rel_root = os.path.relpath(root, _MAINT_LOG_DIR)
                for name in files:
                    path = os.path.join(root, name)
                    rel = name if rel_root == "." else os.path.join(rel_root, name)
                    add_file(path, f"maintenance_logs/{rel}")

        # 2. syslog and rotations, matching the legacy debug download.
        for path in sorted(glob.glob("/var/log/syslog*")):
            add_file(path, f"system/{os.path.basename(path)}")

        # 3. Newest 10 session CSV files
        if os.path.isdir(_SESSION_LOG_DIR):
            csvs = []
            for f in os.listdir(_SESSION_LOG_DIR):
                if f.endswith(".csv") and f != "log_current.csv":
                    fp = os.path.join(_SESSION_LOG_DIR, f)
                    csvs.append((os.path.getmtime(fp), fp, f))
            csvs.sort(reverse=True)
            for _, fp, name in csvs[:10]:
                add_file(fp, f"sessions/{name}")

        # 4. dmesg — kernel log (I2C/SPI/GPIO hardware errors)
        dmesg = _cmd_output(["dmesg", "--time-format=iso"], max_lines=1000)
        if dmesg:
            zf.writestr("system/dmesg.txt", dmesg)
            included.append("system/dmesg.txt")

        # 5. journalctl for bcMeter and hardware support services
        for unit, lines in (("bcMeter", 500), ("bcmeter", 500), ("pigpiod", 200), ("NetworkManager", 300)):
            out = _cmd_output(["journalctl", "-u", unit, "-n", str(lines), "--no-pager"])
            if out:
                arcname = f"system/journalctl_{unit}.txt"
                zf.writestr(arcname, out)
                included.append(arcname)

        # 6. Redacted config files. Keep shape for debugging, never ship secrets.
        for name in ("bcMeter_config.json", "bcMeter_wifi.json"):
            path = os.path.join(BASE_DIR, name)
            if os.path.isfile(path):
                content = _redacted_json_file(path, errors)
                if content:
                    arcname = name.replace(".json", ".redacted.json")
                    zf.writestr(arcname, content)
                    included.append(arcname)

        # 7. Incident log
        try:
            entries = json.loads(incident_log.to_json())
            lines = []
            for e in entries:
                ts = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"[{ts}] [{e['s'].upper():>5}] {e['v']}")
            if lines:
                zf.writestr("incident_log.txt", "\n".join(lines))
                included.append("incident_log.txt")
        except Exception:
            pass

        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "device": socket.gethostname(),
            "base_dir": BASE_DIR,
            "included": sorted(included),
            "errors": errors,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))

    buf.seek(0)

    device_name = "bcmeter"
    if _cfg:
        device_name = _cfg.get_string("device_name", "bcmeter")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{device_name}_debug_{ts}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
