import tempfile
import unittest
from pathlib import Path

from bcmeter.storage import Storage


class LogManagementTests(unittest.TestCase):
    def test_active_log_is_protected_then_deletable_after_session_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(tmp)
            name = storage.start_session(time_synced=True)
            self.assertFalse(storage.delete_log(name))
            storage.end_session()
            self.assertTrue(storage.delete_log(name))
            self.assertFalse((Path(tmp) / name).exists())

    def test_interface_sorts_paths_and_exposes_delete_action(self):
        root = Path(__file__).resolve().parents[1]
        interface = (root / "interface" / "index.html").read_text()
        routes = (root / "api" / "routes_csv.py").read_text()

        self.assertIn("replace(/^.*\\//,'')", interface)
        self.assertIn("deleteLogFile(f.name)", interface)
        self.assertIn("method:'DELETE'", interface)
        self.assertIn('@router.delete(', routes)
        self.assertIn('"active": bool(entry.get("active", False))', routes)
        self.assertIn('fname = logs[0]["name"]', routes)
        self.assertIn("if(r.ok)return r.text();", interface)


if __name__ == "__main__":
    unittest.main()
