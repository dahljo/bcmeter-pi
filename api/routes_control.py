"""Device control endpoints.

Matches the ESP32 /api/control?action=... contract.
"""

import logging
import os
import subprocess
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

logger = logging.getLogger("bcmeter.api.control")

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_cfg = None
_state = None
_engine = None
_storage = None
_status_led = None


def set_dependencies(cfg, state_mgr, engine, storage, status_led=None):
    global _cfg, _state, _engine, _storage, _status_led
    _cfg = cfg
    _state = state_mgr
    _engine = engine
    _storage = storage
    _status_led = status_led


# ---------------------------------------------------------------------------
# Calibration state (mirrors ESP32 calLogBuf / calRunning / calDone globals)
# ---------------------------------------------------------------------------

_cal_lock = threading.Lock()
_cal_running = False
_cal_done = False
_cal_ok = False
_cal_start_ms = 0
_cal_end_ms = 0
_cal_log: list[str] = []


def _cal_log_fn(msg: str):
    """Callback passed to MeasureEngine.calibrate() for log streaming."""
    global _cal_log
    with _cal_lock:
        _cal_log.append(msg)


def _run_calibration():
    """Background thread target for calibration."""
    global _cal_running, _cal_done, _cal_ok, _cal_start_ms, _cal_end_ms, _cal_log

    with _cal_lock:
        _cal_running = True
        _cal_done = False
        _cal_ok = False
        _cal_start_ms = int(time.monotonic() * 1000)
        _cal_end_ms = 0
        _cal_log.clear()
        _cal_log.append("[Cal] Web calibration started\n")

    try:
        ok = _engine.calibrate(log_fn=_cal_log_fn)
    except Exception as exc:
        logger.exception("Calibration thread exception")
        ok = False
        _cal_log_fn(f"[Cal] Exception: {exc}\n")

    with _cal_lock:
        _cal_ok = ok
        _cal_done = True
        _cal_end_ms = int(time.monotonic() * 1000)
        _cal_running = False


def get_calibration_state() -> dict:
    """Return calibration state snapshot (used by routes_calibration)."""
    with _cal_lock:
        running = _cal_running
        done = _cal_done
        ok = _cal_ok
        start = _cal_start_ms
        end = _cal_end_ms
        log_text = "".join(_cal_log)

    now_ms = int(time.monotonic() * 1000)
    if running:
        elapsed = (now_ms - start) if start > 0 else 0
    elif done and end > start:
        elapsed = end - start
    else:
        elapsed = 0

    return {
        "running": running,
        "done": done,
        "ok": ok,
        "elapsed_ms": elapsed,
        "log": log_text,
    }


# ---------------------------------------------------------------------------
# GET /api/control?action=...
# ---------------------------------------------------------------------------

@router.get("/control")
async def api_control(
    action: str = Query("", description="Control action"),
    ts: int = Query(0, description="Unix timestamp for synctime"),
    tz: str = Query("", description="Timezone string for synctime"),
    force: str = Query("0", description="Force flag for start"),
    indoor: str = Query("", description="One-shot indoor override: 1=indoor, 0=outdoor"),
):
    """Device control dispatcher matching ESP32 /api/control."""

    if not action:
        return PlainTextResponse("OK")

    # ----- start -----
    if action == "start":
        return _handle_start(force == "1", indoor)

    # ----- stop -----
    if action == "stop":
        if _state:
            _state.sampling = False
        return PlainTextResponse("Sampling stopped")

    # ----- reboot -----
    if action == "reboot":
        if _storage and _storage.session_active:
            _storage.end_session()
        # Fire-and-forget reboot
        threading.Thread(
            target=lambda: (time.sleep(0.5), subprocess.run(["sudo", "reboot"])),
            daemon=True,
        ).start()
        return PlainTextResponse("Rebooting...")

    # ----- shutdown -----
    if action == "shutdown":
        if _storage and _storage.session_active:
            _storage.end_session()
        threading.Thread(
            target=lambda: (time.sleep(0.5), subprocess.run(["sudo", "shutdown", "-h", "now"])),
            daemon=True,
        ).start()
        return PlainTextResponse("Shutting down...")

    # ----- synctime -----
    if action == "synctime":
        return _handle_synctime(ts, tz)

    # ----- cleardata -----
    if action == "cleardata":
        if _storage:
            _storage.delete_old_logs(keep_count=0)
        return PlainTextResponse("OK")

    # ----- clear_error -----
    if action == "clear_error":
        if _state:
            from bcmeter.errors import ErrorCode
            _state.set("error", ErrorCode.ERR_NONE)
        return PlainTextResponse("Error cleared")

    # ----- calibrate -----
    if action == "calibrate":
        return _handle_calibrate()

    # ----- debug_mobile -----
    if action == "debug_mobile":
        if not (_state and _state.get("modem_present", False)):
            return PlainTextResponse("Modem not available", status_code=409)
        try:
            from bcmeter import email_handler
            if email_handler.send_debug_mobile():
                return PlainTextResponse("Debug mobile started", status_code=202)
            return PlainTextResponse("Already running", status_code=409)
        except Exception as e:
            return PlainTextResponse(str(e), status_code=500)

    # ----- identify -----
    if action == "identify":
        if _status_led:
            _status_led.start_identify()
        return PlainTextResponse("Identifying")

    return PlainTextResponse("OK")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _handle_start(force: bool, indoor: str = "") -> PlainTextResponse:
    """Start sampling with pre-flight validation matching ESP32 logic.

    `indoor` is a one-shot override consumed on the next session start:
    "1" → indoor session, "0" → explicit outdoor, empty → no change.
    Set before time-sync / calibration guards so retries preserve it.
    """
    # Apply indoor override early, before any guard can bail out.
    if indoor in ("0", "1") and _engine is not None:
        _engine.set_next_session_indoor(indoor == "1")

    # Time sync check
    if not force and datetime.now().year <= 2024:
        return PlainTextResponse("Time not synced", status_code=409)

    # Calibration checks (skip in force mode)
    if not force and _cfg:
        cal_time = _cfg.get_string("last_cal_time", "never")
        if cal_time == "never":
            return PlainTextResponse("never_calibrated", status_code=423)
        # Check staleness (>28 days)
        try:
            ct = datetime.strptime(cal_time, "%Y-%m-%d %H:%M")
            if (datetime.now() - ct).total_seconds() > 28 * 86400:
                return PlainTextResponse("calibration_stale", status_code=424)
        except (ValueError, TypeError):
            pass

    # Clear previous error and start
    if _state:
        from bcmeter.errors import ErrorCode
        _state.set("error", ErrorCode.ERR_NONE)
        _state.sampling = True

    return PlainTextResponse("Sampling started")


def _handle_synctime(ts: int, tz: str) -> PlainTextResponse:
    """Set system time from browser-supplied Unix timestamp."""
    if ts <= 0:
        return PlainTextResponse("Missing ts param", status_code=400)

    try:
        # Set system clock via date command
        dt_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(
            ["sudo", "date", "-u", "-s", dt_str],
            capture_output=True, timeout=10,
        )
        logger.info("System time set to %s (ts=%d)", dt_str, ts)
        # Mark the next measurement row with a TIME_SYNC note so a
        # timestamp jump in the CSV is traceable to this event.
        if _state is not None and _state.sampling:
            _state.set("time_just_synced", True)
    except Exception as exc:
        logger.error("Failed to set system time: %s", exc)
        return PlainTextResponse(f"Failed: {exc}", status_code=500)

    # Apply timezone if provided
    if tz and _cfg:
        _cfg.set_string("timezone", tz)
        _cfg.save()
        try:
            subprocess.run(
                ["sudo", "timedatectl", "set-timezone", tz],
                capture_output=True, timeout=10,
            )
        except Exception:
            logger.debug("timedatectl timezone set failed (non-critical)")

    return PlainTextResponse("Time synced")


def _handle_calibrate() -> PlainTextResponse:
    """Start calibration in a background thread."""
    global _cal_running

    if _state and _state.sampling:
        return PlainTextResponse("Stop sampling first", status_code=409)

    with _cal_lock:
        if _cal_running:
            return PlainTextResponse("Calibration already running", status_code=409)

    if not _engine:
        return PlainTextResponse("Engine not available", status_code=503)

    t = threading.Thread(target=_run_calibration, daemon=True, name="calibration")
    t.start()

    return PlainTextResponse("Calibration started", status_code=202)
