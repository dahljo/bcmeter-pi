#!/usr/bin/env python3
"""bcMeter v2.0 — Single-process entry point.

Runs the measurement engine, pump controller, network manager,
and FastAPI REST API all in one process.

Usage:
    sudo python3 main.py
    # or via systemd:
    # sudo uvicorn api.app:app --host 0.0.0.0 --port 80
"""

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

# Ensure the project directory is in sys.path
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

BASE_DIR = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"

# ── Logging setup ──────────────────────────────────────────
LOG_DIR = os.path.join(BASE_DIR, "maintenance_logs")
os.makedirs(LOG_DIR, exist_ok=True)

_log_file = os.path.join(LOG_DIR, "bcmeter.log")
_file_handler = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=0, backupCount=9,  # maxBytes=0: only rotate manually
)
# Rotate on every startup so each session gets its own log file
if os.path.exists(_log_file) and os.path.getsize(_log_file) > 0:
    _file_handler.doRollover()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[_file_handler, logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bcmeter.main")

# ── Imports ────────────────────────────────────────────────
from bcmeter import __version__
from bcmeter.config import CfgStore
from bcmeter.state import state
from bcmeter.errors import ErrorCode, InitStep
from bcmeter.adc import ADC
from bcmeter.optics import Optics, StatusLed
from bcmeter.pump import Pump
from bcmeter.sht4x import SHT4x
from bcmeter.ds18b20 import DS18B20
from bcmeter.sps30 import SPS30
from bcmeter.storage import Storage, was_session_running
from bcmeter.measure import MeasureEngine
from bcmeter.wifimgr import NetworkManager
from bcmeter.gps import GPS
from bcmeter.bme280 import BME280
from bcmeter.email_handler import init_sender as init_email_sender
from bcmeter.email_handler import init_periodic_loop as init_email_periodic_loop
from bcmeter.email_handler import get_configured_api_key
from bcmeter import incident_log, geoloc, qnh, timesync, ota_check
from bcmeter import avahi_alias
from bcmeter.identity import sync_system_hostname

# ── Global stop event ─────────────────────────────────────
stop_event = threading.Event()


def signal_handler(sig, frame):
    logger.info(f"Signal {sig} received, shutting down...")
    stop_event.set()


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def _check_expand_fs():
    """Expand root partition if >10% of the disk is unallocated."""
    import subprocess as _sp
    try:
        # Find root partition and parent disk
        root_part = _sp.run(["findmnt", "-n", "-o", "SOURCE", "/"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        if not root_part:
            return
        disk_name = _sp.run(["lsblk", "-n", "-o", "PKNAME", root_part],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        if not disk_name:
            return
        part_base = root_part.rsplit("/", 1)[-1]  # e.g. mmcblk0p2

        # Read sector counts
        disk_sectors = int(_sp.run(["blockdev", "--getsz", f"/dev/{disk_name}"],
                                   capture_output=True, text=True, timeout=5).stdout.strip())
        part_start = int(open(f"/sys/class/block/{part_base}/start").read().strip())
        part_size = int(open(f"/sys/class/block/{part_base}/size").read().strip())
        unalloc = disk_sectors - (part_start + part_size)

        if unalloc <= 0 or (unalloc / disk_sectors) < 0.10:
            return  # less than 10% unallocated — not worth expanding

        unalloc_mb = unalloc * 512 // (1024 * 1024)
        logger.info("%.0f MB unallocated (%.0f%%) — expanding filesystem",
                     unalloc_mb, (unalloc / disk_sectors) * 100)
        incident_log.add("info", "Expanding root filesystem (%d MB free), rebooting", unalloc_mb)

        r = _sp.run(["sudo", "raspi-config", "nonint", "do_expand_rootfs"],
                     capture_output=True, timeout=30)
        if r.returncode == 0:
            logger.info("Filesystem expansion scheduled, rebooting now")
            _sp.run(["sudo", "reboot"], timeout=10)
            time.sleep(30)  # wait for reboot to take effect
    except FileNotFoundError:
        logger.debug("raspi-config or tools not available, skipping expand check")
    except Exception as e:
        logger.warning("Filesystem expand check failed: %s", e)


def main():
    logger.info(f"bcMeter v{__version__} starting on {os.uname().nodename}")
    incident_log.add("info", "bcMeter v%s starting", __version__)
    _check_expand_fs()

    # ── Load config ────────────────────────────────────────
    cfg_path = os.path.join(BASE_DIR, "bcMeter_config.json")
    cfg = CfgStore(cfg_path)
    logger.info(f"Config loaded from {cfg_path}")

    # ── Sync hostname to device_name ───────────────────────
    # device_name is derived from WiFi MAC on first boot (see config.py).
    # Restart avahi if the hostname changes so the unique .local name is
    # visible on the first boot, not only after a second reboot.
    try:
        sync_system_hostname(cfg.get_string("device_name", "bcMeter"),
                             reason="first-boot hostname sync")
    except Exception as e:
        logger.warning("Could not sync hostname: %s", e)

    # ── Start & connect pigpiod ───────────────────────────
    pi = None
    try:
        import pigpio
        import subprocess as _sp

        # Try connecting first (systemd may have started pigpiod already)
        pi = pigpio.pi('localhost', 8888)
        if not pi.connected:
            # Not running — kill stale instance, start fresh
            logger.info("pigpiod not running, starting...")
            _sp.run(["sudo", "killall", "pigpiod"], capture_output=True, timeout=3)
            time.sleep(1)
            _sp.run(["sudo", "pigpiod", "-l", "-m", "-x", "-1"], capture_output=True, timeout=5)
            time.sleep(2)

            for _attempt in range(5):
                pi = pigpio.pi('localhost', 8888)
                if pi.connected:
                    break
                logger.warning("pigpiod connect attempt %d/5 failed, retrying...", _attempt + 1)
                time.sleep(2)

        if pi is None or not pi.connected:
            logger.error("Failed to connect to pigpiod after 5 attempts")
            pi = None
        else:
            logger.info("pigpiod connected")
    except ImportError:
        logger.warning("pigpio not installed, hardware control unavailable")
    except Exception as e:
        logger.error(f"pigpio connection failed: {e}")

    # ── Initialize hardware ────────────────────────────────
    i2c_lock = threading.Lock()

    # ADC
    adc = ADC(i2c_lock)
    swap_ch = cfg.get_bool("swap_channels", False)
    if adc.detect(swap_channels=swap_ch,
                  spi_vref=cfg.get_float("spi_vref", 4.096)):
        state.update(adc_present=True, adc_type=adc.type)
        logger.info(f"ADC detected: {adc.type}")
        incident_log.add("ok", "ADC detected: %s", adc.type)
    else:
        logger.error("No ADC detected — measurement unavailable")
        incident_log.add("error", "No ADC detected")

    # Optics
    optics = Optics(pi)
    if pi:
        optics.init(pi)

    # Status LED
    status_led = StatusLed()
    if pi:
        status_led.init(pi)

    # Pump
    pump = Pump(config=cfg, adc=adc)
    if pi:
        pump.init(pi)

    # Temperature sensors
    sht = SHT4x(i2c_lock=i2c_lock)
    if sht.init():
        state.set("sht4x_present", True)
        incident_log.add("ok", "SHT4x detected")
    else:
        state.set("sht4x_present", False)
    ds = DS18B20()
    ds.init()

    # SPS30 particulate matter sensor
    sps = SPS30(i2c_lock=i2c_lock)
    if sps.init():
        state.set("sps30_present", True)
        logger.info("SPS30 detected")
        incident_log.add("ok", "SPS30 detected")

    # BME280 environmental sensor (optional)
    bme = BME280(i2c_lock=i2c_lock)
    if bme.init():
        state.set("bme280_present", True)
        logger.info("BME280 detected")
        incident_log.add("ok", "BME280 detected")

    # GPS (optional)
    gps = GPS()
    if gps.init():
        state.set("gps_present", True)
        logger.info("GPS detected")
        incident_log.add("ok", "GPS detected")

    sensors = {"sht": sht, "ds": ds, "sps": sps, "bme": bme}

    # Boot preflight: quick filter read for immediate UI display
    if adc.present and pi:
        duty_0 = cfg.get_int("led_duty_cycle_880nm", 255)
        optics.set_led_duty(0, max(0, min(255, duty_0)))
        cal_k_0 = cfg.get_float("cal_k_880nm", 1.0)
        if cal_k_0 < 0.1 or cal_k_0 > 10.0:
            cal_k_0 = 1.0
        optics.led_on(0)
        time.sleep(0.3)
        sen = adc.read_sensor() * cal_k_0
        ref = adc.read_reference()
        optics.led_off(0)
        state.update(last_sen=sen, last_ref=ref)
        loading = (100.0 - (sen / ref * 100.0)) if ref > 0 else 100.0
        logger.info("Boot filter: %.0f%% (sen=%.4f ref=%.4f)", loading, sen, ref)

    # Storage
    storage = Storage(
        log_dir=os.path.join(BASE_DIR, "logs"),
        log_pump_duty=cfg.get_bool("log_pump_duty", False),
    )

    # Measurement engine
    engine = MeasureEngine(
        cfg=cfg, adc=adc, optics=optics, pump=pump,
        sensors=sensors, storage=storage, gps=gps,
    )

    # Network manager
    network_mgr = NetworkManager(cfg, base_dir=BASE_DIR)

    # Email sender + ESP32-parity periodic loop outside the measure thread
    init_email_sender()
    init_email_periodic_loop(
        stop_event,
        session_active_fn=lambda: storage.session_active,
        measure_stats_fn=lambda: {
            "bc_session_avg": engine.get_session_avg_bc(),
            "bc_hour_avg": engine.get_hour_avg_bc(),
            "loading_pct": engine.get_loading_pct(),
            "session_hours": engine.get_session_hours(),
            "initial_loading_pct": engine.get_initial_loading_pct(),
            "current_atn": engine.get_current_atn(),
            "initial_atn": engine.get_initial_atn(),
        },
    )

    # Time sync
    timesync.sync_ntp()
    # Give NTP and/or a user-browser synctime up to 60s before falling
    # back to unsynced sampling.  Synced-time sessions get proper
    # filenames and mod-5 alignment.
    if timesync.wait_for_valid(timeout_s=60):
        logger.info("System time is valid")
    else:
        logger.warning("System time may not be synced")

    # OTA update checker
    ota_check.init(stop_event)

    # Geolocation + QNH (background, after WiFi is likely up)
    def _boot_geoloc():
        geoloc.try_fetch(
            gps=gps,
            device_name=cfg.get_string("device_name", "bcMeter"),
            api_key=get_configured_api_key(cfg.to_flat_dict()),
        )
        qnh.fetch_if_needed()

    threading.Thread(target=_boot_geoloc, daemon=True, name="boot_geoloc").start()

    # mDNS alias: publish 'bcmeter.local' as CNAME for the MAC-based hostname
    avahi_alias.start_background(stop_event)

    # ── Wire up FastAPI dependencies ───────────────────────
    from api.app import app, set_dependencies
    set_dependencies(
        cfg=cfg, state_mgr=state, engine=engine, storage=storage,
        network_manager=network_mgr, gps=gps, status_led=status_led,
        pi=pi, adc=adc, optics=optics, pump=pump,
    )

    # ── Start background threads ───────────────────────────
    threads = []

    # Measurement engine thread
    t_measure = threading.Thread(
        target=engine.start, args=(stop_event,),
        name="measure", daemon=True
    )
    threads.append(t_measure)

    # Pump control thread
    t_pump = threading.Thread(
        target=pump.control_task, args=(stop_event, state),
        name="pump", daemon=True
    )
    threads.append(t_pump)

    # Network management thread
    if cfg.get_bool("enable_wifi", True):
        t_network = threading.Thread(
            target=network_mgr.task, args=(stop_event,),
            name="network", daemon=True
        )
        threads.append(t_network)

    # Status LED thread
    if not cfg.get_bool("disable_led", False) and pi:
        t_led = threading.Thread(
            target=status_led.task, args=(stop_event,),
            name="status_led", daemon=True
        )
        threads.append(t_led)

    # GPS thread
    if gps.present:
        t_gps = threading.Thread(
            target=gps.task, args=(stop_event,),
            name="gps", daemon=True
        )
        threads.append(t_gps)

    for t in threads:
        t.start()
        logger.info(f"Started thread: {t.name}")

    # ── Auto-start / resume after power loss ─────────────────
    debug_mode = state.get("debug_mode")
    resume_session = was_session_running()
    do_autostart = cfg.get_bool("autostart_logging", False)

    if debug_mode or resume_session or do_autostart:
        reason = "Power-loss recovery" if resume_session else "Autostart"
        cal_time = cfg.get_string("last_cal_time", "never")
        if cal_time == "never" and not debug_mode:
            logger.warning("%s skipped: no calibration found", reason)
            incident_log.add("warn", "%s skipped: no calibration", reason)
        else:
            if debug_mode:
                logger.info("=== DEBUG MODE: auto-starting measurement, Ctrl-C to stop ===")
            else:
                logger.info("%s: beginning measurement", reason)
                incident_log.add("info", "%s: measurement beginning", reason)
            state.sampling = True

    # ── Run uvicorn or block in debug mode ─────────────────
    if debug_mode:
        # Debug mode: no API server, just block until Ctrl-C
        logger.info("Debug mode: API server skipped, streaming to stdout")
        try:
            while not stop_event.is_set():
                stop_event.wait(1)
        except KeyboardInterrupt:
            pass
    else:
        import uvicorn

        logger.info("Starting API server on port 80")
        try:
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=80,
                log_level="warning",
                access_log=False,
            )
        except PermissionError:
            logger.warning("Port 80 requires root, trying port 8080")
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=8080,
                log_level="warning",
                access_log=False,
            )

    # ── Shutdown ───────────────────────────────────────
    logger.info("Shutting down...")
    stop_event.set()
    state.sampling = False

    # Turn off hardware
    if pi:
        optics.all_off()
        pump.shutdown()

    # Close hardware resources
    adc.close()
    if bme.present:
        bme.close()
    sht.close()
    if gps.present:
        gps.close()

    # Wait for threads to finish
    for t in threads:
        t.join(timeout=5)

    incident_log.add("info", "bcMeter shutdown complete")
    logger.info("bcMeter shutdown complete")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="bcMeter measurement daemon")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug mode: verbose ADC output, skip API server")
    args = parser.parse_args()
    if args.debug:
        state.set("debug_mode", True)
    main()
