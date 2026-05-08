"""Pi QC report and on-device system-check endpoints."""

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from bcmeter.qc_pi import ApiQcRunner

router = APIRouter()

BASE_DIR = "/home/bcmeter" if os.path.isdir("/home/bcmeter") else "/home/pi"
LATEST_REPORT = os.path.join(BASE_DIR, "maintenance_logs", "qc-pi-latest.json")
LATEST_HTML = os.path.join(BASE_DIR, "maintenance_logs", "qc-pi-latest.html")

_job_lock = threading.Lock()
_qc_job: dict[str, Any] | None = None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _public_job_locked() -> dict[str, Any]:
    if _qc_job is None:
        return {
            "state": "idle",
            "running": False,
            "done": False,
            "steps": [],
            "message": "Idle",
        }
    return json.loads(json.dumps(_qc_job, default=str))


def _set_job(**fields: Any) -> None:
    with _job_lock:
        if _qc_job is not None:
            _qc_job.update(fields)
            _qc_job["updated_at"] = _now_iso()


def _append_event(event: dict[str, Any]) -> None:
    with _job_lock:
        if _qc_job is None:
            return
        _qc_job["updated_at"] = _now_iso()
        _qc_job["message"] = event.get("message") or _qc_job.get("message", "")
        events = _qc_job.setdefault("events", [])
        events.append({"ts": _qc_job["updated_at"], **event})
        del events[:-80]
        step = event.get("step")
        if step:
            _qc_job.setdefault("steps", []).append(step)


def _run_qc_job(job_id: str, profile: str, calibrate: bool,
                send_email: bool) -> None:
    try:
        out_dir = Path(BASE_DIR) / "maintenance_logs" / f"qc-pi-ui-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        runner = ApiQcRunner(
            profile=profile,
            out_dir=out_dir,
            calibrate=calibrate,
            send_email=send_email,
            factory_reset=False,
            progress_cb=_append_event,
        )
        report = runner.run()
        _set_job(
            state="done",
            running=False,
            done=True,
            passed=bool(report.get("passed")),
            report=report,
            report_url="/api/qc/pi/report",
            html_report_url="/api/qc/pi/report.html",
            message="System check passed" if report.get("passed") else "System check failed",
            finished_at=_now_iso(),
        )
    except Exception as exc:
        _set_job(
            state="error",
            running=False,
            done=True,
            passed=False,
            error=str(exc),
            message=f"System check error: {exc}",
            finished_at=_now_iso(),
        )


@router.get("/qc/pi/report")
async def pi_qc_report():
    if not os.path.exists(LATEST_REPORT):
        raise HTTPException(404, "no Pi QC report found")
    try:
        with open(LATEST_REPORT, "r") as f:
            return json.load(f)
    except Exception as exc:
        raise HTTPException(500, f"could not read Pi QC report: {exc}")


@router.get("/qc/pi/report.html")
async def pi_qc_report_html():
    if not os.path.exists(LATEST_HTML):
        raise HTTPException(404, "no Pi QC HTML report found")
    return FileResponse(LATEST_HTML, media_type="text/html; charset=utf-8")


@router.post("/qc/pi/start")
async def pi_qc_start(
    profile: str = Query("standard", pattern="^(standard|quick)$"),
    calibrate: bool = Query(True),
    send_email: bool = Query(True),
):
    """Start a full Pi system check in the background."""
    global _qc_job
    with _job_lock:
        if _qc_job and _qc_job.get("running"):
            return JSONResponse(_public_job_locked(), status_code=202)
        job_id = uuid.uuid4().hex[:12]
        _qc_job = {
            "id": job_id,
            "state": "running",
            "running": True,
            "done": False,
            "passed": None,
            "profile": profile,
            "calibrate": calibrate,
            "send_email": send_email,
            "factory_reset": False,
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "message": "Starting system check",
            "steps": [],
            "events": [],
            "elapsed_s": 0,
        }
        thread = threading.Thread(
            target=_run_qc_job,
            args=(job_id, profile, calibrate, send_email),
            name=f"pi-qc-{job_id}",
            daemon=True,
        )
        thread.start()
        return JSONResponse(_public_job_locked(), status_code=202)


@router.get("/qc/pi/status")
async def pi_qc_status():
    with _job_lock:
        job = _public_job_locked()
    if job.get("started_at") and job.get("running"):
        try:
            started = datetime.fromisoformat(str(job["started_at"]))
            job["elapsed_s"] = max(0, int(time.time() - started.timestamp()))
        except Exception:
            pass
    return job
