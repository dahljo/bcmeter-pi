"""Modbus TCP status endpoint.

Matches the ESP32 /api/modbus contract.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger("bcmeter.api.modbus")

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_modbus = None


def set_dependencies(modbus):
    global _modbus
    _modbus = modbus


# ---------------------------------------------------------------------------
# GET /api/modbus
# ---------------------------------------------------------------------------

@router.get("/modbus")
async def api_modbus():
    """Return Modbus TCP server state and the full register map."""
    if _modbus is None:
        return JSONResponse(content={"enabled": False, "listening": False})
    return JSONResponse(content=_modbus.info())
