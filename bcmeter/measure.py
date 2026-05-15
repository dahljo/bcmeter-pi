"""Core measurement loop for bcMeter Raspberry Pi.

Port of ESP32 measure.cpp combined with bcMeter.py:bcmeter_main().
Runs the optical Black Carbon measurement cycle: preflight checks,
LED priming, ADC sampling with sigma-reject, Kalman filtering,
flow monitoring, and CSV logging.
"""

import logging
import math
import threading
import time
from collections import deque
from datetime import datetime

from .state import state
from .errors import ErrorCode, InitStep
from .kalman import BCFilter, sigma_reject
from . import notes
from .optics import SIGMA, WAVELENGTH_NAMES
from .storage import MeasureRow, was_session_running
from . import email_handler
from . import incident_log
from . import qnh
from . import timesync

logger = logging.getLogger("bcmeter.measure")

# ---------------------------------------------------------------------------
# Constants (ported from ESP32 measure.cpp / config.h)
# ---------------------------------------------------------------------------
ADC_LOW_LIMIT_DEFAULT = 0.1
TEMP_LIMIT = 65.0
ATN_LIMIT = 120.0
REF_MIN_LED_ON = 0.05
FLOW_SAMPLE_INTERVAL_S = 0.25
OUTLIER_REJECT_PCT = 0.20
PRIME_DURATION_S = 2.0
LED_SETTLE_S = 0.2
SIGMA_REJECT_LIMIT = 3.0
SHADOW_FACTOR = 1.2
LED_DUTY_FLOOR_DEFAULT = 30
BC_HOUR_RING_SIZE = 360


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _calc_atn(sen: float, ref: float) -> float:
    """Calculate attenuation from sensor and reference voltages."""
    if ref <= 0 or sen <= 0:
        return 0.0
    return -100.0 * math.log(sen / ref)


def _loading_corr(atn: float, f: float = SHADOW_FACTOR) -> float:
    """Weingartner loading correction.

    Returns correction factor (always >= 0.1).
    """
    if atn <= 10.0:
        return 1.0
    term1 = (1.0 / f) - 1.0
    term2 = (math.log(atn) - math.log(10.0)) / (math.log(50.0) - math.log(10.0))
    r = (term1 * term2) + 1.0
    return max(r, 0.1)


def _filter_status_from_atn(atn: float) -> int:
    """Derive filter status 1-5 from ATN (Weingartner validity stages).

    5 = optimal (ATN ≤ 75, fully quantitative)
    4 = good (75-100, Weingartner reliable, ±10 %)
    3 = extrapolated (100-130, ±25 %, mail trigger)
    2 = qualitative (130-160, ±50 %)
    1 = potentially unreliable (> 160)
    """
    if atn > 160.0:
        return 1
    if atn > 130.0:
        return 2
    if atn > 100.0:
        return 3
    if atn > 75.0:
        return 4
    return 5


def _timestamp_pair() -> tuple:
    """Return (date_str, time_str) for CSV row."""
    now = datetime.now()
    return now.strftime("%d-%m-%y"), now.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Session statistics (ring buffer for hourly average)
# ---------------------------------------------------------------------------

class _BCStats:
    """Thread-safe session and hourly BC statistics."""

    def __init__(self):
        self._lock = threading.Lock()
        self._session_sum = 0.0
        self._session_count = 0
        self._ring_bc = deque(maxlen=BC_HOUR_RING_SIZE)
        self._ring_time = deque(maxlen=BC_HOUR_RING_SIZE)

    def reset(self):
        with self._lock:
            self._session_sum = 0.0
            self._session_count = 0
            self._ring_bc.clear()
            self._ring_time.clear()

    def push(self, bc: float):
        with self._lock:
            self._session_sum += bc
            self._session_count += 1
            self._ring_bc.append(bc)
            self._ring_time.append(time.time())

    def session_avg(self) -> float:
        with self._lock:
            if self._session_count == 0:
                return 0.0
            return self._session_sum / self._session_count

    def hour_avg(self) -> float:
        with self._lock:
            if not self._ring_bc:
                return 0.0
            cutoff = time.time() - 3600.0
            total = 0.0
            count = 0
            for bc_val, ts in zip(self._ring_bc, self._ring_time):
                if ts >= cutoff:
                    total += bc_val
                    count += 1
            return total / count if count > 0 else 0.0


# ---------------------------------------------------------------------------
# MeasureEngine
# ---------------------------------------------------------------------------

class MeasureEngine:
    """Core measurement engine.

    Encapsulates the entire measurement lifecycle: preflight checks,
    LED priming, ADC sampling, BC calculation, Kalman filtering,
    flow monitoring, and CSV output.
    """

    def __init__(self, cfg, adc, optics, pump, sensors, storage, gps=None):
        """
        Args:
            cfg: CfgStore configuration instance.
            adc: ADC hardware interface.
            optics: Optics LED controller.
            pump: Pump airflow controller.
            sensors: dict with keys 'sht' (SHT4x), 'ds' (DS18B20), 'sps' (SPS30).
            storage: Storage session manager.
            gps: GPS instance (optional).
        """
        self._cfg = cfg
        self._adc = adc
        self._optics = optics
        self._pump = pump
        self._sht = sensors.get("sht")
        self._ds = sensors.get("ds")
        self._sps = sensors.get("sps")
        self._bme = sensors.get("bme")
        self._storage = storage
        self._gps = gps

        # ADC voltage limits (derived from hardware Vref)
        self._adc_high_limit = adc.high_limit if adc.present else 3.8

        # Per-channel state
        self._last_atn = [0.0, 0.0, 0.0]
        self._cal_k = [1.0, 1.0, 1.0]
        self._bc_filters = [BCFilter(), BCFilter(), BCFilter()]
        self._bc_stats = _BCStats()

        # Spot area computed from config
        self._spot_area = 0.0

        # Session timing
        self._session_start_time = 0.0
        self._initial_loading_pct = -1.0
        self._initial_atn = -1.0
        self._zero_flow_cycles = 0

        # Indoor-sampling state (see measure.h for the mirrored ESP32 API).
        # Populated from config default OR from the one-shot override set
        # by /api/control?action=start&indoor=1 (which survives retries).
        self._session_indoor = False
        self._next_session_indoor_override = False
        self._session_row_count = 0

    def set_next_session_indoor(self, on: bool):
        """One-shot override consumed on the next session start."""
        self._next_session_indoor_override = bool(on)

    def is_session_indoor(self) -> bool:
        return self._session_indoor

    # ------------------------------------------------------------------
    # Public getters (parity with ESP32 Measure namespace)
    # ------------------------------------------------------------------

    def get_loading_pct(self) -> float:
        """Current filter loading as percentage (0-100)."""
        ref = state.get("last_ref")
        sen = state.get("last_sen")
        if ref <= 0:
            return 0.0
        pct = (1.0 - sen / ref) * 100.0
        return max(0.0, min(100.0, pct))

    def get_initial_loading_pct(self) -> float:
        """Filter loading % captured at start of session (for delta rate calc)."""
        return self._initial_loading_pct if self._initial_loading_pct >= 0 else 0.0

    def get_current_atn(self) -> float:
        """Current 880 nm ATN."""
        return self._last_atn[0]

    def get_initial_atn(self) -> float:
        """880 nm ATN captured at start of session."""
        return self._initial_atn if self._initial_atn >= 0 else 0.0

    def get_session_hours(self) -> float:
        """Hours since current session started."""
        if self._session_start_time <= 0:
            return 0.0
        return (time.time() - self._session_start_time) / 3600.0

    def get_session_avg_bc(self) -> float:
        """Average BC concentration over the current session."""
        return self._bc_stats.session_avg()

    def get_hour_avg_bc(self) -> float:
        """Average BC concentration over the last hour."""
        return self._bc_stats.hour_avg()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def start(self, stop_event: threading.Event):
        """Main measurement loop. Intended to run in its own thread.

        Blocks until stop_event is set or an unrecoverable error occurs.
        The loop waits for ``state.sampling`` to become True, then
        performs preflight checks, priming, and continuous measurement
        cycles.
        """
        logger.info("Measure engine thread started")
        self._init_constants()

        while not stop_event.is_set():
            # ---- Wait for sampling flag ----
            last_idle_filter_check = 0.0
            while not state.sampling:
                state.set("init_step", InitStep.INIT_IDLE)
                # Periodic idle filter check every 30 s — brief LED burst
                # to keep last_sen/last_ref current for the UI filter indicator
                now = time.time()
                if self._adc.present and (now - last_idle_filter_check >= 30.0):
                    last_idle_filter_check = now
                    try:
                        duty_0 = self._cfg.get_int("led_duty_cycle_880nm", 255)
                        self._optics.set_led_duty(0, max(0, min(255, duty_0)))
                        cal_k_0 = self._cfg.get_float("cal_k_880nm", 1.0)
                        if cal_k_0 < 0.1 or cal_k_0 > 10.0:
                            cal_k_0 = 1.0
                        self._optics.led_on(0)
                        time.sleep(0.2)
                        sen = self._adc.read_sensor() * cal_k_0
                        ref = self._adc.read_reference()
                        self._optics.led_off(0)
                        state.update(last_sen=sen, last_ref=ref)
                    except Exception:
                        logger.debug("Idle filter check failed", exc_info=True)
                if stop_event.wait(0.2):
                    logger.info("Measure engine thread exiting (stop event)")
                    return

            # ---- Begin a measurement session ----
            try:
                self._run_session(stop_event)
            except Exception:
                logger.exception("Unhandled exception in measurement session")
            finally:
                self._optics.all_off()
                if self._storage.session_active:
                    self._storage.end_session()
                state.set("init_step", InitStep.INIT_IDLE)
                # Do not force sampling=False here if error already cleared it
                if state.get("error") != ErrorCode.ERR_NONE:
                    logger.warning(
                        "Session ended with error: %s",
                        ErrorCode(state.get("error")).name,
                    )

        logger.info("Measure engine thread exiting")

    def calibrate(self, log_fn=None):
        """Run calibration routine with a clean filter.

        For each active channel, finds the optimal LED duty cycle and
        computes the calibration factor K = ref / sen.

        Args:
            log_fn: Optional callable(str) for streaming calibration log lines.
        """

        def _log(msg):
            logger.info(msg)
            if log_fn:
                try:
                    log_fn(msg + "\n")
                except Exception:
                    pass

        num_ch = max(1, min(3, self._cfg.get_int("num_channels", 1)))
        _log("[Cal] Starting calibration (clean filter required)")

        for ch in range(num_ch):
            wl_name = WAVELENGTH_NAMES[ch]
            duty_key = f"led_duty_cycle_{wl_name}"
            duty = self._cfg.get_int(duty_key, 128)

            # Phase 1: find highest duty that keeps ADC in-bounds
            for _ in range(20):
                self._optics.set_led_duty(ch, duty)
                self._optics.led_on(ch)
                time.sleep(0.1)
                if self._adc.type == "spi":
                    s, r = self._adc.read_interleaved(duration_s=1.0)
                else:
                    time.sleep(0.2)
                    s = self._adc.read_sensor()
                    r = self._adc.read_reference()
                self._optics.led_off(ch)
                _log(f"[Cal] CH{ch} duty={duty} sen={s:.3f} ref={r:.3f}")

                if s >= self._adc_high_limit or r >= self._adc_high_limit:
                    duty = max(duty - 20, 0)
                else:
                    next_duty = min(duty + 10, 255)
                    self._optics.set_led_duty(ch, next_duty)
                    self._optics.led_on(ch)
                    time.sleep(0.1)
                    if self._adc.type == "spi":
                        s2, r2 = self._adc.read_interleaved(duration_s=1.0)
                    else:
                        time.sleep(0.2)
                        s2 = self._adc.read_sensor()
                        r2 = self._adc.read_reference()
                    self._optics.led_off(ch)
                    if s2 >= self._adc_high_limit or r2 >= self._adc_high_limit:
                        break
                    duty = next_duty
                    if duty >= 255:
                        break

            self._cfg.set_int(duty_key, duty)

            # Phase 2: average sen/ref over 10 seconds to compute K
            self._optics.set_led_duty(ch, duty)
            self._optics.led_on(ch)
            time.sleep(0.5)
            if self._adc.type == "spi":
                avg_sen, avg_ref = self._adc.read_interleaved(duration_s=10.0)
                count = 10
            else:
                sen_sum = 0.0
                ref_sum = 0.0
                count = 0
                end_t = time.time() + 10.0
                while time.time() < end_t:
                    sen_sum += self._adc.read_sensor()
                    ref_sum += self._adc.read_reference()
                    count += 1
                    time.sleep(0.001)
                avg_sen = sen_sum / count if count else 0.0
                avg_ref = ref_sum / count if count else 0.0

            self._optics.led_off(ch)

            if count < 10:
                _log(f"[Cal] CH{ch} FAILED: only {count} reads")
                return False
            if avg_sen < 0.1 or avg_ref < 0.1:
                _log(f"[Cal] CH{ch} FAILED: sen={avg_sen:.4f} ref={avg_ref:.4f}")
                return False

            k = avg_ref / avg_sen
            self._cal_k[ch] = k
            cal_key = f"cal_k_{wl_name}"
            self._cfg.set_float(cal_key, k)
            _log(f"[Cal] CH{ch} K={k:.6f} (sen={avg_sen:.4f} ref={avg_ref:.4f})")
            time.sleep(0.3)

        # Store timestamp
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._cfg.set_string("last_cal_time", ts)
        self._cfg.save()
        state.set("last_cal_time", ts)
        _log("[Cal] Calibration complete")
        return True

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _init_constants(self):
        """Load calibration constants and LED duties from config."""
        for i, wl in enumerate(WAVELENGTH_NAMES):
            k = self._cfg.get_float(f"cal_k_{wl}", 1.0)
            if k < 0.1 or k > 10.0:
                k = 1.0
            self._cal_k[i] = k

            duty = self._cfg.get_int(f"led_duty_cycle_{wl}", 255)
            self._optics.set_led_duty(i, max(0, min(255, duty)))

        spot_d = self._cfg.get_float("sample_spot_diameter", 0.4)
        self._spot_area = math.pi * (spot_d / 2.0) ** 2

    def _run_session(self, stop_event: threading.Event):
        """Execute one complete measurement session."""

        # ---- Preflight ----
        state.set("init_step", InitStep.INIT_PREFLIGHT)
        error = self._preflight_check()
        if error != ErrorCode.ERR_NONE:
            logger.error("Preflight FAILED: %s", error.name)
            self._set_error(error)
            return

        state.set("error", ErrorCode.ERR_NONE)
        logger.info("Sampling session started")
        incident_log.add("info", "Measurement session started")

        # ---- Config snapshot ----
        num_ch = max(1, min(3, self._cfg.get_int("num_channels", 1)))
        is_ebc = self._cfg.get_bool("is_ebcMeter", False)
        filter_mode = self._cfg.get_string("bc_filter", "median3")
        for i in range(3):
            self._bc_filters[i].reset(filter_mode)
        self._bc_stats.reset()
        self._init_constants()

        bc_ever_positive = False
        bc_neg_alert_sent = False
        self._first_cycle = True  # skip BC calc on first cycle (priming ATN ≠ sample ATN)
        session_start_time = time.time()
        self._session_start_time = session_start_time
        self._initial_loading_pct = -1.0  # captured on first sample
        self._initial_atn = -1.0

        # Resolve the session's indoor flag: config default, unless the
        # one-shot override is set. Consume the override so it only applies
        # to the session we're starting.
        self._session_indoor = (
            not self._cfg.get_bool("outdoor_measurement", True)
            or self._next_session_indoor_override
        )
        self._next_session_indoor_override = False
        self._session_row_count = 0

        email_handler.reset_team_offset()
        email_handler.reset_log_mail_offset()
        email_handler.reset_periodic_timers()

        # Capture the crash-resume hint BEFORE start_session rewrites the
        # persistent session flag; used below to skip mod-5 alignment so
        # an already-interrupted run isn't delayed again.
        is_resume = was_session_running()

        # ---- Start storage session ----
        sps_present = self._sps is not None and self._sps.present
        try:
            session_file = self._storage.start_session()
        except Exception:
            logger.exception("Storage session start failed")
            self._set_error(ErrorCode.ERR_NONE)
            state.sampling = False
            return

        email_handler.set_session_start()
        email_handler.send_session_start(session_file)

        # ---- Prime channels ----
        for ch in range(num_ch):
            step = [InitStep.INIT_PRIME_CH0, InitStep.INIT_PRIME_CH1, InitStep.INIT_PRIME_CH2][ch]
            state.set("init_step", step)
            self._prime_channel(ch)
            time.sleep(0.3)
            if not state.sampling or stop_event.is_set():
                return

        # ---- Settling / warmup ----
        state.set("init_step", InitStep.INIT_SETTLING)
        warmup_sec = self._cfg.get_int("warmup_seconds", 600)
        self._warmup_end = time.time() + warmup_sec
        logger.info("Warmup: %d seconds", warmup_sec)

        # ---- Mod-5 minute alignment ----
        # Align first sample to the next wall-clock instant where
        # (minute % 5 == 0 && second == 0).  Adds up to ~5 min of delay.
        # Skipped when time is not synced (mod-5 is meaningless) or on
        # crash resume (avoid further delaying an already-interrupted run).
        if is_resume:
            logger.info("Crash resume: skipping mod-5 alignment")
        elif not timesync.is_valid():
            logger.info("Time not synced: skipping mod-5 alignment")
        else:
            now_dt = datetime.now()
            mins_to_boundary = (5 - (now_dt.minute % 5)) % 5
            wait_sec = mins_to_boundary * 60 - now_dt.second
            if wait_sec <= 0:
                wait_sec += 300  # already past -> next 5-min boundary
            logger.info(
                "Aligning to mod-5 boundary: wait %ds (now %02d:%02d:%02d)",
                wait_sec, now_dt.hour, now_dt.minute, now_dt.second,
            )
            align_deadline = time.monotonic() + wait_sec
            while state.sampling and not stop_event.is_set():
                remaining = align_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.1, remaining))

        if not state.sampling or stop_event.is_set():
            return

        # ---- Main sample loop ----
        # Cadence tracking: each cycle's START is exactly sample_time seconds
        # after the previous cycle's START (monotonic so NTP steps don't
        # perturb the schedule).  The first cycle starts at alignment-end
        # (= now).  Only skip slots when a full sample window was actually
        # missed; small upload/email overruns should not create 5-minute gaps.
        next_cycle_start = time.monotonic()
        first_cadence_cycle = True
        prev_cycle_start_monotonic = next_cycle_start  # BC integration interval tracker

        while state.sampling and not stop_event.is_set():
            sample_time_sec = self._cfg.get_int("sample_time", 300)

            # Wait until this cycle's scheduled start.  On the first
            # cycle we start immediately; on subsequent cycles we block
            # until the deadline, or skip past only fully missed slots.
            if not first_cadence_cycle:
                now_mono = time.monotonic()
                delta = next_cycle_start - now_mono
                if delta > 0:
                    if stop_event.wait(delta):
                        return
                    if not state.sampling:
                        break
                else:
                    slot = max(1.0, float(sample_time_sec))
                    skip = int((-delta) // slot)
                    if skip > 0:
                        next_cycle_start += skip * slot
                        logger.warning(
                            "Cycle overrun by %.1fs - skipping %d full slot(s) to catch up",
                            -delta, skip,
                        )
            first_cadence_cycle = False
            # Advance to next cycle's scheduled start.
            next_cycle_start += float(sample_time_sec)

            # Discard any stale notes accumulated during aborted or warmup
            # cycles so they don't leak into this row.
            notes.drain()
            if state.get("time_just_synced"):
                state.set("time_just_synced", False)
                notes.add(notes.TIME_SYNC)

            # Capture cycle-start monotonic time; the BC integration uses
            # the inter-cycle interval (not work time) as duration.
            cycle_start_monotonic = time.monotonic()
            interval_s = cycle_start_monotonic - prev_cycle_start_monotonic
            prev_cycle_start_monotonic = cycle_start_monotonic

            try:
                row, bc_arr, aborted = self._sample_cycle(
                    num_ch, is_ebc, stop_event, interval_s,
                )
            except Exception:
                logger.exception("Exception during sample cycle")
                continue

            if not state.sampling:
                break

            if aborted:
                logger.warning("Cycle aborted, skipping row")
                continue

            # ---- Session statistics ----
            primary_bc = bc_arr[0] if bc_arr else 0.0
            self._bc_stats.push(primary_bc)
            state.update(
                last_bc=primary_bc,
                last_atn=self._last_atn[0],
                session_avg_bc=self._bc_stats.session_avg(),
                hour_avg_bc=self._bc_stats.hour_avg(),
            )

            # Negative BC alert
            if primary_bc > 0:
                bc_ever_positive = True
            if (
                not bc_ever_positive
                and not bc_neg_alert_sent
                and (time.time() - session_start_time) > 3600
            ):
                bc_neg_alert_sent = True
                notes.add("BC_NEG")
                incident_log.add("warn", "Session BC negative for >1h — low signal or clean air")
                try:
                    email_handler.send_negative_bc_alert(
                        self._bc_stats.session_avg(),
                        self._bc_stats.hour_avg(),
                    )
                except Exception:
                    logger.debug("Failed to send negative BC alert email")

            # ---- Write CSV row (if past warmup) ----
            if time.time() >= self._warmup_end:
                try:
                    # First row of the session carries session-level markers
                    # in the `notice` column. bc_archive_ingest reads this
                    # and excludes matching sessions from the public map.
                    if self._session_row_count == 0:
                        mobile = self._cfg.get_bool("mobile_sampling", False)
                        markers = []
                        if self._session_indoor:
                            markers.append("INDOOR")
                        if mobile:
                            markers.append("MOBILE")
                        if markers:
                            row.notice = " ".join(markers)
                    self._session_row_count += 1
                    self._storage.append_row(row)
                except Exception:
                    logger.exception("Failed to write CSV row")
            else:
                remaining = int(self._warmup_end - time.time())
                logger.debug("Warmup: %ds remaining", remaining)

        # ---- Session teardown ----
        self._optics.all_off()
        if self._storage.session_active:
            self._storage.end_session()
        state.sampling = False
        state.set("init_step", InitStep.INIT_IDLE)
        incident_log.add("info", "Measurement session ended")
        logger.info("Sampling session ended")

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def _preflight_check(self) -> ErrorCode:
        """Verify ADC, LEDs, and temperature before sampling."""
        num_ch = max(1, min(3, self._cfg.get_int("num_channels", 1)))
        adc_low_limit = self._cfg.get_float("adc_low_limit", ADC_LOW_LIMIT_DEFAULT)
        debug_mode = state.get("debug_mode")
        logger.info("Preflight check (%d channels, debug=%s, adc_low=%.2fV)", num_ch, debug_mode, adc_low_limit)

        if not debug_mode:
            for ch in range(num_ch):
                self._optics.led_on(ch)
                time.sleep(0.3)
                sen_raw = self._adc.read_sensor()
                ref = self._adc.read_reference()
                self._optics.led_off(ch)

                sen = sen_raw * self._cal_k[ch]

                # Attempt LED duty recovery if saturated
                if sen > self._adc_high_limit or ref > self._adc_high_limit:
                    logger.warning(
                        "Preflight: CH%d saturated (sen=%.3f ref=%.3f) — attempting recovery",
                        ch, sen, ref,
                    )
                    if self._recover_led_duty(ch):
                        # Re-test with lowered duty
                        self._optics.led_on(ch)
                        time.sleep(0.3)
                        sen_raw = self._adc.read_sensor()
                        ref = self._adc.read_reference()
                        self._optics.led_off(ch)
                        sen = sen_raw * self._cal_k[ch]
                        logger.info(
                            "Preflight: CH%d after recovery: sen=%.3f ref=%.3f",
                            ch, sen, ref,
                        )
                    else:
                        logger.error("Preflight: CH%d LED recovery failed", ch)
                        return ErrorCode.ERR_ADC_SATURATED

                if sen < adc_low_limit:
                    logger.warning("Preflight: sen_cal CH%d = %.3f < %.2f — filter loaded, continuing", ch, sen, adc_low_limit)
                    state.set("warning_msg", "Filter heavily loaded — consider replacing")
                    incident_log.add("warn", "Preflight: sen CH%d = %.3fV < %.2fV — filter loaded", ch, sen, adc_low_limit)
                if sen > self._adc_high_limit:
                    logger.error("Preflight: sen_cal CH%d = %.3f > %.1f", ch, sen, self._adc_high_limit)
                    return ErrorCode.ERR_ADC_SATURATED
                if ref < adc_low_limit:
                    logger.warning("Preflight: ref CH%d = %.3f < %.2f — filter loaded, continuing", ch, ref, adc_low_limit)
                    state.set("warning_msg", "Filter heavily loaded — consider replacing")
                    incident_log.add("warn", "Preflight: ref CH%d = %.3fV < %.2fV — filter loaded", ch, ref, adc_low_limit)
                if ref > self._adc_high_limit:
                    logger.error("Preflight: ref CH%d = %.3f > %.1f", ch, ref, self._adc_high_limit)
                    return ErrorCode.ERR_ADC_SATURATED
                if ref < REF_MIN_LED_ON:
                    logger.error("Preflight: ref CH%d = %.3f < %.2f (LED failure?)", ch, ref, REF_MIN_LED_ON)
                    return ErrorCode.ERR_LED_FAILURE

        # Temperature check
        temp = self._read_temperature()
        if temp is not None and temp > TEMP_LIMIT:
            logger.error("Preflight: temp=%.1f > %.1f", temp, TEMP_LIMIT)
            incident_log.add("error", "Preflight overtemp: %.1f > %.0f", temp, TEMP_LIMIT)
            try:
                email_handler.send_temperature_alert(temp)
            except Exception:
                pass
            return ErrorCode.ERR_OVERTEMP

        # 5-second averaged read for initial filter display in UI
        if not debug_mode:
            self._optics.led_on(0)
            time.sleep(0.3)
            sen_sum = 0.0
            ref_sum = 0.0
            count = 0
            end_time = time.monotonic() + 5.0
            while time.monotonic() < end_time:
                sen_sum += self._adc.read_sensor() * self._cal_k[0]
                ref_sum += self._adc.read_reference()
                count += 1
            self._optics.led_off(0)
            if count > 0:
                avg_sen = sen_sum / count
                avg_ref = ref_sum / count
                state.update(last_sen=avg_sen, last_ref=avg_ref)
                pct = (avg_sen / avg_ref * 100.0) if avg_ref > 0 else 0
                logger.info(
                    "Preflight filter: %.0f%% (sen=%.4f ref=%.4f, %d reads)",
                    pct, avg_sen, avg_ref, count,
                )

        logger.info("Preflight OK (temp=%.1f)", temp if temp is not None else 0.0)
        return ErrorCode.ERR_NONE

    # ------------------------------------------------------------------
    # Priming
    # ------------------------------------------------------------------

    def _prime_channel(self, ch: int):
        """Prime a single LED channel to stabilize and record initial ATN."""
        logger.info("Priming CH%d (%s)...", ch, WAVELENGTH_NAMES[ch])
        self._optics.led_on(ch)
        time.sleep(0.2)

        sen_sum = 0.0
        ref_sum = 0.0
        count = 0
        end_t = time.time() + PRIME_DURATION_S
        while time.time() < end_t:
            try:
                s = self._adc.read_sensor() * self._cal_k[ch]
                r = self._adc.read_reference()
                sen_sum += s
                ref_sum += r
                count += 1
            except Exception:
                logger.debug("ADC read error during priming CH%d", ch)

        self._optics.led_off(ch)

        if count == 0:
            logger.warning("Priming CH%d: no ADC reads collected", ch)
            return

        avg_sen = sen_sum / count
        avg_ref = ref_sum / count
        self._last_atn[ch] = _calc_atn(avg_sen, avg_ref)
        logger.info(
            "CH%d primed: sen_cal=%.4f ref=%.4f ATN=%.2f (%d reads)",
            ch, avg_sen, avg_ref, self._last_atn[ch], count,
        )

    # ------------------------------------------------------------------
    # Sample cycle
    # ------------------------------------------------------------------

    def _sample_cycle(
        self, num_ch, is_ebc, stop_event, interval_s=0.0,
    ):
        """Execute one measurement cycle across all channels.

        Args:
            interval_s: Monotonic seconds since the previous cycle start,
                used as the BC integration duration (0.0 on the very first
                cycle, which is fine because ``_first_cycle`` skips BC).

        Returns (MeasureRow, bc_array, aborted).
        """
        row = MeasureRow()
        row.date, row.time_str = _timestamp_pair()

        sample_time_sec = self._cfg.get_int("sample_time", 300)
        adc_low_limit = self._cfg.get_float("adc_low_limit", ADC_LOW_LIMIT_DEFAULT)
        filter_scatter = self._cfg.get_float("filter_scattering_factor", 1.39)
        shadow_factor = self._cfg.get_float("shadow_factor", SHADOW_FACTOR)
        if shadow_factor < 1.05: shadow_factor = 1.05  # sanity guard
        if shadow_factor > 2.5:  shadow_factor = 2.5
        correction_factor = self._cfg.get_float("device_specific_correction_factor", 1.0)
        ambient_pressure_correction = self._cfg.get_bool("ambient_pressure_correction", True)

        cycle_start = time.time()
        ch_duration = sample_time_sec / num_ch

        # --- Read environmental sensors (pre-cycle) ---
        env_temp, env_hum, env_pressure = self._read_env()
        if env_temp is not None and env_temp > TEMP_LIMIT:
            self._set_error(ErrorCode.ERR_OVERTEMP)
            return row, [], True

        # --- Per-channel ADC sampling ---
        sen_avg = [0.0] * num_ch
        ref_avg = [0.0] * num_ch
        sample_counts = [0] * num_ch
        raw_counts = [0] * num_ch
        adc_high_total = 0
        adc_low_total = 0
        ch_high_counts = [0] * num_ch

        sen_bufs = [[] for _ in range(num_ch)]
        ref_bufs = [[] for _ in range(num_ch)]

        flow_sum = 0.0
        flow_count = 0
        target_flow = self._cfg.get_float("airflow_per_minute", 0.25)
        airflow_sensor_present = self._cfg.get_bool("airflow_sensor", True)
        next_flow_sample_time = cycle_start + FLOW_SAMPLE_INTERVAL_S
        aborted = False
        debug_mode = state.get("debug_mode")
        _dbg_interval = 50  # log every Nth read in debug mode
        keep_single_led_on = num_ch == 1

        for ch in range(num_ch):
            self._optics.led_on(ch)
            time.sleep(LED_SETTLE_S)
            ch_start = time.time()

            while (time.time() - ch_start) < ch_duration:
                if not state.sampling or stop_event.is_set():
                    aborted = True
                    break

                # ADC read
                try:
                    s_raw = self._adc.read_sensor()
                    r = self._adc.read_reference()
                except Exception:
                    logger.debug("ADC read error in CH%d", ch)
                    continue

                raw_counts[ch] += 1

                # Debug: log every Nth raw read
                if debug_mode and raw_counts[ch] % _dbg_interval == 1:
                    logger.info(
                        "DBG CH%d #%d: sen_raw=%.4f ref=%.4f cal_k=%.4f sen_cal=%.4f bounds=[%.2f,%.2f]",
                        ch, raw_counts[ch], s_raw, r, self._cal_k[ch],
                        s_raw * self._cal_k[ch], adc_low_limit, self._adc_high_limit,
                    )

                # Apply calibration factor, then bounds-check (ESP32 parity:
                # check sen*calK, not raw sen)
                s_cal = s_raw * self._cal_k[ch]
                if s_cal > self._adc_high_limit or r > self._adc_high_limit:
                    adc_high_total += 1
                    ch_high_counts[ch] += 1
                elif s_cal < adc_low_limit or r < adc_low_limit:
                    adc_low_total += 1
                    if s_cal > 0.0 and r > 0.0:
                        sen_bufs[ch].append(s_cal)
                        ref_bufs[ch].append(r)
                else:
                    sen_bufs[ch].append(s_cal)
                    ref_bufs[ch].append(r)

                # Transition warmup -> sampling status mid-cycle
                now = time.time()
                if (state.get("init_step") == InitStep.INIT_SETTLING
                        and now >= self._warmup_end):
                    state.set("init_step", InitStep.INIT_DONE)
                    logger.info("Warmup complete, logging started")

                # Flow sampling + pump health check
                while now >= next_flow_sample_time:
                    # Check pump stall recovery state
                    if self._pump.is_recovering():
                        logger.warning("Pump recovering from stall, aborting cycle")
                        aborted = True
                        break
                    if self._pump.is_failed():
                        logger.error("Pump stall fatal — too many recoveries")
                        self._set_error(ErrorCode.ERR_FLOW_ZERO)
                        aborted = True
                        break

                    try:
                        f = self._pump.get_flow_lpm() if airflow_sensor_present else target_flow
                    except Exception:
                        f = 0.0
                    flow_sum += f
                    flow_count += 1

                    next_flow_sample_time += FLOW_SAMPLE_INTERVAL_S

                if aborted:
                    break

            if aborted or not keep_single_led_on:
                self._optics.led_off(ch)

            # --- Outlier rejection / LED recovery ---
            if not aborted and raw_counts[ch] > 0:
                buf_count = len(sen_bufs[ch])
                rejected_ratio = 1.0 - (buf_count / raw_counts[ch]) if raw_counts[ch] > 0 else 0.0

                if rejected_ratio > OUTLIER_REJECT_PCT:
                    high_dominant = ch_high_counts[ch] > (raw_counts[ch] / 2)
                    if high_dominant:
                        logger.warning(
                            "CH%d: %.0f%% saturated, attempting LED recovery",
                            ch, rejected_ratio * 100,
                        )
                        recovered = self._recover_led_duty(ch)
                        if recovered:
                            logger.info("CH%d: LED duty lowered, applying next cycle", ch)
                            incident_log.add("warn", "CH%d LED duty lowered (ADC saturated)", ch)
                            if buf_count >= 5:
                                sen_mean, _, _ = sigma_reject(sen_bufs[ch], SIGMA_REJECT_LIMIT)
                                ref_mean, _, _ = sigma_reject(ref_bufs[ch], SIGMA_REJECT_LIMIT)
                                sen_avg[ch] = sen_mean
                                ref_avg[ch] = ref_mean
                                sample_counts[ch] = buf_count
                        else:
                            logger.error("CH%d: LED recovery failed", ch)
                            incident_log.add("error", "CH%d ADC saturated, LED recovery failed — no filter?", ch)
                            try:
                                email_handler.send_signal_noise_alert(
                                    f"CH{ch} ADC saturated, LED recovery failed"
                                )
                            except Exception:
                                pass
                            self._set_error(ErrorCode.ERR_ADC_SATURATED)
                            aborted = True
                    else:
                        logger.warning(
                            "CH%d: %.0f%% unusable low-signal reads - continuing",
                            ch, rejected_ratio * 100,
                        )
                        incident_log.add(
                            "warn", "CH%d low optical signal - %.0f%% reads skipped, continuing",
                            ch, rejected_ratio * 100,
                        )
                        state.set("warning_msg", "Low optical signal - trend only; replace filter")
                        notes.add(f"LOW{int(rejected_ratio * 100)}")
                        if buf_count >= 5:
                            sen_mean, _, _ = sigma_reject(sen_bufs[ch], SIGMA_REJECT_LIMIT)
                            ref_mean, _, _ = sigma_reject(ref_bufs[ch], SIGMA_REJECT_LIMIT)
                            sen_avg[ch] = sen_mean
                            ref_avg[ch] = ref_mean
                            sample_counts[ch] = buf_count
                else:
                    # Normal sigma-reject averaging
                    if buf_count > 0:
                        sen_mean, _, _ = sigma_reject(sen_bufs[ch], SIGMA_REJECT_LIMIT)
                        ref_mean, _, _ = sigma_reject(ref_bufs[ch], SIGMA_REJECT_LIMIT)
                        sen_avg[ch] = sen_mean
                        ref_avg[ch] = ref_mean
                        sample_counts[ch] = buf_count

            if aborted:
                break

        if aborted or not keep_single_led_on:
            self._optics.all_off()

        if debug_mode:
            for ch in range(num_ch):
                valid = len(sen_bufs[ch])
                high = ch_high_counts[ch]
                total = raw_counts[ch]
                bounds_reject = high + adc_low_total
                logger.info(
                    "DBG CH%d summary: %d total, %d valid, %d high, %d low, "
                    "bounds_reject=%.1f%% cal_k=%.4f",
                    ch, total, valid, high, adc_low_total,
                    (bounds_reject / total) * 100 if total > 0 else 0,
                    self._cal_k[ch],
                )

        if aborted:
            return row, [], True

        # ---- Read SPS30 particulate data ----
        pm25 = 0.0
        pm10 = 0.0
        if self._sps and self._sps.present:
            try:
                pm_data = self._sps.read()
                pm25 = pm_data.get("pm25", 0.0)
                pm10 = pm_data.get("pm10", 0.0)
            except Exception:
                logger.debug("SPS30 read failed")
                notes.add(notes.SPS_F)

        # ---- Flow average ----
        avg_flow = (flow_sum / flow_count) if flow_count > 0 else target_flow
        cycle_duration_s = time.time() - cycle_start
        # BC integration duration: use the inter-cycle interval (monotonic
        # time between the previous cycle start and this one) rather than
        # the per-cycle work time.  deltaATN spans previous-cycle ATN to
        # current-cycle ATN, which matches the interval, not the work
        # time.  Falls back to work time if no interval was supplied (e.g.
        # very first cycle — BC is skipped anyway via ``_first_cycle``).
        if interval_s > 0:
            duration_min = interval_s / 60.0
        else:
            duration_min = cycle_duration_s / 60.0

        # ---- BC calculation per channel ----
        bc_arr = [0.0] * num_ch
        for ch in range(num_ch):
            if sample_counts[ch] < 5:
                logger.debug("CH%d: only %d samples, skipping", ch, sample_counts[ch])
                continue

            sen = sen_avg[ch]
            ref = ref_avg[ch]
            atn = _calc_atn(sen, ref)

            # ATN limit warning (no longer stops measurement)
            if atn > ATN_LIMIT:
                logger.warning("CH%d ATN %.1f > %.0f -- filter needs replacement", ch, atn, ATN_LIMIT)
                incident_log.add("warn", "CH%d ATN %.1f > %.0f — filter needs replacement", ch, atn, ATN_LIMIT)
                state.set("warning_msg", "Filter needs replacement")

            bc_unfiltered = 0.0
            if not self._first_cycle:
                delta_atn = atn - self._last_atn[ch]
                volume = duration_min * avg_flow  # litre-minutes
                # Ambient pressure correction (ESP32 parity): normalise
                # volume to standard pressure so BC is comparable across
                # altitudes / weather conditions.
                if ambient_pressure_correction and env_pressure > 800:
                    volume *= 1013.25 / env_pressure
                load_r = _loading_corr(atn, shadow_factor)
                if volume > 0.0001:
                    atn_coeff = self._spot_area * (delta_atn / 100.0) / volume
                    absorption = atn_coeff / (filter_scatter * load_r)
                    bc_unfiltered = (absorption / SIGMA[ch]) * correction_factor

            bc_filtered = self._bc_filters[ch].update(bc_unfiltered)

            # Store per-channel data on the row
            wl = WAVELENGTH_NAMES[ch]
            setattr(row, f"ref_{wl}", ref)
            setattr(row, f"sen_{wl}", sen)
            setattr(row, f"atn_{wl}", atn)
            setattr(row, f"bc_unfiltered_{wl}", bc_unfiltered)
            setattr(row, f"bc_{wl}", bc_filtered)

            bc_arr[ch] = bc_filtered
            self._last_atn[ch] = atn

        if self._first_cycle:
            self._first_cycle = False
            logger.info("First cycle: baseline set, BC zeroed")

        # ---- Filter status (ATN-based, Weingartner validity stages) ----
        if ref_avg[0] > 0:
            quotient = sen_avg[0] / ref_avg[0]
        else:
            quotient = 1.0
        filt_status = _filter_status_from_atn(self._last_atn[0])
        row.relative_load = 1.0 - quotient
        state.set("filter_status", filt_status)

        # Filter alert email — fire when entering stage 3 (ATN > 100), every 12h
        loading_pct = round((1.0 - quotient) * 100)
        # Capture initial loading on first sample for delta-based rate calculation
        if self._initial_loading_pct < 0:
            self._initial_loading_pct = loading_pct
            self._initial_atn = self._last_atn[0]
        if self._cfg.get_bool("filter_status_mail", False) and self._last_atn[0] > 100.0:
            interval = self._cfg.get_float("filter_mail_interval", 12.0) * 3600
            if email_handler.can_send_mail("Filter", interval):
                try:
                    sh = self.get_session_hours()
                    delta = loading_pct - max(0, self._initial_loading_pct)
                    atn_rate = (delta / sh / 60.0) if (sh > 0.1 and delta > 0) else 0.0  # rate per minute for Lambda
                    email_handler.send_filter_alert(
                        self._last_atn[0], atn_rate, loading_pct,
                        self._cfg.get_string("device_name", "bcMeter"),
                        initial_loading_pct=self.get_initial_loading_pct(),
                    )
                except Exception:
                    pass

        # ---- AAE (Angstrom Absorption Exponent) ----
        if num_ch >= 2 and bc_arr[0] > 0 and bc_arr[1] > 0:
            try:
                row.aae = -math.log(bc_arr[0] / bc_arr[1]) / math.log(880.0 / 520.0)
            except (ValueError, ZeroDivisionError):
                row.aae = 0.0

        # ---- Environmental data ----
        row.temperature = env_temp if env_temp is not None else 0.0
        row.humidity = env_hum if env_hum is not None else 0.0
        row.pressure = env_pressure
        row.airflow = avg_flow
        row.sample_duration = cycle_duration_s
        row.pm25 = pm25
        row.pm10 = pm10
        row.pump_duty = self._pump.get_duty()

        # Guard: pump running but no airflow detected
        if airflow_sensor_present and self._pump.get_duty() > 0 and avg_flow < 0.01:
            self._zero_flow_cycles += 1
            if self._zero_flow_cycles == 3:
                # Brute-force recovery: max duty for 3s, then re-check
                max_duty = self._cfg.get("max_pump_duty", 255)
                incident_log.add("warn", "No airflow for 3 cycles — kicking pump to duty %d", max_duty)
                logger.warning("Zero-flow recovery: duty=%d for 3s", max_duty)
                self._pump.set_duty(max_duty)
                time.sleep(3)
                recovery_flow = self._pump.get_flow_avg(0.5)
                logger.info("Recovery flow: %.4f LPM", recovery_flow)
                if recovery_flow >= 0.01:
                    incident_log.add("info", "Flow recovered (%.3f LPM) after max-duty kick", recovery_flow)
                    self._zero_flow_cycles = 0
                    self._pump.trigger_sweep()
                # else: stays at 3, next zero-flow cycle hits > 3
            elif self._zero_flow_cycles > 3:
                incident_log.add("error", "No airflow after recovery attempt — stopping measurement")
                email_handler.send_pump_error(avg_flow)
                self._set_error(ErrorCode.ERR_FLOW_ZERO)
                self._zero_flow_cycles = 0
                return row, [], True
        else:
            self._zero_flow_cycles = 0

        # Update pressure state and trigger QNH refresh
        if env_pressure > 0:
            state.set("last_pressure", env_pressure)
        qnh.fetch_if_needed()

        # ---- Notes ----
        if adc_high_total > 0:
            notes.add(notes.ADC_HI)
        if adc_low_total > 0:
            notes.add(notes.ADC_LO)
        row.notice = notes.drain()

        # ---- GPS ----
        if self._gps and self._gps.present:
            try:
                gps_data = self._gps.get_data()
                if gps_data.valid:
                    row.latitude = gps_data.lat
                    row.longitude = gps_data.lon
                    row.altitude = gps_data.altitude
            except Exception:
                logger.debug("GPS read failed")

        # ---- Update shared state ----
        state.update(
            last_sen=sen_avg[0] if sen_avg else 1.0,
            last_ref=ref_avg[0] if ref_avg else 1.0,
            last_pm25=pm25,
            last_pm10=pm10,
            last_temp=row.temperature,
            last_humidity=row.humidity,
            last_flow=avg_flow,
            sample_count=state.get("sample_count") + 1,
        )

        return row, bc_arr, False

    # ------------------------------------------------------------------
    # LED duty recovery
    # ------------------------------------------------------------------

    def _recover_led_duty(self, ch: int) -> bool:
        """Attempt to lower LED duty to bring ADC readings in-bounds.

        Returns True if a workable duty was found.
        """
        wl_name = WAVELENGTH_NAMES[ch]
        duty_key = f"led_duty_cycle_{wl_name}"
        old_duty = self._cfg.get_int(duty_key, 128)
        duty_floor = self._cfg.get_int("led_duty_floor", LED_DUTY_FLOOR_DEFAULT)
        duty = old_duty

        for _ in range(40):
            duty = max(duty - 5, 0)
            if duty < duty_floor:
                logger.warning(
                    "CH%d duty %d hit floor %d -- no filter?", ch, duty, duty_floor,
                )
                return False

            self._optics.set_led_duty(ch, duty)
            self._optics.led_on(ch)
            time.sleep(0.2)
            s = self._adc.read_sensor()
            r = self._adc.read_reference()
            self._optics.led_off(ch)

            logger.debug("Recovery CH%d duty=%d sen=%.3f ref=%.3f", ch, duty, s, r)

            if s < self._adc_high_limit and r < self._adc_high_limit:
                next_duty = min(duty + 5, 255)
                self._optics.set_led_duty(ch, next_duty)
                self._optics.led_on(ch)
                time.sleep(0.2)
                s2 = self._adc.read_sensor()
                r2 = self._adc.read_reference()
                self._optics.led_off(ch)

                if s2 >= self._adc_high_limit or r2 >= self._adc_high_limit:
                    # Current duty is the maximum safe value
                    self._cfg.set_int(duty_key, duty)
                    self._optics.set_led_duty(ch, duty)
                    self._cfg.save()
                    notes.add("LED_REC")
                    logger.info(
                        "CH%d LED duty recovered %d -> %d", ch, old_duty, duty,
                    )
                    return True
                duty = next_duty

        return False

    # ------------------------------------------------------------------
    # Sensor helpers
    # ------------------------------------------------------------------

    def _read_temperature(self) -> float:
        """Read temperature from available sensor. Returns None on failure."""
        if self._bme and self._bme.present:
            try:
                temp, _, _ = self._bme.read()
                return temp
            except Exception:
                logger.debug("BME280 read error")
        if self._sht and self._sht.present:
            try:
                temp, _ = self._sht.read()
                return temp
            except Exception:
                logger.debug("SHT4x read error")
        if self._ds and self._ds.present:
            try:
                return self._ds.read()
            except Exception:
                logger.debug("DS18B20 read error")
        return None

    def _read_env(self) -> tuple:
        """Read temperature, humidity, and pressure.

        Returns (temp, humidity, pressure). Pressure may be 0.0 if
        no barometric sensor is available.

        Priority: BME280 -> SHT4x -> DS18B20 (temp only).
        """
        temp = None
        hum = None
        pressure = 0.0

        # Try BME280 first (provides T + H + P)
        if self._bme and self._bme.present:
            try:
                t, h, p = self._bme.read()
                temp = t
                hum = h
                pressure = p
            except Exception:
                notes.add("BME_F")

        # Fallback to SHT4x (up to 3 attempts)
        if temp is None and self._sht and self._sht.present:
            for attempt in range(3):
                try:
                    t, h = self._sht.read()
                    temp = t
                    hum = h
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(0.05)
            else:
                notes.add("SHT_F")

        # Fallback to DS18B20 for temperature
        if temp is None and self._ds and self._ds.present:
            try:
                temp = self._ds.read()
            except Exception:
                pass

        return temp, hum, pressure

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _set_error(self, error: ErrorCode):
        """Set error state and stop sampling."""
        state.set("error", error)
        state.sampling = False
        incident_log.add("error", "Measurement error: %s", error.name)
        logger.error("ERROR %d: %s -- stopping", int(error), error.name)
