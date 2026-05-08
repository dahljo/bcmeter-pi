"""Publish mDNS host/service records via avahi.

The device hostname is e.g. 'bcmeter-a1b2' (MAC-derived), and avahi
automatically publishes 'bcmeter-a1b2.local'.  This module:
  1. Writes a wildcard avahi service file to /etc/avahi/services/ so
     the device is discoverable via `dns-sd -B _http._tcp local.`
     (matching ESP32 ESPmDNS behavior).  The file uses %h, so cloned
     images never contain a baked-in hostname.
  2. Publishes 'bcmeter.local' as a best-effort convenience CNAME alias
     via D-Bus when python3-dbus is available.

The MAC-derived hostname is the stable identity.  The bare 'bcmeter.local'
alias is only a convenience for a single visible unit; on collision it is not
renamed to bcmeter-2.local because that would be ambiguous on multi-device
networks.

Usage:
    from bcmeter.avahi_alias import start
    start(stop_event)          # blocking, run in a thread
    # or
    start_background(stop_event)  # launches its own daemon thread
"""

import logging
import os
import socket
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger("bcmeter.avahi_alias")

ALIAS = "bcmeter"
HTTP_PORT = 80
AVAHI_SERVICE_DIR = "/etc/avahi/services"
AVAHI_SERVICE_FILE = os.path.join(AVAHI_SERVICE_DIR, "bcmeter-http.service")
AVAHI_PUBLISH_UNIQUE = 0x01
AVAHI_PUBLISH_USE_MULTICAST = 0x100
AVAHI_CNAME_FLAGS = AVAHI_PUBLISH_UNIQUE | AVAHI_PUBLISH_USE_MULTICAST

_refresh_event = threading.Event()
_started = False


def _reload_avahi():
    """Ask avahi to re-read static services and re-announce records."""
    for cmd in (["systemctl", "reload", "avahi-daemon"],
                ["sudo", "systemctl", "reload", "avahi-daemon"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=5)
            if proc.returncode == 0:
                return True
        except Exception:
            pass
    return False


def restart_daemon(reason: str = ""):
    """Restart avahi after a system hostname change.

    avahi-daemon caches the host name it announces.  A reload is enough for
    service XML changes, but after first-boot MAC-derived renaming we need a
    restart so `bcmeter-XXXX.local` is published immediately instead of only
    after the next reboot.
    """
    if reason:
        logger.debug("Restarting avahi-daemon (%s)", reason)
    for cmd in (["systemctl", "restart", "avahi-daemon"],
                ["sudo", "systemctl", "restart", "avahi-daemon"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=10)
            if proc.returncode == 0:
                return True
        except Exception:
            pass
    logger.debug("avahi-daemon restart failed; trying reload")
    return _reload_avahi()


def _ensure_http_service(force_reload=False):
    """Write a static avahi service file for _http._tcp discovery.
    This is the most reliable way — avahi reads XML files from
    /etc/avahi/services/ without needing python3-dbus."""
    service_xml = f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service>
    <type>_http._tcp</type>
    <port>{HTTP_PORT}</port>
  </service>
</service-group>
"""

    try:
        # Check if file already exists with correct content
        if os.path.isfile(AVAHI_SERVICE_FILE):
            with open(AVAHI_SERVICE_FILE) as f:
                if f.read().strip() == service_xml.strip():
                    if force_reload:
                        _reload_avahi()
                    logger.debug("Avahi _http._tcp service file already up to date")
                    return True

        # Write service file (needs root)
        with open(AVAHI_SERVICE_FILE, "w") as f:
            f.write(service_xml)

        # Reload avahi to pick up the new service file
        _reload_avahi()

        logger.info("Published _http._tcp service via %s", AVAHI_SERVICE_FILE)
        return True

    except PermissionError:
        # Try with sudo
        try:
            proc = subprocess.run(
                ["sudo", "tee", AVAHI_SERVICE_FILE],
                input=service_xml.encode(),
                capture_output=True, timeout=5,
            )
            if proc.returncode == 0:
                _reload_avahi()
                logger.info("Published _http._tcp service via %s (sudo)", AVAHI_SERVICE_FILE)
                return True
        except Exception:
            pass
        logger.warning("Cannot write avahi service file (no root access)")
        return False
    except Exception as e:
        logger.warning("Failed to write avahi service file: %s", e)
        return False


def _encode_dns_name(name: str) -> list[int]:
    """Encode a domain name in DNS wire format (RFC 1035)."""
    result = bytearray()
    for label in name.rstrip(".").split("."):
        encoded = label.encode("utf-8")
        result.append(len(encoded))
        result.extend(encoded)
    result.append(0)  # root label
    return list(result)


def _wait_for_stop_or_refresh(stop_event: threading.Event,
                              refresh_event: Optional[threading.Event] = None):
    """Block until shutdown or an explicit mDNS refresh request."""
    if refresh_event is None:
        stop_event.wait()
        return
    while not stop_event.is_set():
        if refresh_event.wait(1):
            refresh_event.clear()
            return


def _publish(stop_event: threading.Event,
             refresh_event: Optional[threading.Event] = None):
    """Publish mDNS records: static service file + optional D-Bus CNAME."""

    # ── 1. Static service file for _http._tcp ─────────────────
    _ensure_http_service(force_reload=refresh_event is not None)

    # ── 2. CNAME alias via D-Bus (optional) ───────────────────
    try:
        import dbus
    except ImportError:
        logger.info("python3-dbus not installed, skipping bcmeter.local CNAME alias "
                    "(_http._tcp service is published via static file)")
        _wait_for_stop_or_refresh(stop_event, refresh_event)
        return

    hostname = socket.gethostname()

    # Don't publish alias if hostname is already 'bcmeter' (no need for CNAME)
    if hostname == ALIAS:
        logger.debug("Hostname is already '%s', skipping CNAME alias", ALIAS)
        _wait_for_stop_or_refresh(stop_event, refresh_event)
        return

    # Wait for avahi to be ready (it starts after network is up)
    for attempt in range(10):
        if stop_event.is_set():
            return
        try:
            bus = dbus.SystemBus()
            server = dbus.Interface(
                bus.get_object("org.freedesktop.Avahi", "/"),
                "org.freedesktop.Avahi.Server",
            )
            # Check avahi is running
            _ = server.GetHostNameFqdn()
            break
        except Exception:
            logger.debug("Waiting for avahi (attempt %d/10)...", attempt + 1)
            time.sleep(3)
    else:
        logger.warning("avahi not available after 30s, skipping CNAME alias")
        _wait_for_stop_or_refresh(stop_event, refresh_event)
        return

    group = None
    try:
        target_fqdn = str(server.GetHostNameFqdn())  # e.g. "bcmeter-a1b2.local"
        alias_fqdn = ALIAS + ".local"

        group_path = server.EntryGroupNew()
        group = dbus.Interface(
            bus.get_object("org.freedesktop.Avahi", group_path),
            "org.freedesktop.Avahi.EntryGroup",
        )

        rdata = _encode_dns_name(target_fqdn)

        # AddRecord(interface, protocol, flags, name, clazz, type, ttl, rdata)
        group.AddRecord(
            dbus.Int32(-1),       # AVAHI_IF_UNSPEC
            dbus.Int32(-1),       # AVAHI_PROTO_UNSPEC
            dbus.UInt32(AVAHI_CNAME_FLAGS),
            alias_fqdn,
            dbus.UInt16(0x01),    # DNS class IN
            dbus.UInt16(0x05),    # DNS type CNAME
            dbus.UInt32(60),      # TTL
            dbus.Array(rdata, signature="y"),
        )

        group.Commit()
        logger.info("Published mDNS alias: %s -> %s", alias_fqdn, target_fqdn)

    except Exception as e:
        err_name = getattr(e, "get_dbus_name", lambda: "")()
        if "Collision" in str(err_name) or "Collision" in str(e):
            logger.info("mDNS convenience alias '%s.local' already claimed; "
                        "using stable hostname '%s' only", ALIAS, socket.gethostname())
        else:
            logger.warning("Failed to publish mDNS CNAME alias: %s", e)
        group = None

    # Keep records alive until shutdown or an explicit lifecycle refresh.
    _wait_for_stop_or_refresh(stop_event, refresh_event)

    # Clean up D-Bus group
    if group is not None:
        try:
            group.Reset()
            group.Free()
        except Exception:
            pass
    logger.debug("mDNS records removed")


def start(stop_event: threading.Event):
    """Blocking call — publish records and wait until stop_event is set."""
    global _started
    _started = True
    while not stop_event.is_set():
        _publish(stop_event, _refresh_event)
        if not stop_event.is_set():
            time.sleep(0.5)


def start_background(stop_event: threading.Event):
    """Launch the alias publisher in a daemon thread."""
    t = threading.Thread(
        target=start,
        args=(stop_event,),
        name="avahi_alias",
        daemon=True,
    )
    t.start()
    return t


def refresh(reason: str = ""):
    """Re-publish mDNS service/alias after hostname or netif lifecycle changes."""
    if reason:
        logger.debug("Refreshing mDNS records (%s)", reason)
    if _started:
        _refresh_event.set()
        return True
    return _ensure_http_service(force_reload=True)
