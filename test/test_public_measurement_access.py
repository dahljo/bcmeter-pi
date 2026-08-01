import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bcmeter.storage import Storage

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import (
        routes_config,
        routes_control,
        routes_csv,
        routes_lab,
        routes_qc,
        routes_status,
        routes_wifi,
    )
    from bcmeter.config import CfgStore
    _FASTAPI_AVAILABLE = True
except ModuleNotFoundError:
    _FASTAPI_AVAILABLE = False


ROOT = Path(__file__).resolve().parents[1]


def _route_decorators(path: Path):
    tree = ast.parse(path.read_text())
    routes = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "router"
                and func.attr in {"get", "post", "delete"}
            ):
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                continue
            dependencies = next(
                (kw.value for kw in decorator.keywords if kw.arg == "dependencies"),
                None,
            )
            routes[(func.attr.upper(), decorator.args[0].value)] = dependencies
    return routes


def _dependency_text(dependency) -> str:
    return ast.unparse(dependency) if dependency is not None else ""


class PublicMeasurementAccessPolicyTest(unittest.TestCase):
    def test_measurement_reads_are_anonymous_and_delete_requires_local_intent(self):
        status = _route_decorators(ROOT / "api" / "routes_status.py")
        csv = _route_decorators(ROOT / "api" / "routes_csv.py")

        self.assertIsNone(status[("GET", "/status")])
        self.assertIsNone(csv[("GET", "/csv")])
        self.assertIsNone(csv[("GET", "/files")])
        self.assertIn(
            "require_local_write_access('files-delete')",
            _dependency_text(csv[("DELETE", "/files")]),
        )

        csv_source = (ROOT / "api" / "routes_csv.py").read_text()
        self.assertIn("_stream_file(filepath)", csv_source)
        self.assertNotIn("allowed_columns", csv_source)
        self.assertNotIn("project_csv", csv_source)
        interface = (ROOT / "interface" / "index.html").read_text()
        self.assertIn("deleteLogFile(f.name)", interface)
        self.assertIn("method:'DELETE'", interface)

    def test_active_log_cannot_be_deleted_but_closed_log_can(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(tmp)
            name = storage.start_session()
            self.assertFalse(storage.delete_log(name))
            storage.end_session()
            self.assertTrue(storage.delete_log(name))

    def test_public_health_reads_are_redacted_without_changing_mutation_auth(self):
        config = _route_decorators(ROOT / "api" / "routes_config.py")
        control = _route_decorators(ROOT / "api" / "routes_control.py")
        status = _route_decorators(ROOT / "api" / "routes_status.py")
        qc = _route_decorators(ROOT / "api" / "routes_qc.py")

        self.assertIsNone(config[("GET", "/config")])
        # Routine writes use origin/client-intent protection only. The public
        # Pi intentionally has no password/token/challenge infrastructure.
        self.assertIn(
            "require_local_write_access('config')",
            _dependency_text(config[("POST", "/config")]),
        )
        self.assertIn(
            "require_local_write_access('control')",
            _dependency_text(control[("GET", "/control")]),
        )
        self.assertIsNone(status[("GET", "/logs")])
        self.assertIsNone(status[("GET", "/maintenance-logs")])
        self.assertIsNone(status[("GET", "/debug_mobile/status")])
        self.assertIsNone(qc[("GET", "/qc/pi/report")])
        self.assertIsNone(qc[("GET", "/qc/pi/report.html")])
        self.assertIsNone(qc[("GET", "/qc/pi/status")])

        config_source = (ROOT / "api" / "routes_config.py").read_text()
        status_source = (ROOT / "api" / "routes_status.py").read_text()
        self.assertIn("_redact_anonymous_config", config_source)
        self.assertIn('data["wifi_ssid"] = ""', status_source)
        self.assertIn(
            '"incidents": []',
            status_source,
        )
        self.assertIn("Private diagnostics unavailable", status_source)

    def test_frontend_loads_graph_without_requesting_admin_password(self):
        interface = (ROOT / "interface" / "index.html").read_text()

        self.assertIn("function isPublicMeasurementRead", interface)
        self.assertIn("x.pathname==='/api/status'", interface)
        self.assertIn("x.pathname==='/api/csv'", interface)
        self.assertIn("x.pathname==='/api/files'", interface)
        self.assertIn("function measurementFetch", interface)
        self.assertIn("return window.fetch(input,init||{});", interface)
        self.assertIn("window.fetch=function", interface)
        self.assertIn("h.set('X-bcMeter-Client','ui')", interface)
        self.assertIn("setLocalXHRHeader(xhr)", interface)
        init_chart = interface.split("function initChart(){", 1)[1].split("}\n", 1)[0]
        self.assertIn("fetchCSV();", init_chart)
        self.assertIn("setInterval(fetchCSV,10000)", interface)
        self.assertIn("fetch('/api/config',{cache:'no-store'})", interface)

    def test_router_inventory_guards_all_available_mutations(self):
        expected_local = {
            "routes_config.py": [
                ("POST", "/config"),
                ("POST", "/device/rename"),
                ("POST", "/email/validate"),
                ("POST", "/ap-security"),
            ],
            "routes_control.py": [("GET", "/control")],
            "routes_csv.py": [("DELETE", "/files")],
            "routes_ota.py": [
                ("POST", "/ota/check"),
                ("POST", "/ota/skip"),
                ("POST", "/ota/apply"),
            ],
            "routes_update.py": [("POST", "/update")],
            "routes_wifi.py": [
                ("POST", "/wifi/scan/refresh"),
                ("POST", "/wifi"),
                ("POST", "/wifi/connect"),
                ("POST", "/wifi/delete"),
            ],
        }
        for filename, endpoints in expected_local.items():
            routes = _route_decorators(ROOT / "api" / filename)
            for endpoint in endpoints:
                self.assertIn(
                    "require_local_write_access",
                    _dependency_text(routes[endpoint]),
                    f"{filename} {endpoint}",
                )

        # No credential system exists in this public tree. Raw lab/QC/service
        # endpoints are therefore disabled, not downgraded to local-public.
        lab_source = (ROOT / "api" / "routes_lab.py").read_text()
        qc_source = (ROOT / "api" / "routes_qc.py").read_text()
        self.assertIn("private lab service unavailable", lab_source)
        self.assertIn("private QC service unavailable", qc_source)

    def test_csv_notes_are_machine_status_codes_not_user_annotations(self):
        api_source = "\n".join(
            path.read_text() for path in (ROOT / "api").glob("routes*.py")
        )
        self.assertNotIn('"/notes"', api_source)
        notes_source = (ROOT / "bcmeter" / "notes.py").read_text()
        self.assertIn("diagnostic codes", notes_source)
        self.assertNotIn("Request", notes_source)

@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI test dependencies are not installed")
class PublicReadRedactionApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name) / "logs"
        self.log_dir.mkdir()
        self.csv_name = "log_20260801_120000.csv"
        self.csv_text = (
            "bcmDate;bcmTime;BCngm3_880;GPS_lat;GPS_lon;PM2_5;SHT_humidity\n"
            "2026-08-01;12:00:00;123.4;52.500001;13.400001;4.2;51.0\n"
        )
        (self.log_dir / self.csv_name).write_text(self.csv_text)
        self.cfg = CfgStore(str(Path(self.tmp.name) / "config.json"))
        for key, value in {
            "mail_logs_to": "owner@example.invalid",
            "email_api_key": "existing-email-secret",
            "location_lat": 52.5,
            "location_lon": 13.4,
            "wifi_ssid": "Workshop Secret WiFi",
        }.items():
            self.cfg.set(key, value)
        self.cfg.save()

        class State:
            def __init__(self):
                self.values = {}

            def snapshot(self):
                return {
                    "wifi_ssid": "Workshop Secret WiFi",
                    "wifi_mode": "sta",
                    "wifi_rssi": -42,
                    "internet": True,
                    "gps_present": True,
                }

            def set(self, key, value):
                self.values[key] = value

            def get(self, key, default=None):
                return self.values.get(key, default)

        class Storage:
            session_active = False
            session_filename = None
            session_filepath = None
            csv_header_line = "bcmDate;bcmTime"

            def __init__(storage_self, log_dir, csv_name):
                storage_self.log_dir = str(log_dir)
                storage_self._csv_name = csv_name

            def list_logs(storage_self):
                path = Path(storage_self.log_dir) / storage_self._csv_name
                return [{
                    "name": storage_self._csv_name,
                    "size": path.stat().st_size,
                    "lines": 2,
                    "mtime": path.stat().st_mtime,
                    "active": False,
                }]

        class GPS:
            present = True

            @staticmethod
            def get_data():
                return SimpleNamespace(
                    valid=True,
                    satellites=8,
                    hdop=0.9,
                    lat=52.5,
                    lon=13.4,
                    altitude=42.0,
                    speed=0.0,
                )

        self.state = State()
        self.storage = Storage(self.log_dir, self.csv_name)
        routes_config.set_dependencies(self.cfg)
        routes_control.set_dependencies(
            self.cfg, self.state, engine=None, storage=self.storage,
        )
        routes_csv.set_dependencies(self.cfg, self.storage)
        routes_status.set_dependencies(
            self.cfg, self.state, self.storage, gps=GPS(), pump=None,
        )
        routes_wifi.set_dependencies(self.cfg, None)
        app = FastAPI()
        app.include_router(routes_config.router, prefix="/api")
        app.include_router(routes_control.router, prefix="/api")
        app.include_router(routes_csv.router, prefix="/api")
        app.include_router(routes_lab.router, prefix="/api")
        app.include_router(routes_qc.router, prefix="/api")
        app.include_router(routes_status.router, prefix="/api")
        app.include_router(routes_wifi.router, prefix="/api")
        self.client = TestClient(app)
        self.local_headers = {"X-bcMeter-Client": "ui"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_public_reads_hide_private_fields_but_keep_health_available(self):
        config = self.client.get("/api/config")
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.json()["mail_logs_to"]["value"], "configured")
        self.assertEqual(config.json()["location_lat"]["value"], 0.0)
        self.assertTrue(config.json()["mail_logs_to"]["redacted"])

        status = self.client.get("/api/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["wifi_ssid"], "")

        system = self.client.get("/api/system")
        self.assertEqual(system.status_code, 200)
        self.assertEqual(system.json()["ip"], "")
        self.assertEqual(system.json()["mac"], "")
        self.assertNotIn("gps_lat", system.json())

        logs = self.client.get("/api/logs")
        self.assertEqual(logs.status_code, 200)
        network = {item["k"]: item["v"] for item in logs.json()["network"]}
        self.assertEqual(network["SSID"], "redacted")
        self.assertEqual(network["IP"], "redacted")
        self.assertEqual(logs.json()["incidents"], [])

        scan = self.client.get("/api/wifi/scan")
        self.assertEqual(scan.status_code, 200)
        self.assertTrue(scan.json()["private"])
        self.assertEqual(scan.json()["networks"], [])

        wifi = self.client.get("/api/wifi/status")
        self.assertEqual(wifi.status_code, 200)
        self.assertEqual(wifi.json()["ssid"], "")
        self.assertEqual(wifi.json()["ip"], "")

        connection = self.client.get("/api/wifi/connect/status")
        self.assertEqual(connection.status_code, 200)
        self.assertEqual(connection.json()["ssid"], "")
        self.assertEqual(connection.json()["ip"], "")
        self.assertEqual(connection.json()["log"], "")

    def test_raw_private_diagnostics_are_not_anonymous(self):
        self.assertEqual(self.client.get("/api/maintenance-logs").status_code, 403)
        self.assertEqual(self.client.get("/api/debug_mobile/status").status_code, 403)

    def test_public_csv_streams_newest_log_without_projecting_optional_columns(self):
        response = self.client.get("/api/csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, self.csv_text)
        self.assertIn("GPS_lat;GPS_lon;PM2_5;SHT_humidity", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_passwordless_normal_writes_require_local_client_intent(self):
        missing = self.client.post("/api/config", json={"sample_time": 234})
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(
            self.client.get("/api/control?action=factory_reset").status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/wifi/scan?refresh=1").status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(f"/api/files?name={self.csv_name}").status_code,
            403,
        )
        cross_origin = self.client.post(
            "/api/config",
            json={"sample_time": 234},
            headers={
                "X-bcMeter-Client": "ui",
                "Origin": "https://attacker.example",
            },
        )
        self.assertEqual(cross_origin.status_code, 403)
        wrong_port = self.client.post(
            "/api/config",
            json={"sample_time": 234},
            headers={
                "X-bcMeter-Client": "ui",
                "Origin": "http://testserver:81",
            },
        )
        self.assertEqual(wrong_port.status_code, 403)
        malformed_port = self.client.post(
            "/api/config",
            json={"sample_time": 234},
            headers={
                "X-bcMeter-Client": "ui",
                "Origin": "http://testserver:not-a-port",
            },
        )
        self.assertEqual(malformed_port.status_code, 403)
        allowed = self.client.post(
            "/api/config",
            json={"sample_time": 234},
            headers={**self.local_headers, "Origin": "http://testserver"},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/api/wifi/scan?refresh=1", headers=self.local_headers,
            ).status_code,
            200,
        )

        control = self.client.get(
            "/api/control?action=clear_error", headers=self.local_headers,
        )
        self.assertEqual(control.status_code, 200)

        credential_write = self.client.post(
            "/api/wifi/connect",
            json={"ssid": "Workshop", "pass": "password123"},
            headers=self.local_headers,
        )
        self.assertEqual(credential_write.status_code, 503)

        self.assertEqual(
            self.client.get(
                "/api/lab/run", headers=self.local_headers,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/qc/pi/start", headers=self.local_headers,
            ).status_code,
            403,
        )

    def test_email_api_key_is_bounded_local_only_and_never_echoed(self):
        public = self.client.get("/api/config")
        self.assertEqual(public.json()["email_api_key"]["value"], "configured")
        self.assertNotIn("existing-email-secret", public.text)

        missing_intent = self.client.post(
            "/api/config", json={"email_api_key": "replacement-secret"},
        )
        self.assertEqual(missing_intent.status_code, 403)

        placeholder = self.client.post(
            "/api/config",
            json={"email_api_key": "configured"},
            headers=self.local_headers,
        )
        self.assertEqual(placeholder.status_code, 200)
        self.assertEqual(
            self.cfg.get_string("email_api_key", ""),
            "existing-email-secret",
        )

        replacement = "replacement-email-secret"
        saved = self.client.post(
            "/api/config",
            json={"email_api_key": replacement},
            headers=self.local_headers,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn(replacement, saved.text)
        self.assertEqual(self.cfg.get_string("email_api_key", ""), replacement)

        oversized = self.client.post(
            "/api/config",
            json={"email_api_key": "x" * 513},
            headers=self.local_headers,
        )
        self.assertEqual(oversized.status_code, 413)

        candidate = "candidate-email-secret"
        with mock.patch(
            "bcmeter.email_handler.validate_api_key",
            return_value=(False, f"invalid credential {candidate}"),
        ):
            validation = self.client.post(
                "/api/email/validate",
                json={"api_key": candidate},
                headers=self.local_headers,
            )
        self.assertEqual(validation.status_code, 200)
        self.assertNotIn(candidate, validation.text)


if __name__ == "__main__":
    unittest.main()
