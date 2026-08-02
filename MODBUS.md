# bcMeter Modbus TCP Integration

Optional read-only Modbus TCP server for building management / SCADA systems
(e.g. fire+). Off by default; enable it in the web interface under
**Settings > Device > Integration > "Modbus TCP server (port 502, read-only)"**
or via the JSON API:

```
POST /api/config
{"modbus_tcp": true}
```

The setting applies live (no reboot) and persists across restarts. The device
also documents itself: `GET /api/modbus` returns the server state and the full
register map as JSON.

## Protocol

| Property | Value |
|---|---|
| Transport | Modbus TCP, port 502 |
| Function codes | 3 (read holding registers), 4 (read input registers) - both serve the same table |
| Access | Strictly read-only; any other function code returns exception 01 (ILLEGAL FUNCTION) |
| Unit id | Ignored, echoed back |
| Word order | Big-endian (ABCD) for float32/uint32, the Modbus default |
| Concurrent clients | 2 (ESP32) / 4 (Pi); idle connections are closed after 120 s |

The Raspberry Pi build falls back to port 1502 when it cannot bind 502.

## Register map (version 1)

One contiguous block of 32 registers, 0-based addressing, pollable in a single
read (`FC 3/4, address 0, quantity 32`). Measurement values update once per
sample cycle while sampling, identical to `/api/status` and the CSV.

| Address | Registers | Name | Type | Unit | Description |
|---|---|---|---|---|---|
| 0 | 1 | map_version | uint16 | | Register map version (1) |
| 1 | 1 | status | uint16 | | 0=idle, 1=warmup, 2=sampling, 3=error (error takes precedence) |
| 2 | 1 | error | uint16 | | Error code, 0=none |
| 3 | 1 | valid | uint16 | | Data-valid bits: 0=optics, 1=temp/hum, 2=pressure, 3=pm |
| 4 | 1 | filter | uint16 | | Filter loading, 5=fresh to 1=replace |
| 5 | 1 | duty | uint16 | | Pump duty cycle, 0-255 |
| 6 | 1 | session | uint16 | | 1=logging session active |
| 7 | 1 | reserved | uint16 | | Always 0 |
| 8 | 2 | samples | uint32 | | Samples this session |
| 10 | 2 | uptime | uint32 | s | Seconds since device start |
| 12 | 2 | bc | float32 | ng/m3 | Black carbon, 880 nm, Kalman-filtered |
| 14 | 2 | atn | float32 | | Attenuation, 880 nm |
| 16 | 2 | flow | float32 | L/min | Airflow |
| 18 | 2 | sen | float32 | V | Sensor photodiode voltage |
| 20 | 2 | ref | float32 | V | Reference photodiode voltage |
| 22 | 2 | temperature | float32 | degC | Ambient temperature |
| 24 | 2 | humidity | float32 | %RH | Relative humidity |
| 26 | 2 | pressure | float32 | hPa | Barometric pressure |
| 28 | 2 | pm25 | float32 | ug/m3 | Particulate matter PM2.5 |
| 30 | 2 | pm10 | float32 | ug/m3 | Particulate matter PM10 |

A cleared bit in the `valid` register means the corresponding sensor is not
fitted and its values read 0. Status/error codes match the `/api/status` JSON
fields of the same names. Temperature, humidity and pressure also refresh
about every 10 s while idle; PM values update while sampling. `uptime` counts
from device boot on both platforms.

Legacy exception: Pi v1 devices with only a DS18B20 temperature sensor report
a temperature with `valid` bit 1 cleared (no humidity source).

## Implementation

- Raspberry Pi: `bcmeter/modbus_tcp.py` (thread "modbus"; select-driven,
  stdlib only, no external Modbus dependency)
- ESP32 parity: `bcmeter-esp32/components/bcmeter/src/modbus_tcp.cpp` (FreeRTOS task "Modbus")

Both implementations follow the `modbus_tcp` config key at runtime and share
the identical register map; keep them in sync (parity rule).
