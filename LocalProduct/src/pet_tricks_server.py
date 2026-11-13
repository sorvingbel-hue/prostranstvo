"""Explicit local-only Blockly editor. No server/browser is started on import."""
from __future__ import annotations
import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
import urllib.request
import uuid
import webbrowser

from activity_core import no_links
from pet_tricks import FileLock, PetTrickError, PetTrickStore, parse_json

APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
if not getattr(sys, 'frozen', False) and APP_ROOT.name == 'src':
    APP_ROOT = APP_ROOT.parent
MAX_BODY = 40 * 1024
TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{43}\Z')


def _asset_bytes(path, maximum):
    no_links(path, True)
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum: raise PetTrickError('asset_too_large')
    return data


def load_assets(root):
    root = Path(root).absolute()
    values = {}
    for route, name in (('/', 'index.html'), ('/editor.js', 'editor.js'), ('/editor.css', 'editor.css')):
        path = root / 'tricks' / name
        values[route] = (_asset_bytes(path, 512 * 1024), mimetypes.guess_type(name)[0] or 'application/octet-stream')
    vendor = root / 'blockly'
    manifest = parse_json(_asset_bytes(vendor / 'asset-manifest.json', 64 * 1024).decode('utf-8-sig'))
    if (not isinstance(manifest, dict) or manifest.get('version') != 1 or manifest.get('package') != 'blockly'
            or manifest.get('package_version') != '13.4.0' or manifest.get('license') != 'Apache-2.0'
            or not isinstance(manifest.get('files'), dict) or not 1 <= len(manifest['files']) <= 128):
        raise PetTrickError('asset_manifest_invalid')
    total = 0
    for name, spec in manifest['files'].items():
        p = PurePosixPath(name)
        if (p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name
                or len(p.parts) > 3 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in p.parts)
                or not isinstance(spec, dict) or set(spec) != {'size', 'sha256'}
                or type(spec['size']) is not int or not 0 <= spec['size'] <= 5 * 1024**2
                or not isinstance(spec['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', spec['sha256'])):
            raise PetTrickError('asset_manifest_invalid')
        data = _asset_bytes(vendor.joinpath(*p.parts), 5 * 1024**2)
        total += len(data)
        if total > 8 * 1024**2 or len(data) != spec['size'] or hashlib.sha256(data).hexdigest() != spec['sha256']:
            raise PetTrickError('asset_integrity_failed')
        values['/blockly/' + name] = (data, mimetypes.guess_type(name)[0] or 'application/octet-stream')
    if not {'/blockly/blockly_compressed.js', '/blockly/msg/ru.js'} <= values.keys():
        raise PetTrickError('asset_manifest_invalid')
    return values


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    def __init__(self, store=None, asset_root=None, *, idle_seconds=1800):
        self.store = store if store is not None else PetTrickStore()
        self.assets = load_assets(asset_root if asset_root is not None else APP_ROOT / 'assets')
        self.token = secrets.token_urlsafe(32)
        self.instance_id = uuid.uuid4().hex
        self.last_activity = time.monotonic()
        self.idle_seconds = idle_seconds
        self.closed = threading.Event()
        super().__init__(('127.0.0.1', 0), EditorHandler)
        self.origin = 'http://127.0.0.1:' + str(self.server_address[1])
        self.url = self.origin + '/#' + self.token

    def activity(self): self.last_activity = time.monotonic()

    def stop_async(self):
        if self.closed.is_set(): return
        self.closed.set()
        threading.Thread(target=self.shutdown, daemon=True, name='Tricks-stop').start()

    def watch(self):
        next_session = 0
        while not self.closed.wait(.5):
            if time.monotonic() - self.last_activity >= self.idle_seconds:
                self.stop_async(); return
            if time.monotonic() >= next_session:
                try: self.write_session()
                except (OSError, ValueError): pass
                next_session = time.monotonic() + 2

    def write_session(self):
        with self.store.lock():
            self.store._atomic('server-session.json', {'version': 1, 'port': self.server_address[1],
                'token': self.token, 'pid': os.getpid(), 'updated_utc': int(time.time()), 'instance_id': self.instance_id})

    def clear_session(self):
        try:
            with self.store.lock():
                value = self.store._read('server-session.json')
                if isinstance(value, dict) and value.get('instance_id') == self.instance_id:
                    target = self.store.root / 'server-session.json'
                    no_links(target, True); target.unlink()
        except (OSError, ValueError): pass

    def serve(self):
        self.write_session()
        watcher = threading.Thread(target=self.watch, daemon=True, name='Tricks-idle')
        watcher.start()
        try: self.serve_forever(poll_interval=.2)
        finally:
            self.closed.set(); watcher.join(timeout=2); self.server_close(); self.clear_session()


class EditorHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'LocalEditor'
    sys_version = ''
    def log_message(self, *_): pass
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def _headers(self, code, data, content_type):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def _json(self, value, code=200):
        self._headers(code, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'), 'application/json; charset=utf-8')

    def _reject(self, code, reason):
        self.close_connection = True
        self._json({'error': reason}, code)

    def _gate(self, *, api=False, post=False):
        if self.headers.get('Host') != self.server.origin[7:]:
            self._reject(403, 'host_refused'); return False
        origin = self.headers.get('Origin')
        if (post and origin != self.server.origin) or (origin is not None and origin != self.server.origin):
            self._reject(403, 'origin_refused'); return False
        if api:
            auth = self.headers.get('Authorization', '')
            bearer = auth[7:] if auth.startswith('Bearer ') else ''
            other = self.headers.get('X-Pet-Tricks-Token', '')
            if not (hmac.compare_digest(bearer, self.server.token) or hmac.compare_digest(other, self.server.token)):
                self._reject(403, 'authorization_refused'); return False
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '%' in parsed.path:
            self._reject(400, 'path_refused'); return False
        return True

    def do_GET(self):
        api = self.path.startswith('/api/')
        if not self._gate(api=api): return
        self.server.activity()
        if self.path == '/api/state':
            try: self._json(self.server.store.snapshot())
            except (OSError, ValueError): self._reject(503, 'state_unavailable')
            return
        resource = self.server.assets.get(self.path)
        if resource is None: self._reject(404, 'not_found'); return
        self._headers(200, resource[0], resource[1])

    def do_POST(self):
        if not self._gate(api=True, post=True): return
        if self.headers.get('Transfer-Encoding') is not None:
            self._reject(400, 'body_refused'); return
        content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        length = self.headers.get('Content-Length', '')
        if content_type != 'application/json' or not length.isdigit() or not 0 < int(length) <= MAX_BODY:
            self._reject(400, 'body_refused'); return
        try:
            value = parse_json(self.rfile.read(int(length)).decode('utf-8'))
            if not isinstance(value, dict): raise PetTrickError('invalid_schema')
            self.server.activity()
            command = None
            if self.path == '/api/save':
                if set(value) != {'config'}: raise PetTrickError('invalid_schema')
                self.server.store.save(value['config'])
            elif self.path == '/api/preview':
                if set(value) != {'rule_id'}: raise PetTrickError('invalid_schema')
                command = self.server.store.preview(value['rule_id'])
            elif self.path == '/api/stop':
                if value: raise PetTrickError('invalid_schema')
                command = self.server.store.stop()
            elif self.path == '/api/close':
                if value: raise PetTrickError('invalid_schema')
                self._json({'closed': True}); self.server.stop_async(); return
            else:
                self._reject(404, 'not_found'); return
            snapshot = self.server.store.snapshot()
            if command is not None: snapshot['command'] = command
            self._json(snapshot)
        except PetTrickError as error:
            code = 409 if error.code in ('preview_busy', 'store_busy') else 503 if error.code == 'native_unavailable' else 400
            self._reject(code, error.code)
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
            self._reject(400, 'request_refused')

    def do_OPTIONS(self): self._reject(403, 'method_refused')
    def do_PUT(self): self._reject(405, 'method_refused')
    def do_DELETE(self): self._reject(405, 'method_refused')


def _existing_session(store):
    try:
        with store.lock(): value = store._read('server-session.json')
        if (not isinstance(value, dict) or set(value) != {'version', 'port', 'token', 'pid', 'updated_utc', 'instance_id'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['port']) is not int or not 1 <= value['port'] <= 65535
                or type(value['pid']) is not int or value['pid'] <= 0
                or type(value['updated_utc']) is not int or not -2 <= time.time() - value['updated_utc'] <= 6
                or not isinstance(value['token'], str) or not TOKEN_RE.fullmatch(value['token'])
                or not isinstance(value['instance_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['instance_id'])):
            return None
        origin = 'http://127.0.0.1:' + str(value['port'])
        request = urllib.request.Request(origin + '/api/state', headers={'Authorization': 'Bearer ' + value['token'], 'Origin': origin})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=1) as response:
            state = parse_json(response.read(64 * 1024 + 1))
        if not isinstance(state, dict) or not {'config', 'preview', 'native'} <= state.keys(): return None
        return {'url': origin + '/#' + value['token'], 'port': value['port'], 'reused': True}
    except (OSError, ValueError, TypeError, KeyError): return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_): raise PetTrickError('redirect_refused')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Мурчик · конструктор трюков')
    parser.add_argument('--serve-only', action='store_true')
    parser.add_argument('--ready-stdout', action='store_true')
    parser.add_argument('--open', action='store_true', dest='open_browser')
    args = parser.parse_args(argv)
    store = PetTrickStore()
    with store.lock(): pass
    ownership = FileLock(store.root / 'editor.lock', timeout=0)
    try: ownership.__enter__()
    except PetTrickError:
        existing = None
        for _ in range(8):
            existing = _existing_session(store)
            if existing is not None: break
            time.sleep(.1)
        if existing is None:
            print('editor_already_open', file=sys.stderr); return 2
        if args.open_browser and not args.serve_only: webbrowser.open(existing['url'])
        if args.ready_stdout: print(json.dumps(existing), flush=True)
        return 0
    try:
        server = EditorServer(store)
        server.write_session()
        if args.ready_stdout:
            # This capability is emitted ONLY by the explicit test/launcher flag.
            print(json.dumps({'url': server.url, 'port': server.server_address[1], 'reused': False}), flush=True)
        if args.open_browser and not args.serve_only: webbrowser.open(server.url)
        server.serve()
        return 0
    except (OSError, ValueError):
        print('editor_unavailable', file=sys.stderr); return 1
    finally: ownership.__exit__(None, None, None)


if __name__ == '__main__': raise SystemExit(main())
