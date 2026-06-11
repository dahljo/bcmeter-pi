# Changelog

## v1.6.10

- Improve optical stability during measurement with proactive saturated-read retry and LED recovery handling.
- Add reference-signal drop detection with `REF_DROP` notes and optional notification cooldown.
- Improve calibration behavior with explicit calibration state tracking and configurable LED warmup time.
- Include firmware version and WiFi SSID metadata in Lambda/email upload payloads.
- Pause idle filter LED checks while calibration is running.
