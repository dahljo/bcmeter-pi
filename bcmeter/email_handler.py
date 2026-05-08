"""Notification system via AWS Lambda with outbox queue and retry.

Sends all notifications as HTTP POST JSON to the bcMeter Lambda endpoint.
The Lambda backend handles email delivery. Matches the ESP32 architecture.

No direct SMTP — all email delivery is server-side via the Lambda proxy.
"""

import base64
import csv
import io
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import urllib.request
import urllib.error
import uuid
import zlib
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("bcmeter.email")

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_PLACEHOLDER_EMAILS = {
    "your@email.address",
    "email@example.com",
    "name@example.com",
    "test@example.com",
}

# AWS Lambda endpoint (not a secret — requires API key for auth)
LAMBDA_URL = "https://xwqm43fafwo7w65d4lno3nspzu0ovykv.lambda-url.eu-north-1.on.aws"

# Directories
_base_dir = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
OUTBOX_DIR = os.path.join(_base_dir, "outbox")
OUTBOX_SENT_DIR = os.path.join(OUTBOX_DIR, "sent")
OUTBOX_ATTACH_DIR = os.path.join(OUTBOX_DIR, "attachments")

# Cooldown state (in-memory)
_cooldowns: dict = {}  # {mail_type: last_sent_time}
_session_start: float = 0.0
_session_flags: set = set()
_cooldown_lock = threading.Lock()

# Hardcoded cooldown intervals matching ESP32 email_handler.cpp
_COOLDOWN_INTERVALS: dict[str, float] = {
    "Filter":       12 * 3600,   # 12 hours
    "Pump":         1 * 3600,    # 1 hour
    "HighHumidity": 2 * 3600,    # 2 hours
    "SignalNoise":  24 * 3600,   # 24 hours
    "LowDisk":      12 * 3600,   # 12 hours
    "FirmwareUpdate": 12 * 3600, # 12 hours
    "NegativeBC":   4 * 3600,    # 4 hours (Status subtype)
    "ADCRecovery":  30 * 60,     # 30 minutes
    "FlowRecovery": 30 * 60,     # 30 minutes
    "FlowBump":     60 * 60,     # 1 hour
    "Error":        30 * 60,     # 30 minutes
}

# Sender worker
_sender_thread = None
_sender_wakeup = None
_sender_stop = None


def is_valid_email_address(address: str) -> bool:
    address = str(address or "").strip()
    lowered = address.lower()
    if not address or lowered in _PLACEHOLDER_EMAILS:
        return False
    if len(address) > 254 or any(ch.isspace() for ch in address):
        return False
    if "`" in address or "," in address or ";" in address:
        return False
    if not _EMAIL_RE.fullmatch(address):
        return False
    local, domain = address.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    tld = domain.rsplit(".", 1)[-1]
    return len(tld) >= 2 and tld.isalpha()


def normalize_recipients(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates = raw.split(",")
    else:
        candidates = []
        try:
            iterator = iter(raw)
        except TypeError:
            iterator = [raw]
        for item in iterator:
            candidates.extend(str(item).split(","))

    recipients: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        addr = str(item or "").strip().lower()
        if not is_valid_email_address(addr):
            if addr:
                logger.info("Ignoring invalid email recipient: %s", addr)
            continue
        if addr not in seen:
            recipients.append(addr)
            seen.add(addr)
    return recipients

# Templates matching ESP32 EmailHandler
# Notification types matching ESP32 email_handler.cpp.
# The Lambda backend uses "notification_type" to route; templates here
# are for the fallback body text only — the data JSON is what matters.
TEMPLATES = {
    # --- ESP32 type: "Onboarding" (startup, wifi connected, modem online) ---
    "Onboarding": {
        "subject": "Device Online and Ready",
        "body": "{message}",
        "needs_attachment": False,
    },
    # --- Brief reconnection notice (welcome already sent) ---
    "Online": {
        "subject": "Device Online",
        "body": "{message}",
        "needs_attachment": False,
    },
    # --- ESP32 type: "WarmUp" ---
    "WarmUp": {
        "subject": "Warmup Complete",
        "body": "{message}",
        "needs_attachment": False,
    },
    # --- ESP32 type: "Filter" ---
    "Filter": {
        "subject": "Maintenance Required: Change Filter Strip",
        "body": "{message}",
        "needs_attachment": False,
    },
    # --- ESP32 type: "Pump" ---
    "Pump": {
        "subject": "Critical Alert: Airflow Failure",
        "body": "{message}",
        "needs_attachment": True,
    },
    # --- ESP32 type: "Log" ---
    "Log": {
        "subject": "Data Export",
        "body": (
            "Please find attached the CSV log file for the current session.\n\n"
            "This file contains raw sensor data and calculated "
            "Black Carbon concentrations."
        ),
        "needs_attachment": True,
    },
    # --- ESP32 type: "HighHumidity" ---
    "HighHumidity": {
        "subject": "High Humidity Warning",
        "body": "{message}",
        "needs_attachment": False,
    },
    # --- ESP32 type: "SignalNoise" (also used for BadWiFi) ---
    "SignalNoise": {
        "subject": "Signal Quality Warning",
        "body": "{message}",
        "needs_attachment": True,
    },
    # --- ESP32 type: "LowDisk" ---
    "LowDisk": {
        "subject": "Storage Space Warning",
        "body": "{message}",
        "needs_attachment": False,
    },
    # --- ESP32 type: "Status" (session start, negative BC, flow events, ADC recovery) ---
    "Status": {
        "subject": "Status: {event}",
        "body": "{message}",
        "needs_attachment": False,
    },
    # --- Pi-only: temperature (no ESP32 equivalent) ---
    "TemperatureWarning": {
        "subject": "Temperature Outside Operating Range",
        "body": "{message}",
        "needs_attachment": False,
    },
    # --- OTA update notifications (matching ESP32 email_handler.cpp) ---
    "FirmwareUpdate": {
        "subject": "Firmware Update Available",
        "body": "{message}",
        "needs_attachment": False,
    },
    "FirmwareUpdated": {
        "subject": "Firmware Updated Successfully",
        "body": "{message}",
        "needs_attachment": False,
    },
    "QCFinal": {
        "subject": "QC Final: {result}",
        "body": "{message}",
        "needs_attachment": False,
    },
}


def _ensure_outbox_dirs():
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    os.makedirs(OUTBOX_SENT_DIR, exist_ok=True)
    os.makedirs(OUTBOX_ATTACH_DIR, exist_ok=True)


def can_send_mail(mail_type: str, min_interval_seconds: float) -> bool:
    """Check if enough time has passed since the last mail of this type."""
    with _cooldown_lock:
        last_sent = _cooldowns.get(mail_type, 0.0)
        if last_sent == 0.0:
            return True
        return time.time() - last_sent >= min_interval_seconds


def set_last_mail_time(mail_type: str):
    with _cooldown_lock:
        _cooldowns[mail_type] = time.time()


def set_session_start():
    global _session_start
    with _cooldown_lock:
        if _session_start == 0.0:
            _session_start = time.time()


def get_session_flag(flag: str) -> bool:
    with _cooldown_lock:
        return flag in _session_flags


def set_session_flag(flag: str):
    with _cooldown_lock:
        _session_flags.add(flag)


def _snapshot_attachment(payload: str) -> str | None:
    """Copy current log to outbox for attachment."""
    if payload not in ("Log", "Pump", "SignalNoise"):
        return None
    src = os.path.join(_base_dir, "logs", "log_current.csv")
    if not os.path.exists(src) or os.path.getsize(src) < 500:
        return None
    _ensure_outbox_dirs()
    ts = datetime.now().strftime("%y%m%d_%H%M%S")
    hostname = socket.gethostname()
    dst_name = f"{hostname}_{payload}_{ts}_{uuid.uuid4().hex[:8]}.csv"
    dst = os.path.join(OUTBOX_ATTACH_DIR, dst_name)
    try:
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return None


def _format_body(payload: str, data: dict = None) -> str:
    """Format email body from template + data."""
    template = TEMPLATES.get(payload)
    if not template:
        return f"Notification: {payload}"
    body = template["body"]
    fmt_data = {
        "hostname": socket.gethostname(),
        "ip": "",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if data:
        fmt_data.update(data)
    try:
        return body.format(**fmt_data)
    except (KeyError, ValueError):
        return body


def _get_config() -> dict:
    """Read config values needed for notification delivery."""
    cfg = {}
    cfg_path = os.path.join(_base_dir, "bcMeter_config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                raw = json.load(f)
            cfg = {k: v.get("value", v) if isinstance(v, dict) else v
                   for k, v in raw.items()}
        except Exception:
            pass
    return cfg


def _configured_api_key(cfg: dict | None = None) -> str:
    """Return the single configured Lambda API key."""
    cfg = cfg or _get_config()
    value = str(cfg.get("email_api_key", "") or "").strip()
    return value if value and value not in ("configured", "email_service_password", "your_api_key", "iot_api_key") else ""


def get_configured_api_key(cfg: dict | None = None) -> str:
    return _configured_api_key(cfg)


def _configured_lambda_url(cfg: dict | None = None) -> str:
    cfg = cfg or _get_config()
    return str(cfg.get("email_service_url") or cfg.get("iot_url") or LAMBDA_URL)


def has_email_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or _get_config()
    api_key = _configured_api_key(cfg)
    mail_to = str(cfg.get("mail_logs_to", "your@email.address") or "")
    receivers = normalize_recipients(mail_to)
    return bool(api_key and receivers)


def _set_config_flag(key: str, value):
    """Set a single flag in the config JSON file."""
    cfg_path = os.path.join(_base_dir, "bcMeter_config.json")
    try:
        raw = {}
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                raw = json.load(f)
        raw[key] = {"value": value}
        tmp = cfg_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f)
        os.replace(tmp, cfg_path)
    except Exception as e:
        logger.warning(f"Failed to set config flag {key}: {e}")


_MODEM_KEEP_EXACT = {
    "bcmDate", "bcmTime", "AAE", "Temperature", "sht_humidity",
    "airflow", "relativeLoad", "PM2.5", "PM10",
}
_MODEM_KEEP_PREFIX = ("BCngm3", "BCugm3")


def _keep_aae_for_config(cfg: dict) -> bool:
    try:
        return int(float(cfg.get("num_channels", 1))) > 1
    except (TypeError, ValueError):
        return False


def _is_modem_keep_col(col_name: str, keep_aae: bool) -> bool:
    name = col_name.strip()
    if name == "AAE" and not keep_aae:
        return False
    return name in _MODEM_KEEP_EXACT or any(name.startswith(p) for p in _MODEM_KEEP_PREFIX)


def _abbreviate_csv_for_cellular(csv_data: str, cfg: dict | None = None) -> str:
    """Reduce cellular log CSV like ESP32, omitting AAE on 880nm-only devices."""
    if not csv_data:
        return csv_data
    if cfg is None:
        cfg = _get_config()
    first_line = csv_data.splitlines()[0] if csv_data.splitlines() else ""
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    keep_aae = _keep_aae_for_config(cfg)
    try:
        src = io.StringIO(csv_data)
        reader = csv.reader(src, delimiter=delimiter)
        header = next(reader, None)
        if not header:
            return csv_data
        keep_idx = [i for i, col in enumerate(header) if _is_modem_keep_col(col, keep_aae)]
        if not keep_idx:
            return csv_data
        out = io.StringIO()
        writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
        writer.writerow([header[i] for i in keep_idx])
        for row in reader:
            if not row:
                continue
            writer.writerow([row[i] if i < len(row) else "" for i in keep_idx])
        return out.getvalue()
    except Exception as e:
        logger.warning("CSV abbreviation failed, sending original log: %s", e)
        return csv_data


def _post_json(url: str, api_key: str, device_id: str,
               payload: dict) -> tuple[bool, str]:
    """HTTP POST JSON to Lambda endpoint. Returns (success, error)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("x-device-id", device_id)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, ""
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


def _send_notification(payload: str, data: dict = None,
                       attachment_path: str = None) -> tuple[bool, str]:
    """Send notification to Lambda endpoint. Returns (success, error)."""
    cfg = _get_config()

    api_key = _configured_api_key(cfg)
    mail_to = cfg.get("mail_logs_to", "your@email.address")

    if not api_key:
        return False, "No API key configured"

    receivers = normalize_recipients(mail_to)
    if not receivers:
        return False, "No recipients configured"

    hostname = socket.gethostname()
    device_id = cfg.get("device_name", f"bcMeter_{hostname}")
    template = TEMPLATES.get(payload, {})
    raw_subject = template.get("subject", payload)
    try:
        raw_subject = raw_subject.format(**(data or {}))
    except (KeyError, ValueError, IndexError):
        pass
    subject = "bcMeter Report: " + raw_subject
    body = _format_body(payload, data)

    # Build notification JSON matching ESP32 format
    local_ip = _device_ip()
    notification = {
        "type": "notification",
        "notification_type": payload,
        "recipients": receivers,
        "device_id": device_id,
        "hostname": hostname,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "subject": subject,
        "body": body,
        "data": data or {},
        "local_ip": local_ip,
    }

    # Handle file attachment — compress + base64
    if template.get("needs_attachment") and attachment_path:
        if os.path.exists(attachment_path) and os.path.getsize(attachment_path) >= 500:
            try:
                with open(attachment_path, "rb") as f:
                    raw_content = f.read()
                compressed = zlib.compress(raw_content, 6)
                encoded = base64.b64encode(compressed).decode("ascii")
                ts = datetime.now().strftime("%y%m%d_%H%M")
                notification["filename"] = f"{hostname}_{ts}.csv"
                notification["content_b64"] = encoded
                notification["compressed"] = True
                notification["original_size"] = len(raw_content)
                notification["compressed_size"] = len(compressed)
            except Exception as e:
                logger.warning(f"Failed to attach file: {e}")

    ok, err = _post_json(_configured_lambda_url(cfg), api_key, device_id, notification)
    if ok:
        logger.info(f"Notification sent: {payload}")
    else:
        logger.warning(f"Notification failed ({payload}): {err}")
    return ok, err


# --- Outbox queue (persistent retry with backoff) ---

def _atomic_write_json(path: str, obj):
    tmp = f"{path}.tmp_{uuid.uuid4().hex[:8]}"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _next_backoff(attempts: int) -> int:
    base = 60
    cap = 3600
    delay = base * (2 ** max(0, attempts - 1))
    return min(delay, cap)


def _process_outbox_job(path: str, job: dict):
    """Process a single outbox job."""
    payload = job.get("payload", "")
    data = job.get("data")
    attachment = job.get("attachment")

    ok, err = _send_notification(payload, data, attachment)
    if ok:
        set_last_mail_time(payload)
        # Clean up attachment file
        if attachment and os.path.exists(attachment):
            try:
                os.remove(attachment)
            except Exception:
                pass
        try:
            os.replace(path, os.path.join(OUTBOX_SENT_DIR, os.path.basename(path)))
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
        return

    attempts = int(job.get("attempts", 0)) + 1
    if attempts >= 10:
        logger.warning(f"Outbox job dropped after 10 attempts: {payload}")
        if attachment and os.path.exists(attachment):
            try:
                os.remove(attachment)
            except Exception:
                pass
        try:
            os.replace(path, os.path.join(OUTBOX_SENT_DIR,
                                          "FAILED_" + os.path.basename(path)))
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
        return

    job["attempts"] = attempts
    job["last_error"] = err or ""
    job["next_retry_at"] = time.time() + _next_backoff(attempts)
    try:
        _atomic_write_json(path, job)
    except Exception:
        pass


def _sender_worker_loop():
    """Background worker processing the outbox queue."""
    while not _sender_stop.is_set():
        processed = False
        try:
            jobs = sorted(
                f for f in os.listdir(OUTBOX_DIR) if f.endswith(".json")
            )
            for name in jobs:
                path = os.path.join(OUTBOX_DIR, name)
                try:
                    with open(path) as f:
                        job = json.load(f)
                except Exception:
                    continue
                if float(job.get("next_retry_at", 0)) > time.time():
                    continue
                processed = True
                _process_outbox_job(path, job)
        except Exception as e:
            logger.error(f"Sender worker error: {e}")

        if not processed:
            _sender_wakeup.wait(30)
            _sender_wakeup.clear()


def init_sender():
    """Start the background sender worker."""
    global _sender_thread, _sender_wakeup, _sender_stop
    if _sender_thread is not None and _sender_thread.is_alive():
        return
    _ensure_outbox_dirs()
    # Purge stale outbox jobs from previous sessions to avoid duplicate sends
    try:
        for name in os.listdir(OUTBOX_DIR):
            if name.endswith(".json"):
                path = os.path.join(OUTBOX_DIR, name)
                try:
                    with open(path) as f:
                        job = json.load(f)
                    att = job.get("attachment")
                    if att and os.path.exists(att):
                        os.remove(att)
                except Exception:
                    pass
                os.remove(path)
    except Exception:
        pass
    _sender_wakeup = threading.Event()
    _sender_stop = threading.Event()
    _sender_thread = threading.Thread(target=_sender_worker_loop, daemon=True)
    _sender_thread.start()


def stop_sender():
    """Stop the background sender worker."""
    if _sender_stop:
        _sender_stop.set()
    if _sender_wakeup:
        _sender_wakeup.set()


def send_email(payload: str, data: dict = None) -> bool:
    """Enqueue a notification for delivery. Returns True if enqueued."""
    init_sender()
    attachment = _snapshot_attachment(payload)

    job = {
        "id": uuid.uuid4().hex,
        "payload": payload,
        "data": data or {},
        "attachment": attachment,
        "created_at": time.time(),
        "attempts": 0,
        "next_retry_at": 0,
        "last_error": "",
    }

    try:
        _ensure_outbox_dirs()
        # Replace existing job of same type
        for name in os.listdir(OUTBOX_DIR):
            if not name.endswith(".json"):
                continue
            old_path = os.path.join(OUTBOX_DIR, name)
            try:
                with open(old_path) as f:
                    old = json.load(f)
                if old.get("payload") == payload:
                    old_att = old.get("attachment")
                    os.remove(old_path)
                    if old_att and os.path.exists(old_att):
                        os.remove(old_att)
            except Exception:
                pass

        name = f"{int(job['created_at'])}_{job['id']}.json"
        _atomic_write_json(os.path.join(OUTBOX_DIR, name), job)
        set_last_mail_time(payload)
        if _sender_wakeup:
            _sender_wakeup.set()
        return True
    except Exception as e:
        logger.error(f"Failed to enqueue notification: {e}")
        return False


# ---------------------------------------------------------------------------
# Convenience wrappers — notification types match ESP32 email_handler.cpp
# ---------------------------------------------------------------------------

def _geo_data() -> dict:
    """Collect geolocation fields for notification payloads."""
    try:
        from . import geoloc
        ok, lat, lon = geoloc.get_location()
        if ok:
            return {
                "latitude": lat,
                "longitude": lon,
                "location_url": f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}",
            }
    except Exception:
        pass
    return {}


def _device_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def send_filter_alert(current_atn: float, atn_rate: float = 0.0,
                      loading_pct: int = 0, device_name: str = "bcMeter",
                      initial_loading_pct: float = None):
    if not can_send_mail("Filter", _COOLDOWN_INTERVALS["Filter"]):
        return
    if loading_pct == 0:
        import math
        loading_pct = int((1.0 - math.exp(-current_atn / 100.0)) * 100 + 0.5)
    geo = _geo_data()
    msg = f"Filter loading at {loading_pct}%"
    msg += f"\nDevice: {device_name}"
    if geo.get("location_url"):
        msg += f"\nMap: {geo['location_url']}"
    data = {
        "current_atn": current_atn,
        "loading_pct": loading_pct,
        "atn_rate": atn_rate,
        "filter_status": 5,
        "device_name": device_name,
        "message": msg,
    }
    if initial_loading_pct is not None:
        data["initial_loading_pct"] = initial_loading_pct
    send_email("Filter", data)


def send_pump_error(flow_rate: float):
    if not can_send_mail("Pump", _COOLDOWN_INTERVALS["Pump"]):
        return
    send_email("Pump", {
        "flow_rate": flow_rate,
        "message": (
            "Airflow failure detected (0 L/min). Measurement paused.\n"
            "1. Check pump cable\n2. Check tubing for kinks\n3. Ensure air inlet is clear"
        ),
    })


def send_signal_noise_alert(details: str):
    cfg = _get_config()
    if not cfg.get("send_verbose_emails", False):
        return
    if not can_send_mail("SignalNoise", _COOLDOWN_INTERVALS["SignalNoise"]):
        return
    send_email("SignalNoise", {
        "details": details,
        "message": f"Optical signal quality issue: {details}",
    })


def send_negative_bc_alert(session_avg: float, hour_avg: float):
    if not can_send_mail("NegativeBC", _COOLDOWN_INTERVALS["NegativeBC"]):
        return
    send_email("Status", {
        "event": "Prolonged Negative BC",
        "bc_session_avg": session_avg,
        "bc_hour_avg": hour_avg,
        "message": "BC readings have been negative for over 1 hour. Possible clean air or instrument issue.",
    })


def send_flow_bump(old_target: float, new_target: float):
    cfg = _get_config()
    if not cfg.get("send_verbose_emails", False):
        return
    if not can_send_mail("FlowBump", _COOLDOWN_INTERVALS["FlowBump"]):
        return
    send_email("Status", {
        "event": "Airflow Target Increased",
        "old_target_lpm": old_target,
        "new_target_lpm": new_target,
        "message": "Target airflow too close to stall, bumped by 50 ml/min.",
    })


def send_temperature_alert(temp: float):
    send_email("TemperatureWarning", {
        "temp": temp,
        "message": f"Temperature {temp:.1f}C is outside operating range (5-40C).",
    })


def send_current_log(bc_session_avg: float = None, bc_hour_avg: float = None,
                      loading_pct: float = None, session_hours: float = None,
                      initial_loading_pct: float = None):
    data = {}
    if bc_session_avg is not None:
        data["bc_session_avg"] = bc_session_avg
    if bc_hour_avg is not None:
        data["bc_hour_avg"] = bc_hour_avg
    if loading_pct is not None:
        data["loading_pct"] = loading_pct
    if session_hours is not None:
        data["session_hours"] = session_hours
    if initial_loading_pct is not None:
        data["initial_loading_pct"] = initial_loading_pct
    send_email("Log", data if data else None)


# --- Team sharing: direct upload (no email) ---

_team_sent_offset: int = 0
_team_sent_lock = threading.Lock()


def reset_team_offset():
    """Reset team upload offset (call on new session)."""
    global _team_sent_offset
    with _team_sent_lock:
        _team_sent_offset = 0


def send_team_log() -> bool:
    """Direct upload of incremental CSV to bcMeter archive. No email sent."""
    global _team_sent_offset
    cfg = _get_config()
    api_key = _configured_api_key(cfg)
    if not api_key:
        return False

    log_path = os.path.join(_base_dir, "logs", "log_current.csv")
    if not os.path.exists(log_path):
        return False

    with open(log_path, "r") as f:
        full_csv = f.read()
    if len(full_csv) == 0:
        return False

    with _team_sent_lock:
        offset = _team_sent_offset

    # Get header + new rows since last upload
    header_end = full_csv.find("\n")
    if header_end < 0:
        return False
    header = full_csv[:header_end + 1]

    if offset == 0 or offset >= len(full_csv):
        csv_content = full_csv
    else:
        new_rows = full_csv[offset:]
        if len(new_rows) == 0:
            return False
        csv_content = header + new_rows

    new_offset = len(full_csv)

    # Cap at 30KB
    if len(csv_content) > 30000:
        csv_content = csv_content[:header_end + 1] + csv_content[-(30000 - header_end - 1):]

    device_id = cfg.get("device_name", f"bcMeter_{socket.gethostname()}")
    hostname = socket.gethostname()
    session_file = os.path.basename(log_path)

    payload = {
        "device_id": device_id,
        "filename": session_file,
        "content": csv_content,
        "shared": True,
        # No recipients — Lambda will archive without sending email
    }

    # Add location if available
    try:
        from . import geoloc
        ok, lat, lon = geoloc.get_location()
        if ok:
            payload["lat"] = lat
            payload["lon"] = lon
    except Exception:
        pass

    ok, err = _post_json(_configured_lambda_url(cfg), api_key, device_id, payload)
    if ok:
        with _team_sent_lock:
            _team_sent_offset = new_offset
        logger.info(f"[Team] Direct upload OK ({len(csv_content)} bytes)")
    else:
        logger.warning(f"[Team] Direct upload failed: {err}")
    return ok


def send_startup_mail():
    ip = _device_ip()
    geo = _geo_data()
    msg = f"Device started\nIP: {ip}"
    if "latitude" in geo:
        msg += f"\nMap: {geo['location_url']}"
    send_email("Onboarding", {
        "connection": "wifi",
        "ip": ip,
        "fw_version": f"2.0 (Pi)",
        "message": msg,
        **geo,
    })


def send_adc_saturation_recovery(ch: int, old_duty: int, new_duty: int):
    cfg = _get_config()
    if not cfg.get("send_verbose_emails", False):
        return
    if not can_send_mail("ADCRecovery", _COOLDOWN_INTERVALS["ADCRecovery"]):
        return
    send_email("Status", {
        "event": "ADC Saturation Recovery",
        "channel": ch,
        "old_duty": old_duty,
        "new_duty": new_duty,
        "message": "LED duty lowered in-session to recover from ADC saturation. Correction factor unchanged.",
    })


def send_flow_recovery(flow: float, duty: int):
    cfg = _get_config()
    if not cfg.get("send_verbose_emails", False):
        return
    if not can_send_mail("FlowRecovery", _COOLDOWN_INTERVALS["FlowRecovery"]):
        return
    send_email("Status", {
        "event": "Airflow Recovery",
        "flow_lpm": flow,
        "duty": duty,
        "message": "Pump duty increased to recover airflow.",
    })


def send_wifi_connected(modem_available: bool = False,
                        modem_operator: str = "",
                        modem_signal: str = ""):
    ip = _device_ip()
    geo = _geo_data()
    hostname = socket.gethostname()
    msg = f"Access the device interface at http://{ip}/"
    if "latitude" in geo:
        msg += f"\nMap: {geo['location_url']}"

    payload = {
        "connection": "wifi",
        "ip": ip,
        "local_ip": ip,
        "device_url": f"http://{ip}/",
        "network": hostname,
        **geo,
    }

    if modem_available:
        payload["fallback"] = "4G"
        if modem_operator:
            payload["fallback_operator"] = modem_operator
        if modem_signal:
            payload["fallback_signal"] = modem_signal
        fallback_line = "\n4G fallback: available"
        if modem_operator:
            fallback_line += f" ({modem_operator})"
        msg += fallback_line

    payload["message"] = msg

    # First-time welcome email vs brief online notice
    cfg = _get_config()
    welcome_sent = cfg.get("onboarding_email_sent", False)
    if not welcome_sent:
        logger.info("Sending Welcome (first-time Onboarding)")
        send_email("Onboarding", payload)
        _set_config_flag("onboarding_email_sent", True)
    else:
        logger.info("Sending brief Online notice (welcome already sent)")
        send_email("Online", payload)


def send_session_start(session_file: str):
    cfg = _get_config()
    flow_lpm = float(cfg.get("airflow_per_minute", 0.25))
    flow_ml = int(flow_lpm * 1000)

    # Log delivery info
    log_delivery = ""
    send_log = cfg.get("send_log_by_mail", False)
    recipients = normalize_recipients(cfg.get("mail_logs_to", ""))
    interval = float(cfg.get("mail_sending_interval", 24))
    if send_log and recipients:
        interval_str = f"{interval:.0f}" if interval == int(interval) else f"{interval:.1f}"
        log_delivery = f"Every {interval_str}h to {', '.join(recipients)}"

    # Firmware version
    fw = "unknown"
    try:
        from bcmeter import __version__
        fw = __version__
    except Exception:
        pass

    send_email("Status", {
        "event": "Measurement Session Started",
        "filename": session_file,
        "session_id": session_file,
        "new_session": True,
        "flow_target_ml": flow_ml,
        "firmware": fw,
        "log_delivery": log_delivery,
        "message": f"New measurement session started: {session_file}",
    })


def send_ota_available(new_version: str, notes: str):
    """Notify user that a firmware update is available. Matches ESP32 sendOtaAvailable."""
    if not can_send_mail("FirmwareUpdate", _COOLDOWN_INTERVALS["FirmwareUpdate"]):
        return
    current = "unknown"
    try:
        from bcmeter import __version__
        current = __version__
    except Exception:
        pass
    cfg = _get_config()
    device_name = cfg.get("device_name", "bcMeter")
    send_email("FirmwareUpdate", {
        "new_version": new_version,
        "current_version": current,
        "notes": notes,
        "device_name": device_name,
        "message": f"Firmware update available: {current} → {new_version}\n{notes}",
    })


def send_ota_success(old_version: str, new_version: str):
    """Notify user that firmware was updated successfully. Matches ESP32 sendOtaSuccess."""
    cfg = _get_config()
    device_name = cfg.get("device_name", "bcMeter")
    send_email("FirmwareUpdated", {
        "old_version": old_version,
        "new_version": new_version,
        "device_name": device_name,
        "message": f"Firmware updated: {old_version} → {new_version}",
    })


def send_bad_wifi_alert(drop_count: int, minutes: int):
    send_email("SignalNoise", {
        "event": "Unstable Connection Detected",
        "drops": drop_count,
        "duration_minutes": minutes,
        "message": "Connection is flaky. Reconnection emails paused.",
    })


def send_storage_alert(used_gb: float, total_gb: float):
    if not can_send_mail("LowDisk", _COOLDOWN_INTERVALS["LowDisk"]):
        return
    free_kb = int((total_gb - used_gb) * 1024 * 1024)
    send_email("LowDisk", {
        "disk_free": f"{free_kb} KB",
        "message": f"Low disk space: {free_kb} KB free.",
    })


def send_high_humidity_alert(humidity: float):
    if not can_send_mail("HighHumidity", _COOLDOWN_INTERVALS["HighHumidity"]):
        return
    send_email("HighHumidity", {
        "hum": f"{humidity:.1f}%",
        "message": f"Humidity elevated: {humidity:.1f}%. May affect measurements.",
    })


# Deferred modem onboarding: cached when mail_logs_to is empty at modem init
_pending_modem_onboarding = False
_modem_ob_cache = {}


def send_modem_online(wan_ip: str, city: str, country: str,
                       signal: str = "", operator: str = "",
                       cpsi: str = ""):
    global _pending_modem_onboarding, _modem_ob_cache

    cfg = _get_config()
    mail_to = cfg.get("mail_logs_to", "your@email.address") if cfg else ""
    receivers = normalize_recipients(mail_to)
    if not receivers:
        logger.info("Modem onboarding deferred — no mail_logs_to configured yet")
        _modem_ob_cache = dict(wan_ip=wan_ip, city=city, country=country,
                               signal=signal, operator=operator, cpsi=cpsi)
        _pending_modem_onboarding = True
        return

    geo = _geo_data()
    msg = f"Device online via 4G | WAN: {wan_ip}"
    if city:
        msg += f" | {city}, {country}"
    payload = {
        "connection": "cellular",
        "ip_wan": wan_ip,
        "city": city,
        "country": country,
        "signal": signal,
        "operator": operator,
        "message": msg,
        **geo,
    }
    if cpsi:
        payload["cpsi"] = cpsi
    send_email("Onboarding", payload)


def retry_deferred_modem_onboarding():
    """Retry sending modem onboarding email if it was deferred due to empty recipients."""
    global _pending_modem_onboarding
    if not _pending_modem_onboarding:
        return
    cfg = _get_config()
    mail_to = cfg.get("mail_logs_to", "your@email.address") if cfg else ""
    receivers = normalize_recipients(mail_to)
    if not receivers:
        return
    logger.info("Retrying deferred modem onboarding")
    _pending_modem_onboarding = False
    send_modem_online(**_modem_ob_cache)


# ── Debug Mobile ──────────────────────────────────────────────────────────

_debug_mobile_status = {
    "chunks_sent": 0, "chunks_total": 0,
    "done": False, "success": False, "running": False,
}


def get_debug_mobile_status() -> dict:
    return dict(_debug_mobile_status)


def send_debug_mobile() -> bool:
    """Send current log + telemetry via modem (forceModem). Runs in background thread."""
    if _debug_mobile_status["running"]:
        return False

    def _task():
        _debug_mobile_status.update(running=True, done=False, success=False,
                                     chunks_sent=0, chunks_total=0)
        try:
            from bcmeter import storage, measure, state as st, modem as modem_mod

            cfg = _get_config()
            device_id = cfg.get("device_name", socket.gethostname()) + " [DEBUG-MOBILE]"

            # Build recipients: mail_logs_to + jd@bcmeter.org
            mail_to = cfg.get("mail_logs_to", "")
            receivers = normalize_recipients(mail_to)
            if "jd@bcmeter.org" not in receivers:
                receivers.append("jd@bcmeter.org")

            # 1. Find log data
            log_ok = False
            fname = ""
            csv_data = ""
            try:
                session_file = storage.get_session_filename()
                if session_file and os.path.exists(session_file):
                    fname = os.path.basename(session_file)
                    with open(session_file) as f:
                        csv_data = f.read(16384)
                if not csv_data:
                    logs = storage.list_logs() if hasattr(storage, "list_logs") else []
                    if logs:
                        last = logs[-1] if isinstance(logs[-1], str) else logs[-1].get("name", "")
                        if last and os.path.exists(last):
                            fname = os.path.basename(last)
                            with open(last) as f:
                                csv_data = f.read(16384)
            except Exception as e:
                logger.warning(f"[DebugMobile] Log read error: {e}")

            if csv_data:
                csv_data = _abbreviate_csv_for_cellular(csv_data, cfg)
                _debug_mobile_status["chunks_total"] = 1
                # Send via modem if available, else IP
                mgr = None
                try:
                    mgr = modem_mod.IoTManager(cfg)
                    if mgr.is_connected():
                        ok = mgr.upload_data(
                            csv_data.encode("utf-8"), fname, receivers,
                            modem_abbreviated=True,
                        )
                        log_ok = ok
                    else:
                        log_ok, _ = _send_log_over_ip(csv_data, fname, device_id, receivers)
                except Exception:
                    log_ok, _ = _send_log_over_ip(csv_data, fname, device_id, receivers)
                _debug_mobile_status["chunks_sent"] = 1 if log_ok else 0

            # 2. Send telemetry
            tel_data = {
                "event": "Debug Mobile Test",
                "version": cfg.get("version", "unknown"),
                "sampling": st.get("sampling", False),
                "modem_present": st.get("modem_present", False),
                "log_sent": log_ok,
                "log_file": fname,
            }
            try:
                tel_data["bc_session_avg"] = measure.get_session_avg_bc()
                tel_data["bc_hour_avg"] = measure.get_hour_avg_bc()
            except Exception:
                pass

            send_email("DebugMobile", tel_data)

            _debug_mobile_status.update(done=True, success=log_ok, running=False)
            logger.info(f"[DebugMobile] Done: log={log_ok}")
        except Exception as e:
            logger.error(f"[DebugMobile] Error: {e}")
            _debug_mobile_status.update(done=True, success=False, running=False)

    threading.Thread(target=_task, daemon=True, name="debug_mobile").start()
    return True


def _send_log_over_ip(csv_data, fname, device_id, receivers):
    """Send log data via IP (WiFi) path."""
    cfg = _get_config()
    api_key = _configured_api_key(cfg)
    if not api_key:
        return False, "No API key configured"
    receivers = normalize_recipients(receivers)
    csv_data = _abbreviate_csv_for_cellular(csv_data, cfg)
    notification = {
        "type": "notification",
        "notification_type": "LogUpload",
        "recipients": receivers,
        "device_id": device_id,
        "filename": fname,
        "content": csv_data,
        "modem_abbreviated": True,
    }
    return _post_json(_configured_lambda_url(cfg), api_key, device_id, notification)


def validate_api_key(api_key: str) -> tuple[bool, str]:
    """Quick ping to Lambda to check if an API key is accepted."""
    if not api_key or api_key in ("configured", "email_service_password", "your_api_key", "iot_api_key"):
        return False, "No API key provided"
    payload = json.dumps({"_coap_test": True}).encode("utf-8")
    req = urllib.request.Request(_configured_lambda_url(), data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("x-device-id", socket.gethostname())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, ""
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "Invalid API key"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def send_qc_final_report(report: dict) -> tuple[bool, str]:
    """Send the compact QC result table through the existing Lambda HTML mail path."""
    if not has_email_configured():
        return False, "No recipients/API key configured"
    tests = []
    for step in report.get("steps", []):
        if step.get("passed"):
            result = "PASS"
        elif step.get("hard"):
            result = "FAIL"
        else:
            result = "WARN"
        tests.append({
            "test": step.get("name", ""),
            "result": result,
            "observed": step.get("observed", ""),
        })
    summary = report.get("summary", {})
    result = "PASS" if report.get("passed") else "FAIL"
    return _send_notification("QCFinal", {
        "event": "Raspberry Pi QC Final",
        "result": result,
        "tests": tests,
        "message": f"Pi QC {result}: {summary.get('device', 'bcMeter')}",
        "report_dir": report.get("report_dir"),
        "html_report": report.get("html_report"),
    })
