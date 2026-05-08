"""Raspberry Pi on-device QC runner.

Run directly on the Pi, normally via:

    /home/bcmeter/venv/bin/python3 /home/bcmeter/bcmeter-qc.py

The default runner talks to the live local bcMeter API. The service keeps
ownership of GPIO, ADC, pump, optics, WiFi, and hotspot management while the QC
script starts platform-adapted lab captures, prints a compact result table, and
writes a full JSON report.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .adc import ADC
from .bme280 import BME280
from .config import CfgStore
from .gps import GPS
from .optics import LED_PINS, LED_PWM_FREQ, LED_PWM_RANGE, WAVELENGTH_NAMES, led_duty_to_pwm
from .optics import Optics
from .pump import PUMP_PIN, PUMP_PWM_FREQ, flow_limit_lpm, pump_duty_to_hardware, voltage_to_flow
from .pump import Pump
from .qc_html import render_qc_html
from .sps30 import SPS30
from .state import state

BASE_DIR = Path("/home/bcmeter") if Path("/home/bcmeter").is_dir() else Path("/home/pi")
REPORT_ROOT = BASE_DIR / "maintenance_logs"
STREAM_LED_PWM_HZ = 40000
DEFAULT_API_BASES = ("http://127.0.0.1", "http://127.0.0.1:8080")

STANDARD_BONUS_TARGETS = (30, 50)
STANDARD_REQUIRED_TARGETS = (100, 200, 400, 600)
STANDARD_DURATION_S = 5
STANDARD_REQUIRED_TOLERANCE_ML = 60.0
STANDARD_BONUS_TOLERANCE_ML = 25.0
STANDARD_SIGMA_MAX_ML = 80.0

QUICK_BONUS_TARGETS = (30, 50)
QUICK_REQUIRED_TARGETS = (100, 300, 600)
QUICK_DURATION_S = 3
QUICK_REQUIRED_TOLERANCE_ML = 80.0
QUICK_BONUS_TOLERANCE_ML = 25.0


@dataclass
class Step:
    name: str
    passed: bool
    hard: bool = True
    details: dict[str, Any] = field(default_factory=dict)
    observed: str = ""


class ApiQcRunner:
    """Runs QC through the live local bcMeter API.

    This is the normal Pi QC path. It keeps bcMeter.service running, so the
    service-owned WiFi manager and all hardware drivers remain in one process.
    """

    def __init__(self, profile: str = "standard", out_dir: Optional[Path] = None,
                 api_base: Optional[str] = None, calibrate: bool = True,
                 send_email: bool = True, factory_reset: bool = False,
                 wipe_wifi: bool = False, reboot_after_reset: bool = False,
                 progress_cb: Optional[Callable[[dict[str, Any]], None]] = None) -> None:
        self.profile_name = profile
        self.profile = profile_spec(profile)
        self.out_dir = out_dir or REPORT_ROOT / f"qc-pi-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.api_bases = (
            (api_base.rstrip("/"),) if api_base else DEFAULT_API_BASES
        )
        self.api_base: Optional[str] = None
        self.calibrate = calibrate
        self.send_email = send_email
        self.factory_reset = factory_reset
        self.wipe_wifi = wipe_wifi
        self.reboot_after_reset = reboot_after_reset
        self.progress_cb = progress_cb
        self.steps: list[Step] = []
        self.summary: dict[str, Any] = {}

    def run(self) -> dict[str, Any]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._progress("running", "Starting system check")
        if self.factory_reset:
            self._progress("running", "Applying factory reset option")
            self._run_factory_reset()
            if self.reboot_after_reset:
                return self._finalize()
        self._progress("running", "Collecting preflight data")
        status, lab, system, config = self._collect_preflight()
        i2c = self._read_i2c_bus()

        self._add_step("ADC", bool(status.get("adc")), {
            "adc": status.get("adc"),
            "adc_type": lab.get("adc_type") or status.get("adc_type"),
        }, observed=f"{lab.get('adc_type') or status.get('adc_type') or 'missing'}")
        self._add_step("pigpio", bool(lab.get("pigpio_connected")), {
            "pigpio_connected": lab.get("pigpio_connected"),
        }, observed=str(lab.get("pigpio_connected")))
        sht4x_ok, sht4x_observed = _sht4x_present(status, i2c)
        self._add_step("SHT4x", sht4x_ok, {
            "sht4x": status.get("sht4x"),
            "i2cdetect": i2c,
        }, observed=sht4x_observed)

        adc_type = str(lab.get("adc_type") or status.get("adc_type") or "")
        if adc_type == "i2c":
            self._add_step("MCP342x", "68" in i2c, {"i2cdetect": i2c},
                           observed="0x68" if "68" in i2c else "missing")

        self._add_step("WiFi STA", status.get("wifi_mode") == "sta", {
            "wifi_mode": status.get("wifi_mode"),
            "wifi_ssid": status.get("wifi_ssid"),
            "internet": status.get("internet"),
        }, observed=f"{status.get('wifi_mode')} {status.get('wifi_ssid') or ''}".strip())
        self._add_step("Time", bool(status.get("time_synced")) and _sane_time(system), {
            "time_synced": status.get("time_synced"),
            "device_time": system.get("time"),
        }, observed=str(system.get("time", "")))
        self._add_step("SPS30", bool(status.get("sps30")), {"sps30": status.get("sps30")},
                       hard=False, observed=_present(status.get("sps30")))
        self._add_step("BME280", bool(status.get("bme280")), {"bme280": status.get("bme280")},
                       hard=False, observed=_present(status.get("bme280")))
        self._add_step("GPS", bool(status.get("gps")), {"gps": status.get("gps")},
                       hard=False, observed=_present(status.get("gps")))
        self._add_step("4G modem", bool(system.get("modem")), {
            "modem": system.get("modem"),
            "modem_signal": system.get("modem_signal"),
            "modem_operator": system.get("modem_operator"),
        }, hard=False, observed=_present(system.get("modem")))

        if self.calibrate:
            self._progress("running", "Running calibration")
            self._run_calibration()
            try:
                config = self._api_json("/api/config")
                self._write_json("config_after_calibration.json", config)
                self.summary["last_cal"] = _cfg_json_value(config, "last_cal_time")
            except Exception:
                pass
        else:
            self.summary["calibration"] = {"skipped": True}
            self._progress("running", "Calibration skipped")
        self._progress("running", "Checking optical channels")
        self._run_optics(config)
        self._progress("running", "Checking pump plateaus")
        self._run_pump(lab)
        self._progress("running", "Verifying final state")
        self._run_final_verification()
        self._progress("running", "Writing report")
        return self._finalize()

    def _collect_preflight(self) -> tuple[dict[str, Any], dict[str, Any],
                                          dict[str, Any], dict[str, Any]]:
        status = self._api_json("/api/status")
        lab = self._api_json("/api/lab/info")
        system = self._api_json("/api/system")
        try:
            config = self._api_json("/api/config")
        except Exception as exc:
            config = {"_error": str(exc)}

        self._write_json("status.json", status)
        self._write_json("labinfo.json", lab)
        self._write_json("system.json", system)
        self._write_json("config.json", config)

        self.summary.update({
            "device": status.get("name") or system.get("device_name") or "bcMeter",
            "version": status.get("version", ""),
            "profile": self.profile_name,
            "api_base": self.api_base,
            "adc_type": lab.get("adc_type") or status.get("adc_type"),
            "adc_vref": lab.get("adc_vref"),
            "flow_adc_limit_ml": lab.get("flow_adc_limit_ml"),
            "last_cal": status.get("last_cal") or system.get("last_cal"),
            "optional": {
                "gps": status.get("gps"),
                "sps30": status.get("sps30"),
                "bme280": status.get("bme280"),
                "modem": system.get("modem"),
            },
        })
        return status, lab, system, config

    def _run_factory_reset(self) -> None:
        if os.geteuid() != 0:
            raise RuntimeError("--factory-reset must be run as root, e.g. sudo python3 bcmeter-qc.py --factory-reset --factory-reset-confirm FACTORY_RESET")

        reset_details: dict[str, Any] = {
            "preserve_wifi": not self.wipe_wifi,
            "reboot_after_reset": self.reboot_after_reset,
            "removed": [],
            "preserved": [],
        }
        try:
            config = self._api_json("/api/config", timeout=10)
            self._write_json("config_pre_factory_reset.json", config)
        except Exception as exc:
            reset_details["snapshot_error"] = str(exc)

        try:
            self._api_text("/api/control", params={"action": "stop"}, timeout=10)
            reset_details["sampling_stop"] = "OK"
        except Exception as exc:
            reset_details["sampling_stop_error"] = str(exc)

        for name in ("bcMeter_config.json", "calibration_data.json", "bcMeter_mobile_status.json"):
            path = BASE_DIR / name
            if path.exists():
                path.unlink()
                reset_details["removed"].append(str(path))

        if self.wipe_wifi:
            wifi_path = BASE_DIR / "bcMeter_wifi.json"
            if wifi_path.exists():
                wifi_path.unlink()
                reset_details["removed"].append(str(wifi_path))
            nm_dir = Path("/etc/NetworkManager/system-connections")
            if nm_dir.is_dir():
                for item in nm_dir.iterdir():
                    if item.is_file() and item.name != "bcmeter-eth.nmconnection":
                        try:
                            item.unlink()
                            reset_details["removed"].append(str(item))
                        except Exception as exc:
                            reset_details.setdefault("remove_errors", []).append(f"{item}: {exc}")
        else:
            reset_details["preserved"].append(str(BASE_DIR / "bcMeter_wifi.json"))
            reset_details["preserved"].append("/etc/NetworkManager/system-connections")

        for dirname in ("logs", "outbox"):
            self._clean_dir(BASE_DIR / dirname, reset_details)
        (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "logs" / "log_current.csv").touch(exist_ok=True)

        if _has_systemd():
            cmd = ["systemctl", "restart", "bcMeter.service"]
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
            reset_details["service_restart"] = {
                "returncode": proc.returncode,
                "stdout": proc.stdout[-500:],
                "stderr": proc.stderr[-500:],
            }
            if proc.returncode != 0:
                raise RuntimeError(f"bcMeter.service restart failed: {proc.stderr.strip()}")

        if self.reboot_after_reset:
            reset_details["reboot_requested"] = True
            self._write_json("factory_reset.json", reset_details)
            self._add_step("Factory reset option", True, reset_details,
                           observed="reset applied, reboot requested")
            subprocess.Popen(["reboot"])
            return

        for _ in range(45):
            try:
                status = self._api_json("/api/status", timeout=5)
                reset_details["post_reset_status"] = status
                break
            except Exception:
                time.sleep(2.0)
        self._write_json("factory_reset.json", reset_details)
        self._add_step(
            "Factory reset option",
            "post_reset_status" in reset_details,
            reset_details,
            observed="reset applied, WiFi preserved" if not self.wipe_wifi else "reset applied, WiFi wiped",
        )

    def _clean_dir(self, path: Path, details: dict[str, Any]) -> None:
        if not path.is_dir():
            return
        for item in path.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                details["removed"].append(str(item))
            except Exception as exc:
                details.setdefault("remove_errors", []).append(f"{item}: {exc}")

    def _read_i2c_bus(self) -> str:
        for cmd in (["sudo", "-n", "i2cdetect", "-y", "1"], ["i2cdetect", "-y", "1"]):
            try:
                proc = subprocess.run(cmd, text=True, capture_output=True, timeout=20)
                output = (proc.stdout or "") + (proc.stderr or "")
                if proc.returncode == 0:
                    self._write_json("i2cdetect.json", {"command": cmd, "output": output})
                    return output
            except Exception:
                continue
        self._write_json("i2cdetect.json", {"output": ""})
        return ""

    def _run_optics(self, config: dict[str, Any]) -> None:
        num_channels = max(1, min(3, _cfg_json_int(config, "num_channels", 1)))
        self.summary["num_channels"] = num_channels

        warmup_duty = _cfg_json_int(config, "led_duty_cycle_880nm", 128)
        self._lab_capture("led_warmup_discard.json", led_ch=0, led_duty=warmup_duty,
                          pump_duty=0, duration_s=3, collect_flow=False)
        optics = []
        for ch in range(num_channels):
            wl_name = WAVELENGTH_NAMES[ch]
            led_duty = _cfg_json_int(config, f"led_duty_cycle_{wl_name}", 128)
            result = self._lab_capture(
                f"led_ch{ch}.json",
                led_ch=ch,
                led_duty=led_duty,
                pump_duty=0,
                duration_s=5,
                collect_flow=False,
            )
            ratio_ppm = float(result.get("ratio_rms_ppm") or 1e9)
            sen = float(result.get("sen_mean") or 0.0)
            ref = float(result.get("ref_mean") or 0.0)
            passed = sen > 0.01 and ref > 0.01 and ratio_ppm < 30000
            observed = f"sen={sen:.4f} ref={ref:.4f} cv={ratio_ppm:.0f}ppm"
            details = {
                "channel": ch,
                "sen_mean": sen,
                "ref_mean": ref,
                "led_duty": led_duty,
                "ratio_rms_ppm": ratio_ppm,
                "samples": result.get("n"),
            }
            self._add_step(f"LED ch{ch}", passed, details, observed=observed)
            optics.append(details | {"passed": passed})
        self.summary["optics"] = optics

    def _run_pump(self, lab: dict[str, Any]) -> None:
        self._lab_capture("pump_zero.json", led_ch=0, led_duty=128,
                          pump_duty=0, duration_s=4, collect_flow=True)
        adc_clipped = (
            str(lab.get("adc_type") or "") == "i2c"
            and 0 < float(lab.get("adc_vref") or 0.0) <= 2.1
        )
        flow_limit_ml = float(lab.get("flow_adc_limit_ml") or 0.0)

        pump_results = []
        targets = list(self.profile["bonus_targets"]) + list(self.profile["required_targets"])
        for target in targets:
            kind = "bonus" if target in self.profile["bonus_targets"] else "hard"
            if kind == "hard" and target > flow_limit_ml + self.profile["required_tolerance_ml"]:
                if adc_clipped:
                    kind = "ceiling"
            result = self._lab_capture(
                f"pump_plateau_{target}ml.json",
                led_ch=0,
                led_duty=128,
                pump_duty=0,
                target_flow_ml=float(target),
                duration_s=self.profile["duration_s"],
                collect_flow=True,
            )
            passed = self._evaluate_pump(result, target, kind, flow_limit_ml)
            flow = result.get("flow_mean_ml")
            sigma = result.get("flow_sigma_ml")
            duty = result.get("pump_duty")
            observed = f"flow={_fmt(flow)} sigma={_fmt(sigma)} duty={duty}"
            if kind == "ceiling":
                observed += f" adc_limit={flow_limit_ml:.1f}"
            details = {
                "target_ml": target,
                "kind": kind,
                "flow_mean_ml": flow,
                "flow_sigma_ml": sigma,
                "duty": duty,
                "flow_adc_limit_ml": flow_limit_ml,
                "target_info": {
                    key: result.get(key)
                    for key in (
                        "pump_target_flow_ml",
                        "pump_target_duty",
                        "pump_target_flow_estimate_ml",
                        "pump_target_error_ml",
                        "pump_target_bracket",
                        "pump_target_points",
                    )
                    if key in result
                },
            }
            self._add_step(f"Pump {target} ml/min", passed, details,
                           hard=(kind == "hard"), observed=observed)
            pump_results.append(details | {"passed": passed})
        self.summary["pump"] = pump_results

    def _run_calibration(self) -> None:
        try:
            started = self._api_text(
                "/api/control",
                params={"action": "calibrate"},
                timeout=10,
                allow_status={202},
            )
            cal_state: dict[str, Any] = {}
            for _ in range(120):
                time.sleep(2.0)
                cal_state = self._api_json("/api/calibration", timeout=10)
                cal_msg = cal_state.get("log") or cal_state.get("message") or "Calibration running"
                self._progress("running", str(cal_msg)[-300:])
                if cal_state.get("done") and not cal_state.get("running"):
                    break

            config_after = self._api_json("/api/config", timeout=10)
            details = {
                "start_response": started,
                "state": cal_state,
                "last_cal_time": _cfg_json_value(config_after, "last_cal_time"),
                "cal_k_880nm": _cfg_json_value(config_after, "cal_k_880nm"),
                "cal_k_520nm": _cfg_json_value(config_after, "cal_k_520nm"),
                "cal_k_370nm": _cfg_json_value(config_after, "cal_k_370nm"),
            }
            ok = bool(cal_state.get("done")) and bool(cal_state.get("ok"))
            observed = (
                f"ok={ok} last_cal={details['last_cal_time']} "
                f"k880={_fmt(details['cal_k_880nm'])}"
            )
            self._write_json("calibration.json", details)
            self.summary["calibration"] = details
            self._add_step("Calibration", ok, details, observed=observed)
        except Exception as exc:
            details = {"error": str(exc)}
            self._write_json("calibration.json", details)
            self.summary["calibration"] = details
            self._add_step("Calibration", False, details, observed=str(exc))

    def _run_final_verification(self) -> None:
        try:
            status = self._api_json("/api/status", timeout=10)
            system = self._api_json("/api/system", timeout=10)
            self._write_json("final_status.json", status)
            self._write_json("final_system.json", system)

            service = {}
            if _has_systemd():
                proc = subprocess.run(
                    ["systemctl", "show", "bcMeter.service", "--property=ActiveState,SubState,ActiveEnterTimestamp", "--no-pager"],
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                for line in proc.stdout.splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        service[key] = value
            final_state = {"status": status, "system": system, "service": service}
            self._write_json("final_state.json", final_state)
            self.summary["final"] = {
                "status": status.get("status"),
                "init_msg": status.get("init_msg"),
                "last_cal": status.get("last_cal"),
                "service": service,
            }

            idle = int(status.get("status") or 0) == 0 and not bool(status.get("session"))
            self._add_step("Final state idle", idle, {
                "status": status.get("status"),
                "init_msg": status.get("init_msg"),
                "session": status.get("session"),
            }, observed=str(status.get("init_msg") or status.get("status")))
            last_cal = status.get("last_cal")
            cal_timestamp_ok = last_cal not in (None, "", "never")
            if self.calibrate:
                self._add_step("Calibration timestamp persisted", cal_timestamp_ok, {
                    "last_cal": last_cal,
                }, observed=str(last_cal))
            else:
                self._add_step("Existing calibration timestamp", cal_timestamp_ok, {
                    "last_cal": last_cal,
                    "calibration_skipped": True,
                }, hard=False, observed=str(last_cal))
            if service:
                self._add_step("bcMeter service active after QC", service.get("ActiveState") == "active", {
                    "service": service,
                }, observed=service.get("ActiveEnterTimestamp", ""))
        except Exception as exc:
            details = {"error": str(exc)}
            self._write_json("final_state.json", details)
            self._add_step("Final state verification", False, details, observed=str(exc))

    def _evaluate_pump(self, result: dict[str, Any], target: int, kind: str,
                       flow_limit_ml: float) -> bool:
        flow = result.get("flow_mean_ml")
        if flow is None or not math.isfinite(float(flow)):
            return False
        if kind == "ceiling":
            margin = max(25.0, flow_limit_ml * 0.06)
            return flow_limit_ml > 0 and float(flow) >= flow_limit_ml - margin

        tolerance = (
            self.profile["bonus_tolerance_ml"]
            if kind == "bonus" else self.profile["required_tolerance_ml"]
        )
        sigma = result.get("flow_sigma_ml")
        stable = True
        sigma_limit = self.profile.get("sigma_max_ml")
        if sigma_limit is not None:
            stable = sigma is not None and float(sigma) <= float(sigma_limit)
        return abs(float(flow) - target) <= tolerance and float(flow) >= 15.0 and stable

    def _lab_capture(self, filename: str, led_ch: int, led_duty: int,
                     pump_duty: int, duration_s: float, collect_flow: bool,
                     target_flow_ml: Optional[float] = None) -> dict[str, Any]:
        if target_flow_ml is not None:
            self._progress("running", f"Pump plateau {target_flow_ml:.0f} ml/min")
        elif filename.startswith("led_ch"):
            self._progress("running", f"Optical capture {filename}")
        elif filename.startswith("led_warmup"):
            self._progress("running", "Optical warmup")
        elif filename.startswith("pump_zero"):
            self._progress("running", "Pump zero-flow baseline")
        params: dict[str, Any] = {
            "led_ch": led_ch,
            "led_duty": led_duty,
            "led_hz": STREAM_LED_PWM_HZ,
            "pump_duty": pump_duty,
            "pump_hz": PUMP_PWM_FREQ,
            "duration_s": duration_s,
            "flow": 1 if collect_flow else 0,
            "raw": 0,
        }
        if target_flow_ml is not None:
            params["target_flow_ml"] = target_flow_ml
        result = self._api_json("/api/lab/run", params=params, timeout=duration_s + 45)
        self._write_json(filename, result)
        return result

    def _api_json(self, path: str, params: Optional[dict[str, Any]] = None,
                  timeout: float = 10.0) -> dict[str, Any]:
        body = self._api_text(path, params=params, timeout=timeout)
        return json.loads(body or "{}")

    def _api_text(self, path: str, params: Optional[dict[str, Any]] = None,
                  timeout: float = 10.0,
                  allow_status: Optional[set[int]] = None) -> str:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        bases = (self.api_base,) if self.api_base else self.api_bases
        last_error = None
        for base in [b for b in bases if b]:
            url = f"{base}{path}{query}"
            try:
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    status = getattr(resp, "status", 200)
                    body = resp.read().decode("utf-8", errors="replace")
                    if allow_status is not None and status not in allow_status:
                        raise RuntimeError(f"{url}: HTTP {status} {body}")
                self.api_base = base
                return body
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                if allow_status is not None and exc.code in allow_status:
                    self.api_base = base
                    return body
                last_error = RuntimeError(f"{url}: HTTP {exc.code} {body}")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"bcMeter API unavailable for {path}: {last_error}")

    def _add_step(self, name: str, passed: bool, details: dict[str, Any],
                  hard: bool = True, observed: str = "") -> None:
        step = Step(name=name, passed=passed, hard=hard,
                    details=details, observed=observed)
        self.steps.append(step)
        self._progress(
            "step",
            name,
            step={
                "name": step.name,
                "passed": step.passed,
                "hard": step.hard,
                "observed": step.observed,
            },
        )

    def _progress(self, event: str, message: str = "", **data: Any) -> None:
        if not self.progress_cb:
            return
        payload = {"event": event, "message": message, **data}
        try:
            self.progress_cb(payload)
        except Exception:
            pass

    def _write_json(self, name: str, data: Any) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / name).write_text(json.dumps(data, indent=2, default=str))

    def _write_text(self, name: str, data: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / name
        path.write_text(data, encoding="utf-8")
        return path

    def _send_report_email(self, report: dict[str, Any]) -> dict[str, Any]:
        if not self.send_email:
            return {"attempted": False, "reason": "disabled"}
        try:
            from . import email_handler
            ok, err = email_handler.send_qc_final_report(report)
            return {"attempted": bool(ok or err), "sent": ok, "error": err}
        except Exception as exc:
            return {"attempted": True, "sent": False, "error": str(exc)}

    def _finalize(self) -> dict[str, Any]:
        hard_failures = [s for s in self.steps if s.hard and not s.passed]
        html_path = self.out_dir / "qc_report.html"
        report = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "profile": self.profile_name,
            "mode": "api",
            "passed": not hard_failures,
            "report_dir": str(self.out_dir),
            "html_report": str(html_path),
            "hard_failures": [s.name for s in hard_failures],
            "steps": [
                {
                    "name": s.name,
                    "passed": s.passed,
                    "hard": s.hard,
                    "observed": s.observed,
                    "details": s.details,
                }
                for s in self.steps
            ],
            "summary": self.summary,
        }
        report["summary"]["qc_email"] = self._send_report_email(report)
        html = render_qc_html(report)
        self._write_text("qc_report.html", html)
        self._write_json("qc_report.json", report)
        latest = REPORT_ROOT / "qc-pi-latest.json"
        latest_html = REPORT_ROOT / "qc-pi-latest.html"
        try:
            latest.write_text(json.dumps(report, indent=2, default=str))
            latest_html.write_text(html, encoding="utf-8")
        except PermissionError as exc:
            report["latest_write_error"] = str(exc)
            self._write_json("qc_report.json", report)
        return report


class HardwareQcRunner:
    """Runs QC with direct hardware objects."""

    def __init__(self, cfg, state_mgr, pi, adc, optics, pump,
                 profile: str = "standard", out_dir: Optional[Path] = None,
                 wifi_snapshot: Optional[dict[str, Any]] = None) -> None:
        self.cfg = cfg
        self.state = state_mgr
        self.pi = pi
        self.adc = adc
        self.optics = optics
        self.pump = pump
        self.profile_name = profile
        self.profile = profile_spec(profile)
        self.out_dir = out_dir or REPORT_ROOT / f"qc-pi-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.wifi_snapshot = wifi_snapshot
        self.steps: list[Step] = []
        self.summary: dict[str, Any] = {}
        self._hw_pump = None

    def run(self) -> dict[str, Any]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        status, lab, system = self._collect_preflight()
        i2c = self._read_i2c_bus()

        self._add_step("ADC", bool(status.get("adc")), {
            "adc": status.get("adc"),
            "adc_type": lab.get("adc_type"),
        }, observed=f"{lab.get('adc_type') or 'missing'}")
        self._add_step("pigpio", bool(lab.get("pigpio_connected")), {
            "pigpio_connected": lab.get("pigpio_connected"),
        }, observed=str(lab.get("pigpio_connected")))
        sht4x_ok, sht4x_observed = _sht4x_present(status, i2c)
        self._add_step("SHT4x", sht4x_ok, {
            "sht4x": status.get("sht4x"),
            "i2cdetect": i2c,
        }, observed=sht4x_observed)
        self._add_step("WiFi STA", status.get("wifi_mode") == "sta", {
            "wifi_mode": status.get("wifi_mode"),
            "wifi_ssid": status.get("wifi_ssid"),
            "internet": status.get("internet"),
        }, observed=f"{status.get('wifi_mode')} {status.get('wifi_ssid') or ''}".strip())
        self._add_step("Time", bool(status.get("time_synced")) and _sane_time(system), {
            "time_synced": status.get("time_synced"),
            "device_time": system.get("time"),
        }, observed=str(system.get("time", "")))
        self._add_step("SPS30", bool(status.get("sps30")), {"sps30": status.get("sps30")},
                       hard=False, observed=_present(status.get("sps30")))
        self._add_step("BME280", bool(status.get("bme280")), {"bme280": status.get("bme280")},
                       hard=False, observed=_present(status.get("bme280")))
        self._add_step("GPS", bool(status.get("gps")), {"gps": status.get("gps")},
                       hard=False, observed=_present(status.get("gps")))

        self._run_optics()
        self._run_pump(lab)
        return self._finalize()

    def _collect_preflight(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        from bcmeter import __version__

        snap = self.state.snapshot() if self.state else {}
        device_name = self.cfg.get_string("device_name", "bcMeter") if self.cfg else "bcMeter"
        status = {
            "name": device_name,
            "version": __version__,
            "adc": bool(snap.get("adc_present")),
            "adc_type": snap.get("adc_type", ""),
            "gps": bool(snap.get("gps_present")),
            "sht4x": bool(snap.get("sht4x_present")),
            "sps30": bool(snap.get("sps30_present")),
            "bme280": bool(snap.get("bme280_present")),
            **(self.wifi_snapshot or _wifi_status()),
            "time_synced": datetime.now().year > 2024,
        }
        lab = {
            "adc_type": self.adc.type if self.adc else "",
            "adc_vref": self.adc.vref if self.adc else 0.0,
            "flow_adc_limit_ml": self._flow_adc_limit_ml(),
            "pigpio_connected": bool(self.pi and self.pi.connected),
        }
        system = {
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        self._write_json("status.json", status)
        self._write_json("labinfo.json", lab)
        self._write_json("system.json", system)
        if self.cfg:
            try:
                self._write_json("config.json", json.loads(self.cfg.to_json()))
            except Exception:
                pass

        self.summary.update({
            "device": status["name"],
            "version": status["version"],
            "profile": self.profile_name,
            "adc_type": lab["adc_type"],
            "adc_vref": lab["adc_vref"],
            "flow_adc_limit_ml": lab["flow_adc_limit_ml"],
            "optional": {
                "gps": status["gps"],
                "sps30": status["sps30"],
                "bme280": status["bme280"],
            },
        })
        return status, lab, system

    def _read_i2c_bus(self) -> str:
        for cmd in (["sudo", "-n", "i2cdetect", "-y", "1"], ["i2cdetect", "-y", "1"]):
            try:
                proc = subprocess.run(cmd, text=True, capture_output=True, timeout=20)
                output = (proc.stdout or "") + (proc.stderr or "")
                if proc.returncode == 0:
                    self._write_json("i2cdetect.json", {"command": cmd, "output": output})
                    return output
            except Exception:
                continue
        self._write_json("i2cdetect.json", {"output": ""})
        return ""

    def _run_optics(self) -> None:
        num_channels = 1
        if self.cfg:
            try:
                num_channels = int(self.cfg.get_int("num_channels", 1))
            except Exception:
                num_channels = 1
        num_channels = max(1, min(3, num_channels))
        self.summary["num_channels"] = num_channels

        self._lab_capture("led_warmup_discard.json", led_ch=0, led_duty=128,
                          pump_duty=0, duration_s=3, collect_flow=False)
        optics = []
        for ch in range(num_channels):
            result = self._lab_capture(
                f"led_ch{ch}.json",
                led_ch=ch,
                led_duty=128,
                pump_duty=0,
                duration_s=5,
                collect_flow=False,
            )
            ratio_ppm = float(result.get("ratio_rms_ppm") or 1e9)
            sen = float(result.get("sen_mean") or 0.0)
            ref = float(result.get("ref_mean") or 0.0)
            passed = sen > 0.01 and ref > 0.01 and ratio_ppm < 30000
            observed = f"sen={sen:.4f} ref={ref:.4f} cv={ratio_ppm:.0f}ppm"
            details = {
                "channel": ch,
                "sen_mean": sen,
                "ref_mean": ref,
                "ratio_rms_ppm": ratio_ppm,
                "samples": result.get("n"),
            }
            self._add_step(f"LED ch{ch}", passed, details, observed=observed)
            optics.append(details | {"passed": passed})
        self.summary["optics"] = optics

    def _run_pump(self, lab: dict[str, Any]) -> None:
        self._lab_capture("pump_zero.json", led_ch=0, led_duty=128,
                          pump_duty=0, duration_s=4, collect_flow=True)
        adc_clipped = (
            str(lab.get("adc_type") or "") == "i2c"
            and 0 < float(lab.get("adc_vref") or 0.0) <= 2.1
        )
        flow_limit_ml = float(lab.get("flow_adc_limit_ml") or 0.0)

        pump_results = []
        targets = list(self.profile["bonus_targets"]) + list(self.profile["required_targets"])
        for target in targets:
            kind = "bonus" if target in self.profile["bonus_targets"] else "hard"
            if kind == "hard" and target > flow_limit_ml + self.profile["required_tolerance_ml"]:
                if adc_clipped:
                    kind = "ceiling"
            duty, target_info = self._find_pump_duty_for_flow(target)
            result = self._lab_capture(
                f"pump_plateau_{target}ml.json",
                led_ch=0,
                led_duty=128,
                pump_duty=duty,
                duration_s=self.profile["duration_s"],
                collect_flow=True,
                extra=target_info,
            )
            passed = self._evaluate_pump(result, target, kind, flow_limit_ml)
            flow = result.get("flow_mean_ml")
            sigma = result.get("flow_sigma_ml")
            observed = f"flow={_fmt(flow)} sigma={_fmt(sigma)} duty={duty}"
            if kind == "ceiling":
                observed += f" adc_limit={flow_limit_ml:.1f}"
            details = {
                "target_ml": target,
                "kind": kind,
                "flow_mean_ml": flow,
                "flow_sigma_ml": sigma,
                "duty": duty,
                "flow_adc_limit_ml": flow_limit_ml,
                "target_info": target_info,
            }
            self._add_step(f"Pump {target} ml/min", passed, details,
                           hard=(kind == "hard"), observed=observed)
            pump_results.append(details | {"passed": passed})
        self.summary["pump"] = pump_results

    def _evaluate_pump(self, result: dict[str, Any], target: int, kind: str,
                       flow_limit_ml: float) -> bool:
        flow = result.get("flow_mean_ml")
        if flow is None or not math.isfinite(float(flow)):
            return False
        if kind == "ceiling":
            margin = max(25.0, flow_limit_ml * 0.06)
            return flow_limit_ml > 0 and float(flow) >= flow_limit_ml - margin

        tolerance = (
            self.profile["bonus_tolerance_ml"]
            if kind == "bonus" else self.profile["required_tolerance_ml"]
        )
        sigma = result.get("flow_sigma_ml")
        stable = True
        sigma_limit = self.profile.get("sigma_max_ml")
        if sigma_limit is not None:
            stable = sigma is not None and float(sigma) <= float(sigma_limit)
        return abs(float(flow) - target) <= tolerance and float(flow) >= 15.0 and stable

    def _lab_capture(self, filename: str, led_ch: int, led_duty: int,
                     pump_duty: int, duration_s: float, collect_flow: bool,
                     extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if self.adc is None or self.pi is None:
            raise RuntimeError("QC hardware dependencies are not wired")
        was_sampling = bool(getattr(self.state, "sampling", False))
        if was_sampling:
            self.state.sampling = False
            time.sleep(0.3)

        hw_pump = self._hw_pump_get()
        try:
            for ch in range(3):
                self.pi.set_PWM_dutycycle(LED_PINS[ch], 0)
            self.pi.set_PWM_range(LED_PINS[led_ch], LED_PWM_RANGE)
            actual_led_hz = self.pi.set_PWM_frequency(LED_PINS[led_ch], STREAM_LED_PWM_HZ)
            self.pi.set_PWM_dutycycle(LED_PINS[led_ch], led_duty_to_pwm(led_duty))
            actual_pump_hz = self._set_pump_pwm(hw_pump, PUMP_PWM_FREQ, pump_duty)
            time.sleep(1.0)

            pairs = []
            flows = []
            start = time.monotonic()
            end = start + duration_s
            while time.monotonic() < end:
                s = self.adc.read_sensor()
                r = self.adc.read_reference()
                pairs.append((s, r))
                if collect_flow:
                    f = self._read_flow_lpm()
                    if f is not None:
                        flows.append(f)
            wall = time.monotonic() - start
        finally:
            try:
                self.pi.set_PWM_dutycycle(LED_PINS[led_ch], 0)
                self.pi.set_PWM_range(LED_PINS[led_ch], LED_PWM_RANGE)
                self.pi.set_PWM_frequency(LED_PINS[led_ch], LED_PWM_FREQ)
                self._set_pump_pwm(hw_pump, PUMP_PWM_FREQ, 0)
                self.pi.set_PWM_range(PUMP_PIN, 255)
            finally:
                if was_sampling:
                    self.state.sampling = True

        sens = [p[0] for p in pairs]
        refs = [p[1] for p in pairs]
        n = len(pairs)
        sen_m = sum(sens) / n if n else 0.0
        ref_m = sum(refs) / n if n else 0.0
        ratios = [s / r for s, r in pairs if r > 0]
        ratio_m = sum(ratios) / len(ratios) if ratios else 0.0
        ratio_sd = _pstdev(ratios)
        result = {
            "ok": True,
            "n": n,
            "wall_s": wall,
            "pair_rate": n / wall if wall > 0 else 0.0,
            "led_ch": led_ch,
            "led_duty": led_duty,
            "led_hz_actual": actual_led_hz,
            "pump_duty": pump_duty,
            "pump_hz_actual": actual_pump_hz,
            "sen_mean": sen_m,
            "ref_mean": ref_m,
            "ratio_mean": ratio_m,
            "ratio_rms_ppm": (ratio_sd / ratio_m * 1e6) if ratio_m > 0 else 0.0,
        }
        result.update(extra or {})
        result.update(_flow_stats(flows))
        self._write_json(filename, result)
        return result

    def _find_pump_duty_for_flow(self, target_ml: int) -> tuple[int, dict[str, Any]]:
        target_lpm = target_ml / 1000.0
        hw_pump = self._hw_pump_get()
        d_min = _cfg_int(self.cfg, "min_pump_duty", 0)
        d_max = _cfg_int(self.cfg, "max_pump_duty", 255)
        points = []
        seen: set[int] = set()

        def probe(duty: int, settle_s: float = 0.45,
                  measure_s: float = 0.55) -> Optional[float]:
            duty = max(0, min(255, int(duty)))
            if duty in seen:
                return None
            seen.add(duty)
            self._set_pump_pwm(hw_pump, PUMP_PWM_FREQ, duty)
            time.sleep(settle_s)
            flow_lpm, samples = self._measure_flow_lpm(measure_s)
            points.append({
                "duty": duty,
                "flow_ml": flow_lpm * 1000.0 if flow_lpm is not None else None,
                "samples": len(samples),
            })
            return flow_lpm

        low_d, low_f = 0, 0.0
        high_d: Optional[int] = None
        high_f: Optional[float] = None
        candidates = list(range(d_min, d_max + 1, 20))
        if not candidates or candidates[-1] != d_max:
            candidates.append(d_max)
        for duty in sorted(set(candidates)):
            flow = probe(duty)
            if flow is None:
                continue
            if flow >= target_lpm:
                high_d, high_f = duty, flow
                break
            low_d, low_f = duty, flow

        if high_d is not None and high_d > low_d + 1:
            for _ in range(5):
                mid = (low_d + high_d) // 2
                flow = probe(mid, settle_s=0.35, measure_s=0.45)
                if flow is None:
                    break
                if flow >= target_lpm:
                    high_d, high_f = mid, flow
                else:
                    low_d, low_f = mid, flow

        best = None
        best_error = float("inf")
        for point in points:
            flow_ml = point.get("flow_ml")
            if flow_ml is None:
                continue
            error = abs(flow_ml - target_ml)
            if error < best_error:
                best = point
                best_error = error

        final_duty = int(best["duty"]) if best else d_min
        final_flow = best.get("flow_ml") if best else None
        return final_duty, {
            "target_flow_ml": target_ml,
            "pump_target_duty": final_duty,
            "pump_target_flow_estimate_ml": final_flow,
            "pump_target_error_ml": (
                abs(final_flow - target_ml) if final_flow is not None else None
            ),
            "pump_target_bracket": {
                "low_duty": low_d,
                "low_flow_ml": low_f * 1000.0,
                "high_duty": high_d,
                "high_flow_ml": high_f * 1000.0 if high_f is not None else None,
            },
            "pump_target_points": points,
        }

    def _hw_pump_get(self):
        if self._hw_pump is None:
            try:
                from rpi_hardware_pwm import HardwarePWM
                self._hw_pump = HardwarePWM(pwm_channel=0, hz=PUMP_PWM_FREQ, chip=0)
                self._hw_pump.start(0)
            except Exception:
                self._hw_pump = False
        return self._hw_pump if self._hw_pump is not False else None

    def _set_pump_pwm(self, hw_pump, pump_hz: int, pump_duty: int) -> int:
        duty = max(0, min(255, int(pump_duty)))
        if hw_pump is not None:
            hw_pump.change_frequency(pump_hz)
            hw_pump.change_duty_cycle(duty * 100.0 / 255.0)
            return pump_hz
        rc = self.pi.hardware_PWM(PUMP_PIN, pump_hz, pump_duty_to_hardware(duty))
        if rc == 0:
            return pump_hz
        self.pi.set_PWM_range(PUMP_PIN, 1000)
        actual_hz = self.pi.set_PWM_frequency(PUMP_PIN, pump_hz)
        self.pi.set_PWM_dutycycle(PUMP_PIN, int(duty * 1000 / 255))
        return actual_hz

    def _read_flow_lpm(self) -> Optional[float]:
        try:
            if self.pump is not None:
                value = float(self.pump.get_flow_lpm())
            elif self.adc is not None:
                value = float(voltage_to_flow(
                    self.adc.read_airflow_voltage(),
                    sensor_type=self.cfg.get_int("af_sensor_type", 1),
                ))
            else:
                return None
        except Exception:
            return None
        return max(0.0, value) if math.isfinite(value) else None

    def _measure_flow_lpm(self, duration_s: float) -> tuple[Optional[float], list[float]]:
        samples = []
        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            value = self._read_flow_lpm()
            if value is not None:
                samples.append(value)
            time.sleep(0.01)
        if not samples:
            return None, samples
        return sorted(samples)[len(samples) // 2], samples

    def _flow_adc_limit_ml(self) -> Optional[float]:
        if self.adc is None:
            return None
        try:
            return float(flow_limit_lpm(
                self.adc, self.cfg.get_int("af_sensor_type", 1)
            ) * 1000.0)
        except Exception:
            return None

    def _add_step(self, name: str, passed: bool, details: dict[str, Any],
                  hard: bool = True, observed: str = "") -> None:
        self.steps.append(Step(name=name, passed=passed, hard=hard,
                               details=details, observed=observed))

    def _write_json(self, name: str, data: Any) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / name).write_text(json.dumps(data, indent=2, default=str))

    def _finalize(self) -> dict[str, Any]:
        hard_failures = [s for s in self.steps if s.hard and not s.passed]
        report = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "profile": self.profile_name,
            "passed": not hard_failures,
            "report_dir": str(self.out_dir),
            "hard_failures": [s.name for s in hard_failures],
            "steps": [
                {
                    "name": s.name,
                    "passed": s.passed,
                    "hard": s.hard,
                    "observed": s.observed,
                    "details": s.details,
                }
                for s in self.steps
            ],
            "summary": self.summary,
        }
        self._write_json("qc_report.json", report)
        latest = REPORT_ROOT / "qc-pi-latest.json"
        try:
            latest.write_text(json.dumps(report, indent=2, default=str))
        except PermissionError as exc:
            report["latest_write_error"] = str(exc)
            self._write_json("qc_report.json", report)
        return report


def profile_spec(name: str) -> dict[str, Any]:
    if name == "standard":
        return {
            "bonus_targets": STANDARD_BONUS_TARGETS,
            "required_targets": STANDARD_REQUIRED_TARGETS,
            "duration_s": STANDARD_DURATION_S,
            "bonus_tolerance_ml": STANDARD_BONUS_TOLERANCE_ML,
            "required_tolerance_ml": STANDARD_REQUIRED_TOLERANCE_ML,
            "sigma_max_ml": STANDARD_SIGMA_MAX_ML,
        }
    if name == "quick":
        return {
            "bonus_targets": QUICK_BONUS_TARGETS,
            "required_targets": QUICK_REQUIRED_TARGETS,
            "duration_s": QUICK_DURATION_S,
            "bonus_tolerance_ml": QUICK_BONUS_TOLERANCE_ML,
            "required_tolerance_ml": QUICK_REQUIRED_TOLERANCE_ML,
            "sigma_max_ml": None,
        }
    raise ValueError(f"unknown profile: {name}")


class ServiceGuard:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.was_active = False

    def __enter__(self):
        if not self.enabled or not _has_systemd():
            return self
        self.was_active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "bcMeter.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        if self.was_active:
            subprocess.run(["systemctl", "stop", "bcMeter.service"], check=False)
            time.sleep(2.0)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.enabled and self.was_active and _has_systemd():
            subprocess.run(["systemctl", "restart", "bcMeter.service"], check=False)


def build_runner(profile: str, out_dir: Optional[Path],
                 wifi_snapshot: Optional[dict[str, Any]] = None) -> HardwareQcRunner:
    cfg = CfgStore(str(BASE_DIR / "bcMeter_config.json"))
    i2c_lock = threading.Lock()
    pi = _connect_pigpio()

    adc = ADC(i2c_lock)
    adc.detect(
        swap_channels=cfg.get_bool("swap_channels", False),
        spi_vref=cfg.get_float("spi_vref", 4.096),
    )
    state.update(adc_present=adc.present, adc_type=adc.type)

    optics = Optics(pi)
    if pi:
        optics.init(pi)

    pump = Pump(config=cfg, adc=adc)
    if pi:
        pump.init(pi)

    try:
        sps = SPS30(i2c_lock=i2c_lock)
        if sps.init():
            state.set("sps30_present", True)
    except Exception:
        pass
    try:
        bme = BME280(i2c_lock=i2c_lock)
        if bme.init():
            state.set("bme280_present", True)
    except Exception:
        pass
    try:
        gps = GPS()
        if gps.init():
            state.set("gps_present", True)
    except Exception:
        pass

    return HardwareQcRunner(
        cfg=cfg,
        state_mgr=state,
        pi=pi,
        adc=adc,
        optics=optics,
        pump=pump,
        profile=profile,
        out_dir=out_dir,
        wifi_snapshot=wifi_snapshot,
    )


def _connect_pigpio():
    try:
        import pigpio
    except Exception as exc:
        raise RuntimeError(f"pigpio module unavailable: {exc}") from exc

    pi = pigpio.pi("localhost", 8888)
    if pi.connected:
        return pi

    subprocess.run(["sudo", "killall", "pigpiod"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "pigpiod", "-l", "-m", "-x", "-1"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    pi = pigpio.pi("localhost", 8888)
    if not pi.connected:
        raise RuntimeError("pigpiod is not reachable")
    return pi


def _has_systemd() -> bool:
    return Path("/run/systemd/system").exists()


def _wifi_status() -> dict[str, Any]:
    ssid = ""
    rssi = 0
    try:
        ssid = subprocess.run(["iwgetid", "-r"], text=True, capture_output=True,
                              timeout=5).stdout.strip()
    except Exception:
        pass
    try:
        link = subprocess.run(["iw", "dev", "wlan0", "link"], text=True,
                              capture_output=True, timeout=5).stdout
        for line in link.splitlines():
            line = line.strip()
            if line.startswith("signal:"):
                rssi = int(float(line.split()[1]))
    except Exception:
        pass
    internet = subprocess.run(
        ["ping", "-c", "1", "-W", "2", "1.1.1.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    return {
        "wifi_mode": "sta" if ssid else "unknown",
        "wifi_ssid": ssid,
        "wifi_rssi": rssi,
        "internet": internet,
    }


def format_report_table(report: dict[str, Any]) -> str:
    rows = []
    for step in report.get("steps", []):
        if step.get("passed"):
            result = "PASS"
        elif step.get("hard"):
            result = "FAIL"
        else:
            result = "MISS"
        rows.append((step.get("name", ""), result, step.get("observed", "")))
    headers = ("Test", "Result", "Observed")
    widths = [
        max(len(headers[i]), *(len(str(row[i])) for row in rows)) if rows else len(headers[i])
        for i in range(3)
    ]
    line = "  ".join(headers[i].ljust(widths[i]) for i in range(3))
    sep = "  ".join("-" * widths[i] for i in range(3))
    body = ["  ".join(str(row[i]).ljust(widths[i]) for i in range(3)) for row in rows]
    status = "PASS" if report.get("passed") else "FAIL"
    return "\n".join([line, sep, *body, "", f"Overall: {status}"])


def run_cli(args: argparse.Namespace) -> int:
    if args.factory_reset and args.factory_reset_confirm != "FACTORY_RESET":
        print("Refusing factory reset: pass --factory-reset-confirm FACTORY_RESET", file=sys.stderr)
        return 2
    runner = ApiQcRunner(
        profile=args.profile,
        out_dir=args.out,
        api_base=args.api_base,
        calibrate=not args.skip_calibration,
        send_email=not args.no_email,
        factory_reset=args.factory_reset,
        wipe_wifi=args.wipe_wifi,
        reboot_after_reset=args.reboot_after_reset,
    )
    report = runner.run()
    print(format_report_table(report))
    print(f"\nJSON report: {report.get('report_dir')}/qc_report.json")
    print(f"HTML report: {report.get('html_report')}")
    email = report.get("summary", {}).get("qc_email", {})
    if email.get("sent"):
        print("QC email: sent")
    elif email.get("error") and email.get("error") != "No recipients/API key configured":
        print(f"QC email: not sent ({email.get('error')})")
    return 0 if report.get("passed") else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run on-device bcMeter Pi QC")
    parser.add_argument("--api-base", default=None,
                        help="Override local API base URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--profile", choices=["standard", "quick"],
                        default="standard")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output directory for JSON artifacts")
    parser.add_argument("--skip-calibration", action="store_true",
                        help="Skip the fresh-filter calibration step")
    parser.add_argument("--no-email", action="store_true",
                        help="Do not send the QCFinal email even when email is configured")
    parser.add_argument("--factory-reset", action="store_true",
                        help="Apply a Pi filesystem factory reset before QC; not enabled by default")
    parser.add_argument("--factory-reset-confirm", default="",
                        help="Must be FACTORY_RESET when --factory-reset is used")
    parser.add_argument("--wipe-wifi", action="store_true",
                        help="Also wipe WiFi credentials during --factory-reset")
    parser.add_argument("--reboot-after-reset", action="store_true",
                        help="Reboot after --factory-reset instead of continuing QC")
    return parser.parse_args(argv)


def _cfg_int(cfg, key: str, default: int) -> int:
    if cfg is None:
        return default
    try:
        return int(cfg.get_int(key, default))
    except Exception:
        return default


def _cfg_json_int(config: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(_cfg_json_value(config, key, default))
    except Exception:
        return default


def _cfg_json_value(config: dict[str, Any], key: str, default: Any = None) -> Any:
    raw = config.get(key, default)
    if isinstance(raw, dict):
        return raw.get("value", default)
    return raw


def _flow_stats(flows: list[float]) -> dict[str, Any]:
    if not flows:
        return {
            "flow_samples": 0,
            "flow_mean_ml": None,
            "flow_sigma_ml": None,
            "flow_min_ml": None,
            "flow_max_ml": None,
        }
    mean = sum(flows) / len(flows)
    sigma = _pstdev(flows)
    return {
        "flow_samples": len(flows),
        "flow_mean_lpm": mean,
        "flow_mean_ml": mean * 1000.0,
        "flow_sigma_lpm": sigma,
        "flow_sigma_ml": sigma * 1000.0,
        "flow_min_lpm": min(flows),
        "flow_min_ml": min(flows) * 1000.0,
        "flow_max_lpm": max(flows),
        "flow_max_ml": max(flows) * 1000.0,
    }


def _pstdev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _sane_time(system: dict[str, Any]) -> bool:
    value = str(system.get("time") or "")
    return any(str(year) in value for year in range(2025, 2035))


def _sht4x_present(status: dict[str, Any], i2c: str) -> tuple[bool, str]:
    if bool(status.get("sht4x")):
        return True, "present"
    if "44" in i2c:
        return True, "0x44"
    return False, "missing"


def _present(value: Any) -> str:
    return "present" if value else "optional/missing"


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}"
    except Exception:
        return str(value)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return run_cli(args)
    except Exception as exc:
        print(f"QC CLI error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
