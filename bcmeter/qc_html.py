"""Self-contained HTML rendering for Raspberry Pi QC reports."""
from __future__ import annotations

import html
from datetime import datetime
from typing import Any


GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#f59e0b"
GRAY = "#888"
BLUE = "#0056b3"

STATUS_COLOR = {
    "pass": GREEN,
    "fail": RED,
    "warn": AMBER,
    "skip": GRAY,
}
STATUS_ICON = {
    "pass": "&#9745;",
    "fail": "&#9744;",
    "warn": "&#9888;",
    "skip": "&mdash;",
}

GROUP_ORDER = [
    "Phase A",
    "Phase B",
    "Phase C",
    "Phase D",
    "Phase E",
    "Power & Boot",
    "Optical Sensors (880nm)",
    "Airflow & Filter",
    "WiFi & Internet",
    "Sensors",
    "PM2.5 Sensor",
    "BME280",
    "GPS",
    "4G/Mobile",
    "Diagnostic",
]


def esc(value: Any) -> str:
    if value is None:
        return "N/A"
    return html.escape(str(value))


def _status_label(status: str) -> str:
    return {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}.get(
        status, status.upper()
    )


def _step_group(step: dict[str, Any]) -> str:
    name = str(step.get("name") or "")
    if name in {"ADC", "pigpio", "MCP342x", "SHT4x"}:
        return "Phase A" if name != "SHT4x" else "Sensors"
    if name.startswith("LED"):
        return "Optical Sensors (880nm)"
    if name == "Calibration":
        return "Phase D"
    if name in {"Final state idle", "Calibration timestamp persisted"}:
        return "Phase E"
    if name in {"Time", "bcMeter service active after QC"}:
        return "Power & Boot"
    if name.startswith("Pump"):
        return "Airflow & Filter"
    if name.startswith("WiFi") or name == "Internet reachable":
        return "WiFi & Internet"
    if name == "SPS30":
        return "PM2.5 Sensor"
    if name == "BME280":
        return "BME280"
    if name == "GPS":
        return "GPS"
    if name == "4G modem":
        return "4G/Mobile"
    if "Factory reset" in name:
        return "Phase B"
    return "Diagnostic"


def _step_status(step: dict[str, Any]) -> str:
    if step.get("passed"):
        return "pass"
    if step.get("hard"):
        return "fail"
    name = str(step.get("name") or "")
    observed = str(step.get("observed") or "")
    if name in {"SPS30", "BME280", "GPS", "4G modem"} and "missing" in observed:
        return "skip"
    return "warn"


def _step_note(step: dict[str, Any]) -> str:
    details = step.get("details") if isinstance(step.get("details"), dict) else {}
    name = str(step.get("name") or "")
    if name.startswith("Pump"):
        kind = str(details.get("kind") or "")
        if kind == "ceiling":
            return "MCP3428/I2C flow ADC clips before this target; ceiling is non-blocking on Pi."
        if kind == "bonus":
            return "Non-blocking low-flow diagnostic."
    if name in {"SPS30", "BME280", "4G modem"} and not step.get("passed"):
        return "Optional/non-hard-pass on Raspberry Pi QC."
    if name == "GPS" and not step.get("passed"):
        return "Optional on Raspberry Pi QC."
    return ""


def _grouped_steps(steps: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        groups.setdefault(_step_group(step), []).append(step)
    ordered = []
    for group in GROUP_ORDER:
        if group in groups:
            ordered.append((group, groups.pop(group)))
    ordered.extend(groups.items())
    return ordered


def _render_checks(steps: list[dict[str, Any]]) -> str:
    lines = ['<table class="checks-table">']
    for group, items in _grouped_steps(steps):
        statuses = [_step_status(item) for item in items]
        if "fail" in statuses:
            group_status = "fail"
            note = f'<span class="group-note">{statuses.count("fail")} check failed</span>'
        elif "warn" in statuses:
            group_status = "warn"
            note = f'<span class="group-note warn">{statuses.count("warn")} warning</span>'
        elif statuses and all(status == "skip" for status in statuses):
            group_status = "skip"
            note = '<span class="group-note muted">not detected</span>'
        else:
            group_status = "pass"
            note = ""
        color = STATUS_COLOR[group_status]
        lines.append(
            '<tr class="group-row">'
            f'<td class="icon" style="color:{color};">{STATUS_ICON[group_status]}</td>'
            f'<td class="group-label">{esc(group)} {note}</td>'
            '<td></td>'
            f'<td class="status" style="color:{color};">{_status_label(group_status)}</td>'
            '</tr>'
        )
        for item in items:
            status = _step_status(item)
            color = STATUS_COLOR[status]
            note = _step_note(item)
            note_html = f' <em>{esc(note)}</em>' if note else ""
            lines.append(
                '<tr class="sub-row">'
                f'<td class="icon" style="color:{color};">{STATUS_ICON[status]}</td>'
                f'<td class="sub-label">{esc(item.get("name"))}{note_html}</td>'
                f'<td><code>{esc(item.get("observed") or "")}</code></td>'
                f'<td class="status" style="color:{color};">{_status_label(status)}</td>'
                '</tr>'
            )
    lines.append("</table>")
    return "\n".join(lines)


def _packlist() -> str:
    items = [
        "Rugged case/latches/gasket checked",
        "Hose and air inlet clear, no kinks",
        "Filter holder clean, spring OK",
        "Pump mechanically fixed",
        "5V fan connected to 3.3V header for quiet operation",
        "Device name sticker/label applied",
        "10 filter papers packed",
        "USB power cable/adapter packed",
        "Hose/fittings packed",
        "Carton label applied",
        "Final sign-off complete",
    ]
    rows = [
        "<table>",
        *(
            "<tr>"
            '<td class="box">&#9744;</td>'
            f"<td>{esc(item)}</td>"
            '<td><em>manual</em></td>'
            "</tr>"
            for item in items
        ),
        "</table>",
    ]
    return "\n".join(rows)


def _diag_table(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    values = {
        "ADC": summary.get("adc_type"),
        "ADC vref": summary.get("adc_vref"),
        "Flow ADC limit ml/min": summary.get("flow_adc_limit_ml"),
        "Calibration": summary.get("last_cal"),
        "Profile": summary.get("profile"),
        "API base": summary.get("api_base"),
        "Report dir": report.get("report_dir"),
    }
    lines = ["<h3>Diagnostic</h3>", "<table>"]
    for key, value in values.items():
        lines.append(f"<tr><td>{esc(key)}</td><td><code>{esc(value)}</code></td></tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _css() -> str:
    return """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;margin:28px;color:#1a1d23;background:#f6f8fb;font-size:13px;line-height:1.35}
h1{margin:0 0 6px;color:#0f172a;font-size:28px}
h2{margin:24px 0 10px;font-size:18px;color:#0f172a}
h3{margin:16px 0 8px;font-size:14px;color:#0f172a}
.meta{color:#667085;font-size:12px}
.summary-box{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px;margin:16px 0}
.summary-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;text-align:center}
.summary-num{font-size:24px;font-weight:800}.blue{color:#0056b3}.green{color:#22c55e}.red{color:#ef4444}
.device-card{background:#fff;border:1px solid #e5e7eb;border-left:5px solid #22c55e;border-radius:8px;margin-top:12px;padding:0}
.device-card.has-fail{border-left-color:#ef4444}.device-summary{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:12px 14px;border-bottom:1px solid #edf0f4}
.device-name{font-size:16px;font-weight:750}.badge{border-radius:999px;padding:4px 9px;font-size:11px;font-weight:800}.badge-pass{background:#dcfce7;color:#166534}.badge-fail{background:#fee2e2;color:#991b1b}
.device-body{padding:0 14px 14px}
table{width:100%;border-collapse:collapse;background:#fff}td,th{border-bottom:1px solid #edf0f4;padding:7px 8px;text-align:left;vertical-align:top}
.checks-table .group-row{background:#f8fafc}.checks-table .group-label{font-weight:800}.checks-table .status{font-weight:800;text-align:right;width:64px}.checks-table .icon{width:28px;text-align:center}.checks-table .sub-label{padding-left:18px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f3f4f6;border-radius:4px;padding:1px 4px;white-space:pre-wrap}
em{color:#888;font-style:normal}.group-note{font-weight:500;color:#ef4444;margin-left:6px}.group-note.warn{color:#f59e0b}.group-note.muted{color:#888}.box{width:28px;text-align:center;color:#1a1d23}
.sign-off{display:grid;grid-template-columns:1fr 1fr;gap:32px;margin-top:14px}.sign-line{border-bottom:1px solid #1a1d23;height:32px;margin-bottom:4px}
@media print{body{background:#fff;margin:16px}.device-card,.summary-box{break-inside:auto;page-break-inside:auto}.device-summary{break-after:avoid}.checks-table tr{break-inside:avoid}}
"""


def render_qc_html(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    steps = report.get("steps") if isinstance(report.get("steps"), list) else []
    device = summary.get("device") or "bcMeter"
    version = summary.get("version") or ""
    created = report.get("created_at") or datetime.now().isoformat(timespec="seconds")
    hard_failures = report.get("hard_failures") or []
    warnings = sum(1 for step in steps if _step_status(step) == "warn")
    status = "PASS" if report.get("passed") else "FAIL"
    card_class = "device-card" if report.get("passed") else "device-card has-fail"
    badge_class = "badge badge-pass" if report.get("passed") else "badge badge-fail"

    return "\n".join([
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="UTF-8">',
        f"<title>bcMeter Pi QC Report - {esc(device)}</title>",
        "<style>",
        _css().strip(),
        "</style>",
        "</head>",
        "<body>",
        "<h1>bcMeter Pre-Shipment QC Report</h1>",
        f'<p class="meta">Run: {esc(report.get("report_dir"))} &nbsp;|&nbsp; Collected: {esc(created)} &nbsp;|&nbsp; Platform: Raspberry Pi</p>',
        '<div class="summary-box"><div class="summary-grid">',
        f'<div><div class="summary-num blue">1</div><div>Devices Found</div></div>',
        f'<div><div class="summary-num green">{1 if report.get("passed") else 0}</div><div>All Checks Pass</div></div>',
        f'<div><div class="summary-num red">{len(hard_failures)}</div><div>Need Attention</div></div>',
        "</div></div>",
        "<h2>Deep-Tested Devices</h2>",
        f'<div class="{card_class}">',
        '<div class="device-summary">',
        f'<span class="device-name">{esc(device)} <span class="meta">&nbsp;v{esc(version)} [pi] &nbsp;Raspberry Pi</span></span>',
        f'<span class="{badge_class}">{esc(status)}</span>',
        "</div>",
        '<div class="device-body">',
        f'<p class="meta">Warnings: {warnings} &nbsp;|&nbsp; Profile: {esc(summary.get("profile"))} &nbsp;|&nbsp; ADC: {esc(summary.get("adc_type"))}</p>',
        "<h3>Automated Checks</h3>",
        _render_checks(steps),
        "<h3>Packlist</h3>",
        _packlist(),
        _diag_table(report),
        "</div>",
        "</div>",
        '<div style="margin-top:30px;border-top:2px solid #eee;padding-top:20px;">',
        "<h2>Sign-Off</h2>",
        '<div class="sign-off">',
        "<div><strong>QC Technician</strong><div class=\"sign-line\"></div><small>Name / Signature / Date</small></div>",
        "<div><strong>Shipping Approval</strong><div class=\"sign-line\"></div><small>Name / Signature / Date</small></div>",
        "</div></div>",
        "</body>",
        "</html>",
        "",
    ])
