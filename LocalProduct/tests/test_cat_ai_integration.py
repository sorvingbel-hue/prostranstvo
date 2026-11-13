import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Own withdrawn chat window and fake API only. No child data or provider calls."""
import gc
import threading
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

from kitten_messenger import KittenMessenger


class MemoryLog:
    def __init__(self): self.rows = []
    def append_many(self, rows):
        self.rows.extend(dict(row) for row in rows)
        return [{'inserted': True} for row in rows]


class AiChatIntegrationTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.settings = mock.Mock()
        self.settings.snapshot.return_value = {
            'enabled': False, 'ready': False, 'state': 'disabled', 'mode': 'offline',
            'key_configured': False, 'model': 'gpt-6-luna',
            'daily_request_limit': 50, 'max_output_tokens': 400,
            'parental_consent': False, 'zdr_attested': False, 'approval_reference': ''}
        self.settings.set_enabled.side_effect = self.save_mode
        self.gateway = mock.Mock()
        self.gateway.status.side_effect = lambda: dict(self.settings.snapshot())
        self.log = MemoryLog()
        self.controller = mock.Mock()
        self.window = KittenMessenger(
            controller=self.controller, start_background=False, chat_log=self.log,
            family_context=SimpleNamespace(family_reply=lambda text: None),
            study_monitor=SimpleNamespace(poll=lambda: {'status': {'active': False}, 'events': []}),
            transcriber=SimpleNamespace(status=lambda: {'available': False}),
            ai_settings=self.settings, ai_gateway=self.gateway)
        self.window.withdraw()
        self.pump(lambda: not self.window._busy_ai_status)

    def tearDown(self):
        self.window._busy_dialogue = False
        self.window.close()
        self.window = None
        gc.collect()

    def pump(self, condition):
        until = time.monotonic() + 4
        while not condition() and time.monotonic() < until:
            self.window.update()
            time.sleep(.01)
        self.assertTrue(condition())

    def ask(self, text):
        self.window.fill_phrase(text)
        self.window.talk_to_cat()
        self.pump(lambda: not self.window._busy_dialogue)
        return self.window._last_cat_text

    def save_mode(self, enabled):
        snapshot = self.settings.snapshot.return_value
        state = 'disabled'
        if enabled:
            if not snapshot['key_configured']:
                state = 'key_missing'
            elif not all(snapshot[key] for key in ('parental_consent', 'zdr_attested', 'approval_reference')):
                state = 'privacy_required'
            else:
                state = 'ready'
        snapshot.update(enabled=enabled, mode='api' if enabled else 'offline',
                        ready=state == 'ready', state=state)
        return dict(snapshot)

    def select(self, button):
        button.invoke()
        self.pump(lambda: self.window._pending_ai_mode is None)

    def test_offline_onboarding_in_real_chat_and_no_api_or_parent_send(self):
        self.assertIn('компьютерный', self.ask('Давай познакомимся'))
        self.ask('Меня зовут Тестик')
        self.assertIn('Тестик', self.ask('Как меня зовут?'))
        self.assertIn('Roblox', self.ask('Мне нравится Roblox'))
        self.gateway.respond.assert_not_called()
        self.controller.send.assert_not_called()
        self.assertEqual(len(self.log.rows), 8)
        self.assertIn('Офлайн', self.window.ai_status.get())
        self.assertEqual(self.window.offline_mode_button.cget('relief'), 'sunken')

    def test_fake_api_success_is_labeled_and_switch_off_prevents_next_call(self):
        self.settings.snapshot.return_value.update(key_configured=True, parental_consent=True,
            zdr_attested=True, approval_reference='Синтетическая настройка')
        self.select(self.window.ai_mode_button)
        self.settings.set_enabled.assert_called_once_with(True)
        self.gateway.respond.assert_not_called()
        self.assertIn('ИИ выбран', self.window.ai_status.get())
        self.gateway.respond.return_value = {
            'status': 'completed', 'text': 'Свет рассеивается в воздухе, поэтому небо кажется голубым.'}
        self.assertIn('внешнего ИИ', self.ask('Почему небо голубое?'))
        self.gateway.respond.assert_called_once()
        self.assertIn('внешний ИИ', self.window.transcript.get('1.0', 'end'))
        self.select(self.window.offline_mode_button)
        self.settings.set_enabled.assert_called_with(False)
        self.ask('Как растут деревья?')
        self.assertEqual(self.gateway.respond.call_count, 1)
        self.assertTrue(self.settings.snapshot.return_value['key_configured'])
        self.controller.send.assert_not_called()

    def test_ai_selection_without_key_displays_waiting_and_sends_nothing(self):
        self.select(self.window.ai_mode_button)
        self.assertEqual(self.window.ai_mode_button.cget('relief'), 'sunken')
        self.assertIn('Ответы ждут API-ключ', self.window.ai_status.get())
        self.assertFalse(self.settings.snapshot.return_value['ready'])
        self.gateway.respond.assert_not_called()
        self.controller.send.assert_not_called()

    def test_pending_mode_save_preserves_draft_and_blocks_both_send_paths(self):
        started, release = threading.Event(), threading.Event()

        def save_when_released(enabled):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test timeout')
            return self.save_mode(enabled)

        self.settings.set_enabled.side_effect = save_when_released
        self.window.ai_mode_button.invoke()
        try:
            self.assertTrue(started.wait(2))
            self.window.fill_phrase('Как растут деревья?')
            self.window.talk_to_cat()
            self.window.send_draft()
            self.assertEqual(self.window.cat_button.cget('state'), 'disabled')
            self.assertEqual(self.window.draft.get('1.0', 'end-1c'), 'Как растут деревья?')
            self.assertEqual(self.log.rows, [])
            self.gateway.respond.assert_not_called()
            self.controller.send.assert_not_called()
        finally:
            release.set()
            self.pump(lambda: self.window._pending_ai_mode is None)
        self.assertEqual(self.window.cat_button.cget('state'), 'normal')
        self.assertIn('ИИ выбран', self.window.ai_status.get())

    def test_greeting_is_visible_once_and_does_not_send_or_invent_a_turn(self):
        with mock.patch.object(self.window, 'open_chat') as open_chat:
            self.window.show_intro()
            self.window.show_intro()
        self.assertEqual(open_chat.call_count, 2)
        self.assertEqual(self.window.transcript.get('1.0', 'end').count('Мурчик · приветствие'), 1)
        self.assertEqual(self.log.rows, [])
        self.gateway.respond.assert_not_called()
        self.controller.send.assert_not_called()

    def test_settings_window_is_singleton_and_never_sends_test_request(self):
        self.window.open_ai()
        first = self.window._ai_window
        first.withdraw()
        self.window.open_ai()
        self.assertIs(first, self.window._ai_window)
        first.close()
        self.window.update()
        self.pump(lambda: not self.window._busy_ai_status)
        self.assertIsNone(self.window._ai_window)
        self.gateway.respond.assert_not_called()

    def test_settings_save_cannot_outlive_and_overwrite_later_mode_selection(self):
        started, release = threading.Event(), threading.Event()

        def save_when_released(**values):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test timeout')
            self.settings.snapshot.return_value.update(values)
            self.save_mode(values['enabled'])

        self.settings.configure.side_effect = save_when_released
        self.window.open_ai()
        form = self.window._ai_window
        form.withdraw()
        self.pump(lambda: not form._busy)
        form.enabled_var.set(True)
        form.save()
        try:
            self.assertTrue(started.wait(2))
            form.close()
            self.assertIs(self.window._ai_window, form)
            self.window.close()
            self.assertFalse(self.window._closed)
            self.window.offline_mode_button.invoke()
            self.settings.set_enabled.assert_not_called()

            self.window.fill_phrase('Искусственная реплика')
            self.window.talk_to_cat()
            self.assertFalse(self.window._busy_dialogue)
            self.gateway.respond.assert_not_called()

            self.controller.send.return_value = {'status': 'queued'}
            self.window.send_draft()
            self.pump(lambda: not self.window._busy_send)
            self.controller.send.assert_called_once()
        finally:
            release.set()
            self.pump(lambda: self.window._ai_window is None and not self.window._busy_ai_status)

        self.assertTrue(self.settings.snapshot.return_value['enabled'])
        self.select(self.window.offline_mode_button)
        self.settings.set_enabled.assert_called_once_with(False)
        self.assertFalse(self.settings.snapshot.return_value['enabled'])
        self.assertIn('Офлайн выбран', self.window.ai_status.get())

    def test_all_footer_controls_fit_minimum_chat_width(self):
        self.window.deiconify()
        self.window.geometry('440x570+0+0')
        self.window.update()
        for button in (self.window.tricks_button, self.window.ai_button, self.window.refresh_button,
                       self.window.ai_mode_button, self.window.offline_mode_button):
            self.assertGreater(button.winfo_width(), 5)
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_x() + button.winfo_width(), button.master.winfo_width())
        self.window.withdraw()


if __name__ == '__main__': unittest.main()
