"""Data file endpoints.

Matches the ESP32 /api/csv and /api/files contracts.
"""

import logging
import os
import re
import socket
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .local_access import require_local_write_access

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


_DOWNLOAD_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DEVICE_SUFFIX_RE = re.compile(r"-[A-Za-z0-9]{4}$")
_GENERIC_LOG_PREFIX_RE = re.compile(r"^bcmeter_+", re.IGNORECASE)
_MAX_DOWNLOAD_COMPONENT = 64


def _safe_download_component(value: str, fallback: str) -> str:
    """Return one attachment-name component without losing a ``-XXXX`` suffix."""
    raw = str(value or "").strip()
    safe = _DOWNLOAD_COMPONENT_RE.sub("-", raw).strip("._-")
    if not safe:
        safe = fallback

    # Keep a defensive limit for legacy/imported configurations, truncating
    # the descriptive prefix rather than the hardware-identifying suffix.
    if len(safe) > _MAX_DOWNLOAD_COMPONENT:
        suffix_match = _DEVICE_SUFFIX_RE.search(safe)
        suffix = suffix_match.group(0) if suffix_match else ""
        prefix_limit = _MAX_DOWNLOAD_COMPONENT - len(suffix)
        prefix = safe[:prefix_limit].rstrip("._-")
        safe = (prefix or fallback[:prefix_limit]) + suffix
    return safe


def _configured_device_download_name() -> str:
    name = _cfg.get_string("device_name", "") if _cfg else ""
    if not str(name or "").strip():
        name = socket.gethostname().split(".", 1)[0]
    return _safe_download_component(name, "bcMeter")


def _csv_download_name(
    source_filename: str = "", now: datetime = None, device_name: str = ""
) -> str:
    """Build a safe CSV attachment name headed by the full configured name."""
    device_name = device_name or _configured_device_download_name()
    if source_filename:
        leaf = os.path.basename(str(source_filename)).removesuffix(".csv")
        leaf = _safe_download_component(leaf, "session")
        if leaf.lower().startswith(device_name.lower() + "_"):
            return f"{leaf}.csv"
        leaf = _GENERIC_LOG_PREFIX_RE.sub("", leaf)
        leaf = _safe_download_component(leaf, "session")
        return f"{device_name}_{leaf}.csv"

    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{device_name}_{timestamp}.csv"


def _csv_attachment_headers(source_filename: str = "") -> dict:
    device_name = _configured_device_download_name()
    return {
        "Content-Disposition": (
            f'attachment; filename="{_csv_download_name(source_filename, device_name=device_name)}"'
        ),
        "X-bcMeter-Download-Device": device_name,
        "Access-Control-Expose-Headers": (
            "Content-Disposition, X-bcMeter-Download-Device"
        ),
        "Cache-Control": "no-store",
    }


# ---------------------------------------------------------------------------
# GET /api/csv
# ---------------------------------------------------------------------------

@router.get("/csv")
async def api_csv(file: str = Query("", description="Filename to download")):
    """Download a CSV file.

    If ``file`` is specified, serve that log file.
    If ``file`` is empty, serve the current active session.
    If no session is active, serve the newest existing log so the graph can
    populate immediately. If no log exists, return a header-only CSV.
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
        if (
            _storage
            and _storage.session_active
            and _storage.session_filepath
            and os.path.isfile(_storage.session_filepath)
        ):
            filepath = _storage.session_filepath
            fname = _storage.session_filename or "session.csv"
        else:
            logs = _storage.list_logs() if _storage else []
            if logs:
                fname = logs[0]["name"]
                filepath = os.path.join(log_directory, fname)
            else:
                header = (
                    _storage.csv_header_line
                    if _storage and hasattr(_storage, "csv_header_line")
                    else "bcmDate;bcmTime"
                )
                return PlainTextResponse(
                    header + "\n",
                    media_type="text/csv",
                    headers=_csv_attachment_headers(),
                )

    # The direct API response is authoritative for browser and UI downloads.
    # Preserve the stored timestamp for an explicitly selected archive; current
    # CSV downloads use the current wall-clock timestamp.
    headers = _csv_attachment_headers(fname if file else "")

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
        return JSONResponse(content=[], headers={"Cache-Control": "no-store"})

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
            "active": bool(entry.get("active", False)),
        })

    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


@router.delete(
    "/files",
    dependencies=[Depends(require_local_write_access("files-delete"))],
)
async def api_files_delete(name: str = Query("", description="Log filename")):
    """Delete one inactive CSV log from the operator LAN."""
    fname = str(name or "").strip().lstrip("/")
    if (
        not fname
        or not fname.endswith(".csv")
        or os.path.basename(fname) != fname
        or ".." in fname
    ):
        return PlainTextResponse("Invalid log filename", status_code=400)
    if not _storage:
        return PlainTextResponse("Storage not available", status_code=503)
    if not _storage.delete_log(fname):
        return PlainTextResponse("Log file not found or active", status_code=404)
    return JSONResponse(content={"deleted": True})
