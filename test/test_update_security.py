import io
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bcmeter import ota_check
from bcmeter.config import CfgStore
from bcmeter.update_package import UnsafeArchiveError, safe_extract_archive
from bcmeter.wifimgr import NetworkManager


ROOT = Path(__file__).resolve().parents[1]


class PublicUpdateBoundaryTest(unittest.TestCase):
    def test_public_tree_has_no_arbitrary_archive_upload(self):
        self.assertFalse((ROOT / "api" / "routes_update.py").exists())
        app_source = (ROOT / "api" / "app.py").read_text()
        interface = (ROOT / "interface" / "index.html").read_text()
        self.assertNotIn("routes_update", app_source)
        self.assertNotIn("/api/update", interface)

    def test_release_package_requires_release_asset_and_sha256(self):
        digest = "ab" * 32
        data = {
            "tarball_url": "https://api.github.com/unsafe/source-tarball",
            "assets": [{
                "name": "bcmeter-pi-v1.6.11.tar.gz",
                "browser_download_url": "https://github.com/dahljo/bcmeter-pi/releases/download/v1.6.11/bcmeter-pi-v1.6.11.tar.gz",
            }, {
                "name": "bcmeter-pi-v1.6.11.tar.gz.sha256",
                "browser_download_url": "https://github.com/dahljo/bcmeter-pi/releases/download/v1.6.11/bcmeter-pi-v1.6.11.tar.gz.sha256",
            }],
        }
        with mock.patch.object(ota_check, "_download_text", return_value=digest):
            name, url, actual = ota_check._resolve_release_package(data)
        self.assertEqual(name, "bcmeter-pi-v1.6.11.tar.gz")
        self.assertIn("/releases/download/", url)
        self.assertEqual(actual, digest)
        self.assertNotEqual(url, data["tarball_url"])

    def test_tar_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "update.tar.gz"
            extract = Path(tmp) / "extract"
            extract.mkdir()
            with tarfile.open(archive, "w:gz") as tar:
                payload = b"escaped"
                info = tarfile.TarInfo("../outside.txt")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            with self.assertRaises(UnsafeArchiveError):
                safe_extract_archive(str(archive), archive.name, str(extract))
            self.assertFalse((Path(tmp) / "outside.txt").exists())

    def test_tar_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "update.tar.gz"
            extract = Path(tmp) / "extract"
            extract.mkdir()
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            with self.assertRaises(UnsafeArchiveError):
                safe_extract_archive(str(archive), archive.name, str(extract))

    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "update.zip"
            extract = Path(tmp) / "extract"
            extract.mkdir()
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../outside.txt", "escaped")
            with self.assertRaises(UnsafeArchiveError):
                safe_extract_archive(str(archive), archive.name, str(extract))
            self.assertFalse((Path(tmp) / "outside.txt").exists())


class CredentialFilePermissionsTest(unittest.TestCase):
    def test_config_is_created_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bcMeter_config.json"
            cfg = CfgStore(str(path))
            cfg.set_string("email_api_key", "test-secret")
            self.assertTrue(cfg.save())
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_existing_config_permissions_are_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bcMeter_config.json"
            path.write_text("{}")
            os.chmod(path, 0o644)
            CfgStore(str(path))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_wifi_credentials_are_written_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = NetworkManager(cfg=None, base_dir=tmp)
            manager.save_credentials("Example", "correct-horse")
            path = Path(tmp) / "bcMeter_wifi.json"
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            manager.delete_credentials()
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

if __name__ == "__main__":
    unittest.main()
