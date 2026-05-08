# bcMeter Pi

Redesigned Raspberry Pi Python codebase for the [bcMeter](https://bcmeter.org) — a portable black carbon measurement device for citizen science and air quality monitoring.

This repository replaces the legacy Raspberry Pi codebase with a redesign focused on maintainability, reliability, and a clearer installation and update workflow. The legacy Raspberry Pi repository remains available at [dahljo/bcmeter](https://github.com/dahljo/bcmeter).

## Features

- Optical absorption measurement for black carbon monitoring
- ADS8344E (SPI) + MCP342X (I2C) ADC support
- FastAPI-based web API and configuration interface
- OTA updates via GitHub Releases
- CSV data logging with session management
- Optional BME280 / SHT4x environmental sensors
- Optional SPS30 particulate matter sensor
- Optional GPS positioning
- Optional SIM7080G cellular modem support
- Multi-wavelength measurement support is in development

## Installation

Recommended: download the current Raspberry Pi bcMeter image from [bcmeter.org](https://bcmeter.org), flash it to a microSD card, boot the Raspberry Pi, then complete setup through the bcMeter web interface.

Manual installation is also possible on a fresh Raspberry Pi OS Lite installation. After booting the Pi with network access, log in and run:

```bash
wget -N https://raw.githubusercontent.com/dahljo/bcmeter-pi/main/install.sh
sudo bash install.sh
```

## Project Structure

- `bcmeter/` — Core modules (sensors, optics, pump, config, measurement engine)
- `api/` — FastAPI REST API routes
- `interface/` — Web frontend assets
- `main.py` — Application entry point
- `bcMeter_config.json` — Default configuration template; installed devices keep their local runtime config during updates

## Helper Scripts

- `install.sh` — Bootstrap wrapper for fresh Raspberry Pi OS Lite installs; it downloads/updates the installer and runs it inside `screen`.
- `install.py` — Main installer/updater used by the image, manual installs, OTA updates, and v1-to-v2 migrations.
- `bcmctl_pi.py` — LAN maintenance CLI for discovering Pi devices, checking status, starting/stopping measurement, syncing time, downloading CSV files, and pushing controlled updates.
- `bcmeter-qc.py` — On-device QC entry point that runs the Raspberry Pi bcMeter quality-check workflow through the local API.

## License

Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0).
For commercial licensing, visit [bcmeter.org](https://bcmeter.org).
