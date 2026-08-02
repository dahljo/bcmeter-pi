#!/usr/bin/env python3
"""CSV downloads must retain the complete configured device identity."""

import ast
from datetime import datetime
import os
from pathlib import Path
import re
import socket
from types import SimpleNamespace
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PI_UI = (ROOT / "interface" / "index.html").read_text()
ROUTES = (ROOT / "api" / "routes_csv.py").read_text()


def _load_route_name_helpers_without_fastapi():
    """Execute only the pure filename helpers when FastAPI is unavailable."""
    wanted = {
        "_safe_download_component",
        "_configured_device_download_name",
        "_csv_download_name",
        "_csv_attachment_headers",
    }
    tree = ast.parse(ROUTES)
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & {
                "_DOWNLOAD_COMPONENT_RE",
                "_DEVICE_SUFFIX_RE",
                "_GENERIC_LOG_PREFIX_RE",
                "_MAX_DOWNLOAD_COMPONENT",
            }:
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
    module = types.ModuleType("routes_csv_name_helpers")
    module.__dict__.update(
        {"os": os, "re": re, "socket": socket, "datetime": datetime, "_cfg": None}
    )
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROOT / "api/routes_csv.py"), "exec"), module.__dict__)
    return module


try:
    from api import routes_csv
except ModuleNotFoundError:
    routes_csv = _load_route_name_helpers_without_fastapi()


class CsvDownloadNameTests(unittest.TestCase):
    def setUp(self):
        self.old_cfg = routes_csv._cfg

    def tearDown(self):
        routes_csv._cfg = self.old_cfg

    @staticmethod
    def _cfg(name):
        return SimpleNamespace(get_string=lambda key, default="": name)

    def test_current_and_archived_names_keep_full_device_suffix(self):
        routes_csv._cfg = self._cfg("bcMeter-CA38")
        self.assertEqual(
            routes_csv._csv_download_name(now=datetime(2026, 8, 2, 12, 34, 56)),
            "bcMeter-CA38_20260802_123456.csv",
        )
        self.assertEqual(
            routes_csv._csv_download_name("17-06-26_121700.csv"),
            "bcMeter-CA38_17-06-26_121700.csv",
        )
        self.assertEqual(
            routes_csv._csv_download_name("bcmeter_20260802_1103.csv"),
            "bcMeter-CA38_20260802_1103.csv",
        )
        self.assertEqual(
            routes_csv._csv_download_name("bcMeter-CA38_20260802_1103.csv"),
            "bcMeter-CA38_20260802_1103.csv",
        )

    def test_sanitization_blocks_path_syntax_without_dropping_suffix(self):
        routes_csv._cfg = self._cfg("../../bcMeter-CA38")
        name = routes_csv._csv_download_name("../../bcmeter_bad name.csv")
        self.assertEqual(name, "bcMeter-CA38_bad-name.csv")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)

    def test_defensive_length_cap_preserves_four_character_suffix(self):
        routes_csv._cfg = self._cfg("Measurement-" + ("very-long-" * 12) + "CA38")
        device = routes_csv._configured_device_download_name()
        self.assertLessEqual(len(device), 64)
        self.assertTrue(device.endswith("-CA38"))
        self.assertTrue(routes_csv._csv_download_name("session.csv").startswith(device + "_"))

    def test_api_attachment_header_uses_safe_device_name(self):
        routes_csv._cfg = self._cfg("bcMeter-CA38")
        headers = routes_csv._csv_attachment_headers("bcmeter_log_1.csv")
        self.assertEqual(
            headers["Content-Disposition"],
            'attachment; filename="bcMeter-CA38_log_1.csv"',
        )
        self.assertEqual(headers["X-bcMeter-Download-Device"], "bcMeter-CA38")
        self.assertIn(
            "X-bcMeter-Download-Device",
            headers["Access-Control-Expose-Headers"],
        )


class CsvDownloadInterfaceGuardTests(unittest.TestCase):
    def test_current_selected_and_header_only_api_paths_set_attachment_names(self):
        self.assertIn("headers=_csv_attachment_headers(),", ROUTES)
        self.assertIn('headers = _csv_attachment_headers(fname if file else "")', ROUTES)
        self.assertNotIn("bcMeter-0000", ROUTES)

    def test_interface_uses_device_aware_names_for_every_csv_path(self):
        self.assertIn("function safeDeviceDownloadName(name)", PI_UI)
        self.assertIn("function csvDownloadNameFromResponse(response,fallback)", PI_UI)
        self.assertIn("function rememberCsvDownloadDevice(response)", PI_UI)
        self.assertIn("headers.get('x-bcmeter-download-device')", PI_UI)
        self.assertIn(
            "then(function(r){rememberCsvDownloadDevice(r);return r.text()})",
            PI_UI,
        )
        self.assertIn("var fn=csvDownloadNameFromResponse(r,makeDownloadName(name))", PI_UI)
        self.assertIn(
            "var fallback=sel==='current'?makeDownloadName():makeDownloadName(sel)",
            PI_UI,
        )
        self.assertIn("prefix=safeDeviceDownloadName(deviceName)+'_'", PI_UI)
        self.assertIn("blobDl(b,prefix+'_view.csv')", PI_UI)
        self.assertIn(
            "if(leaf.toLowerCase().indexOf(dev.toLowerCase()+'_')===0)return leaf+'.csv';leaf=leaf.replace(/^bcmeter_+/i,'')",
            PI_UI,
        )
        self.assertNotIn("prefix='bcmeter_'", PI_UI)
        self.assertNotIn("blobDl(b,sel==='current'?makeDownloadName():sel)", PI_UI)
        self.assertNotIn("then(function(b){blobDl(b,name)})", PI_UI)
        self.assertIn("location.hostname||'bcMeter'", PI_UI)
        self.assertNotIn("bcMeter-0000", PI_UI)


if __name__ == "__main__":
    unittest.main()
