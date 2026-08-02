# Changelog

## v1.6.11 (unreleased)

- Remove product-specific mode compatibility from the Raspberry Pi bcMeter base.
- Reset stale measurement errors, warnings, filter state, and warm-up state when a new session begins.
- Report authoritative warm-up progress through `/api/status`, including the frozen progress at which an error stopped a session.
- Treat 65°C as a thermal incident by default: finalize the active session cleanly, stop the pump, send one incident notification, and expose a bounded 40–80°C threshold in DEV settings.
- Use the configured `bcMeter-XXXX` device name for current, archived, combined, and viewed CSV download filenames.
- Harden pump startup and low-flow recovery so measurement sessions start airflow predictably without masking thermal incidents as airflow failures.
- Add read-only Modbus TCP integration and resilient log handling across late system-time synchronization.
- Remove private QC, lab-control, maintenance CLI, and arbitrary archive-upload surfaces from the public DIY tree.
- Require a SHA-256-pinned GitHub Release asset and path-safe extraction for OTA updates.
- Store configuration and WiFi credential files atomically with owner-only permissions.

## v1.6.10

- Improve optical stability during measurement with proactive saturated-read retry and LED recovery handling.
- Add reference-signal drop detection with `REF_DROP` notes and optional notification cooldown.
- Improve calibration behavior with explicit calibration state tracking and configurable LED warmup time.
- Include firmware version and WiFi SSID metadata in Lambda/email upload payloads.
- Pause idle filter LED checks while calibration is running.
