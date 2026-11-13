import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Bounded rule/IPC fixtures only; no native process, Telegram or personal data."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from pet_tricks import PetTrickError, PetTrickStore, parse_json, validate_config


def config():
    return {'version': 1, 'enabled': False, 'rules': [
        {'id': 'a' * 32, 'name': 'Трюк Паши', 'enabled': False, 'event': 'petted',
         'cooldown_seconds': 20, 'steps': [{'action': 'turn', 'duration_ms': 450},
             {'action': 'heart', 'duration_ms': 1000}, {'action': 'purr', 'duration_ms': 1500}]}]}


class TrickStoreTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / 'tests-work'; base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='tricks-', dir=base)
        self.root = Path(self.temp.name)
        self.now = 1800000000
        self.store = PetTrickStore(self.root / 'state', clock=lambda: self.now)
    def tearDown(self): self.temp.cleanup()
    def native(self, **changes):
        value = {'version': 1, 'pid': 1234, 'updated_utc': self.now, 'config_ok': True, 'active_rule_id': ''}
        value.update(changes)
        self.store.root.mkdir(exist_ok=True)
        (self.store.root / 'status.json').write_text(json.dumps(value), encoding='utf-8')
    def ack(self, command, state='completed', reason='completed'):
        (self.store.root / 'preview-status.json').write_text(json.dumps({'version': 1,
            'request_id': command['request_id'], 'state': state, 'reason': reason,
            'updated_utc': self.now}), encoding='utf-8')

    def test_constructor_inert_default_disabled_and_templates_not_seeded(self):
        self.assertFalse(self.store.root.exists())
        self.assertEqual(self.store.load(), {'version': 1, 'enabled': False, 'rules': []})
        self.assertFalse((self.store.root / 'rules.json').exists())
        self.assertTrue(all(not r['enabled'] for r in self.store.templates()))
        self.assertFalse((self.store.root / 'rules.json').exists())

    def test_roundtrip_exact_bounded_data_without_enabling(self):
        value = self.store.save(config())
        self.assertEqual(value, config())
        self.assertEqual(self.store.load(), value)
        self.assertFalse(self.store.load()['enabled'])
        self.assertFalse((self.store.root / 'command.json').exists())

    def test_unknown_fields_and_actions_refused(self):
        for mutate in (lambda c: c.update(script='danger'),
                       lambda c: c['rules'][0].update(command='calc'),
                       lambda c: c['rules'][0]['steps'][0].update(path='file'),
                       lambda c: c['rules'][0]['steps'][0].update(action='open_url'),
                       lambda c: c['rules'][0].update(event='parent_command')):
            value = config(); mutate(value)
            with self.subTest(value=value), self.assertRaises(PetTrickError): self.store.save(value)

    def test_types_and_plain_names_are_strict(self):
        cases = []
        for field, value in (('id', 'A'*32), ('id', True), ('name', ''), ('name', ' padded '),
                             ('name', 'x\ny'), ('name', 'x'*61), ('enabled', 1),
                             ('event', []), ('cooldown_seconds', True), ('cooldown_seconds', 9), ('cooldown_seconds', 3601)):
            case = config(); case['rules'][0][field] = value; cases.append(case)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(PetTrickError): validate_config(case)
        for value in (True, 0, 99):
            case = config(); case['version'] = value
            with self.assertRaises(PetTrickError): validate_config(case)

    def test_rule_and_step_limits_and_duplicate_ids(self):
        value = config(); value['rules'] *= 2
        with self.assertRaises(PetTrickError): validate_config(value)
        value = config(); value['rules'] = [dict(value['rules'][0], id=f'{i:032x}') for i in range(9)]
        with self.assertRaises(PetTrickError): validate_config(value)
        for steps in ([], [{'action': 'wait', 'duration_ms': 100}] * 9):
            value = config(); value['rules'][0]['steps'] = steps
            with self.assertRaises(PetTrickError): validate_config(value)

    def test_duration_and_purr_bounds(self):
        cases = [([{'action': 'wait', 'duration_ms': 5000}]*7),
                 ([{'action': 'purr', 'duration_ms': 2001}]),
                 ([{'action': 'purr', 'duration_ms': 1700}]*2),
                 ([{'action': 'turn', 'duration_ms': True}]),
                 ([{'action': [], 'duration_ms': 100}])]
        for steps in cases:
            value = config(); value['rules'][0]['steps'] = steps
            with self.subTest(steps=steps), self.assertRaises(PetTrickError): validate_config(value)
        value = config(); value['rules'][0]['steps'] = [{'action': 'wait', 'duration_ms': 5000}] * 6
        self.assertEqual(sum(s['duration_ms'] for s in validate_config(value)['rules'][0]['steps']), 30000)

    def test_duplicate_json_keys_nan_and_bad_json_refused(self):
        for body in ('{"version":1,"version":1}', '{"x":NaN}', '{'):
            with self.assertRaises(PetTrickError): parse_json(body)

    def test_preview_needs_native_and_saved_rule(self):
        self.store.save(config())
        with self.assertRaisesRegex(PetTrickError, 'native_unavailable'): self.store.preview('a'*32)
        self.native()
        with self.assertRaisesRegex(PetTrickError, 'rule_missing'): self.store.preview('b'*32)
        self.native(config_ok=False)
        with self.assertRaisesRegex(PetTrickError, 'config_invalid'): self.store.preview('a'*32)

    def test_preview_disabled_saved_rule_explicit_only_and_stop_supersedes(self):
        self.store.save(config()); self.native()
        run = self.store.preview('a'*32)
        self.assertEqual(run['action'], 'run'); self.assertEqual(run['expires_utc'], self.now+20)
        self.assertEqual(self.store.snapshot()['preview']['state'], 'queued')
        with self.assertRaisesRegex(PetTrickError, 'preview_busy'): self.store.preview('a'*32)
        stop = self.store.stop()
        self.assertNotEqual(stop['request_id'], run['request_id'])
        self.assertEqual(stop['action'], 'stop'); self.assertEqual(stop['rule_id'], '')
        self.assertEqual(self.store.snapshot()['preview']['action'], 'stop')

    def test_only_matching_terminal_ack_frees_pending(self):
        self.store.save(config()); self.native(); run = self.store.preview('a'*32)
        self.ack(dict(run, request_id='b'*32))
        self.assertEqual(self.store.snapshot()['preview']['state'], 'queued')
        with self.assertRaises(PetTrickError): self.store.preview('a'*32)
        self.ack(run)
        self.assertEqual(self.store.snapshot()['preview']['state'], 'completed')
        self.assertNotEqual(self.store.preview('a'*32)['request_id'], run['request_id'])

    def test_stale_heartbeat_unavailable_expired_pickup_and_active_no_overlap(self):
        self.store.save(config()); self.native(); run = self.store.preview('a'*32)
        self.now += 21
        self.assertFalse(self.store.snapshot()['native']['available'])
        self.assertEqual(self.store.snapshot()['preview']['state'], 'expired')
        self.native(); self.store.preview('a'*32)
        self.now += 21; self.native(active_rule_id='a'*32)
        with self.assertRaisesRegex(PetTrickError, 'preview_busy'): self.store.preview('a'*32)

    def test_native_malformed_and_clock_future_fail_closed(self):
        self.native(updated_utc=self.now+5)
        self.assertFalse(self.store.snapshot()['native']['available'])
        self.native(pid=True)
        self.assertFalse(self.store.snapshot()['native']['available'])
        (self.store.root / 'status.json').write_text('{', encoding='utf-8')
        self.assertFalse(self.store.snapshot()['native']['available'])

    def test_native_reason_codes_remain_static(self):
        self.store.save(config()); self.native(); run = self.store.preview('a'*32)
        for reason in ('busy', 'disabled', 'clock_invalid', 'cooldown', 'interrupted'):
            self.ack(run, 'blocked', reason)
            self.assertEqual(self.store.snapshot()['preview']['reason'], reason)
        self.ack(run, 'failed', 'private_payload')
        self.assertEqual(self.store.snapshot()['preview']['reason'], 'failed')

    def test_stop_is_available_without_native(self):
        command = self.store.stop()
        self.assertEqual(command['action'], 'stop')
        self.assertFalse(self.store.snapshot()['native']['available'])

    def test_corrupt_and_oversized_config_preserved(self):
        self.store.root.mkdir()
        target = self.store.root / 'rules.json'
        for body in (b'not json', b' '*32769):
            target.write_bytes(body)
            with self.assertRaises(PetTrickError): self.store.snapshot()
            self.assertEqual(target.read_bytes(), body)

    def test_hardlink_config_refused_without_modifying_target(self):
        self.store.root.mkdir(); outside = self.root / 'owned-target.json'
        outside.write_text(json.dumps(config()), encoding='utf-8')
        try: os.link(outside, self.store.root / 'rules.json')
        except OSError: self.skipTest('Fixture hardlink unavailable')
        before = outside.read_bytes()
        with self.assertRaises(ValueError): self.store.save(config())
        self.assertEqual(outside.read_bytes(), before)


if __name__ == '__main__': unittest.main()
