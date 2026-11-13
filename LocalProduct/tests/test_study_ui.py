import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Exercise the panel with an isolated session and an unshown Tk root."""
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest import mock

from study_ui import StudyPanel


class Settings:
    def __init__(self):
        self.value = {"duration_minutes": 30, "delay_seconds": 10, "allowed_paths": []}

    def load(self):
        return dict(self.value)

    def save(self, **value):
        self.value = dict(value)
        return dict(self.value)


class Session:
    def __init__(self):
        self.active = False
        self.preview = False
        self.remaining_seconds = 1800
        self.pending = []
        self.events = []
        self.starts = []
        self.tick_count = 0

    def start(self, **value):
        self.starts.append(value)
        self.active = True
        self.preview = value["preview"]

    def stop(self):
        self.active = False
        self.pending.clear()

    def tick(self):
        self.tick_count += 1
        events, self.events = self.events, []
        return events


class Guard:
    def __init__(self, configured=True):
        self.pin = "123456" if configured else None

    def configured(self):
        return self.pin is not None

    def set_pin(self, pin):
        self.pin = pin

    def verify(self, pin):
        return pin == self.pin


class StudyPanelTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.settings = Settings()
        self.session = Session()
        self.guard = Guard()
        self.panel = StudyPanel(self.root, settings=self.settings, session=self.session, guard=self.guard)
        self.panel._show_indicator = lambda: None
        self.dialog = mock.patch("study_ui.simpledialog.askstring", return_value="123456").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.panel.close()
        self.root.destroy()

    def test_launch_keeps_session_off(self):
        self.root.update()
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.starts, [])
        self.assertEqual(self.session.tick_count, 0)

    def test_preview_passes_settings_and_disables_editing(self):
        self.panel.delay.set("5")
        self.panel.duration.set("45")
        self.panel.start_preview()
        self.assertEqual(self.session.starts[0], {
            "duration_minutes": 45, "delay_seconds": 5, "allowed_paths": [], "preview": True,
        })
        self.assertEqual(str(self.panel.start_button["state"]), "disabled")
        self.assertEqual(str(self.panel.stop_button["state"]), "normal")
        self.assertIn("Пробный", self.panel.status.get())

    def test_pending_window_and_close_result_are_visible(self):
        self.panel.start_lessons()
        self.session.pending.append({
            "window": SimpleNamespace(handle=1, pid=2, executable="browser.exe", title="A page"),
            "remaining_seconds": 4.2, "requested": False,
        })
        self.panel._refresh()
        row = self.panel.windows.item(self.panel.windows.get_children()[0])["values"]
        self.assertEqual(row, ["browser.exe", "A page", "Через 5 с"])
        self.session.pending[0]["requested"] = True
        self.session.events.append({"action": "requested", "application": "browser.exe"})
        self.panel._refresh()
        row = self.panel.windows.item(self.panel.windows.get_children()[0])["values"]
        self.assertEqual(row[-1], "Закрытие запрошено")
        self.assertIn("browser.exe", self.panel.notice.get())

    def test_failed_close_result_is_not_reported_as_requested(self):
        self.panel.start_lessons()
        self.session.pending.append({
            "window": SimpleNamespace(handle=1, pid=2, executable="browser.exe", title="A page"),
            "remaining_seconds": 0, "requested": True, "result": "unavailable",
        })
        self.panel._refresh()
        row = self.panel.windows.item(self.panel.windows.get_children()[0])["values"]
        self.assertEqual(row[-1], "Окно недоступно")

    def test_stop_clears_deadlines_and_reenables_controls(self):
        self.panel.start_lessons()
        self.panel.stop()
        self.assertFalse(self.session.active)
        self.assertEqual(self.panel.windows.get_children(), ())
        self.assertEqual(str(self.panel.start_button["state"]), "normal")
        self.assertEqual(str(self.panel.duration_box["state"]), "readonly")

    def test_close_cancels_periodic_callback(self):
        self.panel.start_lessons()
        token = self.panel._timer
        self.assertIn(token, self.root.tk.call("after", "info"))
        self.panel.close()
        self.assertFalse(self.session.active)
        self.assertTrue(self.panel.closed)
        self.assertNotIn(token, self.root.tk.call("after", "info"))

    def test_error_stops_the_session(self):
        self.panel.start_lessons()
        def broken_tick():
            raise OSError("fixture")
        self.session.tick = broken_tick
        self.panel.after_cancel(self.panel._timer)
        self.panel._timer = None
        self.panel._poll()
        self.assertFalse(self.session.active)
        self.assertIn("ошибки", self.panel.notice.get())

    def test_expiration_refreshes_controls(self):
        self.panel.start_lessons()
        self.session.active = False
        self.session.events.append({"action": "expired"})
        self.panel._refresh()
        self.assertEqual(self.panel.status.get(), "Выключено")
        self.assertEqual(str(self.panel.stop_button["state"]), "disabled")
        self.assertIn("истекло", self.panel.notice.get())

    def test_incorrect_parent_pin_keeps_lesson_active(self):
        self.panel.start_lessons()
        self.dialog.return_value = "999999"
        self.assertFalse(self.panel.request_stop())
        self.assertTrue(self.session.active)
        self.assertIn("Неверный PIN", self.panel.notice.get())

    def test_cancelled_parent_pin_keeps_lesson_active(self):
        self.panel.start_lessons()
        self.dialog.return_value = None
        self.assertFalse(self.panel.request_stop())
        self.assertTrue(self.session.active)

    def test_correct_parent_pin_stops_lesson(self):
        self.panel.start_lessons()
        self.assertTrue(self.panel.request_stop())
        self.assertFalse(self.session.active)
        self.assertEqual(self.dialog.call_args.kwargs["show"], "•")

    def test_preview_can_stop_without_parent_pin(self):
        self.panel.start_preview()
        self.assertTrue(self.panel.request_stop())
        self.dialog.assert_not_called()

    def test_first_active_start_sets_masked_parent_pin_twice(self):
        self.guard.pin = None
        self.dialog.side_effect = ["654321", "654321"]
        self.panel.start_lessons()
        self.assertTrue(self.session.active)
        self.assertEqual(self.guard.pin, "654321")
        self.assertEqual(self.dialog.call_count, 2)
        self.assertTrue(all(call.kwargs["show"] == "•" for call in self.dialog.call_args_list))
        self.assertIn("установлен", self.panel.guard_status.get())

    def test_cancelled_pin_setup_leaves_session_off(self):
        self.guard.pin = None
        self.dialog.return_value = None
        self.panel.start_lessons()
        self.assertFalse(self.session.active)
        self.assertIsNone(self.guard.pin)
        self.assertEqual(self.session.starts, [])

    def test_mismatched_pin_confirmation_leaves_session_off(self):
        self.guard.pin = None
        self.dialog.side_effect = ["123456", "654321"]
        self.panel.start_lessons()
        self.assertFalse(self.session.active)
        self.assertIsNone(self.guard.pin)

    def test_invalid_pin_setup_leaves_session_off(self):
        self.guard.pin = None
        self.dialog.return_value = "１２３４５６"
        self.panel.start_lessons()
        self.assertFalse(self.session.active)
        self.assertIsNone(self.guard.pin)
        self.assertEqual(self.dialog.call_count, 1)

    def test_guard_error_keeps_active_session(self):
        self.panel.start_lessons()
        self.guard.verify = mock.Mock(side_effect=ValueError("Подождите 30 секунд"))
        self.assertFalse(self.panel.request_stop())
        self.assertTrue(self.session.active)
        self.assertIn("Подождите", self.panel.notice.get())

    def test_expiry_during_pin_dialog_allows_app_close(self):
        self.panel.start_lessons()
        self.dialog.side_effect = lambda *args, **kwargs: self.session.stop()
        self.assertTrue(self.panel.request_stop())
        self.assertFalse(self.session.active)


if __name__ == "__main__":
    unittest.main()
