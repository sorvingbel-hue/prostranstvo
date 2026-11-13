"""Manual finite local speech recognition. Construction never loads a model."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from activity_core import no_links
from speech_worker import APP_ROOT, MAX_WAV, MAX_TEXT, SpeechFailure, decode_wave, local_assets

ERRORS = frozenset(('model_missing', 'assets_invalid', 'invalid_audio', 'asr_unavailable', 'no_speech',
                    'worker_missing', 'recording_unavailable', 'asr_timeout', 'asr_busy', 'invalid_response'))


class CatSpeechError(RuntimeError):
    def __init__(self, code='asr_unavailable'):
        self.code = code if isinstance(code, str) and code in ERRORS else 'asr_unavailable'
        super().__init__(self.code)


def _record_root():
    from paths import DEPLOY_ROOT
    return DEPLOY_ROOT / 'data' / 'cat-companion' / 'voice'


class CatSpeech:
    def __init__(self, root=None, record_root=None, *, popen=None):
        self.root = Path(root if root is not None else APP_ROOT).absolute()
        self.record_root = Path(record_root if record_root is not None else _record_root()).absolute()
        self._popen = popen if popen is not None else subprocess.Popen
        self._lock = threading.RLock()
        self._process = None
        self._job_event = None

    def _command(self):
        try:
            if getattr(sys, 'frozen', False):
                helper = self.root / 'ProstranstvoCore.exe'
                no_links(helper, True)
                if not helper.is_file():
                    raise ValueError()
                return [str(helper), 'speech-transcribe']
            script = self.root / 'speech_worker.py'
            no_links(script, True)
            if not script.is_file():
                raise ValueError()
            config = self.root / 'python-path.txt'
            no_links(config, True)
            if config.stat().st_size > 2048:
                raise ValueError()
            helper = Path(config.read_text(encoding='utf-8-sig').strip())
            if not helper.is_absolute() or helper.name.lower() not in ('python.exe', 'pythonw.exe'):
                raise ValueError()
            no_links(helper, True)
            if not helper.is_file():
                raise ValueError()
            return [str(helper), '-B', str(script)]
        except (OSError, ValueError):
            raise CatSpeechError('worker_missing') from None

    def status(self):
        try:
            local_assets(self.root)
            self._command()
            return {'available': True, 'language': 'ru', 'engine': 'vosk', 'reason': 'ready'}
        except SpeechFailure as error:
            reason = error.code
        except CatSpeechError as error:
            reason = error.code
        return {'available': False, 'language': 'ru', 'engine': 'vosk', 'reason': reason}

    def _read(self, source):
        try:
            path = Path(source).absolute()
            if path.parent != self.record_root or not re.fullmatch(r'recording-\d{8}T\d{6}Z-[a-f0-9]{32}\.wav', path.name):
                raise ValueError()
            no_links(path, True)
            initial = path.stat()
            if not 46 <= initial.st_size <= MAX_WAV:
                raise ValueError()
            with path.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                if opened.st_nlink != 1 or (opened.st_ino, opened.st_dev) != (initial.st_ino, initial.st_dev):
                    raise ValueError()
                payload = stream.read(MAX_WAV + 1)
            no_links(path, True)
            final = path.stat()
            if (final.st_ino, final.st_dev, final.st_size, final.st_mtime_ns) != (initial.st_ino, initial.st_dev, initial.st_size, initial.st_mtime_ns):
                raise ValueError()
            decode_wave(payload)
            return payload
        except SpeechFailure as error:
            raise CatSpeechError(error.code) from None
        except (OSError, ValueError, TypeError):
            raise CatSpeechError('recording_unavailable') from None

    @staticmethod
    def _receipt(raw, returncode):
        try:
            if not isinstance(raw, bytes) or len(raw) > 32768:
                raise ValueError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
            if isinstance(value, dict) and value.get('status') == 'error':
                raise CatSpeechError(value.get('code'))
            if (returncode != 0 or not isinstance(value, dict) or set(value) != {'status', 'text', 'language', 'local'}
                    or value['status'] != 'transcribed' or value['language'] != 'ru' or value['local'] is not True
                    or not isinstance(value['text'], str) or not value['text'].strip() or len(value['text']) > MAX_TEXT
                    or any(ord(c) < 32 for c in value['text'])):
                raise ValueError()
            return value
        except CatSpeechError:
            raise
        except (ValueError, UnicodeError, TypeError, KeyError):
            raise CatSpeechError('invalid_response') from None

    def transcribe(self, path, *, cancel_event=None):
        event = cancel_event if cancel_event is not None else threading.Event()
        with self._lock:
            if self._job_event is not None:
                raise CatSpeechError('asr_busy')
            if event.is_set():
                return {'status': 'stopped'}
            self._job_event = event
        process = None
        try:
            payload = self._read(path)
            try:
                local_assets(self.root)
            except SpeechFailure as error:
                raise CatSpeechError(error.code) from None
            command = self._command()
            with self._lock:
                if event.is_set():
                    return {'status': 'stopped'}
                process = self._popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      cwd=str(self.root), shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._process = process
                if event.is_set():
                    process.terminate()
                    return {'status': 'stopped'}
            raw, _ = process.communicate(input=payload, timeout=60)
            if event.is_set():
                return {'status': 'stopped'}
            return self._receipt(raw, process.returncode)
        except subprocess.TimeoutExpired:
            raise CatSpeechError('asr_timeout') from None
        except (OSError, ValueError):
            if event.is_set():
                return {'status': 'stopped'}
            raise CatSpeechError() from None
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            process.kill()
                            process.wait(timeout=2)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                for name in ('stdin', 'stdout'):
                    stream = getattr(process, name, None)
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
            with self._lock:
                self._process, self._job_event = None, None

    def stop(self):
        with self._lock:
            if self._job_event is None:
                return {'status': 'idle'}
            self._job_event.set()
            if self._process is not None and self._process.poll() is None:
                try:
                    self._process.terminate()
                except OSError:
                    pass
            return {'status': 'stopping'}
