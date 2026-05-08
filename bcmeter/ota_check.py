"""Background OTA update checking via GitHub Releases.

Port of ESP32 ota_check.h/cpp. Platform adaptation: Pi updates are
GitHub release archives (.tar.gz), extracted over /home/bcmeter,
followed by systemd service restart. Not binary flashing.
"""

import hashlib
import json
import logging
import os
import random
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import urllib.error
from enum import IntEnum

from . import incident_log, email_handler
from .state import state

logger = logging.getLogger("bcmeter.ota_check")

# Match ESP32 ApplyState values
APPLY_IDLE = 0
APPLY_DOWNLOADING = 1
APPLY_EXTRACTING = 2   # was FLASHING on ESP32
APPLY_DONE = 3
APPLY_ERROR = 4

GITHUB_REPO = "dahljo/bcmeter-pi"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

BASE_INTERVAL_S = 6 * 3600       # 6 hours
JITTER_MAX_S = 15 * 60           # 15 minutes
INTERNET_CHECK_HOST = "api.github.com"
INTERNET_CHECK_PORT = 443
INTERNET_CHECK_TIMEOUT_S = 3
INTERNET_STABLE_SAMPLES = 2
INTERNET_STABLE_SAMPLE_S = 5
INTERNET_SETTLE_S = 5
CODE_DIR = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
SERVICE_NAME = "bcMeter.service"
_OTA_VERSION_FILE = os.path.join(CODE_DIR, ".ota_last_version")

_available = False
_skipped = False
_version = ""
_notes = ""
_url = ""
_sha256 = ""
_apply_state = APPLY_IDLE
_apply_progress = 0
_force_check = False
_checking = False
_last_checked_at = 0.0
_last_error = ""
_lock = threading.Lock()


def _get_current_version() -> str:
    """Return the package version string."""
    try:
        from bcmeter import __version__
        return __version__
    except Exception:
        return "0.0.0"


def _is_newer(candidate: str, current: str) -> bool:
    """Semver comparison: True if candidate > current."""
    def parse(v):
        return tuple(int(p) for p in v.lstrip("v").split(".")[:3])
    try:
        return parse(candidate) > parse(current)
    except (ValueError, IndexError):
        return False


def _internet_available() -> bool:
    """Return True when network is ready for the GitHub OTA metadata request."""
    try:
        if bool(state.get("internet")):
            return True
    except Exception:
        pass

    try:
        with socket.create_connection(
            (INTERNET_CHECK_HOST, INTERNET_CHECK_PORT),
            timeout=INTERNET_CHECK_TIMEOUT_S,
        ):
            return True
    except OSError:
        return False


def _wait_for_stable_internet(stop_event: threading.Event) -> bool:
    """Block until internet is stable enough for the initial OTA check."""
    stable = 0
    last_log = 0.0
    while not stop_event.is_set():
        if _internet_available():
            stable += 1
            if stable >= INTERNET_STABLE_SAMPLES:
                logger.info("Internet stable; settling before OTA check")
                return not stop_event.wait(INTERNET_SETTLE_S)
        else:
            stable = 0
            now = time.monotonic()
            if now - last_log > 60:
                logger.info("Waiting for stable internet before OTA check")
                last_log = now
        stop_event.wait(INTERNET_STABLE_SAMPLE_S)
    return False


def _do_check() -> bool:
    """Fetch latest GitHub release and update state."""
    global _available, _skipped, _version, _notes, _url, _sha256
    global _checking, _last_checked_at, _last_error

    logger.info("Checking for updates...")
    with _lock:
        _checking = True
        _last_error = ""
    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "bcMeter/2.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        tag = data.get("tag_name", "")
        notes_text = data.get("body", "")
        tarball_url = data.get("tarball_url", "")

        if not tag or not tarball_url:
            logger.warning("OTA: missing tag_name or tarball_url in release")
            with _lock:
                _last_error = "missing release metadata"
            return False

        current = _get_current_version()
        if _is_newer(tag, current):
            with _lock:
                if tag != _version:
                    _skipped = False
                _available = True
                _version = tag.lstrip("v")
                _notes = notes_text[:511]
                _url = tarball_url
                _sha256 = ""  # GitHub releases don't include SHA256
            incident_log.add("info", "Update available: %s -> %s", current, tag)
            logger.info("Update available: %s -> %s", current, tag)
            email_handler.send_ota_available(tag.lstrip("v"), notes_text[:511])
        else:
            with _lock:
                _available = False
                _version = ""
                _notes = ""
                _url = ""
                _sha256 = ""
            logger.info("Up to date (%s)", current)
        return True

    except Exception as e:
        logger.error("OTA check failed: %s", e)
        with _lock:
            _last_error = str(e)
        return False
    finally:
        with _lock:
            _checking = False
            _last_checked_at = time.time()


def _apply():
    """Download release tarball, extract to CODE_DIR, restart service."""
    global _apply_state, _apply_progress

    with _lock:
        url = _url
        expected_hash = _sha256

    logger.info("Downloading from %s", url)

    with _lock:
        _apply_state = APPLY_DOWNLOADING
        _apply_progress = 0

    tmp_path = None
    try:
        # Download the tarball
        req = urllib.request.Request(
            url, headers={"User-Agent": "bcMeter/2.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", prefix="bcmeter_ota_")
            sha = hashlib.sha256()
            total = int(resp.headers.get("Content-Length", 0))
            written = 0
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    sha.update(chunk)
                    written += len(chunk)
                    with _lock:
                        _apply_progress = int(written * 50 / total) if total > 0 else 25

        logger.info("Downloaded %d bytes", written)

        # Verify SHA256 if available
        if expected_hash:
            computed = sha.hexdigest()
            if computed.lower() != expected_hash.lower():
                logger.error("SHA256 mismatch! expected=%s computed=%s",
                             expected_hash, computed)
                with _lock:
                    _apply_state = APPLY_ERROR
                return
            logger.info("SHA256 verified OK")

        # Extract
        with _lock:
            _apply_state = APPLY_EXTRACTING
            _apply_progress = 60

        extract_dir = tempfile.mkdtemp(prefix="bcmeter_ota_extract_")
        with tarfile.open(tmp_path, "r:gz") as tar:
            tar.extractall(path=extract_dir)

        # Determine source root (GitHub tarballs have a single top-level dir)
        entries = os.listdir(extract_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            src_dir = os.path.join(extract_dir, entries[0])
        else:
            src_dir = extract_dir

        with _lock:
            _apply_progress = 80

        # Copy files into CODE_DIR
        for item in os.listdir(src_dir):
            src = os.path.join(src_dir, item)
            dst = os.path.join(CODE_DIR, item)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        incident_log.add("ok", "OTA update extracted to %s", CODE_DIR)
        logger.info("Update files extracted to %s", CODE_DIR)

        # Cleanup
        shutil.rmtree(extract_dir, ignore_errors=True)

        with _lock:
            _apply_progress = 100
            _apply_state = APPLY_DONE

        # Restart the service
        logger.info("Restarting %s...", SERVICE_NAME)
        subprocess.run(
            ["sudo", "systemctl", "restart", SERVICE_NAME],
            capture_output=True, timeout=30,
        )

    except Exception as e:
        incident_log.add("error", "OTA apply failed: %s", e)
        logger.exception("OTA apply failed: %s", e)
        with _lock:
            _apply_state = APPLY_ERROR
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _check_loop(stop_event: threading.Event):
    """Background thread: initial check after stable internet, then periodic."""
    global _force_check

    if not _wait_for_stable_internet(stop_event):
        return
    _do_check()

    interval = BASE_INTERVAL_S + random.randint(0, JITTER_MAX_S)
    last_check = time.monotonic()

    while not stop_event.is_set():
        stop_event.wait(10)

        with _lock:
            if _apply_state != APPLY_IDLE:
                continue
            force = _force_check
            if force:
                _force_check = False

        due = time.monotonic() - last_check >= interval
        if not force and not due:
            continue

        if not _internet_available() and not _wait_for_stable_internet(stop_event):
            return

        if _do_check():
            last_check = time.monotonic()
            interval = BASE_INTERVAL_S + random.randint(0, JITTER_MAX_S)


def _check_ota_success():
    """On startup, check if version changed since last boot → send success email.

    Mirrors ESP32 checkOtaStatus() in webserver.cpp which uses NVS Preferences.
    Pi uses a simple file to persist the last known version.
    """
    current = _get_current_version()
    prev = ""
    try:
        if os.path.exists(_OTA_VERSION_FILE):
            with open(_OTA_VERSION_FILE, "r") as f:
                prev = f.read().strip()
    except Exception:
        pass

    if prev and prev != current:
        logger.info("OTA success detected: %s -> %s", prev, current)
        incident_log.add("ok", "Firmware updated: %s -> %s", prev, current)
        email_handler.send_ota_success(prev, current)

    # Always persist current version
    try:
        with open(_OTA_VERSION_FILE, "w") as f:
            f.write(current)
    except Exception as e:
        logger.warning("Failed to write OTA version file: %s", e)


def init(stop_event: threading.Event):
    """Start background check thread."""
    _check_ota_success()
    t = threading.Thread(
        target=_check_loop, args=(stop_event,),
        daemon=True, name="ota_check",
    )
    t.start()


def request_check():
    """Force an immediate check on next loop iteration."""
    global _force_check
    _force_check = True


def get_info() -> dict:
    """Return OTA status dict matching ESP32 /api/ota/status contract."""
    with _lock:
        return {
            "available": _available and not _skipped,
            "version": _version,
            "notes": _notes,
            "checking": _checking,
            "last_checked": _last_checked_at,
            "last_error": _last_error,
            "apply_state": _apply_state,
            "apply_progress": _apply_progress,
        }


def skip():
    """Skip the pending update for this boot cycle."""
    global _skipped
    with _lock:
        _skipped = True
    logger.info("Update skipped for this boot")


def start_apply() -> bool:
    """Begin download + extract. Returns False if nothing pending."""
    with _lock:
        if not _available or _apply_state != APPLY_IDLE:
            return False

    t = threading.Thread(target=_apply, daemon=True, name="ota_apply")
    t.start()
    return True
