"""Finite offline Vosk worker. Reads WAV bytes, never a path or a microphone.

The application supplies only a fixed local model. There is deliberately no
download, network, shell, model discovery or Python Vosk package dependency.
"""
from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import wave

from activity_core import no_links

APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
if not getattr(sys, 'frozen', False) and APP_ROOT.name == 'src':
    APP_ROOT = APP_ROOT.parent
MAX_WAV = 15 * 48000 * 2 + 4096
MAX_TEXT = 4000
ERRORS = frozenset(('model_missing', 'assets_invalid', 'invalid_audio', 'asr_unavailable', 'no_speech'))


class SpeechFailure(RuntimeError):
    def __init__(self, code='asr_unavailable'):
        self.code = code if code in ERRORS else 'asr_unavailable'
        super().__init__(self.code)


def local_assets(root, *, verify_hashes=False):
    """Inspect fixed assets only; does not load a DLL or activate a device."""
    folder = Path(root).absolute() / 'assets' / 'speech'
    manifest_path = folder / 'manifest.json'
    if not manifest_path.is_file():
        raise SpeechFailure('model_missing')
    try:
        no_links(manifest_path, True)
        if manifest_path.stat().st_size > 512 * 1024:
            raise ValueError()
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if (manifest.get('schema') != 1 or manifest.get('engine') != 'vosk'
                or manifest.get('version') != '0.3.45' or manifest.get('language') != 'ru'
                or manifest.get('model') != 'vosk-model-small-ru-0.22'
                or not isinstance(manifest.get('files'), dict) or not 2 <= len(manifest['files']) <= 1000):
            raise ValueError()
        total = 0
        for name, specification in manifest['files'].items():
            parts = PurePosixPath(name).parts
            if (not parts or parts[0] not in ('native', 'model', 'licenses') or len(parts) < 2
                    or any(not re.fullmatch(r'[A-Za-z0-9_+.\-]+', part) or part in ('.', '..') for part in parts)
                    or not isinstance(specification, dict) or set(specification) != {'size', 'sha256'}
                    or type(specification['size']) is not int or not 0 <= specification['size'] <= 100 * 1024**2
                    or not isinstance(specification['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', specification['sha256'])):
                raise ValueError()
            path = folder.joinpath(*parts)
            no_links(path, True)
            if path.stat().st_size != specification['size']:
                raise ValueError()
            total += specification['size']
            if total > 320 * 1024**2:
                raise ValueError()
            if verify_hashes:
                with path.open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != specification['sha256']:
                        raise ValueError()
        required = {'native/libvosk.dll', 'model/am/final.mdl', 'model/conf/mfcc.conf'}
        if not required <= set(manifest['files']):
            raise ValueError()
        # Search-directory DLLs must all belong to the reviewed manifest.
        native = folder / 'native'
        for path in native.iterdir():
            no_links(path, True)
            if 'native/' + path.name not in manifest['files'] or path.suffix.lower() != '.dll':
                raise ValueError()
        return folder
    except SpeechFailure:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise SpeechFailure('assets_invalid') from None


def decode_wave(payload):
    if not isinstance(payload, bytes) or not 46 <= len(payload) <= MAX_WAV:
        raise SpeechFailure('invalid_audio')
    if payload[:4] != b'RIFF' or payload[8:12] != b'WAVE' or int.from_bytes(payload[4:8], 'little') != len(payload) - 8:
        raise SpeechFailure('invalid_audio')
    try:
        with wave.open(io.BytesIO(payload), 'rb') as source:
            rate, frames = source.getframerate(), source.getnframes()
            if (source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getcomptype() != 'NONE'
                    or not 8000 <= rate <= 48000 or not rate // 10 <= frames <= rate * 15):
                raise SpeechFailure('invalid_audio')
            pcm = source.readframes(frames + 1)
            if len(pcm) != frames * 2:
                raise SpeechFailure('invalid_audio')
        return rate, pcm
    except SpeechFailure:
        raise
    except (wave.Error, EOFError, OSError, ValueError, OverflowError):
        raise SpeechFailure('invalid_audio') from None


def _text(raw):
    try:
        if not raw or len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw.decode('utf-8'))
        text = value['text']
        if not isinstance(text, str) or len(text) > MAX_TEXT or any(ord(c) < 32 for c in text):
            raise ValueError()
        return text.strip()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise SpeechFailure() from None


def transcribe_bytes(payload, root=APP_ROOT):
    rate, pcm = decode_wave(payload)
    folder = local_assets(root, verify_hashes=True)
    if os.name != 'nt':
        raise SpeechFailure()
    model = recognizer = None
    try:
        # These settings are scoped to this disposable worker, not the desktop.
        os.environ['OPENBLAS_NUM_THREADS'] = '1'
        native = folder / 'native'
        with os.add_dll_directory(str(native)):
            library = ctypes.CDLL(str(native / 'libvosk.dll'), winmode=0x1100)
        library.vosk_set_log_level.argtypes = [ctypes.c_int]
        library.vosk_set_log_level.restype = None
        library.vosk_model_new.argtypes = [ctypes.c_char_p]
        library.vosk_model_new.restype = ctypes.c_void_p
        library.vosk_model_free.argtypes = [ctypes.c_void_p]
        library.vosk_model_free.restype = None
        library.vosk_recognizer_new.argtypes = [ctypes.c_void_p, ctypes.c_float]
        library.vosk_recognizer_new.restype = ctypes.c_void_p
        library.vosk_recognizer_free.argtypes = [ctypes.c_void_p]
        library.vosk_recognizer_free.restype = None
        library.vosk_recognizer_accept_waveform.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        library.vosk_recognizer_accept_waveform.restype = ctypes.c_int
        library.vosk_recognizer_result.argtypes = [ctypes.c_void_p]
        library.vosk_recognizer_result.restype = ctypes.c_char_p
        library.vosk_recognizer_final_result.argtypes = [ctypes.c_void_p]
        library.vosk_recognizer_final_result.restype = ctypes.c_char_p
        library.vosk_set_log_level(-1)
        # An explicit local path bypasses all Vosk model-name/download helpers.
        model = library.vosk_model_new(str(folder / 'model').encode('utf-8'))
        if not model:
            raise SpeechFailure()
        recognizer = library.vosk_recognizer_new(model, rate)
        if not recognizer:
            raise SpeechFailure()
        phrases = []
        for offset in range(0, len(pcm), 8000):
            chunk = pcm[offset:offset + 8000]
            accepted = library.vosk_recognizer_accept_waveform(recognizer, chunk, len(chunk))
            if accepted < 0:
                raise SpeechFailure()
            if accepted:
                phrases.append(_text(library.vosk_recognizer_result(recognizer)))
        phrases.append(_text(library.vosk_recognizer_final_result(recognizer)))
        text = ' '.join(part for part in phrases if part).strip()
        if not text:
            raise SpeechFailure('no_speech')
        if len(text) > MAX_TEXT:
            raise SpeechFailure()
        return {'status': 'transcribed', 'text': text, 'language': 'ru', 'local': True}
    except SpeechFailure:
        raise
    except (OSError, ValueError, AttributeError, TypeError):
        raise SpeechFailure() from None
    finally:
        if recognizer:
            library.vosk_recognizer_free(recognizer)
        if model:
            library.vosk_model_free(model)


def main():
    try:
        payload = sys.stdin.buffer.read(MAX_WAV + 1)
        receipt = transcribe_bytes(payload)
        code = 0
    except SpeechFailure as error:
        receipt, code = {'status': 'error', 'code': error.code}, 2
    except Exception:
        receipt, code = {'status': 'error', 'code': 'asr_unavailable'}, 3
    sys.stdout.write(json.dumps(receipt, ensure_ascii=True) + '\n')
    sys.stdout.flush()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
