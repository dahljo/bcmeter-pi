"""Configuration endpoints.

Matches the ESP32 /api/config, /api/device/rename, and /api/ap-security contracts.
"""

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from bcmeter import avahi_alias
from bcmeter.identity import hostname_from_device_name, sync_system_hostname
from .local_access import require_local_write_access

logger = logging.getLogger("bcmeter.api.config")

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_cfg = None

_SECRET_KEYS = {
    "ap_password",
    "email_api_key",
    "email_service_password",
    "iot_api_key",
}
_CANONICAL_SECRET_KEYS = {"email_api_key"}
_SECRET_PLACEHOLDERS = {"", "email_service_password", "your_api_key", "configured", "iot_api_key"}
_MAX_CONFIG_BODY_BYTES = 64 * 1024
_MAX_EMAIL_API_KEY_BYTES = 512
_ANONYMOUS_PRIVATE_KEYS = {
    "bcmeter_team_email",
    "location_lat",
    "location_lon",
    "mail_logs_to",
    "wifi_password",
    "wifi_pwd",
    "wifi_ssid",
}


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


def _redact_anonymous_config(config: dict) -> dict:
    """Remove personal coordinates, addresses and network details."""
    for key in _ANONYMOUS_PRIVATE_KEYS:
        entry = config.get(key)
        if not isinstance(entry, dict) or "value" not in entry:
            continue
        value = entry.get("value")
        if key in {"location_lat", "location_lon"}:
            entry["value"] = 0.0
        else:
            entry["value"] = "configured" if str(value or "").strip() else ""
        entry["redacted"] = True
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
    """Return the public, consistently redacted configuration view."""
    if not _cfg:
        return JSONResponse(content={}, status_code=503)

    data = _mask_config_secrets(json.loads(_cfg.to_json()))
    data = _redact_anonymous_config(data)
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# POST /api/config
# ---------------------------------------------------------------------------

@router.post(
    "/config",
    dependencies=[Depends(require_local_write_access("config"))],
)
async def api_config_post(request: Request):
    """Apply configuration from JSON body.

    Accepts either ``{key: value}`` or ``{key: {value: ...}}`` format,
    matching the ESP32 CfgStore::applyJSON behaviour.
    """
    if not _cfg:
        return PlainTextResponse("Config store not available", status_code=503)

    try:
        body = await request.body()
        if len(body) > _MAX_CONFIG_BODY_BYTES:
            return PlainTextResponse("Config body too large", status_code=413)
        body_str = body.decode("utf-8")
    except Exception as exc:
        return PlainTextResponse(f"Bad request: {exc}", status_code=400)

    if not body_str or not body_str.strip():
        return PlainTextResponse("No body", status_code=400)

    try:
        incoming = json.loads(body_str)
    except json.JSONDecodeError:
        return PlainTextResponse("Invalid JSON", status_code=400)
    if not isinstance(incoming, dict):
        return PlainTextResponse("Config body must be an object", status_code=400)
    if "email_api_key" in incoming:
        api_key = incoming["email_api_key"]
        if isinstance(api_key, dict):
            api_key = api_key.get("value", "")
        if not isinstance(api_key, str):
            return PlainTextResponse("Invalid API key", status_code=400)
        if len(api_key.encode("utf-8")) > _MAX_EMAIL_API_KEY_BYTES:
            return PlainTextResponse("API key too large", status_code=413)

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

@router.post(
    "/device/rename",
    dependencies=[Depends(require_local_write_access("device-rename"))],
)
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

    _cfg.set_device_name(name, custom=True)
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
        "password": "",
    })


# ---------------------------------------------------------------------------
# POST /api/ap-security
# ---------------------------------------------------------------------------

@router.post(
    "/email/validate",
    dependencies=[Depends(require_local_write_access("email-validate"))],
)
async def api_email_validate(request: Request):
    """Validate an email service API key against the Lambda endpoint."""
    try:
        raw = await request.body()
        if len(raw) > 1024:
            return JSONResponse(
                content={"valid": False, "error": "Request too large"},
                status_code=413,
            )
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse(content={"valid": False, "error": "Invalid JSON"}, status_code=400)

    api_key = body.get("api_key", "")
    if not isinstance(api_key, str) or not api_key:
        return JSONResponse(content={"valid": False, "error": "No API key provided"})
    if len(api_key.encode("utf-8")) > _MAX_EMAIL_API_KEY_BYTES:
        return JSONResponse(
            content={"valid": False, "error": "API key too large"},
            status_code=413,
        )

    from bcmeter.email_handler import validate_api_key
    valid, err = validate_api_key(api_key)
    safe_error = str(err or "").replace(api_key, "[redacted]")[:256]
    return JSONResponse(content={"valid": valid, "error": safe_error})


@router.post(
    "/ap-security",
    dependencies=[Depends(require_local_write_access("ap-security"))],
)
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
