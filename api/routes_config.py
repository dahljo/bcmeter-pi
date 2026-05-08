"""Configuration endpoints.

Matches the ESP32 /api/config, /api/device/rename, and /api/ap-security contracts.
"""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from bcmeter import avahi_alias
from bcmeter.identity import hostname_from_device_name, sync_system_hostname

logger = logging.getLogger("bcmeter.api.config")

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_cfg = None

_SECRET_KEYS = {"email_api_key", "email_service_password", "iot_api_key"}
_CANONICAL_SECRET_KEYS = {"email_api_key"}
_SECRET_PLACEHOLDERS = {"", "email_service_password", "your_api_key", "configured", "iot_api_key"}


def _secret_is_configured(value) -> bool:
    return str(value or "").strip() not in _SECRET_PLACEHOLDERS


def _mask_config_secrets(config: dict) -> dict:
    """Hide configured service credentials from browser-readable config."""
    for key in _SECRET_KEYS:
        entry = config.get(key)
        if not isinstance(entry, dict) or "value" not in entry:
            continue
        entry["value"] = "configured" if _secret_is_configured(entry.get("value")) else ""
    return config


def _drop_masked_secret_updates(body_str: str) -> str:
    """Do not overwrite stored secrets when a client echoes masked values."""
    try:
        incoming = json.loads(body_str)
    except Exception:
        return body_str
    if not isinstance(incoming, dict):
        return body_str
    changed = False
    for key in list(_SECRET_KEYS):
        if key not in incoming:
            continue
        if key not in _CANONICAL_SECRET_KEYS:
            incoming.pop(key, None)
            changed = True
            continue
        val = incoming[key]
        if isinstance(val, dict) and "value" in val:
            val = val.get("value")
        if str(val or "").strip() in _SECRET_PLACEHOLDERS:
            incoming.pop(key, None)
            changed = True
    return json.dumps(incoming) if changed else body_str


def set_dependencies(cfg):
    global _cfg
    _cfg = cfg


def _maybe_send_deferred_wifi_onboarding():
    """Send Phase 1 welcome if WiFi is already online and mail was just added."""
    if not _cfg:
        return
    try:
        if _cfg.get_bool("onboarding_step_one", False):
            return
        from bcmeter import email_handler
        if not email_handler.has_email_configured():
            return
        from bcmeter.state import state
        snap = state.snapshot()
        wifi_online = (
            snap.get("wifi_mode") == "sta"
            and bool(snap.get("wifi_ssid"))
            and (bool(snap.get("internet")) or bool(snap.get("time_synced")))
        )
        if not wifi_online:
            return
        logger.info("Sending deferred WiFi onboarding email after mail config update")
        email_handler.send_wifi_connected()
        _cfg.set_bool("onboarding_step_one", True)
        _cfg.save()
        logger.info("onboarding_step_one = true")
    except Exception as exc:
        logger.debug("Deferred WiFi onboarding check failed: %s", exc)


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------

@router.get("/config")
async def api_config_get():
    """Return full configuration JSON (same format as CfgStore.to_json)."""
    if not _cfg:
        return JSONResponse(content={}, status_code=503)

    raw = _cfg.to_json()
    return JSONResponse(content=_mask_config_secrets(json.loads(raw)))


# ---------------------------------------------------------------------------
# POST /api/config
# ---------------------------------------------------------------------------

@router.post("/config")
async def api_config_post(request: Request):
    """Apply configuration from JSON body.

    Accepts either ``{key: value}`` or ``{key: {value: ...}}`` format,
    matching the ESP32 CfgStore::applyJSON behaviour.
    """
    if not _cfg:
        return PlainTextResponse("Config store not available", status_code=503)

    try:
        body = await request.body()
        body_str = body.decode("utf-8")
    except Exception as exc:
        return PlainTextResponse(f"Bad request: {exc}", status_code=400)

    if not body_str or not body_str.strip():
        return PlainTextResponse("No body", status_code=400)

    body_str = _drop_masked_secret_updates(body_str)
    ok = _cfg.apply_json(body_str)
    if ok:
        # If mail_logs_to was just set and we have a deferred modem onboarding, send it now
        try:
            from bcmeter import email_handler
            email_handler.retry_deferred_modem_onboarding()
        except Exception:
            pass
        _maybe_send_deferred_wifi_onboarding()
        return PlainTextResponse("OK")
    return PlainTextResponse("Invalid config", status_code=400)


# ---------------------------------------------------------------------------
# POST /api/device/rename
# ---------------------------------------------------------------------------

@router.post("/device/rename")
async def api_device_rename(request: Request):
    """Rename the device (update config + system hostname).

    Expects JSON body: ``{"name": "new-name"}``.
    """
    if not _cfg:
        return PlainTextResponse("Config store not available", status_code=503)

    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400)

    name = body.get("name", "")
    if not isinstance(name, str) or not (1 <= len(name) <= 32):
        return PlainTextResponse("Name must be 1-32 chars", status_code=400)

    _cfg.set_string("device_name", name)
    _cfg.save()

    # Update system hostname (best effort)
    hostname = hostname_from_device_name(name)
    try:
        sync_system_hostname(name, reason="device rename")
        avahi_alias.refresh("device rename")
        logger.info("Device renamed to '%s' (hostname: %s)", name, hostname)
    except Exception as exc:
        logger.warning("Failed to set hostname: %s", exc)

    return PlainTextResponse("OK")


# ---------------------------------------------------------------------------
# GET /api/ap-security
# ---------------------------------------------------------------------------

@router.get("/ap-security")
async def api_ap_security_get():
    """Return AP security configuration."""
    if not _cfg:
        return JSONResponse(content={"secured": False, "password": ""}, status_code=503)

    return JSONResponse(content={
        "secured": _cfg.get_bool("ap_secured", False),
        "password": _cfg.get_string("ap_password", "bcMeterbcMeter"),
    })


# ---------------------------------------------------------------------------
# POST /api/ap-security
# ---------------------------------------------------------------------------

@router.post("/email/validate")
async def api_email_validate(request: Request):
    """Validate an email service API key against the Lambda endpoint."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"valid": False, "error": "Invalid JSON"}, status_code=400)

    api_key = body.get("api_key", "")
    if not api_key:
        return JSONResponse(content={"valid": False, "error": "No API key provided"})

    from bcmeter.email_handler import validate_api_key
    valid, err = validate_api_key(api_key)
    return JSONResponse(content={"valid": valid, "error": err})


@router.post("/ap-security")
async def api_ap_security_post(request: Request):
    """Update AP security settings.

    Expects JSON body: ``{"secured": bool, "password": "..."}``
    """
    if not _cfg:
        return PlainTextResponse("Config store not available", status_code=503)

    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400)

    secured = body.get("secured", False)
    password = body.get("password", "bcMeterbcMeter")

    _cfg.set_bool("ap_secured", bool(secured))

    if isinstance(password, str) and len(password) >= 8:
        _cfg.set_string("ap_password", password)

    _cfg.save()

    return PlainTextResponse("OK")
