"""OTA update endpoints matching ESP32 /api/ota/* contract."""

import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from bcmeter import ota_check

logger = logging.getLogger("bcmeter.api.ota")

router = APIRouter()


@router.get("/ota/status")
async def ota_status():
    """Return OTA update status."""
    return JSONResponse(content=ota_check.get_info())


@router.post("/ota/check")
async def ota_check_now():
    """Force an immediate update check."""
    ota_check.request_check()
    return JSONResponse(content={"ok": True})


@router.post("/ota/skip")
async def ota_skip():
    """Skip the pending update for this boot cycle."""
    ota_check.skip()
    return JSONResponse(content={"ok": True})


@router.post("/ota/apply")
async def ota_apply():
    """Begin downloading and applying the pending update."""
    started = ota_check.start_apply()
    if not started:
        return JSONResponse(
            content={"ok": False, "error": "No update pending or already applying"},
            status_code=409,
        )
    return JSONResponse(content={"ok": True})
