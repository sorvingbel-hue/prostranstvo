import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
"""Offline ASR contract fixtures. No microphone, audio playback or network."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import wave

from cat_speech import CatSpeech, CatSpeechError
import speech_worker


def wav(seconds=1, rate=22050, channels=1, width=2):
    memory = io.BytesIO()
    with wave.open(memory, 'wb') as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(b'\0' * int(seconds * rate) * channels * width)
    return memory.getvalue()


class Process:
    def __init__(self, receipt=None):
        self.returncode = None
        self.receipt = json.dumps(receipt or {'status': 'transcribed', 'text': 'привет котик', 'language': 'ru', 'local': True}).encode()
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
        self.terminated = self.killed = False
        self.payload = None
        self.error = None

    def communicate(self, input=None, timeout=None):
        self.payload = input
        if self.error:
            raise self.error
        self.returncode = 0
        return self.receipt, None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated, self.returncode = True, -15

    def kill(self):
        self.killed, self.returncode = True, -9

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class SpeechTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / 'tests-work'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='cat-speech-', dir=base)
        self.root = Path(self.temp.name)
        self.record_root = self.root / 'recordings'
        self.record_root.mkdir()
        self.path = self.record_root / ('recording-20261106T120000Z-' + 'a' * 32 + '.wav')
        self.payload = wav()
        self.path.write_bytes(self.payload)
        (self.root / 'speech_worker.py').write_text('# fixture never executed', encoding='utf-8')
        (self.root / 'python-path.txt').write_text(sys.executable, encoding='utf-8')
        self.assets = self.root / 'assets' / 'speech'
        files = {}
        for name in ('native/libvosk.dll', 'model/am/final.mdl', 'model/conf/mfcc.conf'):
            path = self.assets / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
            files[name] = {'size': 7, 'sha256': hashlib.sha256(b'fixture').hexdigest()}
        self.manifest = dict(schema=1, engine='vosk', version='0.3.45', language='ru', model='vosk-model-small-ru-0.22', files=files)
        self.save_manifest()
        self.process = Process()
        self.factory = mock.Mock(return_value=self.process)
        self.speech = CatSpeech(self.root, self.record_root, popen=self.factory)

    def save_manifest(self):
        (self.assets / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_construct_status_inert(self):
        self.assertTrue(self.speech.status()['available'])
        self.factory.assert_not_called()
        self.assertEqual(self.speech.status()['engine'], 'vosk')

    def test_missing_model_static_status(self):
        (self.assets / 'manifest.json').unlink()
        self.assertEqual(self.speech.status(), {'available': False, 'language': 'ru', 'engine': 'vosk', 'reason': 'model_missing'})
        self.factory.assert_not_called()

    def test_missing_worker_static_status(self):
        (self.root / 'speech_worker.py').unlink()
        self.assertEqual(self.speech.status()['reason'], 'worker_missing')

    def test_bytes_only_stdin_fixed_worker(self):
        value = self.speech.transcribe(self.path)
        self.assertEqual(value['text'], 'привет котик')
        command = self.factory.call_args.args[0]
        self.assertEqual(command[-1], str(self.root / 'speech_worker.py'))
        self.assertNotIn(str(self.path), repr(command))
        self.assertEqual(self.process.payload, self.payload)
        self.assertFalse(self.factory.call_args.kwargs['shell'])
        self.assertIs(self.factory.call_args.kwargs['stderr'], subprocess.DEVNULL)

    def test_no_caller_executable_path(self):
        (self.root / 'python-path.txt').write_text('cmd.exe', encoding='utf-8')
        with self.assertRaises(CatSpeechError) as caught:
            self.speech.transcribe(self.path)
        self.assertEqual(caught.exception.code, 'worker_missing')
        self.factory.assert_not_called()

    def test_wrong_folder_or_filename_refused(self):
        for path in (self.root / self.path.name, self.record_root / 'arbitrary.wav'):
            path.write_bytes(self.payload)
            with self.subTest(path=path.name), self.assertRaises(CatSpeechError):
                self.speech.transcribe(path)
        self.factory.assert_not_called()

    def test_hardlink_record_refused(self):
        target = self.record_root / ('recording-20261106T120000Z-' + 'b' * 32 + '.wav')
        target.hardlink_to(self.path)
        with self.assertRaises(CatSpeechError):
            self.speech.transcribe(self.path)
        self.factory.assert_not_called()

    def test_private_file_stable_during_read(self):
        self.path.write_bytes(b'not a wav')
        with self.assertRaises(CatSpeechError):
            self.speech.transcribe(self.path)
        self.factory.assert_not_called()

    def test_bad_or_long_wave_rejected(self):
        for payload in (b'no', wav(16), wav(channels=2), wav(width=1), wav(seconds=.01), self.payload + b'extra'):
            with self.subTest(size=len(payload)), self.assertRaises(speech_worker.SpeechFailure):
                speech_worker.decode_wave(payload)

    def test_actual_rate_and_pcm_preserved(self):
        rate, pcm = speech_worker.decode_wave(self.payload)
        self.assertEqual((rate, len(pcm)), (22050, 44100))

    def test_path_traversal_asset_refused(self):
        self.manifest['files']['../bad.dll'] = {'size': 0, 'sha256': 'a' * 64}
        self.save_manifest()
        self.assertEqual(self.speech.status()['reason'], 'assets_invalid')

    def test_extra_dll_refused(self):
        (self.assets / 'native' / 'injected.dll').write_bytes(b'bad')
        self.assertEqual(self.speech.status()['reason'], 'assets_invalid')

    def test_native_hash_verified_in_worker(self):
        (self.assets / 'native' / 'libvosk.dll').write_bytes(b'changed')
        with self.assertRaises(speech_worker.SpeechFailure):
            speech_worker.local_assets(self.root, verify_hashes=True)

    def test_hardlink_model_refused(self):
        (self.root / 'model-link').hardlink_to(self.assets / 'model' / 'am' / 'final.mdl')
        self.assertEqual(self.speech.status()['reason'], 'assets_invalid')

    def test_precancelled_no_spawn(self):
        event = threading.Event()
        event.set()
        self.assertEqual(self.speech.transcribe(self.path, cancel_event=event), {'status': 'stopped'})
        self.factory.assert_not_called()

    def test_stop_before_process_creation(self):
        original = self.speech._read
        def read(path):
            result = original(path)
            self.assertEqual(self.speech.stop(), {'status': 'stopping'})
            return result
        with mock.patch.object(self.speech, '_read', side_effect=read):
            self.assertEqual(self.speech.transcribe(self.path), {'status': 'stopped'})
        self.factory.assert_not_called()

    def test_stop_during_process_creation(self):
        event = threading.Event()
        def factory(*args, **kwargs):
            event.set()
            return self.process
        self.factory.side_effect = factory
        self.assertEqual(self.speech.transcribe(self.path, cancel_event=event), {'status': 'stopped'})
        self.assertTrue(self.process.terminated)
        self.assertIsNone(self.process.payload)

    def test_stop_own_child_only(self):
        other = Process()
        self.speech._process, self.speech._job_event = self.process, threading.Event()
        self.speech.stop()
        self.assertTrue(self.process.terminated)
        self.assertFalse(other.terminated)

    def test_busy_no_second_process(self):
        self.speech._job_event = threading.Event()
        with self.assertRaises(CatSpeechError) as caught:
            self.speech.transcribe(self.path)
        self.assertEqual(caught.exception.code, 'asr_busy')
        self.factory.assert_not_called()

    def test_timeout_cleanup(self):
        self.process.error = subprocess.TimeoutExpired('fixture', 60)
        with self.assertRaises(CatSpeechError) as caught:
            self.speech.transcribe(self.path)
        self.assertEqual(caught.exception.code, 'asr_timeout')
        self.assertTrue(self.process.terminated)
        self.assertIsNone(self.speech._job_event)

    def test_untrusted_receipts_no_fake_success(self):
        bad = [b'[]', b'bad', b'{"status":"transcribed","status":"bad"}', b'x' * 32769,
               json.dumps({'status': 'transcribed', 'text': 'hello', 'local': False, 'language': 'ru'}).encode()]
        for raw in bad:
            with self.subTest(raw=raw[:40]), self.assertRaises(CatSpeechError):
                self.speech._receipt(raw, 0)

    def test_static_native_error_only(self):
        raw = b'{"status":"error","code":"no_speech","private":"secret"}'
        with self.assertRaises(CatSpeechError) as caught:
            self.speech._receipt(raw, 2)
        self.assertEqual(str(caught.exception), 'no_speech')
        self.assertEqual(CatSpeechError({'secret': 'text'}).code, 'asr_unavailable')

    def test_worker_text_bounds(self):
        for value in (b'{}', b'not JSON', b'{"text":42}', b'{"text":"hello\\nworld"}'):
            with self.assertRaises(speech_worker.SpeechFailure):
                speech_worker._text(value)


if __name__ == '__main__':
    unittest.main()
