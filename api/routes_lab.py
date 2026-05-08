"""Lab-mode endpoint for direct hardware noise characterisation.

Pauses sampling, configures LED + pump to specified parameters, reads raw
sen/ref pairs for a fixed duration, returns stats + (optionally) the raw
pairs.  Mirrors the ESP32 STREAM / LABFLOW commands exposed over serial.

GET /api/lab/run
  led_ch=0..2        LED channel (0=880nm, 1=520nm, 2=370nm)
  led_duty=0..255
  led_hz=10..200000  target LED PWM frequency (actual may be quantised)
  pump_duty=0..255
  target_flow_ml=0..1000 optional closed-loop pump plateau target
  pump_hz=10..200000
  duration_s=float   capture window (seconds)
  flow=0|1           collect airflow stats during capture (default 0)
  raw=0|1            include raw pairs array in response (default 1)
"""
from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import JSONResponse

from bcmeter.optics import LED_PINS, LED_PWM_FREQ, LED_PWM_RANGE, led_duty_to_pwm
from bcmeter.pump import (
    PUMP_PIN,
    PUMP_PWM_FREQ,
    STALL_FLOW,
    flow_limit_lpm,
    pump_duty_to_hardware,
    voltage_to_flow,
)

logger = logging.getLogger("bcmeter.api.lab")
router = APIRouter()

_cfg = None
_state = None
_engine = None
_pi = None
_adc = None
_optics = None
_pump = None

_lab_lock = threading.Lock()

# rpi-hardware-pwm instance for pump (GPIO 12 = PWM0, requires
# dtoverlay=pwm-2chan,pin=12,func=4 in /boot/firmware/config.txt).
_hw_pump = None


def _hw_pump_get():
    """Lazy-init the HW PWM pump channel (channel 0 on pwmchip0)."""
    global _hw_pump
    if _hw_pump is None:
        try:
            from rpi_hardware_pwm import HardwarePWM
            _hw_pump = HardwarePWM(pwm_channel=0, hz=PUMP_PWM_FREQ, chip=0)
            _hw_pump.start(0)
        except Exception as e:
            logger.warning("rpi-hardware-pwm unavailable for pump: %s", e)
            _hw_pump = False  # sentinel for failed init
    return _hw_pump if _hw_pump is not False else None


def set_dependencies(cfg, state_mgr, engine, pi, adc, optics, pump):
    global _cfg, _state, _engine, _pi, _adc, _optics, _pump
    _cfg = cfg
    _state = state_mgr
    _engine = engine
    _pi = pi
    _adc = adc
    _optics = optics
    _pump = pump


def _bound(x, lo, hi):
    return max(lo, min(hi, x))


def _set_pump_pwm(hw_pump, pump_hz: int, pump_duty: int) -> int:
    """Set lab pump PWM through the active controller and return actual Hz."""
    bounded_duty = int(_bound(pump_duty, 0, 255))
    pump_duty_pct = bounded_duty * 100.0 / 255.0
    if hw_pump is not None:
        hw_pump.change_frequency(pump_hz)
        hw_pump.change_duty_cycle(pump_duty_pct)
        return pump_hz

    rc = _pi.hardware_PWM(PUMP_PIN, pump_hz, pump_duty_to_hardware(bounded_duty))
    if rc == 0:
        return pump_hz

    logger.warning("pigpio hardware PWM unavailable for pump (rc=%s); using software PWM", rc)
    _pi.set_PWM_range(PUMP_PIN, 1000)
    actual_pump_hz = _pi.set_PWM_frequency(PUMP_PIN, pump_hz)
    _pi.set_PWM_dutycycle(PUMP_PIN, int(bounded_duty * 1000 / 255))
    return actual_pump_hz


def _read_flow_lpm() -> Optional[float]:
    """Read airflow in LPM, using Pump bias when available."""
    try:
        if _pump is not None:
            value = float(_pump.get_flow_lpm())
        elif _adc is not None:
            value = float(voltage_to_flow(
                _adc.read_airflow_voltage(), sensor_type=_cfg_int("af_sensor_type", 1)
            ))
        else:
            return None
    except Exception as e:
        logger.warning("flow read failed: %s", e)
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, value)


def _measure_flow_lpm(duration_s: float = 0.5) -> tuple[Optional[float], list[float]]:
    samples: list[float] = []
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        value = _read_flow_lpm()
        if value is not None:
            samples.append(value)
        time.sleep(0.01)
    if not samples:
        return None, samples
    return statistics.median(samples), samples


def _flow_stats(flows: list[float]) -> dict:
    n = len(flows)
    stats = {
        "flow_samples": n,
        "flow_mean_lpm": None,
        "flow_mean_ml": None,
        "flow_sigma_lpm": None,
        "flow_sigma_ml": None,
        "flow_min_lpm": None,
        "flow_min_ml": None,
        "flow_max_lpm": None,
        "flow_max_ml": None,
    }
    if not flows:
        return stats

    mean_lpm = sum(flows) / n
    sigma_lpm = statistics.pstdev(flows) if n > 1 else 0.0
    stats.update({
        "flow_mean_lpm": mean_lpm,
        "flow_mean_ml": mean_lpm * 1000.0,
        "flow_sigma_lpm": sigma_lpm,
        "flow_sigma_ml": sigma_lpm * 1000.0,
        "flow_min_lpm": min(flows),
        "flow_min_ml": min(flows) * 1000.0,
        "flow_max_lpm": max(flows),
        "flow_max_ml": max(flows) * 1000.0,
    })
    return stats


def _flow_adc_limit_ml() -> Optional[float]:
    if _adc is None:
        return None
    try:
        return float(flow_limit_lpm(_adc, _cfg_int("af_sensor_type", 1)) * 1000.0)
    except Exception:
        return None


def _cfg_int(key: str, default: int) -> int:
    if _cfg is None:
        return default
    try:
        return int(_cfg.get_int(key, default))
    except Exception:
        return default


def _find_pump_duty_for_flow(hw_pump, pump_hz: int, target_lpm: float,
                             start_duty: int) -> tuple[int, dict]:
    """Find a duty that produces a target flow using the lab pump controller.

    Small diaphragm pumps often need a higher-duty spin-up before they can hold
    a lower flow.  Search upward until flow starts, then walk downward while the
    pump is already moving.  This avoids treating a stalled low-duty point as
    the best match for low targets.
    """
    d_min = int(_bound(_cfg_int("min_pump_duty", 0), 0, 255))
    d_max = int(_bound(_cfg_int("max_pump_duty", 255), d_min, 255))
    if target_lpm <= 0:
        _set_pump_pwm(hw_pump, pump_hz, 0)
        return 0, {
            "pump_target_flow_ml": target_lpm * 1000.0,
            "pump_target_points": [],
        }

    points = []
    seen: set[int] = set()

    def probe(duty: int, settle_s: float = 0.45,
              measure_s: float = 0.55, phase: str = "probe") -> Optional[float]:
        bounded = int(_bound(duty, 0, 255))
        if bounded in seen:
            return None
        seen.add(bounded)
        _set_pump_pwm(hw_pump, pump_hz, bounded)
        time.sleep(settle_s)
        flow_lpm, samples = _measure_flow_lpm(measure_s)
        points.append({
            "duty": bounded,
            "flow_ml": flow_lpm * 1000.0 if flow_lpm is not None else None,
            "samples": len(samples),
            "phase": phase,
        })
        return flow_lpm

    low_d, low_f = 0, 0.0
    high_d: Optional[int] = None
    high_f: Optional[float] = None
    step = 20
    candidates = list(range(d_min, d_max + 1, step))
    if candidates[-1] != d_max:
        candidates.append(d_max)
    hint = int(_bound(start_duty, d_min, d_max)) if start_duty else d_min
    if hint not in candidates:
        candidates.insert(0, hint)
    candidates = sorted(set(candidates))

    for duty in candidates:
        flow_lpm = probe(duty, settle_s=0.6, measure_s=0.7, phase="ramp_up")
        if flow_lpm is None:
            continue
        if flow_lpm >= target_lpm:
            high_d, high_f = duty, flow_lpm
            break
        if flow_lpm >= STALL_FLOW:
            low_d, low_f = duty, flow_lpm

    # If we found a flowing point, walk downward with the pump already moving to
    # capture low-flow plateaus that cannot self-start from rest.
    if high_d is not None:
        ramp_step = 5
        d = high_d - ramp_step
        while d >= d_min:
            flow_lpm = probe(d, settle_s=0.45, measure_s=0.7, phase="ramp_down")
            if flow_lpm is None:
                d -= ramp_step
                continue
            if flow_lpm < STALL_FLOW:
                low_d, low_f = d, 0.0
                break
            if flow_lpm >= target_lpm:
                high_d, high_f = d, flow_lpm
            else:
                low_d, low_f = d, flow_lpm
                break
            d -= ramp_step

    if high_d is not None and high_d > low_d + 1:
        for _ in range(5):
            mid = (low_d + high_d) // 2
            if mid in seen:
                break
            flow_lpm = probe(mid, settle_s=0.45, measure_s=0.7, phase="bracket")
            if flow_lpm is None:
                break
            if flow_lpm >= target_lpm:
                high_d, high_f = mid, flow_lpm
            elif flow_lpm >= STALL_FLOW:
                low_d, low_f = mid, flow_lpm
            else:
                low_d, low_f = mid, 0.0
                break

    best = None
    best_error = float("inf")
    flowing_points = [
        p for p in points
        if p.get("flow_ml") is not None and p.get("flow_ml", 0.0) >= STALL_FLOW * 1000.0
    ]
    candidates_for_best = flowing_points or [p for p in points if p.get("flow_ml") is not None]
    for point in candidates_for_best:
        flow_ml = point.get("flow_ml")
        error = abs(flow_ml - target_lpm * 1000.0)
        if error < best_error:
            best = point
            best_error = error

    if best is None:
        final_duty = int(_bound(start_duty or d_min, 0, 255))
        final_flow_ml = None
    else:
        final_duty = int(best["duty"])
        final_flow_ml = best.get("flow_ml")

    _set_pump_pwm(hw_pump, pump_hz, final_duty)
    target_ml = target_lpm * 1000.0
    return final_duty, {
        "target_flow_ml": target_ml,
        "pump_target_flow_ml": target_ml,
        "pump_target_duty": final_duty,
        "pump_target_flow_estimate_ml": final_flow_ml,
        "pump_target_error_ml": (
            abs(final_flow_ml - target_ml) if final_flow_ml is not None else None
        ),
        "pump_target_bracket": {
            "low_duty": low_d,
            "low_flow_ml": low_f * 1000.0,
            "high_duty": high_d,
            "high_flow_ml": high_f * 1000.0 if high_f is not None else None,
        },
        "pump_target_points": points,
    }


@router.get("/lab/run")
def lab_run(
    led_ch:     int   = Query(0,      ge=0, le=2),
    led_duty:   int   = Query(128,    ge=0, le=255),
    led_hz:     int   = Query(40000,  ge=10, le=200000),
    pump_duty:  int   = Query(0,      ge=0, le=255),
    target_flow_ml: Optional[float] = Query(None, ge=0.0, le=1000.0),
    pump_hz:    int   = Query(PUMP_PWM_FREQ, ge=10, le=200000),
    duration_s: float = Query(10.0,   gt=0.1, le=120.0),
    flow:       int   = Query(0,      ge=0, le=1),
    raw:        int   = Query(1,      ge=0, le=1),
):
    if _adc is None or _pi is None:
        raise HTTPException(500, "lab dependencies not wired")

    if not _lab_lock.acquire(blocking=False):
        raise HTTPException(409, "lab already running")

    try:
        # Pause sampling — measure loop checks state.sampling at cycle top
        was_sampling = bool(getattr(_state, "sampling", False))
        if was_sampling:
            _state.sampling = False
            # give engine loop a moment to reach check point
            time.sleep(0.3)

        # All LEDs off, then drive target channel
        for ch in range(3):
            _pi.set_PWM_dutycycle(LED_PINS[ch], 0)
        # Explicit high PWM range -> higher duty resolution than pigpio's
        # default 255 at high frequencies (at 40 kHz + -s 1 sample rate, default
        # resolution is only 25 levels, causing duty quantisation artefacts).
        _pi.set_PWM_range(LED_PINS[led_ch], LED_PWM_RANGE)
        actual_led_hz = _pi.set_PWM_frequency(LED_PINS[led_ch], led_hz)
        # Scale 0..255 API value onto the active LED PWM range.
        scaled_duty = led_duty_to_pwm(led_duty)
        _pi.set_PWM_dutycycle(LED_PINS[led_ch], scaled_duty)

        # Pump: use kernel HW PWM (dtoverlay=pwm-2chan) if available; fall
        # back to pigpio.  After the overlay is enabled GPIO 12 is locked by
        # kernel PWM and pigpio writes are no-ops.
        hw_pump = _hw_pump_get()
        actual_pump_hz = _set_pump_pwm(hw_pump, pump_hz, pump_duty)
        target_info = {}
        if target_flow_ml is not None and target_flow_ml > 0:
            pump_duty, target_info = _find_pump_duty_for_flow(
                hw_pump, pump_hz, target_flow_ml / 1000.0, pump_duty
            )
            actual_pump_hz = _set_pump_pwm(hw_pump, pump_hz, pump_duty)

        # Warm-up: 1 s gives LED junction thermal + pump spin-up enough time
        # after PWM freq/range reconfiguration to reach steady state.
        time.sleep(1.0)

        # Capture loop — polls ADC as fast as possible
        pairs = []
        flows = []
        flow_enabled = bool(flow) or bool(target_info)
        t0 = time.monotonic()
        t_end = t0 + duration_s
        while time.monotonic() < t_end:
            s = _adc.read_sensor()
            r = _adc.read_reference()
            pairs.append((s, r))
            if flow_enabled:
                f = _read_flow_lpm()
                if f is not None:
                    flows.append(f)
        t_end_actual = time.monotonic()
        wall = t_end_actual - t0

        # Cleanup — restore production LED PWM settings so the measure engine's
        # public 0..255 duty values map through the same scaler as on boot.
        _pi.set_PWM_dutycycle(LED_PINS[led_ch], 0)
        _pi.set_PWM_range(LED_PINS[led_ch], LED_PWM_RANGE)
        _pi.set_PWM_frequency(LED_PINS[led_ch], LED_PWM_FREQ)
        if hw_pump is not None:
            hw_pump.change_duty_cycle(0)
        else:
            _set_pump_pwm(hw_pump, PUMP_PWM_FREQ, 0)

        if was_sampling:
            _state.sampling = True

        # Stats
        sens = [p[0] for p in pairs]
        refs = [p[1] for p in pairs]
        n = len(pairs)
        sen_m = sum(sens)/n if n else 0.0
        ref_m = sum(refs)/n if n else 0.0
        sen_sd = statistics.pstdev(sens) if n > 1 else 0.0
        ref_sd = statistics.pstdev(refs) if n > 1 else 0.0
        ratio_m = sen_m / ref_m if ref_m > 0 else 0.0
        ratio_sd = 0.0
        if n > 1 and ref_m > 0:
            ratios = [s/r for s, r in pairs if r > 0]
            if ratios:
                ratio_sd = statistics.pstdev(ratios)

        resp = {
            "ok": True,
            "n": n,
            "wall_s": wall,
            "pair_rate": n / wall if wall > 0 else 0.0,
            "led_ch": led_ch,
            "led_duty": led_duty,
            "led_hz_requested": led_hz,
            "led_hz_actual": actual_led_hz,
            "pump_duty": pump_duty,
            "pump_hz_requested": pump_hz,
            "pump_hz_actual": actual_pump_hz,
            "flow_enabled": flow_enabled,
            "flow_adc_limit_ml": _flow_adc_limit_ml(),
            "sen_mean": sen_m,
            "ref_mean": ref_m,
            "sen_rms_uV": sen_sd * 1e6,
            "ref_rms_uV": ref_sd * 1e6,
            "ratio_mean": ratio_m,
            "ratio_rms_ppm": (ratio_sd / ratio_m * 1e6) if ratio_m > 0 else 0.0,
        }
        resp.update(target_info)
        resp.update(_flow_stats(flows))
        if raw:
            resp["pairs"] = pairs
            if flow_enabled:
                resp["flows_lpm"] = flows
        return JSONResponse(resp)
    except Exception as e:
        logger.exception("lab_run failed: %s", e)
        # Best-effort cleanup
        try:
            for ch in range(3):
                _pi.set_PWM_dutycycle(LED_PINS[ch], 0)
            _set_pump_pwm(_hw_pump_get(), PUMP_PWM_FREQ, 0)
        except Exception:
            pass
        if _state and getattr(_state, "sampling", None) is not None:
            try:
                _state.sampling = True
            except Exception:
                pass
        raise HTTPException(500, f"lab error: {e}")
    finally:
        _lab_lock.release()


@router.get("/lab/info")
async def lab_info():
    return {
        "led_pins": {ch: LED_PINS[ch] for ch in range(3)},
        "pump_pin": PUMP_PIN,
        "adc_type": _adc.type if _adc else None,
        "adc_vref": _adc.vref if _adc else None,
        "flow_adc_limit_ml": _flow_adc_limit_ml(),
        "pigpio_connected": _pi.connected if _pi else False,
    }
