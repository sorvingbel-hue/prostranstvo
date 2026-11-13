"""Recipient-path and opt-in checks with fake registry/processes; no desktop access."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
SRC = BASE / 'src'


class FakeRegistry:
    HKEY_CURRENT_USER = 'current-user'
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.writes = []

    def OpenKey(self, hive, key):
        return mock.MagicMock()

    CreateKey = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value
        self.writes.append(('set', name))

    def DeleteValue(self, key, name):
        self.values.pop(name)
        self.writes.append(('delete', name))


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = BASE / 'build' / 'test-temporary'
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'Recipient Application'
        self.app.mkdir()
        self.data = self.root / 'Recipient Data'
        self.paths = types.SimpleNamespace(APP_ROOT=self.app, DATA_ROOT=self.data)
        self.registry = FakeRegistry()

    def load(self, name):
        spec = importlib.util.spec_from_file_location('_beta_test_' + name, SRC / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, paths=self.paths, winreg=self.registry):
            spec.loader.exec_module(module)
        return module

    def test_startup_is_opt_in_and_preserves_legacy_entries(self):
        self.registry.values['ProstranstvoActivity'] = 'personal legacy command'
        self.registry.values['ProstranstvoFamilyMessages'] = 'other personal command'
        for name, key, executable in (
            ('activity_startup', 'ProstranstvoLocalBetaActivity', 'ActivityAgent.exe'),
            ('family_startup', 'ProstranstvoLocalBetaFamily', 'FamilyMessageAgent.exe'),
        ):
            module = self.load(name)
            self.assertEqual(module.TARGET, self.app / executable)
            self.assertEqual(module.NAME, key)
            self.assertFalse(module.startup_enabled())
            self.assertNotIn(key, self.registry.values)
            module.TARGET.write_bytes(b'fixture-only')
            self.assertTrue(module.set_startup_enabled(True))
            self.assertEqual(self.registry.values[key], '"' + str(module.TARGET) + '"')
            self.assertFalse(module.set_startup_enabled(False))
            self.assertNotIn(key, self.registry.values)
        self.assertEqual(self.registry.values, {
            'ProstranstvoActivity': 'personal legacy command',
            'ProstranstvoFamilyMessages': 'other personal command',
        })

    def test_startup_refuses_foreign_collision_and_missing_module(self):
        for name in ('activity_startup', 'family_startup'):
            module = self.load(name)
            with self.assertRaises(ValueError):
                module.set_startup_enabled(True)
            self.assertEqual(self.registry.writes, [])
            module.TARGET.write_bytes(b'fixture-only')
            self.registry.values[module.NAME] = 'foreign command'
            for enabled in (False, True):
                with self.assertRaises(ValueError):
                    module.set_startup_enabled(enabled)
            self.assertEqual(self.registry.values[module.NAME], 'foreign command')
        self.assertEqual(self.registry.writes, [])

    def test_activity_starts_only_recipient_sibling_after_explicit_enable(self):
        module = self.load('activity_control')
        module.TARGET.write_bytes(b'fixture-only')
        store = mock.Mock()
        store.status.side_effect = [{'running': False}, {'running': True}]
        store.set_config.return_value = {'enabled': True}
        with mock.patch.object(module.subprocess, 'Popen') as start:
            self.assertEqual(module.set_mode(store, True), {'enabled': True})
        start.assert_called_once_with(
            [str(self.app / 'ActivityAgent.exe')],
            cwd=self.app, creationflags=module.subprocess.CREATE_NO_WINDOW,
        )
        store.set_config.assert_called_once_with(enabled=True)

    def test_activity_pause_does_not_start_process(self):
        module = self.load('activity_control')
        store = mock.Mock()
        with mock.patch.object(module.subprocess, 'Popen') as start:
            module.set_mode(store, False)
        start.assert_not_called()
        store.status.assert_not_called()
        store.set_config.assert_called_once_with(enabled=False)

    def test_message_default_data_is_separate_and_lazy(self):
        module = self.load('family_messages')
        with mock.patch.dict(sys.modules, paths=self.paths):
            self.assertEqual(module.default_root(), self.data / 'family-messages')
        self.assertFalse(self.data.exists())


if __name__ == '__main__':
    unittest.main()
