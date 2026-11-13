"""Recovery must finish in the queue worker, without GUI/profile/Windows integration."""
import ast
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock

PRODUCT = Path(__file__).resolve().parents[1]


class BusyFixture(RuntimeError):
    pass


def load_app():
    modules = {}
    engine = types.ModuleType('engine')
    engine.Executor = object
    engine.Busy = BusyFixture
    modules['engine'] = engine
    paths = types.ModuleType('paths')
    paths.APP_ROOT = Path('fixture-app')
    paths.DATA_ROOT = Path('fixture-data')
    modules['paths'] = paths
    version = types.ModuleType('version')
    version.VERSION = 'fixture'
    modules['version'] = version
    for name, panel in [('activity_ui', 'ActivityPanel'), ('browser_history_ui', 'BrowserHistoryPanel'),
                        ('family_messages_ui', 'FamilyMessagesPanel'), ('file_audit_ui', 'FileAuditPanel'),
                        ('study_runtime', 'PersistentStudyPanel'), ('study_shell', 'StudyShell'),
                        ('daily_plan_ui', 'DailyPlanPanel'),
                        ('folder_ui', 'FolderPanel'), ('rules_ui', 'RulePanel'), ('archive_ui', 'ArchivePanel')]:
        module = types.ModuleType(name)
        setattr(module, panel, object)
        modules[name] = module
    with mock.patch.dict(sys.modules, modules):
        specification = importlib.util.spec_from_file_location('beta_recovery_app_fixture', PRODUCT / 'src' / 'app.py')
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module.App


class RecoveryWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def harness(self, engine, stop=None):
        state = types.SimpleNamespace(engine=engine, stop=stop or threading.Event(), statuses=[])
        state.statusvar_safe = state.statuses.append
        return state

    def test_constructor_delegates_recovery_to_worker(self):
        tree = ast.parse((PRODUCT / 'src' / 'app.py').read_text(encoding='utf-8-sig'))
        app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'App')
        constructor = next(node for node in app.body if isinstance(node, ast.FunctionDef) and node.name == '__init__')
        calls = [node for node in ast.walk(constructor) if isinstance(node, ast.Call)]
        self.assertFalse(any(isinstance(node.func, ast.Attribute) and node.func.attr == 'recover' for node in calls))
        self.assertTrue(any(isinstance(node.func, ast.Attribute) and node.func.attr == 'Thread'
                            and any(keyword.arg == 'target' and isinstance(keyword.value, ast.Attribute) and keyword.value.attr == 'worker'
                                    for keyword in node.keywords) for node in calls))

    def test_slow_recovery_leaves_caller_free_and_queue_waiting(self):
        started = threading.Event()
        release = threading.Event()
        recovered = threading.Event()
        engine = mock.Mock()
        def recover():
            started.set()
            if not release.wait(3):
                raise RuntimeError('fixture recovery not released')
            recovered.set()
            return {'blocked': []}
        engine.recover.side_effect = recover
        engine.jobs.return_value = [{'id': 'fixture-job', 'status': 'queued'}]
        state = self.harness(engine)
        def execute(job):
            self.assertTrue(recovered.is_set())
            state.stop.set()
        engine.execute.side_effect = execute
        thread = threading.Thread(target=self.app.worker, args=(state,), daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertTrue(thread.is_alive())
            engine.jobs.assert_not_called()
            engine.execute.assert_not_called()
            # The test/caller thread remains available while recovery is blocked.
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            engine.execute.assert_called_once_with('fixture-job')
        finally:
            state.stop.set()
            release.set()
            thread.join(3)

    def test_busy_retries_without_running_queue_until_success(self):
        class FastStop:
            def __init__(self): self.stopped = False; self.waits = []
            def is_set(self): return self.stopped
            def set(self): self.stopped = True
            def wait(self, seconds): self.waits.append(seconds); return self.stopped
        stop = FastStop()
        engine = mock.Mock()
        def recover():
            engine.jobs.assert_not_called()
            engine.execute.assert_not_called()
            if engine.recover.call_count == 1:
                raise BusyFixture('fixture executor busy')
            return {'blocked': []}
        engine.recover.side_effect = recover
        engine.jobs.return_value = [{'id': 'fixture-job', 'status': 'queued'}]
        engine.execute.side_effect = lambda job: stop.set()
        state = self.harness(engine, stop)
        self.app.worker(state)
        self.assertEqual(engine.recover.call_count, 2)
        self.assertEqual(stop.waits, [1])
        engine.execute.assert_called_once_with('fixture-job')

    def test_fatal_recovery_error_leaves_queue_paused(self):
        engine = mock.Mock()
        engine.recover.side_effect = OSError('fixture private path or detail')
        state = self.harness(engine)
        self.app.worker(state)
        engine.jobs.assert_not_called()
        engine.execute.assert_not_called()
        self.assertFalse(state.stop.is_set())  # The GUI can remain open.
        self.assertIn('приостановлена', state.statuses[-1])
        self.assertIn('OSError', state.statuses[-1])
        self.assertNotIn('private path', state.statuses[-1])

    def test_already_stopped_worker_does_not_recover_or_run_jobs(self):
        engine = mock.Mock()
        state = self.harness(engine)
        state.stop.set()
        self.app.worker(state)
        engine.recover.assert_not_called()
        engine.jobs.assert_not_called()
        engine.execute.assert_not_called()

    def test_stop_during_recovery_prevents_queue_execution(self):
        engine = mock.Mock()
        state = self.harness(engine)
        def recover():
            state.stop.set()
            return {'blocked': []}
        engine.recover.side_effect = recover
        self.app.worker(state)
        engine.jobs.assert_not_called()
        engine.execute.assert_not_called()

    def test_stop_while_loading_jobs_does_not_execute_loaded_job(self):
        engine = mock.Mock()
        engine.recover.return_value = {'blocked': []}
        state = self.harness(engine)
        def jobs():
            state.stop.set()
            return [{'id': 'fixture-job', 'status': 'queued'}]
        engine.jobs.side_effect = jobs
        self.app.worker(state)
        engine.execute.assert_not_called()

    def test_stop_during_busy_wait_prevents_retry_and_execution(self):
        engine = mock.Mock()
        engine.recover.side_effect = BusyFixture('fixture executor busy')
        state = self.harness(engine)
        with mock.patch.object(state.stop, 'wait', side_effect=lambda seconds: state.stop.set() or True):
            self.app.worker(state)
        engine.recover.assert_called_once()
        engine.jobs.assert_not_called()
        engine.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
