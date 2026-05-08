"""WiFi and hotspot management via NetworkManager (nmcli).

Absorbs functionality from the standalone bcMeter_ap_control_loop.py into
the bcmeter package.  Runs as a background thread in the single-process
architecture, managing STA/AP mode switching, credential storage, signal
monitoring, and NTP time synchronisation.
"""

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime

from collections import deque

from .state import state
from . import email_handler
from . import incident_log
from . import avahi_alias
from .modem import IoTManager

logger = logging.getLogger("bcmeter.wifimgr")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AP_CON_NAME = "bcMeter-ap"
STA_CON_NAME = "bcMeter-sta"
WLAN_IFACE = "wlan0"

INTERNET_CHECK_HOST = "www.google.com"
INTERNET_CHECK_PORT = 80
INTERNET_CHECK_TIMEOUT = 3

AP_TIMEOUT = 3600  # seconds before giving up on AP if no client
RECONNECT_INTERVAL = 30

_MAX_CONNECTION_RETRIES = 3
_CONNECTIVITY_TIMEOUT = 120
_HAPPY_CHECK_INTERVAL = 20
_SCAN_INTERVAL = 5

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _sh(cmd, timeout=30):
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return (
            result.returncode,
            (result.stdout or "").strip(),
            (result.stderr or "").strip(),
        )
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as exc:
        return 1, "", str(exc)


def _nmcli(args, timeout=30):
    """Run an nmcli command with colourless output."""
    return _sh(["nmcli", "--colors", "no"] + args, timeout=timeout)


def _systemctl(args, timeout=30):
    """Run a systemctl command."""
    return _sh(["systemctl"] + args, timeout=timeout)


# ---------------------------------------------------------------------------
# NetworkManager class
# ---------------------------------------------------------------------------


class NetworkManager:
    """WiFi STA/AP manager backed by NetworkManager."""

    def __init__(self, cfg, base_dir="/home/bcmeter"):
        self._cfg = cfg
        self._base_dir = base_dir
        self._credentials_file = os.path.join(base_dir, "bcMeter_wifi.json")

        # Internal tracking (used by task loop)
        self._time_synced = False
        self._in_happy_state = False
        self._last_happy_check = 0.0
        self._internet_wait_start = 0.0
        self._connection_retries = 0
        self._recovery_attempts = 0
        self._manage_guard_until = 0.0
        self._last_sta_error = ""
        self._last_sta_error_time = 0.0
        self._last_mdns_key = None

        # WiFi drop monitoring (30-min sliding window)
        self._drop_times = deque()
        self._DROP_WINDOW_S = 30 * 60
        self._DROP_ALERT_THRESHOLD = 4
        self._was_connected = False

        # Shared modem instance (populated by _modem_onboarding thread)
        self._iot_manager: "IoTManager | None" = None

    # ------------------------------------------------------------------
    # WiFi credential management
    # ------------------------------------------------------------------

    def get_credentials(self):
        """Return (ssid, password) from bcMeter_wifi.json, or (None, None)."""
        try:
            with open(self._credentials_file, "r") as fh:
                data = json.load(fh)
            ssid = data.get("wifi_ssid")
            pwd = data.get("wifi_pwd")
            if not self.validate_credentials(ssid, pwd):
                return None, None
            return ssid, pwd
        except FileNotFoundError:
            return None, None
        except Exception as exc:
            logger.warning("Failed to read WiFi credentials: %s", exc)
            return None, None

    def save_credentials(self, ssid, password):
        """Persist WiFi credentials to bcMeter_wifi.json."""
        try:
            with open(self._credentials_file, "w") as fh:
                json.dump({"wifi_ssid": ssid, "wifi_pwd": password}, fh, indent=2)
            os.chmod(self._credentials_file, 0o666)
            logger.info("WiFi credentials saved for SSID=%s", ssid)
        except Exception as exc:
            logger.error("Failed to save WiFi credentials: %s", exc)

    def delete_credentials(self):
        """Clear stored WiFi credentials."""
        try:
            with open(self._credentials_file, "w") as fh:
                fh.write('{\n\t"wifi_ssid": "",\n\t"wifi_pwd": ""\n}')
            os.chmod(self._credentials_file, 0o666)
        except Exception as exc:
            logger.error("Failed to delete WiFi credentials: %s", exc)
        logger.debug("WiFi credentials cleared")

    @staticmethod
    def validate_credentials(ssid, pwd):
        """Return True if *ssid* and *pwd* look plausible."""
        if ssid is None or pwd is None:
            return False
        if not isinstance(ssid, str) or not isinstance(pwd, str):
            return False
        if not (1 <= len(ssid) <= 32):
            return False
        if not (8 <= len(pwd) <= 63):
            return False
        if not all(32 <= ord(c) <= 126 for c in pwd):
            return False
        return True

    # ------------------------------------------------------------------
    # WiFi scanning
    # ------------------------------------------------------------------

    def scan_networks(self):
        """Return a list of visible WiFi networks.

        Each entry is a dict with keys *ssid*, *signal_dbm* (dBm int),
        and *security*.
        """
        # Trigger a rescan first (best effort)
        _nmcli(["dev", "wifi", "rescan"], timeout=15)
        time.sleep(3)

        # Request BSSID too — deduplication by SSID, keep strongest
        rc, out, _ = _nmcli(
            ["-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
             "--rescan", "no"],
            timeout=15,
        )
        if rc != 0:
            return []

        seen = {}
        for line in out.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            ssid = parts[0].strip().replace(r"\:", ":")
            if not ssid:
                continue
            try:
                signal_pct = int(parts[1].strip())
            except ValueError:
                signal_pct = 0
            security = parts[2].strip()
            # nmcli SIGNAL is 0-100 percentage; convert to approximate dBm
            # dBm ≈ (percent / 2) - 100  (rough but standard mapping)
            signal_dbm = int(signal_pct / 2) - 100 if signal_pct > 0 else -100
            # Keep the strongest sighting per SSID
            if ssid not in seen or signal_dbm > seen[ssid]["signal_dbm"]:
                seen[ssid] = {
                    "ssid": ssid,
                    "signal_dbm": signal_dbm,
                    "security": security,
                }
        return sorted(seen.values(), key=lambda n: n["signal_dbm"], reverse=True)

    def get_current_network(self):
        """Return the SSID of the currently connected WiFi, or None."""
        rc, out, _ = _nmcli(
            ["-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=15
        )
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(":", 1)
                if len(parts) == 2 and parts[0] == "yes":
                    ssid = parts[1].replace(r"\:", ":")
                    return ssid or None

        # Fallback: iwgetid
        for _ in range(3):
            try:
                out = subprocess.check_output(
                    ["iwgetid", "-r"], timeout=5
                ).decode("utf-8").strip()
                if out:
                    return out
            except Exception:
                pass
            time.sleep(1)
        return None

    def get_signal_info(self, ssid):
        """Return signal info dict for *ssid*, or None if not visible.

        Dict keys: *signal_percent*, *signal_dbm*.
        """
        if not ssid:
            return None
        _nmcli(["dev", "wifi", "rescan"], timeout=15)
        time.sleep(2)

        rc, out, _ = _nmcli(
            ["-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list"], timeout=15
        )
        if rc != 0:
            return None

        best = None
        for line in out.splitlines():
            parts = line.split(":", 1)
            if len(parts) != 2:
                continue
            line_ssid = parts[0].strip().replace(r"\:", ":")
            if line_ssid != ssid:
                continue
            try:
                pct = int(parts[1].strip())
            except ValueError:
                continue
            if best is None or pct > best:
                best = pct

        if best is None:
            return None

        dbm = int((best / 1.42) - 110)
        return {"signal_percent": best, "signal_dbm": dbm}

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect_sta(self, ssid, password):
        """Create an NM STA profile and try to connect. Return True on success.

        Keeps the AP running during the attempt so the user doesn't lose
        their connection to the device.  AP is only torn down after STA
        is confirmed working.
        """
        if not self.validate_credentials(ssid, password):
            logger.error("Invalid WiFi credentials for SSID=%s", ssid)
            return False

        self._ensure_nm_ready()

        # (Re-)create the STA profile (don't tear down AP yet)
        if not self._ensure_sta_profile(ssid, password):
            return False

        connected = self._activate_sta(ssid, timeout=45)
        if not connected and self._last_error_missing_secret():
            logger.warning("STA profile missing PSK -- restoring stored WiFi credential")
            if self._write_sta_secret(password):
                connected = self._activate_sta(ssid, timeout=45)

        if not connected:
            logger.warning("STA connection to %s failed — keeping AP", ssid)
            # Make sure AP is still up
            if self._active_connection_name() != AP_CON_NAME:
                self._ensure_ap()
            return False

        # Verify we got a real DHCP lease
        if not self._dev_has_ip():
            logger.warning("No DHCP lease for %s — keeping AP", ssid)
            _nmcli(["con", "down", STA_CON_NAME], timeout=10)
            if self._active_connection_name() != AP_CON_NAME:
                self._ensure_ap()
            return False

        # STA confirmed — now tear down AP
        self.stop_ap()
        self._refresh_mdns("sta")
        logger.info("Connected to WiFi SSID=%s", ssid)
        incident_log.add("ok", "WiFi connected: %s", ssid)
        return True

    def start_ap(self):
        """Start the bcMeter hotspot. Return True on success."""
        logger.info("Starting Access Point...")
        self._ensure_nm_ready()

        ap_ssid = (
            self._cfg.get("device_name") or socket.gethostname() or "bcMeter"
        )
        ap_psk = self._cfg.get("ap_password") or "bcMeterbcMeter"
        ap_ip = "192.168.18.8/24"
        ap_channel = str(self._cfg.get_int("ap_channel", 7))
        ap_secured = self._cfg.get_bool("ap_secured", False)

        self._con_down_all_wifi()
        time.sleep(1)

        # Delete old AP profile if present
        if AP_CON_NAME in self._list_connections():
            self._con_delete(AP_CON_NAME)
            time.sleep(1)

        # Build nmcli arguments
        add_args = [
            "con", "add",
            "type", "wifi",
            "ifname", WLAN_IFACE,
            "con-name", AP_CON_NAME,
            "ssid", ap_ssid,
            "autoconnect", "no",
            "wifi.mode", "ap",
            "wifi.band", "bg",
            "wifi.channel", ap_channel,
            "ipv4.method", "shared",
            "ipv4.addresses", ap_ip,
            "ipv6.method", "disabled",
        ]
        if ap_secured:
            add_args += [
                "wifi-sec.key-mgmt", "wpa-psk",
                "wifi-sec.psk", ap_psk,
            ]

        rc, _, err = _nmcli(add_args, timeout=30)
        if rc != 0:
            logger.error("Failed to create AP profile: %s", err)
            return False

        time.sleep(1)
        rc, _, err = _nmcli(["-w", "30", "con", "up", AP_CON_NAME], timeout=40)
        if rc != 0:
            logger.error("Failed to activate AP: %s", err)
            return False

        time.sleep(3)
        active = self._active_connection_name()
        if active == AP_CON_NAME:
            logger.info("Hotspot active: SSID=%s (secured=%s)", ap_ssid, ap_secured)
            self._refresh_mdns("ap")
            return True

        logger.error("AP activation verification failed (active=%s)", active)
        return False

    def stop_ap(self):
        """Deactivate the bcMeter hotspot. Return True on success."""
        logger.debug("Stopping hotspot")
        _nmcli(["con", "down", AP_CON_NAME], timeout=20)
        return True

    def _ensure_ap(self):
        """Start AP with retries and escalating recovery.

        Guarantees that either the AP is running when this method returns
        or every recovery strategy has been exhausted (in which case the
        device is in a bad state but at least it is logged clearly).
        """
        _MAX_AP_RETRIES = 3
        for attempt in range(1, _MAX_AP_RETRIES + 1):
            if self.start_ap():
                return True
            logger.warning("AP start failed (attempt %d/%d)", attempt, _MAX_AP_RETRIES)
            incident_log.add("error", "AP start failed (attempt %d/%d)", attempt, _MAX_AP_RETRIES)
            if attempt < _MAX_AP_RETRIES:
                self._force_wlan0_reset()
                time.sleep(5)
        # All retries exhausted
        logger.critical("AP could not be started after %d attempts", _MAX_AP_RETRIES)
        incident_log.add("error", "AP could not be started after %d attempts — device unreachable via WiFi", _MAX_AP_RETRIES)
        state.set("wifi_mode", "error")
        return False

    def is_connected(self):
        """Return True if wlan0 has a non-link-local IP address."""
        return self._dev_has_ip()

    def check_internet(self):
        """Return True if we can reach the Internet (TCP connect test)."""
        try:
            sock = socket.create_connection(
                (INTERNET_CHECK_HOST, INTERNET_CHECK_PORT),
                timeout=INTERNET_CHECK_TIMEOUT,
            )
            sock.close()
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Service management
    # ------------------------------------------------------------------

    @staticmethod
    def check_service_running(name):
        """Return True if the systemd service *name* is active."""
        rc, out, _ = _systemctl(["is-active", name], timeout=10)
        return rc == 0 and out.strip() == "active"

    @staticmethod
    def restart_service(name):
        """Restart a systemd service by *name*. Return True on success."""
        rc, _, err = _systemctl(["restart", name], timeout=30)
        if rc != 0:
            logger.error("Failed to restart service %s: %s", name, err)
            return False
        # Verify it came back
        time.sleep(2)
        rc2, out2, _ = _systemctl(["is-active", name], timeout=10)
        return rc2 == 0 and out2.strip() == "active"

    # ------------------------------------------------------------------
    # Main background task
    # ------------------------------------------------------------------

    def task(self, stop_event: threading.Event):
        """Main background loop managing WiFi connectivity.

        Intended to be run in a daemon thread::

            t = threading.Thread(target=nm.task, args=(stop_evt,), daemon=True)
            t.start()
        """
        logger.info(
            "Network manager started for %s", socket.gethostname()
        )

        enable_wifi = self._cfg.get_bool("enable_wifi", True)
        if not enable_wifi:
            logger.info("WiFi disabled in config -- network task exiting")
            state.update(wifi_enabled=False)
            return

        state.set("wifi_enabled", True)
        _nmcli(["radio", "wifi", "on"], timeout=10)
        self._set_regulatory_domain()
        self._ensure_nm_ready()

        # ----- initial connection attempt -----
        wifi_ssid, wifi_pwd = self.get_credentials()
        if wifi_ssid and self._is_ssid_in_range(wifi_ssid):
            logger.info("Attempting initial STA connection to %s", wifi_ssid)
            self._manage_wifi("boot")
        else:
            logger.info("No known WiFi in range -- starting hotspot")
            self._ensure_ap()

        # Kick off modem onboarding in a sub-thread so it doesn't block
        # the WiFi loop. If WiFi already achieved internet the email is
        # skipped; otherwise we send the "online via 4G" onboarding email.
        def _modem_onboarding():
            try:
                mgr = IoTManager(self._cfg)
                if not mgr.detect():
                    return
                state.set("modem_present", True)
                self._iot_manager = mgr
                logger.info("Modem detected, attempting cellular connection")
                if not mgr.connect():
                    logger.warning("Modem connection failed")
                    return
                info = mgr.get_sim_info()
                wan_ip = info.get("ip", "")
                operator = info.get("operator", "")
                signal = str(info.get("signal", ""))
                state.set("modem_signal", signal)
                state.set("modem_operator", operator)
                # If WiFi already connected with internet, skip modem email
                if self._in_happy_state:
                    logger.info("WiFi already online, skipping modem onboarding email")
                    return
                logger.info("WiFi not online, sending modem onboarding email")
                email_handler.send_modem_online(
                    wan_ip=wan_ip, city="", country="",
                    signal=signal, operator=operator,
                    cpsi=mgr.get_cpsi(),
                )
            except Exception as exc:
                logger.debug("Modem onboarding failed: %s", exc)

        threading.Thread(
            target=_modem_onboarding, daemon=True, name="modem_onboard"
        ).start()

        # Kick off NTP synchronisation in a sub-thread
        ntp_stop = threading.Event()
        ntp_thread = threading.Thread(
            target=self._time_sync_loop, args=(ntp_stop,), daemon=True
        )
        ntp_thread.start()

        # ----- main loop -----
        try:
            while not stop_event.is_set():
                try:
                    self._loop_iteration()
                except Exception as exc:
                    logger.error("Loop iteration error: %s", exc)
                stop_event.wait(_SCAN_INTERVAL)
        finally:
            ntp_stop.set()
            # Best-effort cleanup
            try:
                self.stop_ap()
            except Exception:
                pass
            try:
                _nmcli(["con", "down", STA_CON_NAME], timeout=10)
            except Exception:
                pass
            logger.info("Network manager stopped")

    # ------------------------------------------------------------------
    # Loop internals
    # ------------------------------------------------------------------

    def _loop_iteration(self):
        """Single iteration of the main monitoring loop."""
        run_hotspot = self._cfg.get_bool("run_hotspot", False)
        is_online = self.check_internet()
        ap_active = self._active_connection_name() == AP_CON_NAME
        current_network = self.get_current_network()
        wifi_ssid, _ = self.get_credentials()
        now = time.time()

        # --- update shared state ---
        state.update(
            wifi_mode="ap" if ap_active else "sta",
            wifi_ssid=current_network or "",
            internet=is_online,
            in_hotspot=run_hotspot or ap_active,
        )

        sig = self._check_connection_quality(wifi_ssid)
        if sig and sig.get("signal_dbm") is not None:
            state.set("wifi_rssi", sig["signal_dbm"])

        # --- connectivity timeout ---
        if (
            current_network
            and not is_online
            and not ap_active
            and self._internet_wait_start > 0
        ):
            if now - self._internet_wait_start > _CONNECTIVITY_TIMEOUT:
                logger.warning("Connectivity timeout")
                self._internet_wait_start = 0.0
                self._manage_wifi("timeout")
                return

        # --- connected but no internet ---
        if current_network and not is_online and not ap_active:
            router_ok = self._ping_router()
            if not router_ok:
                quality = self._check_connection_quality(wifi_ssid)
                if quality and quality.get("signal_dbm") is not None:
                    if self._evaluate_wifi_quality(quality["signal_dbm"]) <= 1:
                        self._manage_wifi("poor_signal")
                        return
                if not self._in_happy_state and self._internet_wait_start == 0:
                    self._manage_wifi("router_unreachable")

        # --- connected and reachable, but happy-state entry has not run yet ---
        if (
            not self._in_happy_state
            and current_network == wifi_ssid
            and not ap_active
            and (is_online or self._ping_router())
        ):
            self._manage_wifi("connected")
            return

        # --- happy state monitoring ---
        if self._in_happy_state:
            if now - self._last_happy_check > _HAPPY_CHECK_INTERVAL:
                self._last_happy_check = now
                if not self._check_happy_state(wifi_ssid):
                    self._handle_exit_from_happy_state(wifi_ssid)
            return

        # --- WiFi driver sanity ---
        if not self._is_wifi_driver_loaded():
            logger.error("WiFi driver not loaded")
            if not self._reload_wifi_driver():
                self._ensure_ap()
            return

        # --- not connected to expected SSID ---
        if current_network != wifi_ssid and wifi_ssid and not ap_active:
            self._manage_wifi("ssid_mismatch")

        # --- periodic reconnect attempt from AP ---
        if ap_active and wifi_ssid:
            if int(now) % 60 < _SCAN_INTERVAL:
                if self._is_ssid_in_range(wifi_ssid):
                    logger.debug("Known network %s detected during AP", wifi_ssid)
                    self._manage_wifi("periodic_reconnect")

        # --- AP timeout (no client, no connection ever) ---
        if not is_online and not run_hotspot:
            uptime = self._get_uptime() if self._time_synced else AP_TIMEOUT - 1
            if uptime >= AP_TIMEOUT:
                if not ap_active:
                    self._ensure_ap()

    # ------------------------------------------------------------------
    # WiFi management state machine
    # ------------------------------------------------------------------

    def _send_wifi_onboarding_if_needed(self):
        """Queue the WiFi onboarding notification when mail is configured."""
        try:
            modem_available = False
            modem_op = ""
            modem_sig = ""
            mgr = self._iot_manager
            if mgr is not None and mgr.is_connected():
                modem_available = True
                try:
                    info = mgr.get_sim_info()
                    modem_op = info.get("operator", "")
                    modem_sig = str(info.get("signal", ""))
                except Exception:
                    pass
            if email_handler.has_email_configured():
                email_handler.send_wifi_connected(
                    modem_available=modem_available,
                    modem_operator=modem_op,
                    modem_signal=modem_sig,
                )
                # Mark Phase 1 onboarding complete only after the welcome
                # notification was actually queued.
                if not self._cfg.get_bool("onboarding_step_one", False):
                    self._cfg.set_bool("onboarding_step_one", True)
                    self._cfg.save()
                    logger.info("onboarding_step_one = true")
            else:
                logger.info(
                    "WiFi online; onboarding email deferred until mail config is set"
                )
        except Exception as exc:
            logger.debug("WiFi onboarding notification failed: %s", exc)

    def _manage_wifi(self, checkpoint=None):
        """Attempt to (re)connect to STA or fall back to AP."""
        now = time.time()
        if now < self._manage_guard_until:
            logger.debug("manage_wifi suppressed (%s)", checkpoint)
            return
        self._manage_guard_until = now + 8

        run_hotspot = self._cfg.get_bool("run_hotspot", False)
        wifi_ssid, wifi_pwd = self.get_credentials()
        current_network = self.get_current_network()
        is_online = self.check_internet()

        logger.debug(
            "manage_wifi(%s): current=%s online=%s ssid=%s",
            checkpoint, current_network, is_online, wifi_ssid,
        )

        # No credentials or forced hotspot
        if (not wifi_ssid or not wifi_pwd) or run_hotspot:
            if self._active_connection_name() != AP_CON_NAME:
                logger.info("No credentials / forced hotspot -- starting AP")
                self._ensure_ap()
            return

        # Already connected and reachable
        if current_network == wifi_ssid and (is_online or self._ping_router()):
            self._connection_retries = 0
            self._recovery_attempts = 0
            if self._active_connection_name() == AP_CON_NAME:
                self.stop_ap()
            self._refresh_mdns("sta")
            if not self._in_happy_state:
                self._send_wifi_onboarding_if_needed()
            self._in_happy_state = True
            self._was_connected = True
            return

        # Target SSID in range?
        sig_info = self.get_signal_info(wifi_ssid)
        if sig_info:
            logger.debug(
                "Scan: %s at %d%% (%d dBm)",
                wifi_ssid, sig_info["signal_percent"], sig_info["signal_dbm"],
            )
        else:
            logger.debug("SSID %s not visible -- keeping/starting AP", wifi_ssid)
            if self._active_connection_name() != AP_CON_NAME:
                self._ensure_ap()
            return

        # Tear down AP if it is active
        was_ap = self._active_connection_name() == AP_CON_NAME
        if was_ap:
            if not self.stop_ap():
                self._force_wlan0_reset()
                self._ensure_ap()
                return

        # Attempt STA connection
        if not self._ensure_sta_profile(wifi_ssid, wifi_pwd):
            self._ensure_ap()
            return

        connected = self._activate_sta(wifi_ssid, timeout=45)
        if not connected and self._last_error_missing_secret():
            logger.warning("STA profile missing PSK -- restoring stored WiFi credential")
            if self._write_sta_secret(wifi_pwd):
                connected = self._activate_sta(wifi_ssid, timeout=45)

        if not connected:
            self._connection_retries += 1
            self._recovery_attempts += 1
            if self._check_psk_errors():
                if self._connection_retries >= _MAX_CONNECTION_RETRIES:
                    logger.warning("Repeated PSK auth failures -- clearing credentials")
                    self._connection_retries = 0
                    self.delete_credentials()
                else:
                    logger.warning(
                        "PSK auth failure detected (%d/%d) -- keeping credentials",
                        self._connection_retries, _MAX_CONNECTION_RETRIES,
                    )
                self._ensure_ap()
                return
            if was_ap or self._connection_retries >= _MAX_CONNECTION_RETRIES:
                self._connection_retries = 0
                self._ensure_ap()
            return

        if not self._dev_has_ip():
            self._connection_retries += 1
            if was_ap or self._connection_retries >= _MAX_CONNECTION_RETRIES:
                self._connection_retries = 0
                self._ensure_ap()
            return
        self._refresh_mdns("sta")

        # Check quality
        quality = self._check_connection_quality(wifi_ssid)
        if quality:
            logger.debug(
                "STA quality: %s sig=%d%% dbm=%s stable=%s",
                quality.get("ssid"), quality.get("signal_percent", 0),
                quality.get("signal_dbm"), quality.get("is_stable"),
            )
        if quality and quality.get("is_stable"):
            self._connection_retries = 0
            self._recovery_attempts = 0
            if not self._in_happy_state:
                self._send_wifi_onboarding_if_needed()
            self._in_happy_state = True
            return

        self._in_happy_state = False

    # ------------------------------------------------------------------
    # Happy-state helpers
    # ------------------------------------------------------------------

    def _check_happy_state(self, wifi_ssid):
        """Return True if we're still happily connected to *wifi_ssid*."""
        if not wifi_ssid:
            return False
        current = self.get_current_network()
        if current != wifi_ssid:
            return False
        if self.check_internet():
            if self._active_connection_name() == AP_CON_NAME:
                return False
            return True
        if self._ping_router():
            return True
        return False

    def _record_wifi_drop(self):
        """Record a WiFi drop and send alert if threshold exceeded."""
        now = time.time()
        self._drop_times.append(now)
        # Prune old entries outside the sliding window
        cutoff = now - self._DROP_WINDOW_S
        while self._drop_times and self._drop_times[0] < cutoff:
            self._drop_times.popleft()
        drop_count = len(self._drop_times)
        incident_log.add("warn", "WiFi drop (%d in 30 min)", drop_count)
        logger.warning("WiFi drop recorded (%d in last 30 min)", drop_count)
        if drop_count >= self._DROP_ALERT_THRESHOLD:
            try:
                email_handler.send_bad_wifi_alert(drop_count, 30)
            except Exception:
                logger.debug("Failed to send bad WiFi alert")

    def _handle_exit_from_happy_state(self, wifi_ssid):
        """Recover when the happy state is disturbed."""
        logger.info("Happy state lost -- analysing")
        self._in_happy_state = False
        self._record_wifi_drop()

        if not self._is_wifi_driver_loaded():
            logger.error("WiFi driver gone")
            if not self._reload_wifi_driver():
                self._ensure_ap()
            return

        current = self.get_current_network()
        is_online = self.check_internet()

        if current == wifi_ssid and not is_online:
            if self._ping_router():
                logger.debug("Router reachable but no internet")
                self._internet_wait_start = time.time()
                return
            quality = self._check_connection_quality(wifi_ssid)
            if quality and quality.get("signal_dbm") is not None:
                if self._evaluate_wifi_quality(quality["signal_dbm"]) <= 1:
                    self._internet_wait_start = 0.0
                    self._manage_wifi("poor_signal")
                    return
            if self._internet_wait_start == 0:
                self._internet_wait_start = time.time()
            elif time.time() - self._internet_wait_start > _CONNECTIVITY_TIMEOUT:
                self._internet_wait_start = 0.0
                self._manage_wifi("connectivity_timeout")
        elif current != wifi_ssid:
            self._internet_wait_start = 0.0
            self._manage_wifi("network_change")
        else:
            self._internet_wait_start = 0.0
            self._manage_wifi("unknown_issue")

    # ------------------------------------------------------------------
    # NTP time synchronisation
    # ------------------------------------------------------------------

    def _time_sync_loop(self, stop_event: threading.Event):
        """Check NTP sync periodically until achieved."""
        while not stop_event.is_set():
            self._time_synced = self._check_time_sync()
            if self._time_synced:
                logger.info("System clock synchronised")
                return
            stop_event.wait(120)

    @staticmethod
    def _check_time_sync():
        """Return True if the system clock appears synchronised."""
        try:
            if datetime.now().year > 2024:
                return True
            rc, out, _ = _sh(["timedatectl", "status"], timeout=10)
            return rc == 0 and "System clock synchronized: yes" in out
        except Exception:
            return False

    # ------------------------------------------------------------------
    # NM profile helpers
    # ------------------------------------------------------------------

    def _ensure_nm_ready(self):
        """Make sure NetworkManager is running and wlan0 is managed."""
        if not self.check_service_running("NetworkManager"):
            _systemctl(["start", "NetworkManager"], timeout=20)
            time.sleep(2)

        # Wait for NM to report running
        for _ in range(20):
            rc, out, _ = _nmcli(["-t", "-f", "RUNNING", "general"], timeout=5)
            if rc == 0 and out.strip().lower() == "running":
                break
            time.sleep(0.5)
        else:
            rc, out, err = _nmcli(["general", "status"], timeout=10)
            logger.warning(
                "NM not confirmed running (rc=%d) out=%s err=%s", rc, out, err
            )

        # Ensure wlan0 is managed
        rc, out, _ = _nmcli(
            ["-g", "GENERAL.STATE", "dev", "show", WLAN_IFACE], timeout=10
        )
        if "unmanaged" in out.lower():
            _nmcli(["dev", "set", WLAN_IFACE, "managed", "yes"], timeout=10)
            time.sleep(1)

        _nmcli(["radio", "wifi", "on"], timeout=10)

    def _ensure_sta_profile(self, ssid, pwd):
        """Create (or recreate) the STA connection profile. Return True on success."""
        cons = self._list_connections()
        if STA_CON_NAME in cons:
            self._con_delete(STA_CON_NAME)
            time.sleep(1)

        rc, _, err = _nmcli([
            "con", "add",
            "type", "wifi",
            "ifname", WLAN_IFACE,
            "con-name", STA_CON_NAME,
            "ssid", ssid,
            "autoconnect", "no",
            "wifi.mode", "infrastructure",
            "ipv4.method", "auto",
            "ipv6.method", "disabled",
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", pwd,
        ], timeout=30)
        if rc != 0:
            logger.error("Failed to create STA profile: %s", err)
            return False
        return self._write_sta_secret(pwd)

    @staticmethod
    def _list_connections():
        """Return a set of NM connection names."""
        rc, out, _ = _nmcli(["-t", "-f", "NAME", "con", "show"], timeout=20)
        if rc != 0:
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}

    @staticmethod
    def _active_connection_name(ifname=WLAN_IFACE):
        """Return the active NM connection name on *ifname*, or None."""
        rc, out, _ = _nmcli(
            ["-g", "GENERAL.CONNECTION", "dev", "show", ifname], timeout=15
        )
        if rc != 0:
            return None
        val = out.strip()
        return val if val and val != "--" else None

    def _con_up(self, name, timeout=45):
        """Bring a connection up. Return True on success."""
        self._con_down_all_wifi()
        time.sleep(1)
        rc, out, err = _nmcli(
            ["-w", str(timeout), "con", "up", name], timeout=timeout + 10
        )
        if rc != 0:
            self._last_sta_error = err or out
            self._last_sta_error_time = time.time()
            logger.warning("nmcli con up %s failed: %s", name, self._last_sta_error)
            return False
        self._last_sta_error = ""
        self._last_sta_error_time = 0.0
        time.sleep(2)
        return True

    def _activate_sta(self, ssid, timeout=45):
        """Activate the STA profile and wait until it is associated to *ssid*."""
        ok = self._con_up(STA_CON_NAME, timeout=timeout)
        time.sleep(2)
        if not ok:
            return False

        for _ in range(10):
            if self.get_current_network() == ssid:
                return True
            time.sleep(2)
        return False

    @staticmethod
    def _write_sta_secret(pwd):
        """Force the stored PSK into the NM profile as a persistent secret."""
        rc, _, err = _nmcli([
            "con", "modify", STA_CON_NAME,
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", pwd,
            "wifi-sec.psk-flags", "0",
        ], timeout=30)
        if rc != 0:
            logger.error("Failed to store STA PSK in NetworkManager profile: %s", err)
            return False
        return True

    @staticmethod
    def _con_down(name):
        _nmcli(["con", "down", name], timeout=20)

    @staticmethod
    def _con_delete(name):
        _nmcli(["con", "delete", name], timeout=20)

    @staticmethod
    def _con_down_all_wifi():
        """Bring down every active wireless connection."""
        rc, out, _ = _nmcli(
            ["-t", "-f", "NAME,TYPE", "con", "show", "--active"], timeout=15
        )
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and "wireless" in parts[1]:
                    _nmcli(["con", "down", parts[0]], timeout=10)

    # ------------------------------------------------------------------
    # IP / routing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dev_ipv4(ifname=WLAN_IFACE):
        """Return first non-link-local IPv4 address for *ifname*, or ''."""
        rc, out, _ = _nmcli(
            ["-g", "IP4.ADDRESS", "dev", "show", ifname], timeout=10
        )
        if rc != 0:
            return ""
        for line in out.splitlines():
            ip = line.split("/")[0].strip()
            if ip and not ip.startswith("169.254."):
                return ip
        return ""

    @staticmethod
    def _dev_has_ip(ifname=WLAN_IFACE):
        """Return True if *ifname* has a non-link-local IPv4 address."""
        return bool(NetworkManager._dev_ipv4(ifname))

    def _refresh_mdns(self, mode):
        """Re-publish Avahi records after AP/STA has a real lifecycle boundary."""
        ip = self._dev_ipv4()
        key = (mode, ip, socket.gethostname())
        if key == self._last_mdns_key:
            return
        try:
            avahi_alias.refresh(f"{mode} {ip}".strip())
            self._last_mdns_key = key
        except Exception as exc:
            logger.debug("mDNS refresh failed: %s", exc)

    @staticmethod
    def _get_default_gateway():
        rc, out, _ = _sh(["ip", "route", "show", "default"], timeout=5)
        if rc != 0:
            return None
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None

    def _ping_router(self):
        """Return True if the default gateway responds to a ping."""
        gw = self._get_default_gateway()
        if not gw:
            return False
        rc, _, _ = _sh(["ping", "-c", "1", "-W", "2", gw], timeout=5)
        return rc == 0

    # ------------------------------------------------------------------
    # Signal quality helpers
    # ------------------------------------------------------------------

    def _check_connection_quality(self, ssid=None):
        """Return a dict of current connection quality, or None.

        Keys: *ssid*, *signal_percent*, *signal_dbm*, *is_stable*.
        """
        try:
            rc, out, _ = _sh(
                ["nmcli", "-t", "-f", "ACTIVE,SIGNAL,SSID", "dev", "wifi"],
                timeout=10,
            )
            if rc != 0:
                return None
            for line in out.splitlines():
                if line.startswith("yes:"):
                    parts = line.split(":", 2)
                    if len(parts) < 3:
                        continue
                    _, sig_str, cur_ssid = parts
                    cur_ssid = cur_ssid.replace(r"\:", ":")
                    if ssid and cur_ssid != ssid:
                        return None
                    try:
                        pct = int(sig_str)
                        dbm = int((pct / 1.42) - 110)
                    except ValueError:
                        return None
                    return {
                        "ssid": cur_ssid,
                        "signal_percent": pct,
                        "signal_dbm": dbm,
                        "is_stable": pct > 30,
                    }
            return None
        except Exception as exc:
            logger.error("Error checking WiFi quality: %s", exc)
            return None

    @staticmethod
    def _evaluate_wifi_quality(signal_dbm):
        """Map a dBm value to a 0-4 quality tier."""
        if signal_dbm is None:
            return 0
        if signal_dbm >= -55:
            return 4
        if signal_dbm >= -65:
            return 3
        if signal_dbm >= -75:
            return 2
        if signal_dbm >= -85:
            return 1
        return 0

    def _is_ssid_in_range(self, ssid):
        """Return True if *ssid* is visible in a WiFi scan."""
        return self.get_signal_info(ssid) is not None

    # ------------------------------------------------------------------
    # Driver / interface recovery
    # ------------------------------------------------------------------

    @staticmethod
    def _is_wifi_driver_loaded():
        try:
            rc, out, _ = _sh(["ip", "link", "show", WLAN_IFACE], timeout=5)
            return rc == 0 and WLAN_IFACE in out
        except Exception:
            return False

    def _reload_wifi_driver(self):
        logger.debug("Reloading WiFi driver (brcmfmac)")
        try:
            self._con_down_all_wifi()
            _sh(["sudo", "modprobe", "-r", "brcmfmac"], timeout=10)
            time.sleep(3)
            _sh(["sudo", "modprobe", "brcmfmac"], timeout=10)
            time.sleep(5)
            _nmcli(["dev", "set", WLAN_IFACE, "managed", "yes"], timeout=10)
            return self._is_wifi_driver_loaded()
        except Exception:
            return False

    def _force_wlan0_reset(self):
        logger.debug("Resetting %s interface", WLAN_IFACE)
        try:
            self._con_down_all_wifi()
            time.sleep(1)
            _sh(["sudo", "ip", "link", "set", WLAN_IFACE, "down"], timeout=5)
            time.sleep(2)
            _sh(["sudo", "ip", "link", "set", WLAN_IFACE, "up"], timeout=5)
            time.sleep(3)
            _nmcli(["dev", "set", WLAN_IFACE, "managed", "yes"], timeout=10)
            time.sleep(2)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Regulatory domain
    # ------------------------------------------------------------------

    def _set_regulatory_domain(self):
        country = self._cfg.get_string("country", "DE")
        if not country or len(country) != 2:
            country = "DE"
        try:
            subprocess.run(
                ["sudo", "iw", "reg", "set", country],
                check=True, capture_output=True, timeout=10,
            )
        except Exception as exc:
            logger.error("Failed to set regulatory domain to %s: %s", country, exc)

    # ------------------------------------------------------------------
    # PSK error detection
    # ------------------------------------------------------------------

    def _check_psk_errors(self):
        """Return True if recent logs suggest a wrong WiFi password."""
        psk_patterns = [
            r"psk.*invalid",
            r"wrong.*(?:key|password|psk)",
            r"(?:pre-shared key|psk).*incorrect",
            r"4[- ]?way handshake.*fail",
            r"auth(?:entication)?.*fail",
            r"bad.*password",
        ]

        # Check cached last error
        now = time.time()
        if self._last_sta_error and now - self._last_sta_error_time < 120:
            for pat in psk_patterns:
                if re.search(pat, self._last_sta_error, re.IGNORECASE):
                    return True

        # Check journalctl
        try:
            rc, out, _ = _sh(
                [
                    "journalctl", "-u", "NetworkManager",
                    "--since", "2 min ago", "-n", "50", "--no-pager",
                ],
                timeout=10,
            )
            if rc != 0:
                return False
            for line in out.splitlines():
                for pat in psk_patterns:
                    if re.search(pat, line, re.IGNORECASE):
                        return True
        except Exception:
            pass
        return False

    def _last_error_missing_secret(self):
        """Return True when NM failed because its profile lacked a supplied PSK."""
        now = time.time()
        if not self._last_sta_error or now - self._last_sta_error_time >= 120:
            return False
        missing_secret_patterns = [
            r"password .*not given",
            r"secrets? were required, but not provided",
            r"no secrets",
        ]
        for pat in missing_secret_patterns:
            if re.search(pat, self._last_sta_error, re.IGNORECASE):
                return True
        return False

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_uptime():
        try:
            with open("/proc/uptime", "r") as fh:
                return int(float(fh.read().split()[0]))
        except Exception:
            return 0
