"""Exercise the actual package in an isolated workspace profile. No history collection."""
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
import queue
import re
import runpy
from pathlib import Path, PurePosixPath
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave
import zipfile

PRODUCT = Path(__file__).resolve().parents[1]
RELEASE = PRODUCT / 'releases'
VERSION = runpy.run_path(str(PRODUCT / 'src' / 'version.py'))['VERSION']
SETUP = RELEASE / f'Prostranstvo-Local-Beta-{VERSION}-Setup.exe'
PORTABLE = RELEASE / f'Prostranstvo-Local-Beta-{VERSION}-Portable.zip'
SOURCE = RELEASE / f'Prostranstvo-Local-Beta-{VERSION}-Source.zip'
FIXTURE = PRODUCT / 'build' / ('release-check-' + time.strftime('%Y%m%d-%H%M%S'))
ENV = dict(os.environ, LOCALAPPDATA=str(FIXTURE / 'LocalAppData'), APPDATA=str(FIXTURE / 'Roaming'), PYTHONPATH='', PYTHONHOME='')
# Deliberately no Python in the child's PATH. Windows components remain available.
ENV['PATH'] = os.path.join(os.environ['SystemRoot'], 'System32') + ';' + os.environ['SystemRoot']

def clean_archive_names(names):
    """Both distributions exclude personal state and unreviewed raw datasets."""
    assert len(names) == len(set(names)), 'Duplicate ZIP entry'
    banned_names = ('telegram-config.json', 'api-settings.json', 'api_settings.json', 'python-path.txt', 'queue.sqlite',
                    'activity.sqlite', 'history-archive.sqlite', 'user-file-index.sqlite', 'codex-config-backup',
                    'child-profile.json', 'family-profile.json', 'family-context.sqlite', 'cat-notifications.sqlite',
                    'dialogue-history.sqlite', 'editor-session.json', 'server-session.json', 'command.json',
                    'preview-status.json', 'rules.json')
    private_suffixes = ('.sqlite', '.sqlite-wal', '.sqlite-shm', '.sqlite-journal', '.log', '.wav', '.partial')
    raw_suffixes = ('.jsonl', '.jsonl.gz', '.jsonl.bz2', '.parquet', '.arrow')
    banned_parts = {'data', 'logs', 'profile', 'private', 'userdata', 'cat-companion', 'family-messages',
                    'research', 'raw', 'rawcorpus', 'raw-corpus', 'raw_corpus', 'dataset', 'datasets'}
    raw_dataset_names = ('blended_skill_talk', 'blended-skill-talk', 'oasst', 'openassistant', 'open-assistant')
    for name in names:
        normalized = name.replace('\\', '/').lower()
        parts = PurePosixPath(normalized).parts
        assert parts and not PurePosixPath(normalized).is_absolute() and ':' not in normalized
        assert not any(part in ('.', '..') for part in parts), 'Unsafe ZIP path'
        assert not any(banned in normalized for banned in banned_names), 'Private state in archive'
        assert not any(part in banned_parts for part in parts), 'Private/research directory in archive'
        assert not normalized.endswith(private_suffixes + raw_suffixes), 'Private/raw file in archive'
        assert not any(token in parts[-1] for token in raw_dataset_names), 'Raw dataset in archive'


def approved_knowledge_check(archive):
    """Validate the packaged original cards independently of runtime imports."""
    name = 'assets/cat-knowledge-approved-v1.json'
    assert name in archive.namelist(), 'Approved kitten knowledge missing'
    assert 1 <= archive.getinfo(name).file_size <= 1024 * 1024, 'Unexpected approved knowledge size'
    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            assert key not in value, 'Duplicate approved knowledge JSON field'
            value[key] = item
        return value
    def finite_json(_):
        raise AssertionError('Nonfinite approved knowledge JSON')
    body = archive.read(name)
    pack = json.loads(body.decode('utf-8'), object_pairs_hook=unique_fields, parse_constant=finite_json)
    assert type(pack) is dict and set(pack) == {'version', 'pack_id', 'language', 'origin', 'approved',
                                               'source_refs', 'dataset_research_refs', 'records'}
    assert type(pack['version']) is int and pack['version'] == 1
    assert pack['pack_id'] == 'cat-knowledge-approved-v1' and pack['language'] == 'ru'
    assert pack['origin'] == 'original_project' and pack['approved'] is True
    references = pack['source_refs']
    assert type(references) is list and 1 <= len(references) <= 10
    research = pack['dataset_research_refs']
    assert type(research) is list and len(research) <= 6
    reference_ids = set()
    for reference in references + research:
        assert type(reference) is dict
        assert type(reference['id']) is str and re.fullmatch(r'[a-z][a-z0-9_]{1,63}', reference['id'])
        assert reference['id'] not in reference_ids
        reference_ids.add(reference['id'])
        assert type(reference['url']) is str and 1 <= len(reference['url']) <= 500
        parsed = urllib.parse.urlsplit(reference['url'])
        assert parsed.scheme == 'https' and parsed.hostname and parsed.username is None and parsed.password is None
        assert not parsed.query and not parsed.fragment
    for reference in references:
        assert set(reference) == {'id', 'url', 'role'}
        assert reference['role'] == 'principle_reference_not_quoted'
    for reference in research:
        assert set(reference) == {'id', 'url', 'status', 'runtime_approved', 'approval'}
        assert reference['status'] == 'research_only' and reference['runtime_approved'] is False
        assert reference['approval'] == 'RAW_NOT_APPROVED'
    records = pack['records']
    assert type(records) is list and len(records) == 18, 'Expected 18 reviewed original cards'
    record_ids = set()
    for row in records:
        assert type(row) is dict and set(row) == {'id', 'text', 'keywords', 'source', 'approved'}
        assert type(row['id']) is str and re.fullmatch(r'[a-z][a-z0-9_]{1,63}', row['id'])
        assert row['id'] not in record_ids
        record_ids.add(row['id'])
        assert type(row['text']) is str and row['text'].strip() == row['text'] and 1 <= len(row['text']) <= 700
        assert not any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in row['text'])
        assert row['source'] == 'original_project' and row['approved'] is True
        assert type(row['keywords']) is list and 1 <= len(row['keywords']) <= 20
        assert all(type(keyword) is str and keyword.strip() == keyword and 1 <= len(keyword) <= 80
                   for keyword in row['keywords'])
        assert len(row['keywords']) == len(set(row['keywords']))
    return {'version': 1, 'approved_cards': len(records), 'origin': 'original_project',
            'all_approved': True, 'raw_research_not_approved': True, 'sha256': hashlib.sha256(body).hexdigest()}


def run(command, stdin=None, seconds=60):
    result = subprocess.run([str(x) for x in command], input=stdin, capture_output=True,
                            encoding='utf-8', env=ENV, timeout=seconds)
    if result.returncode:
        raise AssertionError((result.returncode, result.stdout[-1000:], result.stderr[-1000:]))
    return result.stdout.strip().lstrip('\ufeff')

def synthetic_asr_check(core, voice_helper):
    """Known Russian speech in RAM -> actual frozen ASR; no audio devices."""
    source = FIXTURE / 'SyntheticRussianSpeech.cs'
    helper = FIXTURE / 'SyntheticRussianSpeech.exe'
    source.write_text(r'''using System;
using System.Reflection;
static class SyntheticRussianSpeech {
 [STAThread] public static int Main(string[] args) {
  if(args.Length!=1)return 2;
  var assembly=Assembly.LoadFrom(args[0]);
  var voice=assembly.GetType("ProstranstvoKittenVoice.RussianVoice",true);
  var render=voice.GetMethod("RenderWave",BindingFlags.Public|BindingFlags.Static);
  // RenderWave writes to SAPI's memory stream; SoundPlayer is never invoked.
  byte[] wave=(byte[])render.Invoke(null,new object[]{"Привет, котик! Я хочу поговорить. Как у тебя дела?"});
  using(var output=Console.OpenStandardOutput()){output.Write(wave,0,wave.Length);}
  return 0;
 }
}
''', encoding='utf-8')
    csc = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    run([csc, '/nologo', '/platform:x64', '/target:exe', '/codepage:65001', '/out:' + str(helper), source])
    generated = subprocess.run([str(helper), str(voice_helper)], stdin=None, capture_output=True,
                               env=ENV, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
    assert generated.returncode == 0 and not generated.stderr.strip(), (generated.returncode, generated.stderr[-1000:])
    payload = generated.stdout
    assert 46 <= len(payload) <= 15 * 48000 * 2 + 4096 and payload[:4] == b'RIFF' and payload[8:12] == b'WAVE'
    with wave.open(io.BytesIO(payload), 'rb') as audio:
        assert audio.getnchannels() == 1 and audio.getsampwidth() == 2 and audio.getcomptype() == 'NONE'
        assert audio.getframerate() == 22050 and 1 <= audio.getnframes() / audio.getframerate() <= 15
    started = time.monotonic()
    result = subprocess.run([str(core), 'speech-transcribe'], input=payload, capture_output=True,
                            env=ENV, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    elapsed = time.monotonic() - started
    assert result.returncode == 0 and not result.stderr.strip(), (result.returncode, result.stdout[-1000:], result.stderr[-1000:])
    assert len(result.stdout) <= 32768 and 0 < elapsed <= 60.5
    receipt = json.loads(result.stdout.decode('utf-8'))
    assert set(receipt) == {'status', 'text', 'language', 'local'}
    assert receipt['status'] == 'transcribed' and receipt['language'] == 'ru' and receipt['local'] is True
    assert isinstance(receipt['text'], str) and 1 <= len(receipt['text']) <= 4000
    words = set(receipt['text'].lower().split())
    assert {'привет', 'котик'} <= words and len(words & {'хочу', 'поговорить', 'как', 'тебя', 'дела'}) >= 3, receipt
    # WAV stays in memory; the installed profile must receive no recording.
    return {'tested': True, 'synthetic_phrase_only': True, 'engine': 'vosk', 'language': 'ru',
            'known_words_matched': True, 'elapsed_seconds': round(elapsed, 3), 'wav_bytes': len(payload),
            'microphone_opened': False, 'audio_played': False, 'runtime_python_on_path': False}


def tricks_editor_check(core):
    """Actual frozen loopback API in ENV's private fixture; no browser/native pet.

    The readiness capability is retained in memory and never included in this
    report, assertion messages or subprocess error output.
    """
    child = subprocess.Popen([str(core), 'tricks', '--serve-only', '--ready-stdout'],
                             env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    ready_lines = queue.Queue()
    def read_ready():
        try: ready_lines.put(child.stdout.readline(8193))
        except (OSError, ValueError): ready_lines.put(b'')
    reader = threading.Thread(target=read_ready, daemon=True, name='release-tricks-ready')
    reader.start()
    try:
        try: line = ready_lines.get(timeout=20)
        except queue.Empty: raise AssertionError('Local editor readiness timed out') from None
        assert 1 <= len(line) <= 8192, 'Invalid local editor readiness length'
        try: ready = json.loads(line.decode('utf-8').lstrip('\ufeff'))
        except (ValueError, UnicodeError): raise AssertionError('Invalid local editor readiness JSON') from None
        assert isinstance(ready, dict) and isinstance(ready.get('url'), str), 'Missing local editor URL'
        try: parsed = urllib.parse.urlsplit(ready['url'])
        except ValueError: raise AssertionError('Invalid local editor URL') from None
        assert parsed.scheme == 'http' and parsed.hostname == '127.0.0.1', 'Editor must bind IPv4 loopback'
        assert parsed.username is None and parsed.password is None and parsed.path == '/' and not parsed.query, 'Unexpected editor URL component'
        assert parsed.port is not None and 1024 <= parsed.port <= 65535 and ready.get('port') == parsed.port, 'Editor needs an ephemeral port'
        token = parsed.fragment
        assert re.fullmatch(r'[A-Za-z0-9_-]{43}', token), 'Invalid editor capability'
        assert ready.get('reused') is False, 'Fixture must own a new server'
        origin = 'http://127.0.0.1:' + str(parsed.port)
        # Explicitly bypass environment proxies for these loopback-only calls.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path, body=None, *, authorized=True, request_origin=None, host=None):
            headers = {}
            if authorized: headers['Authorization'] = 'Bearer ' + token
            if body is not None:
                headers['Content-Type'] = 'application/json'
                headers['Origin'] = origin if request_origin is None else request_origin
            if host is not None: headers['Host'] = host
            payload = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            message = urllib.request.Request(origin + path, data=payload, headers=headers, method='GET' if body is None else 'POST')
            try: response = opener.open(message, timeout=8)
            except urllib.error.HTTPError as error: response = error
            except (OSError, ValueError): raise AssertionError('Local editor request failed') from None
            with response:
                data = response.read(512 * 1024 + 1)
                assert len(data) <= 512 * 1024, 'Oversized editor response'
                return response.code, data
        def json_request(path, body=None, **kwargs):
            code, raw = request(path, body, **kwargs)
            try: value = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeError): raise AssertionError('Invalid editor API JSON') from None
            assert isinstance(value, dict), 'Editor API must return an object'
            return code, value
        code, page = request('/', authorized=False)
        assert code == 200 and token.encode('ascii') not in page
        for link in (b'/editor.js', b'/editor.css', b'/blockly/blockly_compressed.js', b'/blockly/msg/ru.js'):
            assert link in page, 'Missing local editor dependency'
        assert b'https://' not in page and b'http://' not in page, 'Editor HTML must use local assets only'
        code, denied = json_request('/api/state', authorized=False)
        assert code == 403 and denied.get('error') == 'authorization_refused'
        code, initial = json_request('/api/state')
        assert code == 200 and {'config', 'preview', 'native'} <= initial.keys()
        assert initial['config'] == {'version': 1, 'enabled': False, 'rules': []}, 'New fixture rules must be disabled and empty'
        assert initial['native'].get('available') is False, 'No native pet is started by this API test'
        synthetic = {'version': 1, 'enabled': False, 'rules': [{
            'id': '0123456789abcdef0123456789abcdef', 'name': 'Synthetic manual fixture',
            'enabled': False, 'event': 'manual', 'cooldown_seconds': 30,
            'steps': [{'action': 'blink', 'duration_ms': 500}, {'action': 'wait', 'duration_ms': 100}]}]}
        code, denied = json_request('/api/save', {'config': synthetic}, request_origin='https://example.invalid')
        assert code == 403 and denied.get('error') == 'origin_refused'
        code, unchanged = json_request('/api/state')
        assert code == 200 and unchanged['config'] == initial['config'], 'Rejected Origin must not change rules'
        code, denied = json_request('/api/state', host='example.invalid')
        assert code == 403 and denied.get('error') == 'host_refused'
        code, saved = json_request('/api/save', {'config': synthetic})
        assert code == 200 and saved['config'] == synthetic
        code, loaded = json_request('/api/state')
        assert code == 200 and loaded['config'] == synthetic
        code, unavailable = json_request('/api/preview', {'rule_id': synthetic['rules'][0]['id']})
        assert code == 503 and unavailable.get('error') == 'native_unavailable'
        code, stopped = json_request('/api/stop', {})
        assert code == 200 and stopped['command']['action'] == 'stop'
        assert stopped['preview']['state'] == 'queued' and stopped['preview']['action'] == 'stop', 'Without native acknowledgement Stop is only queued'
        assert stopped['config'] == synthetic and stopped['native'].get('available') is False
        code, closed = json_request('/api/close', {})
        assert code == 200 and closed.get('closed') is True
        try: child.wait(timeout=12)
        except subprocess.TimeoutExpired: raise AssertionError('Own editor server did not close') from None
        assert child.returncode == 0, 'Own editor server exited with an error'
        return {'tested': True, 'frozen_runtime': True, 'loopback_only': True, 'ephemeral_port': True,
                'authorization_required': True, 'origin_and_host_required': True, 'local_assets': True,
                'canonical_disabled_config_roundtrip': True, 'no_native_false_ack': True,
                'stop_queued_without_native': True, 'own_server_closed': True,
                'browser_opened': False, 'microphone_opened': False, 'audio_played': False}
    finally:
        # Retain and terminate only this test-created child, never a discovered
        # PID, browser, desktop pet, service or another editor instance.
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill();child.wait(timeout=3)
        for stream in (child.stdout, child.stderr):
            if stream is not None: stream.close()
        reader.join(timeout=1)

def gui_check(core, mode='ui', title='Пространство · Local Beta'):
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    expected_title = title
    with subprocess.Popen([str(core), mode], env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8') as child:
        window = None
        deadline = time.monotonic() + 20
        while child.poll() is None and time.monotonic() < deadline:
            found = []
            @callback_type
            def check(hwnd, _):
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == child.pid:
                    title = ctypes.create_unicode_buffer(256)
                    user.GetWindowTextW(hwnd, title, len(title))
                    if title.value == expected_title:
                        found.append(hwnd)
                return True
            user.EnumWindows(check, 0)
            if found:
                window = found[0]
                break
            time.sleep(.1)
        if window:
            # Give tab background tasks time to run once, then close only our child.
            time.sleep(2)
            user.PostMessageW(window, 0x10, 0, 0)
        else:
            child.terminate()  # Only this test-created process, never another app.
        stdout, stderr = child.communicate(timeout=20)
        assert window and child.returncode == 0, (child.returncode, stdout, stderr)
        assert not stderr.strip(), stderr
    return True

def main():
    FIXTURE.mkdir(parents=True)
    report = {'product': 'prostranstvo-local-beta', 'version': VERSION, 'checks': {}}
    checks = report['checks']
    with zipfile.ZipFile(PORTABLE) as archive:
        names = archive.namelist()
        clean_archive_names(names)
        assert 'CAT-AI-RU.txt' in names and 1 <= archive.getinfo('CAT-AI-RU.txt').file_size <= 100 * 1024
        manifest = json.loads(archive.read('manifest.json'))
        assert set(names) == set(manifest['files']) | {'manifest.json'}
        for name, spec in manifest['files'].items():
            body = archive.read(name)
            assert len(body) == spec['size'] and hashlib.sha256(body).hexdigest() == spec['sha256'], name
        # A clean installer carries code and the reviewed public corpus, never
        # local recordings, message/profile databases, logs or partial captures.
        checks['approved_knowledge_asset'] = approved_knowledge_check(archive)
        corpus_name = 'assets/cat-dialogues-ru-v1.json'
        assert corpus_name in names, 'Russian dialogue corpus missing'
        corpus = json.loads(archive.read(corpus_name))
        assert corpus['version'] == 1 and corpus['language'] == 'ru'
        assert corpus['age_range'] == [10, 12] and isinstance(corpus['intents'], list) and len(corpus['intents']) >= 20
        assert all(isinstance(intent, dict) and isinstance(intent.get('responses'), list) and intent['responses']
                   and all(isinstance(text, str) and text.strip() for text in intent['responses']) for intent in corpus['intents'])
        checks['russian_dialogue_asset'] = {'language': 'ru', 'intents': len(corpus['intents']), 'version': corpus['version']}
        editor_assets = {'assets/tricks/index.html', 'assets/tricks/editor.js', 'assets/tricks/editor.css'}
        assert {name for name in names if name.startswith('assets/tricks/')} == editor_assets, 'Offline editor assets missing or unexpected'
        editor_html = archive.read('assets/tricks/index.html')
        for dependency in (b'/editor.js', b'/editor.css', b'/blockly/blockly_compressed.js', b'/blockly/msg/ru.js'):
            assert dependency in editor_html, 'Missing packaged local editor dependency'
        assert b'https://' not in editor_html and b'http://' not in editor_html
        blockly_prefix = 'assets/blockly/'
        blockly_manifest_name = blockly_prefix + 'asset-manifest.json'
        assert blockly_manifest_name in names, 'Pinned Blockly asset receipt missing'
        blockly = json.loads(archive.read(blockly_manifest_name))
        assert blockly['version'] == 1 and blockly['package'] == 'blockly' and blockly['package_version'] == '13.4.0'
        assert blockly['license'] == 'Apache-2.0' and isinstance(blockly['files'], dict) and 3 <= len(blockly['files']) <= 128
        assert blockly['source'] == 'https://registry.npmjs.org/blockly/-/blockly-13.4.0.tgz'
        assert blockly['archive_sha256'] == '9c191ff35384acdc334cfd3757aeafcf4c74e3021b3a10046346338ec7fd1f9c'
        assert blockly['integrity'] == 'sha512-cWZJV5nocL+VWOG9Vfm6j8shdOLGyvKkyoGOS2bk9u6PKb4qqLp50NeL8YoNMTEi7fHmY37eBYT1yC6Y7/yZ7w=='
        assert {'LICENSE', 'blockly_compressed.js', 'msg/ru.js'} <= set(blockly['files'])
        for relative, spec in blockly['files'].items():
            parts = PurePosixPath(relative).parts
            assert parts and len(parts) <= 3 and not PurePosixPath(relative).is_absolute()
            assert all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) and part not in ('.', '..') for part in parts)
            assert '\\' not in relative and ':' not in relative
            assert isinstance(spec, dict) and set(spec) == {'size', 'sha256'}
            assert type(spec['size']) is int and 0 <= spec['size'] <= 5 * 1024**2
            assert isinstance(spec['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', spec['sha256'])
            body = archive.read(blockly_prefix + relative)
            assert len(body) == spec['size'] and hashlib.sha256(body).hexdigest() == spec['sha256'], 'Blockly asset hash mismatch'
        assert {name for name in names if name.startswith(blockly_prefix)} == {blockly_manifest_name} | {blockly_prefix + relative for relative in blockly['files']}
        checks['offline_blockly_assets_verified'] = {'package': 'blockly', 'version': blockly['package_version'],
                                                    'license': blockly['license'], 'files': len(blockly['files']), 'editor_files': len(editor_assets)}
        speech_prefix = 'assets/speech/'
        speech_manifest_name = speech_prefix + 'manifest.json'
        assert speech_manifest_name in names, 'Offline speech asset receipt missing'
        speech = json.loads(archive.read(speech_manifest_name))
        assert speech['schema'] == 1 and speech['engine'] == 'vosk' and speech['version'] == '0.3.45'
        assert speech['language'] == 'ru' and speech['model'] == 'vosk-model-small-ru-0.22' and speech['runtime_network'] is False
        assert speech['license'] == 'Apache-2.0' and isinstance(speech['files'], dict)
        required_speech = {'native/libvosk.dll', 'native/libgcc_s_seh-1.dll', 'native/libstdc++-6.dll', 'native/libwinpthread-1.dll',
                           'model/am/final.mdl', 'model/conf/mfcc.conf', 'licenses/Vosk-Apache-2.0.txt',
                           'licenses/GCC-GPL-3.0.txt', 'licenses/GCC-Runtime-Exception-3.1.txt', 'licenses/MinGW-winpthreads-COPYING.txt'}
        assert required_speech <= set(speech['files'])
        assert speech['sources']['wheel']['sha256'] == '6994ddc68556c7e5730c3b6f6bad13320e3519b13ce3ed2aa25a86724e7c10ac'
        assert speech['sources']['wheel']['published_sha256_verified'] is True
        assert speech['sources']['model']['sha256'] == '961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4'
        assert 10 <= len(speech['files']) <= 1000
        for relative, spec in speech['files'].items():
            parts = PurePosixPath(relative).parts
            assert parts and len(parts) >= 2 and parts[0] in ('native', 'model', 'licenses')
            assert not PurePosixPath(relative).is_absolute() and all(part not in ('.', '..') for part in parts)
            assert '\\' not in relative and ':' not in relative
            body = archive.read(speech_prefix + relative)
            assert set(spec) == {'size', 'sha256'} and len(body) == spec['size'] and hashlib.sha256(body).hexdigest() == spec['sha256']
        assert {n for n in names if n.startswith(speech_prefix)} == {speech_manifest_name} | {speech_prefix + n for n in speech['files']}
        checks['offline_speech_assets_verified'] = {'engine': 'vosk', 'language': 'ru', 'model': speech['model'], 'files': len(speech['files'])}
        images = {}
        for name in ('kitten-sprites-v2.png', 'kitten-portrait-v2.png'):
            asset = 'assets/' + name
            assert asset in names, 'Detailed kitten image missing: ' + name
            header = archive.read(asset)[:24]
            assert len(header) == 24 and header[:8] == b'\x89PNG\r\n\x1a\n' and header[12:16] == b'IHDR'
            width, height = int.from_bytes(header[16:20], 'big'), int.from_bytes(header[20:24], 'big')
            assert width >= 512 and height >= 512
            if name == 'kitten-sprites-v2.png':
                assert width == height and 1024 <= width <= 4096
            images[name] = {'width': width, 'height': height}
        checks['detailed_kitten_assets_v2_bundled'] = images
        owner = str(Path.home())
        # Search all uncompressed payload files, including embedded bytecode.
        for name in names:
            body = archive.read(name)
            assert owner.encode() not in body and owner.encode('utf-16-le') not in body, 'owner path in ' + name
        checks['manifest_and_clean_payload'] = len(manifest['files'])
    with zipfile.ZipFile(SOURCE) as archive:
        names = archive.namelist()
        clean_archive_names(names)
        required = {'src/cat_api.py', 'src/cat_brain.py', 'src/cat_knowledge.py', 'src/cat_ai_ui.py',
                    'docs/CAT-AI-RU.txt', 'assets/cat-knowledge-approved-v1.json'}
        assert required <= set(names), 'AI modules/instruction/approved knowledge missing from source archive'
        assert 1 <= archive.getinfo('docs/CAT-AI-RU.txt').file_size <= 100 * 1024
        source_knowledge = approved_knowledge_check(archive)
        assert source_knowledge == checks['approved_knowledge_asset'], 'Source and portable approved knowledge differ'
        checks['source_archive_clean_and_approved_knowledge'] = {'files': len(names), 'private_state_absent': True,
                                                               'raw_datasets_absent': True, 'approved_cards': 18}
    checks['installer_self_test'] = json.loads(run([SETUP, '--self-test']))
    checks['payload_inspect'] = json.loads(run([SETUP, '--inspect-payload']))
    previous = RELEASE / 'Prostranstvo-Local-Beta-0.1.0-Setup.exe'
    previous_sentinel = None
    if VERSION != '0.1.0' and previous.exists():
        old = json.loads(run([previous, '--install']))
        previous_sentinel = Path(old['data']) / 'previous-version-fixture.txt'
        previous_sentinel.parent.mkdir(parents=True, exist_ok=True)
        previous_sentinel.write_text('synthetic previous version data', encoding='utf-8')
    installed = json.loads(run([SETUP, '--install']))
    if previous_sentinel is not None:
        assert previous_sentinel.read_text(encoding='utf-8') == 'synthetic previous version data'
        assert Path(installed['backup']).is_dir()
        checks['upgrade_from_0_1_0_preserves_data_and_code_backup'] = True
    app = Path(installed['app'])
    assert app == Path(ENV['LOCALAPPDATA']) / 'Programs' / 'ProstranstvoLocalBeta' / 'app'
    core = app / 'ProstranstvoCore.exe'
    checks['clean_install'] = True
    status = json.loads(run([core, 'status']))
    assert status['data'] == str(Path(ENV['LOCALAPPDATA']) / 'ProstranstvoLocalBeta' / 'data')
    assert not status['status']['system_service']['available'] and not status['status']['catalog_exists']
    checks['frozen_runtime_without_python_on_path'] = True
    checks['tk_and_imports'] = json.loads(run([core, 'self-test']))
    assert checks['tk_and_imports']['cat_ai'] == {'offline_default': True, 'approved_cards': 18,
                                               'session_name': True, 'no_network_calls': True}
    assert type(checks['tk_and_imports']['cat_ai']['approved_cards']) is int
    assert all(checks['tk_and_imports']['cat_ai'][name] is True
               for name in ('offline_default', 'session_name', 'no_network_calls'))
    voice_helper = app / 'KittenVoice.exe'
    assert voice_helper.is_file(), 'Packaged KittenVoice.exe missing'
    voice_data = Path(status['data']) / 'cat-companion' / 'voice'
    assert not voice_data.exists()
    voice_test = json.loads(run([voice_helper, '--self-test']))
    assert voice_test['status'] == 'self_test_ok' and voice_test['checks'] >= 16
    assert voice_test['microphone_opened'] is False and voice_test['audio_played'] is False and voice_test['network'] is False
    checks['voice_helper_pure_self_test'] = voice_test
    voice_status = json.loads(run([voice_helper, '--status']))
    assert set(voice_status) == {'status', 'russian_tts_available', 'microphone_devices', 'asr_available', 'microphone_opened', 'audio_played'}
    assert voice_status['status'] == 'ok' and type(voice_status['russian_tts_available']) is bool
    assert type(voice_status['microphone_devices']) is int and 0 <= voice_status['microphone_devices'] <= 128
    assert voice_status['asr_available'] is False and voice_status['microphone_opened'] is False and voice_status['audio_played'] is False
    assert not voice_data.exists(), 'Capability inquiry must not create recording storage'
    checks['voice_helper_status_without_audio_or_recording'] = voice_status
    if voice_status['russian_tts_available']:
        checks['frozen_offline_russian_asr'] = synthetic_asr_check(core, voice_helper)
    else:
        # ASR model/runtime are still mandatory and verified above. A machine
        # lacking Russian TTS cannot generate this independent speech fixture.
        checks['frozen_offline_russian_asr'] = {'tested': False, 'reason': 'russian_tts_unavailable',
                                               'microphone_opened': False, 'audio_played': False}
    assert not voice_data.exists(), 'Synthetic ASR must not create microphone recording storage'
    checks['frozen_offline_tricks_editor_api'] = tricks_editor_check(core)
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'computer_status', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'catalog_search', 'arguments': {'query': 'empty'}}},
    ]
    responses = [json.loads(line) for line in run([core, 'mcp'], '\n'.join(json.dumps(x) for x in requests) + '\n').splitlines()]
    assert [x['id'] for x in responses] == [1, 2, 3, 4]
    assert len(responses[1]['result']['tools']) == 7
    assert json.loads(responses[3]['result']['content'][0]['text']) == []
    checks['stdio_mcp_initialize_list_status_catalog'] = True
    family_requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'family_channel_status', 'arguments': {}}},
    ]
    family = [json.loads(line) for line in run([core, 'family-mcp'], '\n'.join(json.dumps(x) for x in family_requests) + '\n').splitlines()]
    assert len(family[1]['result']['tools']) == 3
    channel = json.loads(family[2]['result']['content'][0]['text'])
    assert channel['state'] == 'not_configured' and not channel['channel_ready']
    checks['family_mcp_status_unconfigured_without_delivery'] = True
    run([app / 'Prostranstvo.exe', '--study-resume'])
    assert not (Path(status['data']) / 'study' / 'active-session.json').exists()
    checks['study_resume_launcher_exits_without_active_lease'] = True
    checks['whole_app_open_close'] = gui_check(core)
    if (app / 'KittenMessenger.exe').exists():
        checks['kitten_chat_open_close'] = gui_check(core, 'kitten', 'Мурчик · Написать папе')
        checks['native_pet_self_test'] = json.loads(run([app / 'NativeDesktopPet.exe', '--self-test']))
        assert (app / 'assets' / 'kitten-sprites-v1.png').is_file()
        assert (app / 'assets' / 'kitten-sprites-v2.png').is_file() and (app / 'assets' / 'kitten-portrait-v2.png').is_file()
        checks['kitten_asset_bundled'] = True
    data = Path(status['data'])
    assert not json.loads((data / 'activity' / 'config.json').read_text())['enabled']
    assert not (data / 'family-messages' / 'telegram-config.json').exists()
    assert not (data / 'browser-history' / 'schedule-config.json').exists()
    assert json.loads((data / 'cat-companion' / 'tricks' / 'rules.json').read_text(encoding='utf-8'))['enabled'] is False
    checks['features_disabled_after_first_open'] = True
    assert not voice_data.exists(), 'Opening the app/chat must never start or save a microphone recording'
    checks['no_voice_recording_after_gui_open'] = True
    sentinel = data / 'preserve-fixture.txt'
    sentinel.write_text('synthetic data; preserve on update', encoding='utf-8')
    updated = json.loads(run([SETUP, '--install']))
    assert sentinel.read_text(encoding='utf-8') == 'synthetic data; preserve on update'
    assert Path(updated['backup']).is_dir()
    checks['update_preserves_data_and_old_app_backup'] = True
    report['clean_windows_vm_tested'] = False
    report['network_delivery_or_telegram_tested'] = False
    (PRODUCT / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
