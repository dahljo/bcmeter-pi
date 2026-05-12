"""Software update endpoint.

For the Raspberry Pi platform this accepts an uploaded archive, extracts
it over the bcmeter code directory, and restarts the systemd service.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import PlainTextResponse

logger = logging.getLogger("bcmeter.api.update")

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

_CODE_DIR = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
_SERVICE_NAME = "bcMeter.service"

_update_lock = threading.Lock()
_update_running = False


def _normalize_permissions(path: str):
    """Make deployed code readable/importable by the bcmeter login user."""
    try:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                os.chmod(root, 0o755)
                for dirname in dirs:
                    os.chmod(os.path.join(root, dirname), 0o755)
                for filename in files:
                    file_path = os.path.join(root, filename)
                    mode = 0o755 if filename.endswith(".sh") else 0o644
                    os.chmod(file_path, mode)
        elif os.path.exists(path):
            mode = 0o755 if path.endswith(".sh") else 0o644
            os.chmod(path, mode)
    except Exception as exc:
        logger.warning("Failed to normalize permissions for %s: %s", path, exc)


def _apply_update(archive_path: str, original_filename: str):
    """Apply the update archive and restart the service.

    Supports ``.tar.gz`` and ``.zip`` formats.
    """
    global _update_running

    try:
        extract_dir = tempfile.mkdtemp(prefix="bcmeter_update_")

        if original_filename.endswith(".tar.gz") or original_filename.endswith(".tgz"):
            subprocess.run(
                ["tar", "xzf", archive_path, "-C", extract_dir],
                check=True, capture_output=True, timeout=120,
            )
        elif original_filename.endswith(".zip"):
            subprocess.run(
                ["unzip", "-o", archive_path, "-d", extract_dir],
                check=True, capture_output=True, timeout=120,
            )
        else:
            logger.error("Unsupported archive format: %s", original_filename)
            return

        # Determine the root of the extracted content.  If the archive
        # contains a single top-level directory, use that as the source.
        entries = os.listdir(extract_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            src_dir = os.path.join(extract_dir, entries[0])
        else:
            src_dir = extract_dir

        # Copy files into the code directory
        for item in os.listdir(src_dir):
            src = os.path.join(src_dir, item)
            dst = os.path.join(_CODE_DIR, item)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            _normalize_permissions(dst)

        logger.info("Update files extracted to %s", _CODE_DIR)

        # Cleanup temp files
        shutil.rmtree(extract_dir, ignore_errors=True)
        try:
            os.remove(archive_path)
        except Exception:
            pass

        # Restart the service
        logger.info("Restarting %s...", _SERVICE_NAME)
        subprocess.run(
            ["sudo", "systemctl", "restart", _SERVICE_NAME],
            capture_output=True, timeout=30,
        )

    except Exception as exc:
        logger.exception("Update failed: %s", exc)
    finally:
        with _update_lock:
            _update_running = False


# ---------------------------------------------------------------------------
# POST /api/update
# ---------------------------------------------------------------------------

@router.post("/update")
async def api_update(
    file: Optional[UploadFile] = File(None),
    update: Optional[UploadFile] = File(None),
):
    """Accept an uploaded archive, extract it, and restart the service.

    The uploaded file should be a ``.tar.gz`` or ``.zip`` archive
    containing the new bcmeter code.
    """
    global _update_running
    upload = file or update
    if upload is None:
        return PlainTextResponse("Missing update file", status_code=400)

    with _update_lock:
        if _update_running:
            return PlainTextResponse("Update already in progress", status_code=409)
        _update_running = True

    # Stop active measurement session
    if _storage and _storage.session_active:
        logger.info("Stopping active session for update")
        _storage.end_session()

    # Save uploaded file to a temporary location
    try:
        suffix = ""
        filename = upload.filename or "update.tar.gz"
        if filename.endswith(".tar.gz") or filename.endswith(".tgz"):
            suffix = ".tar.gz"
        elif filename.endswith(".zip"):
            suffix = ".zip"
        else:
            suffix = ".tar.gz"

        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="bcmeter_upload_")
        with os.fdopen(fd, "wb") as tmp:
            while True:
                chunk = await upload.read(65536)
                if not chunk:
                    break
                tmp.write(chunk)

        logger.info(
            "Update uploaded: %s (%d bytes)",
            filename,
            os.path.getsize(tmp_path),
        )
    except Exception as exc:
        with _update_lock:
            _update_running = False
        logger.error("Failed to save uploaded file: %s", exc)
        return PlainTextResponse(f"Upload failed: {exc}", status_code=500)

    # Apply in background thread (service will restart)
    t = threading.Thread(
        target=_apply_update,
        args=(tmp_path, filename),
        daemon=True,
        name="update_apply",
    )
    t.start()

    return PlainTextResponse("OK")
