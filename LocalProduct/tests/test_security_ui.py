"""Security report view tests without Tk, Windows queries, processes or network."""
import copy
import importlib.util
from pathlib import Path
import queue
import unittest
from unittest import mock


PRODUCT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("isolated_security_ui", PRODUCT / "src/security_ui.py")
security_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security_ui)
SecurityPanel = security_ui.SecurityPanel


class Value:
    def __init__(self, *_):
        self.value = ""

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


def payload():
    fields = {
        "windows": {"product": "windows_11", "release": "24H2", "build": 26100, "revision": 0},
        "updates": {"latest_kb_id": "KB1234567", "latest_kb_installed_date": "2026-11-18"},
        "defender": {"antivirus_enabled": True, "realtime_enabled": False, "signatures_version": "1.2.3.4",
                     "signatures_updated_utc": "2026-11-19T00:00:00Z", "quick_scan_end_utc": None,
                     "full_scan_end_utc": None, "quick_scan_overdue": False, "full_scan_overdue": None},
        "threats": {"active_threat_count": 0},
        "firewall": {"domain_enabled": True, "private_enabled": False, "public_enabled": None},
        "encryption": {"system_volume_protection": None, "system_volume_encryption": None},
        "backup": {"legacy_windows_backup_configuration_seen": None},
        "backup_assurance": {"restore_tested": None},
        "permissions": {"agent_permissions_coverage": None, "extension_permissions_coverage": None},
    }
    evidence = {group: {name: {"value": value, "observed_utc": "2026-11-19T01:00:00Z",
                               "source": "local_synthetic_source", "unknown_reason": "access_denied" if value is None else None}
                        for name, value in entries.items()} for group, entries in fields.items()}
    return {"report": {"report_id": "a" * 32, "collected_utc": "2026-11-19T01:00:00Z", "evidence": evidence},
            "freshness": {"age_seconds": 0, "stale": False}, "execution_available": False, "network_available": False}


class SecurityUiTests(unittest.TestCase):
    def setUp(self):
        self.payload = payload()
        self.reporter = mock.Mock()
        self.reporter.latest.return_value = self.payload["report"]
        self.reporter.collect.return_value = self.payload["report"]
        self.reporter.for_ai.return_value = self.payload
        self.panel = SecurityPanel.__new__(SecurityPanel)
        self.panel.reporter = self.reporter
        self.panel.closed = False
        self.panel._busy = False
        self.panel._timer = None
        self.panel._results = queue.Queue()
        self.panel._display_key = None
        self.panel.output = mock.Mock()
        self.panel.collect_button = mock.Mock()
        self.panel.status = Value()
        self.panel.notice = Value()
        self.panel.after = mock.Mock(return_value="timer")
        self.panel.after_cancel = mock.Mock()
        patch = mock.patch.object(security_ui.threading, "Thread")
        self.thread = patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.panel.close)

    def displayed(self):
        return "".join(call.args[1] for call in self.panel.output.insert.call_args_list)

    def test_constructor_only_reads_saved_report_without_worker_timer_or_collection(self):
        self.reporter.latest.return_value = None
        base = SecurityPanel.__mro__[1]
        with mock.patch.object(base, "__init__", return_value=None), \
             mock.patch.object(security_ui.ttk, "Frame"), \
             mock.patch.object(security_ui.ttk, "Label"), \
             mock.patch.object(security_ui.ttk, "Button"), \
             mock.patch.object(security_ui.ttk, "Scrollbar"), \
             mock.patch.object(security_ui.tk, "Text"), \
             mock.patch.object(security_ui.tk, "StringVar", Value), \
             mock.patch.object(SecurityPanel, "columnconfigure"), \
             mock.patch.object(SecurityPanel, "rowconfigure"), \
             mock.patch.object(SecurityPanel, "bind"), \
             mock.patch.object(SecurityPanel, "after") as after:
            panel = SecurityPanel(object(), reporter=self.reporter)
            self.assertIn("ещё не сформирован", panel.status.get())
            self.reporter.latest.assert_called_once_with()
            self.reporter.collect.assert_not_called()
            self.reporter.for_ai.assert_not_called()
            self.thread.assert_not_called()
            after.assert_not_called()
            panel.close()

    def test_worker_has_no_widget_calls_and_one_explicit_collection(self):
        self.assertTrue(self.panel.collect_report())
        self.assertFalse(self.panel.collect_report())
        self.thread.assert_called_once()
        self.reporter.collect.assert_not_called()
        target = self.thread.call_args.kwargs["target"]
        status_before = self.panel.status.get()
        self.panel.after.reset_mock()
        self.panel.output.reset_mock()
        target()
        self.reporter.collect.assert_called_once_with()
        self.reporter.for_ai.assert_called_once_with("a" * 32)
        self.assertEqual(self.panel.status.get(), status_before)
        self.panel.output.insert.assert_not_called()
        self.panel.after.assert_not_called()
        self.panel._poll()
        self.assertFalse(self.panel._busy)
        self.assertEqual(self.panel.collect_button.configure.call_args.kwargs["state"], "normal")
        self.assertIn("сохранён локально", self.panel.notice.get())
        self.assertIn("не подтверждение передачи", self.panel.notice.get())

    def test_poll_only_reads_saved_report_and_shows_unknown_sources_times_and_scope(self):
        self.panel._poll()
        self.reporter.latest.assert_called_once_with()
        self.reporter.for_ai.assert_called_once_with("a" * 32)
        self.reporter.collect.assert_not_called()
        self.thread.assert_not_called()
        text = self.displayed()
        self.assertIn("НЕИЗВЕСТНО: нет доступа", text)
        self.assertIn("Наблюдение (UTC): 2026-11-19T01:00:00Z; источник: local_synthetic_source", text)
        self.assertIn("Защита в реальном времени: нет", text)
        self.assertIn("Число активных угроз, известных Defender: 0", text)
        self.assertIn("не доказывает отсутствие заражения", text)
        self.assertIn("не доказывает успешную копию", text)
        self.assertIn("НЕПОЛНЫЙ", self.panel.status.get())
        self.assertIn("Заключения о безопасности нет", self.panel.status.get())
        self.panel.after.assert_called_with(3000, self.panel._poll)

    def test_stale_and_unknown_age_are_clearly_stale(self):
        for freshness in ({"age_seconds": 90000, "stale": True}, {"age_seconds": None, "stale": True}):
            with self.subTest(freshness=freshness):
                candidate = copy.deepcopy(self.payload)
                candidate["freshness"] = freshness
                self.panel._display_key = None
                self.panel.output.reset_mock()
                self.panel._render(candidate)
                self.assertIn("УСТАРЕЛИ", self.displayed())
                self.assertIn("УСТАРЕЛ", self.panel.status.get())
                self.assertIn("НЕПОЛНЫЙ", self.panel.status.get())

    def test_read_and_collection_errors_hide_private_paths_and_restore_button(self):
        self.reporter.latest.side_effect = RuntimeError("C:/Users/PRIVATE_SECRET/file")
        self.panel.refresh()
        self.assertIn("состояние неизвестно", self.panel.status.get())
        self.assertNotIn("PRIVATE_SECRET", self.panel.notice.get())
        self.reporter.collect.side_effect = RuntimeError("C:/Users/PRIVATE_SECRET/key")
        self.panel.collect_report()
        self.thread.call_args.kwargs["target"]()
        self.panel._poll()
        self.assertFalse(self.panel._busy)
        self.assertIn("не завершены", self.panel.status.get())
        self.assertNotIn("PRIVATE_SECRET", self.panel.notice.get())
        self.assertEqual(self.panel.collect_button.configure.call_args.kwargs["state"], "normal")
        self.panel.output.insert.assert_not_called()

    def test_startup_thread_failure_is_static_and_does_not_claim_report(self):
        self.thread.return_value.start.side_effect = RuntimeError("PRIVATE_SECRET")
        self.assertFalse(self.panel.collect_report())
        self.assertFalse(self.panel._busy)
        self.assertIn("Сбор не начат", self.panel.status.get())
        self.assertNotIn("PRIVATE_SECRET", self.panel.notice.get())
        self.reporter.collect.assert_not_called()

    def test_close_cancels_timer_and_late_worker_does_not_render_or_kill_process(self):
        self.panel.start_polling()
        self.panel.close()
        self.panel.after_cancel.assert_called_once_with("timer")
        self.panel.after.reset_mock()
        self.panel._collect_worker()
        self.reporter.collect.assert_not_called()
        self.panel._poll()
        self.panel.after.assert_not_called()
        self.assertFalse(self.panel.collect_report())
        self.panel.closed = False
        def finish_after_close():
            self.panel.close()
            return self.payload["report"]
        self.reporter.collect.side_effect = finish_after_close
        self.panel._collect_worker()
        self.assertTrue(self.panel._results.empty())
        self.reporter.for_ai.assert_not_called()
        self.panel.output.insert.assert_not_called()
        self.assertFalse(any("kill" in call[0] or "terminate" in call[0] for call in self.reporter.mock_calls))


if __name__ == "__main__":
    unittest.main()
