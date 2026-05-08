#!/usr/bin/env python3
"""bcMeter Pi fleet management CLI — discover, control, and update Raspberry Pi devices on LAN."""

import subprocess, json, sys, time, os, argparse, socket, tarfile, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request
from urllib.error import URLError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 8

# Files/dirs to include in update archive
UPDATE_INCLUDES = [
    "bcmeter", "api", "interface", "main.py", "install.py", "install.sh",
    "bcmeter-qc.py",
]
# Patterns to exclude from archive
UPDATE_EXCLUDES = ["__pycache__", "*.pyc", ".DS_Store"]

SSH_OPTS = ["-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes", "-o", "LogLevel=ERROR"]
SSH_USERS = ["bcmeter", "pi"]  # try in order

# ── HTTP helpers ─────────────────────────────────────────────────────────────

def api_get(ip, path, timeout=TIMEOUT):
    try:
        r = urlopen(f"http://{ip}{path}", timeout=timeout)
        return r.read().decode()
    except Exception:
        return None

def api_get_json(ip, path, timeout=TIMEOUT):
    raw = api_get(ip, path, timeout)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None

def api_post_json(ip, path, data, timeout=TIMEOUT):
    try:
        body = json.dumps(data).encode()
        req = Request(f"http://{ip}{path}", data=body,
                      headers={"Content-Type": "application/json"})
        r = urlopen(req, timeout=timeout)
        return r.read().decode()
    except Exception:
        return None

# ── SSH helpers ──────────────────────────────────────────────────────────────

def _ssh_find_user(ip):
    """Find which SSH user works for this device (bcmeter or pi)."""
    for user in SSH_USERS:
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"{user}@{ip}", "echo ok"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and "ok" in result.stdout:
            return user
    return None

def _ssh_run(ip, user, cmd, timeout=300):
    """Run a command on a remote device via SSH. Returns (ok, output)."""
    try:
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"{user}@{ip}", cmd],
            capture_output=True, text=True, timeout=timeout
        )
        out = result.stdout.strip()
        if result.returncode != 0:
            err = result.stderr.strip()
            return (False, err or out or f"exit {result.returncode}")
        return (True, out)
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    except Exception as e:
        return (False, str(e))

def _scp_upload(ip, user, local_path, remote_path):
    """Upload a file via SCP. Returns (ok, message)."""
    try:
        result = subprocess.run(
            ["scp"] + SSH_OPTS + [local_path, f"{user}@{ip}:{remote_path}"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return (True, "OK")
        return (False, result.stderr.strip() or f"exit {result.returncode}")
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    except Exception as e:
        return (False, str(e))

# ── Discovery ────────────────────────────────────────────────────────────────

def _mdns_browse():
    """Browse mDNS for bcmeter devices, return deduplicated (name, ip) list."""
    proc = subprocess.Popen(
        ["dns-sd", "-B", "_http._tcp", "local."],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    time.sleep(3)
    proc.kill()
    out = proc.communicate()[0]

    seen = set()
    names = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[1] == "Add":
            name = " ".join(parts[6:])
            if "bcmeter" in name.lower() and name not in seen:
                seen.add(name)
                names.append(name)

    seen_ips = set()
    results = []
    for name in names:
        try:
            ip = socket.getaddrinfo(f"{name}.local", 80, socket.AF_INET)[0][4][0]
            if ip not in seen_ips:
                seen_ips.add(ip)
                results.append((name, ip))
        except Exception:
            pass
    return results


def _classify_device(ip):
    """Check if a device is v2 Pi, v1 Pi, or other. Returns 'v2', 'v1', or None."""
    # Try v2 API
    info = api_get_json(ip, "/api/status", timeout=3)
    if info and info.get("env") == "pi":
        return "v2"
    # Try v1 Flask endpoint (port 5000)
    v1 = api_get_json(ip, ":5000/load-config", timeout=3)
    if v1 is not None:
        return "v1"
    return None


def _arp_scan_pis():
    """Scan ARP table for Raspberry Pi MAC prefixes. Returns list of (name, ip).
    Uses -n flag to skip slow DNS reverse lookups."""
    PI_MAC_PREFIXES = ("b8:27:eb", "dc:a6:32", "e4:5f:01")
    results = []
    try:
        # -n: numeric only (no DNS), -a: all entries
        out = subprocess.check_output(["arp", "-na"], text=True, timeout=5)
        for line in out.splitlines():
            lower = line.lower()
            for prefix in PI_MAC_PREFIXES:
                if prefix in lower:
                    # macOS format: "? (192.168.x.x) at b8:27:eb:... on en0 ..."
                    parts = line.split()
                    ip = None
                    for p in parts:
                        if p.startswith("(") and p.endswith(")"):
                            ip = p.strip("()")
                            break
                    if ip:
                        # Try to get a friendly name via mDNS
                        try:
                            host = socket.gethostbyaddr(ip)[0]
                            name = host.split(".")[0]  # strip .fritz.box / .local
                        except Exception:
                            name = f"pi-{ip.split('.')[-1]}"
                        results.append((name, ip))
                    break
    except Exception:
        pass
    return results


def discover(include_v1=False):
    """Discover Pi devices. Returns list of (name, ip) for v2 devices,
    or (name, ip, arch) tuples when include_v1=True."""
    # Primary: mDNS service browse
    candidates = _mdns_browse()

    # Fallback: ARP table scan for Raspberry Pi MACs
    seen_ips = {ip for _, ip in candidates}
    for name, ip in _arp_scan_pis():
        if ip not in seen_ips:
            seen_ips.add(ip)
            candidates.append((name, ip))

    devices = []
    for name, ip in candidates:
        arch = _classify_device(ip)
        if arch == "v2":
            devices.append((name, ip, "v2") if include_v1 else (name, ip))
        elif arch == "v1" and include_v1:
            devices.append((name, ip, "v1"))
    return devices


def resolve_targets(args, include_v1=False):
    if args.ip:
        ips = [("manual", ip.strip()) for ip in args.ip.split(",")]
        if include_v1:
            return [(n, ip, _classify_device(ip) or "?") for n, ip in ips]
        return ips
    label = "bcMeter Pi devices" if not include_v1 else "bcMeter Pi devices (v1+v2)"
    print(f"Discovering {label}...")
    devices = discover(include_v1=include_v1)
    if not devices:
        print("No Pi devices found.")
        sys.exit(1)
    if include_v1:
        v1_count = sum(1 for *_, a in devices if a == "v1")
        v2_count = sum(1 for *_, a in devices if a == "v2")
        print(f"Found {len(devices)} device(s) ({v2_count} v2, {v1_count} v1)\n")
    else:
        print(f"Found {len(devices)} Pi device(s)\n")
    return devices

# ── Device info ──────────────────────────────────────────────────────────────

def get_device_info(ip):
    status = api_get_json(ip, "/api/status")
    if not status:
        return None
    return {
        "version": status.get("version", "?"),
        "sampling": status.get("status") == 2,
        "status_code": status.get("status", -1),
        "error": status.get("error_msg", ""),
        "bc": status.get("bc", 0),
        "atn": status.get("atn", 0),
        "samples": status.get("samples", 0),
        "wifi_mode": status.get("wifi_mode", "?"),
        "wifi_ssid": status.get("wifi_ssid", ""),
        "internet": status.get("internet", False),
        "time_synced": status.get("time_synced", False),
        "name": status.get("name", "?"),
        "env": status.get("env", "?"),
    }

def local_version():
    try:
        init_file = os.path.join(SCRIPT_DIR, "bcmeter", "__init__.py")
        with open(init_file) as f:
            for line in f:
                if line.startswith("__version__"):
                    return line.split("=")[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None

STATUS_LABELS = {0: "stopped", 1: "warming up", 2: "sampling", 3: "error"}

# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_status(devices, args):
    local_ver = local_version()
    if local_ver:
        print(f"Local code version: {local_ver}\n")

    def check(name, ip):
        info = get_device_info(ip)
        return (name, ip, info)

    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        futures = [pool.submit(check, n, ip) for n, ip in devices]
        for f in as_completed(futures):
            name, ip, info = f.result()
            if not info:
                print(f"  {name:20s} {ip:16s} unreachable")
                continue
            state = STATUS_LABELS.get(info["status_code"], "unknown")
            ver = info["version"]
            outdated = f" (outdated!)" if local_ver and ver != local_ver else ""
            sync = "synced" if info["time_synced"] else "no time"
            net = info["wifi_ssid"] if info["wifi_mode"] == "sta" else "AP mode"
            line = f"v{ver}{outdated}  {state:12s}  {sync:8s}  {net}"
            if info["sampling"]:
                line += f"  BC={info['bc']:.1f} ATN={info['atn']:.2f} n={info['samples']}"
            print(f"  {name:20s} {ip:16s} {line}")


def cmd_start(devices, args):
    force = "--force" in sys.argv
    def do(name, ip):
        info = get_device_info(ip)
        if info and info["status_code"] in (1, 2):
            state = STATUS_LABELS.get(info["status_code"], "running")
            return (name, ip, f"already {state}, skipped")
        param = "&force=1" if force else ""
        resp = api_get(ip, f"/api/control?action=start{param}")
        return (name, ip, resp)
    _parallel_action(devices, do, "Starting")


def cmd_stop(devices, args):
    def do(name, ip):
        return (name, ip, api_get(ip, "/api/control?action=stop"))
    _parallel_action(devices, do, "Stopping")


CAL_KEYS = ["cal_k_880nm", "cal_k_520nm", "cal_k_370nm"]

def cmd_calibrate(devices, args):
    def do(name, ip):
        resp = api_get(ip, "/api/control?action=calibrate")
        return (name, ip, resp)

    print("Starting calibration on all devices...")
    _parallel_action(devices, do, "Calibrating")
    print("\nPolling calibration status (Ctrl+C to stop)...")

    results = {}
    try:
        while True:
            time.sleep(5)
            all_done = True
            for name, ip in devices:
                cal = api_get_json(ip, "/api/calibration")
                if not cal:
                    print(f"  {name:20s} unreachable")
                    continue
                if cal.get("running"):
                    elapsed = cal.get("elapsed_ms", 0) // 1000
                    print(f"  {name:20s} running... {elapsed}s")
                    all_done = False
                elif cal.get("done"):
                    ok = cal.get("ok", False)
                    results[ip] = ok
                    print(f"  {name:20s} {'OK' if ok else 'FAILED'}")
                else:
                    print(f"  {name:20s} idle")
            if all_done:
                break
    except KeyboardInterrupt:
        print("\nStopped polling.")

    if not results:
        return
    print("\nReading correction factors...")
    rows = []
    for name, ip in devices:
        ok = results.get(ip)
        if ok is None:
            rows.append((name, ip, None, {}))
            continue
        factors = {}
        if ok:
            cfg = api_get_json(ip, "/api/config")
            if cfg:
                for k in CAL_KEYS:
                    item = cfg.get(k)
                    if item:
                        factors[k] = item.get("value")
        rows.append((name, ip, ok, factors))

    hdr_k = [k.replace("cal_k_", "") for k in CAL_KEYS]
    print(f"\n  {'Device':20s} {'IP':16s} {'Result':8s}", end="")
    for h in hdr_k:
        print(f" {h:>8s}", end="")
    print()
    print(f"  {'─'*20} {'─'*16} {'─'*8}", end="")
    for _ in hdr_k:
        print(f" {'─'*8}", end="")
    print()
    for name, ip, ok, factors in rows:
        if ok is None:
            print(f"  {name:20s} {ip:16s} {'???':8s}")
            continue
        tag = "OK" if ok else "FAILED"
        print(f"  {name:20s} {ip:16s} {tag:8s}", end="")
        for k in CAL_KEYS:
            v = factors.get(k)
            print(f" {v:8.4f}" if v is not None else f" {'─':>8s}", end="")
        print()


def cmd_reboot(devices, args):
    def do(name, ip):
        return (name, ip, api_get(ip, "/api/control?action=reboot"))
    _parallel_action(devices, do, "Rebooting")


def cmd_shutdown(devices, args):
    names = ", ".join(n for n, _ in devices)
    confirm = input(f"Shutdown {len(devices)} device(s) ({names})? Type YES to confirm: ")
    if confirm.strip() != "YES":
        print("Aborted.")
        return
    def do(name, ip):
        return (name, ip, api_get(ip, "/api/control?action=shutdown"))
    _parallel_action(devices, do, "Shutting down")


def _get_posix_tz():
    """Derive POSIX TZ string from host system."""
    import datetime, platform
    try:
        if platform.system() == "Darwin":
            link = os.readlink("/etc/localtime")
            iana = link.split("zoneinfo/")[-1]
        else:
            iana = datetime.datetime.now().astimezone().tzname()
    except Exception:
        iana = ""

    IANA_TO_POSIX = {
        "Europe/Berlin":       "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Vienna":       "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Zurich":       "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Paris":        "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Amsterdam":    "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Brussels":     "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Rome":         "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Madrid":       "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Warsaw":       "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Stockholm":    "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Copenhagen":   "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Oslo":         "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Prague":       "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/Budapest":     "CET-1CEST,M3.5.0,M10.5.0/3",
        "Europe/London":       "GMT0BST,M3.5.0/1,M10.5.0",
        "Europe/Dublin":       "GMT0IST,M3.5.0/1,M10.5.0",
        "Europe/Athens":       "EET-2EEST,M3.5.0/3,M10.5.0/4",
        "Europe/Helsinki":     "EET-2EEST,M3.5.0/3,M10.5.0/4",
        "Europe/Bucharest":    "EET-2EEST,M3.5.0/3,M10.5.0/4",
        "Europe/Istanbul":     "<+03>-3",
        "Europe/Moscow":       "MSK-3",
        "America/New_York":    "EST5EDT,M3.2.0,M11.1.0",
        "America/Chicago":     "CST6CDT,M3.2.0,M11.1.0",
        "America/Denver":      "MST7MDT,M3.2.0,M11.1.0",
        "America/Los_Angeles": "PST8PDT,M3.2.0,M11.1.0",
        "America/Sao_Paulo":   "<-03>3",
        "Asia/Tokyo":          "JST-9",
        "Asia/Shanghai":       "CST-8",
        "Asia/Kolkata":        "IST-5:30",
        "Asia/Dubai":          "<+04>-4",
        "Australia/Sydney":    "AEST-10AEDT,M10.1.0,M4.1.0/3",
        "Africa/Nairobi":      "EAT-3",
        "Africa/Lagos":        "WAT-1",
    }
    posix = IANA_TO_POSIX.get(iana)
    if posix:
        return posix

    off_h = -time.timezone // 3600
    std = time.tzname[0]
    if time.daylight:
        dst = time.tzname[1]
        return f"{std}{-off_h}{dst}"
    return f"{std}{-off_h}"


def cmd_synctime(devices, args):
    ts = int(time.time())
    tz = _get_posix_tz()
    print(f"Timezone: {tz}")
    def do(name, ip):
        return (name, ip, api_get(ip, f"/api/control?action=synctime&ts={ts}&tz={tz}"))
    _parallel_action(devices, do, "Syncing time")


def cmd_config_get(devices, args):
    for name, ip in devices:
        cfg = api_get_json(ip, "/api/config")
        if not cfg:
            print(f"[{name}] unreachable")
            continue
        print(f"\n[{name}] {ip}")
        for key in sorted(cfg.keys()):
            item = cfg[key]
            print(f"  {key:30s} = {item['value']:>12}  ({item.get('description', '')})")


def cmd_config_set(devices, args):
    if not args.key or not args.value:
        print("Usage: bcmctl_pi config-set --key <key> --value <value>")
        return
    val = args.value
    if val.lower() == "true": val = True
    elif val.lower() == "false": val = False
    else:
        try: val = float(val)
        except ValueError: pass
        else:
            if val == int(val): val = int(val)

    def do(name, ip):
        resp = api_post_json(ip, "/api/config", {args.key: val})
        return (name, ip, resp)
    _parallel_action(devices, do, f"Setting {args.key}={val}")


def cmd_logs(devices, args):
    for name, ip in devices:
        data = api_get_json(ip, "/api/logs")
        if not data:
            print(f"[{name}] unreachable")
            continue
        print(f"\n{'='*60}")
        print(f"[{name}] {ip}  ({data.get('timestamp', '?')})")
        print(f"{'='*60}")
        for section in ["hardware", "measurement", "system", "network"]:
            entries = data.get(section, [])
            if not entries:
                continue
            print(f"\n  {section.upper()}")
            for e in entries:
                level = e.get("s", "info").upper()
                print(f"    [{level:5s}] {e['k']}: {e['v']}")


def cmd_files(devices, args):
    for name, ip in devices:
        files = api_get_json(ip, "/api/files")
        if files is None:
            print(f"[{name}] unreachable")
            continue
        print(f"\n[{name}] {ip}  ({len(files)} files)")
        if not files:
            print("  (no log files)")
            continue
        for f in files:
            size_kb = f.get("size", 0) / 1024
            print(f"  {f['name']:40s} {size_kb:8.1f} KB  {f.get('date', '')}")


def cmd_download(devices, args):
    """Download CSV log files from devices."""
    outdir = args.outdir or "."
    os.makedirs(outdir, exist_ok=True)

    for name, ip in devices:
        files = api_get_json(ip, "/api/files")
        if files is None:
            print(f"[{name}] unreachable")
            continue

        if args.filename:
            targets = [f for f in files if f["name"] == args.filename]
            if not targets:
                print(f"[{name}] file '{args.filename}' not found")
                continue
        else:
            targets = files

        for f in targets:
            fname = f["name"]
            raw = api_get(ip, f"/api/csv?file={fname}", timeout=30)
            if raw is None:
                print(f"  [{name}] {fname} — failed")
                continue
            safe_name = f"{name}_{fname}".replace(" ", "_")
            path = os.path.join(outdir, safe_name)
            with open(path, "w") as out:
                out.write(raw)
            print(f"  [{name}] {fname} → {path} ({len(raw)} bytes)")


def _build_update_archive():
    """Create a tar.gz archive of the bcmeter-pi code for upload."""
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", prefix="bcmeter_pi_update_", delete=False)
    tmp.close()

    def tar_filter(info):
        name = info.name
        for excl in UPDATE_EXCLUDES:
            if excl.startswith("*"):
                if name.endswith(excl[1:]):
                    return None
            elif excl in name:
                return None
        parts = name.split("/")
        for p in parts:
            if p in (".git", "logs", "maintenance_logs", ".claude"):
                return None
        if info.isdir():
            info.mode = 0o755
        elif info.isfile():
            executable = bool(info.mode & 0o111) or name.endswith(".sh")
            info.mode = 0o755 if executable else 0o644
        return info

    with tarfile.open(tmp.name, "w:gz") as tar:
        for item in UPDATE_INCLUDES:
            path = os.path.join(SCRIPT_DIR, item)
            if os.path.exists(path):
                tar.add(path, arcname=item, filter=tar_filter)

    return tmp.name


def _deploy_via_ssh(name, ip, archive, run_installer=False):
    """Upload archive via SCP and optionally run install.py local.
    Returns (name, ip, ok, message)."""
    # Find SSH user
    user = _ssh_find_user(ip)
    if not user:
        return (name, ip, False, "SSH: no working user (tried bcmeter, pi)")

    # Determine home directory
    ok, home = _ssh_run(ip, user, "echo $HOME", timeout=10)
    if not ok or not home:
        home = f"/home/{user}"

    # Upload archive
    remote_archive = "/tmp/bcmeter_update.tar.gz"
    ok, msg = _scp_upload(ip, user, archive, remote_archive)
    if not ok:
        return (name, ip, False, f"SCP failed: {msg}")

    # Extract to home directory
    ok, msg = _ssh_run(ip, user, f"tar xzf {remote_archive} -C {home}/", timeout=60)
    if not ok:
        return (name, ip, False, f"extract failed: {msg}")

    # Clean up archive on device
    _ssh_run(ip, user, f"rm -f {remote_archive}", timeout=10)

    if run_installer:
        # Run install.py local (v1→v2 upgrade or full reinstall)
        # This takes a long time and ends with a reboot
        print(f"  {name:20s} {ip:16s} running installer (this takes several minutes)...")
        ok, msg = _ssh_run(ip, user,
            f"sudo python3 {home}/install.py local 2>&1 | tail -5",
            timeout=600)
        if not ok:
            # install.py reboots at the end, so connection drop = success
            if "timeout" in msg.lower() or "closed" in msg.lower() or "reset" in msg.lower():
                return (name, ip, True, "installer finished, rebooting")
            return (name, ip, False, f"installer failed: {msg}")
        return (name, ip, True, "installer finished, rebooting")
    else:
        # Just restart the service (v2 code update)
        ok, msg = _ssh_run(ip, user,
            "sudo systemctl restart bcMeter.service", timeout=30)
        if not ok:
            return (name, ip, False, f"restart failed: {msg}")
        return (name, ip, True, "OK, service restarted")


def cmd_update(devices, args):
    """Push code update to v2 devices via SCP + service restart."""
    archive = args.firmware
    if archive and not os.path.isfile(archive):
        print(f"Archive not found: {archive}")
        sys.exit(1)

    if not archive:
        print("Building update archive...")
        archive = _build_update_archive()
        print(f"Archive: {archive} ({os.path.getsize(archive) / 1024:.1f} KB)")

    local_ver = local_version()
    print(f"Local code version: {local_ver or '?'}\n")

    # Show current versions
    print("Current device versions:")
    for name, ip in devices:
        info = get_device_info(ip)
        ver = info["version"] if info else "unreachable"
        print(f"  {name:20s} {ip:16s} v{ver}")

    if not args.force:
        confirm = input(f"\nUpdate {len(devices)} device(s)? [y/N]: ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return

    print(f"\nDeploying to {len(devices)} device(s)...")
    results = []
    # Sequential — SSH sessions can conflict in parallel on same device
    for name, ip in devices:
        result = _deploy_via_ssh(name, ip, archive, run_installer=False)
        _, _, ok, msg = result
        tag = "OK" if ok else "FAIL"
        print(f"  [{tag}] {name:20s} {ip:16s} {msg}")
        results.append(result)

    # Clean up temp archive
    if not args.firmware:
        try: os.remove(archive)
        except Exception: pass

    ok_count = sum(1 for *_, ok, _ in results if ok)
    print(f"\nDeploy: {ok_count}/{len(devices)} succeeded.")

    if ok_count == 0:
        return

    print("Waiting 10s for service restart...")
    time.sleep(10)

    print("\nVerifying (polling every 3s, timeout 60s):")
    ok_devices = [(n, ip) for n, ip, ok, _ in results if ok]
    verified = set()
    deadline = time.time() + 50  # 10s already waited = 60s total
    while ok_devices and time.time() < deadline:
        for name, ip in ok_devices:
            if (name, ip) in verified:
                continue
            info = get_device_info(ip)
            if info:
                match = " OK" if local_ver and info["version"] == local_ver else ""
                print(f"  {name:20s} {ip:16s} v{info['version']}{match}")
                verified.add((name, ip))
        if len(verified) == len(ok_devices):
            break
        time.sleep(3)
    for name, ip in ok_devices:
        if (name, ip) not in verified:
            print(f"  {name:20s} {ip:16s} unreachable after 60s")


def cmd_upgrade(devices_with_arch, args):
    """Upgrade v1 devices to v2, or reinstall v2 devices via install.py local.
    Expects devices as (name, ip, arch) tuples."""
    archive = args.firmware
    if archive and not os.path.isfile(archive):
        print(f"Archive not found: {archive}")
        sys.exit(1)

    if not archive:
        print("Building update archive...")
        archive = _build_update_archive()
        print(f"Archive: {archive} ({os.path.getsize(archive) / 1024:.1f} KB)")

    local_ver = local_version()
    print(f"Local code version: {local_ver or '?'}\n")

    print("Devices to upgrade:")
    for name, ip, arch in devices_with_arch:
        label = "v1 → v2 UPGRADE" if arch == "v1" else "v2 reinstall"
        print(f"  {name:20s} {ip:16s} [{label}]")

    if not args.force:
        confirm = input(f"\nUpgrade {len(devices_with_arch)} device(s)? This runs install.py and reboots. [y/N]: ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return

    print(f"\nUpgrading {len(devices_with_arch)} device(s) (this takes several minutes per device)...\n")
    results = []
    for name, ip, arch in devices_with_arch:
        print(f"{'─'*60}")
        print(f"  [{arch}] {name} ({ip})")
        result = _deploy_via_ssh(name, ip, archive, run_installer=True)
        _, _, ok, msg = result
        tag = "OK" if ok else "FAIL"
        print(f"  [{tag}] {msg}")
        results.append((name, ip, arch, ok, msg))

    # Clean up temp archive
    if not args.firmware:
        try: os.remove(archive)
        except Exception: pass

    ok_count = sum(1 for *_, ok, _ in results if ok)
    print(f"\n{'='*60}")
    print(f"Upgrade: {ok_count}/{len(devices_with_arch)} succeeded.")

    if ok_count == 0:
        return

    print("Waiting 10s for devices to reboot and start services...")
    time.sleep(10)

    print("\nVerifying (polling every 3s, timeout 60s):")
    ok_devices = [(n, ip) for n, ip, arch, ok, _ in results if ok]
    verified = set()
    deadline = time.time() + 50  # 10s already waited = 60s total
    while ok_devices and time.time() < deadline:
        for name, ip in ok_devices:
            if (name, ip) in verified:
                continue
            info = get_device_info(ip)
            if info:
                match = " OK" if local_ver and info["version"] == local_ver else ""
                print(f"  {name:20s} {ip:16s} v{info['version']}{match}  env={info['env']}")
                verified.add((name, ip))
        if len(verified) == len(ok_devices):
            break
        time.sleep(3)
    for name, ip in ok_devices:
        if (name, ip) not in verified:
            print(f"  {name:20s} {ip:16s} unreachable after 60s (install takes ~5 min)")


def cmd_ssh(devices, args):
    """Run an SSH command on all Pi devices."""
    if not args.ssh_cmd:
        print("Usage: bcmctl_pi ssh --ssh-cmd 'command to run'")
        return
    cmd = args.ssh_cmd

    def do(name, ip):
        user = _ssh_find_user(ip)
        if not user:
            return (name, ip, "SSH: no working user")
        ok, out = _ssh_run(ip, user, cmd, timeout=30)
        return (name, ip, out or "(no output)")

    print(f"Running on {len(devices)} device(s): {cmd}\n")
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        futures = [pool.submit(do, n, ip) for n, ip in devices]
        for f in as_completed(futures):
            name, ip, out = f.result()
            print(f"[{name}] {ip}")
            for line in out.splitlines():
                print(f"  {line}")
            print()


def cmd_api(devices, args):
    path = args.path
    if not path:
        print("Usage: bcmctl_pi api --path /api/status [--data '{\"key\":\"val\"}']")
        return
    if not path.startswith("/"):
        path = "/" + path

    if args.data:
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}")
            return
        for name, ip in devices:
            print(f"\n[{name}] {ip}  POST {path}")
            resp = api_post_json(ip, path, data)
            if resp is None:
                print("  failed/unreachable")
            else:
                _print_resp(resp)
    else:
        for name, ip in devices:
            print(f"\n[{name}] {ip}  GET {path}")
            resp = api_get(ip, path)
            if resp is None:
                print("  failed/unreachable")
            else:
                _print_resp(resp)


def _print_resp(raw):
    try:
        obj = json.loads(raw)
        print(f"  {json.dumps(obj, indent=2, ensure_ascii=False)}")
    except (json.JSONDecodeError, TypeError):
        for line in raw.strip().splitlines():
            print(f"  {line}")


def cmd_menu(devices, args):
    MENU = [
        ("Status",        "Show device status and versions",      cmd_status),
        ("Start",         "Start measurement on all devices",     cmd_start),
        ("Stop",          "Stop measurement on all devices",      cmd_stop),
        ("Calibrate",     "Run calibration with live progress",   cmd_calibrate),
        ("Sync time",     "Sync device clocks to this machine",   cmd_synctime),
        ("Logs",          "Show system logs",                     cmd_logs),
        ("Files",         "List log files on devices",            cmd_files),
        ("Config",        "Dump all configuration parameters",    cmd_config_get),
        ("Reboot",        "Reboot all devices",                   cmd_reboot),
        ("Update",        "Push code update (SCP + restart)",     cmd_update),
        ("Shutdown",      "Shutdown all devices (danger)",        cmd_shutdown),
    ]

    while True:
        print(f"\n{'─'*50}")
        print(f"  bcMeter Pi maintenance  ({len(devices)} device(s))")
        print(f"{'─'*50}")
        for i, (label, desc, _) in enumerate(MENU, 1):
            print(f"  {i:2d}) {label:16s} {desc}")
        print(f"   q) Quit")
        print()

        choice = input("Pick action: ").strip().lower()
        if choice in ("q", "quit", "exit", ""):
            break
        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(MENU)):
                raise ValueError
        except ValueError:
            print("Invalid choice.")
            continue

        label, _, fn = MENU[idx]
        print()
        try:
            fn(devices, args)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        print()
        input("Press Enter to continue...")


def _parallel_action(devices, fn, label):
    print(f"{label} {len(devices)} device(s)...")
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        futures = [pool.submit(fn, n, ip) for n, ip in devices]
        for f in as_completed(futures):
            name, ip, resp = f.result()
            status = resp.strip() if resp else "failed/unreachable"
            print(f"  {name:20s} {ip:16s} {status}")

# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="bcMeter Pi fleet management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  status          Show all device status and versions
  start           Start measurement (--force to skip checks)
  stop            Stop measurement
  calibrate       Run calibration with live progress
  reboot          Reboot devices
  shutdown        Shutdown devices (requires YES confirmation)
  synctime        Sync device clocks to this machine's time
  logs            Show system logs from all devices
  files           List log files on devices
  download        Download CSV files (--filename for specific file)
  config-get      Dump all configuration parameters
  config-set      Set a config value (--key <k> --value <v>)
  update          Push code update via SCP + service restart (v2 only)
  upgrade         Full upgrade via SCP + install.py (v1→v2 or v2 reinstall)
  ssh             Run SSH command on all devices (--ssh-cmd 'cmd')
  api             Raw API call (--path /api/... [--data '{"k":"v"}'])
  menu            Interactive maintenance menu (default)

options:
  --ip 1.2.3.4              Target specific IP(s), comma-separated
  --firmware path.tar.gz     Use specific archive for update/upgrade
  --path /api/endpoint       API path for 'api' command
  --data '{"key":"val"}'     JSON body for POST (omit for GET)
  --ssh-cmd 'command'        Command for 'ssh' command
  --filename name.csv        Specific file for 'download' command
  --outdir ./downloads       Output directory for 'download' command
""")
    parser.add_argument("command", nargs="?", default="menu",
                        choices=["menu", "status", "start", "stop", "calibrate",
                                 "reboot", "shutdown", "synctime",
                                 "logs", "files", "download",
                                 "config-get", "config-set",
                                 "update", "upgrade",
                                 "ssh", "api"])
    parser.add_argument("--ip", help="Target specific IP(s), comma-separated")
    parser.add_argument("--force", action="store_true", help="Force start/update without checks")
    parser.add_argument("--firmware", help="Update archive path (tar.gz or zip)")
    parser.add_argument("--key", help="Config key for config-set")
    parser.add_argument("--value", help="Config value for config-set")
    parser.add_argument("--path", help="API path for api command")
    parser.add_argument("--data", help="JSON body for POST (omit for GET)")
    parser.add_argument("--ssh-cmd", help="Command to run via SSH")
    parser.add_argument("--filename", help="Specific file for download")
    parser.add_argument("--outdir", help="Output directory for download")
    args = parser.parse_args()

    # upgrade command discovers both v1 and v2
    if args.command == "upgrade":
        devices = resolve_targets(args, include_v1=True)
        cmd_upgrade(devices, args)
        return

    devices = resolve_targets(args)

    cmds = {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "calibrate": cmd_calibrate,
        "reboot": cmd_reboot,
        "shutdown": cmd_shutdown,
        "synctime": cmd_synctime,
        "logs": cmd_logs,
        "files": cmd_files,
        "download": cmd_download,
        "config-get": cmd_config_get,
        "config-set": cmd_config_set,
        "update": cmd_update,
        "ssh": cmd_ssh,
        "api": cmd_api,
        "menu": cmd_menu,
    }
    cmds[args.command](devices, args)

if __name__ == "__main__":
    main()
