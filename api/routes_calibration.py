"""Calibration status endpoint.

Matches the ESP32 /api/calibration contract.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger("bcmeter.api.calibration")

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_engine = None
_state = None


def set_dependencies(engine, state_mgr):
    global _engine, _state
    _engine = engine
    _state = state_mgr


# ---------------------------------------------------------------------------
# GET /api/calibration
# ---------------------------------------------------------------------------

@router.get("/calibration")
async def api_calibration():
    """Return calibration status matching ESP32 /api/calibration contract.

    Response::

        {
            "running": bool,
            "done": bool,
            "ok": bool,
            "elapsed_ms": int,
            "log": str
        }
    """
    # Calibration state is managed by routes_control (shared globals)
    from .routes_control import get_calibration_state

    data = get_calibration_state()
    return JSONResponse(content=data)
