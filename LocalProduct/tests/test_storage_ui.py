"""Fake widgets/backend only; no native input or real storage traversal."""
from pathlib import Path
import queue
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import storage_ui
from storage_ui import StoragePanel, format_snapshot


class Value:
    def __init__(self, *_args, value="", **_kwargs):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


SNAPSHOT = {"version": 1, "task_id": "a" * 32, "state": "partial", "updated_utc": "now",
            "error_code": None, "report_path": "C:/fixture/report.json",
            "progress": {"updated_utc": "2026-11-19T06:40:00+00:00", "stale": False},
            "report": {"finished_utc": "2026-11-19T06:40:00+00:00", "counters": {
                "files": 50, "logical_bytes_raw": 10 * 1024**3, "permission_denied": 2,
                "skipped_entries": 4, "errors": 2},
                "output_limits": {"budget_truncated": True},
                "folder_aggregates": [{"path": "C:/fixture/parent", "logical_bytes_raw": 5 * 1024**3}],
                "largest_files": [{"path": "C:/fixture/file.zip", "logical_bytes": 1024**3}]}}


class StoragePanelTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.tasks = mock.Mock()
        self.tasks.status.return_value = SNAPSHOT
        self.tasks.start.return_value = {"task_id": "b" * 32, "state": "queued", "progress": None,
                                         "report": None, "updated_utc": "later"}
        self.panel = StoragePanel.__new__(StoragePanel)
        self.panel.tasks = self.tasks
        self.panel.closed = False
        self.panel._io_busy = False
        self.panel._active = False
        self.panel._timer = None
        self.panel._results = queue.Queue()
        self.panel._task_id = None
        self.panel._display_key = None
        self.panel.c_drive = Value(value=True)
        self.panel.d_drive = Value(value=False)
        self.panel.status = Value()
        self.panel.notice = Value()
        self.panel.start_button = mock.Mock()
        self.panel.output = mock.Mock()
        self.panel.after = mock.Mock(return_value="timer")
        self.panel.after_cancel = mock.Mock()
        self.threads = mock.patch.object(storage_ui.threading, "Thread").start()
        self.addCleanup(self.panel.close)

    def run_thread(self):
        call = self.threads.call_args.kwargs
        call["target"](*call.get("args", ()))

    def displayed(self):
        return "".join(call.args[1] for call in self.panel.output.insert.call_args_list)

    def test_constructor_does_not_scan_read_spawn_or_schedule(self):
        base = StoragePanel.__mro__[1]
        with mock.patch.object(base, "__init__", return_value=None), \
             mock.patch.object(storage_ui.ttk, "Frame"), \
             mock.patch.object(storage_ui.ttk, "Label"), \
             mock.patch.object(storage_ui.ttk, "Button"), \
             mock.patch.object(storage_ui.ttk, "Checkbutton"), \
             mock.patch.object(storage_ui.ttk, "Scrollbar"), \
             mock.patch.object(storage_ui.tk, "Text"), \
             mock.patch.object(storage_ui.tk, "StringVar", Value), \
             mock.patch.object(storage_ui.tk, "BooleanVar", Value), \
             mock.patch.object(StoragePanel, "columnconfigure"), \
             mock.patch.object(StoragePanel, "rowconfigure"), \
             mock.patch.object(StoragePanel, "bind"), \
             mock.patch.object(StoragePanel, "after") as after:
            panel = StoragePanel(object(), tasks=self.tasks)
            self.tasks.status.assert_not_called()
            self.tasks.start.assert_not_called()
            self.threads.assert_not_called()
            after.assert_not_called()
            panel.close()

    def test_explicit_start_only_uses_checked_roots_and_does_io_off_tk_thread(self):
        self.panel.d_drive.set(True)
        self.assertTrue(self.panel.start_audit())
        self.assertFalse(self.panel.start_audit())
        self.tasks.start.assert_not_called()
        self.panel.output.reset_mock()
        self.panel.after.reset_mock()
        before = self.panel.status.get()
        self.run_thread()
        self.tasks.start.assert_called_once_with(["C:\\", "D:\\"])
        self.panel.output.insert.assert_not_called()
        self.panel.after.assert_not_called()
        self.assertEqual(self.panel.status.get(), before)
        self.panel._poll()
        self.assertTrue(self.panel._active)
        self.assertEqual(self.panel.start_button.configure.call_args.kwargs["state"], "disabled")
        self.assertIn("обход ещё не подтверждён", self.panel.status.get())

    def test_empty_selection_does_not_make_request(self):
        self.panel.c_drive.set(False)
        self.panel.d_drive.set(False)
        self.assertFalse(self.panel.start_audit())
        self.assertIn("хотя бы один", self.panel.notice.get())
        self.threads.assert_not_called()
        self.tasks.start.assert_not_called()

    def test_polling_reads_saved_status_in_background_without_autostart(self):
        self.panel.start_polling()
        self.panel.start_polling()
        self.threads.assert_called_once()
        self.tasks.status.assert_not_called()
        self.run_thread()
        self.tasks.status.assert_called_once_with()
        self.panel._poll()
        self.assertFalse(self.panel._io_busy)
        self.assertIn("10.00 ГиБ", self.displayed())
        self.assertIn("отказов доступа: 2", self.displayed())
        self.assertIn("физически", self.displayed())
        self.assertIn("сокращён", self.displayed())
        self.assertEqual(self.panel.start_button.configure.call_args.kwargs["state"], "normal")
        self.tasks.start.assert_not_called()
        self.panel._poll()
        self.assertEqual(self.threads.call_count, 2)
        self.run_thread()
        self.tasks.status.assert_called_with()
        self.tasks.start.assert_not_called()

    def test_error_never_claims_completion_or_erases_saved_result(self):
        self.tasks.start.side_effect = RuntimeError("fixture failure")
        self.panel.start_audit()
        self.run_thread()
        self.panel._poll()
        self.assertFalse(self.panel._io_busy)
        self.assertIn("не подтверждён", self.panel.status.get())
        self.assertIn("fixture failure", self.panel.notice.get())
        self.panel.output.insert.assert_not_called()
        self.assertEqual(self.panel.start_button.configure.call_args.kwargs["state"], "normal")

    def test_stale_progress_completed_label_cannot_claim_job_completed(self):
        value = {"task_id": "c" * 32, "state": "running", "report": None,
                 "progress": {"status": "completed", "updated_utc": "old", "stale": True,
                              "counters": {"files": 999, "logical_bytes_raw": 123}}}
        self.panel._render(value)
        self.assertIn("Идёт обход метаданных", self.panel.status.get())
        self.assertIn("завершение не подтверждено", self.displayed())
        self.assertNotIn("Отчёт сохранён", self.displayed())
        before = self.displayed()
        self.panel._render(value)
        self.assertEqual(before, self.displayed())
        self.assertNotIn("%", self.displayed())

    def test_close_cancels_timer_and_does_not_kill_or_retry_workers(self):
        self.panel.start_polling()
        self.panel.close()
        self.panel.after_cancel.assert_called_once_with("timer")
        self.panel.after.reset_mock()
        self.run_thread()
        self.tasks.status.assert_not_called()
        self.panel._results.put((True, SNAPSHOT))
        self.panel._poll()
        self.assertFalse(self.panel.start_audit())
        self.panel.after.assert_not_called()
        self.panel.output.insert.assert_not_called()
        self.tasks.terminate.assert_not_called()
        self.tasks.kill.assert_not_called()

    def test_destroy_event_only_closes_own_panel(self):
        self.panel._on_destroy(SimpleNamespace(widget=object()))
        self.assertFalse(self.panel.closed)
        self.panel._on_destroy(SimpleNamespace(widget=self.panel))
        self.assertTrue(self.panel.closed)

    def test_invalid_saved_payload_does_not_stop_polling_or_claim_completion(self):
        bad = dict(SNAPSHOT, state="completed", report=dict(SNAPSHOT["report"], output_limits=[]))
        self.panel._io_busy = True
        self.panel._results.put((True, bad))
        self.panel._poll()
        self.assertIn("не подтверждён", self.panel.status.get())
        self.assertIn("не удалось отобразить", self.panel.notice.get())
        self.panel.after.assert_called_with(1500, self.panel._poll)
        self.panel.output.insert.assert_not_called()
        self.panel._results.put((True, SNAPSHOT))
        self.panel._poll()
        self.assertIn("10.00 ГиБ", self.displayed())
        self.assertEqual(self.panel.notice.get(), "")
        self.assertIn("с пропусками", self.panel.status.get())

    def test_render_lists_are_bounded_and_partial_is_explicit(self):
        report = dict(SNAPSHOT["report"], largest_files=[{"path": str(number), "logical_bytes": 1} for number in range(1000)])
        text = format_snapshot(dict(SNAPSHOT, report=report))
        self.assertIn("Крупные папки", text)
        self.assertNotIn("  999\n", text)
        self.assertIn("пропущенных каталогах", text)
        self.assertIn("ничего не удаляется", text)


if __name__ == "__main__":
    unittest.main()
