import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Synthetic timer observations; no real lesson, registry, windows or messages."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cat_study import CatStudyMonitor


class Clock:
    def __init__(self):
        self.wall, self.mono = 1000000., 500.
    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class CatStudyTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / 'data' / 'tests-cat-study'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temporary.name)
        self.clock = Clock()
        self.monitor = CatStudyMonitor(self.root / 'study', lambda:self.clock.wall, lambda:self.clock.mono)
        self.path = self.monitor.path
    def tearDown(self):
        self.temporary.cleanup()
    def lease(self, seconds=3600, ident='a'*32):
        return {'version':1, 'session_id':ident, 'expires_utc':self.clock.wall+seconds, 'duration_seconds':seconds}
    def write(self, value=None):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.lease() if value is None else value), encoding='utf-8')

    def test_import_constructor_and_idle_poll_create_nothing(self):
        self.assertFalse(self.path.parent.exists())
        value = self.monitor.poll()
        self.assertEqual(value['events'], [])
        self.assertEqual(value['status']['state'], 'idle')
        self.assertFalse(value['status']['enforcement_confirmed'])
        self.assertFalse(self.path.parent.exists())

    def test_start_five_minutes_and_end_are_once_and_local_timer_attributed(self):
        self.write()
        initial = self.path.read_bytes()
        self.assertEqual([e['kind'] for e in self.monitor.poll()['events']], ['study_started'])
        self.assertEqual(self.monitor.poll()['events'], [])
        self.clock.advance(3301)
        warning = self.monitor.poll()
        self.assertEqual([e['kind'] for e in warning['events']], ['study_five_minutes'])
        self.assertEqual(warning['events'][0]['author'], 'local_study_timer')
        self.assertEqual(self.monitor.poll()['events'], [])
        self.clock.advance(300)
        finished = self.monitor.poll()
        self.assertEqual(finished['status']['state'], 'expired')
        self.assertEqual([e['kind'] for e in finished['events']], ['study_ended'])
        self.assertEqual(self.monitor.poll()['events'], [])
        self.assertEqual(self.path.read_bytes(), initial)

    def test_short_session_has_one_start_notice_not_two(self):
        self.write(self.lease(120))
        self.assertEqual([e['kind'] for e in self.monitor.poll()['events']], ['study_started'])
        self.clock.advance(1)
        self.assertEqual(self.monitor.poll()['events'], [])

    def test_indefinite_session_has_one_start_and_no_countdown_until_cleared(self):
        self.write({'version': 1, 'session_id': 'a' * 32, 'expires_utc': None, 'duration_seconds': 0})
        started = self.monitor.poll()
        self.assertTrue(started['status']['active'])
        self.assertIsNone(started['status']['remaining_seconds'])
        self.assertEqual([(event['kind'], event['remaining_seconds']) for event in started['events']],
                         [('study_started', None)])
        self.clock.advance(86400)
        later = self.monitor.poll()
        self.assertTrue(later['status']['active'])
        self.assertEqual(later['events'], [])
        self.path.unlink()
        cleared = self.monitor.poll()
        self.assertFalse(cleared['status']['active'])
        self.assertEqual([event['kind'] for event in cleared['events']], ['study_cleared'])

    def test_no_end_for_an_expired_session_first_seen(self):
        self.write(self.lease(60))
        self.clock.advance(61)
        self.assertEqual(self.monitor.poll()['status']['state'], 'expired')
        self.assertEqual(self.monitor.poll()['events'], [])

    def test_clock_rollback_cannot_extend_and_expiry_cannot_resurrect(self):
        self.write(self.lease(600))
        self.monitor.poll()
        self.clock.mono += 200
        self.clock.wall -= 200
        self.assertEqual(self.monitor.snapshot()['remaining_seconds'], 400)
        self.clock.mono += 401
        self.assertEqual(self.monitor.poll()['status']['state'], 'expired')
        self.clock.wall -= 10000
        self.assertEqual(self.monitor.snapshot()['remaining_seconds'], 0)

    def test_parent_new_session_adopts_new_original_deadline(self):
        self.write(self.lease(120))
        first = self.monitor.poll()['events'][0]
        self.clock.advance(30)
        self.write(self.lease(3600, 'b'*32))
        value = self.monitor.poll()
        self.assertEqual(value['status']['remaining_seconds'], 3600)
        self.assertEqual(value['events'][0]['kind'], 'study_started')
        self.assertNotEqual(first['id'], value['events'][0]['id'])

    def test_same_session_edited_fails_honestly_without_end_notice(self):
        value = self.lease()
        self.write(value)
        self.monitor.poll()
        value['expires_utc'] += 3600
        self.write(value)
        observed = self.monitor.poll()
        self.assertEqual(observed['status']['state'], 'unavailable')
        self.assertIsNone(observed['status']['active'])
        self.assertEqual(observed['events'], [])

    def test_file_removed_neutral_cleared_notice_not_permission_to_play(self):
        self.write()
        self.monitor.poll()
        self.path.unlink()
        value = self.monitor.poll()
        self.assertEqual(value['events'][0]['kind'], 'study_cleared')
        self.assertIsNone(value['events'][0]['remaining_seconds'])
        self.assertEqual(self.monitor.poll()['events'], [])

    def test_malformed_oversized_and_wrong_schema_are_unknown_not_ended(self):
        self.write()
        self.monitor.poll()
        for raw in (b'{', b' '*2049, b'[]', json.dumps({**self.lease(), 'extra':True}).encode(), b'{"version":1,"version":1}'):
            with self.subTest(raw=raw[:30]):
                self.path.write_bytes(raw)
                value = self.monitor.poll()
                self.assertEqual(value['status']['state'], 'unavailable')
                self.assertEqual(value['events'], [])
                self.assertNotIn('raw', value['status'])

    def test_error_recovery_does_not_repeat_start(self):
        value = self.lease()
        self.write(value)
        self.monitor.poll()
        self.path.write_bytes(b'bad')
        self.assertEqual(self.monitor.poll()['status']['state'], 'unavailable')
        self.clock.advance(20)
        self.write(value)
        self.assertEqual(self.monitor.poll()['events'], [])

    def test_disappearing_during_read_is_unknown_not_an_end(self):
        self.write()
        self.monitor.poll()
        with mock.patch('cat_study.no_links'), mock.patch.object(Path, 'stat', side_effect=FileNotFoundError):
            value = self.monitor.poll()
        self.assertEqual(value['status']['state'], 'unavailable')
        self.assertEqual(value['events'], [])

    def test_hardlink_lease_is_rejected(self):
        target = self.root/'source.json'
        target.write_text(json.dumps(self.lease()), encoding='utf-8')
        self.path.parent.mkdir()
        try:
            os.link(target, self.path)
        except OSError:
            self.skipTest('Hardlink fixture unavailable')
        self.assertEqual(self.monitor.poll()['status']['state'], 'unavailable')

    def test_invalid_or_reversed_clocks_are_unknown(self):
        self.write()
        self.monitor.poll()
        self.clock.mono -= 1
        self.assertEqual(self.monitor.poll()['status']['state'], 'unavailable')
        self.clock.mono += 1
        self.clock.wall = float('nan')
        self.assertEqual(self.monitor.poll()['status']['state'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
