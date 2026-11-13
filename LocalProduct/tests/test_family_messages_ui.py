import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from pathlib import Path
from types import SimpleNamespace
import gc
import tempfile
import threading
import time
import tkinter as tk
import unittest
import weakref
from unittest import mock

from family_messages import MessageStore
from family_messages_ui import AsyncFrame, ReplyComposer, TelegramSetup, safe_error
from family_telegram import TelegramError, TelegramRejected


class Editor:
    def __init__(self, value):
        self.value, self.state = value, 'normal'
    def get(self, *args):
        return self.value
    def configure(self, **kwargs):
        self.state = kwargs.get('state', self.state)
    def delete(self, *args):
        self.value = ''


class ComposerTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).parent / 'data' / 'tests-family-ui'
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.store = MessageStore(Path(self.temp.name) / 'messages')

    def tearDown(self):
        self.temp.cleanup()

    def composer(self):
        result = SimpleNamespace(_busy=False, text=Editor('Artificial child reply'),
                                 store=self.store, client_id='test-click', reply_to=None,
                                 save_button=mock.Mock(), info=mock.Mock(), settings=mock.Mock(), on_saved=mock.Mock())
        def run(action, success, failure):
            try:
                value = action()
            except Exception as error:
                failure(error)
            else:
                success(value)
        result.background = run
        return result

    def test_delivery_error_after_commit_is_reported_as_saved(self):
        ui = self.composer()
        ui.settings.status.side_effect = RuntimeError('cannot read private config')
        ReplyComposer.save(ui)
        self.assertEqual(self.store.status()['queued'], 1)
        ui.on_saved.assert_called_once()
        self.assertIn('сохранён', ui.info.set.call_args.args[0])
        self.assertEqual(ui.text.value, '')
        self.assertEqual(ui.text.state, 'normal')

    def test_editor_is_locked_while_committing_and_kept_on_storage_failure(self):
        ui = self.composer()
        store = mock.Mock()
        def fail(*args, **kwargs):
            self.assertEqual(ui.text.state, 'disabled')
            raise OSError('synthetic disk failure')
        store.enqueue_reply.side_effect = fail
        ui.store = store
        ReplyComposer.save(ui)
        self.assertEqual(ui.text.value, 'Artificial child reply')
        self.assertEqual(ui.text.state, 'normal')
        ui.on_saved.assert_not_called()
        self.assertIn('не сохранён', ui.info.set.call_args.args[0])

    def test_network_error_details_are_not_rendered(self):
        secret = 'https://api.telegram.org/botSYNTHETIC_SECRET/getMe'
        self.assertNotIn('SYNTHETIC_SECRET', safe_error(RuntimeError(secret)))
        self.assertIn('токен', safe_error(TelegramRejected(401)))

    def test_enabled_reply_uses_current_pinned_channel_queue(self):
        ui = self.composer()
        ui.channel_enabled = True
        ui.settings.status.return_value = {'enabled': True}
        ui.settings.queue_reply.side_effect = lambda store, text, client_id, reply_to:store.enqueue_reply(text, client_id=client_id, reply_to=reply_to)
        with mock.patch('family_agent.ensure_running') as worker:
            ReplyComposer.save(ui)
        ui.settings.queue_reply.assert_called_once_with(self.store, 'Artificial child reply', 'test-click', None)
        worker.assert_called_once_with(self.store)
        self.assertEqual(self.store.status()['queued'], 1)
        self.assertEqual(ui.text.value, '')

    def test_pause_after_channel_refresh_keeps_unsent_draft(self):
        ui = self.composer()
        ui.channel_enabled = True
        ui.settings.queue_reply.side_effect = TelegramError('channel_paused')
        ReplyComposer.save(ui)
        self.assertEqual(self.store.status()['queued'], 0)
        self.assertEqual(ui.text.value, 'Artificial child reply')
        ui.on_saved.assert_not_called()

    def test_enabled_without_full_binding_is_not_rendered_as_ready(self):
        ui = SimpleNamespace(save_button=mock.Mock(), info=mock.Mock())
        ReplyComposer._channel_state(ui, {'enabled': True, 'configured': True, 'paired': True, 'has_binding': False})
        self.assertFalse(ui.channel_enabled)
        self.assertIn('Сохранить', ui.save_button.configure.call_args.kwargs['text'])
        ReplyComposer._channel_state(ui, dict(enabled=True, configured=True, paired=True, has_binding=True))
        self.assertTrue(ui.channel_enabled)
        self.assertIn('Отправить', ui.save_button.configure.call_args.kwargs['text'])


class TelegramSetupTests(unittest.TestCase):
    def test_saved_bot_can_resume_without_reading_or_reentering_token(self):
        window = SimpleNamespace(body=SimpleNamespace(_busy=False, background=mock.Mock()),
                                 settings=SimpleNamespace(begin_pairing=mock.Mock()),
                                 note=mock.Mock(), _pairing_ready=mock.Mock(), _pairing_failure=mock.Mock())
        TelegramSetup.resume_pairing(window)
        window.body.background.assert_called_once_with(window.settings.begin_pairing, window._pairing_ready, window._pairing_failure)
        window.settings.begin_pairing.assert_not_called()

    def test_setup_scrolled_tk_flow_preserves_masked_token_and_link(self):
        root = tk.Tk()
        root.withdraw()
        settings = mock.Mock()
        settings.status.return_value = {'configured': True}
        settings.begin_pairing.return_value = {'url': 'https://t.me/SyntheticFamilyBot?start=synthetic_nonce', 'nonce': 'synthetic_nonce', 'expires_at': 2000000000}
        changed = mock.Mock()
        window = TelegramSetup(root, settings, changed)
        window.withdraw()
        try:
            deadline = time.monotonic()+3
            while window.body._busy and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(window.body._busy)
            self.assertEqual(window.token.cget('show'), '•')
            self.assertNotIn('disabled', window.resume_button.state())
            window.resume_pairing()
            while window.body._busy and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertEqual(window.link.get(), settings.begin_pairing.return_value['url'])
            settings.setup.assert_not_called()
            settings.enable.assert_not_called()
            changed.assert_called_once_with()
            self.assertNotIn('disabled', window.copy_button.state())
        finally:
            window.destroy()
            root.destroy()
            window = root = None
            gc.collect()

    def test_destroyed_frame_and_callbacks_are_not_retained_by_worker(self):
        root = tk.Tk()
        root.withdraw()
        frame = AsyncFrame(root)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def action():
            started.set()
            release.wait(3)
            finished.set()
            return {'plain': 'result'}
        reference = weakref.ref(frame)
        try:
            self.assertTrue(frame.background(action, frame.winfo_exists))
            self.assertTrue(started.wait(1))
            root.destroy()
            frame = root = None
            gc.collect()  # Tcl objects are released by the thread that owns Tk.
            self.assertIsNone(reference())
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
            if root is not None:
                root.destroy()
            frame = root = None
            gc.collect()

    def test_background_error_transfers_code_without_traceback_or_callbacks(self):
        root = tk.Tk()
        root.withdraw()
        frame = AsyncFrame(root)
        ready = threading.Event()
        def fail():
            ready.set()
            raise TelegramError('channel_paused')
        try:
            frame.background(fail, frame.winfo_exists, frame.winfo_exists)
            self.assertTrue(ready.wait(1))
            operation, result, error = frame._results.get(timeout=2)
            self.assertEqual(operation, 1)
            self.assertIsNone(result)
            self.assertEqual(error.code, 'channel_paused')
            self.assertIsNone(error.__traceback__)
        finally:
            root.destroy()
            frame = root = None
            gc.collect()


if __name__ == '__main__':
    unittest.main()
