import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Voice fixtures never open a microphone, play sound, or spawn a real helper."""
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import cat_voice
from cat_voice import CatVoice, CatVoiceError


STATUS = dict(status='ok', russian_tts_available=True, microphone_devices=0, asr_available=False, microphone_opened=False, audio_played=False)
SPOKEN = dict(status='spoken', playback_requested=True, audible_confirmed=False)


class FakeProcess:
    def __init__(self, response, code=0):
        self.response, self.code = response, code
        self.returncode = None
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(response)
        self.input = None
        self.terminated = self.killed = False
        self.wait_error = None

    def communicate(self, input=None, timeout=None):
        self.input = input
        if self.wait_error:
            raise self.wait_error
        self.returncode = self.code
        return self.response, None

    def wait(self, timeout=None):
        if self.wait_error:
            error, self.wait_error = self.wait_error, None
            raise error
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


@unittest.skipUnless(os.name == 'nt', 'Windows local helper contract')
class VoiceTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / 'tests-work'
        base.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='cat-voice-', dir=base)
        self.root = Path(self.temporary.name)
        (self.root / 'KittenVoice.exe').write_bytes(b'fixture, not executable')
        self.created = []
        self.response = json.dumps(STATUS)
        self.factory_error = None
        def factory(*args, **kwargs):
            if self.factory_error:
                raise self.factory_error
            process = FakeProcess(self.response)
            self.created.append((process, args, kwargs))
            return process
        self.factory = mock.Mock(side_effect=factory)
        self.voice = CatVoice(self.root, popen=self.factory)

    def tearDown(self):
        self.temporary.cleanup()

    def test_constructor_inert(self):
        self.factory.assert_not_called()
        self.assertEqual([p.name for p in self.root.iterdir()], ['KittenVoice.exe'])

    def test_status_fixed_helper_and_no_device_activation_claim(self):
        self.assertEqual(self.voice.status(), STATUS)
        process, args, kwargs = self.created[0]
        self.assertEqual(args[0], [str(self.root / 'KittenVoice.exe'), '--status'])
        self.assertIs(kwargs['shell'], False)
        self.assertEqual(kwargs['cwd'], str(self.root))
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertIsNone(process.input)

    def test_speech_text_only_stdin_and_receipt_not_audibility(self):
        self.response = json.dumps(SPOKEN)
        value = self.voice.speak('Привет, Паша! <не XML>')
        self.assertEqual(value, SPOKEN)
        process, args, kwargs = self.created[0]
        self.assertEqual(args[0], [str(self.root / 'KittenVoice.exe'), '--speak'])
        self.assertEqual(process.input, 'Привет, Паша! <не XML>')
        self.assertNotIn('Паша', repr(args))
        self.assertFalse(value['audible_confirmed'])

    def test_invalid_text_never_starts_helper(self):
        for value in ('', ' ', 'a' * 4001, '\ud800', 'bad\0', '\ufffe', '\uffff', None, '😀' * 2001):
            with self.subTest(value=repr(value)[:25]), self.assertRaises(CatVoiceError):
                self.voice.speak(value)
        self.factory.assert_not_called()

    def test_invalid_durations_never_start_helper(self):
        for value in (0, 16, -1, True, 8.0, '8'):
            with self.subTest(value=value), self.assertRaises(CatVoiceError):
                self.voice.record(value)
        self.factory.assert_not_called()

    def test_missing_or_hardlinked_helper_refused(self):
        (self.root / 'KittenVoice.exe').unlink()
        with self.assertRaises(CatVoiceError) as caught:
            self.voice.status()
        self.assertEqual(caught.exception.code, 'helper_missing')
        (self.root / 'source.exe').write_bytes(b'fixture')
        (self.root / 'KittenVoice.exe').hardlink_to(self.root / 'source.exe')
        with self.assertRaises(CatVoiceError):
            self.voice.status()
        self.factory.assert_not_called()

    def test_start_error_does_not_expose_payload(self):
        self.factory_error = OSError('private microphone or token error')
        with self.assertRaises(CatVoiceError) as caught:
            self.voice.status()
        self.assertEqual(str(caught.exception), 'voice_unavailable')

    def test_invalid_and_duplicate_json_refused(self):
        for raw in ('not json', '[]', '{"status":"ok","status":"ok"}', 'x' * (cat_voice.MAX_RESPONSE + 1)):
            self.response = raw
            with self.subTest(raw=raw[:35]), self.assertRaises(CatVoiceError):
                self.voice.status()

    def test_static_error_only(self):
        self.response = json.dumps({'status': 'error', 'code': 'microphone_unavailable', 'private_text': 'secret'})
        with self.assertRaises(CatVoiceError) as caught:
            self.voice.record()
        self.assertEqual(caught.exception.code, 'microphone_unavailable')
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(CatVoiceError({'unexpected': True}).code, 'voice_unavailable')

    def test_status_schema_strict(self):
        for value in ({**STATUS, 'microphone_devices': True}, {**STATUS, 'asr_available': True}, {**STATUS, 'microphone_opened': True}, {**STATUS, 'extra': 1}):
            self.response = json.dumps(value)
            with self.subTest(value=value), self.assertRaises(CatVoiceError):
                self.voice.status()

    def recording(self, **changes):
        folder = self.root / 'recordings'
        folder.mkdir(exist_ok=True)
        path = folder / ('recording-20261106T120000Z-' + 'a' * 32 + '.wav')
        pcm = b'\0' * 44100
        path.write_bytes(b'RIFF' + struct.pack('<I', len(pcm) + 36) + b'WAVE' + b'\0' * 32 + pcm)
        value = dict(status='recorded', path=str(path), duration_seconds=1, bytes=path.stat().st_size, stopped=False, asr_available=False, transcribed=False)
        value.update(changes)
        self.response = json.dumps(value)
        return folder, path

    def test_manual_record_fixed_duration_local_wave_no_asr(self):
        folder, path = self.recording()
        with mock.patch('cat_voice._record_root', return_value=folder):
            value = self.voice.record(8)
        self.assertEqual(value['path'], str(path))
        self.assertFalse(value['transcribed'])
        self.assertEqual(self.created[0][1][0], [str(self.root / 'KittenVoice.exe'), '--record', '8'])

    def test_recorded_path_or_wave_mismatch_refused(self):
        folder, path = self.recording()
        with mock.patch('cat_voice._record_root', return_value=self.root / 'other'), self.assertRaises(CatVoiceError):
            self.voice.record()
        self.recording(bytes=100)
        with mock.patch('cat_voice._record_root', return_value=folder), self.assertRaises(CatVoiceError):
            self.voice.record()

    def test_no_fake_transcription(self):
        folder, path = self.recording(transcribed=True)
        with mock.patch('cat_voice._record_root', return_value=folder), self.assertRaises(CatVoiceError):
            self.voice.record()

    def test_stop_when_idle_does_nothing(self):
        self.assertEqual(self.voice.stop(), {'status': 'idle'})
        self.factory.assert_not_called()

    def test_stop_record_requests_own_pipe_and_preserves_process(self):
        process = FakeProcess('')
        self.voice._process, self.voice._mode = process, 'record'
        self.assertEqual(self.voice.stop(), {'status': 'stopping'})
        self.assertEqual(process.stdin.getvalue(), 'stop\n')
        self.assertFalse(process.terminated)
        self.assertIs(self.voice._cancelled, process)

    def test_stop_speech_only_retained_child(self):
        process, other = FakeProcess(''), FakeProcess('')
        self.voice._process, self.voice._mode = process, 'speak'
        self.voice.stop()
        self.assertTrue(process.terminated)
        self.assertFalse(other.terminated)

    def test_busy_does_not_spawn_second_child(self):
        self.voice._process = FakeProcess('')
        with self.assertRaises(CatVoiceError) as caught:
            self.voice.status()
        self.assertEqual(caught.exception.code, 'voice_busy')
        self.factory.assert_not_called()

    def test_timeout_terminates_only_own_child_and_clears_busy_state(self):
        process = FakeProcess('')
        process.wait_error = subprocess.TimeoutExpired('fixture-helper', 8)
        self.factory.side_effect = None
        self.factory.return_value = process
        with self.assertRaises(CatVoiceError) as caught:
            self.voice.status()
        self.assertEqual(caught.exception.code, 'voice_unavailable')
        self.assertTrue(process.terminated)
        self.assertIsNone(self.voice._process)

    def test_record_keeps_stop_pipe_open_until_child_finishes(self):
        folder, path = self.recording(stopped=True)
        process = FakeProcess(self.response)
        original_wait = process.wait
        def wait(timeout=None):
            self.assertFalse(process.stdin.closed)
            return original_wait(timeout)
        process.wait = wait
        self.factory.side_effect = None
        self.factory.return_value = process
        with mock.patch('cat_voice._record_root', return_value=folder):
            self.assertTrue(self.voice.record()['stopped'])

    def test_cancel_before_start_never_activates_device(self):
        event = threading.Event()
        event.set()
        self.assertEqual(self.voice.record(cancel_event=event), {'status': 'stopped'})
        self.assertEqual(self.voice.speak('Привет!', cancel_event=event), {'status': 'stopped'})
        self.factory.assert_not_called()

    def test_cancel_during_process_creation_terminates_registered_child(self):
        event = threading.Event()
        process = FakeProcess(json.dumps(SPOKEN))
        def factory(*args, **kwargs):
            event.set()
            return process
        self.factory.side_effect = factory
        self.assertEqual(self.voice.speak('Привет!', cancel_event=event), {'status': 'stopped'})
        self.assertTrue(process.terminated)
        self.assertIsNone(process.input)
        self.assertIsNone(self.voice._process)


if __name__ == '__main__':
    unittest.main()
