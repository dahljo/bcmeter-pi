# Changelog

## v1.6.11 (unreleased)

- Remove product-specific mode compatibility from the Raspberry Pi bcMeter base.
- Reset stale measurement errors, warnings, filter state, and warm-up state when a new session begins.
- Report authoritative warm-up progress through `/api/status`, including the frozen progress at which an error stopped a session.
- Keep the 65°C emergency cutoff unchanged; it is already above the 60°C rated operating maximum of optional D6F and SPS30 peripherals.
- Remove private QC, lab-control, maintenance CLI, and arbitrary archive-upload surfaces from the public DIY tree.
- Require a SHA-256-pinned GitHub Release asset and path-safe extraction for OTA updates.
- Store configuration and WiFi credential files atomically with owner-only permissions.

## v1.6.10

- Improve optical stability during measurement with proactive saturated-read retry and LED recovery handling.
- Add reference-signal drop detection with `REF_DROP` notes and optional notification cooldown.
- Improve calibration behavior with explicit calibration state tracking and configurable LED warmup time.
- Include firmware version and WiFi SSID metadata in Lambda/email upload payloads.
- Pause idle filter LED checks while calibration is running.
