"""Synthetic requests only: no Tk, real process, workstation lock or power action."""
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import power_control
from power_control import PowerControl, PowerControlError
REAL_RESOLVER = power_control._shutdown_executable


class PowerControlTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / 'tests-work'
        base.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='power-fixture-', dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'power'
        self.now = 1000.25
        self.runner = mock.Mock(return_value=SimpleNamespace(returncode=0))
        self.controller = self.make()
        self.shutdown = self.root / 'fake-System32' / 'shutdown.exe'
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(power_control, '_shutdown_executable', return_value=self.shutdown).start()
        mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('Real processes forbidden')).start()
        mock.patch.object(subprocess, 'run', side_effect=AssertionError('Real commands forbidden')).start()

    def make(self):
        return PowerControl(self.root, clock=lambda: self.now, runner=self.runner)

    def advance(self, seconds):
        self.now += seconds

    def read(self):
        return json.loads(self.controller.path.read_text(encoding='utf-8'))

    def due(self, action='restart'):
        request = self.controller.schedule(action)
        self.advance(60)
        return request

    def ticket(self):
        request = self.controller.schedule('restart', 1)
        spawn = mock.Mock()
        result = self.controller.start_worker(request['request_id'], popen=spawn)
        self.assertEqual(result['state'], 'queued')
        return request['request_id'], self.read()['current']['worker_id'], spawn

    def test_plan_and_idle_are_inert(self):
        self.assertEqual(self.controller.status()['state'], 'idle')
        self.assertEqual(self.controller.plan('restart')['delay_seconds'], 60)
        self.assertFalse(self.controller.plan('shutdown')['force'])
        self.assertFalse(self.root.exists())
        self.runner.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows path resolution fixture')
    def test_system_directory_resolver_allows_owned_system_file_hardlinks(self):
        directory = self.root.parent / 'fake-system-directory'
        directory.mkdir()
        executable = directory / 'shutdown.exe'
        executable.write_bytes(b'inert fixture, never executable')
        os.link(executable, self.root.parent / 'component-copy')
        def get_directory(buffer, length):
            self.assertGreater(length, len(str(directory)))
            buffer.value = str(directory)
            return len(str(directory))
        kernel = SimpleNamespace(GetSystemDirectoryW=get_directory)
        with mock.patch.object(power_control.ctypes, 'WinDLL', return_value=kernel):
            self.assertEqual(REAL_RESOLVER(), executable)
        self.runner.assert_not_called()

    def test_invalid_actions_delays_and_injection_fail_before_writes(self):
        for action, delay in [('lock', 60), ('restart /f', 60), ([], 60),
                              ('restart', True), ('shutdown', 0), ('restart', -1),
                              ('restart', 1.0), ('restart', float('nan')), ('restart', 86401)]:
            with self.subTest(action=action, delay=delay), self.assertRaises(PowerControlError):
                self.controller.schedule(action, delay)
        self.assertFalse(self.root.exists())

    def test_schedule_persists_without_running_or_spawning(self):
        request = self.controller.schedule('shutdown')
        self.assertRegex(request['request_id'], r'^[0-9a-f]{32}$')
        self.assertEqual(request['state'], 'queued')
        self.assertEqual(request['deadline_utc'], self.now + 60)
        self.assertTrue(request['can_cancel'])
        self.assertEqual(self.make().status(), request)
        self.assertNotIn('worker_id', request)
        subprocess.Popen.assert_not_called()
        self.runner.assert_not_called()

    def test_single_pending_and_exact_idempotent_cancel(self):
        request = self.controller.schedule('restart')
        with self.assertRaisesRegex(PowerControlError, 'request_pending'):
            self.controller.schedule('shutdown')
        with self.assertRaisesRegex(PowerControlError, 'request_not_found'):
            self.controller.cancel('b' * 32)
        result = self.controller.cancel(request['request_id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(self.make().cancel(request['request_id']), result)
        fresh = self.controller.schedule('shutdown')
        with self.assertRaisesRegex(PowerControlError, 'request_not_found'):
            self.controller.cancel(request['request_id'])
        self.assertEqual(self.controller.status()['request_id'], fresh['request_id'])

    def test_expired_queue_never_runs_automatically(self):
        request = self.controller.schedule('restart')
        self.advance(59.9)
        self.assertEqual(self.controller.execute_due(request['request_id'])['state'], 'queued')
        self.advance(3600)
        self.assertEqual(self.make().status()['state'], 'queued')
        self.runner.assert_not_called()

    def test_exact_unforced_dispatch_once_for_each_action(self):
        for action, flag in [('restart', '/r'), ('shutdown', '/s')]:
            with self.subTest(action=action):
                request = self.due(action)
                self.assertEqual(self.controller.execute_due(request['request_id'])['state'], 'dispatched')
                args, kwargs = self.runner.call_args
                self.assertEqual(args[0], [str(self.shutdown), flag, '/t', '0'])
                self.assertFalse(kwargs['shell'])
                self.assertNotIn('/f', args[0])
                self.assertNotIn('/a', args[0])
                count = self.runner.call_count
                self.make().execute_due(request['request_id'])
                self.assertEqual(self.runner.call_count, count)
                with self.assertRaisesRegex(PowerControlError, 'request_not_cancellable'):
                    self.controller.cancel(request['request_id'])

    def test_failed_command_records_no_subprocess_content(self):
        self.runner.return_value = SimpleNamespace(returncode=5, stderr='private diagnostic')
        request = self.due()
        result = self.controller.execute_due(request['request_id'])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error_code'], 'shutdown_rejected')
        self.assertNotIn('private', self.controller.path.read_text(encoding='utf-8'))

    def test_timeout_is_uncertain_and_not_retried(self):
        self.runner.side_effect = subprocess.TimeoutExpired('fake', 15)
        request = self.due()
        self.assertEqual(self.controller.execute_due(request['request_id'])['state'], 'uncertain')
        self.make().execute_due(request['request_id'])
        self.runner.assert_called_once()

    def test_crash_after_durable_executing_barrier_never_retries(self):
        self.runner.side_effect = KeyboardInterrupt()
        request = self.due()
        with self.assertRaises(KeyboardInterrupt):
            self.controller.execute_due(request['request_id'])
        self.assertEqual(self.read()['current']['state'], 'executing')
        self.assertEqual(self.make().status()['state'], 'uncertain')
        self.make().execute_due(request['request_id'])
        self.runner.assert_called_once()

    def test_live_execution_is_visible_and_not_cancellable(self):
        request = self.due()
        def fake_run(*_args, **_kwargs):
            self.assertEqual(self.make().status()['state'], 'executing')
            with self.assertRaisesRegex(PowerControlError, 'request_not_cancellable'):
                self.make().cancel(request['request_id'])
            return SimpleNamespace(returncode=0)
        self.runner.side_effect = fake_run
        self.assertEqual(self.controller.execute_due(request['request_id'])['state'], 'dispatched')

    def test_worker_launch_is_explicit_fixed_and_single_use(self):
        request_id, ticket, spawn = self.ticket()
        args, kwargs = spawn.call_args
        self.assertEqual(args[0], [str(Path(sys.executable).resolve()), '-B',
                                 str(Path(power_control.__file__).resolve()), '--worker', request_id,
                                 '--worker-id', ticket, '--root', str(self.root)])
        self.assertFalse(kwargs['shell'])
        with self.assertRaisesRegex(PowerControlError, 'worker_not_startable'):
            self.controller.start_worker(request_id, popen=spawn)
        spawn.assert_called_once()
        self.runner.assert_not_called()

    def test_worker_spawn_failure_is_terminal(self):
        request = self.controller.schedule('restart')
        result = self.controller.start_worker(request['request_id'], popen=mock.Mock(side_effect=OSError('private')))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error_code'], 'worker_start_failed')
        self.runner.assert_not_called()

    def test_frozen_worker_uses_only_own_executable_fixed_ids_and_default_root(self):
        executable = self.root.parent / 'ProstranstvoCore.exe'
        executable.write_bytes(b'inert frozen executable fixture')
        request = self.controller.schedule('restart')
        spawn = mock.Mock()
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'executable', str(executable)), \
                mock.patch.object(power_control, '_default_root', return_value=self.root):
            result = self.controller.start_worker(request['request_id'], popen=spawn)
        self.assertEqual(result['state'], 'queued')
        ticket = self.read()['current']['worker_id']
        command = spawn.call_args.args[0]
        self.assertEqual(command, [str(executable), 'power-worker', '--request-id',
                                   request['request_id'], '--worker-id', ticket])
        self.assertFalse(spawn.call_args.kwargs['shell'])
        self.runner.assert_not_called()

    def test_frozen_custom_data_root_fails_without_spawning(self):
        request = self.controller.schedule('restart')
        spawn = mock.Mock()
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(power_control, '_default_root', return_value=self.root.parent / 'other'):
            result = self.controller.start_worker(request['request_id'], popen=spawn)
        self.assertEqual(result['state'], 'failed')
        spawn.assert_not_called()
        self.runner.assert_not_called()

    def test_countdown_worker_uses_only_fake_sleep_and_fake_runner(self):
        request_id, ticket, _spawn = self.ticket()
        result = self.controller.run_worker(request_id, ticket, sleeper=self.advance)
        self.assertEqual(result['state'], 'dispatched')
        self.runner.assert_called_once()
        with self.assertRaisesRegex(PowerControlError, 'worker_not_startable'):
            self.make().run_worker(request_id, ticket, sleeper=self.advance)

    def test_cancel_during_countdown_never_dispatches(self):
        request_id, ticket, _spawn = self.ticket()
        def cancel(_seconds):
            self.make().cancel(request_id)
        self.assertEqual(self.controller.run_worker(request_id, ticket, sleeper=cancel)['state'], 'cancelled')
        self.runner.assert_not_called()

    def test_crashed_countdown_cannot_be_revived_by_execute_due(self):
        request_id, ticket, _spawn = self.ticket()
        with self.assertRaises(KeyboardInterrupt):
            self.controller.run_worker(request_id, ticket, sleeper=mock.Mock(side_effect=KeyboardInterrupt()))
        self.advance(60)
        self.assertEqual(self.make().execute_due(request_id)['state'], 'uncertain')
        self.runner.assert_not_called()
        self.assertNotEqual(self.controller.schedule('restart')['request_id'], request_id)

    def test_expired_launch_reservation_is_uncertain(self):
        request_id, ticket, _spawn = self.ticket()
        self.advance(16)
        self.assertEqual(self.make().status()['state'], 'uncertain')
        with self.assertRaises(PowerControlError):
            self.make().run_worker(request_id, ticket, sleeper=self.advance)
        self.runner.assert_not_called()

    def test_concurrent_schedulers_share_one_pending_slot(self):
        def schedule(_index):
            try:
                return self.make().schedule('restart')['request_id']
            except PowerControlError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(schedule, range(4)))
        self.assertEqual(results.count('request_pending'), 3)
        self.runner.assert_not_called()

    def test_strict_corrupt_json_is_preserved_without_execution(self):
        self.controller.schedule('restart')
        good = self.read()
        bodies = [b'{', b'\xff', b'{"version":1,"version":1,"current":null,"audit":[]}',
                  b'{"version":1,"current":NaN,"audit":[]}']
        mutations = [lambda x:x.update(version=True), lambda x:x.update(extra=1),
                     lambda x:x.update(audit={}), lambda x:x['current'].update(state=[]),
                     lambda x:x['current'].update(action=[]),
                     lambda x:x['current'].update(deadline_utc=float('inf')),
                     lambda x:x['current'].update(worker_claimed=1),
                     lambda x:x['current'].update(error_code='unexpected_error')]
        for mutate in mutations:
            bad = copy.deepcopy(good)
            mutate(bad)
            bodies.append(json.dumps(bad).encode('utf-8'))
        for body in bodies:
            with self.subTest(body=body[:50]):
                self.controller.path.write_bytes(body)
                with self.assertRaises(PowerControlError):
                    self.make().status()
                self.assertEqual(self.controller.path.read_bytes(), body)
        self.runner.assert_not_called()

    def test_atomic_write_failure_keeps_previous_receipt(self):
        request = self.controller.schedule('restart')
        self.controller.cancel(request['request_id'])
        before = self.controller.path.read_bytes()
        with mock.patch.object(power_control.os, 'replace', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.controller.schedule('shutdown')
        self.assertEqual(self.controller.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.power-*.tmp')))
        self.runner.assert_not_called()

    def test_minimal_audit_is_bounded(self):
        for _index in range(35):
            request = self.controller.schedule('restart', 1)
            self.controller.cancel(request['request_id'])
        audit = self.read()['audit']
        self.assertEqual(len(audit), power_control.AUDIT_LIMIT)
        self.assertEqual(set(audit[0]), power_control.AUDIT_FIELDS)
        self.runner.assert_not_called()

    def test_hardlinked_state_is_rejected(self):
        self.controller.schedule('restart')
        os.link(self.controller.path, self.root.parent / 'hardlink.json')
        with self.assertRaises(ValueError):
            self.controller.status()
        self.runner.assert_not_called()


class PowerIntegrationTests(unittest.TestCase):
    """Use unconstructed App objects and mocks; never create a Tk interpreter."""
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('Real processes forbidden')).start()
        mock.patch.object(subprocess, 'run', side_effect=AssertionError('Real commands forbidden')).start()

    def test_app_wires_existing_parent_authorizer_and_closes_panel_on_destroy(self):
        app = importlib.import_module('app')
        instance = object.__new__(app.App)
        instance.powertab = object()
        authorizer = mock.Mock()
        instance.studypanel = SimpleNamespace(authorize_parent=authorizer)
        with mock.patch.object(app, 'PowerPanel') as panel, \
                mock.patch.object(app.tk.Tk, 'destroy') as destroy:
            instance._build_power_panel()
            panel.assert_called_once_with(instance.powertab, authorize_parent=authorizer)
            panel.return_value.pack.assert_called_once_with(fill='both', expand=True)
            instance.destroy()
            panel.return_value.close.assert_called_once_with()
            destroy.assert_called_once_with()

    def test_fixed_dispatch_passes_only_ids_to_mock_worker(self):
        main = importlib.import_module('local_main')
        request_id, worker_id = 'a' * 32, 'b' * 32
        for state, expected in [('dispatched', 0), ('cancelled', 0), ('uncertain', 1)]:
            controller = mock.Mock()
            controller.run_worker.return_value = {'state': state, 'request_id': request_id}
            with self.subTest(state=state), mock.patch.object(power_control, 'PowerControl', return_value=controller) as factory, \
                    mock.patch.object(sys, 'argv', ['core', 'power-worker', '--request-id', request_id, '--worker-id', worker_id]), \
                    mock.patch.object(sys, 'stdout', io.StringIO()):
                self.assertEqual(main.main(), expected)
                factory.assert_called_once_with()
                controller.run_worker.assert_called_once_with(request_id, worker_id)

    def test_power_dispatch_rejects_other_flags_paths_and_nonexact_ids(self):
        main = importlib.import_module('local_main')
        valid = ['power-worker', '--request-id', 'a' * 32, '--worker-id', 'b' * 32]
        invalid = [valid + [flag] for flag in ['--setup', '--mini', '--greet', '--activity',
                                             '--messages', '--background', '--ready-stdout', '--open', '--serve-only']]
        invalid += [valid + ['--root', 'C:/other'], valid + ['--force'],
                    ['power-worker', '--req', 'a' * 32, '--worker-id', 'b' * 32],
                    ['power-worker'], ['power-worker', '--request-id', '../unsafe', '--worker-id', 'b' * 32],
                    ['power-worker', '--request-id', 'A' * 32, '--worker-id', 'b' * 32],
                    ['status', '--request-id', 'a' * 32], ['ui', '--worker-id', 'b' * 32]]
        with mock.patch.object(power_control, 'PowerControl') as factory:
            for arguments in invalid:
                with self.subTest(arguments=arguments), mock.patch.object(sys, 'argv', ['core', *arguments]), \
                        mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main.main()
                self.assertEqual(caught.exception.code, 2)
            factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
