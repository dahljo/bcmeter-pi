import csv
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bcmeter import email_handler, timesync
from bcmeter.measure import MeasureEngine
from bcmeter.state import state
from bcmeter.storage import MeasureRow, Storage


class StorageTimeCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.flag_patch = mock.patch("bcmeter.storage._set_session_flag")
        self.flag_patch.start()
        self.addCleanup(self.flag_patch.stop)

        self.storage = Storage(self.tmp.name)
        self.storage.start_session()
        self.addCleanup(self._close_storage)
        generated = Path(self.storage.session_filepath)
        old_path = Path(self.tmp.name) / "17-06-26_121243.csv"
        os.replace(generated, old_path)
        generated_pending = Path(str(generated) + ".time_pending")
        if generated_pending.exists():
            os.replace(generated_pending, Path(str(old_path) + ".time_pending"))
        self.storage._session_file = str(old_path)
        self.storage._ensure_current_link()

    def _close_storage(self):
        state.consume_time_sync()
        if self.storage.session_active:
            self.storage.end_session()
        else:
            state.close_logging_session()

    def _append(self, date, time_str, notes="", timestamp_monotonic=None):
        row = MeasureRow(date=date, time_str=time_str, notes=notes)
        if timestamp_monotonic is not None:
            row._timestamp_monotonic = timestamp_monotonic
        self.storage.append_row(row)

    def test_corrects_rows_filename_and_current_link_across_midnight(self):
        old_pending = Path(self.storage.session_filepath + ".time_pending")
        self.assertTrue(old_pending.exists())
        self._append("17-06-26", "23:59:30", notes="quoted;note")
        self._append("18-06-26", "00:00:30")

        corrected = self.storage.correct_active_session_timestamps(30 * 86400)

        self.assertEqual(corrected, 2)
        self.assertEqual(self.storage.session_filename, "17-07-26_121243.csv")
        self.assertEqual(
            os.path.realpath(Path(self.tmp.name) / "log_current.csv"),
            os.path.realpath(self.storage.session_filepath),
        )
        self.assertFalse((Path(self.tmp.name) / "17-06-26_121243.csv").exists())
        self.assertFalse(old_pending.exists())
        self.assertFalse(Path(self.storage.session_filepath + ".time_pending").exists())
        with open(self.storage.session_filepath, newline="") as source:
            rows = list(csv.DictReader(source, delimiter=";"))
        self.assertEqual(
            [(row["bcmDate"], row["bcmTime"]) for row in rows],
            [("17-07-26", "23:59:30"), ("18-07-26", "00:00:30")],
        )
        self.assertEqual(rows[0]["notes"], "quoted;note")

    def test_rows_written_after_clock_step_are_not_shifted_twice(self):
        self._append("17-06-26", "12:00:00", timestamp_monotonic=100.0)
        self._append("18-07-26", "12:05:00", timestamp_monotonic=110.0)

        corrected = self.storage.correct_active_session_timestamps(
            31 * 86400,
            row_limit=1,
            old_local_cutoff=datetime(2026, 6, 17, 12, 2, 0),
            old_timeline_local=datetime(2026, 6, 17, 12, 0, 0),
            old_timeline_monotonic=100.0,
        )

        self.assertEqual(corrected, 1)
        with open(self.storage.session_filepath, newline="") as source:
            rows = list(csv.DictReader(source, delimiter=";"))
        self.assertEqual(
            [(row["bcmDate"], row["bcmTime"]) for row in rows],
            [("18-07-26", "12:00:00"), ("18-07-26", "12:05:00")],
        )

    def test_inflight_old_timeline_row_after_boundary_is_corrected(self):
        self._append("17-06-26", "12:00:00", timestamp_monotonic=100.0)
        boundary = self.storage.capture_time_sync_boundary()
        self._append("17-06-26", "12:00:01", timestamp_monotonic=101.0)
        self._append("18-07-26", "12:00:03", timestamp_monotonic=103.0)

        corrected = self.storage.correct_active_session_timestamps(
            31 * 86400,
            row_limit=boundary["row_limit"],
            old_local_cutoff=datetime(2026, 6, 17, 12, 0, 2),
            old_timeline_local=datetime(2026, 6, 17, 12, 0, 0),
            old_timeline_monotonic=100.0,
        )

        self.assertEqual(corrected, 2)
        with open(self.storage.session_filepath, newline="") as source:
            rows = list(csv.DictReader(source, delimiter=";"))
        self.assertEqual(
            [(row["bcmDate"], row["bcmTime"]) for row in rows],
            [
                ("18-07-26", "12:00:00"),
                ("18-07-26", "12:00:01"),
                ("18-07-26", "12:00:03"),
            ],
        )

    def test_multiple_sync_events_keep_delivery_blocked_until_all_apply(self):
        self._append("17-06-26", "12:00:00")
        boundary = self.storage.capture_time_sync_boundary()
        state.mark_time_synced(86400, **boundary)
        state.mark_time_synced(60, **boundary)

        self.assertEqual(self.storage.apply_pending_time_sync(), (True, 1))
        self.assertFalse(state.get("session_time_synced"))
        self.assertTrue(Path(self.storage.session_filepath + ".time_pending").exists())

        self.assertEqual(self.storage.apply_pending_time_sync(), (True, 1))
        self.assertTrue(state.get("session_time_synced"))
        self.assertFalse(Path(self.storage.session_filepath + ".time_pending").exists())
        with open(self.storage.session_filepath, newline="") as source:
            rows = list(csv.DictReader(source, delimiter=";"))
        self.assertEqual(
            (rows[0]["bcmDate"], rows[0]["bcmTime"]),
            ("18-06-26", "12:01:00"),
        )

    def test_event_after_session_end_corrects_captured_generation(self):
        self._append("17-06-26", "12:00:00", timestamp_monotonic=100.0)
        boundary = self.storage.capture_time_sync_boundary()
        self.storage.end_session()

        handled = self.storage.handle_time_sync_event(
            31 * 86400,
            source="manual",
            old_local_cutoff=datetime(2026, 6, 17, 12, 0, 1),
            old_timeline_local=datetime(2026, 6, 17, 12, 0, 0),
            old_timeline_monotonic=100.0,
            **boundary,
        )

        self.assertTrue(handled)
        self.assertEqual(self.storage.session_filename, "18-07-26_121243.csv")
        self.assertFalse(Path(self.storage.session_filepath + ".time_pending").exists())
        with open(self.storage.session_filepath, newline="") as source:
            rows = list(csv.DictReader(source, delimiter=";"))
        self.assertEqual(
            (rows[0]["bcmDate"], rows[0]["bcmTime"]),
            ("18-07-26", "12:00:00"),
        )

    def test_event_corrects_closed_generation_after_new_session_started(self):
        self._append("17-06-26", "12:00:00", timestamp_monotonic=100.0)
        old_boundary = self.storage.capture_time_sync_boundary()
        old_generation = old_boundary["session_generation"]
        self.storage.end_session()
        self.storage.start_session(time_synced=False)
        self._append("17-06-26", "12:00:01", timestamp_monotonic=101.0)
        current_generation = self.storage._session_generation

        handled = self.storage.handle_time_sync_event(
            31 * 86400,
            source="ntp",
            old_local_cutoff=datetime(2026, 6, 17, 12, 0, 2),
            old_timeline_local=datetime(2026, 6, 17, 12, 0, 0),
            old_timeline_monotonic=100.0,
            unsynced_start_monotonic=99.0,
            **old_boundary,
        )

        self.assertTrue(handled)
        self.assertEqual(self.storage.apply_pending_time_sync(), (True, 1))
        old_record = self.storage._session_records[old_generation]
        current_record = self.storage._session_records[current_generation]
        with open(old_record["path"], newline="") as source:
            old_rows = list(csv.DictReader(source, delimiter=";"))
        with open(current_record["path"], newline="") as source:
            current_rows = list(csv.DictReader(source, delimiter=";"))
        self.assertEqual(
            (old_rows[0]["bcmDate"], old_rows[0]["bcmTime"]),
            ("18-07-26", "12:00:00"),
        )
        self.assertEqual(
            (current_rows[0]["bcmDate"], current_rows[0]["bcmTime"]),
            ("18-07-26", "12:00:01"),
        )
        self.assertEqual(
            os.path.realpath(Path(self.tmp.name) / "log_current.csv"),
            os.path.realpath(current_record["path"]),
        )
        self.assertFalse(Path(old_record["path"] + ".time_pending").exists())
        self.assertFalse(Path(current_record["path"] + ".time_pending").exists())

    def test_late_initial_ntp_observation_does_not_shift_valid_session(self):
        self.storage.end_session()
        self.storage.start_session(time_synced=True)
        self._append("18-07-26", "12:00:00", timestamp_monotonic=100.0)
        before_path = self.storage.session_filepath
        before = Path(before_path).read_bytes()

        handled = self.storage.handle_time_sync_event(
            31 * 86400,
            source="ntp",
            old_local_cutoff=datetime(2026, 6, 17, 12, 0, 0),
            old_timeline_local=datetime(2026, 6, 17, 12, 0, 0),
            old_timeline_monotonic=90.0,
        )

        self.assertTrue(handled)
        self.assertEqual(self.storage.session_filepath, before_path)
        self.assertEqual(Path(before_path).read_bytes(), before)
        self.assertIsNone(state.consume_time_sync_event())

    def test_later_ntp_resync_only_corrects_unsynced_epoch(self):
        self.storage.end_session()
        self.storage.start_session(time_synced=True)
        self._append("18-07-26", "12:00:00", timestamp_monotonic=100.0)
        epoch_start = self.storage.capture_time_sync_boundary()
        self.assertTrue(self.storage.handle_time_sync_lost(**epoch_start))
        self.assertFalse(state.get("session_time_synced"))
        self.assertTrue(Path(self.storage.session_filepath + ".time_pending").exists())
        self._append("18-07-26", "12:00:05", timestamp_monotonic=110.0)
        boundary = self.storage.capture_time_sync_boundary()
        self._append("18-07-26", "13:00:15", timestamp_monotonic=120.0)
        before_name = self.storage.session_filename

        handled = self.storage.handle_time_sync_event(
            3600,
            source="ntp",
            old_local_cutoff=datetime(2026, 7, 18, 12, 0, 10),
            old_timeline_local=datetime(2026, 7, 18, 12, 0, 0),
            old_timeline_monotonic=105.0,
            row_start_exclusive=epoch_start["row_limit"],
            row_start_session_generation=epoch_start["session_generation"],
            unsynced_start_monotonic=105.0,
            **boundary,
        )
        self.assertTrue(handled)
        self.assertEqual(self.storage.apply_pending_time_sync(), (True, 1))

        self.assertEqual(self.storage.session_filename, before_name)
        with open(self.storage.session_filepath, newline="") as source:
            rows = list(csv.DictReader(source, delimiter=";"))
        self.assertEqual(
            [(row["bcmDate"], row["bcmTime"]) for row in rows],
            [
                ("18-07-26", "12:00:00"),
                ("18-07-26", "13:00:05"),
                ("18-07-26", "13:00:15"),
            ],
        )

    def test_later_resync_does_not_rename_initially_provisional_session_again(self):
        self._append("17-06-26", "12:00:00", timestamp_monotonic=100.0)
        first_boundary = self.storage.capture_time_sync_boundary()
        self.assertTrue(self.storage.handle_time_sync_event(
            86400,
            source="ntp",
            old_local_cutoff=datetime(2026, 6, 17, 12, 0, 1),
            old_timeline_local=datetime(2026, 6, 17, 12, 0, 0),
            old_timeline_monotonic=100.0,
            unsynced_start_monotonic=90.0,
            **first_boundary,
        ))
        self.assertEqual(self.storage.apply_pending_time_sync(), (True, 1))
        corrected_start_name = self.storage.session_filename

        epoch_start = self.storage.capture_time_sync_boundary()
        self.assertTrue(self.storage.handle_time_sync_lost(**epoch_start))
        self._append("18-06-26", "12:00:05", timestamp_monotonic=110.0)
        second_boundary = self.storage.capture_time_sync_boundary()
        self.assertTrue(self.storage.handle_time_sync_event(
            3600,
            source="ntp",
            old_local_cutoff=datetime(2026, 6, 18, 12, 0, 10),
            old_timeline_local=datetime(2026, 6, 18, 12, 0, 0),
            old_timeline_monotonic=105.0,
            row_start_exclusive=epoch_start["row_limit"],
            row_start_session_generation=epoch_start["session_generation"],
            unsynced_start_monotonic=105.0,
            **second_boundary,
        ))
        self.assertEqual(self.storage.apply_pending_time_sync(), (True, 1))

        self.assertEqual(self.storage.session_filename, corrected_start_name)


    def test_collision_uses_non_overwriting_suffix(self):
        self._append("17-06-26", "12:17:43")
        collision = Path(self.tmp.name) / "17-07-26_121243.csv"
        collision.write_text("do-not-overwrite\n")

        self.assertEqual(self.storage.correct_active_session_timestamps(30 * 86400), 1)

        self.assertEqual(collision.read_text(), "do-not-overwrite\n")
        self.assertEqual(
            self.storage.session_filename,
            "17-07-26_121243_timesync1.csv",
        )

        self.assertEqual(self.storage.correct_active_session_timestamps(86400), 1)
        self.assertEqual(self.storage.session_filename, "18-07-26_121243.csv")

    def test_zero_or_unknown_offset_does_not_rewrite(self):
        self._append("17-06-26", "12:17:43")
        before_path = self.storage.session_filepath
        before = Path(before_path).read_bytes()

        self.assertEqual(self.storage.correct_active_session_timestamps(0), 0)
        self.assertEqual(self.storage.correct_active_session_timestamps(None), 0)
        self.assertEqual(self.storage.session_filepath, before_path)
        self.assertEqual(Path(before_path).read_bytes(), before)

    def test_successful_zero_offset_clears_provisional_time_marker(self):
        pending = Path(self.storage.session_filepath + ".time_pending")
        self.assertTrue(pending.exists())

        state.mark_time_synced(0)
        self.assertEqual(self.storage.apply_pending_time_sync(), (True, 0))

        self.assertFalse(pending.exists())
        self.assertTrue(state.get("session_time_synced"))

    def test_header_only_session_still_gets_corrected_filename(self):
        self.assertEqual(self.storage.correct_active_session_timestamps(30 * 86400), 0)
        self.assertEqual(self.storage.session_filename, "17-07-26_121243.csv")
        self.assertEqual(
            os.path.realpath(Path(self.tmp.name) / "log_current.csv"),
            os.path.realpath(self.storage.session_filepath),
        )

    def test_start_session_rolls_back_if_current_link_cannot_be_created(self):
        self.storage.end_session()
        with mock.patch.object(
            self.storage, "_ensure_current_link", side_effect=PermissionError("denied"),
        ):
            with self.assertLogs("bcmeter.storage", level="ERROR"):
                with self.assertRaises(PermissionError):
                    self.storage.start_session()
        self.assertFalse(self.storage.session_active)
        self.assertIsNone(self.storage.session_filepath)

    def test_same_second_session_start_never_truncates_existing_log(self):
        self.storage.end_session()
        fixed_now = datetime(2026, 7, 18, 12, 0, 0)
        with mock.patch("bcmeter.storage.datetime") as clock:
            clock.now.return_value = fixed_now
            first_name = self.storage.start_session(time_synced=True)
            self._append("18-07-26", "12:00:01")
            first_path = Path(self.storage.session_filepath)
            self.storage.end_session()
            second_name = self.storage.start_session(time_synced=True)

        self.assertEqual(first_name, "18-07-26_120000.csv")
        self.assertEqual(second_name, "18-07-26_120000_session1.csv")
        self.assertIn("18-07-26;12:00:01", first_path.read_text())

    def test_retained_session_provenance_is_bounded(self):
        self.storage.end_session()
        for _ in range(12):
            self.storage.start_session(time_synced=True)
            self._append("18-07-26", "12:00:00", timestamp_monotonic=100.0)
            self.storage.end_session()

        self.assertLessEqual(len(self.storage._session_records), 8)

    def test_pending_sync_resets_delivery_offsets_after_success(self):
        self._append("17-06-26", "12:17:43")
        state.mark_time_synced(30 * 86400)
        with mock.patch("bcmeter.email_handler.reset_team_offset") as team, \
                mock.patch("bcmeter.email_handler.reset_log_mail_offset") as mail:
            self.assertEqual(self.storage.apply_pending_time_sync(), (True, 1))
        team.assert_called_once_with()
        mail.assert_called_once_with()
        self.assertTrue(state.get("session_time_synced"))

    def test_failed_sync_rewrite_is_requeued(self):
        state.mark_time_synced(3600)
        with mock.patch.object(
            self.storage, "correct_active_session_timestamps", return_value=None,
        ):
            self.assertEqual(self.storage.apply_pending_time_sync(), (True, None))
        self.assertEqual(state.consume_time_sync(), 3600)


class TimeSynchronizationTest(unittest.TestCase):
    def setUp(self):
        self.old_sampling = state.sampling
        state.sampling = False
        state.consume_time_sync()
        state.close_logging_session()
        with timesync._status_lock:
            self.old_manual_valid = timesync._manual_time_valid
            self.old_generation = timesync._manual_set_generation
            timesync._manual_time_valid = False
            timesync._manual_set_generation = 0

    def tearDown(self):
        state.consume_time_sync()
        state.close_logging_session()
        state.sampling = self.old_sampling
        with timesync._status_lock:
            timesync._manual_time_valid = self.old_manual_valid
            timesync._manual_set_generation = self.old_generation

    def test_is_valid_requires_real_systemd_sync_state(self):
        no = SimpleNamespace(returncode=0, stdout="no\n", stderr="")
        yes = SimpleNamespace(returncode=0, stdout="yes\n", stderr="")
        with mock.patch.object(timesync.subprocess, "run", side_effect=[no, no]):
            self.assertFalse(timesync.is_valid())
        with mock.patch.object(timesync.subprocess, "run", side_effect=[yes, no]):
            self.assertTrue(timesync.is_valid())

    def test_manual_sync_publishes_measured_wall_clock_offset(self):
        state.sampling = True
        state.begin_logging_session(False)
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(timesync.subprocess, "run", return_value=ok), \
                mock.patch.object(timesync, "_local_now", side_effect=[
                    datetime(2026, 6, 17, 12, 0, 0),
                    datetime(2026, 7, 18, 12, 0, 0),
                ]), mock.patch.object(
                    timesync.time, "monotonic", side_effect=[10.0, 10.2],
                ):
            self.assertTrue(timesync.set_time(2_000_000_000))
        self.assertAlmostEqual(state.consume_time_sync(), 31 * 86400 - 0.2)

    def test_manual_sync_offset_includes_local_timezone_change(self):
        state.sampling = True
        state.begin_logging_session(False)
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(timesync.subprocess, "run", return_value=ok) as run, \
                mock.patch.object(timesync, "_local_now", side_effect=[
                    datetime(2026, 6, 17, 12, 0, 0),
                    datetime(2026, 7, 18, 14, 0, 0),
                ]), mock.patch.object(
                    timesync.time, "monotonic", side_effect=[10.0, 10.0],
                ):
            self.assertTrue(timesync.set_time(2_000_000_000, "Europe/Berlin"))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(state.consume_time_sync(), 31 * 86400 + 2 * 3600)

    def test_ntp_transition_publishes_offset_against_monotonic_baseline(self):
        state.sampling = True
        state.begin_logging_session(False)
        stop = mock.Mock()
        stop.wait.side_effect = [False, False, True]
        with mock.patch.object(
            timesync, "system_clock_synchronized", side_effect=[False, False, True],
        ), mock.patch.object(
            timesync, "_local_now", side_effect=[
                datetime(2026, 6, 17, 12, 0, 0),
                datetime(2026, 6, 17, 12, 0, 1),
                datetime(2026, 7, 18, 12, 0, 2),
            ],
        ), mock.patch.object(
            timesync.time, "monotonic", side_effect=[100.0, 101.0, 102.0],
        ):
            timesync.monitor_sync(stop, poll_interval_s=0.1)
        self.assertEqual(state.consume_time_sync(), 31 * 86400)

    def test_unconsumed_clock_steps_are_queued_in_order(self):
        state.mark_time_synced(3600)
        state.mark_time_synced(30)

        self.assertEqual(state.consume_time_sync(), 3600)
        self.assertEqual(state.consume_time_sync(), 30)

    def test_late_ntp_poll_does_not_shift_already_synced_session(self):
        state.begin_logging_session(True)

        self.assertFalse(state.mark_ntp_synced_if_needed(31 * 86400))
        self.assertIsNone(state.consume_time_sync())

    def test_implausible_manual_timestamp_is_rejected(self):
        with mock.patch.object(timesync.subprocess, "run") as run, \
                self.assertLogs("bcmeter.timesync", level="ERROR"):
            self.assertFalse(timesync.set_time(1))
        run.assert_not_called()


class MeasureTimeSyncDeliveryTest(unittest.TestCase):
    def setUp(self):
        state.consume_time_sync()
        self.engine = MeasureEngine.__new__(MeasureEngine)
        self.engine._storage = mock.Mock()
        self.engine._pending_time_sync_note = False

    def tearDown(self):
        state.consume_time_sync()

    def test_successful_correction_keeps_note_pending(self):
        self.engine._storage.apply_pending_time_sync.return_value = (True, 2)
        self.assertEqual(self.engine._apply_pending_time_sync(), 2)
        self.assertTrue(self.engine._pending_time_sync_note)

    def test_unknown_offset_keeps_note_but_does_not_reset_delivery(self):
        self.engine._storage.apply_pending_time_sync.return_value = (True, 0)
        self.assertEqual(self.engine._apply_pending_time_sync(), 0)
        self.assertTrue(self.engine._pending_time_sync_note)


@unittest.skipUnless(
    hasattr(email_handler, "_reset_session_upload_identity"),
    "private delivery identity is not part of the public Pi runtime",
)
class DeliveryResetRaceTest(unittest.TestCase):
    def setUp(self):
        self.old_session_time_synced = state.get("session_time_synced")
        state.set("session_time_synced", True)
        email_handler._reset_session_upload_identity()

    def tearDown(self):
        email_handler._reset_session_upload_identity()
        state.set("session_time_synced", self.old_session_time_synced)

    def test_rewrite_during_team_log_read_invalidates_old_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            log_path = log_dir / "log_current.csv"
            header = "bcmDate;bcmTime;BCngm3_880nm\n"
            old_row = "17-06-26;12:17:00;-4888"
            new_row = "18-07-26;12:17:00;-4888"
            log_path.write_text(header + old_row + "\n", encoding="utf-8")
            email_handler._begin_session_upload_identity("17-06-26_121700.csv")
            email_handler.reset_team_offset()
            posts = []
            session_ids = []
            filenames = []
            real_open = open
            reset_during_first_read = True

            class ResetOnRead:
                def __init__(self, handle):
                    self._handle = handle

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return self._handle.__exit__(*args)

                def read(self, *args, **kwargs):
                    nonlocal reset_during_first_read
                    content = self._handle.read(*args, **kwargs)
                    if reset_during_first_read:
                        reset_during_first_read = False
                        log_path.write_text(header + new_row + "\n", encoding="utf-8")
                        email_handler.reset_team_offset()
                    return content

            def open_with_rewrite(path, *args, **kwargs):
                handle = real_open(path, *args, **kwargs)
                if (os.path.realpath(path) == os.path.realpath(log_path)
                        and args and args[0] == "r"):
                    return ResetOnRead(handle)
                return handle

            def post(_url, _key, _device, payload):
                posts.append(payload["content"])
                session_ids.append(payload["session_id"])
                filenames.append(payload["filename"])
                return True, ""

            with mock.patch.object(email_handler, "_base_dir", tmp), \
                    mock.patch.object(email_handler, "_get_config", return_value={}), \
                    mock.patch.object(email_handler, "_configured_api_key", return_value="key"), \
                    mock.patch.object(
                        email_handler, "_configured_lambda_url",
                        return_value="https://example.invalid",
                    ), mock.patch.object(
                        email_handler, "canonical_device_id", return_value="bcMeter-4191",
                    ), mock.patch.object(
                        email_handler, "_upload_geo_data", return_value={},
                    ), mock.patch.object(email_handler, "_post_json", side_effect=post), \
                    mock.patch("builtins.open", side_effect=open_with_rewrite):
                self.assertFalse(email_handler.send_team_log())
                self.assertTrue(email_handler.send_team_log())
                self.assertFalse(email_handler.send_team_log())

            self.assertEqual(len(posts), 1)
            self.assertIn(new_row, posts[0])
            self.assertEqual(session_ids, ["17-06-26_121700.csv"])
            self.assertEqual(filenames, ["18-07-26_121700.csv"])

    def test_time_sync_filename_change_preserves_canonical_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            log_path = log_dir / "log_current.csv"
            header = "bcmDate;bcmTime;BCngm3_880nm\n"
            old_row = "17-06-26;12:17:00;-4888"
            new_row = "18-07-26;12:17:00;-4888"
            log_path.write_text(header + old_row + "\n", encoding="utf-8")
            email_handler._begin_session_upload_identity("17-06-26_121700.csv")
            email_handler.reset_team_offset()
            payloads = []

            def post(_url, _key, _device, payload):
                payloads.append(payload)
                return True, ""

            with mock.patch.object(email_handler, "_base_dir", tmp), \
                    mock.patch.object(email_handler, "_get_config", return_value={}), \
                    mock.patch.object(email_handler, "_configured_api_key", return_value="key"), \
                    mock.patch.object(
                        email_handler, "_configured_lambda_url",
                        return_value="https://example.invalid",
                    ), mock.patch.object(
                        email_handler, "canonical_device_id", return_value="bcMeter-4191",
                    ), mock.patch.object(
                        email_handler, "_upload_geo_data", return_value={},
                    ), mock.patch.object(email_handler, "_post_json", side_effect=post):
                self.assertTrue(email_handler.send_team_log())
                log_path.write_text(header + new_row + "\n", encoding="utf-8")
                # TIME_SYNC resets delivery offsets, not physical-session ID.
                email_handler.reset_team_offset()
                self.assertTrue(email_handler.send_team_log())

            self.assertEqual(
                [payload["filename"] for payload in payloads],
                ["17-06-26_121700.csv", "18-07-26_121700.csv"],
            )
            self.assertEqual(
                [payload["session_id"] for payload in payloads],
                ["17-06-26_121700.csv", "17-06-26_121700.csv"],
            )

    def test_inflight_team_upload_cannot_restore_invalidated_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            log_path = log_dir / "log_current.csv"
            old_row = "17-06-26;12:17:00;-4888"
            new_row = "18-07-26;12:17:00;-4888"
            log_path.write_text(
                "bcmDate;bcmTime;BCngm3_880nm\n" + old_row + "\n",
                encoding="utf-8",
            )
            email_handler._begin_session_upload_identity("17-06-26_121700.csv")
            email_handler.reset_team_offset()
            posts = []
            session_ids = []

            def post(_url, _key, _device, payload):
                posts.append(payload["content"])
                session_ids.append(payload["session_id"])
                if len(posts) == 1:
                    log_path.write_text(
                        "bcmDate;bcmTime;BCngm3_880nm\n" + new_row + "\n",
                        encoding="utf-8",
                    )
                    email_handler.reset_team_offset()
                return True, ""

            with mock.patch.object(email_handler, "_base_dir", tmp), \
                    mock.patch.object(email_handler, "_get_config", return_value={}), \
                    mock.patch.object(email_handler, "_configured_api_key", return_value="key"), \
                    mock.patch.object(
                        email_handler, "_configured_lambda_url",
                        return_value="https://example.invalid",
                    ), mock.patch.object(
                        email_handler, "canonical_device_id", return_value="bcMeter-4191",
                    ), mock.patch.object(
                        email_handler, "_upload_geo_data", return_value={},
                    ), mock.patch.object(email_handler, "_post_json", side_effect=post):
                self.assertTrue(email_handler.send_team_log())
                self.assertTrue(email_handler.send_team_log())

            self.assertIn(old_row, posts[0])
            self.assertIn(new_row, posts[1])
            self.assertEqual(session_ids, [
                "17-06-26_121700.csv",
                "17-06-26_121700.csv",
            ])

if __name__ == "__main__":
    unittest.main()
