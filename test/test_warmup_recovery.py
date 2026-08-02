import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bcmeter import ota_check
from bcmeter.config import CfgStore
from bcmeter.errors import ErrorCode, InitStep
from bcmeter.measure import (
    MeasureEngine,
    TEMP_LIMIT,
    TEMP_LIMIT_MAX,
    TEMP_LIMIT_MIN,
    TEMP_REARM_HYSTERESIS,
)
from bcmeter.state import state

try:
    from api import routes_control, routes_status
    _FASTAPI_AVAILABLE = True
except ModuleNotFoundError:
    routes_control = routes_status = None
    _FASTAPI_AVAILABLE = False


class _FakeState:
    def __init__(self, **values):
        self.values = dict(values)
        self.sampling = bool(values.get("sampling", False))

    def update(self, **values):
        self.values.update(values)
        if "sampling" in values:
            self.sampling = bool(values["sampling"])

    def set(self, key, value):
        self.values[key] = value
        if key == "sampling":
            self.sampling = bool(value)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def snapshot(self):
        snap = dict(self.values)
        snap["sampling"] = self.sampling
        return snap


class _FakeADC:
    present = False
    high_limit = 3.8


class _FakeConfig:
    @staticmethod
    def get_int(key, default=0):
        return 600 if key == "warmup_seconds" else default


class WarmupRecoveryTests(unittest.TestCase):
    def tearDown(self):
        state.update(
            sampling=False,
            error=ErrorCode.ERR_NONE,
            warning_msg="",
            init_step=InitStep.INIT_IDLE,
            warmup_started_monotonic=0.0,
            warmup_end_monotonic=0.0,
            warmup_progress=0,
        )

    @unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI test dependencies are not installed")
    def test_start_clears_stale_error_warning_and_warmup(self):
        fake = _FakeState(
            sampling=False,
            error=ErrorCode.ERR_ADC_SATURATED,
            warning_msg="Replace filter",
            warmup_started_monotonic=10.0,
            warmup_end_monotonic=20.0,
            warmup_progress=40,
        )
        with patch.object(routes_control, "_state", fake), patch.object(
            routes_control, "_cfg", None
        ), patch.object(routes_control, "_engine", None):
            response = routes_control._handle_start(force=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(fake.sampling)
        self.assertEqual(fake.get("error"), ErrorCode.ERR_NONE)
        self.assertEqual(fake.get("warning_msg"), "")
        self.assertEqual(fake.get("warmup_progress"), 0)
        self.assertEqual(fake.get("warmup_started_monotonic"), 0.0)

    @unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI test dependencies are not installed")
    def test_duplicate_start_preserves_active_warmup_state(self):
        fake = _FakeState(
            sampling=True,
            error=ErrorCode.ERR_NONE,
            warning_msg="Filter heavily loaded",
            warmup_started_monotonic=100.0,
            warmup_end_monotonic=700.0,
            warmup_progress=42,
        )
        with patch.object(routes_control, "_state", fake), patch.object(
            routes_control, "_cfg", None
        ), patch.object(routes_control, "_engine", None):
            response = routes_control._handle_start(force=True, indoor="1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"Sampling already running")
        self.assertTrue(fake.sampling)
        self.assertEqual(fake.get("warning_msg"), "Filter heavily loaded")
        self.assertEqual(fake.get("warmup_started_monotonic"), 100.0)
        self.assertEqual(fake.get("warmup_end_monotonic"), 700.0)
        self.assertEqual(fake.get("warmup_progress"), 42)

    def test_engine_clears_warning_for_autostart_before_preflight(self):
        engine = MeasureEngine(
            cfg=_FakeConfig(), adc=_FakeADC(), optics=object(), pump=object(),
            sensors={}, storage=object(),
        )
        state.update(sampling=True, warning_msg="Replace filter")
        with patch.object(
            engine, "_preflight_check", return_value=ErrorCode.ERR_LED_FAILURE
        ):
            engine._run_session(threading.Event())

        self.assertEqual(state.get("warning_msg"), "")
        self.assertEqual(state.get("error"), ErrorCode.ERR_LED_FAILURE)
        self.assertGreater(state.get("warmup_started_monotonic"), 0.0)
        self.assertEqual(state.get("warmup_progress"), 0)

    @unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI test dependencies are not installed")
    def test_status_reports_live_and_frozen_error_progress(self):
        live = _FakeState(
            sampling=True,
            error=0,
            init_step=int(InitStep.INIT_SETTLING),
            warmup_started_monotonic=100.0,
            warmup_end_monotonic=120.0,
            warmup_progress=0,
        )
        with patch.object(routes_status, "_state", live), patch.object(
            routes_status, "_cfg", None
        ), patch.object(routes_status, "_storage", None), patch.object(
            routes_status.time, "monotonic", return_value=110.0
        ):
            response = asyncio.run(routes_status.api_status())
        payload = json.loads(response.body)
        self.assertEqual(payload["warmup_progress"], 50)
        self.assertTrue(payload["warmup_started"])

        failed = _FakeState(
            sampling=False,
            error=int(ErrorCode.ERR_ADC_SATURATED),
            init_step=int(InitStep.INIT_IDLE),
            warmup_started_monotonic=100.0,
            warmup_end_monotonic=120.0,
            warmup_progress=35,
        )
        with patch.object(routes_status, "_state", failed), patch.object(
            routes_status, "_cfg", None
        ), patch.object(routes_status, "_storage", None), patch.object(
            routes_status.time, "monotonic", return_value=999.0
        ):
            response = asyncio.run(routes_status.api_status())
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], 3)
        self.assertEqual(payload["warmup_progress"], 35)

    def test_unknown_config_is_preserved_for_version_compatibility(self):
        extension_key = "future_extension_key"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                extension_key: {
                    "value": True,
                    "description": "newer-version setting",
                    "type": "boolean",
                    "parameter": "dev:system",
                }
            }))
            with patch("bcmeter.config._wifi_mac_suffix", return_value=""):
                cfg = CfgStore(str(path))
            self.assertIn(extension_key, cfg.keys())
            self.assertTrue(cfg.apply_json({extension_key: False}))
            self.assertFalse(cfg.get_bool(extension_key))
            self.assertIn(extension_key, json.loads(path.read_text()))

    def test_legacy_auto_prefix_is_normalized_but_custom_name_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "device_name": {"value": "XbcMeter-1234"},
            }))
            with patch("bcmeter.config._wifi_mac_suffix", return_value="ABCD"):
                cfg = CfgStore(str(path))
            self.assertEqual(cfg.get_string("device_name"), "bcMeter-ABCD")

            path.write_text(json.dumps({
                "device_name": {"value": "Workshop Sensor"},
            }))
            with patch("bcmeter.config._wifi_mac_suffix", return_value="ABCD"):
                cfg = CfgStore(str(path))
            self.assertEqual(cfg.get_string("device_name"), "Workshop Sensor")

    def test_semver_compares_double_digit_patch_numerically(self):
        self.assertTrue(ota_check._is_newer("v1.6.10", "1.6.9"))
        self.assertFalse(ota_check._is_newer("v1.6.9", "1.6.10"))

    def test_emergency_temperature_cutoff_defaults_to_65c(self):
        self.assertEqual(TEMP_LIMIT, 65.0)
        self.assertEqual(TEMP_LIMIT_MIN, 40.0)
        self.assertEqual(TEMP_LIMIT_MAX, 80.0)
        self.assertEqual(TEMP_REARM_HYSTERESIS, 2.0)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("bcmeter.config._wifi_mac_suffix", return_value=""):
                cfg = CfgStore(str(Path(tmp) / "config.json"))
            self.assertEqual(cfg.get_float("temperature_shutdown_c"), 65.0)
            entry = json.loads(cfg.to_json())["temperature_shutdown_c"]
            self.assertEqual(entry["parameter"], "dev:system")
            cfg.set_float("temperature_shutdown_c", 100.0)
            self.assertEqual(cfg.get_float("temperature_shutdown_c"), 80.0)
            self.assertTrue(cfg.apply_json({"temperature_shutdown_c": 20.0}))
            self.assertEqual(cfg.get_float("temperature_shutdown_c"), 40.0)

    def test_interface_uses_backend_progress_and_resets_filter_memory(self):
        interface = (
            Path(__file__).resolve().parents[1] / "interface" / "index.html"
        ).read_text()
        self.assertIn("+d.warmup_progress+'%'", interface)
        self.assertIn("var previousDeviceStatus=lastDeviceStatus", interface)
        self.assertIn(
            "chartData=[];filterPeak=-1;alertFilterShown=false;"
            "alertPumpShown=false;alertHumidityShown=false",
            interface,
        )
        self.assertIn(
            "filterPeak=-1;alertFilterShown=false;alertPumpShown=false",
            interface,
        )
        self.assertNotIn("Date.now()-warmupStartMs", interface)
        self.assertIn(
            "if(d.status==3&&d.error===3&&!alertPumpShown)", interface,
        )
        self.assertNotIn(
            "d.status==2&&!alertPumpShown&&d.flow<0.05", interface,
        )
        self.assertIn("item.key==='temperature_shutdown_c'", interface)
        self.assertIn("inp.min=40;inp.max=80;inp.step=.5", interface)


if __name__ == "__main__":
    unittest.main()
