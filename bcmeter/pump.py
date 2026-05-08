"""Pump and airflow control with AFC (Automatic Flow Control).

Ported from ESP32 pump.cpp with pigpiod PWM control.
"""

import logging
import math
import statistics
import threading
import time
from collections import deque

from . import incident_log

logger = logging.getLogger("bcmeter.pump")

PUMP_PIN = 12
TWELVEVOLT_PIN = 27
PUMP_PWM_FREQ = 300
PUMP_PWM_DUTY_MAX = 1_000_000


def pump_duty_to_hardware(duty: int) -> int:
    """Convert public 0..255 pump duty to pigpio hardware_PWM millionths."""
    bounded = max(0, min(255, int(duty)))
    return int(round(bounded * PUMP_PWM_DUTY_MAX / 255))

# Airflow voltage-to-flow lookup tables.
# Sensor type 0: D6F-P0001A1 (legacy low-flow sensor)
# Sensor type 1: D6F-P0010A2 (ESP32 pump.cpp 25-point calibration)
P0001A1_V_LUT = [0.5000, 2.5000]
P0001A1_F_LUT = [0.0000, 0.1000]
P0010A2_V_LUT = [
    0.7120, 0.9570, 1.1210, 1.2390, 1.4040, 1.4950, 1.6300, 1.7560,
    1.8340, 1.8960, 1.9390, 2.0140, 2.0290, 2.1000, 2.1340, 2.1430,
    2.1820, 2.2200, 2.2540, 2.2660, 2.2800, 2.3270, 2.3790, 2.4000,
    2.4380,
]
P0010A2_F_LUT = [
    0.0195, 0.0836, 0.1085, 0.1431, 0.1764, 0.2043, 0.2525, 0.2861,
    0.3278, 0.3568, 0.3996, 0.4342, 0.4685, 0.5086, 0.5532, 0.5977,
    0.6273, 0.6836, 0.7175, 0.7585, 0.7841, 0.8135, 0.8616, 0.9073,
    0.9374,
]
FLOW_TABLES = {
    0: (P0001A1_V_LUT, P0001A1_F_LUT),
    1: (P0010A2_V_LUT, P0010A2_F_LUT),
}

# AFC table: BC concentration (ng/m³) → target flow (LPM)
AFC_BC = [100.0, 500.0, 1000.0, 5000.0]
AFC_FLOW = [0.300, 0.100, 0.050, 0.025]

STALL_FLOW = 0.015  # LPM below which pump is considered stalled


def _table_lookup(x, x_table, y_table):
    """Linear interpolation in a lookup table."""
    if x <= x_table[0]:
        return y_table[0]
    for i in range(1, len(x_table)):
        if x <= x_table[i]:
            t = (x - x_table[i - 1]) / (x_table[i] - x_table[i - 1])
            return y_table[i - 1] + t * (y_table[i] - y_table[i - 1])
    return y_table[-1]


def _flow_table(sensor_type=1):
    try:
        sensor_type = int(sensor_type)
    except Exception:
        sensor_type = 1
    return FLOW_TABLES.get(sensor_type, FLOW_TABLES[1])


def voltage_to_flow(v, bias=0.0, sensor_type=1):
    """Convert airflow sensor voltage to flow rate (LPM)."""
    v_lut, f_lut = _flow_table(sensor_type)
    v += bias
    if v <= v_lut[0]:
        return 0.0
    return _table_lookup(v, v_lut, f_lut)


def flow_limit_lpm(adc=None, sensor_type=1, bias=0.0, headroom=True):
    """Maximum measurable flow for the configured D6F sensor and ADC range."""
    v_lut, f_lut = _flow_table(sensor_type)
    sensor_max_v = v_lut[-1]
    adc_limit_v = sensor_max_v
    if adc is not None:
        try:
            adc_limit_v = float(adc.high_limit if headroom else adc.vref)
        except Exception:
            try:
                adc_limit_v = float(adc.vref)
            except Exception:
                adc_limit_v = sensor_max_v
    usable_v = max(0.0, min(sensor_max_v, adc_limit_v))
    return min(f_lut[-1], voltage_to_flow(usable_v, bias=bias, sensor_type=sensor_type))


def afc_table_lookup(bc):
    """AFC: map BC concentration to target flow rate."""
    return _table_lookup(bc, AFC_BC, AFC_FLOW)


class Pump:
    """Pump control with pigpiod PWM and automatic flow control.

    Runs a background thread that adjusts pump duty cycle to maintain
    the desired airflow rate.
    """

    def __init__(self, config=None, adc=None):
        self._pi = None
        self._config = config
        self._adc = adc
        self._duty = 0
        self._bias = 0.0
        self._flow_bump_lpm = 0.0
        self._last_target = 0.0
        self._lock = threading.Lock()
        self._use_hardware_pwm = False

        # Stall detection state
        self._stall_seconds = 0
        self._recovery_count = 0
        self._settled_seconds = 0
        self._recovering = False
        self._failed = False
        self._sweep_requested = False

        # Rolling flow buffer for median smoothing
        self._roll_buf = deque(maxlen=80)
        self._roll_time = deque(maxlen=80)

        # BC history for AFC
        self._bc_hist = deque(maxlen=64)
        self._bc_hist_time = deque(maxlen=64)
        self._afc_last_output = 0.0
        self._last_limit_warn = 0.0

    def init(self, pi):
        """Initialize pump PWM via pigpiod."""
        self._pi = pi
        if not pi or not pi.connected:
            logger.error("pigpiod not connected, cannot init pump")
            return False

        # BB16 ADS8344 tests showed 300 Hz gives lower optical ratio noise near
        # 300 ml/min than the old 48 Hz setting. Use GPIO12 hardware PWM so
        # pigpio does not quantize the requested frequency.
        pwm_freq = PUMP_PWM_FREQ

        pi.set_mode(PUMP_PIN, 1)  # OUTPUT
        rc = pi.hardware_PWM(PUMP_PIN, pwm_freq, 0)
        if rc == 0:
            self._use_hardware_pwm = True
        else:
            self._use_hardware_pwm = False
            pi.set_PWM_range(PUMP_PIN, 255)
            actual = pi.set_PWM_frequency(PUMP_PIN, pwm_freq)
            pi.set_PWM_dutycycle(PUMP_PIN, 0)
            logger.warning(
                "Pump hardware PWM unavailable on GPIO%d (rc=%s); falling back to pigpio software PWM at %s Hz",
                PUMP_PIN, rc, actual,
            )

        logger.info("Pump initialized (freq=%s Hz, hardware_pwm=%s)", pwm_freq, self._use_hardware_pwm)
        return True

    def set_duty(self, duty: int):
        """Set pump duty cycle directly."""
        with self._lock:
            self._duty = max(0, min(255, int(duty)))
            if self._pi and self._pi.connected:
                if self._use_hardware_pwm:
                    rc = self._pi.hardware_PWM(
                        PUMP_PIN, PUMP_PWM_FREQ, pump_duty_to_hardware(self._duty)
                    )
                    if rc != 0:
                        logger.warning("Pump hardware PWM write failed (rc=%s); using software PWM", rc)
                        self._use_hardware_pwm = False
                if not self._use_hardware_pwm:
                    self._pi.set_PWM_dutycycle(PUMP_PIN, self._duty)

    def get_duty(self) -> int:
        return self._duty

    def get_flow_lpm(self) -> float:
        """Read current airflow in LPM from ADC."""
        if self._adc is None:
            return 0.0
        v = self._adc.read_airflow_voltage()
        return voltage_to_flow(v, self._bias, self._sensor_type())

    def get_flow_avg(self, duration_s=1.0) -> float:
        """Read flow averaged over a duration using median."""
        samples = []
        end = time.time() + duration_s
        while time.time() < end:
            samples.append(self.get_flow_lpm())
            time.sleep(0.025)
        if not samples:
            return 0.0
        return statistics.median(samples)

    def calibrate_bias(self):
        """Calibrate airflow sensor zero offset with pump off."""
        self.set_duty(0)
        time.sleep(0.5)
        samples = []
        for _ in range(50):
            if self._adc:
                samples.append(self._adc.read_airflow_voltage())
            time.sleep(0.01)
        if samples:
            self._bias = 0.5 - statistics.median(samples)
            if abs(self._bias) > 0.1:
                logger.warning(f"Airflow bias {self._bias:.4f} exceeds 0.1V limit, using 0")
                incident_log.add("warn", "Airflow sensor bias %.4f out of range, zeroed", self._bias)
                self._bias = 0.0
            else:
                logger.info(f"Airflow bias calibrated: {self._bias:.4f}V")

    def get_flow_bump(self) -> float:
        return self._flow_bump_lpm

    def is_recovering(self) -> bool:
        """True while pump is executing stall recovery (findDuty re-sweep)."""
        return self._recovering

    def is_failed(self) -> bool:
        """True after too many stall recoveries — fatal pump error."""
        return self._failed

    def trigger_sweep(self):
        """Request a duty re-sweep on next pump loop iteration."""
        self._sweep_requested = True

    def push_bc(self, bc: float):
        """Record a BC measurement for AFC calculations."""
        self._bc_hist.append(bc)
        self._bc_hist_time.append(time.time())

    def _sensor_type(self) -> int:
        if self._config is None:
            return 1
        try:
            return int(self._config.get_int("af_sensor_type", 1))
        except Exception:
            return 1

    def flow_limit_lpm(self) -> float:
        return flow_limit_lpm(self._adc, self._sensor_type(), self._bias)

    def clamp_target_lpm(self, target_lpm: float, source: str = "target") -> float:
        """Clamp requested airflow to what the D6F sensor and ADC can measure."""
        try:
            target = float(target_lpm)
        except Exception:
            target = 0.25
        limit = self.flow_limit_lpm()
        if limit > 0 and target > limit:
            now = time.time()
            if now - self._last_limit_warn > 30:
                adc_type = getattr(self._adc, "type", "") if self._adc is not None else "unknown"
                logger.warning(
                    "%s airflow %.0f ml/min exceeds %s/D6F measurable limit %.0f ml/min; clamping",
                    source, target * 1000, adc_type, limit * 1000,
                )
                incident_log.add(
                    "warn", "%s airflow clamped to %.0f ml/min by ADC/D6F limit",
                    source, limit * 1000,
                )
                self._last_limit_warn = now
            return limit
        return max(0.0, target)

    def _avg_10min_bc(self) -> float:
        """Average BC over the last 10 minutes."""
        if not self._bc_hist:
            return float("nan")
        cutoff = time.time() - 600
        values = [
            bc
            for bc, t in zip(self._bc_hist, self._bc_hist_time)
            if t >= cutoff
        ]
        if len(values) < 2:
            return float("nan")
        return sum(values) / len(values)

    def auto_target_lpm(self) -> float:
        """Calculate AFC target flow based on recent BC levels."""
        fallback = self._config.get_float("airflow_per_minute", 0.25) if self._config else 0.25
        fallback = self.clamp_target_lpm(fallback, "Configured")
        f_low = self._config.get_float("afc_flow_low", 0.025) if self._config else 0.025
        f_high = self._config.get_float("afc_flow_high", 0.5) if self._config else 0.5
        flow_limit = self.flow_limit_lpm()
        if flow_limit > 0:
            f_high = min(f_high, flow_limit)
            f_low = min(f_low, f_high)

        avg_bc = self._avg_10min_bc()
        if math.isnan(avg_bc):
            return self._afc_last_output if self._afc_last_output > 0 else fallback

        target = afc_table_lookup(avg_bc)
        filter_days = max(1, int(self._config.get_int("filter_days", 7)) if self._config else 7)
        target *= 20.0 / filter_days
        target = self.clamp_target_lpm(max(f_low, min(f_high, target)), "AFC")

        # Rate-limit changes
        if self._afc_last_output > 0:
            diff = target - self._afc_last_output
            # ESP32 parity: dead-band 50 ml/min, rate-limit 20% of diff
            if abs(diff) < 0.050:
                return self._afc_last_output
            max_step = abs(diff) * 0.20
            if diff > max_step:
                target = self._afc_last_output + max_step
            elif diff < -max_step:
                target = self._afc_last_output - max_step

        self._afc_last_output = target
        logger.info(f"AFC: avgBC={avg_bc:.0f} -> {target * 1000:.0f} ml/min")
        return target

    def find_duty(self, target_lpm: float, should_continue=None):
        """Search for pump duty cycle that achieves target flow.

        Binary-search style ramp up/down to find the best duty.
        """
        target_lpm = self.clamp_target_lpm(target_lpm, "Duty search")

        def keep_running() -> bool:
            if should_continue is None:
                return True
            try:
                return bool(should_continue())
            except Exception:
                return False

        d_min = self._config.get_int("min_pump_duty", 0) if self._config else 0
        d_max = self._config.get_int("max_pump_duty", 255) if self._config else 255
        start = self._duty if self._duty > 0 else (
            self._config.get_int("pump_dutycycle", 20) if self._config else 40
        )
        start = max(d_min, min(d_max, start))

        if not keep_running():
            return None
        self.set_duty(start)
        time.sleep(1.5)
        if not keep_running():
            return None
        flow = self.get_flow_avg(1.0)
        logger.debug(f"findDuty start d={start} flow={flow:.4f} target={target_lpm:.4f}")

        lo_d, lo_flow = 0, 0.0
        hi_d, hi_flow = 0, 0.0

        if flow > STALL_FLOW:
            if flow >= target_lpm:
                hi_d, hi_flow = start, flow
            else:
                lo_d, lo_flow = start, flow

        go_up = flow < target_lpm or flow < STALL_FLOW

        if go_up:
            d = d_min if flow < STALL_FLOW else start + 2
            while d <= d_max:
                if not keep_running():
                    logger.info("Duty search cancelled")
                    return None
                self.set_duty(d)
                time.sleep(0.3)
                if not keep_running():
                    logger.info("Duty search cancelled")
                    return None
                f = self.get_flow_avg(0.5)
                if f < STALL_FLOW:
                    d += 2
                    continue
                if f < target_lpm:
                    lo_d, lo_flow = d, f
                else:
                    hi_d, hi_flow = d, f
                    break
                d += 2
        else:
            d = start - 2
            while d >= d_min:
                if not keep_running():
                    logger.info("Duty search cancelled")
                    return None
                self.set_duty(d)
                time.sleep(0.3)
                if not keep_running():
                    logger.info("Duty search cancelled")
                    return None
                f = self.get_flow_avg(0.5)
                if f < STALL_FLOW:
                    break
                if f >= target_lpm:
                    hi_d, hi_flow = d, f
                else:
                    lo_d, lo_flow = d, f
                    break
                d -= 2

        if hi_d and lo_d:
            best = lo_d if (target_lpm - lo_flow) <= (hi_flow - target_lpm) else hi_d
        elif hi_d:
            best = hi_d
        elif lo_d:
            best = lo_d
        else:
            logger.warning(f"No duty found for {target_lpm:.3f} LPM")
            return 0

        if self._config:
            self._config.set_int("pump_dutycycle", best)
            self._config.save()

        logger.info(f"Found duty={best} for {target_lpm:.3f} LPM")
        return best

    def control_task(self, stop_event: threading.Event, state=None):
        """Background pump control loop.

        Args:
            stop_event: Threading event to signal shutdown.
            state: SystemState manager for reading sampling flag and writing flow.
        """
        need_sweep = True

        if self._config is None or self._config.get_bool("airflow_sensor", True):
            self.calibrate_bias()

        while not stop_event.is_set():
            sampling = state.get("sampling") if state else False

            if not sampling:
                if self._duty != 0:
                    self.set_duty(0)
                if state:
                    state.set("last_flow", 0.0)
                need_sweep = True
                self._flow_bump_lpm = 0
                self._afc_last_output = 0
                self._bc_hist.clear()
                self._bc_hist_time.clear()
                self._last_target = 0
                self._stall_seconds = 0
                self._recovery_count = 0
                self._settled_seconds = 0
                self._recovering = False
                self._failed = False
                stop_event.wait(0.5)
                continue

            target_lpm = (
                self._config.get_float("airflow_per_minute", 0.25) + self._flow_bump_lpm
                if self._config else 0.25 + self._flow_bump_lpm
            )

            disable_ctrl = self._config.get_bool("disable_pump_control", False) if self._config else False
            airflow_sensor = self._config.get_bool("airflow_sensor", True) if self._config else True
            auto_afc = self._config.get_bool("automatic_airflow_control", False) if self._config else False

            if auto_afc and state:
                cur_bc = state.get("last_bc")
                self.push_bc(cur_bc)
                target_lpm = self.auto_target_lpm() + self._flow_bump_lpm
                target_lpm = self.clamp_target_lpm(target_lpm, "AFC")
            else:
                target_lpm = self.clamp_target_lpm(target_lpm, "Configured")

            if disable_ctrl or not airflow_sensor:
                d = self._config.get_int("pump_dutycycle", 20) if self._config else 40
                if self._duty != d:
                    self.set_duty(d)
                flow = self.get_flow_avg(0.2) if airflow_sensor else target_lpm
                if state:
                    state.set("last_flow", flow)
                stop_event.wait(1.0)
                continue

            if self._sweep_requested:
                need_sweep = True
                self._sweep_requested = False
            if need_sweep or abs(target_lpm - self._last_target) > 0.03:
                def should_continue():
                    return not stop_event.is_set() and bool(state.get("sampling") if state else True)

                found = self.find_duty(target_lpm, should_continue=should_continue)
                if found is None:
                    self.set_duty(0)
                    need_sweep = True
                    stop_event.wait(0.5)
                    continue
                if found == 0:
                    self.set_duty(0)
                    logger.error("Pump stall: no flow at any duty")
                    incident_log.add("error", "Pump stall: no flow at any duty (target %.0f ml/min)", target_lpm * 1000)
                    stop_event.wait(10.0)
                    continue
                self._duty = found
                self.set_duty(found)
                self._last_target = target_lpm
                need_sweep = False
                time.sleep(1.0)

            # Sample flow for 1 second
            end = time.time() + 1.0
            while time.time() < end and not stop_event.is_set():
                self._roll_buf.append(self.get_flow_lpm())
                self._roll_time.append(time.time())
                time.sleep(0.025)

            # Compute 1-second rolling median
            cutoff = time.time() - 1.0
            recent = [f for f, t in zip(self._roll_buf, self._roll_time) if t >= cutoff]
            flow = statistics.median(recent) if recent else 0.0

            if state:
                state.set("last_flow", flow)

            # Stall detection and recovery
            if flow < STALL_FLOW and self._duty > 0:
                self._stall_seconds += 1
                self._settled_seconds = 0
                if self._stall_seconds >= 3:
                    self._flow_bump_lpm += 0.05
                    new_target = target_lpm + 0.05
                    logger.warning(
                        f"Stall confirmed ({self._stall_seconds}s), "
                        f"bump +50ml -> target {new_target * 1000:.0f} ml/min, "
                        f"kicking to duty {self._config.get_int('max_pump_duty', 255) // 2}"
                    )
                    incident_log.add("warn", "Stall confirmed, target bumped to %.0f ml/min", new_target * 1000)
                    d_max = self._config.get_int("max_pump_duty", 255) if self._config else 255
                    self._recovering = True
                    if state:
                        state.set("flow_health", 1)
                    self.set_duty(d_max // 2)
                    time.sleep(2.0)
                    found = self.find_duty(new_target, should_continue=lambda: (
                        not stop_event.is_set() and bool(state.get("sampling") if state else True)
                    ))
                    if found is None:
                        self.set_duty(0)
                    self._recovering = False
                    self._stall_seconds = 0
                    self._recovery_count += 1
                    if self._recovery_count > 3:
                        logger.error("Pump stall: 4th recovery failed — fatal")
                        incident_log.add("error", "Pump stall: too many recoveries — fatal")
                        self._failed = True
                        if state:
                            state.set("flow_health", 2)
                        try:
                            from . import email_handler
                            email_handler.send_pump_error(flow)
                        except Exception:
                            pass
                    else:
                        if state:
                            state.set("flow_health", 0)
                        need_sweep = False
                        self._last_target = new_target
            else:
                self._stall_seconds = 0
                if flow >= STALL_FLOW and self._duty > 0:
                    self._settled_seconds += 1
                    if self._settled_seconds >= 10:
                        self._recovery_count = 0
                        self._settled_seconds = 0

                    # Continuous P-hold: nudge duty ±1 to keep flow at target
                    # (v1 parity — find_duty only runs at start/target change,
                    # which leaves the pump at fixed duty and lets filter
                    # loading / pump wear drag flow away from target).
                    # Runs regardless of AFC: AFC adjusts target, this holds it.
                    # Deadband ±50 ml/min prevents oscillation.
                    if not self._recovering and target_lpm > 0:
                        d_min = self._config.get_int("min_pump_duty", 0) if self._config else 0
                        d_max = self._config.get_int("max_pump_duty", 255) if self._config else 255
                        nd = self._duty
                        if flow < target_lpm - 0.050:
                            nd += 1
                        elif flow > target_lpm + 0.050:
                            nd -= 1
                        nd = max(d_min, min(d_max, nd))
                        if nd != self._duty:
                            self._duty = nd
                            self.set_duty(nd)

    def shutdown(self, reverse=False):
        """Safely stop the pump."""
        safe_duty = 100 if reverse else 0
        self.set_duty(safe_duty)
