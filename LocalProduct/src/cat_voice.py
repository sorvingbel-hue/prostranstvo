"""Explicit local voice actions. Import/construct never activates audio or a mic."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from activity_core import no_links

APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
MAX_RESPONSE = 32768
ERRORS = frozenset(('invalid_arguments', 'invalid_text', 'invalid_duration', 'russian_voice_unavailable',
                   'speech_timeout', 'speech_render_failed', 'speech_unavailable', 'audio_format_invalid',
                   'microphone_unavailable', 'microphone_capture_failed', 'recording_too_short',
                   'recording_save_failed', 'private_folder_unavailable', 'voice_unavailable'))


class CatVoiceError(RuntimeError):
    def __init__(self, code='voice_unavailable'):
        self.code = code if isinstance(code, str) and code in ERRORS | {'voice_busy', 'helper_missing', 'invalid_response', 'recording_unavailable'} else 'voice_unavailable'
        super().__init__(self.code)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CatVoiceError('invalid_response')
        value[key] = item
    return value


def _record_root():
    from paths import DEPLOY_ROOT
    return DEPLOY_ROOT / 'data' / 'cat-companion' / 'voice'


class CatVoice:
    """One child process at a time. Call blocking actions from a non-Tk worker.

    ``record`` records only after an explicit GUI action, for 1..15 seconds.
    Its WAV remains local; no transcription, Telegram or AI call is performed.
    ``stop`` only contacts/kills the process created and retained by this object.
    """
    def __init__(self, root=None, *, popen=None):
        self.root = Path(root if root is not None else APP_ROOT).absolute()
        self._popen = popen if popen is not None else subprocess.Popen
        self._lock = threading.RLock()
        self._process = None
        self._mode = None
        self._cancelled = None

    def _begin(self, mode, *arguments, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return None
        if os.name != 'nt':
            raise CatVoiceError()
        helper = self.root / 'KittenVoice.exe'
        try:
            no_links(helper, True)
            if not helper.is_file():
                raise CatVoiceError('helper_missing')
        except CatVoiceError:
            raise
        except (OSError, ValueError):
            raise CatVoiceError('helper_missing') from None
        with self._lock:
            if cancel_event is not None and cancel_event.is_set():
                return None
            if self._process is not None and self._process.poll() is None:
                raise CatVoiceError('voice_busy')
            try:
                process = self._popen([str(helper), '--' + mode, *arguments], cwd=str(self.root),
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, encoding='utf-8', errors='strict',
                                      creationflags=subprocess.CREATE_NO_WINDOW, shell=False)
            except (OSError, ValueError):
                raise CatVoiceError() from None
            self._process, self._mode, self._cancelled = process, mode, None
            # A stop/close can race the OS process creation. Check again while
            # the retained child is registered and the process lock is held.
            if cancel_event is not None and cancel_event.is_set():
                self._cancelled = process
                try:
                    process.terminate()
                except OSError:
                    pass
            return process

    def _finish(self, process):
        with self._lock:
            cancelled = self._cancelled is process
            if self._process is process:
                self._process, self._mode, self._cancelled = None, None, None
        for stream_name in ('stdin', 'stdout'):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        return cancelled

    def _terminate(self, process):
        with self._lock:
            if self._process is process and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with self._lock:
                if self._process is process and process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    @staticmethod
    def _parse(raw, returncode):
        if not isinstance(raw, str) or len(raw) > MAX_RESPONSE:
            raise CatVoiceError('invalid_response')
        try:
            value = json.loads(raw.strip().lstrip('\ufeff'), object_pairs_hook=_object)
        except (ValueError, TypeError):
            raise CatVoiceError('invalid_response') from None
        if not isinstance(value, dict):
            raise CatVoiceError('invalid_response')
        if value.get('status') == 'error':
            raise CatVoiceError(value.get('code', 'voice_unavailable'))
        if returncode != 0:
            raise CatVoiceError()
        return value

    def _capture(self, mode, *, text=None, seconds=None, cancel_event=None):
        process = self._begin(mode, *([str(seconds)] if mode == 'record' else []), cancel_event=cancel_event)
        if process is None:
            return {'status': 'stopped'}
        raw = None
        try:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate(process)
                return {'status': 'stopped'}
            if mode == 'record':
                # Keep stdin open so stop() can finish the WAV early. The native
                # helper prints one bounded receipt only after capture ends.
                process.wait(timeout=seconds + 6)
                raw = process.stdout.read(MAX_RESPONSE + 1)
            else:
                raw, _ = process.communicate(input=text, timeout=185 if mode == 'speak' else 8)
            if cancel_event is not None and cancel_event.is_set():
                return {'status': 'stopped'}
            return self._parse(raw, process.returncode)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            raise CatVoiceError('speech_timeout' if mode == 'speak' else 'voice_unavailable') from None
        except CatVoiceError:
            with self._lock:
                cancelled = self._cancelled is process
            if cancelled:
                return {'status': 'stopped'}
            raise
        except (OSError, ValueError, UnicodeError):
            with self._lock:
                cancelled = self._cancelled is process
            if cancelled:
                return {'status': 'stopped'}
            raise CatVoiceError() from None
        finally:
            self._finish(process)

    def status(self):
        value = self._capture('status')
        expected = {'status', 'russian_tts_available', 'microphone_devices', 'asr_available', 'microphone_opened', 'audio_played'}
        if (set(value) != expected or value['status'] != 'ok'
                or type(value['russian_tts_available']) is not bool
                or type(value['microphone_devices']) is not int or not 0 <= value['microphone_devices'] <= 128
                or value['asr_available'] is not False or value['microphone_opened'] is not False or value['audio_played'] is not False):
            raise CatVoiceError('invalid_response')
        return value

    def speak(self, text, *, cancel_event=None):
        if (not isinstance(text, str) or not text.strip() or len(text) > 4000
                or any((ord(c) < 32 and c not in '\r\n\t') or c in '\ufffe\uffff' for c in text)):
            raise CatVoiceError('invalid_text')
        try:
            if len(text.encode('utf-16-le', 'strict')) > 8000 or len(text.encode('utf-8', 'strict')) > 16000:
                raise CatVoiceError('invalid_text')
        except UnicodeError:
            raise CatVoiceError('invalid_text') from None
        value = self._capture('speak', text=text, cancel_event=cancel_event)
        if value.get('status') == 'stopped':
            return value
        if (set(value) != {'status', 'playback_requested', 'audible_confirmed'} or value['status'] != 'spoken'
                or value['playback_requested'] is not True or value['audible_confirmed'] is not False):
            raise CatVoiceError('invalid_response')
        return value

    def record(self, seconds=8, *, cancel_event=None):
        if type(seconds) is not int or not 1 <= seconds <= 15:
            raise CatVoiceError('invalid_duration')
        value = self._capture('record', seconds=seconds, cancel_event=cancel_event)
        if value.get('status') == 'stopped':
            return value
        expected = {'status', 'path', 'duration_seconds', 'bytes', 'stopped', 'asr_available', 'transcribed'}
        if (set(value) != expected or value['status'] != 'recorded' or not isinstance(value['path'], str)
                or type(value['duration_seconds']) not in (int, float) or not .09 <= value['duration_seconds'] <= seconds + .1
                or type(value['bytes']) is not int or not 100 <= value['bytes'] <= 15 * 22050 * 2 + 44
                or type(value['stopped']) is not bool or value['asr_available'] is not False or value['transcribed'] is not False):
            raise CatVoiceError('invalid_response')
        path, root = Path(value['path']).absolute(), Path(_record_root()).absolute()
        try:
            if path.parent != root or not re.fullmatch(r'recording-\d{8}T\d{6}Z-[a-f0-9]{32}\.wav', path.name):
                raise CatVoiceError('recording_unavailable')
            no_links(path, True)
            if path.stat().st_size != value['bytes']:
                raise CatVoiceError('recording_unavailable')
            with path.open('rb') as stream:
                header = stream.read(12)
            if len(header) != 12 or header[:4] != b'RIFF' or header[8:12] != b'WAVE':
                raise CatVoiceError('recording_unavailable')
        except CatVoiceError:
            raise
        except (OSError, ValueError):
            raise CatVoiceError('recording_unavailable') from None
        return value

    def stop(self):
        with self._lock:
            process, mode = self._process, self._mode
            if process is None or process.poll() is not None:
                return {'status': 'idle'}
            self._cancelled = process
            if mode == 'record' and process.stdin is not None:
                try:
                    process.stdin.write('stop\n')
                    process.stdin.flush()
                    return {'status': 'stopping'}
                except (OSError, ValueError):
                    pass
            try:
                process.terminate()
            except OSError:
                pass
            return {'status': 'stopping'}
