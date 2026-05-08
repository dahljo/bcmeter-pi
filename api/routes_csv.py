"""Data file endpoints.

Matches the ESP32 /api/csv and /api/files contracts.
"""

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

logger = logging.getLogger("bcmeter.api.csv")

router = APIRouter()

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_cfg = None
_storage = None


def set_dependencies(cfg, storage):
    global _cfg, _storage
    _cfg = cfg
    _storage = storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_dir() -> str:
    """Return the log directory path."""
    if _storage and hasattr(_storage, "log_dir"):
        return _storage.log_dir
    # Fallback
    base = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
    return os.path.join(base, "logs")


def _stream_file(filepath: str):
    """Generator that yields file content in chunks."""
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            yield chunk


# ---------------------------------------------------------------------------
# GET /api/csv
# ---------------------------------------------------------------------------

@router.get("/csv")
async def api_csv(file: str = Query("", description="Filename to download")):
    """Download a CSV file.

    If ``file`` is specified, serve that log file.
    If ``file`` is empty, serve the current active session.
    If no session is active, return a CSV with just the header line.
    """
    log_directory = _log_dir()

    if file:
        # Sanitise: strip leading slashes and prevent path traversal
        fname = os.path.basename(file)
        filepath = os.path.join(log_directory, fname)
        if not os.path.isfile(filepath):
            return PlainTextResponse("File not found", status_code=404)
    else:
        # Serve current session
        if _storage and _storage.session_filepath and os.path.isfile(_storage.session_filepath):
            filepath = _storage.session_filepath
            fname = _storage.session_filename or "session.csv"
        else:
            # No active session -- return an empty CSV with header
            header = "No active session\n"
            return PlainTextResponse(header, media_type="text/csv")

    # Build a descriptive download filename
    now = datetime.now()
    if now.year > 2024:
        download_name = now.strftime("bcmeter_%Y%m%d_%H%M%S.csv")
    else:
        download_name = f"bcmeter_{fname}" if fname else "bcmeter_session.csv"

    headers = {
        "Content-Disposition": f'attachment; filename="{download_name}"',
        "Cache-Control": "no-store",
    }

    return StreamingResponse(
        _stream_file(filepath),
        media_type="text/csv",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# GET /api/files
# ---------------------------------------------------------------------------

@router.get("/files")
async def api_files():
    """List log files matching ESP32 /api/files contract.

    Returns ``[{name, size, date}, ...]`` sorted newest first.
    """
    if not _storage:
        return JSONResponse(content=[])

    logs = _storage.list_logs()

    result = []
    for entry in logs:
        # Format date from mtime
        try:
            mtime = entry.get("mtime", 0)
            date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_str = ""

        result.append({
            "name": entry.get("name", ""),
            "size": entry.get("size", 0),
            "lines": entry.get("lines", 0),
            "date": date_str,
        })

    return JSONResponse(content=result)
