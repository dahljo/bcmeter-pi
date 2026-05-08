"""Device identity helpers for Linux hostname and mDNS."""

import logging
import re
import socket
import subprocess

logger = logging.getLogger("bcmeter.identity")


def hostname_from_device_name(device_name: str) -> str:
    """Convert a UI device name to a DNS-safe local hostname."""
    name = str(device_name or "bcMeter").strip().lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return (name or "bcmeter")[:63]


def _write_hosts_hostname(hostname: str):
    hosts = "/etc/hosts"
    with open(hosts, "r") as f:
        lines = [l for l in f.read().splitlines() if "127.0.1.1" not in l]
    lines.append(f"127.0.1.1\t{hostname}")
    with open(hosts, "w") as f:
        f.write("\n".join(lines) + "\n")


def sync_system_hostname(device_name: str, reason: str = "hostname changed") -> tuple[str, bool]:
    """Sync Linux hostname to a device name and restart avahi if it changes.

    Returns ``(hostname, changed)``.  The avahi restart is intentionally tied
    only to hostname changes; normal network lifecycle refreshes use the lighter
    avahi service reload path.
    """
    desired = hostname_from_device_name(device_name)
    current = socket.gethostname()
    if current == desired:
        return desired, False

    proc = subprocess.run(
        ["sudo", "hostnamectl", "set-hostname", desired],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        subprocess.run(["sudo", "hostname", desired], capture_output=True, timeout=5)
        with open("/etc/hostname", "w") as f:
            f.write(desired + "\n")

    _write_hosts_hostname(desired)

    try:
        from . import avahi_alias
        avahi_alias.restart_daemon(reason)
    except Exception as exc:
        logger.debug("Could not restart avahi after hostname change: %s", exc)

    logger.info("Hostname updated: %s -> %s", current, desired)
    return desired, True
