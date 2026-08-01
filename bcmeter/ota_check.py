"""Background OTA update checking via GitHub Releases.

Port of ESP32 ota_check.h/cpp. Platform adaptation: Pi updates are
GitHub release archives (.tar.gz), extracted over /home/bcmeter,
followed by systemd service restart. Not binary flashing.
"""

import hashlib
import hmac
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from enum import IntEnum

from . import incident_log, email_handler
from .state import state
from .update_package import copy_update_items, safe_extract_archive, select_source_root

logger = logging.getLogger("bcmeter.ota_check")

# Match ESP32 ApplyState values
APPLY_IDLE = 0
APPLY_DOWNLOADING = 1
APPLY_EXTRACTING = 2   # was FLASHING on ESP32
APPLY_DONE = 3
APPLY_ERROR = 4

GITHUB_REPO = "dahljo/bcmeter-pi"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASE_ASSET_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"

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
_PRESERVE_RUNTIME_ITEMS = {
    "bcMeter_config.json",
    "bcMeter_wifi.json",
    "logs",
    "outbox",
    "downloads",
    ".upgrade_backup",
}

_available = False
_skipped = False
_version = ""
_notes = ""
_url = ""
_sha256 = ""
_asset_name = ""
_apply_state = APPLY_IDLE
_apply_progress = 0
_apply_error = ""
_force_check = False
_checking = False
_last_checked_at = 0.0
_last_error = ""
_success_old_version = ""
_success_new_version = ""
_success_at = 0.0
_lock = threading.Lock()
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _is_hex_sha256(value: str) -> bool:
    return bool(_SHA256_RE.match(str(value or "").strip()))


def _download_text(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "bcMeter/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(256 * 1024).decode("utf-8", errors="replace")


def _asset_url(asset: dict) -> str:
    url = str(asset.get("browser_download_url") or "").strip()
    return url if url.startswith(GITHUB_RELEASE_ASSET_PREFIX) else ""


def _find_asset_by_name(assets: list[dict], name: str) -> dict | None:
    wanted = os.path.basename(str(name or "").strip())
    if not wanted:
        return None
    for asset in assets:
        if os.path.basename(str(asset.get("name") or "")) == wanted:
            return asset
    return None


def _tarball_assets(assets: list[dict]) -> list[dict]:
    result = []
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith((".tar.gz", ".tgz")) and _asset_url(asset):
            result.append(asset)
    return result


def _manifest_candidates(manifest) -> list[dict]:
    candidates = []
    if isinstance(manifest, dict):
        candidates.append(manifest)
        for key in ("firmware", "ota", "update", "package"):
            if isinstance(manifest.get(key), dict):
                candidates.append(manifest[key])
        if isinstance(manifest.get("assets"), list):
            candidates.extend(x for x in manifest["assets"] if isinstance(x, dict))
    return candidates


def _first_value(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def _parse_sha256sums(text: str, asset_name: str, allow_single: bool = False) -> str:
    wanted = os.path.basename(asset_name)
    single_hash = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace("*", " ").split()
        if parts and _is_hex_sha256(parts[0]):
            if len(parts) == 1:
                single_hash = parts[0].lower()
                continue
            if os.path.basename(parts[-1]) == wanted:
                return parts[0].lower()
        if line.startswith("SHA256 (") and ") =" in line:
            name = line.split("SHA256 (", 1)[1].split(")", 1)[0]
            digest = line.rsplit("=", 1)[-1].strip()
            if os.path.basename(name) == wanted and _is_hex_sha256(digest):
                return digest.lower()
    return single_hash if allow_single and _is_hex_sha256(single_hash) else ""


def _sha_from_release_assets(assets: list[dict], tar_asset: dict) -> str:
    tar_name = str(tar_asset.get("name") or "")
    tar_lower = tar_name.lower()
    for asset in assets:
        name = str(asset.get("name") or "")
        lower = name.lower()
        if not _asset_url(asset):
            continue
        sums_asset = lower in ("sha256sums", "sha256sums.txt", "checksums.txt")
        sidecar_asset = lower in (f"{tar_lower}.sha256", f"{tar_lower}.sha256.txt")
        if sums_asset or sidecar_asset:
            try:
                digest = _parse_sha256sums(
                    _download_text(_asset_url(asset)),
                    tar_name,
                    allow_single=sidecar_asset,
                )
            except Exception as exc:
                logger.warning("OTA: could not read %s: %s", name, exc)
                continue
            if digest:
                return digest
    return ""


def _resolve_release_package(data: dict) -> tuple[str, str, str]:
    """Return (asset_name, download_url, sha256) for a pinned release asset."""
    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    tar_assets = _tarball_assets(assets)

    manifest_asset = _find_asset_by_name(assets, "version.json")
    if manifest_asset and _asset_url(manifest_asset):
        try:
            manifest = json.loads(_download_text(_asset_url(manifest_asset)))
        except Exception as exc:
            logger.warning("OTA: could not parse version.json: %s", exc)
        else:
            for candidate in _manifest_candidates(manifest):
                digest = _first_value(candidate, ("sha256", "sha_256", "hash"))
                if not _is_hex_sha256(digest):
                    continue
                name = _first_value(candidate, ("asset", "filename", "file", "name"))
                url = _first_value(
                    candidate,
                    ("browser_download_url", "download_url", "url"),
                )
                asset = _find_asset_by_name(assets, name) if name else None
                if asset and _asset_url(asset):
                    return str(asset.get("name") or name), _asset_url(asset), digest.lower()
                if url:
                    asset = next(
                        (item for item in tar_assets if _asset_url(item) == url),
                        None,
                    )
                    if asset:
                        return str(asset.get("name") or name), url, digest.lower()
                if tar_assets:
                    asset = tar_assets[0]
                    return str(asset.get("name") or ""), _asset_url(asset), digest.lower()

    if tar_assets:
        asset = tar_assets[0]
        return (
            str(asset.get("name") or ""),
            _asset_url(asset),
            _sha_from_release_assets(assets, asset),
        )
    return "", "", ""


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


def _wait_for_stable_internet(
    stop_event: threading.Event,
    timeout_s=None,
) -> bool:
    """Block until internet is stable enough for the initial OTA check."""
    stable = 0
    last_log = 0.0
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None
    while not stop_event.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            return False
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


def _set_apply_error(message: str):
    """Set the OTA apply error and expose it through /api/ota/status."""
    global _apply_state, _apply_error
    with _lock:
        _apply_error = message or "OTA apply failed"
        _apply_state = APPLY_ERROR
    incident_log.add("error", "OTA apply failed: %s", _apply_error)
    logger.error("OTA apply failed: %s", _apply_error)


def _set_available_flag():
    """Mirror pending OTA status into shared status state."""
    try:
        state.set("ota_available", _available and not _skipped)
    except Exception:
        pass


def _do_check(notify: bool = True) -> bool:
    """Fetch latest GitHub release and update state."""
    global _available, _skipped, _version, _notes, _url, _sha256, _asset_name
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
        asset_name, download_url, expected_sha = _resolve_release_package(data)

        if not tag or not download_url or not _is_hex_sha256(expected_sha):
            logger.warning("OTA: release lacks a pinned asset with SHA256 metadata")
            with _lock:
                _available = False
                _version = ""
                _notes = ""
                _url = ""
                _sha256 = ""
                _asset_name = ""
                _last_error = "missing verified release metadata"
            _set_available_flag()
            return False

        current = _get_current_version()
        if _is_newer(tag, current):
            with _lock:
                if tag != _version:
                    _skipped = False
                _available = True
                _version = tag.lstrip("v")
                _notes = notes_text[:511]
                _url = download_url
                _sha256 = expected_sha.lower()
                _asset_name = asset_name
            _set_available_flag()
            incident_log.add("info", "Update available: %s -> %s", current, tag)
            logger.info("Update available: %s -> %s", current, tag)
            if notify:
                email_handler.send_ota_available(tag.lstrip("v"), notes_text[:511])
        else:
            with _lock:
                _available = False
                _version = ""
                _notes = ""
                _url = ""
                _sha256 = ""
                _asset_name = ""
            _set_available_flag()
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

    if not _internet_available():
        stop = threading.Event()
        if not _wait_for_stable_internet(stop, timeout_s=150):
            _set_apply_error("Internet unavailable")
            return

    logger.info("Refreshing OTA metadata before apply")
    if not _do_check(notify=False):
        _set_apply_error("OTA metadata refresh failed")
        return

    with _lock:
        url = _url
        expected_hash = _sha256
        asset_name = _asset_name
        update_available = _available and not _skipped

    if not update_available or not url:
        _set_apply_error("No update available after refresh")
        return
    if not _is_hex_sha256(expected_hash):
        _set_apply_error("OTA SHA256 metadata missing")
        return

    logger.info("Downloading from %s", url)

    with _lock:
        _apply_state = APPLY_DOWNLOADING
        _apply_progress = 0

    tmp_path = None
    extract_dir = None
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

        computed = sha.hexdigest()
        if not hmac.compare_digest(computed.lower(), expected_hash.lower()):
            logger.error("SHA256 mismatch! expected=%s computed=%s", expected_hash, computed)
            _set_apply_error("SHA256 mismatch")
            return
        logger.info("SHA256 verified OK")

        # Extract
        with _lock:
            _apply_state = APPLY_EXTRACTING
            _apply_progress = 60

        extract_dir = tempfile.mkdtemp(prefix="bcmeter_ota_extract_")
        archive_name = asset_name or os.path.basename(urllib.parse.urlparse(url).path)
        safe_extract_archive(tmp_path, archive_name, extract_dir)
        src_dir = select_source_root(extract_dir)

        with _lock:
            _apply_progress = 80

        copy_update_items(src_dir, CODE_DIR, _PRESERVE_RUNTIME_ITEMS, logger=logger)

        incident_log.add("ok", "OTA update extracted to %s", CODE_DIR)
        logger.info("Update files extracted to %s", CODE_DIR)

        # Cleanup
        shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir = None

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
        logger.exception("OTA apply failed: %s", e)
        _set_apply_error(str(e))
    finally:
        if extract_dir:
            shutil.rmtree(extract_dir, ignore_errors=True)
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
        global _success_old_version, _success_new_version, _success_at
        with _lock:
            _success_old_version = prev
            _success_new_version = current
            _success_at = time.time()
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
        success_visible = bool(_success_at and (time.time() - _success_at) < 120)
        return {
            "available": _available and not _skipped,
            "skipped": _skipped,
            "version": _version,
            "notes": _notes,
            "url": _url,
            "checking": _checking,
            "last_checked": _last_checked_at,
            "last_error": _last_error,
            "apply_state": _apply_state,
            "apply_progress": _apply_progress,
            "apply_error": _apply_error,
            "ota_success": success_visible,
            "ota_old_version": _success_old_version if success_visible else "",
            "ota_new_version": _success_new_version if success_visible else "",
        }


def skip():
    """Skip the pending update for this boot cycle."""
    global _skipped
    with _lock:
        _skipped = True
    _set_available_flag()
    logger.info("Update skipped for this boot")


def start_apply() -> bool:
    """Begin download + extract. Returns False if nothing pending."""
    global _apply_state, _apply_progress, _apply_error
    with _lock:
        if not _available or _skipped:
            return False
        if _apply_state in (APPLY_DOWNLOADING, APPLY_EXTRACTING):
            return False
        _apply_state = APPLY_IDLE
        _apply_progress = 0
        _apply_error = ""

    t = threading.Thread(target=_apply, daemon=True, name="ota_apply")
    t.start()
    return True
