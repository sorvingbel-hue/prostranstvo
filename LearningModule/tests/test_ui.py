"""Client semantics without opening a window or touching real learning data."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
from types import SimpleNamespace

from learning_module.ui import COPY, LearningWindow, RequestController, pick_text, session_summary, visual_rows


class FakeService:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def handle(self, request):
        self.requests.append(dict(request))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ClientContractTests(unittest.TestCase):
    def test_uncertain_retry_reuses_request_id_not_a_second_attempt(self):
        service = FakeService([OSError('private disk path'), {'status': 'completed', 'result': {'text_ru': 'Готово'}}])
        client = RequestController(service, id_factory=lambda: 'same-command')
        first = client.call('turn.submit', {'session_id': 's1', 'action': 'answer', 'text': '3/4'}, 4)
        self.assertEqual(first['status'], 'unavailable')
        second = client.call('turn.submit', {'session_id': 's1', 'action': 'answer', 'text': '3/4'}, 4)
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(service.requests[0], service.requests[1])
        self.assertEqual(service.requests[0]['expected_revision'], 4)
        self.assertIsNone(client.pending)
        self.assertNotIn('private disk path', str(first))

    def test_different_mutation_is_blocked_while_result_is_unknown(self):
        service = FakeService([RuntimeError('secret'), {'status': 'completed', 'result': {}}])
        client = RequestController(service)
        client.call('turn.submit', {'session_id': 's1', 'action': 'hint'}, 0)
        response = client.call('session.stop', {'session_id': 's1'}, 0)
        self.assertEqual(response['ui_error'], 'uncertain')
        self.assertEqual(len(service.requests), 1)
        snapshot = client.call('session.snapshot', {'session_id': 's1'})
        self.assertEqual(snapshot['status'], 'completed')
        self.assertIsNotNone(client.pending)

    def test_known_conflict_allows_new_command_after_explicit_resolution(self):
        ids = iter(['a', 'b'])
        service = FakeService([{'status': 'conflict', 'result': {}}, {'status': 'completed', 'result': {}}])
        client = RequestController(service, id_factory=lambda: next(ids))
        client.call('turn.submit', {'session_id': 's1', 'action': 'hint'}, 1)
        client.call('turn.submit', {'session_id': 's1', 'action': 'hint'}, 2)
        self.assertEqual([r['request_id'] for r in service.requests], ['a', 'b'])

    def test_malformed_response_does_not_claim_completion(self):
        service = FakeService([{'status': 'completed', 'result': 'not an object'}])
        client = RequestController(service)
        response = client.call('session.start', {'task_id': 't1'})
        self.assertEqual(response['status'], 'unavailable')
        self.assertIsNotNone(client.pending)

    def test_read_failures_have_no_arbitrary_exception_details(self):
        client = RequestController(FakeService([ValueError('token=private')]))
        response = client.call('history.list')
        self.assertEqual(response['ui_error'], 'error')
        self.assertNotIn('private', str(response))
        self.assertIsNone(client.pending)


class DisplayBoundaryTests(unittest.TestCase):
    def test_fraction_visual_checks_consistency_and_bounds(self):
        self.assertEqual(visual_rows({'denominator': 6, 'left_parts': 3, 'right_parts': 2, 'total_parts': 5}), [('left_bar', 3, 6), ('right_bar', 2, 6), ('sum_bar', 5, 6)])
        for invalid in (None, {}, {'denominator': True, 'left_parts': 1, 'right_parts': 1, 'total_parts': 2}, {'denominator': 25, 'left_parts': 1, 'right_parts': 1, 'total_parts': 2}, {'denominator': 4, 'left_parts': 1, 'right_parts': 2, 'total_parts': 9}, {'denominator': 4, 'left_parts': 1, 'right_parts': 2, 'total_parts': 4}):
            self.assertEqual(visual_rows(invalid), [])

    def test_translation_is_bounded_and_does_not_stringify_payload(self):
        self.assertEqual(pick_text({'text_en': 'English', 'text_ru': 'Русский'}, 'ru'), 'Русский')
        self.assertEqual(pick_text({'text_ru': {'secret': 'not text'}}, 'ru', 'fallback'), 'fallback')
        self.assertEqual(len(pick_text({'text_ru': 'a' * 13000})), 12000)

    def test_summary_is_observation_not_mastery_claim(self):
        text = session_summary({'state': 'active', 'attempt_count': 2, 'hint_level': 1, 'independent_correct': True}, 'ru')
        self.assertIn('Попыток: 2', text)
        self.assertIn('Подсказок: 1', text)
        self.assertNotIn('освоен', text)
        self.assertNotIn('master', text.lower())

    def test_both_languages_have_same_ui_keys(self):
        self.assertEqual(set(COPY['ru']), set(COPY['en']))


class WithdrawnWindowTests(unittest.TestCase):
    def test_real_local_workflow_without_showing_window(self):
        from learning_module.service import LearningService
        with TemporaryDirectory(prefix='learning-ui-test-') as root:
            service = LearningService(root)
            try:
                window = LearningWindow(service)
            except tk.TclError as error:
                service.close()
                self.skipTest('Tk display unavailable: ' + type(error).__name__)
            window.withdraw()
            try:
                window.update_idletasks()
                window.start_preset()
                self.assertEqual(window.session['state'], 'active')
                before = window.session['revision']
                window.answer_var.set('3/4')
                window.submit_answer()
                self.assertEqual(window.session['revision'], before + 1)
                window.turn('hint')
                self.assertEqual(window.session['hint_level'], 1)
                previous_task = window.task['task_id']
                window.turn('practice')
                self.assertNotEqual(window.task['task_id'], previous_task)
                window.language_var.set('English')
                window.change_language()
                self.assertEqual(str(window.check_button.cget('text')), 'Check')
                window._page_resized(SimpleNamespace(width=760))
                self.assertEqual(int(window.lesson_card.grid_info()['row']), 2)
                window._page_resized(SimpleNamespace(width=1040))
                self.assertEqual(int(window.lesson_card.grid_info()['column']), 1)
                window.refresh_history()
                self.assertTrue(window.history_rows)
                window.history_tree.selection_set('0')
                window.resume_selected()
                self.assertEqual(window.session['session_id'], window.history_rows[0]['session_id'])
                window.export_session()
                self.assertTrue(list(Path(root).rglob('*.json')))
                window.stop_session()
                self.assertEqual(window.session['state'], 'stopped')
                self.assertEqual(str(window.check_button.cget('state')), 'disabled')
            finally:
                window.close()
                service.close()

    def test_start_retry_preserves_created_task(self):
        from learning_module.service import LearningService

        class UncertainStart:
            def __init__(self, actual):
                self.actual = actual
                self.lost = False
                self.task_creations = 0

            def handle(self, request):
                if request['operation'] == 'task.create':
                    self.task_creations += 1
                response = self.actual.handle(request)
                if request['operation'] == 'session.start' and not self.lost:
                    self.lost = True
                    raise OSError('response lost after local commit')
                return response

        with TemporaryDirectory(prefix='learning-ui-retry-') as root:
            service = LearningService(root)
            transport = UncertainStart(service)
            try:
                window = LearningWindow(transport)
            except tk.TclError as error:
                service.close()
                self.skipTest('Tk display unavailable: ' + type(error).__name__)
            window.withdraw()
            try:
                window.start_preset()
                self.assertIsNone(window.session)
                self.assertIsNotNone(window._pending_start)
                window.start_preset()
                self.assertEqual(window.session['state'], 'active')
                self.assertEqual(transport.task_creations, 1)
                window.refresh_history()
                self.assertEqual(len(window.history_rows), 1)
            finally:
                window.close()
                service.close()


if __name__ == '__main__':
    unittest.main()
