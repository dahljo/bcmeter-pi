"""Structured error codes and initialization steps.

Ported from ESP32 measure.h to maintain consistency across platforms.
"""

from enum import IntEnum


class ErrorCode(IntEnum):
    ERR_NONE = 0
    ERR_ADC_LOW = 1
    ERR_ADC_SATURATED = 2
    ERR_FLOW_ZERO = 3
    ERR_OVERTEMP = 4
    ERR_LED_FAILURE = 5
    ERR_ATN_LIMIT = 6
    ERR_OUTLIER_EXCESS = 7


class InitStep(IntEnum):
    INIT_IDLE = 0
    INIT_PREFLIGHT = 1
    INIT_PRIME_CH0 = 2
    INIT_PRIME_CH1 = 3
    INIT_PRIME_CH2 = 4
    INIT_SETTLING = 5
    INIT_DONE = 10


_ERROR_STRINGS = {
    ErrorCode.ERR_NONE: "OK",
    ErrorCode.ERR_ADC_LOW: "ADC signal too low — filter extremely loaded",
    ErrorCode.ERR_ADC_SATURATED: "ADC saturated — LED too bright or no filter present",
    ErrorCode.ERR_FLOW_ZERO: "No airflow detected",
    ErrorCode.ERR_OVERTEMP: "Temperature exceeds 65°C",
    ErrorCode.ERR_LED_FAILURE: "No reference signal from LED",
    ErrorCode.ERR_ATN_LIMIT: "Filter needs replacement",
    ErrorCode.ERR_OUTLIER_EXCESS: "ADC noise — excessive reads out-of-bounds",
}

_INIT_STRINGS = {
    InitStep.INIT_IDLE: "Idle",
    InitStep.INIT_PREFLIGHT: "Preflight checks",
    InitStep.INIT_PRIME_CH0: "Priming channel 0 (880nm)",
    InitStep.INIT_PRIME_CH1: "Priming channel 1 (520nm)",
    InitStep.INIT_PRIME_CH2: "Priming channel 2 (370nm)",
    InitStep.INIT_SETTLING: "Settling",
    InitStep.INIT_DONE: "Ready",
}


def error_string(code: ErrorCode) -> str:
    return _ERROR_STRINGS.get(code, f"Unknown error ({code})")


def init_step_string(step: InitStep) -> str:
    return _INIT_STRINGS.get(step, f"Unknown step ({step})")
