import math
import threading
import unittest
from unittest.mock import patch

from bcmeter import email_handler
from bcmeter.errors import ErrorCode, InitStep
from bcmeter.measure import MeasureEngine
from bcmeter.state import state
from bcmeter.storage import MeasureRow


class _Config:
    def __init__(self, threshold=65.0):
        self.threshold = threshold

    def get_float(self, key, default=0.0):
        if key == "temperature_shutdown_c":
            return self.threshold
        if key == "sample_spot_diameter":
            return 0.4
        return default

    @staticmethod
    def get_int(key, default=0):
        values = {
            "warmup_seconds": 0,
            "num_channels": 1,
            "sample_time": 1,
        }
        return values.get(key, default)

    @staticmethod
    def get_bool(key, default=False):
        return default

    @staticmethod
    def get_string(key, default=""):
        return default

    @staticmethod
    def to_flat_dict():
        return {}


class _ADC:
    present = False
    high_limit = 3.8


class _Optics:
    def all_off(self):
        pass

    def set_led_duty(self, _channel, _duty):
        pass


class _Pump:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class _Storage:
    def __init__(self):
        self.session_active = False
        self.end_calls = 0

    def start_session(self, **_kwargs):
        self.session_active = True
        return "test.csv"

    @staticmethod
    def apply_pending_time_sync():
        return False, 0

    def end_session(self):
        self.end_calls += 1
        self.session_active = False


def _engine(threshold=65.0, storage=None):
    return MeasureEngine(
        cfg=_Config(threshold),
        adc=_ADC(),
        optics=_Optics(),
        pump=_Pump(),
        sensors={},
        storage=storage or _Storage(),
    )


class OvertemperatureShutdownTests(unittest.TestCase):
    def tearDown(self):
        state.update(
            sampling=False,
            error=ErrorCode.ERR_NONE,
            init_step=InitStep.INIT_IDLE,
            warmup_started_monotonic=0.0,
            warmup_end_monotonic=0.0,
            warmup_progress=0,
            last_temp=0.0,
        )

    def test_threshold_is_clamped_to_40_through_80(self):
        self.assertEqual(_engine(20.0)._temperature_shutdown_c(), 40.0)
        self.assertEqual(_engine(100.0)._temperature_shutdown_c(), 80.0)
        self.assertEqual(_engine(math.nan)._temperature_shutdown_c(), 65.0)

    def test_one_incident_mail_per_thermal_episode(self):
        engine = _engine()
        state.sampling = True
        with patch.object(
            email_handler, "send_email", return_value=True,
        ) as enqueue, patch(
            "bcmeter.measure.incident_log.add",
        ) as add_incident, patch(
            "bcmeter.incident_log.to_json", return_value="[]",
        ):
            self.assertTrue(engine._handle_overtemperature(65.0, "measurement"))
            self.assertTrue(engine._handle_overtemperature(70.0, "measurement"))
            self.assertFalse(engine._handle_overtemperature(63.1, "measurement"))
            self.assertFalse(engine._handle_overtemperature(63.0, "measurement"))
            state.sampling = True
            self.assertTrue(engine._handle_overtemperature(65.0, "preflight"))

        self.assertEqual(enqueue.call_count, 2)
        self.assertTrue(all(call.args[0] == "Status" for call in enqueue.call_args_list))
        self.assertEqual(enqueue.call_args_list[0].args[1]["phase"], "measurement")
        self.assertEqual(enqueue.call_args_list[1].args[1]["phase"], "preflight")
        self.assertEqual(add_incident.call_count, 2)
        self.assertEqual(engine._pump.shutdown_calls, 3)
        self.assertFalse(state.sampling)
        self.assertEqual(state.get("error"), ErrorCode.ERR_OVERTEMP)
        self.assertEqual(state.get("last_temp"), 65.0)

    def test_measurement_overtemp_finalizes_active_session(self):
        storage = _Storage()
        engine = _engine(storage=storage)
        state.sampling = True

        def overtemp_cycle(*_args, **_kwargs):
            engine._handle_overtemperature(66.0, "measurement")
            return MeasureRow(), [], True

        with patch.object(
            engine, "_preflight_check", return_value=ErrorCode.ERR_NONE,
        ), patch.object(
            engine, "_prime_channel",
        ), patch.object(
            engine, "_sample_cycle", side_effect=overtemp_cycle,
        ), patch(
            "bcmeter.measure.was_session_running", return_value=False,
        ), patch(
            "bcmeter.measure.timesync.is_valid", return_value=False,
        ), patch(
            "bcmeter.measure.email_handler.reset_team_offset",
        ), patch(
            "bcmeter.measure.email_handler.reset_log_mail_offset",
        ), patch(
            "bcmeter.measure.email_handler.reset_periodic_timers",
        ), patch(
            "bcmeter.measure.email_handler.set_session_start",
        ), patch(
            "bcmeter.measure.email_handler.send_session_start",
        ), patch(
            "bcmeter.measure.email_handler.send_overtemperature_incident",
            return_value=True,
        ), patch(
            "bcmeter.measure.geoloc.try_fetch",
        ):
            engine._run_session(threading.Event())

        self.assertFalse(storage.session_active)
        self.assertEqual(storage.end_calls, 1)
        self.assertFalse(state.sampling)
        self.assertEqual(state.get("error"), ErrorCode.ERR_OVERTEMP)
        self.assertEqual(engine._pump.shutdown_calls, 1)

    def test_incident_email_payload_uses_status_contract(self):
        with patch(
            "bcmeter.incident_log.to_json",
            return_value='[{"s":"error","v":"hot"}]',
        ), patch.object(
            email_handler, "send_email", return_value=True,
        ) as enqueue:
            self.assertTrue(
                email_handler.send_overtemperature_incident(
                    66.2, 65.0, "measurement",
                )
            )

        payload, data = enqueue.call_args.args
        self.assertEqual(payload, "Status")
        self.assertEqual(data["event"], "Overtemperature shutdown")
        self.assertEqual(data["error_code"], "OVERTEMP")
        self.assertEqual(data["temperature_c"], 66.2)
        self.assertEqual(data["threshold_c"], 65.0)
        self.assertEqual(data["phase"], "measurement")
        self.assertEqual(data["incidents"][0]["v"], "hot")


if __name__ == "__main__":
    unittest.main()
