import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Synthetic tray/keyboard checks. No native icon, input or registry access."""
from types import SimpleNamespace
from pathlib import Path
import unittest

from study_shell import ChordLatch, StudyShell, shell_identity


class FakeRoot:
    def __init__(self):
        self.timers = {}
        self.idle = []
        self.bindings = {}
        self.next_timer = 0
        self.visible = True
        self.window_state = 'normal'
        self.shown = 0
        self.focused = 0

    def bind(self, sequence, callback, add):
        self.bindings[sequence] = callback
        return sequence + '-id'

    def unbind(self, sequence, identifier):
        self.bindings.pop(sequence, None)

    def after(self, milliseconds, callback):
        self.next_timer += 1
        identifier = str(self.next_timer)
        self.timers[identifier] = callback
        return identifier

    def after_idle(self, callback):
        self.idle.append(callback)

    def after_cancel(self, identifier):
        self.timers.pop(identifier, None)

    def deiconify(self):
        self.visible = True
        self.window_state = 'normal'
        self.shown += 1

    def lift(self):
        pass

    def focus_force(self):
        self.focused += 1

    def withdraw(self):
        self.visible = False
        self.window_state = 'withdrawn'

    def state(self):
        return self.window_state

    def run_timer(self):
        identifier, callback = next(iter(self.timers.items()))
        self.timers.pop(identifier)
        callback()

    def run_idle(self):
        callbacks, self.idle = self.idle, []
        for callback in callbacks:
            callback()


class FakeTray:
    def __init__(self, on_show):
        self.on_show = on_show
        self.available = False
        self.installed = 0
        self.closed = 0

    def install(self):
        self.installed += 1
        self.available = True

    def close(self):
        self.available = False
        self.closed += 1


class FakeOwnership:
    def __init__(self, root, available=True):
        self.root = root
        self.available = available
        self.acquired = 0
        self.closed = 0

    def acquire(self):
        self.acquired += 1
        return self.available

    def close(self):
        self.closed += 1


class ChordTests(unittest.TestCase):
    def test_fires_once_for_simultaneous_s_d_i(self):
        chord = ChordLatch()
        self.assertFalse(chord.update((True, False, False)))
        self.assertFalse(chord.update((True, True, False)))
        self.assertTrue(chord.update((True, True, True)))
        self.assertFalse(chord.update((True, True, True)))

    def test_rearm_requires_all_keys_up(self):
        chord = ChordLatch()
        self.assertTrue(chord.update((True, True, True)))
        self.assertFalse(chord.update((False, True, True)))
        self.assertFalse(chord.update((True, True, True)))
        self.assertFalse(chord.update((False, False, False)))
        self.assertTrue(chord.update((True, True, True)))

    def test_lock_disarms_until_every_key_is_released(self):
        chord = ChordLatch()
        self.assertFalse(chord.update((True, True, True), enabled=False))
        self.assertFalse(chord.update((True, True, True), enabled=True))
        self.assertFalse(chord.update((False, False, False), enabled=True))
        self.assertTrue(chord.update((True, True, True), enabled=True))


class StudyShellTests(unittest.TestCase):
    def setUp(self):
        self.root = FakeRoot()
        self.keys = {0x53: 0, 0x44: 0, 0x49: 0}
        self.key_queries = []
        self.unlocked = True

        def key_state(key):
            self.key_queries.append(key)
            return self.keys[key]

        self.shell = StudyShell(self.root, SimpleNamespace(), tray_factory=FakeTray,
                                key_state=key_state, desktop_ready=lambda: self.unlocked,
                                ownership_factory=FakeOwnership)

    def tearDown(self):
        self.shell.close()

    def test_install_is_idempotent_and_only_queries_three_keys(self):
        self.shell.install()
        tray = self.shell.tray
        self.assertTrue(self.shell.install())
        self.assertIs(self.shell.tray, tray)
        self.assertEqual(tray.installed, 1)
        self.assertEqual(self.key_queries, [0x53, 0x44, 0x49])
        self.assertEqual(len(self.root.timers), 1)

    def test_low_bits_from_recent_presses_do_not_activate(self):
        self.keys = dict.fromkeys(self.keys, 1)
        self.shell.install()
        self.assertEqual(self.root.shown, 0)
        self.keys = dict.fromkeys(self.keys, 0x8000)
        self.root.run_timer()
        self.assertEqual(self.root.shown, 1)
        self.root.run_timer()
        self.assertEqual(self.root.shown, 1)

    def test_locked_desktop_is_not_queried_or_revealed(self):
        self.unlocked = False
        self.keys = dict.fromkeys(self.keys, 0x8000)
        self.shell.install()
        self.assertEqual(self.key_queries, [])
        self.unlocked = True
        self.root.run_timer()
        self.assertEqual(self.root.shown, 0)
        self.keys = dict.fromkeys(self.keys, 0)
        self.root.run_timer()
        self.keys = dict.fromkeys(self.keys, 0x8000)
        self.root.run_timer()
        self.assertEqual(self.root.shown, 1)

    def test_key_query_failure_does_not_arm_shortcut(self):
        self.shell.key_state = lambda key: (_ for _ in ()).throw(OSError('fixture failure'))
        self.shell.install()
        self.assertEqual(self.root.shown, 0)
        self.assertFalse(self.shell.chord.armed)
        self.assertEqual(len(self.root.timers), 1)

    def test_icon_click_shows_panel_on_tk_idle(self):
        self.shell.install()
        self.shell.hide()
        self.assertFalse(self.root.visible)
        self.shell.tray.on_show()
        self.assertFalse(self.root.visible)
        self.root.run_idle()
        self.assertTrue(self.root.visible)
        self.assertEqual(self.root.focused, 1)

    def test_minimize_moves_panel_to_tray(self):
        self.shell.install()
        self.root.window_state = 'iconic'
        self.root.bindings['<Unmap>'](SimpleNamespace(widget=self.root))
        self.root.run_idle()
        self.assertFalse(self.root.visible)

    def test_child_unmap_does_not_hide_root(self):
        self.shell.install()
        self.root.window_state = 'iconic'
        self.root.bindings['<Unmap>'](SimpleNamespace(widget=object()))
        self.assertEqual(self.root.idle, [])

    def test_hide_requires_available_tray(self):
        self.shell.hide()
        self.assertTrue(self.root.visible)
        self.shell.install()
        self.shell.tray.available = False
        self.shell.hide()
        self.assertTrue(self.root.visible)

    def test_lost_tray_restores_panel_once(self):
        self.shell.install()
        self.shell.hide()
        self.shell.tray.available = False
        self.root.run_timer()
        self.assertTrue(self.root.visible)
        self.assertEqual(self.root.shown, 1)
        self.root.run_timer()
        self.assertEqual(self.root.shown, 1)

    def test_close_disposes_icon_timers_bindings_and_idle_show(self):
        self.shell.install()
        tray = self.shell.tray
        ownership = self.shell.ownership
        self.shell.tray.on_show()
        self.shell.close()
        self.shell.close()
        self.root.run_idle()
        self.assertEqual(tray.closed, 1)
        self.assertEqual(ownership.closed, 1)
        self.assertEqual(self.root.timers, {})
        self.assertEqual(self.root.bindings, {})
        self.assertEqual(self.root.shown, 0)

    def test_failed_icon_install_preserves_panel_access(self):
        tray = FakeTray(lambda: None)

        def fail():
            raise OSError('fixture icon failure')

        tray.install = fail
        self.shell.tray_factory = lambda callback: tray
        with self.assertRaises(OSError):
            self.shell.install()
        self.assertIsNone(self.shell.tray)
        self.assertEqual(tray.closed, 1)
        self.assertTrue(self.root.visible)

    def test_other_owner_leaves_panel_visible_without_shortcut_or_icon(self):
        ownership = FakeOwnership(Path('synthetic-study'), available=False)
        self.shell.ownership_factory = lambda root: ownership
        self.assertFalse(self.shell.install())
        self.assertIsNone(self.shell.tray)
        self.assertIsNone(self.shell.ownership)
        self.assertEqual(ownership.closed, 1)
        self.assertEqual(self.root.timers, {})
        self.assertEqual(self.root.bindings, {})
        self.assertEqual(self.key_queries, [])
        self.assertTrue(self.root.visible)

    def test_ownership_uses_the_panels_actual_study_root(self):
        root = Path('synthetic-recipient') / 'study'
        self.shell.panel = SimpleNamespace(resume_store=SimpleNamespace(root=root))
        self.assertTrue(self.shell.install())
        self.assertEqual(self.shell.ownership.root, root)

    def test_failed_tray_releases_ownership_for_retry(self):
        ownership = FakeOwnership(Path('synthetic-study'))
        self.shell.ownership_factory = lambda root: ownership
        self.shell.tray_factory = lambda callback: (_ for _ in ()).throw(OSError('fixture construction failure'))
        with self.assertRaises(OSError):
            self.shell.install()
        self.assertEqual(ownership.closed, 1)
        self.assertIsNone(self.shell.ownership)
        self.shell.tray_factory = FakeTray
        self.assertTrue(self.shell.install())


class OwnershipIdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_scoped_to_account_and_data_root(self):
        root = Path('synthetic-one')
        first = shell_identity(root, 'COMPUTER\\Parent')
        self.assertTrue(first.startswith('Local\\ProstranstvoStudyShell-'))
        self.assertEqual(first, shell_identity(root, 'computer\\parent'))
        self.assertNotEqual(first, shell_identity(root, 'COMPUTER\\Other'))
        self.assertNotEqual(first, shell_identity(Path('synthetic-two'), 'COMPUTER\\Parent'))


if __name__ == '__main__':
    unittest.main()
