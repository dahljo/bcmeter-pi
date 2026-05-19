"""Configuration management with JSON persistence.

Matches the ESP32 CfgStore pattern while staying backward-compatible
with the existing bcMeter_config.json format.
"""

import json
import os
import logging
import re
import threading

logger = logging.getLogger("bcmeter.config")

# Type constants matching ESP32 CfgStore
T_BOOL = "boolean"
T_INT = "number"
T_FLOAT = "number"
T_STRING = "string"
T_ARRAY = "array"


class CfgEntry:
    __slots__ = ("key", "description", "group", "type", "default")

    def __init__(self, key, description, group, cfg_type, default):
        self.key = key
        self.description = description
        self.group = group
        self.type = cfg_type
        self.default = default


# Registry of all config entries, matching ESP32 cfgstore.cpp entries[]
# Groups: session, device, email, dev:optical, dev:pump, dev:afc, dev:hardware, dev:system
REGISTRY = [
    # --- session ---
    CfgEntry("sample_time", "Time between samples (s)", "session", T_INT, 300),
    CfgEntry("automatic_airflow_control", "Auto-adjust airflow by loading", "session", T_BOOL, False),
    CfgEntry("airflow_per_minute", "Airflow per minute (L)", "session", T_FLOAT, 0.25),
    CfgEntry("autostart_logging", "Auto-start logging after boot", "session", T_BOOL, False),
    CfgEntry("mobile_sampling", "Mobile measurement mode (log GPS per sample)", "session", T_BOOL, False),
    CfgEntry("outdoor_measurement", "Outdoor measurement — please disable when taking indoor or emission control measurements", "session", T_BOOL, True),
    CfgEntry("location_mode", "Location mode (0=off,1=auto,2=manual)", "session", T_INT, 1),
    CfgEntry("location_lat", "Manual latitude", "session", T_FLOAT, 0.0),
    CfgEntry("location_lon", "Manual longitude", "session", T_FLOAT, 0.0),
    # --- device ---
    CfgEntry("disable_led", "Disable blinking LED", "device", T_BOOL, False),
    CfgEntry("warmup_seconds", "Warm-up time (s)", "device", T_INT, 600),
    CfgEntry("timezone", "Timezone", "device", T_STRING, "UTC0"),
    CfgEntry("show_undervoltage_warning", "Display undervoltage warnings", "device", T_BOOL, False),
    # --- calibration (dev-only) ---
    CfgEntry("sample_spot_diameter", "Sample spot diameter (cm)", "dev:calibration", T_FLOAT, 0.4),
    CfgEntry("filter_scattering_factor", "Filter scattering factor", "dev:calibration", T_FLOAT, 1.39),
    CfgEntry("device_specific_correction_factor", "Device correction factor", "dev:calibration", T_FLOAT, 1.0),
    CfgEntry("ap_secured", "Protect hotspot with password", "device", T_BOOL, False),
    CfgEntry("ap_password", "Hotspot password", "device", T_STRING, "bcMeterbcMeter"),
    CfgEntry("disable_pump_control", "Disable internal pump control", "device", T_BOOL, False),
    CfgEntry("airflow_sensor", "Airflow sensor available", "device", T_BOOL, True),
    CfgEntry("af_sensor_type", "Airflow sensor type (0=P0001A1, 1=P0010A2)", "device", T_INT, 1),
    CfgEntry("run_hotspot", "Hotspot mode", "device", T_BOOL, False),
    # --- email ---
    CfgEntry("send_log_by_mail", "Periodically send logs by mail", "email", T_BOOL, False),
    CfgEntry("mail_logs_to", "Email address(es) for logs", "email", T_STRING, "your@email.address"),
    CfgEntry("filter_status_mail", "Mail on filter change suggestion", "email", T_BOOL, False),
    CfgEntry("mail_sending_interval", "Send logs every X hours", "email", T_FLOAT, 24.0),
    CfgEntry("share_with_bcmeter", "Share data with bcMeter team", "email", T_BOOL, False),
    CfgEntry("send_verbose_emails", "Send verbose diagnostic emails", "email", T_BOOL, False),
    # --- dev:optical ---
    CfgEntry("led_duty_cycle_370nm", "370nm UV LED brightness", "dev:optical", T_INT, 255),
    CfgEntry("led_duty_cycle_520nm", "520nm Green LED brightness", "dev:optical", T_INT, 255),
    CfgEntry("led_duty_cycle_880nm", "880nm IR LED brightness", "dev:optical", T_INT, 100),
    CfgEntry("cal_k_370nm", "Calibration factor 370nm", "dev:optical", T_FLOAT, 1.0),
    CfgEntry("cal_k_520nm", "Calibration factor 520nm", "dev:optical", T_FLOAT, 1.0),
    CfgEntry("cal_k_880nm", "Calibration factor 880nm", "dev:optical", T_FLOAT, 1.0),
    CfgEntry("shadow_factor", "Weingartner shadow factor f (1.1 diesel, 1.2 urban, 1.5 biomass)", "dev:optical", T_FLOAT, 1.2),
    CfgEntry("adc_low_limit", "Min ADC voltage before error (V)", "dev:optical", T_FLOAT, 0.5),
    # --- dev:pump ---
    CfgEntry("pump_dutycycle", "Pump power (0-255)", "dev:pump", T_INT, 20),
    CfgEntry("max_pump_duty", "Maximum pump duty cycle", "dev:pump", T_INT, 255),
    CfgEntry("min_pump_duty", "Minimum pump duty cycle", "dev:pump", T_INT, 0),
    CfgEntry("min_airflow_ml", "Minimum airflow (ml/min)", "dev:pump", T_INT, 70),
    # pwm_freq removed — hardcoded to 48 Hz in pump.py to avoid ADC aliasing
    CfgEntry("twelvevolt_duty", "12V pump duty cycle", "dev:pump", T_INT, 20),
    CfgEntry("reverse_dutycycle", "Reverse pump duty cycle", "dev:pump", T_BOOL, False),
    CfgEntry("TWELVEVOLT_ENABLE", "Enable 12V power output", "dev:pump", T_BOOL, False),
    CfgEntry("log_pump_duty", "Log pump duty cycle per sample in CSV", "dev:pump", T_BOOL, False),
    # --- dev:afc ---
    CfgEntry("afc_bc_low", "AFC: BC below this = max flow (ng)", "dev:afc", T_FLOAT, 300.0),
    CfgEntry("afc_bc_high", "AFC: BC above this = min flow (ng)", "dev:afc", T_FLOAT, 2000.0),
    CfgEntry("afc_flow_low", "AFC: min airflow (LPM)", "dev:afc", T_FLOAT, 0.05),
    CfgEntry("afc_flow_high", "AFC: max airflow (LPM)", "dev:afc", T_FLOAT, 0.7),
    CfgEntry("afc_slope_gain", "AFC: flow boost per 100ng/min descent", "dev:afc", T_FLOAT, 0.05),
    # --- dev:hardware ---
    CfgEntry("ambient_pressure_correction", "Correct BC for ambient pressure", "dev:calibration", T_BOOL, True),
    CfgEntry("enable_wifi", "Enable WiFi", "dev:hardware", T_BOOL, True),
    CfgEntry("iot_enable", "4G/IOT connectivity", "dev:hardware", T_BOOL, False),
    CfgEntry("swap_channels", "Swap data channels", "dev:hardware", T_BOOL, False),
    CfgEntry("spi_vref", "Reference voltage for SPI ADC", "dev:hardware", T_FLOAT, 4.096),
    CfgEntry("use_display", "Device has display", "dev:hardware", T_BOOL, False),
    # --- dev:system ---
    CfgEntry("num_channels", "Number of channels", "dev:system", T_INT, 1),
    CfgEntry("is_ebcMeter", "Direct emission measurement mode", "dev:system", T_BOOL, False),
    CfgEntry("device_name", "Device name", "dev:system", T_STRING, "bcMeter"),
    CfgEntry("email_api_key", "Email service API key", "dev:system", T_STRING, ""),
    CfgEntry("email_service_password", "Legacy email service API key", "dev:system", T_STRING, "email_service_password"),
    CfgEntry("onboarding_step_one", "Phase 1 onboarding complete (WiFi+email)", "dev:system", T_BOOL, False),
    CfgEntry("onboarding_step_two", "Phase 2 onboarding complete (cal+sharing)", "dev:system", T_BOOL, False),
    CfgEntry("team_upload_interval", "Cloud sync interval (hours, gated by share_with_bcmeter)", "dev:system", T_FLOAT, 1.0),
    CfgEntry("bcmeter_team_email", "bcMeter team contact email", "dev:system", T_STRING, ""),
    CfgEntry("heating", "Enable heating", "dev:system", T_BOOL, False),
    CfgEntry("last_cal_time", "Last calibration timestamp", "dev:system", T_STRING, "never"),
    # --- session: BC filter ---
    CfgEntry("filter_days", "Target filter lifetime (days)", "session", T_INT, 7),
    CfgEntry("bc_filter", "BC smoothing (median3, ema, kalman)", "session", T_STRING, "median3"),
]

_REGISTRY_MAP = {e.key: e for e in REGISTRY}
_SECRET_PLACEHOLDERS = {"", "configured", "email_service_password", "your_api_key", "iot_api_key"}
DEVICE_NAME_CUSTOM_KEY = "device_name_custom"
_AUTO_DEVICE_NAME_RE = re.compile(r"^bcMeter-[0-9A-Fa-f]{4}$")


def _hidden_entry(value, description=""):
    return {
        "value": value,
        "description": description,
        "type": T_BOOL if isinstance(value, bool) else T_STRING,
        "parameter": "hidden",
    }


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _wifi_mac_suffix() -> str:
    try:
        with open("/sys/class/net/wlan0/address", "r") as f:
            mac = f.read().strip()
        octets = mac.split(":")
        if len(octets) == 6:
            return (octets[-2] + octets[-1]).upper()
    except Exception:
        pass
    return ""


def _is_real_secret(value) -> bool:
    return str(value or "").strip() not in _SECRET_PLACEHOLDERS


class CfgStore:
    """Configuration store with JSON file persistence.

    Thread-safe. Backward-compatible with existing bcMeter_config.json format.
    """

    def __init__(self, path: str):
        self._path = path
        self._data: dict = {}  # key -> {value, description, type, parameter}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        """Load config from JSON file, merging with registry defaults."""
        migrated = False
        # Start with defaults from registry
        for entry in REGISTRY:
            self._data[entry.key] = {
                "value": entry.default,
                "description": entry.description,
                "type": entry.type,
                "parameter": entry.group,
            }
        self._data[DEVICE_NAME_CUSTOM_KEY] = _hidden_entry(
            False, "Device name was explicitly set by the user"
        )

        # Override with values from file
        custom_marker_present = False
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    file_data = json.load(f)
                for key, obj in file_data.items():
                    if key == DEVICE_NAME_CUSTOM_KEY:
                        custom_marker_present = True
                    if isinstance(obj, dict) and "value" in obj:
                        if key in self._data:
                            self._data[key]["value"] = obj["value"]
                        else:
                            # Preserve unknown keys from file
                            self._data[key] = obj
            except Exception as e:
                logger.error(f"Failed to load config from {self._path}: {e}")

        # One-time migration from old credential names to the single canonical
        # key. Runtime code should only read email_api_key after this point.
        email_key = self._data.get("email_api_key", {}).get("value", "")
        if not _is_real_secret(email_key):
            for legacy_key in ("email_service_password", "iot_api_key"):
                legacy_entry = self._data.get(legacy_key, {})
                legacy_value = legacy_entry.get("value") if isinstance(legacy_entry, dict) else legacy_entry
                if _is_real_secret(legacy_value):
                    self._data["email_api_key"]["value"] = str(legacy_value).strip()
                    migrated = True
                    logger.info("Migrated %s to email_api_key", legacy_key)
                    break
        if not _is_real_secret(self._data.get("email_api_key", {}).get("value", "")):
            if self._data["email_api_key"]["value"] != "":
                self._data["email_api_key"]["value"] = ""
                migrated = True

        if self._data.get("email_service_password", {}).get("value") != "email_service_password":
            self._data["email_service_password"]["value"] = "email_service_password"
            migrated = True
        if "iot_api_key" in self._data:
            self._data.pop("iot_api_key", None)
            migrated = True

        # Derive device_name from WiFi MAC (last 2 bytes) only while the
        # device still uses automatic naming. Once a user renames the device,
        # device_name_custom disables this boot-time auto rename permanently.
        current_name = str(
            self._data.get("device_name", {}).get("value", "bcMeter") or "bcMeter"
        ).strip()
        hw_suffix = _wifi_mac_suffix()
        expected_auto_name = f"bcMeter-{hw_suffix}" if hw_suffix else ""

        if not custom_marker_present:
            # Migrate existing configs: a name that is neither the bare default
            # nor this hardware's exact auto name is treated as intentional.
            inferred_custom = (
                current_name != "bcMeter" and current_name != expected_auto_name
            )
            self._data[DEVICE_NAME_CUSTOM_KEY]["value"] = inferred_custom
            if inferred_custom:
                migrated = True

        device_name_custom = _truthy(
            self._data.get(DEVICE_NAME_CUSTOM_KEY, {}).get("value", False)
        )
        needs_rename = False
        if not device_name_custom:
            if current_name == "bcMeter":
                needs_rename = True
            elif hw_suffix and _AUTO_DEVICE_NAME_RE.match(current_name):
                # Auto-generated name from a different device's MAC.
                needs_rename = current_name != expected_auto_name

        if needs_rename and hw_suffix:
            self._data["device_name"]["value"] = expected_auto_name
            migrated = True

        if migrated:
            self.save()

    def save(self):
        """Persist current config to JSON file."""
        with self._lock:
            tmp = self._path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(self._data, f, indent=4)
                os.replace(tmp, self._path)
            except Exception as e:
                logger.error(f"Failed to save config: {e}")
                if os.path.exists(tmp):
                    os.remove(tmp)

    def get(self, key: str, default=None):
        """Get the value of a config key."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            return entry.get("value", default)

    def get_float(self, key: str, default: float = 0.0) -> float:
        val = self.get(key, default)
        try:
            return float(str(val).replace(",", "."))
        except (ValueError, TypeError):
            return default

    def get_int(self, key: str, default: int = 0) -> int:
        val = self.get(key, default)
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    def get_string(self, key: str, default: str = "") -> str:
        val = self.get(key, default)
        return str(val) if val is not None else default

    def set(self, key: str, value):
        """Set a config value. Creates entry if it doesn't exist."""
        with self._lock:
            if key in self._data:
                self._data[key]["value"] = value
            else:
                # Infer type
                if isinstance(value, bool):
                    t = T_BOOL
                elif isinstance(value, (int, float)):
                    t = T_INT
                elif isinstance(value, list):
                    t = T_ARRAY
                else:
                    t = T_STRING
                reg = _REGISTRY_MAP.get(key)
                self._data[key] = {
                    "value": value,
                    "description": reg.description if reg else "",
                    "type": t,
                    "parameter": reg.group if reg else "hidden",
                }

    def set_float(self, key: str, value: float):
        self.set(key, value)

    def set_int(self, key: str, value: int):
        self.set(key, value)

    def set_bool(self, key: str, value: bool):
        self.set(key, value)

    def set_string(self, key: str, value: str):
        self.set(key, value)

    def set_device_name(self, value: str, custom: bool = True):
        self.set_string("device_name", str(value))
        self.set_bool(DEVICE_NAME_CUSTOM_KEY, bool(custom))

    def to_json(self) -> str:
        """Return full config as JSON string (for /api/config GET)."""
        with self._lock:
            return json.dumps(self._data)

    def to_flat_dict(self) -> dict:
        """Return {key: value} dict (convenience for internal use)."""
        with self._lock:
            return {k: v["value"] for k, v in self._data.items()}

    def apply_json(self, json_str: str) -> bool:
        """Apply config from JSON string (for /api/config POST).

        Accepts either {key: value} or {key: {value: ...}} format.
        """
        try:
            incoming = json.loads(json_str) if isinstance(json_str, str) else json_str
        except (json.JSONDecodeError, TypeError):
            return False

        with self._lock:
            incoming_device_name = None
            device_name_seen = False
            custom_marker_seen = False
            for key, val in incoming.items():
                if isinstance(val, dict) and "value" in val:
                    val = val["value"]
                if key == "device_name":
                    incoming_device_name = val
                    device_name_seen = True
                elif key == DEVICE_NAME_CUSTOM_KEY:
                    custom_marker_seen = True
                if key in self._data:
                    self._data[key]["value"] = val
                else:
                    # Unknown key — store it
                    reg = _REGISTRY_MAP.get(key)
                    if isinstance(val, bool):
                        t = T_BOOL
                    elif isinstance(val, (int, float)):
                        t = T_INT
                    elif isinstance(val, list):
                        t = T_ARRAY
                    else:
                        t = T_STRING
                    self._data[key] = {
                        "value": val,
                        "description": reg.description if reg else "",
                        "type": t,
                        "parameter": reg.group if reg else "hidden",
                    }
            if device_name_seen and not custom_marker_seen:
                self._data[DEVICE_NAME_CUSTOM_KEY]["value"] = (
                    str(incoming_device_name or "").strip() != "bcMeter"
                )

        self.save()
        return True

    def keys(self):
        with self._lock:
            return list(self._data.keys())
