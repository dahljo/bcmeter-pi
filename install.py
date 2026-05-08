#!/usr/bin/env python3
"""bcMeter installer and updater.

Works as both a fresh installer and an updater for existing installations:
- Fresh install (no bcMeter code): full setup from scratch
- v1 upgrade (old monolithic architecture): backup → wipe → deploy → restore
- v2 update (current modular architecture): backup → redeploy → restore
"""

import os
import sys
import subprocess
import shutil
import time
import argparse
import re
from datetime import datetime
from pathlib import Path

INSTALLER_VERSION = "3.2.0 2026-05-08"
REPO_URL = "https://github.com/dahljo/bcmeter-pi.git"

APT_PACKAGES = [
	"git", "rsync", "rsyslog", "screen", "rfkill", "openssl",
	"iptables", "zram-tools", "avahi-daemon", "python3-dbus",
	"network-manager", "net-tools",
	"python3-pip", "python3-venv",
	"i2c-tools", "cloud-guest-utils",
]

# Only needed when pip must compile C extensions (fresh install / v1 upgrade).
# On V2 updates, piwheels provides pre-built wheels for all VENV_PACKAGES.
BUILD_PACKAGES = ["build-essential", "python3-dev"]

# Packages whose names differ across Pi OS versions.
# Each entry is a list of alternatives tried in order.
APT_PACKAGES_PLATFORM = [
	["python3-smbus"],
	["python3-rpi.gpio", "python3-rpi-lgpio"],
	["python3-spidev"],
	["python3-libgpiod"],
]
# pigpio/python3-pigpio removed from APT — not available on newer Pi OS.
# install_pigpiod() handles building from source; pip pigpio is in VENV_PACKAGES.

VENV_PACKAGES = [
	"fastapi",
	"uvicorn[standard]",
	"python-multipart",
	"smbus2",
	"pyserial",
	"pigpio"
]

OLD_APT_PACKAGES = [
	"nginx", "php-fpm", "php-cli", "php-common",
	"python3-numpy", "python3-pil",
	"python3-flask",
	"wireless-tools"
]

# Packages bundled with Pi OS that bcMeter never uses.
# Pi 3A+/3B+/4/5/Zero 2W all use Broadcom WiFi/BT (firmware-brcm80211),
# so the other vendor blobs are dead weight.  rpi-eeprom is Pi 4/5-only.
# rpi-connect-lite is a cloud remote-access daemon we don't ship with.
SLIM_PURGE_PACKAGES = [
	"firmware-atheros", "firmware-mediatek", "firmware-realtek",
	"rpi-eeprom", "rpi-connect-lite",
	"mkvtoolnix",
	"gdb",
]

OLD_VENV_PACKAGES = [
	"adafruit-blinka",
	"adafruit-circuitpython-sht4x",
	"oled-text",
	"flask-cors",
	"flask"
]

OLD_SERVICES = [
	"bcMeter_flask.service",
	"bcMeter_ap_control_loop.service"
]

# Items to keep during a wipe (never deleted)
KEEP_ON_WIPE = {
	"venv", "logs", "maintenance_logs", "outbox",
	"bcMeter_config.json", "bcMeter_wifi.json",
	".bashrc", ".bash_history", ".profile",
	".ssh", ".gnupg", ".config",
}

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
MAINTENANCE_LOG_DIR = BASE_DIR / "maintenance_logs"
INSTALL_LOG = MAINTENANCE_LOG_DIR / "bcMeter_install.log"
UPDATING_FLAG = Path("/tmp/bcmeter_updating")
SYSTEMD_ETC = Path("/etc/systemd/system")


# ─── Utilities ────────────────────────────────────────────────

def run_cmd(cmd, shell=False, ignore_error=False, **kwargs):
	try:
		if shell:
			subprocess.run(cmd, shell=True, check=True, executable="/bin/bash", **kwargs)
		else:
			subprocess.run(cmd, check=True, **kwargs)
	except subprocess.CalledProcessError as e:
		cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
		log(f"COMMAND FAILED (rc={e.returncode}): {cmd_str}")
		if not ignore_error:
			log("FATAL: installer aborted due to command failure above")
			sys.exit(1)


def write_file(path, content, mode="w"):
	Path(path).parent.mkdir(parents=True, exist_ok=True)
	with open(path, mode) as f:
		f.write(content)


def append_if_missing(path, text):
	path = Path(path)
	if not path.exists():
		write_file(path, text + "\n")
		return
	current_content = path.read_text(errors="ignore")
	if text.strip() not in current_content:
		with open(path, "a") as f:
			f.write("\n" + text + "\n")


def set_boot_config_value(path, key, value):
	path = Path(path)
	line = f"{key}={value}"
	if not path.exists():
		write_file(path, line + "\n")
		return
	current_content = path.read_text(errors="ignore")
	pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
	if pattern.search(current_content):
		updated = pattern.sub(line, current_content, count=1)
		if not updated.endswith("\n"):
			updated += "\n"
		path.write_text(updated)
	elif line.strip() not in current_content:
		with open(path, "a") as f:
			f.write("\n" + line + "\n")


def setup_logging():
	MAINTENANCE_LOG_DIR.mkdir(parents=True, exist_ok=True)
	with open(INSTALL_LOG, "a") as f:
		f.write(f"\n{'='*50}\n{time.ctime()} — install.py {INSTALLER_VERSION}\n{'='*50}\n")


def log(message):
	print(message, flush=True)
	with open(INSTALL_LOG, "a") as f:
		f.write(f"{message}\n")
		f.flush()
		os.fsync(f.fileno())


def is_chroot_mode(mode: str):
	return mode == "chroot"


def systemd_online(mode: str):
	if is_chroot_mode(mode):
		return False
	if os.environ.get("SYSTEMD_OFFLINE") == "1":
		return False
	return Path("/run/systemd/system").exists()


# ─── Systemd helpers ──────────────────────────────────────────

def unit_file_path(unit: str):
	candidates = [
		SYSTEMD_ETC / unit,
		Path("/lib/systemd/system") / unit,
		Path("/usr/lib/systemd/system") / unit,
	]
	for c in candidates:
		if c.exists():
			return c
	return None


def disable_unit_fs(unit: str):
	for p in SYSTEMD_ETC.glob(f"*.wants/{unit}"):
		try:
			if p.exists() or p.is_symlink():
				p.unlink()
		except Exception:
			pass
	for p in SYSTEMD_ETC.glob(f"*.requires/{unit}"):
		try:
			if p.exists() or p.is_symlink():
				p.unlink()
		except Exception:
			pass


def enable_unit_fs(unit: str):
	target = unit_file_path(unit)
	if target is None:
		return
	wants_dir = SYSTEMD_ETC / "multi-user.target.wants"
	wants_dir.mkdir(parents=True, exist_ok=True)
	link = wants_dir / unit
	try:
		if link.exists() or link.is_symlink():
			link.unlink()
	except Exception:
		pass
	try:
		link.symlink_to(target)
	except Exception:
		pass


def mask_unit_fs(unit: str):
	SYSTEMD_ETC.mkdir(parents=True, exist_ok=True)
	mask_path = SYSTEMD_ETC / unit
	try:
		if mask_path.is_symlink() or mask_path.exists():
			mask_path.unlink()
	except Exception:
		pass
	try:
		mask_path.symlink_to("/dev/null")
	except Exception:
		pass
	disable_unit_fs(unit)


def unmask_unit_fs(unit: str):
	p = SYSTEMD_ETC / unit
	try:
		if p.is_symlink() and os.readlink(p) == "/dev/null":
			p.unlink()
	except Exception:
		pass


# ─── Installation detection ──────────────────────────────────

def detect_installation() -> str:
	"""Detect what architecture is currently installed.

	Returns:
		"none" — Fresh RPi, no bcMeter code
		"v1"   — Old monolithic architecture (bcMeter.py + PHP/Flask)
		"v2"   — New modular architecture (bcmeter/ package + FastAPI)

	v1 markers take priority: when new v2 code is uploaded alongside old
	v1 files, the installation still needs v1 legacy cleanup.
	"""
	has_v1 = (BASE_DIR / "bcMeter.py").exists() or (BASE_DIR / "bcMeter_shared.py").exists()
	has_v2 = (BASE_DIR / "bcmeter" / "__init__.py").exists()

	if has_v1:
		return "v1"
	if has_v2:
		return "v2"
	return "none"


# ─── Backup & restore ────────────────────────────────────────

def backup_user_data() -> Path:
	"""Backup all user data before wipe. Returns backup directory path."""
	ts = datetime.now().strftime("%Y%m%d_%H%M%S")
	backup = BASE_DIR / f".upgrade_backup_{ts}"
	backup.mkdir(parents=True)
	log(f"Backing up user data to {backup.name}/")

	for item in ["bcMeter_config.json", "bcMeter_wifi.json"]:
		src = BASE_DIR / item
		if src.exists():
			shutil.copy2(src, backup / item)
			log(f"  Backed up {item}")

	for directory in ["logs", "maintenance_logs", "outbox"]:
		src = BASE_DIR / directory
		if src.is_dir() and any(src.iterdir()):
			shutil.copytree(src, backup / directory)
			log(f"  Backed up {directory}/")

	return backup


def restore_user_data(backup: Path):
	"""Restore user data from backup after deploy."""
	log(f"Restoring user data from {backup.name}/")

	for item in ["bcMeter_config.json", "bcMeter_wifi.json"]:
		src = backup / item
		dst = BASE_DIR / item
		if src.exists():
			shutil.copy2(src, dst)
			log(f"  Restored {item}")

	# Logs/maintenance_logs/outbox are preserved in-place (KEEP_ON_WIPE),
	# but if they were somehow lost, restore from backup
	for directory in ["logs", "maintenance_logs", "outbox"]:
		dst = BASE_DIR / directory
		src = backup / directory
		if not dst.exists() and src.exists():
			shutil.copytree(src, dst)
			log(f"  Restored {directory}/ from backup")


def cleanup_old_backups(keep: int = 3):
	"""Keep only the most recent backup directories."""
	backups = sorted(BASE_DIR.glob(".upgrade_backup_*"))
	for old in backups[:-keep]:
		shutil.rmtree(old, ignore_errors=True)
		log(f"  Removed old backup: {old.name}")


# ─── Wipe & deploy ───────────────────────────────────────────

def wipe_old_code():
	"""Remove all code files, keeping user data and system files."""
	log("Wiping old code...")
	removed = 0
	for entry in sorted(BASE_DIR.iterdir()):
		name = entry.name
		# Never touch backups
		if name.startswith(".upgrade_backup_"):
			continue
		# Never touch user data and system files
		if name in KEEP_ON_WIPE:
			continue
		try:
			if entry.is_dir():
				shutil.rmtree(entry)
			else:
				entry.unlink()
			removed += 1
		except Exception as e:
			log(f"  Warning: could not remove {name}: {e}")
	log(f"  Removed {removed} items")


def cleanup_legacy_install(mode: str):
	"""Remove old-architecture APT packages, services, and PHP files."""
	venv_dir = BASE_DIR / "venv"

	log("Removing legacy system-wide Python packages...")
	import shutil as _sh
	pip = _sh.which("pip3")
	if pip:
		pkgs = " ".join(OLD_VENV_PACKAGES)
		run_cmd(f"{pip} uninstall -y {pkgs} --break-system-packages", shell=True, ignore_error=True)

	log("Removing legacy APT packages...")
	run_cmd(["apt", "purge", "-y"] + OLD_APT_PACKAGES, ignore_error=True)

	log("Removing old services...")
	for svc in OLD_SERVICES:
		svc_path = SYSTEMD_ETC / svc
		if systemd_online(mode):
			svc_name = svc.replace(".service", "")
			run_cmd(f"systemctl stop {svc_name}", shell=True, ignore_error=True)
			run_cmd(f"systemctl disable {svc_name}", shell=True, ignore_error=True)
			run_cmd(f"systemctl reset-failed {svc_name}", shell=True, ignore_error=True)
		disable_unit_fs(svc)
		if svc_path.exists() or svc_path.is_symlink():
			try:
				svc_path.unlink()
			except Exception:
				pass

	log("Removing legacy nginx config...")
	for f in [Path("/etc/nginx/sites-available/default"),
	          Path("/etc/nginx/sites-enabled/default")]:
		if f.exists() or f.is_symlink():
			try:
				f.unlink()
			except Exception:
				pass

	if venv_dir.exists():
		log("Removing old venv (will be recreated)...")
		shutil.rmtree(venv_dir, ignore_errors=True)

	log("Removing v1 code artifacts...")
	v1_artifacts = [
		"bcMeter.py", "bcMeter_shared.py", "bcMeter_ap_control_loop.py",
		"app.py", "requirements.txt",
		"helper_scripts", "tmp",
		"bcMeter_mobile_status.json", "calibration_data.json",
	]
	for name in v1_artifacts:
		path = BASE_DIR / name
		try:
			if path.is_dir():
				shutil.rmtree(path)
				log(f"  Removed {name}/")
			elif path.exists():
				path.unlink()
				log(f"  Removed {name}")
		except Exception as e:
			log(f"  Warning: could not remove {name}: {e}")

	for pycache in BASE_DIR.rglob("__pycache__"):
		shutil.rmtree(pycache, ignore_errors=True)


def deploy_local():
	"""Deploy from already-uploaded files instead of git clone.

	Stashes the uploaded code, wipes old files, then restores the stash.
	This gives a clean slate (no leftover old files) while preserving the
	manually-uploaded new code.
	"""
	stash = Path("/tmp/bcmeter_local_stash")
	if stash.exists():
		shutil.rmtree(stash)
	stash.mkdir(parents=True)

	log("Stashing uploaded code to /tmp ...")
	for entry in sorted(BASE_DIR.iterdir()):
		name = entry.name
		if name.startswith(".upgrade_backup_"):
			continue
		if name in KEEP_ON_WIPE:
			continue
		dst = stash / name
		try:
			if entry.is_dir():
				shutil.copytree(entry, dst)
			else:
				shutil.copy2(entry, dst)
		except Exception as e:
			log(f"  Warning: could not stash {name}: {e}")

	wipe_old_code()

	log("Restoring stashed code ...")
	for entry in sorted(stash.iterdir()):
		dst = BASE_DIR / entry.name
		try:
			if entry.is_dir():
				shutil.copytree(entry, dst)
			else:
				shutil.copy2(entry, dst)
		except Exception as e:
			log(f"  Warning: could not restore {entry.name}: {e}")

	shutil.rmtree(stash, ignore_errors=True)

	app_user = BASE_DIR.name
	run_cmd(f"chown -R {app_user}:{app_user} {BASE_DIR}", shell=True, ignore_error=True)

	log("Local code deployed successfully")


def deploy_codebase():
	"""Clone the latest code from the repository."""
	UPDATING_FLAG.touch()
	try:
		log("Fetching latest bcMeter repository...")
		tmp_repo = BASE_DIR / "bcmeter_tmp"
		if tmp_repo.exists():
			shutil.rmtree(tmp_repo)

		run_cmd(f"git clone --depth 1 {REPO_URL} {tmp_repo}", shell=True)

		# rsync new code, excluding user data and backups
		excludes = " ".join(
			f"--exclude={x}" for x in [
				"venv/", "logs/", "maintenance_logs/", "outbox/",
				".upgrade_backup_*/", "bcMeter_config.json", "bcMeter_wifi.json",
			]
		)
		run_cmd(f"rsync -a --delete {excludes} {tmp_repo}/ {BASE_DIR}/", shell=True)

		# Cleanup
		shutil.rmtree(tmp_repo, ignore_errors=True)
		shutil.rmtree(BASE_DIR / "gerbers", ignore_errors=True)
		shutil.rmtree(BASE_DIR / "stl", ignore_errors=True)

		app_user = BASE_DIR.name
		run_cmd(f"chown -R {app_user}:{app_user} {BASE_DIR}", shell=True, ignore_error=True)

		log("Code deployed successfully")
	finally:
		if UPDATING_FLAG.exists():
			UPDATING_FLAG.unlink()


# ─── System setup ────────────────────────────────────────────

def _apt_lists_fresh(max_age_s=120):
	"""True if apt package lists were updated recently (e.g. by install.sh)."""
	lists_dir = Path("/var/lib/apt/lists")
	if not lists_dir.exists():
		return False
	try:
		newest = max(f.stat().st_mtime for f in lists_dir.iterdir() if f.is_file())
		return (time.time() - newest) < max_age_s
	except (ValueError, OSError):
		return False


def configure_slim_apt():
	"""Disable apt recommends and exclude doc/man/extra-locales from future installs.

	Must run BEFORE any apt install so newly installed packages also respect
	the exclusions.  Idempotent — safe to call on every install.
	"""
	write_file("/etc/apt/apt.conf.d/99-bcmeter-no-recommends",
	           'APT::Install-Recommends "false";\n'
	           'APT::Install-Suggests "false";\n')
	write_file("/etc/dpkg/dpkg.cfg.d/01-bcmeter-slim",
	           "# bcMeter -- exclude non-essential files from future installs\n"
	           "path-exclude /usr/share/doc/*\n"
	           "path-include /usr/share/doc/*/copyright\n"
	           "path-exclude /usr/share/man/*\n"
	           "path-exclude /usr/share/groff/*\n"
	           "path-exclude /usr/share/info/*\n"
	           "path-exclude /usr/share/lintian/*\n"
	           "path-exclude /usr/share/linda/*\n"
	           "path-exclude /usr/share/locale/*\n"
	           "path-include /usr/share/locale/en*\n"
	           "path-include /usr/share/locale/de*\n"
	           "path-include /usr/share/locale/locale.alias\n")


def slim_purge_unused():
	"""Purge Pi-OS bundled packages bcMeter doesn't use, then trim existing
	locale/doc/man trees that path-exclude can't retroactively shrink.

	Pi 3A+/3B+/4/5/Zero 2W all use Broadcom WiFi/BT — Atheros/MediaTek/Realtek
	firmware blobs are dead weight.  rpi-eeprom is Pi 4/5-only.  rpi-connect-lite
	is a cloud daemon we don't ship.  gdb/mkvtoolnix have no runtime use.
	"""
	installed = [p for p in SLIM_PURGE_PACKAGES if subprocess.run(
		["dpkg", "-s", p], capture_output=True).returncode == 0]
	if installed:
		log(f"Purging unused Pi OS packages: {', '.join(installed)}")
		run_cmd("apt purge -y " + " ".join(installed), shell=True, ignore_error=True)

	# Drop existing oversize trees — path-exclude only affects future installs
	log("Trimming existing locale/doc/man (keeping en/de + copyright)")
	run_cmd(
		"find /usr/share/locale -mindepth 1 -maxdepth 1 -type d "
		"! -name 'en*' ! -name 'de*' -exec rm -rf {} +",
		shell=True, ignore_error=True
	)
	run_cmd("find /usr/share/doc -type f ! -name 'copyright' -delete",
	        shell=True, ignore_error=True)
	run_cmd("find /usr/share/doc -type d -empty -delete",
	        shell=True, ignore_error=True)
	for d in ("/usr/share/man", "/usr/share/info", "/usr/share/groff", "/usr/share/lintian"):
		run_cmd(f"rm -rf {d}/*", shell=True, ignore_error=True)


def system_setup(mode: str, noupgrade=False, is_update=False):
	# Keep existing conffiles without interactive prompts
	dpkg_opts = ["-o", 'Dpkg::Options::=--force-confold']

	# Slim config first — must precede any apt install so future packages
	# respect the path-excludes and skip recommends/suggests.
	configure_slim_apt()

	if not is_update:
		# Only needed on fresh / v1 installs — a working V2 system is healthy
		run_cmd("rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/*", shell=True, ignore_error=True)
		run_cmd("dpkg --configure -a --force-confold", shell=True, ignore_error=True)
		run_cmd(["apt"] + dpkg_opts + ["-f", "install", "-y"], ignore_error=True)

		# Hold packages we plan to remove so apt upgrade doesn't waste time
		# downloading updates for them.  We can't purge dhcpcd5 yet because
		# it may be managing the active network/SSH session; it gets purged
		# later in configure_network_manager() after NetworkManager takes over.
		log("Holding packages marked for later removal...")
		run_cmd("apt-mark hold dhcpcd5 dhcpcd-base hostapd ifupdown", shell=True, ignore_error=True)

		log("Removing cloud-init...")
		run_cmd("apt purge -y cloud-init", shell=True, ignore_error=True)
		shutil.rmtree("/etc/cloud", ignore_errors=True)
		shutil.rmtree("/var/lib/cloud", ignore_errors=True)

	# Skip apt update if install.sh already did it (within last 2 minutes)
	if _apt_lists_fresh():
		log("Package lists are fresh, skipping apt update")
	else:
		log("Updating package lists...")
		if not is_update:
			run_cmd("rm -rf /var/lib/apt/lists/*", shell=True, ignore_error=True)
		run_cmd(["apt", "update", "-y"], ignore_error=True)

	if not is_update:
		log("Fixing broken deps (pre)...")
		run_cmd(["apt"] + dpkg_opts + ["--fix-broken", "install", "-y"], ignore_error=True)

	if not noupgrade:
		log("Upgrading base system...")
		run_cmd(["apt"] + dpkg_opts + ["upgrade", "-y"], ignore_error=True)

	pkg_list = APT_PACKAGES if is_update else APT_PACKAGES + BUILD_PACKAGES
	log("Installing system dependencies...")
	rc = subprocess.run(["apt"] + dpkg_opts + ["install", "-y"] + pkg_list).returncode
	if rc != 0:
		log(f"WARNING: apt install returned {rc} — retrying packages individually...")
		for pkg in pkg_list:
			result = subprocess.run(
				["apt"] + dpkg_opts + ["install", "-y", pkg],
				capture_output=True, text=True
			)
			if result.returncode == 0:
				log(f"  Installed {pkg}")
			else:
				log(f"  FAILED to install {pkg}: {result.stderr.strip()[:200]}")

	log("Installing platform-specific packages...")
	for alternatives in APT_PACKAGES_PLATFORM:
		installed = False
		for pkg in alternatives:
			result = subprocess.run(
				["apt"] + dpkg_opts + ["install", "-y", pkg],
				capture_output=True, text=True
			)
			if result.returncode == 0:
				log(f"  Installed {pkg}")
				installed = True
				break
			elif len(alternatives) > 1:
				log(f"  {pkg} not available, trying next alternative...")
		if not installed:
			log(f"  WARNING: could not install any of {alternatives}")

	if not is_update:
		log("Fixing broken deps (post)...")
		run_cmd(["apt"] + dpkg_opts + ["--fix-broken", "install", "-y"], ignore_error=True)

	log("Autoremoving unused packages...")
	run_cmd(["apt", "autoremove", "-y"], ignore_error=True)

	log("Cleaning apt cache...")
	run_cmd(["apt", "clean"], ignore_error=True)


def install_pigpiod(mode: str):
	log("Checking for pigpiod...")
	pigpiod_bin = shutil.which("pigpiod") or (
		"/usr/local/bin/pigpiod" if Path("/usr/local/bin/pigpiod").exists() else None
	)

	if pigpiod_bin:
		log(f"  pigpiod already installed at {pigpiod_bin}")
	else:
		log("Building pigpiod from source...")
		tmp_dir = Path("/tmp/pigpio")
		if tmp_dir.exists():
			shutil.rmtree(tmp_dir)
		try:
			run_cmd(["git", "clone", "--depth", "1",
			         "https://github.com/joan2937/pigpio.git", str(tmp_dir)])
			run_cmd("sed -i '/setup.py/d' Makefile", shell=True, cwd=str(tmp_dir))
			run_cmd("make -j$(nproc)", shell=True, cwd=str(tmp_dir))
			run_cmd("make install", shell=True, cwd=str(tmp_dir))
			run_cmd("ldconfig", shell=True)
			if Path("/usr/local/bin/pigpiod").exists():
				pigpiod_bin = "/usr/local/bin/pigpiod"
				log("  pigpiod built and installed successfully")
			else:
				log("  WARNING: build completed but pigpiod binary not found")
		except Exception as e:
			log(f"  Build failed: {e}")
		finally:
			shutil.rmtree(tmp_dir, ignore_errors=True)

	if not pigpiod_bin:
		log("CRITICAL: pigpiod installation failed.")
		return

	service_content = f"""[Unit]
Description=Pigpio daemon
After=network.target

[Service]
ExecStart={pigpiod_bin} -l -x -1
Type=forking

[Install]
WantedBy=multi-user.target
"""
	write_file(SYSTEMD_ETC / "pigpiod.service", service_content)
	enable_unit_fs("pigpiod.service")

	if systemd_online(mode):
		run_cmd("systemctl daemon-reload", shell=True, ignore_error=True)
		run_cmd("killall pigpiod", shell=True, ignore_error=True)
		run_cmd("systemctl enable pigpiod", shell=True, ignore_error=True)
		run_cmd("systemctl restart pigpiod", shell=True, ignore_error=True)

	append_if_missing("/etc/environment", "PIGPIO_ADDR=localhost")
	append_if_missing("/etc/environment", "PIGPIO_PORT=8888")


def configure_hardware(mode: str):
	log("Configuring hardware...")
	zram_conf = Path("/etc/default/zramswap")
	if zram_conf.exists():
		content = zram_conf.read_text()
		content = re.sub(r"^PERCENT=.*", "#PERCENT=50", content, flags=re.MULTILINE)
		if "SIZE=" in content:
			content = re.sub(r"^#*SIZE=.*", "SIZE=256", content, flags=re.MULTILINE)
		else:
			content += "\nSIZE=256\n"
		content = re.sub(r"^#*ALGO=.*", "ALGO=lz4", content, flags=re.MULTILINE)
		zram_conf.write_text(content)
		if systemd_online(mode):
			run_cmd("systemctl restart zramswap.service > /dev/null 2>&1", shell=True, ignore_error=True)

	config_txt = Path("/boot/firmware/config.txt") if Path("/boot/firmware/config.txt").exists() else Path("/boot/config.txt")
	set_boot_config_value(config_txt, "dtparam=i2c_arm", "on")
	set_boot_config_value(config_txt, "dtparam=spi", "on")
	set_boot_config_value(config_txt, "enable_uart", "1")
	append_if_missing(config_txt, "dtoverlay=disable-bt")
	write_file("/etc/modules-load.d/bcmeter-i2c.conf", "i2c-dev\n")

	cmdline = Path("/boot/firmware/cmdline.txt") if Path("/boot/firmware/cmdline.txt").exists() else Path("/boot/cmdline.txt")
	if cmdline.exists():
		if "ipv6.disable=1" not in cmdline.read_text(errors="ignore"):
			cmdline.write_text(cmdline.read_text(errors="ignore").strip() + " ipv6.disable=1")
	append_if_missing("/etc/sysctl.conf", "net.ipv6.conf.all.disable_ipv6=1")

	if not is_chroot_mode(mode):
		w1_devices = Path("/sys/bus/w1/devices/")
		has_sensor = any("28" in x.name for x in w1_devices.iterdir()) if w1_devices.exists() else False
		run_cmd(f"raspi-config nonint do_onewire {0 if has_sensor else 1}", shell=True, ignore_error=True)

	# First-boot partition resize service (replaces do_expand_rootfs).
	# Runs once on every boot, checks if the root partition can be grown,
	# expands it online, then disables itself.  Safe for both normal installs
	# and pre-built images cloned to differently-sized SD cards.
	resize_script = r"""#!/bin/bash
set -e
ROOT_PART="$(findmnt -n -o SOURCE /)"
ROOT_DISK="/dev/$(lsblk -n -o PKNAME "$ROOT_PART" | head -1)"
PART_NUM="$(echo "$ROOT_PART" | grep -o '[0-9]*$')"

if [ -z "$ROOT_DISK" ] || [ -z "$PART_NUM" ]; then
    logger -t bcmeter-resize "Cannot determine root disk/partition"
    exit 0
fi

# Check if partition already fills the disk
DISK_END=$(blockdev --getsz "$ROOT_DISK")
PART_END=$(cat /sys/class/block/$(basename "$ROOT_PART")/start 2>/dev/null || echo 0)
PART_SIZE=$(cat /sys/class/block/$(basename "$ROOT_PART")/size 2>/dev/null || echo 0)
USED_END=$((PART_END + PART_SIZE))
FREE_SECTORS=$((DISK_END - USED_END))

# Less than 16MB free — already expanded
if [ "$FREE_SECTORS" -lt 32768 ]; then
    logger -t bcmeter-resize "Partition already fills disk, disabling service"
    systemctl disable bcmeter-resize.service
    exit 0
fi

logger -t bcmeter-resize "Expanding partition $PART_NUM on $ROOT_DISK ($((FREE_SECTORS / 2048)) MiB free)"
growpart "$ROOT_DISK" "$PART_NUM" || true
resize2fs "$ROOT_PART" || true
logger -t bcmeter-resize "Resize complete, disabling service"
systemctl disable bcmeter-resize.service
"""
	write_file("/usr/local/sbin/bcmeter-resize.sh", resize_script)
	Path("/usr/local/sbin/bcmeter-resize.sh").chmod(0o755)

	resize_unit = """[Unit]
Description=bcMeter first-boot partition resize
DefaultDependencies=no
Before=bcMeter.service
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/bcmeter-resize.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
	write_file(SYSTEMD_ETC / "bcmeter-resize.service", resize_unit)
	enable_unit_fs("bcmeter-resize.service")
	if systemd_online(mode):
		run_cmd("systemctl daemon-reload", shell=True, ignore_error=True)
		run_cmd("systemctl enable bcmeter-resize", shell=True, ignore_error=True)


def configure_sudoers(app_user):
	sudoers_content = [f"{app_user} ALL=(ALL) NOPASSWD: ALL"]
	write_file("/etc/sudoers.d/010_bcmeter", "\n".join(sudoers_content) + "\n")
	run_cmd("chmod 0440 /etc/sudoers.d/010_bcmeter", shell=True)


def setup_python_env():
	log("Setting up Python environment...")
	venv_dir = BASE_DIR / "venv"
	if not venv_dir.exists():
		run_cmd([sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)])
	pip_bin = venv_dir / "bin" / "pip"
	run_cmd([str(pip_bin), "install", "--no-cache-dir"] + VENV_PACKAGES)
	return venv_dir


def configure_services(mode: str, venv_dir: Path):
	log("Configuring services...")

	py_bin = venv_dir / "bin" / "python3"
	common_env = f'Environment="PATH={venv_dir}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'

	# Remove old services (only touch if unit file exists to avoid D-Bus timeouts)
	for old_svc in OLD_SERVICES:
		disable_unit_fs(old_svc)
		old_path = SYSTEMD_ETC / old_svc
		has_unit = old_path.exists() or old_path.is_symlink() or unit_file_path(old_svc) is not None
		if has_unit:
			try:
				old_path.unlink()
			except Exception:
				pass
		if systemd_online(mode) and has_unit:
			svc_name = old_svc.replace(".service", "")
			run_cmd(f"systemctl stop {svc_name}", shell=True, ignore_error=True)
			run_cmd(f"systemctl disable {svc_name}", shell=True, ignore_error=True)
			run_cmd(f"systemctl reset-failed {svc_name}", shell=True, ignore_error=True)

	# Single bcMeter.service
	bcmeter_unit = f"""[Unit]
Description=bcMeter
After=multi-user.target NetworkManager.service pigpiod.service
Wants=NetworkManager.service
Requires=pigpiod.service

[Service]
Type=idle
ExecStart={py_bin} {BASE_DIR}/main.py
ExecStartPre=/bin/sleep 5
KillSignal=SIGINT
SyslogIdentifier=bcMeter
Restart=always
RestartSec=3
OOMScoreAdjust=-500
User=root
{common_env}
[Install]
WantedBy=multi-user.target
"""

	write_file(SYSTEMD_ETC / "bcMeter.service", bcmeter_unit)
	enable_unit_fs("bcMeter.service")

	if systemd_online(mode):
		run_cmd("systemctl daemon-reload", shell=True, ignore_error=True)
		run_cmd("systemctl enable bcMeter", shell=True, ignore_error=True)

	if not is_chroot_mode(mode):
		for cmd in ["do_boot_behaviour B2", "do_i2c 0", "do_spi 0",
		            "do_serial_hw 0", "do_serial_cons 1", "do_net_names 0"]:
			run_cmd(f"raspi-config nonint {cmd}", shell=True, ignore_error=True)

	app_user = BASE_DIR.name
	run_cmd(f"chown -R {app_user}:{app_user} {BASE_DIR}", shell=True, ignore_error=True)

	bashrc = BASE_DIR / ".bashrc"
	service_check = "\nif ! systemctl is-active --quiet bcMeter; then\n    sudo systemctl enable bcMeter\n    sudo systemctl start bcMeter\nfi\n"
	append_if_missing(bashrc, service_check)

	bashrc_content = bashrc.read_text(errors="ignore") if bashrc.exists() else ""
	bashrc_content = re.sub(r"^alias bcd=.*$", "", bashrc_content, flags=re.MULTILINE)
	bashrc_content = re.sub(r"^alias bcc=.*$", "", bashrc_content, flags=re.MULTILINE)
	bashrc_content = bashrc_content.strip() + "\n"
	bashrc_content += f"alias bcd='sudo {py_bin} {BASE_DIR}/main.py debug'\n"
	bashrc_content += f"alias bcc='sudo {py_bin} {BASE_DIR}/main.py cal'\n"
	bashrc.write_text(bashrc_content)


def configure_network_manager(mode: str, is_update: bool = False):
	log("Configuring NetworkManager...")

	nm_conf = "[main]\nplugins=keyfile\n\n[device]\nwifi.scan-rand-mac-address=no\n\n[connection]\nwifi.cloned-mac-address=preserve\n"
	write_file("/etc/NetworkManager/conf.d/10-bcmeter.conf", nm_conf)

	# Captive portal DNS hijack: when AP is active (ipv4.method=shared),
	# NM's internal dnsmasq resolves all domains to the AP IP so that
	# phones/laptops detect the captive portal and open the setup page.
	Path("/etc/NetworkManager/dnsmasq-shared.d").mkdir(parents=True, exist_ok=True)
	write_file("/etc/NetworkManager/dnsmasq-shared.d/bcmeter-captive.conf",
	           "address=/#/192.168.18.8\n")

	for f in ["/etc/NetworkManager/conf.d/10-globally-managed-devices.conf",
	          "/usr/lib/NetworkManager/conf.d/10-globally-managed-devices.conf"]:
		Path(f).unlink(missing_ok=True)

	for f in ["/etc/network/interfaces.d/wlan0_wifi",
	          "/etc/wpa_supplicant/wpa_supplicant.conf",
	          "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"]:
		Path(f).unlink(missing_ok=True)

	# Remove WiFi connections pre-configured by Raspberry Pi Imager / cloud-init.
	# Only on fresh install / image prep — updates must keep the user's WiFi
	# so the device stays reachable after reboot.
	if not is_update:
		nm_sys_conn = Path("/etc/NetworkManager/system-connections")
		if nm_sys_conn.is_dir():
			for conn_file in nm_sys_conn.iterdir():
				if not conn_file.is_file():
					continue
				try:
					content = conn_file.read_text(errors="ignore")
					if "type=wifi" in content or "type=802-11-wireless" in content:
						conn_file.unlink()
						log(f"  Removed pre-existing WiFi profile: {conn_file.name}")
				except Exception as e:
					log(f"  Warning: could not inspect {conn_file.name}: {e}")

		# Remove netplan WiFi configs (Trixie Imager uses netplan, which generates
		# NM profiles at runtime — they won't appear in system-connections/).
		# Strip only the wifis: section; keep ethernets: so SSH over ethernet survives.
		netplan_dir = Path("/etc/netplan")
		if netplan_dir.is_dir():
			for np_file in netplan_dir.glob("*.yaml"):
				try:
					content = np_file.read_text(errors="ignore")
					if "wifis:" not in content:
						continue
					# Remove the wifis: block (YAML indent-based)
					lines = content.splitlines(keepends=True)
					filtered = []
					skip_depth = None
					for line in lines:
						stripped = line.lstrip()
						indent = len(line) - len(stripped)
						if stripped.startswith("wifis:"):
							skip_depth = indent
							continue
						if skip_depth is not None:
							if indent > skip_depth or (not stripped and indent == 0):
								continue
							skip_depth = None
						filtered.append(line)
					remaining = "".join(filtered).strip()
					if not remaining or remaining == "---":
						np_file.unlink()
						log(f"  Removed empty netplan config: {np_file.name}")
					else:
						np_file.write_text(remaining + "\n")
						log(f"  Stripped WiFi from netplan config: {np_file.name}")
				except Exception as e:
					log(f"  Warning: could not process {np_file.name}: {e}")

		# Delete any active NM WiFi connections created by netplan or imager
		if systemd_online(mode):
			rc = subprocess.run(
				["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"],
				capture_output=True, text=True, timeout=10
			)
			if rc.returncode == 0:
				for line in rc.stdout.strip().splitlines():
					parts = line.split(":", 1)
					if len(parts) == 2 and "wireless" in parts[1]:
						con_name = parts[0]
						log(f"  Deleting NM WiFi connection: {con_name}")
						subprocess.run(
							["nmcli", "con", "delete", con_name],
							capture_output=True, timeout=10
						)
	else:
		log("  Update mode — preserving existing WiFi connections")

	# Ensure an ethernet fallback connection always exists in NM so SSH
	# over ethernet is never lost, even if netplan configs are removed.
	eth_conn = Path("/etc/NetworkManager/system-connections/bcmeter-eth.nmconnection")
	if not eth_conn.exists():
		eth_content = "[connection]\nid=bcmeter-eth\ntype=ethernet\nautoconnect=true\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=disabled\n"
		write_file(eth_conn, eth_content)
		eth_conn.chmod(0o600)
		log("  Created ethernet fallback connection: bcmeter-eth")

	ifaces = Path("/etc/network/interfaces")
	if ifaces.exists():
		content = ifaces.read_text(errors="ignore")
		ifaces.write_text("\n".join(l for l in content.splitlines() if "wlan0" not in l) + "\n")

	for unit in ["systemd-networkd.service", "dhcpcd.service",
	             "hostapd.service", "dnsmasq.service"]:
		disable_unit_fs(unit)
		mask_unit_fs(unit)

	unmask_unit_fs("NetworkManager.service")
	enable_unit_fs("NetworkManager.service")

	if systemd_online(mode):
		# Only stop/disable/mask services that aren't already masked
		for s in ["systemd-networkd", "dhcpcd", "hostapd", "dnsmasq"]:
			unit_path = SYSTEMD_ETC / f"{s}.service"
			already_masked = unit_path.is_symlink() and os.readlink(str(unit_path)) == "/dev/null"
			if not already_masked:
				run_cmd(f"systemctl stop {s}", shell=True, ignore_error=True)
				run_cmd(f"systemctl disable {s}", shell=True, ignore_error=True)
				run_cmd(f"systemctl mask {s}", shell=True, ignore_error=True)

		# Start NetworkManager BEFORE purging dhcpcd so it takes over the interface
		run_cmd("systemctl unmask NetworkManager && systemctl enable NetworkManager && systemctl restart NetworkManager",
		        shell=True, ignore_error=True)
		time.sleep(3)
		run_cmd("nmcli dev set wlan0 managed yes", shell=True, ignore_error=True)
		run_cmd("nmcli radio wifi on", shell=True, ignore_error=True)

	# Purge legacy network packages only if any are still installed
	legacy_net = ["dhcpcd5", "dhcpcd-base", "hostapd", "ifupdown"]
	installed = [p for p in legacy_net if subprocess.run(
		["dpkg", "-s", p], capture_output=True).returncode == 0]
	if installed:
		log(f"Purging legacy network packages: {', '.join(installed)}")
		run_cmd("apt-mark unhold " + " ".join(legacy_net), shell=True, ignore_error=True)
		run_cmd("apt purge -y " + " ".join(installed), shell=True, ignore_error=True)
		run_cmd(["apt", "autoremove", "-y"], ignore_error=True)


def configure_device_identity(mode: str, is_update: bool = False):
	"""Prepare for first-boot device identity.

	The actual device_name (bcMeter-XXYY from WiFi MAC) is derived at
	boot time by config.py, since each cloned image runs on different
	hardware.  The installer just:
	1. Resets device_name to the bare default so config.py re-derives it
	   (fresh install / image prep only — updates keep the user's name)
	2. Sets a generic hostname (main.py updates it on boot)
	3. Deletes WiFi credentials to force AP mode on first boot
	   (fresh install / image prep only)
	"""
	log("Configuring device identity...")

	if is_update:
		log("  Update detected — keeping existing device name and WiFi credentials")
		return

	# Reset device_name to default so config.py derives it from MAC at boot
	config_path = BASE_DIR / "bcMeter_config.json"
	if config_path.exists():
		try:
			import json
			with open(config_path, "r") as f:
				cfg_data = json.load(f)
			if "device_name" in cfg_data:
				cfg_data["device_name"]["value"] = "bcMeter"
				with open(config_path, "w") as f:
					json.dump(cfg_data, f, indent=4)
				log("  Reset device_name to default (will derive from MAC on boot)")
		except Exception as e:
			log(f"  Warning: could not update config: {e}")

	# Generic hostname — main.py will update on boot from device_name
	generic_hostname = "bcmeter"
	if not is_chroot_mode(mode):
		run_cmd(f"hostnamectl set-hostname {generic_hostname}", shell=True, ignore_error=True)
	else:
		write_file("/etc/hostname", generic_hostname + "\n")
	Path("/etc/avahi/services/bcmeter-http.service").unlink(missing_ok=True)

		# Delete WiFi credentials only on fresh prepared installs.
	# On v1→v2 upgrades and v2 updates, keep credentials so the device
	# stays reachable on the network after reboot.
	wifi_creds = BASE_DIR / "bcMeter_wifi.json"
	if wifi_creds.exists() and (is_chroot_mode(mode) or mode == "force"):
		wifi_creds.unlink()
		log("  Deleted WiFi credentials — first boot will start in AP mode")
	elif wifi_creds.exists():
		log("  Preserved WiFi credentials (update mode — keeping network access)")


def post_install_cleanup(mode: str, is_update=False):
	log("Post-install cleanup...")

	if is_chroot_mode(mode):
		log("  Chroot mode — removing build tools, git, rsync")
		run_cmd("apt remove --purge build-essential python3-dev git rsync -y", shell=True, ignore_error=True)
	elif not is_update:
		# build-essential was only installed on fresh/v1; skip on V2 updates
		log("  Removing build tools (keeping git/rsync for updates)")
		run_cmd("apt remove --purge build-essential python3-dev -y", shell=True, ignore_error=True)

	# Purge Pi-OS bundled packages bcMeter never uses (firmware blobs for non-Broadcom
	# chips, Pi 4/5-only EEPROM tools, cloud-connect daemon, etc.)
	slim_purge_unused()

	# --purge also removes config files from orphaned packages
	log("  Autoremoving orphaned packages (with config purge)...")
	run_cmd("apt autoremove --purge -y", shell=True, ignore_error=True)
	run_cmd("apt clean", shell=True, ignore_error=True)

	# Remove leftover dpkg backup files and apt lists
	log("  Cleaning dpkg/apt cruft...")
	run_cmd("rm -f /var/cache/apt/archives/*.deb", shell=True, ignore_error=True)
	run_cmd("rm -rf /var/lib/apt/lists/*", shell=True, ignore_error=True)
	for dpkg_bak in Path("/var/lib/dpkg").glob("*-old"):
		dpkg_bak.unlink(missing_ok=True)

	# Cap systemd journal to 16 MB (can grow to hundreds of MB on upgraded systems)
	log("  Capping systemd journal...")
	journal_conf = Path("/etc/systemd/journald.conf")
	if journal_conf.exists():
		content = journal_conf.read_text(errors="ignore")
		if "SystemMaxUse=" not in content or re.search(r"^#\s*SystemMaxUse=", content, re.MULTILINE):
			content = re.sub(r"^#?\s*SystemMaxUse=.*$", "SystemMaxUse=16M", content, flags=re.MULTILINE)
			if "SystemMaxUse=" not in content:
				content += "\nSystemMaxUse=16M\n"
			journal_conf.write_text(content)
	if systemd_online(mode):
		run_cmd("journalctl --vacuum-size=16M", shell=True, ignore_error=True)

	# Purge pip caches
	log("  Purging pip caches...")
	venv_dir = BASE_DIR / "venv"
	pip_bin = venv_dir / "bin" / "pip"
	if pip_bin.exists():
		run_cmd(f"{pip_bin} cache purge", shell=True, ignore_error=True)
	run_cmd("pip cache purge", shell=True, ignore_error=True)

	# Remove stale .pyc files and __pycache__ dirs
	for pycache in BASE_DIR.rglob("__pycache__"):
		shutil.rmtree(pycache, ignore_errors=True)

	# Remove desktop/GUI leftovers that may remain from full Pi OS images
	for cruft_dir in ["/home/*/.cache/thumbnails", "/home/*/.local/share/Trash",
	                  "/var/cache/fontconfig", "/var/cache/man"]:
		run_cmd(f"rm -rf {cruft_dir}", shell=True, ignore_error=True)

	log("Cleanup complete.")


# ─── Pre-image scrub ──────────────────────────────────────────

def prepare_image_cleanup():
	"""Scrub all customer-/operator-specific state so the SD card can be
	cloned into a master image that boots like a fresh install on every
	device.

	What gets removed:
	  - WiFi profiles (NetworkManager keyfiles + bcMeter_wifi.json) —
	    forces AP mode on first boot for customer onboarding
	  - SSH host keys — regenerated per-device on first boot via the
	    ssh-keygen-once.service that this function installs (otherwise
	    every flashed card would share host keys = MITM risk)
	  - All authorized_keys (operator's SSH key won't ship to customers)
	  - All bcMeter logs, maintenance logs, outbox, notes, session data
	  - bcMeter_config.json — recreated from defaults at next boot;
	    config.py derives device_name from MAC, so the new device gets
	    its own name
	  - Bash / less / python history under /home and /root
	  - systemd machine-id (regenerated at next boot)
	  - DHCP leases + NetworkManager runtime state
	  - apt cache, pip cache, journal, /tmp, /var/log files
	  - Python __pycache__ trees

	After this runs, the Pi halts. Pull the SD card and image it.
	"""
	log("=" * 60)
	log("PREPARE-IMAGE — scrubbing device state for SD-card cloning")
	log("=" * 60)

	app_user = BASE_DIR.name

	# Stop our service first so it can't recreate state files mid-cleanup
	run_cmd("systemctl stop bcMeter", shell=True, ignore_error=True)
	run_cmd("systemctl stop pigpiod", shell=True, ignore_error=True)

	# WiFi: drop every NM profile except the ethernet fallback we ship
	nm_dir = Path("/etc/NetworkManager/system-connections")
	if nm_dir.is_dir():
		for f in nm_dir.iterdir():
			if f.is_file() and f.name != "bcmeter-eth.nmconnection":
				try:
					f.unlink()
					log(f"  Removed NM profile: {f.name}")
				except Exception:
					pass
	(BASE_DIR / "bcMeter_wifi.json").unlink(missing_ok=True)
	# DHCP leases + NM runtime state — force re-DHCP on next boot
	run_cmd("rm -rf /var/lib/dhcp/* /var/lib/NetworkManager/* "
	        "/run/NetworkManager/* /var/lib/dhcpcd/*",
	        shell=True, ignore_error=True)

	# SSH host keys — regenerate per-device on first boot
	for k in Path("/etc/ssh").glob("ssh_host_*"):
		k.unlink(missing_ok=True)
	write_file("/etc/systemd/system/ssh-keygen-once.service",
	           "[Unit]\n"
	           "Description=Regenerate SSH host keys on first boot\n"
	           "ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key\n"
	           "Before=ssh.service\n"
	           "DefaultDependencies=no\n"
	           "After=local-fs.target\n\n"
	           "[Service]\n"
	           "Type=oneshot\n"
	           "ExecStart=/usr/bin/ssh-keygen -A\n"
	           "ExecStartPost=/bin/systemctl disable ssh-keygen-once.service\n\n"
	           "[Install]\n"
	           "WantedBy=multi-user.target\n")
	enable_unit_fs("ssh-keygen-once.service")
	log("  Removed host SSH keys; ssh-keygen-once.service will regenerate at next boot")

	# Authorized keys + known_hosts under bcmeter + root
	for home in (Path(f"/home/{app_user}"), Path("/root")):
		ssh_dir = home / ".ssh"
		for f in ("authorized_keys", "known_hosts", "id_rsa", "id_ed25519",
		         "id_rsa.pub", "id_ed25519.pub"):
			(ssh_dir / f).unlink(missing_ok=True)

	# All bcMeter user data
	(BASE_DIR / "bcMeter_config.json").unlink(missing_ok=True)
	for sub in ("logs", "maintenance_logs", "outbox"):
		run_cmd(f"rm -rf {BASE_DIR}/{sub}/*", shell=True, ignore_error=True)
	# Recreate empty log dirs so the service can start without warnings
	for sub in ("logs", "maintenance_logs"):
		(BASE_DIR / sub).mkdir(exist_ok=True, parents=True)
	(BASE_DIR / "logs" / "log_current.csv").touch()

	# Python bytecode trees — small, but tidy
	for pc in BASE_DIR.rglob("__pycache__"):
		shutil.rmtree(pc, ignore_errors=True)

	# Shell / editor history under both homes
	for home in (Path(f"/home/{app_user}"), Path("/root")):
		for hist in (".bash_history", ".lesshst", ".viminfo", ".python_history",
		             ".wget-hsts", ".sudo_as_admin_successful"):
			(home / hist).unlink(missing_ok=True)

	# machine-id (regenerated by systemd-machine-id-setup at next boot)
	run_cmd("truncate -s 0 /etc/machine-id", shell=True, ignore_error=True)
	Path("/var/lib/dbus/machine-id").unlink(missing_ok=True)

	# Hostname placeholder (config.py + main.py rename to MAC-derived form on boot)
	write_file("/etc/hostname", "bcmeter\n")
	hosts = Path("/etc/hosts")
	if hosts.exists():
		lines = [l for l in hosts.read_text(errors="ignore").splitlines()
		         if "127.0.1.1" not in l]
		lines.append("127.0.1.1\tbcmeter")
		hosts.write_text("\n".join(lines) + "\n")
	Path("/etc/avahi/services/bcmeter-http.service").unlink(missing_ok=True)

	# Caches + journal + tmp + /var/log files
	venv_pip = BASE_DIR / "venv" / "bin" / "pip"
	if venv_pip.exists():
		run_cmd(f"{venv_pip} cache purge", shell=True, ignore_error=True)
	run_cmd("apt clean", shell=True, ignore_error=True)
	run_cmd("rm -rf /var/cache/apt/archives/*.deb /var/lib/apt/lists/*",
	        shell=True, ignore_error=True)
	run_cmd("journalctl --rotate", shell=True, ignore_error=True)
	run_cmd("journalctl --vacuum-time=1s", shell=True, ignore_error=True)
	run_cmd("find /var/log -type f -delete", shell=True, ignore_error=True)
	run_cmd("rm -rf /tmp/* /tmp/.[!.]*", shell=True, ignore_error=True)
	run_cmd("rm -rf /var/tmp/*", shell=True, ignore_error=True)

	# Restore ownership for anything we touched as root
	run_cmd(f"chown -R {app_user}:{app_user} {BASE_DIR}",
	        shell=True, ignore_error=True)

	log("Scrub complete. Halting in 5 s — pull the SD card after the LED stops blinking.")
	run_cmd("sync", shell=True)
	time.sleep(5)
	run_cmd("shutdown -h now", shell=True, ignore_error=True)


# ─── Main ─────────────────────────────────────────────────────

def main():
	parser = argparse.ArgumentParser(description="bcMeter installer/updater")
	parser.add_argument("mode", nargs="?", default="install",
	                    help="install | local | chroot | force | noupgrade | prepare-image")
	parser.add_argument("--clone", action="store_true")
	args = parser.parse_args()

	mode = args.mode

	# prepare-image is a destructive scrub that ends in shutdown — handle
	# it before the install pipeline so we don't redo apt/venv work.
	if mode == "prepare-image":
		setup_logging()
		if os.geteuid() != 0:
			sys.exit("Run with sudo")
		log(f"bcMeter installer v{INSTALLER_VERSION} — prepare-image mode")
		prepare_image_cleanup()
		return

	setup_logging()
	if os.geteuid() != 0:
		sys.exit("Run with sudo")

	log(f"bcMeter installer v{INSTALLER_VERSION}")
	app_user = BASE_DIR.name

	# ── Step 1: Detect what's installed ──
	arch = detect_installation()
	log(f"Detected installation: {arch}")
	backup = None

	# ── Step 2: Backup + wipe based on architecture ──
	if mode == "chroot":
		log("=" * 50)
		log(f"CHROOT mode — using pre-staged code at {BASE_DIR}")
		log("=" * 50)
			# Prepared-install path: files are already at BASE_DIR.
			# Skip wipe + git clone; force fresh-install semantics
		# downstream so BUILD_PACKAGES are installed for the pigpiod compile.
		arch = "none"

	elif mode == "local":
		log("=" * 50)
		log("LOCAL mode — using uploaded code, wiping old files")
		log("=" * 50)
		backup = backup_user_data()
		if arch != "v2":
			cleanup_legacy_install(mode)
		deploy_local()

	elif arch == "v1":
		log("=" * 50)
		log("UPGRADING from v1 (old monolithic architecture)")
		log("=" * 50)
		backup = backup_user_data()
		cleanup_legacy_install(mode)
		wipe_old_code()

	elif arch == "v2":
		log("=" * 50)
		log("UPDATING v2 installation")
		log("=" * 50)
		backup = backup_user_data()
		wipe_old_code()

	else:
		log("=" * 50)
		log("FRESH INSTALL")
		log("=" * 50)

	is_update = arch == "v2"

	# ── Step 3: System setup ──
	system_setup(mode, noupgrade=(mode == "noupgrade"), is_update=is_update)
	install_pigpiod(mode)

	# ── Step 4: Deploy new code ──
	# chroot mode uses the already-staged source; local mode preserved
	# the uploaded source via deploy_local() above. Both skip the git
	# clone path of deploy_codebase().
	if mode not in ("local", "chroot"):
		deploy_codebase()

	# ── Step 5: Restore user data ──
	if backup:
		restore_user_data(backup)

	# ── Step 6: Configure system ──
	configure_hardware(mode)
	configure_sudoers(app_user)

	LOG_DIR.mkdir(parents=True, exist_ok=True)
	(LOG_DIR / "log_current.csv").touch(exist_ok=True)

	# ── Step 7: Device identity (hostname, device_name, force AP) ──
	# Only reset device name + wipe WiFi on fresh install / image prep.
	# On updates (v1→v2 or v2→v2) keep the user's custom name.
	configure_device_identity(mode, is_update=(arch in ("v1", "v2")))

	# ── Step 8: Network (may drop SSH) ──
	log("=" * 50)
	log("Network reconfiguration")
	log("SSH connection may drop temporarily.")
	log("=" * 50)
	configure_network_manager(mode, is_update=(arch in ("v1", "v2")))

	# ── Step 9: Cleanup (before venv so apt autoremove can't break it) ──
	post_install_cleanup(mode, is_update=is_update)
	cleanup_old_backups(keep=3)

	# ── Step 10: Python environment ──
	# Must run AFTER post_install_cleanup: the venv uses --system-site-packages,
	# so pip skips deps already present in system Python.  If apt autoremove
	# later removes those system packages (e.g. python3-jinja2 orphaned after
	# flask purge), the venv silently breaks.  By running pip last, it sees the
	# true post-cleanup system state and installs everything it actually needs.
	venv_dir = setup_python_env()
	configure_services(mode, venv_dir)

	run_cmd(f"chown -R {app_user}:{app_user} {BASE_DIR}", shell=True, ignore_error=True)
	run_cmd(f"chmod -R u+rwX,go+rX,go-w {BASE_DIR}", shell=True, ignore_error=True)

	if is_chroot_mode(mode):
		log("Chroot install complete.")
		return

	log("Installation complete. Rebooting...")
	time.sleep(3)
	run_cmd("reboot now", shell=True)


if __name__ == "__main__":
	main()
