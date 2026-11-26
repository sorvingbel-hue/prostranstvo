import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Opt-in desktop tests that can only close their own nonce-tagged Tk fixture.

Run directly to exercise the real backend. Ordinary unittest discovery skips
these tests unless PROSTRANSTVO_STUDY_DESKTOP_TEST=1 is explicitly supplied.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from study_core import StudySession
from study_windows import WindowsDesktop


def fixture(directory, title, save_dialog):
    import tkinter as tk
    base = Path(directory)
    root = tk.Tk()
    root.title(title)
    root.geometry('420x110+30+30')
    tk.Label(root, text='Пространство · тест режима «Уроки»', font=('Segoe UI', 12)).pack(pady=25)
    def close_requested():
        (base / 'close-received').write_text('yes', encoding='utf-8')
        if save_dialog:
            root.attributes('-disabled', True)
            dialog = tk.Toplevel(root)
            dialog.transient(root)
            dialog.title(title + ' save dialog')
            tk.Label(dialog, text='Тестовый запрос сохранения').pack(padx=15, pady=15)
        else:
            root.destroy()
    root.protocol('WM_DELETE_WINDOW', close_requested)
    def stop_when_requested():
        if (base / 'stop').exists():
            root.destroy()
        else:
            root.after(100, stop_when_requested)
    root.after(100, stop_when_requested)
    root.mainloop()


class FixtureDesktop:
    def __init__(self, desktop, process, title):
        self.desktop = desktop
        self.pid = process.pid
        self.title = title

    def list_windows(self):
        return [window for window in self.desktop.list_windows()
                if window.pid == self.pid and window.title == self.title]

    def close_window(self, window):
        # Hard gate in addition to the backend's identity checks. A failure in
        # session/window selection can never send a message to a personal app.
        if window.pid != self.pid or window.title != self.title:
            raise AssertionError('Desktop test attempted to address another app')
        return self.desktop.close_window(window)


@unittest.skipUnless(os.name == 'nt' and os.environ.get('PROSTRANSTVO_STUDY_DESKTOP_TEST') == '1',
                     'Explicit isolated desktop test required')
class DesktopIntegrationTests(unittest.TestCase):
    @contextmanager
    def app(self, save_dialog=False):
        desktop = WindowsDesktop()
        if not desktop.is_interactive_desktop():
            self.skipTest('No unlocked interactive desktop in this process context')
        test_root = Path(__file__).resolve().parent / 'tests-work'
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='study-window-', dir=test_root) as directory:
            title = 'StudyModeFixture-' + uuid.uuid4().hex
            # A Windows venv python.exe may be a redirector that creates another
            # process. Launch the real interpreter so Popen.pid owns the GUI.
            interpreter = getattr(sys, '_base_executable', None) or sys.executable
            command = [interpreter, '-B', str(Path(__file__).resolve()), '--fixture', directory, title]
            if save_dialog:
                command.append('--save-dialog')
            process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
            filtered = FixtureDesktop(desktop, process, title)
            try:
                deadline = time.monotonic() + 10
                while not filtered.list_windows() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertIsNone(process.poll(), 'Fixture exited before it was observed')
                self.assertEqual(len(filtered.list_windows()), 1)
                yield filtered, process, Path(directory)
            finally:
                (Path(directory) / 'stop').write_text('yes', encoding='utf-8')
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Disposal is limited to the exact child created above.
                    process.terminate()
                    process.wait(timeout=5)

    def test_real_window_closes_after_five_seconds(self):
        with self.app() as (desktop, process, directory):
            session = StudySession(desktop)
            session.start(1, 5, False, [])
            started = time.monotonic()
            self.assertEqual(session.tick(), [])
            events = []
            deadline = started + 10
            while process.poll() is None and time.monotonic() < deadline:
                events.extend(session.tick())
                time.sleep(.1)
            self.assertTrue((directory / 'close-received').exists())
            self.assertGreaterEqual(time.monotonic() - started, 5)
            self.assertIn('requested', [event['action'] for event in events])
            self.assertIsNotNone(process.poll())

    def test_preview_and_cancel_leave_fixture_open(self):
        with self.app() as (desktop, process, directory):
            clock = [0.0]
            session = StudySession(desktop, clock=lambda: clock[0])
            session.start(1, 5, True, [])
            session.tick()
            for second in range(1, 5):
                clock[0] = second
                session.tick()
            clock[0] = 5
            self.assertEqual([event['action'] for event in session.tick()], ['preview'])
            self.assertFalse((directory / 'close-received').exists())
            self.assertIsNone(process.poll())
            session.start(1, 5, False, [])
            session.tick()
            session.stop()
            clock[0] = 15
            self.assertEqual(session.tick(), [])
            self.assertFalse((directory / 'close-received').exists())
            self.assertIsNone(process.poll())

    def test_owned_save_dialog_survives_close_request(self):
        with self.app(save_dialog=True) as (desktop, process, directory):
            clock = [0.0]
            session = StudySession(desktop, clock=lambda: clock[0])
            session.start(1, 5, False, [])
            session.tick()
            for second in range(1, 5):
                clock[0] = second
                session.tick()
            clock[0] = 5
            self.assertEqual([event['action'] for event in session.tick()], ['requested'])
            deadline = time.monotonic() + 5
            while not (directory / 'close-received').exists() and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertTrue((directory / 'close-received').exists())
            time.sleep(.2)
            self.assertIsNone(process.poll())
            self.assertEqual(desktop.list_windows(), [])
            clock[0] = 15
            self.assertEqual(session.tick(), [])
            self.assertIsNone(process.poll())


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--fixture':
        fixture(sys.argv[2], sys.argv[3], '--save-dialog' in sys.argv)
    else:
        unittest.main()
