"""Read-only maintenance UI contracts with fake widgets, threads and advisors."""
import ast
import json
from pathlib import Path
import queue
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import maintenance_ui
from maintenance_ui import MaintenancePanel


REPORT = {
    'version': 1, 'report_id': 'a' * 32, 'collected_utc': '2026-11-18T23:30:00Z',
    'facts': {'drives': [{'drive': 'C:', 'free': 10 * 1024**3, 'total': 100 * 1024**3}],
              'system_service': {'available': True, 'installed': True, 'state': 'running',
                                 'verdict': 'review_required', 'stale': True, 'age_seconds': 1000,
                                 'evidence_utc': '2026-11-18T23:00:00Z', 'job': {'state': 'finished'}},
              'os': {'system': 'Windows', 'release': '25H2', 'build': 26200}, 'unavailable': []},
    'findings': [{'code': 'review', 'severity': 'warning', 'message': 'Нужна проверка результата.',
                  'action_ids': ['verify_component_store'], 'evidence': {'state': 'review_required'}}],
    'ai': {'status': 'advice_received', 'observations': ['Проверьте сохранённый результат.'],
           'action_ids': ['verify_component_store'], 'verification': 'unverified', 'note': 'Совет, не диагноз.'},
}


class Value:
    def __init__(self, *_args, **_kwargs):
        self.value = ''

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


class MaintenancePanelTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('No real processes')).start()
        mock.patch.object(subprocess, 'run', side_effect=AssertionError('No real commands')).start()
        self.advisor = mock.Mock()
        self.advisor.latest.return_value = REPORT
        self.advisor.collect.return_value = REPORT
        self.advisor.ai_report.return_value = {
            'report': REPORT, 'role_instructions': 'Review the facts; advice is unverified.',
            'execution_available': False, 'autonomous_api': False}
        self.panel = MaintenancePanel.__new__(MaintenancePanel)
        self.panel.advisor = self.advisor
        self.panel.closed = False
        self.panel._busy = False
        self.panel._timer = None
        self.panel._results = queue.Queue()
        self.panel._showing_payload = False
        self.panel._display_key = None
        self.panel.output = mock.Mock()
        self.panel.collect_button = mock.Mock()
        self.panel.status = Value()
        self.panel.notice = Value()
        self.panel.after = mock.Mock(return_value='timer')
        self.panel.after_cancel = mock.Mock()
        self.thread = mock.patch.object(maintenance_ui.threading, 'Thread').start()
        self.addCleanup(self.panel.close)

    def displayed(self):
        return ''.join(call.args[1] for call in self.panel.output.insert.call_args_list)

    def test_constructor_only_reads_latest_without_thread_or_timer(self):
        base = MaintenancePanel.__mro__[1]
        self.advisor.latest.return_value = None
        with mock.patch.object(base, '__init__', return_value=None), \
             mock.patch.object(maintenance_ui.ttk, 'Frame'), \
             mock.patch.object(maintenance_ui.ttk, 'Label'), \
             mock.patch.object(maintenance_ui.ttk, 'Button'), \
             mock.patch.object(maintenance_ui.ttk, 'Scrollbar'), \
             mock.patch.object(maintenance_ui.tk, 'Text'), \
             mock.patch.object(maintenance_ui.tk, 'StringVar', Value), \
             mock.patch.object(MaintenancePanel, 'columnconfigure'), \
             mock.patch.object(MaintenancePanel, 'rowconfigure'), \
             mock.patch.object(MaintenancePanel, 'bind'), \
             mock.patch.object(MaintenancePanel, 'after') as after:
            panel = MaintenancePanel(object(), advisor=self.advisor)
            self.assertIn('ещё не сформирован', panel.status.get())
            self.advisor.latest.assert_called_once_with()
            self.advisor.collect.assert_not_called()
            self.thread.assert_not_called()
            after.assert_not_called()
            panel.close()

    def test_explicit_collection_uses_one_worker_and_only_queue_before_gui_poll(self):
        self.assertTrue(self.panel.collect_report())
        self.assertFalse(self.panel.collect_report())
        self.thread.assert_called_once()
        self.advisor.collect.assert_not_called()
        target = self.thread.call_args.kwargs['target']
        status_before = self.panel.status.get()
        self.panel.output.reset_mock()
        self.panel.after.reset_mock()
        target()
        self.advisor.collect.assert_called_once_with()
        self.assertEqual(self.panel.status.get(), status_before)
        self.panel.output.insert.assert_not_called()
        self.panel.after.assert_not_called()
        self.panel._poll()
        self.assertFalse(self.panel._busy)
        self.assertIn('10.0 ГиБ', self.displayed())
        self.assertEqual(self.panel.collect_button.configure.call_args.kwargs['state'], 'normal')

    def test_poll_reads_latest_and_marks_stale_facts_and_ai_as_unverified(self):
        self.panel._poll()
        self.advisor.latest.assert_called_once_with()
        self.advisor.collect.assert_not_called()
        self.thread.assert_not_called()
        self.assertIn('УСТАРЕЛИ', self.displayed())
        self.assertIn('НЕ ПРОВЕРЕНЫ', self.displayed())
        observed = [call for call in self.panel.output.insert.call_args_list
                    if 'Проверьте сохранённый результат.' in call.args[1]]
        self.assertEqual(observed[0].args[2], 'unverified')
        self.panel.after.assert_called_with(3000, self.panel._poll)
        self.advisor.submit_advice.assert_not_called()

    def test_for_assistant_only_displays_json_and_poll_does_not_replace_it(self):
        self.panel.show_ai_report()
        payload = json.loads(self.displayed())
        self.assertFalse(payload['execution_available'])
        self.assertFalse(payload['autonomous_api'])
        self.panel.output.reset_mock()
        self.panel._poll()
        self.advisor.latest.assert_not_called()
        self.advisor.collect.assert_not_called()
        self.advisor.submit_advice.assert_not_called()
        self.panel.output.insert.assert_not_called()
        self.thread.assert_not_called()
        self.panel._busy = True
        self.panel._results.put((True, REPORT))
        self.panel._poll()
        self.panel.output.insert.assert_not_called()
        self.assertIn('Новый отчёт сохранён', self.panel.notice.get())

    def test_collection_error_restores_button_without_claiming_a_new_report(self):
        self.advisor.collect.side_effect = RuntimeError('fixture')
        self.panel.collect_report()
        self.thread.call_args.kwargs['target']()
        self.panel._poll()
        self.assertFalse(self.panel._busy)
        self.assertIn('Сбор не завершён', self.panel.status.get())
        self.assertIn('fixture', self.panel.notice.get())
        self.panel.output.insert.assert_not_called()

    def test_close_cancels_timer_and_late_worker_result_never_updates_widgets(self):
        self.panel.start_polling()
        self.panel.close()
        self.panel.after_cancel.assert_called_once_with('timer')
        self.panel.after.reset_mock()
        self.panel._collect_worker()
        self.advisor.collect.assert_not_called()
        self.panel._poll()
        self.panel.after.assert_not_called()
        self.assertFalse(self.panel.collect_report())
        # Simulate close arriving while a previously started collector is reading.
        self.panel.closed = False
        def finish_after_close():
            self.panel.closed = True
            return REPORT
        self.advisor.collect.side_effect = finish_after_close
        self.panel._collect_worker()
        self.assertTrue(self.panel._results.empty())
        self.panel.output.insert.assert_not_called()


class AppMaintenanceIntegrationTests(unittest.TestCase):
    def test_home_has_nested_pages_and_destroy_closes_panel_without_real_tk(self):
        source = Path(__file__).resolve().parents[1] / 'src/app.py'
        app_class = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                         if isinstance(node, ast.ClassDef) and node.name == 'App')
        methods = [node for node in app_class.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('_build_home_pages', 'destroy')]
        fake_ttk = SimpleNamespace(Notebook=mock.Mock(), Frame=mock.Mock(side_effect=[object(), object()]))
        panel_factory = mock.Mock()
        security_factory = mock.Mock()
        storage_factory = mock.Mock()
        fake_base = type('Base', (), {'destroy': mock.Mock()})
        sandbox = {'ttk': fake_ttk, 'MaintenancePanel': panel_factory,
                   'SecurityPanel': security_factory, 'StoragePanel': storage_factory, 'Base': fake_base}
        wrapper = ast.ClassDef(name='App', bases=[ast.Name(id='Base', ctx=ast.Load())],
                               keywords=[], body=methods, decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
        exec(compile(module, str(source), 'exec'), sandbox)
        app = sandbox['App']()
        app.home = object()
        app._build_home_pages()
        fake_ttk.Notebook.assert_called_once_with(app.home)
        self.assertEqual([call.kwargs['text'] for call in app.homepages.add.call_args_list],
                         ['Обзор', 'Диагностика и ИИ', 'Безопасность', 'Место на дисках'])
        panel_factory.assert_called_once_with(app.maintenancetab)
        app.maintenancepanel.start_polling.assert_called_once_with()
        security_factory.assert_called_once_with(app.homepages)
        storage_factory.assert_called_once_with(app.homepages)
        app.securitypanel.start_polling.assert_called_once_with()
        app.storagepanel.start_polling.assert_called_once_with()
        app.securitypanel.collect_report.assert_not_called()
        app.storagepanel.start_audit.assert_not_called()
        app.destroy()
        app.maintenancepanel.close.assert_called_once_with()
        app.securitypanel.close.assert_called_once_with()
        app.storagepanel.close.assert_called_once_with()
        fake_base.destroy.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
