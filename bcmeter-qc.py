#!/usr/bin/env python3
"""Convenience entry point for on-device Raspberry Pi QC."""

from __future__ import annotations

import os
import sys


def _reexec_venv() -> None:
    if os.environ.get("BCMETER_QC_NO_REEXEC") == "1":
        return
    venv_python = "/home/bcmeter/venv/bin/python3"
    if not os.path.exists(venv_python):
        return
    if os.path.realpath(sys.executable) == os.path.realpath(venv_python):
        return
    os.environ["BCMETER_QC_NO_REEXEC"] = "1"
    os.execv(venv_python, [venv_python, *sys.argv])


_reexec_venv()

from bcmeter.qc_pi import main


if __name__ == "__main__":
    raise SystemExit(main())
