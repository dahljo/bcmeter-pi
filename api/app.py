"""FastAPI application -- serves REST API and static frontend."""

import logging
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse

from .routes_status import router as status_router
from .routes_control import router as control_router
from .routes_config import router as config_router
from .routes_wifi import router as wifi_router
from .routes_csv import router as csv_router
from .routes_calibration import router as calibration_router
from .routes_update import router as update_router
from .routes_ota import router as ota_router
from .routes_lab import router as lab_router
from .routes_qc import router as qc_router

logger = logging.getLogger("bcmeter.api")

BASE_DIR = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
INTERFACE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "interface")

app = FastAPI(title="bcMeter API", version="2.0.0")

# CORS -- allows the bcmeter.org discovery page (and any browser) to
# query /api/status from a remote origin so devices can be found on
# the local network without mDNS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Include all route modules
app.include_router(status_router, prefix="/api")
app.include_router(control_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(wifi_router, prefix="/api")
app.include_router(csv_router, prefix="/api")
app.include_router(calibration_router, prefix="/api")
app.include_router(update_router, prefix="/api")
app.include_router(ota_router, prefix="/api")
app.include_router(lab_router, prefix="/api")
app.include_router(qc_router, prefix="/api")


# ---------------------------------------------------------------------------
# Serve the SPA frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(INTERFACE_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path) as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>bcMeter</h1><p>Frontend not installed.</p>")


@app.get("/discover", response_class=HTMLResponse)
async def discover():
    """Standalone discovery page — also hostable on bcmeter.org."""
    discover_path = os.path.join(INTERFACE_DIR, "discover.html")
    if os.path.exists(discover_path):
        with open(discover_path) as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Not found</h1>", status_code=404)


# ---------------------------------------------------------------------------
# Captive portal redirects (for AP mode)
# ---------------------------------------------------------------------------

@app.get("/generate_204")
async def captive_generate_204():
    return RedirectResponse("/")


@app.get("/hotspot-detect.html")
async def captive_hotspot_detect():
    return RedirectResponse("/")


@app.get("/canonical.html")
async def captive_canonical():
    return RedirectResponse("/")


@app.get("/ncsi.txt")
async def captive_ncsi():
    return RedirectResponse("/")


@app.get("/success.txt")
async def success():
    return "success"


# Legacy redirect
@app.get("/current_log.csv")
async def legacy_csv_redirect():
    return RedirectResponse("/api/csv")


# ---------------------------------------------------------------------------
# Mount static files for manual/assets
# ---------------------------------------------------------------------------

_manual_dir = os.path.join(INTERFACE_DIR, "manual")
if os.path.isdir(_manual_dir):
    app.mount("/manual", StaticFiles(directory=_manual_dir), name="manual")


# ---------------------------------------------------------------------------
# Dependency injection wiring
# ---------------------------------------------------------------------------

def set_dependencies(cfg, state_mgr, engine, storage, network_manager,
                     gps=None, status_led=None,
                     pi=None, adc=None, optics=None, pump=None):
    """Wire shared objects into all route modules.

    Called once during application startup after all subsystems are
    initialised.  This avoids circular imports and makes testing
    straightforward (just call with mocks).
    """
    from . import routes_status, routes_control, routes_config
    from . import routes_wifi, routes_csv, routes_calibration, routes_update
    from . import routes_lab

    routes_status.set_dependencies(cfg=cfg, state_mgr=state_mgr, storage=storage, gps=gps, pump=pump)
    routes_control.set_dependencies(cfg=cfg, state_mgr=state_mgr, engine=engine, storage=storage, status_led=status_led)
    routes_config.set_dependencies(cfg=cfg)
    routes_wifi.set_dependencies(cfg=cfg, network_manager=network_manager)
    routes_csv.set_dependencies(cfg=cfg, storage=storage)
    routes_calibration.set_dependencies(engine=engine, state_mgr=state_mgr)
    routes_update.set_dependencies(cfg=cfg, storage=storage)
    routes_lab.set_dependencies(cfg=cfg, state_mgr=state_mgr, engine=engine,
                                pi=pi, adc=adc, optics=optics, pump=pump)

    logger.info("API dependencies wired")
